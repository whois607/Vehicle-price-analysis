# NOTES — vehicle-price-analysis

Working notes for whoever picks this up next (including future me). Written
2026-08-01. Companion to `README.md`, which covers *how to use* the tool;
this covers *how it is built*, what is done, what is broken, and what to do
next.

Python 3.12 is **not on PATH**. Every command below assumes:

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
```

Run everything from `vehicle-price-analysis/`.

---

## 1. Data model

One SQLite file: `data/vpa.db`. Schema is created idempotently by
`Store.__init__` from the `_SCHEMA` constant at the top of `vpa/store.py`
(`CREATE TABLE IF NOT EXISTS`), so opening a fresh path just works — no
migration step. Live as of writing: **769 vehicles, 877 observations,
15/05/2026 → 28/07/2026, sources: main site 708 / classifieds site 61, 77 ingest
batches.**

### Tables

**`vehicles`** — one row per *physical listing* (an ad), not per car model.
```
key TEXT PRIMARY KEY          -- see "Identity" below
source, listing_id            -- a site label|'quick_add'|..., site ad id or NULL
vehicle_type                  -- 'car' | 'bike'
make, model, variant, year, body_type
state, seller_type            -- 'NSW'.., 'dealer'|'private'
raw_title, url
first_seen, last_seen         -- 'YYYY-MM-DD'
```

**`observations`** — one row per *sighting* of a listing on a capture day.
This is the price history.
```
id INTEGER PRIMARY KEY
key TEXT NOT NULL REFERENCES vehicles(key) ON DELETE CASCADE
capture_date TEXT NOT NULL    -- 'YYYY-MM-DD'
price_aud REAL, price_note TEXT, odometer_km REAL
batch_id INTEGER              -- soft link to ingest_log.id (NOT a declared FK)
```

**`ingest_log`** — one row per ingest, for provenance/debugging.
```
id, ts, source_file, adapter, rows_parsed, vehicles_new, obs_new, obs_duplicate
```

Indexes: `idx_obs_key(key)`, `idx_obs_date(capture_date)`.
`PRAGMA foreign_keys = ON` is set per connection, so deleting a vehicle
cascades its observations.

### Relationships

```
vehicles 1 ──< observations        (one listing, many dated price sightings)
ingest_log 1 ──< observations      (via observations.batch_id, soft)
```

Re-ingesting the same file is idempotent: `Store.ingest` (vpa/store.py)
skips an observation when `(key, capture_date, price_aud, odometer_km)`
already exists (NULL-safe via `IS`), counting it in `obs_duplicate`.
`Store.preview` uses **exactly the same rules read-only** so the import UI's
promise ("what preview says is what commit does") holds by construction.

### Identity — `Record.dedupe_key()` in `vpa/schema.py`

* If the source gave a listing id → `sha1("id|source|listing_id")`.
  Saved listing-site HTML yields `EXA-AD-…`/`EXB-AD-…`; this is the
  reliable path.
* Otherwise a fingerprint over `source, make, model, year, variant, state`
  and odometer bucketed to 5,000 km.
* **Extra rule:** when there is neither an id nor an odometer (classified-ad
  tiles, quick adds), a $500 price bucket is appended, so five same-model
  ads on one day stay distinct. Consequence: an FB re-list at a new price
  reads as a new listing. Unavoidable without stable ids.

### Derived fields — computed, never stored

| field | where | meaning |
|---|---|---|
| `detail_level` | `Store.frame()` (vpa/store.py) | `brief` if `source=='quick_add'` else `full` |
| `variant` (grouped), `variant_stored` | `trims.add_trim_cols()`, called by `Store.frame()` | the trim the analysis groups by; the trim exactly as stored |
| `model_line`, `year_band`, `segment`, `body_segment` | `segments.add_segment_cols()` | analysis groupings |
| `sel_key` | `viewer_xlsx.generate()` | `"Make Model Variant"`, or `"-"` when trimless |

`Store.frame()` is the single analysis dataset — one row per observation
joined to its vehicle. Everything downstream (analyze, report, viewer)
starts there.

### Where things live on disk (tidied 02/08/2026)

```
vehicle-price-analysis/
  vpa/                     the package - the only irreplaceable code
  tools/                   make_launcher.py
  assets/                  app icon (vpa.ico / .svg / -192.png / -512.png)
  tests/
    synthetic/             GENERATED fixtures + out/ report tree - safe to delete
  data/                    PRIVATE, irreplaceable
    vpa.db                 the database
    backups/               vpa_backup_*.db snapshots (Store.backup())
    phone.json             phone sync switches + paired phones (token HASHES)
    phone_stars.json       star requests from phones: waiting, and what became
                           of each (vpa/phoneinbox.py)
    photos.json            listing photo ADDRESSES by ad id (vpa/photos.py);
                           .bak = the version before the last save
    raw/                   the 16 saved .htm captures
  output/                  GENERATED - rebuild with `python -m vpa report --all`
    reports/<segment>/     trend/forecast .html + .png, stats/listings .csv
    reports/plotly.min.js  ONE shared copy for every report page
    exports/               dataset.csv and summaries
    viewer.xlsx            the Excel workbook
    phone/                 the phone copy: snapshot-<version>.json.gz (newest
                           two), meta.json, sel_cache.json (per-selection
                           results reused between builds)
    vpa-app.log            what a console-less launch printed
```

Only `data/` cannot be regenerated. `output/` and `tests/synthetic/` are
derived and are both in `.gitignore`.

**Two storage rules worth keeping:**

1. **Never inline the plotly bundle.** `charts.trend_html()` /
   `forecast_html()` take a `plotlyjs` argument;
   `report.py::_segment_report()` calls `charts.write_plotly_bundle()`
   once per report tree and passes `charts.SHARED_PLOTLYJS` (`"../plotly.min.js"`).
   With `include_plotlyjs=True` every page was **4.7 MB** for ~40 kB of
   chart — 16 files, 74 MB. Now each page is ~10 kB against one shared
   4.6 MB bundle. Still fully offline; no CDN.
2. **`data/raw/`, `data/backups/`, `output/` and `tests/synthetic/` are
   NTFS-compressed** (`compact /C /S`), including the directory inherit
   flag so new files compress automatically. This is transparent — every
   program reads them as ordinary files — and reversible with
   `compact /U /S`. HTML and JS compress about 2:1.

`parse_file()` derives `capture_date` from the file's mtime, so **move
capture files, never copy-then-recreate them** — a reset mtime silently
rewrites the capture date on re-import.

---

## 2. Module map

**Core**
* `vpa/schema.py` — the one normalised record. `Record` dataclass,
  `make_record()` (the only way rows enter the system), `parse_price()`,
  `parse_odometer()`, `norm_make/model/state/seller/year()`,
  `norm_capture_date()`, `guess_vehicle_type()`.
* `vpa/store.py` — `Store` class: `ingest()`, `preview()`, `frame()`,
  `status()`, `backup()`, `price_changes()`.
* `vpa/segments.py` — `add_segment_cols()`, `band_label()`,
  `filter_segment()`, `segment_groups()`, `slugify()`.

**Ingestion** (`vpa/adapters/`)
* `__init__.py` — `parse_file()` (returns records + raw block count),
  `load_file()` (older signature, wraps it), `detect_format()`.
* `htmlpage.py` — saved pages. `parse()` tries JSON-LD first
  (`_walk_ld`/`_ld_to_raw`), falls back to a tag-stripped text pass.
  `_variant_from_name()` derives the trim; `_tracking_supplement()` joins
  per-listing seller/state from the main site's analytics blobs; `detect_source()`
  matches `_SITE_HINTS`.
* `pastetext.py` — `parse()` handles both title-led and **price-led**
  (classified-ad) tile layouts; `split_title()` splits make/model/variant.
* `tabular.py` — `parse_csv()`, `parse_json()`, `map_columns()` (synonym
  matching), `_finish_row()` (backfills make/model from a title).
* `v2db.py` — `load()` imports the old tracker at `DEFAULT_V2_DB`.

**Analysis**
* `vpa/analyze.py` — `prep()`, `bucket_stats()`, `fit_mileage()`,
  `apply_mileage_adjustment()`, `segment_summary()`, `data_caveats()`.
* `vpa/forecast.py` — `forecast_segment()` is the ladder; `_robust_trend()`
  (Theil-Sen + bootstrap), `_ets()`, `_theil_sen()`, `_direction()`.
* `vpa/report.py` — `run_report()`, `_segment_report()`, `export_all()`.
* `vpa/charts.py` — `trend_png/html()`, `forecast_png/html()` (CLI reports
  only; the Excel viewer has its own charts).

**Interfaces**
* `vpa/cli.py` — `build_parser()` + `_cmd_*`: ingest, add, import-v2,
  report, export, status, backup, repair-identities, verify, viewer,
  import-ui.
* `vpa/importui.py` — local server. `parse_payload()`, `commit_payload()`,
  `heatmap_payload()`, `overview_payload()`, `status_payload()`,
  `_running_instance()` (build-stamp check), `_Handler`, `serve()`. The page
  HTML/CSS/JS is the `PAGE` string.
  `overview_payload(db, level="line"|"trim")` backs `GET /api/overview` and
  the page's dataset-at-a-glance tiles, top-models table, movers, latest
  capture and data-health panels. It reuses `add_segment_cols()`'s
  `model_line` and the **same `sel_key` rule as `viewer_xlsx.generate()`**,
  so a name in the app is the same string you pick in the workbook.
  Two inner helpers carry the analysis and matter (added 02/08/2026):
  * `like_for_like(sub, day_a, day_b)` — median change over vehicles present
    on **both** days, matched on `key`. This is the only honest "prices
    moved" number; it returns `None` when fewer than `_MIN_MOVER_PRICES`
    vehicles match.
  * `mixed(a, b)` — `True` when the listing count roughly doubled or halved
    between two captures, i.e. the pool changed. Surfaces as the `mix` /
    `mix_overall` flags and the amber **mix** tag in the table.
* `vpa/explore.py` — `/explore`, the **individual-vehicle** side (added
  02/08/2026). `picker_payload()` builds the make → model → trim tree;
  `detail_payload(db, make, model, variant)` returns headline stats,
  per-capture-day trend **and** supply, monthly buckets, mileage fit,
  forecast, sibling trims, the vehicles themselves, and price movements.
  Extended 03/08/2026 with the workbook's deeper sheets, rendered as four
  tabs so the page stays scannable: `supply` (the Supply sheet's now / average
  / most / fewest / change block), `splits` (seller, state, source and year —
  each **with its median**, the RichStats seller split generalised), `scatter`
  (price vs odometer, plus the fitted curve, the RichStats scatter), and
  `siblings` grown into a full trim breakdown carrying lo/hi and each trim's
  own latest capture. `bymonth`/`adjusted` were already computed and are now
  actually drawn.
  `_select()` is exact-equality at three levels, mirroring
  `viewer_xlsx.Match`. It writes nothing and calls the same engines as
  `report.py` (`analyze.prep/bucket_stats/fit_mileage/data_caveats`,
  `forecast.forecast_segment`), so a median here equals the workbook's.
* `vpa/bulk.py` — `/bulk`, **direct upload** (added 11/08/2026, by request:
  "direct uploads without double checking, bulk amounts at once, for cars and
  trims that are verified as working"). Same adapters, same `make_record`,
  same `Store.ingest` as the Import page — only the preview is missing, which
  is asserted by a test that runs one file through BOTH paths and compares the
  keys. Skipping the human check moves the safety net to either side of the
  write, and none of it is optional:
  **before** — one `Store.backup()` for the whole run, taken before the first
  write (NOTES §6 listed this as un-started; a page that writes without
  showing you anything is where it stopped being optional), plus a refusal of
  anything that is not a listings page (challenge interstitial, empty file,
  empty shell) named per file with its reason;
  **after** — `bulk_runs` records the ingest batch ids and a snapshot of every
  vehicle that already existed, so `undo()` removes exactly the run's
  observations, deletes only the vehicles it introduced, and restores the
  columns `ingest`'s COALESCE filled in on the others.
  Deliberately NOT one transaction: `Store.ingest()` commits per call and is
  the write path shared with the CLI, so wrapping it would mean editing the
  most load-bearing code in the project for a convenience page. Per-file
  commits plus a run-level undo means a run that dies at file 7 keeps files
  1-6 AND stays completely reversible.
  The page's layout is fixed by request and by CSS that says so: fixed-height
  drop zone, status strip, tiles and results box; results scroll inside their
  box rather than growing the page; buttons keep one label and one min-width
  and are disabled rather than relabelled. Verified by measuring every
  control's bounding box across 27 samples during a live upload and 20 during
  an undo — see §8.
* `vpa/turnover.py` — what happens **between** the captures (added
  11/08/2026). Pure engine: no I/O, no writes, no HTML; `explore.py` hands it
  a selection it has already made and filtered. `turnover(sub, full)` returns
  exits, arrivals, carry-over, a per-car-day hazard with its 30-day rate and
  half-life, bracketed time-on-market, the age of the standing stock, and
  whether the cars that left were priced differently from the ones that
  stayed — each measured **against its own day's median**, so a market that
  drifted between two dates cannot masquerade as a discount.
  `by_variant(line)` runs it per trim and pools the line's rate from them.
  `daily[]` feeds the capture chart's composition even when the rate itself
  is refused. Every refusal path returns the **same dict shape** as a
  success, because a caller that KeyErrors on the refusal has been broken by
  the one case the module exists to handle. See §5.3 for the three gates and
  the measurements behind them.
* `vpa/repair.py` — `python -m vpa repair-identities [--apply]`. Splits
  vehicle identities that merged two different cars. `merged_keys()` finds
  them, `_target_key()` decides each observation's correct key (recomputing
  only where there is a reason to, which is what makes it idempotent),
  `plan()` is a pure dry run, `apply()` does the work. **Safety:** takes a
  `Store.backup()` first, runs in one transaction, and refuses to commit
  unless the multiset of (date, price, note, odometer) is unchanged, no
  observation is orphaned, and the vehicle count has not fallen. Any failure
  rolls back. Builds keys through the real `Record.dedupe_key()`, never a
  copy, so migrated and future keys cannot disagree.
* `vpa/analyze.py::price_curve()` / `z_of()` / `live_keys()` — the bell-curve
  engine (added 03/08/2026), shared by Explore and Compare so the two pages
  cannot draw different distributions for one selection.
  * Fitted on **log price**, not price. Used-car prices have a floor at zero
    and a long tail of rare cars; a plain normal would run its left tail
    below $0 and understate the top. Measured on the live XR6 FG MkII trim:
    raw skew **2.85**, log skew **1.02** — better, but still not a tidy bell,
    which is why `marks[]` carries `beyond` (real count) beside `expected`
    (what the curve predicts) for every sigma. On that trim **0** cars sit
    below −2σ where the curve expects 2.8, and **5** above +2σ where it
    expects 2.8. The panel shows that gap rather than implying the curve is
    gospel — and the excess IS the standout set.
  * The 1/x Jacobian is applied when drawing the density in price space.
    Dropping it is the classic log-normal mistake and visibly tilts the curve.
  * `CURVE_MIN_N = 12` distinct cars; below it the curve is refused with a
    reason and the page draws the plain histogram instead.
  * `live_keys()` returns the vehicles in **that selection's own** most recent
    capture (different trims were captured on different days). It is never
    presented as "still for sale": absence from a capture is often a format
    artefact, so the UI says "in latest capture" and nothing stronger.
* `vpa/explore.py::car_payload()` — judging **one physical car** against its
  trim (added 03/08/2026), behind `GET /api/explore/car`. Read-only; nothing
  typed is stored — the subject rides in the URL.
  * **One implementation, two entry paths.** `key=` names a stored car;
    `price/odometer/year/seller/state` describe one the user has only seen
    (normalised by the importer's own `schema.parse_*`/`norm_*`). A typed car
    and a stored car cannot be graded by two different rules.
  * **The peer pool is `analyze.latest_per_vehicle(analyze.prep(sub))`** —
    one price per physical car, subject excluded. `prep()` alone keeps one row
    per car per *month*, which would let a car unsold across seven captures
    vote seven times in the distribution judging someone's purchase.
    `latest_per_vehicle()` is the new 2-line helper in `analyze` (there so a
    rank is never derived twice in two files).
  * **Gates, all counted in distinct vehicles:** `CAR_MIN_PEERS = 8` (below
    it no rank at all, comparables only), `CAR_PERCENTILE_MIN = 20` (below it
    a literal count, never a percentile). The strips are gated on the same
    rule — an earlier build drew a distribution from two cars while refusing
    to rank against them.
  * **The subject never contaminates what judges it.** It is excluded from
    `fit_mileage`'s input, and joins the frame only as one sentinel row for a
    single `apply_mileage_adjustment()` call — which takes the fit as a
    parameter, so the slope provably cannot move. A stored subject therefore
    fits over one fewer row than a typed one; that is correct, not a bug.
  * **The model line's fit is never substituted** when a trim's refuses. The
    Falcon line fit is roughly twice the km penalty of an individual trim's
    because pooling trims of different value makes the slope pick up mix
    rather than wear. `fit_mileage`'s own refusal text is printed instead.
  * Comparables rank on `|ln(odo / subject_odo)|`, matching the log-linear
    model the adjustment uses — 20,000 km means something different at 60k
    than at 260k.
  * The **model-line rank** is reported as plain context, not as a
    mis-picked-trim alarm. It started as an alarm and had to be reframed: a
    trim systematically cheaper than its line disagrees on *every* car in it,
    so the alarm fired constantly and meant nothing.
  * **`_timeline()` — where this car sat over time** (added 04/08/2026), two
    panels merged into `history`, both only for a stored car.
    * `byday` runs over the **trim's** capture days, not the car's, so a day
      it was missing is a visible gap in its line rather than a straight
      segment implying it was there. Daily peer stats **exclude the subject**,
      like every other panel, and the daily rank counts only cars captured
      that day — a car can be 1st of 12 on the day and 3rd of 44 against the
      all-time pool without either being wrong.
    * `rows` is the **audit** view: every sighting names the import that
      recorded it — timestamp, source, batch size — from the new
      `Store.sightings(key)`. It exists because *"I have never seen this car,
      but it claims two earlier captures"* is unanswerable from statistics:
      three sightings from three separate imports on three days is a real
      repeat capture, three from one import is not. `LEFT JOIN` on
      `ingest_log` — an observation that cannot name its import is itself the
      finding, reported as `orphans` (0 in the live database).
    * `unchanged` marks an ad re-captured at an identical price *and*
      odometer, because that is exactly the pattern that makes a genuine
      repeat sighting look like a duplicated row.
    * `Store.sightings()` is the only place batch columns leave the store.
      `frame()` deliberately carries none: no statistic may depend on which
      file a row arrived in.
* `explore._select_multi()` / `explore._apply_filters()` — **pooled trims and
  filters** (added 06/08/2026). `detail_payload(variants=[...])` pools several
  trims of ONE model into one set; `variant=` (singular) still works and is
  folded into the list, so one trim, several trims and all trims are a single
  code path. Pooling is offered only within a make and model — a median over
  the trims of a Corvette is a real number, one over a Corvette and a Falcon
  is a confident number describing nothing.
  * Filters: `year_min/year_max`, `price_min/price_max`, `keyword`. They run
    AFTER selection and BEFORE any statistic, so every number on the page
    describes the filtered set rather than a filtered view of unfiltered
    stats. `keyword` matches variant, raw_title and model, which is how
    "stingray" finds cars whose stored trim spells it differently.
  * **A blank is not a small number.** A year filter keeps rows with no year
    and a price filter keeps rows with no price; dropping them would change
    the vehicle count as a side effect of asking for cars under $50,000.
  * The payload returns `filters`, `kept` and `unfiltered`, and the page
    prints them as badges — a filtered page that looks unfiltered is the
    quickest way to misread a median.
  * The URL carries `variant` repeated plus `y0/y1/p0/p1/q`, so a pooled,
    filtered selection is an ordinary bookmarkable link.
  * A car clicked in the vehicles table is judged against **its own trim**,
    never the pool it was listed in: the row carries `data-v` and passes it
    to `car_payload`, which still refuses above trim level.
* `vpa/starred.py` — **the watchlist** (added 06/08/2026). Star a car in the
  Explore vehicles table; `/api/star` toggles, `/api/watchlist` reports.
  * **A star is an annotation, never data.** Its own table, no foreign key, no
    statistic groups by it. Asserted directly: the whole `detail_payload`
    headline is byte-identical before and after starring a car in it. If it
    lived as a column on `vehicles`, `Store.ingest`'s COALESCE update could
    clear it on a re-import — there is a test for that too.
  * **A car that has gone is the headline, not an error to clean up.** A star
    whose vehicle was deleted or merged away is reported as such, and a car
    absent from its trim's latest sweep is flagged. That is the single most
    useful thing a watchlist can say, so both survive to be shown rather than
    being dropped.
  * **Each car is compared against its OWN trim's capture days**, not the
    database's latest day. Trims are swept on different days; comparing a
    Falcon against the evening you happened to sweep Audis would invent
    movement.
  * The column that justifies the feature is **`vs_trim`** — the car's own
    move minus its trim's move over the same two days. A car holding its
    price while its trim falls has risen, and a bare percentage reports that
    as "unchanged".
  * Rank reuses `explore._rank_in`, so `CAR_MIN_PEERS` gates it exactly as
    the verdict card does; a starred car in a four-car trim gets no rank.
  * `merge.py` re-points stars from an absorbed identity to the survivor
    before deleting it, so folding two identities together does not silently
    drop the fact you were watching one.
  * After a commit the Import page prints what the import did to the
    watchlist, from the same `/api/watchlist` Explore uses — so the two can
    never tell different stories about the same shortlist.
* `vpa/edit.py` — **correcting rows that are already committed** (added
  06/08/2026). Behind `/api/edit/*`, driven from the **Fix data** button on
  the Explore car card; `python -m vpa edit` lists and undoes.
  * Ingestion is append-only and reproducible from the source files. A hand
    edit is neither, so it carries the heaviest protocol here: dry run →
    backup → one `BEGIN IMMEDIATE` transaction → invariants → COMMIT or full
    ROLLBACK, and **every change written to the new `edits` table with its
    old value**, which is what makes an edit undoable rather than merely
    regrettable.
  * **Typed values go through the importer's own normalisers** (`parse_price`,
    `parse_odometer`, `norm_year/state/seller`, `norm_capture_date`), so you
    cannot store a value the importer would have refused, and `$18,990` means
    the same on both pages. A future capture date is refused by name — it
    silently breaks every trend that orders by it.
  * **The preview is the write.** `apply()` re-runs `plan()` and compares a
    fingerprint of what was shown; if another tab committed an import in
    between, the save is refused rather than applied to rows nobody approved.
  * **Identity is never re-keyed.** Editing make/model/variant corrects what
    the row SAYS; `vehicles.key` is the fingerprint taken when the row
    arrived. Re-deriving it would detach the vehicle from its own
    observations. Splitting and merging identities remain `repair.py` and
    `merge.py`.
  * Invariants after the write: totals must equal the ones the preview
    promised, no sighting may point at a missing vehicle, and **no vehicle
    may be left with no sighting** — deleting a car's last sighting therefore
    deletes the car, which is warned about in the plan.
  * `rename_variant` is the bulk case, aimed squarely at the 35 split trims
    the Data check reports. It plans one recorded change per vehicle, so a
    rename of 29 cars is 29 undoable rows, not one blanket UPDATE.
  * Schema migration is additive and idempotent: opening an existing database
    adds `edits` and touches nothing else (verified on a copy of the real
    2,281-vehicle database — integrity ok, totals identical).
* `vpa/adapters/challenge.py` — **recognising a bot-protection page saved
  instead of a real one** (added 06/08/2026). A site that decides a request
  looks automated serves an interstitial with HTTP 200; Ctrl+S then saves a
  few hundred bytes that look like a capture and hold nothing. Measured on
  `data/raw/`: **4 of 49 saved pages were challenge pages**, each exactly
  777 bytes — the broad `Ford cars`, `Audi cars` and `Mercedes-Benz cars`
  searches, plus one narrow `Ford Falcon FG MkII Sedan`.
  * **The marker is the interstitial, not the vendor.** Every main-site page
    loads its bot-protection vendor's ordinary tag (tag script); only the
    challenge contacts `captcha-delivery.com`. Keying on "datadome" flags all
    49. Verified: 4 hits, 0 false positives across the whole folder.
  * `parse_payload` refuses such a file outright and names the system that
    fired, because "0 listings" sends you to the parser instead of the file.
  * It **identifies only**. Getting past a challenge is off limits — the fix
    is always to re-open the search in a normal browser session, complete the
    check the site shows, and save again.
* Import accepts **several files at once** (06/08/2026), which is what makes
  many narrow captures practical. Each file keeps ITS OWN capture date from
  its own mtime — a folder saved across three evenings is three capture days,
  and collapsing them onto one would invent price history. The date box is
  disabled while more than one file is queued, since it cannot speak for all
  of them. The preview breaks the row count down per file and prints any
  refused file inline, so a page that produced nothing is visible rather than
  averaged away by the ones that worked.
* `vpa/adapters/titles.py` — **one splitter, both capture paths** (added
  04/08/2026). `strip_tail()`, `is_badge_line()`, `split_make_model()`,
  `take_model()`. Imported by BOTH `pastetext` and `htmlpage`, for the same
  reason `analyze` is shared by the app, the CLI and the workbook: before
  they shared it they disagreed, and one physical trim reached the database
  under three names (`420i M Sport` / `420i M Sport Auto R` /
  `420i M Sport F32`).
  * **The badge line.** A results card puts the model on the title line and
    the trim on the line below it (`2007 Lexus LS` / `LS460 Version U I
    Package Auto R`). The paste path never read the second line, so every
    Lexus LS on a results page — LS460, LS500, LS500h, F Sport, Sports
    Luxury, three Version packages — parsed as one trimless `Lexus LS`.
    `is_badge_line()` is written as ordered REJECTIONS (price, odometer,
    engine spec, body word, state, `Dealer used • VIC`, UI furniture,
    year-led next title, tile location); anything not clearly a badge
    is refused, because a wrong badge invents a trim while a missed one only
    leaves the variant blank, which is what it did before.
  * **The badge line makes the model exact.** Whatever remains of the title
    after the make IS the model, so `4 Series`, `Grand Cherokee` and
    `Land Cruiser Prado` need no word list. `take_model()`'s list is only a
    fallback for when the badge shares the title line.
  * **The tail rule, in one sentence:** take the trailing run of descriptor
    words, start the cut at the first GEARBOX-or-SPEED word inside it,
    extend left over `Spts`/`Dual Clutch`, and remove that. The starting
    point is the whole trick — `Golf R Auto 4` and `Auto R` both end in a
    descriptor run, and cutting at the run's START turns the first into
    plain `Golf`. Speed is included so `8 Sp Automatic R` goes too.
  * **Never stripped:** `Sport` AND `Sports` (badge words — Lexus `IS200
    Sports`, `LS500h Sports Luxury`, BMW `M Sport`; the main site writes the
    gearbox as `Spts Auto`, so only the abbreviation is a descriptor);
    `quattro`, `xDrive`, `4MATIC`, `AllGrip`; and every series/chassis code
    (`FG X`, `FG MkII`, `F32`, `PX MkIII`, `GUN126R`, `W205`) — the whole
    Falcon dataset is separated by exactly such codes.
  * **Also removed**, because each duplicates a column and otherwise splits
    one trim: `MY##` model-year codes (on either side of the gearbox), and
    body styles a listing appends AFTER the gearbox on utes (`XR6 Turbo FG X
    Manual Super Cab`). A LEADING gearbox word means the badge field was
    blank (`Auto quattro MY23` → `quattro`), and a badge that is entirely
    descriptor (`Auto`, `Auto R`) stores **no** variant — a base-model card
    is a car with no trim, not a car whose trim is "Auto".
  * **Deliberately unhandled**, guessing being worse than leaving alone:
    Audi's `S tronic` (would need `S` strippable, and `S` is a badge —
    `A45 S`, `C63 S`, `Cooper S`); Toyota's parenthesised `SR5 (4x4) Auto`,
    where the drive sits inside the badge; chassis codes as a separate
    column; and linking `Mercedes-AMG A45` rows to `Mercedes-Benz A-Class`
    rows.
* `verify._check_trims()` — the **"one trim, one name"** data check. Reports
  variants that become identical once `strip_tail` is applied, and variants
  differing only in case. It never renames anything: a rename is a data
  change. On the real database it found 35 split trims covering 351
  vehicles, including an Audi RS3 split six ways by model-year code alone.
* `vpa/adapters/detailpage.py` + `explore.identify_payload()` — **find one
  specific car by uploading its saved ad page** (added 03/08/2026), behind
  `POST /api/explore/identify`. Read-only: uploading an ad to ask "how does
  this compare" must never quietly import a listing.
  * A **details** page (Ctrl+S on the ad itself) carries one analytics
    `metaData` object stating everything at once — ad id, make, model, badge,
    state, seller, year, odometer, price and **colour**. A search-results
    page states none of that per listing, which is why this is a separate
    parser from `htmlpage`'s `tracking/item/networkId` clusters.
  * **Colour exists only here.** It cannot be back-filled onto stored
    vehicles, so it is displayed as read off the file and explicitly labelled
    as not being in the database. No schema change.
  * **Colour is NOT read from `metaData`** (corrected 04/08/2026). On the real
    page the ad displays *Exterior colour Kinetic* and *Interior colour
    Black*, and the two analytics blobs disagree with each other: the
    enquiry-form `metaData.colour` says `Black` — the **interior** — while
    page-level `tracking/page/details/colour` says `kinetic`. Taking the
    obvious-looking analytics field reported a Kinetic Blue car as black.
    `_colours()` now reads the ad's own **labelled spec rows** (what a human
    reading the page sees), falls back to page-level tracking, and never
    consults `metaData` — better no colour than the seat trim as the paint.
    Exterior and interior are returned separately and shown separately.
  * Manufacturer paint names are not CSS colours, so the swatch is filled
    only for words a browser understands and is neutral grey otherwise —
    `background:kinetic` renders as nothing at all.
  * Matching runs strongest-first and names the rule that fired: the site's
    ad id (conclusive), then `Record.dedupe_key()`, then merge.py's evidence
    rule (same trim + exact price + exact odometer, and only when the
    odometer is a reading rather than a whole thousand). No match is not a
    failure — the typed-car form is prefilled from the file instead.
  * `is_detail_page()` counts distinct `networkId`s **case-folded**. The real
    file emits `EXA-AD-20672993` 15 times and `exa-ad-20672993` 10 times; a
    case-sensitive count reads that as two vehicles and rejects the page.
    A search page carries 5-10 genuinely different ids, so folding case
    cannot make one look like a details page. Verified against all 26 files
    in `data/raw/`: exactly one is a details page.
* `vpa/merge.py` — `python -m vpa merge-duplicates [--apply]`. Folds together
  identities that are provably ONE car. The opposite direction to `repair.py`
  and therefore far more dangerous: a wrong split costs a link, a wrong merge
  welds two cars' prices into one history irreversibly. Seven conditions must
  ALL hold or the group is reported and left untouched — same source/make/
  model/variant, a shared EXACT price and odometer, that odometer not a whole
  thousand (150,000 km is what people type when they mean "about"), year and
  state not conflicting, exactly one real site ad id (`EXA-AD-`/`EXB-AD-`,
  which becomes the surviving key), no two prices landing on one capture day,
  and **the combined odometer never running backwards through time** — a
  physical check no amount of matching metadata can fake.
  Same safety protocol as `repair.py`: backup first, one transaction, the
  (date, price, note, odometer) multiset compared before and after, vehicle
  count must fall by exactly the number folded in, colliding keys must not
  grow, rollback on any failure. Applied 03/08/2026: **835 → 729 vehicles,
  1,011 observations and $33,229,610 untouched**, and multi-day price
  histories rose 145 → 166 because it reconnected cars split across capture
  eras (NOTES bug 3). 63 groups were held back: 58 rounded odometers, 5
  without a single site ad id.
* `vpa/ui.py` — the shared page chrome (added 03/08/2026): `PAGES`, `ICONS`
  (inline SVG, no font, no CDN), `nav()`, `action()`, `CHROME_CSS`,
  `CHROME_JS`, and `chrome(page, active, actions)` which substitutes
  `__NAV__` / `__CHROME_CSS__` / `__CHROME_JS__` into a page template. Every
  page previously hand-rolled its header, so the same destination was a link
  on one page and a button on another, **no page marked which one you were
  on**, and `Shut down` was visually identical to `Quick add`. Now:
  navigation tabs (filled + `aria-current` when active) are visibly a
  different class of thing from actions (outlined), and Shut down is styled
  as a danger action, sits last, and is behind a confirm. It also exists on
  all four pages — before, only the import page could stop the app.
  **Four tabs, not six (27/09/2026).** Import, Bulk and Quick add were
  three nav tabs for one job; they are now the three modes of **Add data**
  (`ui.MODES`, drawn by `modes()` into `__MODES__`). Each mode keeps its own
  page and URL - `/add` (also `/import`), `/bulk`, `/quick` - so nothing
  about their code or their fixed layouts changed; the nav marks Add data on
  all three. The dataset panels, data check and workbook rebuild moved off
  the import page onto **Overview** (`importui.OVERVIEW`), which is now `/`:
  the home page reads the data, and adding it is one tab over. On Explore
  the projection and per-car price changes became tabs - they were the only
  analysis on the page in a card of their own.
  **One stylesheet (27/09/2026).** `CHROME_CSS` is now the whole base: colour
  tokens on `:root` (`--accent`, `--good`, `--bad`, `--warn`, `--line`, …),
  type sizes, cards, buttons, inputs, tables, tiles, tags, chips, tabs and
  the help panel. Each page used to carry its own copy and they had drifted
  into five page widths, three button styles and two grey scales. Pages put
  `__CHROME_CSS__` FIRST in their `<style>` and add only their own rules
  after it; element rules are wrapped in `:where()` so any page rule wins
  without a specificity fight. The header is now identical on every page
  (tabs, Excel viewer, How to use, Shut down); a page's own actions go in
  `__ACTIONS__` beside its title. `CHROME_JS` owns the database line
  (`#dbstat`, db path on hover), the Excel viewer button, and the
  workbook-stale flag, which is a dot ON that button rather than a banner:
  the banner pushed every page down and would have moved every control on
  Bulk. `vpaTicks()`/`vpaAxMoney()` put chart axes on round numbers ($20k,
  $40k) instead of 0/25/50/75/100% of the range ($30,794, $53,627).
* `vpa/compare.py` — `/compare`, the **many-vehicles-at-once** side (added
  03/08/2026). The workbook's **Comparison** and **Models** sheets brought
  across. `catalogue_payload()` lists every selection that exists at all three
  levels with its counts and median (searchable, sortable — the Models sheet);
  `compare_payload(db, labels)` returns up to `MAX_SELECTIONS` (6) columns of
  the stat rows declared in `CMP_ROWS`, which mirror `viewer_xlsx._CMP_ROWS`
  row for row **except the `turnover` group** (see below), plus a shared month
  axis for the overlay chart.
  **Turnover, added 11/08/2026** and computed by `vpa/turnover.py` on the
  selection *before* the live-only filter — filtering to the newest capture
  first would delete exactly the cars that left and report zero. Three
  places carry it: seven `CMP_ROWS` stats, the supply line chart (each point's
  tooltip names every part of that day's count, and a capture sharing no cars
  with the one before it is drawn **hollow** so a scope change cannot read as
  a cliff), and a new **what changed between the captures** panel — grouped
  bars diverging from zero, arrivals up and exits down, one colour per
  selection, with pairs that are not comparable faded rather than dropped.
  A line chart cannot show this on its own: supply holding flat at forty is
  the same picture whether it is the same forty cars or forty out and forty
  in, and those are opposite markets.
  **These seven rows are deliberately NOT in the workbook's Comparison
  sheet.** They are the one block a formula cannot reproduce — recognising
  the same listing across capture days, splitting the days where that
  recognition fails, and rating exits per car-day are set operations over a
  selection the reader picks in the dropdown *after* the file was built. The
  sheet's row 33 note points at the app instead of printing a number it
  cannot stand behind; same reasoning as the de-duplication note under its
  month block (§5.12).
  `selection_index()` / `resolve()` turn a typed label into a selection using
  the same disjoint three-level rule as `viewer_xlsx.Match`, so a label typed
  into the workbook's row 3 and the same label here pool the same rows.
  Rank and "vs cheapest" are relative to the columns on screen, matching the
  workbook's `RANK` over `$B$10:$G$10`. Selections live in the URL
  (`?sel=…&sel=…`), so a comparison is bookmarkable, and Explore's **Compare**
  link carries the current selection plus its three nearest trims.
* `vpa/verify.py` — `python -m vpa verify`, `GET /api/verify`, and the import
  page's **Data check** panel (added 03/08/2026). Everything else in the
  project computes; this cross-checks. Seven checks, each comparing two
  independently produced answers or asserting a property no correct database
  can violate: `sqlite` (integrity + FK), `orphans`, `engine` (six totals in
  SQL vs through `store.frame()` + pandas), `identities` (two prices on one
  capture day), `workbook` (`viewer.xlsx`'s `_Data` sheet vs the live DB —
  the only artefact that cannot recompute itself), `values` (prices,
  odometers, future dates), `duplicates`. `ok` is False only for a real
  failure, so a warning does not gate `vpa verify`'s exit code.
* `vpa/quickadd.py` — `/quick`. `price_stats()`, `pct_change()`,
  `stat_changes()`, `models_payload()`, `stats_payload()`, `commit_payload()`.

### Which page owns which question (settled 02/08/2026, extended 03/08/2026)

| page | question | writes? |
|---|---|---|
| `/` (importui) | dataset-level: how much do I have, is it healthy, do the numbers check out, what landed last, which models am I richest in | **yes** — the only writer |
| `/explore` (explore) | one vehicle in depth: what is this trim worth, how does its price move, what does mileage do to it, who sells it, which cars make up the number | no |
| `/compare` (compare) | many vehicles against each other: which is cheaper, which has more data, which is moving — and what else could I pick | no |
| `/quick` (quickadd) | jot prices fast for one model | yes |
| `output/viewer.xlsx` | the same numbers, offline and yours to restyle | rebuilt on demand |

Adding a fourth page rather than a fourth section of Explore was deliberate:
"what is this worth" and "which of these is better value" need different
layouts (one selection deep vs six selections wide) and would have fought each
other on one page.

**Freshness.** Every web page recomputes from SQLite per request, so none can
go stale. The workbook is the exception — a file frozen when built — so
`status_payload()` returns `data_version` (`maxIngestId:observations:vehicles`)
and `workbook.{built,stale}`. Open pages poll `/api/status` on focus and every
20 s; a changed `data_version` triggers a silent reload, and a stale workbook
raises an amber banner on both pages. Committing refreshes overview, heatmap,
chart and status together.
* `vpa/viewer_xlsx.py` — `generate()` builds `output/viewer.xlsx`.
  Backend writers `_sheet_data/_sheet_forecasts_data/_sheet_picks/
  _sheet_listings/_sheet_chart`; view writers `_view_picker/_view_trend/
  _view_supply/_view_comparison/_view_richstats/_view_forecasts/_view_models`;
  `_sheet_sandbox()` creates MyView once. `Match` builds selection formulas;
  `_name()` defines workbook named ranges; `_keep_style()` implements the
  styling-preservation rule.
* `tools/make_launcher.py` — `draw_icon()`, `make_shortcut(where)`. One-time,
  run via `Setup VPA.bat`. Draws the brand mark (the nav logo's rising line on
  its blue gradient tile) as `assets/vpa.ico` (9 sizes; small ones get a
  bolder line), `vpa.svg` (browser tab) and full-bleed `vpa-192/512.png`
  (phone home screen, for later). Writes `VPA.lnk` to the Desktop (asked of
  Windows - OneDrive moves it), the Start menu and the project folder, and
  removes the old `Vehicle Import.lnk`.
* App icon in the browser: `ui.HEAD_LINKS` goes in every page's head via
  `chrome()`; importui serves `/favicon.ico`, `/icon.svg`, `/icon-192.png`,
  `/icon-512.png` from `assets/` (`_STATIC`). Tab titles read "Page · VPA".
* Console-less launch: `serve_quiet()` (used by `main()` and the CLI) - under
  pythonw, output goes to `output/vpa-app.log` (restarted past 1 MB) and a
  failed start shows a Windows message box instead of doing nothing.

---

## 3. What's done

All of the following is built **and** verified by an automated suite (see §7).

* **Ingestion** — CSV, JSON, saved HTML (JSON-LD + text fallback), pasted
  text (title-led and classified-ad price-led), manual `vpa add`, and v2 import.
  Messy input handled: `$18,990` / `Negotiable` / `98k` / `FORD` /
  `New South Wales` / DD-MM-YYYY.
* **Store** — append + dedupe with price history preserved; idempotent
  re-ingest; `vpa backup`.
* **Analysis** — segment stats with sample sizes, mileage adjustment gated
  at 20 odometer'd rows, honest caveat strings.
* **Forecasting** — three-rung ladder (insufficient → robust trend → ETS),
  always with 80% intervals, method and n.
* **Import UI** (`vpa import-ui`, or the desktop shortcut) — preview before
  commit, inline flags, editable cells, capture-day heatmap (auto-scaled to
  the busiest day), cumulative-collection line chart, in-app help.
* **One car vs its trim** (`/explore`, the verdict card) — click a row in the
  vehicles table or a point on the scatter, or type a car you have only seen.
  Raw rank paired with the mileage-adjusted rank, two position strips,
  expected-price-for-its-kilometres, six nearest comparable ads, like-for-like
  medians, the listing's own history, and an optional target price that never
  leaves the browser.
* **Explore** (`/explore`) — one vehicle in depth: capture-day trend and
  supply with its summary block, a **price-projection chart** (history solid,
  forecast dashed, 80% interval shaded and fanning out from the last observed
  point), a five-tab analysis card (monthly trend with the mileage-adjusted
  overlay; price distribution histogram plus a range-and-median bar per trim;
  price-vs-odometer scatter with the fitted curve; seller/state/source splits
  each with a median plus a median-by-model-year bar chart; and a full trim
  breakdown), movements, and the individual cars.
* **Compare** (`/compare`) — up to six selections side by side across ~27
  stat rows, best-in-row shading, deep links into Explore, a searchable
  catalogue of every selection that exists, and **six charts**: median by
  month with each selection's projection carried forward (interval bands
  drawn for one or two selections, since six would overlap into mud),
  range-and-median bars, any single stat as bars via a dropdown, supply per
  capture day, price against odometer colour-coded per selection, and the
  dealer/private split.
* **Data check** (`vpa verify`, or the panel on the import page) — seven
  cross-checks including SQL vs the analysis frame and the Excel workbook vs
  the live database.
* **Quick Add** (`/quick`) — keyboard-first price entry, live stats vs the
  model's history.
* **Excel viewer** (`vpa viewer`) — backend `_` sheets + refreshed views +
  untouched `MyView`; cascading Picker; Trend / Supply / Comparison /
  RichStats / Forecasts / Models. Selections are offered at all three levels
  (make, model line, trim), matching the app exactly.
* **Launcher** — icon'd Windows shortcut, no console, single instance,
  auto-replaces a stale instance after a code change.
* **Phone sync, laptop side** (`/phone`) — a read-only, compact copy of the
  data kept current automatically, handed to paired phones by a separate
  listener that cannot reach the database.
* **Phone app** (`vpa/phone_app/`, served by the listener) — Home, Find
  (advert link, browse, search a range), every make/model/trim's Explore
  view, the car card, Price check, Compare, Starred; read-only, offline,
  auto-syncing on open. Same numbers as the laptop (check_phone_app).
* **Listing photos** (`vpa/photos.py`) — the photo addresses in saved pages,
  shown on the laptop (car card gallery, car table, comparables, starred)
  and the phone (car lists, swipeable gallery, kept offline once seen).
* **Light and dark** — every laptop page and the phone app; follows the
  system, or a choice kept in that browser.
* **Stars from the phone** — the one change a phone can make; queued on the
  phone offline, applied by the laptop to the starred table only.
* **Expected asking price** — Price check without a price gives the range
  cars with those details ask (laptop and phone, identical).

---

## 4. In progress

**Nothing is mid-flight.** Every feature listed above is complete and
green; there is no half-finished branch or disabled code path. The items in
§6 are un-started.

---

## 5. Known bugs and limitations

1. ~~`requirements.txt` is missing `openpyxl`~~ — **fixed 02/08/2026.**
   `openpyxl>=3.1` and `scipy>=1.11` are both declared now; both are
   imported directly (`vpa/viewer_xlsx.py`, `vpa/forecast.py::theilslopes`)
   and were only present here as transitive installs.
2. ~~No `.gitignore`~~ — **added 02/08/2026**, covering `data/`, `output/`,
   `tests/synthetic/`, `*.lnk`, `__pycache__/`.
3. ~~**Turnover/"how many sold" is not measurable on most history.**~~ —
   **measurable at TRIM level as of 11/08/2026** (`vpa/turnover.py`); still
   not measurable at model-line level, and the reason is the interesting
   part. The original finding stands: identity breaks across capture eras
   (v2 `BASE-`/`IMG-` ids → real `site` ids → the 28/07 batch which was
   pasted text with *no* ids), so a naive cross-day join fails. What changed
   is that the failure is **measurable per pair of captures** rather than
   global, so it can be gated instead of being a blanket refusal.

   Measured on the live database. **Model line Ford Falcon**, consecutive
   captures: `03/08 → 05/08` shares **0** of 134 and 52 cars, `05/08 → 06/08`
   shares **0** of 52 and 66 — two sweeps of genuinely different cars (05/08
   caught AU/BA-era Falcons, 03/08 was FG-era). **Its trims over the same
   days**: 0.82–1.00 of the smaller capture carries over, with the worst pair
   anywhere at 0.45. So one dataset supports a trim-level rate and refuses a
   line-level one, and `turnover.spans()` is what tells those apart — the
   line's rate is instead **pooled from its trims** (`by_variant`), which
   avoids the scope problem rather than averaging through it.

   Three gates, each seeded as a regression in `tests/check_turnover.py`:

   * **cohort** — only keys carrying a site-issued ad id
     (`merge.SITE_ID_PREFIXES`). `IMG-`/`BASE-`/fingerprint identities are
     excluded and counted aloud, never diluted into the rate.
   * **spans** — a pair sharing under `MIN_LINK` (0.25) of the smaller
     capture ends the span. Without this, the Falcon line reports a ~100%
     clearance for a fortnight nobody bought anything in.
   * **scope, asymmetrically** (`_scope()`) — a capture much bigger than its
     neighbour inflates *arrivals* (the search widened: 66 cars on 06/08,
     168 on 10/08, so "113 arrivals in four days"), while a much smaller one
     inflates *exits*. Opposite failures, opposite treatment: a wider net
     that still misses a car is *stronger* evidence it has gone, so widened
     pairs keep their exits and drop their arrivals, and narrowed pairs do
     the reverse. One blanket rule would throw away half the evidence.

   Rates are **exits per car-day**, not per capture, because the real gaps
   run 1 to 22 days; that is also what makes two trims on different capture
   rhythms comparable at all. Time-on-market is reported as a **bracket**
   (floor = last − first, ceiling = next capture − the capture before first),
   because reporting only the floor prints "listed 0 days" for every car
   seen once. The page filters are handled by passing the *unfiltered*
   selection alongside: a car that repriced out of "under $27,000" is vetoed
   rather than counted as a delisting.

   **The vocabulary is still not "sold"** — a listing can vanish by selling,
   being withdrawn, expiring, or being re-listed under a new id, and a test
   asserts the word appears nowhere in any verdict or note. Capturing with
   **Ctrl+S saved HTML** over a **consistent search** is still what makes
   any of this work.
3b. ~~Merged identities: 14 keys hold two different cars~~ — **fixed
   03/08/2026.** A key showing two prices on the *same* capture day cannot
   be one car. Two causes: the id-less fingerprint bucketed odometer at
   5,000 km, so two cars of the same year/variant/state with similar mileage
   collapsed (318,200 km and 315,000 km both landed in bucket 63); and
   v2-era `IMG-` ids are image-derived and not unique per listing.

   `Record.ODO_BUCKET_KM` is now **500 km**, chosen by measuring both failure
   modes against the real data rather than guessing (see the comment in
   `vpa/schema.py`): 5,000 km left 11 merged keys, exact odometer broke a
   genuine link where a car was driven 120 km between captures, 500 km did
   neither. `vpa/repair.py` migrated the identities already stored —
   769 → 785 vehicles, 877 observations untouched, 0 merged keys left, and
   all 78 multi-day histories plus all 19 real price movements preserved.
   `analyze.collided_keys()` and the exclusions in
   `explore.detail_payload` / `importui.overview_payload` stay in place as a
   permanent guard for any that slip through.
4. **Excel must be closed before a refresh.** `generate()` can't write a
   locked file; the CLI and the app both say so clearly. OneDrive sync can
   also lock it briefly.
5. **Windows-only launcher.** `Setup VPA.bat`, the `.lnk`, `pythonw`
   and `os.startfile` (used to open the workbook) are Windows-specific. The
   CLI and `python -m vpa import-ui` work anywhere.
6. **Classifieds-site rows carry no trim and no odometer**, so they land under the
   plain model line and never contribute to mileage-adjusted stats.
6b. **Early capture history is trim-narrow — a sparse trim view is not data
   loss.** (Reported as "a lot of historical data has been destroyed",
   02/08/2026; investigated, nothing was lost.) The dataset has 9 capture
   days, but the May/June sweeps only covered **five** trims — `XR6 FG MkII`
   (7 days), `XR6 Turbo FG MkII`, `G6E Turbo FG X`, `G6E Turbo FG MkII`
   (6 days each) and `XR6 Turbo FG X` (4 days). The Falcon-wide sweeps only
   happened on **06/07 and 28/07**, so every other trim has at most 2 days.
   `Match` (vpa/viewer_xlsx.py) matches **exact equality** on three disjoint
   columns, so picking a trim (`vpa_key`) shows only that trim's days, while
   picking the model line (`vpa_line`, e.g. "Ford Falcon") pools all trims
   and shows all 9 days and 789 rows. `_Data` always holds all 877 rows,
   which is why the backend looks fuller than the view. The capture-day
   header now prints "appears on N of M capture days" so a narrow selection
   is distinguishable from breakage at a glance.
7. **Trim vocabulary is main-site-canonical** (decided 06/07/2026). Rows from
   other sources with different variant text create separate trim entries.
   The Models sheet is where you spot near-duplicates.
8. **Forecast coverage is thin** — real history is only a few monthly
   buckets, so most segments correctly report "insufficient". Expect
   genuine forecasts once ~3+ months of regular capture accumulate.
9. **PDF export from Excel COM hangs on this machine** (`PageSetup` throws,
   `ExportAsFixedFormat` never returns) — almost certainly no printer driver
   installed. Only affects *my* screenshot tooling, not the product.
10. **`_Chart` sheet shows `#N/A` by design** — the scatter needs them to
    skip non-matching rows. The error scan in `tests/check_viewer.py`
    excludes that sheet deliberately.
11. ~~**~186 surplus identities: cars counted twice**~~ — **78 remain, by
    choice, 03/08/2026.** `vpa/merge.py` folded 106 of them together after a
    rehearsal on a copy; see §2. What is left is deliberate: 58 groups whose
    linking odometer is a whole thousand (two different cars genuinely can
    share "150,000 km" and a price) and 5 with no single site ad id to
    survive onto. `vpa verify` reports the remainder every run. Original
    finding below, kept because the diagnosis still explains the cause.

11b. **~186 surplus identities: cars counted twice** (found 03/08/2026 by
    `vpa verify`, not fixed — deliberately). Two identities carry the same
    car at the same price and the same odometer *to the kilometre*.

    **The first version of this check reported 26 and was wrong.** It grouped
    by `capture_date`, and this failure mode arises precisely *across*
    capture sessions: the 28/07 sweep was pasted text (no ad id, state
    present) and the 03/08 sweep was saved HTML (ad id present, state
    absent), so the two copies of one car never share a capture day.
    Requiring one excluded exactly the case being looked for. Corrected rule:
    group on make+model+variant+price+odometer, ignore date, ignore state and
    year (one copy routinely records what the other does not). Measured after
    the fix: **153 groups, 186 surplus copies, 0 conflicting on state or
    year, and 138 of 153 showing the mixed has-id/no-id pattern.**
    `tests/check_verify.py` now seeds a cross-day duplicate as the regression
    guard. The dedupe rule's blind spot: `Record.dedupe_key()` uses the ad id when there
    is one and the fingerprint otherwise, so a car captured once from saved
    HTML (which carries an id) and once from pasted text (which does not) can
    never collide. The other 3 carry two different image-derived `IMG-` ids.
    Effect: supply is over-counted by ~3% and the doubled prices get double
    weight in a median.
    **Not auto-fixed on purpose.** Merging is the dangerous direction —
    `repair.py` only ever splits, because merging two identities that turn
    out to be two cars corrupts a price history permanently, while a split
    only costs a link. A merge pass would need the same rehearse-on-a-copy
    treatment plus a rule for which id survives. The check reports it; the
    decision is yours.
12. **Two bases are in play, and the app now says which.** Counts and price
    stats are over every priced sighting; monthly medians, the mileage fit
    and the forecast use `analyze.prep()`, which keeps one row per vehicle
    per month so a car re-seen weekly cannot dominate. The workbook's month
    block can only do the former (a formula cannot de-duplicate), so it can
    differ slightly from `/compare`'s. Both sides now carry a note saying so;
    everything else agrees exactly.

---

## 6. Next steps

In the order I'd do them:

1. **Sharing prep** (asked about 01/08/2026): package the folder minus
   `data/` and `output/`; the recipient runs `pip install -r
   requirements.txt` then `Setup VPA.bat`, which rebuilds the
   shortcut against *their* Python path and folder. Optionally add a
   `--db` flag to `tools/make_launcher.py::make_shortcut()` so two people
   can share one code copy on one machine.
2. **Keep capturing with saved HTML, over a search you do not change.**
   Still a habit change and not code, and now it is the single highest-value
   one: `vpa/turnover.py` rates 9 of Ford Falcon's 88 trims and refuses the
   other 79 — **39 for having only one usable capture and 37 for carrying no
   site ad id**. Both are fixed by the same habit. Re-running the *same*
   saved-HTML search weekly is worth more than a broader one-off sweep,
   because a sweep that changes shape is what `_scope()` has to throw away.
3. **Decide what to do about the 26 double-counted cars** (bug 11). A merge
   pass in the style of `repair.py` — rehearse on a copy, back up, one
   transaction, refuse to commit unless every price survives — is the shape
   it would take. Needs a rule for which identity wins and a same-day
   price/odometer match strict enough that two genuinely identical cars are
   never merged. Until then `vpa verify` reports it every run.
4. Deferred feature ideas, none started, roughly by value:
   * a **deals view** — listings priced well under their trim median
     (`Store.frame()` + `analyze.segment_summary()` have everything needed;
     `/explore` now has the per-vehicle table this would slot into);
   * **year-band as a fourth picker level** in the Excel viewer;
   * **automatic backup before each ingest** (`Store.backup()` already
     exists; call it from `_cmd_ingest`);
   * move the forecast bootstrap off the `vpa viewer` critical path if
     generation time ever becomes annoying (currently fine).

---

## 7. Verification

```powershell
& $py -m tests.run_all          # all 26 suites, six at a time, ~1.5 min
& $py -m tests.run_all --real   # + the phone app on a read-only copy of the real data
& $py tools/equiv.py            # committed code vs working tree, on the real data
& $py -m vpa verify             # cross-check the real database (seconds)
```

The table below is the original core; `tests/run_all.py` lists every
suite in one line each, and README "Changing the code safely" is the
procedure.

| suite | what it proves |
|---|---|
| `tests/selfcheck.py` (31) | full pipeline on synthetic messy data with known ground truth: parsing, dedupe, aggregation, mileage fit, forecast intervals bracketing truth, and the "too little data" refusal |
| `tests/check_repair.py` (11) | seeds a legacy database through the old 5,000 km rule, then proves the migration keeps every price byte-for-byte, clears merged identities, never fragments a car driven 300 km between captures, leaves healthy ad-id vehicles alone, is idempotent, and **rolls back completely when an invariant fails** |
| `tests/check_explore.py` (43) | selection levels pool exactly the right rows; headline and monthly numbers equal `analyze` computed independently; the supply summary matches the series it summarises; splits carry medians and sum to the selection; the scatter holds every priced row with an odometer; the price histogram bins every priced row into contiguous gap-free bins and is withheld entirely when there are too few; a real re-price is reported while a deliberately merged identity is flagged and excluded; exploring writes nothing; `data_version` moves when an import lands; several trims pool to exactly the sum of the trims alone and stay inside the model; a pooled median equals pandas over the pooled rows; year / price / keyword filters narrow and report what they kept, compose with pooling, and never silently drop rows that have no year or no price |
| `tests/check_car.py` (62) | judging ONE car against its trim: the peer pool is one price per car and self-excluding (asserts sightings > cars, so it cannot pass by coincidence); all three gates fire on distinct vehicles; a typed price never reaches the fit however absurd; the model line's fit is **never** substituted (paired with an assertion that a line fit *was* available, or the check would pass trivially); the extrapolation guard withholds outside independently-computed 5th/95th percentiles; comparables order by log ratio and the fixture is built so absolute distance would give a different answer; duplicates are flagged and **not** merged; splits refuse below their gate *and say why*; a merged identity reports no change; the .db is byte-identical after 6 calls; the over-time panel spans the TRIM's days so a missing day is a gap (asserted on a car that HAS gaps, not the four-day car); daily peer stats exclude the subject and match pandas; every sighting names its import and the count of separate imports matches the ingest log; the exterior colour beats the metaData field that holds the interior |
| `tests/check_compare.py` (42) | the catalogue lists all three levels with the right pooling; every stat in the side-by-side block equals an independent computation; rank and "vs cheapest" re-rank when a column is dropped; the month series matches `analyze.bucket_stats`; the chart series (per-day supply, scatter, forecast points with intervals) are present and the projection sits **after** the history on the axis; an unknown label is **reported** rather than silently dropped; duplicates and over-long lists are handled; comparing writes nothing; turnover rides along per column and a selection that cannot be measured carries its REASON into the cell rather than a blank a reader would take for zero; the churn chart's per-day parts never exceed the day's own total; and a trim seeded with two captures sharing no car is flagged as a scope break while its overlapping sibling is not |
| `tests/check_merge.py` (19) | seeds eight groups that share an exact price and odometer, of which six each violate exactly ONE condition (rounded odometer, conflicting year, two site ids, no site id, a same-day price clash, a backwards odometer) and asserts each is left alone **with its reason named** — a group that simply never formed would otherwise pass silently. Then: the safe cases merge, three identities become one three-day history, the price multiset survives byte-for-byte, a dry run leaves the file byte-identical, a second run is a no-op, and a forced invariant failure rolls back completely |
| `tests/check_verify.py` (23) | runs the Data check against **deliberately broken copies**: a stale workbook is caught and a rebuilt one clears, a $3 price and a 2099 capture date are caught, a merged identity is caught, a car recorded with *and* without an ad id is caught, two different cars sharing an `IMG-` id are **not** falsely flagged, an orphaned observation FAILS (not warns), and checking leaves the database byte-identical |
| `tests/check_importui.py` (74) | live HTTP: preview writes nothing, commit matches preview, FB price-led paste binds prices correctly, empty-HTML hint, `/api/overview` totals agree with `/api/status`, and `_mix_scenario_db()` proves a changed pool (pooled median −61.5%) still reports the true same-cars +10%; the workbook rebuilds into a throwaway file with `os.startfile` watched, proving `/api/viewer/build` opens nothing while `/api/viewer/open` opens exactly once; the data check card precedes 'dataset at a glance'; `/`, `/?v=1` and `/index.html` all serve, with the module run in a SUBPROCESS because importing it hides the `NameError: PAGE` this catches; and all five pages share one header, one stylesheet (placed first, never re-declared) and unique element ids |
| `tests/check_turnover.py` (30) | seeds four trims, one per failure mode, because the value of `turnover.py` is its refusals: a clean weekly run rates correctly (exits, arrivals, and car-days asserted as a PROPERTY of the intervals rather than a hand-computed constant, so a reseed cannot silently break it); a trim with `IMG-` ids is refused and its untrackable cars counted; two captures sharing NO car break the span instead of reporting a 100% clearance; a capture a sixth the size of its predecessor has its ten 'exits' shown but never rated, and `_scope()` is asserted to treat widening and narrowing OPPOSITELY; a car that repriced out of a page filter is vetoed rather than counted as a delisting; time on market is bracketed with the floor strictly below the ceiling, asserted on a car seeded to both arrive AND leave inside the window so the check cannot pass vacuously; and the word "sold" is asserted absent from every verdict and note |
| `tests/check_bulk.py` (24) | writing with no preview, and undoing it: the backup is proved to hold the state from BEFORE the run (a copy taken after would restore the very thing you are undoing); one file through the preview path and the bulk path produces an identical key set, so the fast path cannot drift into being a second importer; a bot-protection page and an empty file are refused by name while the good files in the same run land; undo returns the counts exactly, keeps pre-existing cars WITH their earlier sightings, empties a column the run filled by COALESCE, and leaves an unrelated observation-less car alone; a forced failure in the restore step rolls back byte-identical and the run is still undoable afterwards |
| `tests/check_quickadd.py` (16) | stats/percentage maths, mode edge cases, commit into the shared store |
| `tests/check_viewer.py` (45) | opens the workbook in **real Excel via COM** and reads what the formulas compute; styling preservation, MyView untouched, block sizing, layout migration, and an error scan that separates intentional `#N/A` chart gaps from real `#REF!`/`#VALUE!` |

`tests/make_synthetic.py` generates the fixture dataset; `tests/synthetic/`
is scratch output and safe to delete.

---

## 8. Gotchas that cost real time

Keep these — each one was a bug that took a while to find.

**Testing a page that WRITES**
* **Confirm which database a server is pointed at before clicking anything
  on it.** 11/08/2026, testing `/bulk`: the launch config named a scratch
  database, the tool that started the server ignored it and used the default,
  and nine fabricated vehicles and eighteen observations went into the real
  `data/vpa.db`. They came straight back out — `bulk.undo()` on its own two
  runs, batches 210-211, removed exactly them and nothing else, and
  `vpa verify` came back with 0 failures — but the check that would have
  prevented it costs one request: `GET /api/status` reports `db`, so read it
  and match it before the first interaction. Every read-only page (Explore,
  Compare) is safe to point anywhere; Import, Bulk and Quick add are not.
* A related trap in the same session: opening a `Store` inside a
  comprehension (`for s in [Store(DB)]`) leaks the connection, and on Windows
  the next `unlink()` of that file fails with `PermissionError: [WinError 32]`
  half-way through a test run. Use `with Store(...)` and collect first.

**The layout that must not move**
* The Bulk results box is a SESSION list (27/09/2026): every car uploaded
  since the page was opened, appended in arrival order (drop order, then
  the order each file lists them). `bulk.run` returns `cars` for this; it
  is never written to `bulk_runs`. Undone runs stay listed, struck
  through, and stop counting. Uploads are expected cheapest-first: a live
  priced car cheaper than the live priced car before it is marked, and a
  dismissible flag says so. Dismissals are remembered PER CAR - a count got
  it wrong after an undo removed one drop and hid a different one.
* Everything on that page whose TEXT changes (card title, header counts) is
  one line and clipped. The first version let the session title wrap at
  narrow widths and it pushed the page down 38px; measured, not eyeballed.
  The flag is absolutely positioned over the card corner for the same reason.
* `/bulk` was asked for with "no button moves around", which is a testable
  property, not a style note: capture `getBoundingClientRect()` for every
  control before, during (on a 40 ms interval) and after a real upload, and
  assert the boxes are identical. Two real defects only showed up that way —
  a 90-character backup path wrapping to a second line and pushing the panel
  below it down, and the results table growing the card instead of scrolling
  inside it. Fixed heights plus `overflow:auto` are load-bearing there;
  changing one to `max-height` reintroduces both.

**A site format change fails SILENTLY (found 27/09/2026)**
* From 27/07/2026 no main-site capture stored a seller type or state: 3,264 of
  3,564 main-site cars have neither. The main site moved its per-listing analytics
  from prefixed keys with the fields after the ad id to flat objects with
  the state BEFORE it (`{.."state":"VIC","networkId":"EXA-AD-1",..,
  "adtype":"Private"}`). Nothing errored: the regex simply matched nothing,
  every listing still parsed, and the gap only showed as a thin "Who is
  selling" tab. `htmlpage._flat_tracking` reads the new shape from each
  object's own braces, skips an object naming two cars, and drops a field
  whose repeated copies disagree. Body type was never stored from any page:
  JSON-LD `bodyType` is blank; each tile's `keyDetailsTexts` states it but
  carries no ad id, so it is used only when the tiles provably line up (same
  count, same order, every odometer equal). Across the 61 saved pages:
  seller 12 -> 675 of 722 listings, state 9 -> 675, body 3 -> 619, and no
  previously parsed value changed. The remaining gaps are EXCD-AD showroom
  "New car" entries, left blank rather than called dealer.
* Worth a periodic check: parse the newest saved page and look at the
  per-field fill (`tests/check_sitepages.py` pins both formats).

**Tailscale route (05/10/2026)**
* The HTTPS address the phone app needs for offline use comes from
  `tailscale serve --bg 8766` (tailnet only) on the laptop. `vpa/tailnet.py`
  reads `tailscale status --json` + `serve status --json` (read-only, cached
  20 s) into a state: missing / stopped / no-https / not-served / ready /
  funnel. FUNNEL ON IS NEVER "ready": it is shown red with the fix
  (`tailscale funnel reset`, then serve again). The Phone page shows it as
  one line (lock + address + Copy), the phone's Sync screen shows its
  connection, and the supervisor stores settings["https_url"] so a phone on
  the plain-http address is offered the secure one via /api/meta.
* Android gotchas met on the first setup: an exit node selected, a Samsung
  Private DNS hostname, or another VPN app each leave the phone with no
  internet while Tailscale is on. Check those first.
* Home-screen icon: Android launchers cannot draw an SVG favicon (blank
  tile). index.html now links 192/512 PNGs + apple-touch-icon and the
  manifest lists PNGs for purpose "any" and "maskable" separately. Firefox
  keeps the icon it saw when the shortcut was made: remove and re-add.
* Windows refuses os.replace while any reader holds the target (another
  thread, the other process, OneDrive syncing data/): phonecfg._replace
  retries for ~1.5 s; phonebuild uses it for meta/snapshot/cache, and a
  failed "last seen" write can never fail a sync.
* Tailscale need not stay on: it is only the route for the few seconds of a
  sync (the app and its copy live on the phone). Even on the same Wi-Fi the
  phone cannot reach the laptop without it: the ts.net name resolves (on
  public DNS too) to a 100.x Tailscale address, and an HTTPS page may not
  fetch the laptop's plain-http LAN address. So the app tells 'offline' (no
  signal: quiet) from 'unreachable' (signal, no laptop: pill 'Not synced ·
  <date>', Sync screen says turn Tailscale on, with an Open Tailscale button
  - an Android intent: link with a Play Store fallback), and on coming back
  to VPA after a failed try it syncs at once, and again 4 s later while the
  VPN finishes connecting.
* The background listener restarts itself when VPA's code changes
  (phone._code_stamp: newest mtime of vpa/*.py/js/html/css): it stops
  listening first, then starts a fresh copy, so it never serves old code.

**Searches saved and sweep planning (12/10/2026)** - `vpa/sweeps.py`
* Results pages state their search's size ("446 ... cars for sale"). Each
  imported results page is noted in data/sweeps.json (beside the database,
  never in it): capture day, page title, stated total, the cars saved. A
  search = (day, title, total); its pages add up, a page saved twice once.
  Hooked in every import path: bulk.run (so the watch folder), Import with
  preview (held between preview and commit, like photos), `vpa ingest`. The
  app backfills from kept pages on first start (4 s for 203 pages).
* The total is read from VISIBLE text only (scripts/styles dropped): a
  count, up to 12 words, then for sale / results / listings found. On the
  real kept pages: 252 of 267 read, never a total below the page's own
  car count. A site can be given its own `total_pattern` (site file).
* Short = saved below stated by more than max(2, 1%). Turnover sets a
  capture aside (scope "cut short", exits shown, never rated - like a
  narrowed search) only when more than 5% is missing: on the real data a
  Ute search 5 short of 159 would otherwise have discarded 114 good exits.
  The frame carries this as a derived `cut` column (store._frame_from),
  so the by_variant cache keys on it automatically.
* Real data, once noted: BMW 3 Series' "64% leave per 30 days" rested on 3
  exits into a 550-of-895 capture - now refused; Falcon loses the 16
  exits into the 09/10 capture (the page-saved-twice night, 415 of 437).
* The limit per search: `max_results` in the site file first (set to 440:
  20 pages of 22). Learning it from data is only a fallback - a search is
  also short when you stop early (a Mustang sweep gave 660 over 30 pages).
* Planner: size from the latest search made mostly of the selection's cars
  (or entered, or the latest month's cars); shape from each car's latest
  price over 60 days (all cars, not the cut-off end of one sweep); bands at
  85% of the limit, edges rounded. A search up to twice the limit also gets
  the two-sort tip. Nothing site-specific in the code: check_neutral passes.
* Equivalence on the real data (no notes beside the copies): everything
  identical apart from the new fields.

**Listing details and listing filters (13/10/2026)** - `vpa/details.py`
* What saved pages state beyond the price, kept in data/details.json
  beside the database (never in it), keyed by ad id like photos.json:
  per car condition (used / demo / new / new to order), engine text,
  cylinders, litres, turbo / supercharged / natural, fuel, gearbox, seller,
  state, seller id; from details pages also drive, doors, seats, cc,
  colour. Per sighting (car + capture day): price type (drive away / excl.
  charges), the site's own rating (great / good / fair, either wording),
  just listed, suggested (not part of the search), promoted.
* Attribution is proved, never positional: a tile is the smallest part of
  the page's own JSON data holding one `keyDetailsTexts` and naming ONE ad
  (5,022 of 5,036 tiles on the kept pages; two old pages have none);
  analytics objects only when they name one ad, a field kept only when all
  copies agree. Check on the real data: the tile's state and seller type
  agreed with the stored ones 4,511 of 4,511 times.
* Classic engines are written in cubic inches with an L ("8cyl 289L
  Petrol"): three digits are converted (4.7 L). 154 cars.
* The frame (store._frame_from) adds FRAME_COLS and fills only BLANKS in
  stored state / seller type and in the trim-read gearbox / drive. On the
  real data: state filled for 362 cars, seller for 459, drive for 5; no
  stored value changed; the trim name's gearbox agrees with the listing's
  7,636 of 7,640 times. 2,618 of 5,279 cars have details (the rest came
  from pasted text, v2 or pages no longer kept).
* Explore: filters condition (+ "not new or demo", which keeps unknowns,
  and "new (in stock or to order)"), state, seller, gearbox, body, fuel,
  engine, cylinders, price type, rating; "~" = not stated; leave out
  suggested listings. Options come from the selection with car counts.
  Always applied in FACETS order. Splits by condition / price type /
  gearbox / engine / fuel / rating; a spec column in the cars table.
  Defaults change nothing: every number is as before until a filter is set.
* The phone copy carries the same columns (phonebuild copies details.json
  beside its work copy); stats.js ports the filters, options, splits and
  car fields - check_phone_app compares them, fixture and real data.
* Backfill keys sightings by the day the page's cars were stored (or the
  file's date): 5,026 of 5,038 real sightings line up; the other 12 get
  car fields only. `python -m vpa details --backfill`; the app backfills on
  first start.
* Searches: the stated total falls back to the count the page's listing
  data repeats (agrees with the visible text on every page that has both);
  each search records the sort orders it was saved in.
* Real numbers: Mustang 749 cars, median $62,500; not new or demo 683,
  $59,900. BMW 3 Series drive-away median $99,325 (new cars) against
  $12,990 excl. charges.

**Australian dates on screen (12/10/2026)**
* Display only: a day reads 19/09/2026, a month Sep 2026. The database,
  keys, API payloads, exports and every comparison stay ISO, so nothing
  stored or compared changed and the phone parity check is unaffected.
* One rule, applied as text appears: `vpaAu` in ui.py's shared script (a
  MutationObserver over text nodes and title/aria-label/placeholder, never
  inputs, scripts or styles) and the same lines in phone_app/app.js;
  `vpa/dates.py` is the Python twin, used by `vpa status`. check_explore
  asserts all three give the same text, and that payloads stay ISO.
* Doing it per template would have meant ~130 places plus server-written
  notes ("not in the 2026-09-19 sweep"). The phone already wrote dates as
  "19 Sep 2026"; no ISO date shows on any phone screen.
* Guarded against: year ranges (2015-2016), ad ids with digits, an already
  Australian date. No stored make, model, trim or title contains anything
  the month rule could catch.

**Workflow and tests streamlined (12/10/2026)**
* `run_all` runs suites six at a time (each already worked in its own
  folder; `selfcheck` used to delete ALL of tests/synthetic/ and now owns
  tests/synthetic/selfcheck/ only - that was the one thing stopping
  parallel runs, and it also killed a real-data parity run started beside
  it). The Excel COM suite runs alone after the rest. 187 s -> ~80 s;
  `--quick` (no Excel) ~45 s. Logs: tests/synthetic/logs/.
* Real-data phone parity 12+ min -> 2.5 min with every selection and card
  still checked: its laptop side re-read the database for each of ~2,400
  calls (0.58 s a selection, ~0.5 s a card); now each worker reads the
  frame once (the memo phonebuild uses), 4 workers, overlapping the phone
  build. Proven: the expected answers are byte-identical to before apart
  from the fixture's seed time. Its own folder (phone_app_real) so it can
  run beside the fixture suite. `run_all --real` runs it with the private
  site file and checks the real database fingerprint before and after.
* Retired `check_perf`: its core was today's code against verbatim copies
  of the code it replaced (bootstrap loop, pandas cohort, groupby day sets)
  - one-time proofs from 27/09. Kept, moved: Theil-Sen vs scipy (selfcheck),
  the cohort/day-set awkward inputs as fixed expected values and the
  by_variant cache (check_turnover), latest-wins loading (check_explore).
  `check_links` folded into `check_sitepages`. Each moved check was shown
  to fail on a planted fault.
* Found with tools/linecov.py (sys.monitoring, no package) +
  tools/suite_overlap.py: unique lines per suite. Low unique coverage is a
  prompt to read the suite, not a verdict: sitepages/titles cover few
  unique lines but pin real page formats.
* New tools: `dbguard.py` (row-level fingerprint, read-only, --save /
  --check), `equiv.py` (git archive of a commit vs the working tree, both
  run on copies of the real data; proven by running it against the stage 7
  commit: only the duplicates section differs, 0 -> 8 proven, 0 -> 8
  review), `scratch_app.py` (the app on a throwaway copy, port 8873).

**No site named in the code (12/10/2026)** - `vpa/sites.py`
* So the code can be packaged and shared, it names no website. Everything
  site-specific moved to one private file, `data/sites.json` (gitignored
  with data/): source labels and the words that recognise a page or link
  (`domains`, `link_words`, `links: false`), the address for bare advert
  paths, `ad_paths`, the site ad-id prefixes, the "search for it" site, the
  photo server and its size query, the label for a single saved advert
  (`default_source`), the watch folder's own name (via inbox.json and
  .git/info/exclude). `sites.example.json` shows the shape with fictional
  sites. No file = neutral: 'other', no links, no photos, no site ids.
* A label is part of every car's dedupe key, so the private file must
  reproduce the old constants EXACTLY. Proven before commit by running the
  old code (git HEAD) and the new side by side: all 238 kept pages -> the
  same 4,459 records and keys and the same photos; ad/archive links for all
  5,268 cars; 24,912 photo addresses; site ids, turnover cohort, duplicate
  groups and identify-from-page all equal; the page script's photo/link
  output and the phone's photo URL and worker check equal in node. Only
  messages changed wording. Kept as-is: the id-prefix list deliberately
  excludes the showroom-style prefix (its ids are not counted as site ids).
* The phone gets link rules and the photo server in its copy (`links`,
  `photos`, in the code signature with the site file, so a change rebuilds
  it); the offline worker gets the server substituted at serve time like
  BUILD. The listener's reach now includes `sites` (stdlib only).
* Tests use fictional sites (tests/sites_test.json, set by
  tests/__init__.py). Two fixtures had depended on key ORDER (which car is
  "first by id"), which a label change moves: the workbook "teeth" check now
  bumps a car's latest sighting, the trims tamper check a make no car has.
* `tests/check_neutral.py` scans every tracked or addable file for each
  private word; `tools/make_package.py` builds a zip from the committed tree
  only (no data, no history) and refuses on the same scan. The git history
  still holds the old names: share the zip, never the repository.

**Trim names grouped on read (12/10/2026)** - `vpa/trims.py`
* The database keeps every trim exactly as imported. `Store.frame()` adds
  `variant_stored` (as stored) and replaces `variant` with the name the
  analysis GROUPS by, so every page, the workbook and the phone copy share one
  set of names. Nothing is written: a wrong rule is fixed in trims.py and
  every past car regroups at once. Everything that WRITES a trim (edit,
  rename, quick add, import) goes through SQL on the stored name.
* A trim asked for by a stored name (old link, saved comparison) resolves to
  its group: `trims.resolve` in explore._select/_select_multi/detail/car,
  `compare.stored_aliases` (kept OUT of selection_index, which is iterated
  as the list of selections - an alias there is a duplicate row).
* `python -m vpa trims [--save F] [--against F]`: per make, stored names ->
  groups; save a record of every car's group, or list which cars moved
  since one. Baseline before any rule: data/trim_record_2026-10-12_before.json
  (5,268 cars, 815 groups). Stage 1 had NO rules - 80 page fingerprints
  (pickers, details, 40 car checks, typed checks, Compare, watchlist) were
  identical before and after. `tests/check_trims.py`.
* Stage 2 - wording fixes (`trims.clean`): titles.strip_tail (what the
  importer already does) plus what older imports kept - MY codes anywhere
  (MY13.5, MY22.25, MY24.50), gearbox words anywhere ("edition one Auto
  quattro"), "Mk II"/"MK II" -> "MkII". Then names equal but for capitals or
  a hyphen are one trim, shown in their most common spelling (by cars, ties
  alphabetical). Spaces are NOT ignored: the only effect on real data was
  merging Corvette "Sting Ray" (1963-67) with "Stingray" (1968-76).
  Drive words leave a name only where they trail a gearbox word, so
  "quattro"/"xDrive" stay (A4 vs A4 quattro are different cars).
  Real data: 815 stored names -> 602 groups, 1,101 cars moved, every move
  reviewed (Audi "Auto quattro MYxx" -> "quattro" 90 -> 21; BMW "Auto
  MY13.5" tails; Falcon/Territory "Mk II"; ute "Auto Super Cab"; Mustang
  "Auto RWD MY21.5"; Corvette/Ford LTD "Auto"/"Manual" -> no trim, as the
  importer already treats a gearbox-only badge). No stored data changed.
* `gearbox` / `drive` / `model_year` columns, read from the stored trim then
  the title. Manual and auto share a trim group, so the car check (laptop
  and phone, column "gear" in the copy) adds a "same gearbox" like-for-like
  row - only when the car's gearbox is known.
* Stage 3 - too few of a trim (explore.car_payload, stats.js carCard, both
  identical): a trim with fewer than CAR_MIN_PEERS (8) other priced cars,
  or a car with no trim, is judged against EVERY car of its model within
  CAR_NEAR_YEARS (2) years of its own year - level "years", label
  "Lexus LS 2012-2016", a `fallback` note shown first on both cards, the
  card heading "this car vs nearby years". Only when that window itself
  reaches 8; never a whole model across all years (a 1966 Corvette with a
  2024 one) - no year, or too few nearby, refuses exactly as before. No
  mileage curve on a pooled set (it would measure the trim mix - the same
  reason the line fit is never substituted) and no trim caveats; the
  expected range then comes from the nearest-in-km or all of them. A trim
  with 8+ others is untouched. The phone's price check now takes a model
  (with the year) as well as a trim, and a no-trim car's page gets a card.
  Real data: 1,179 of the 1,457 cars in thin trims / with no trim now get
  a card (BMW 387, Ford 567 - those pool trims, e.g. XR8 with XT, hence
  "rough guide"); 278 still cannot (classic Corvettes, old Mercedes, bikes
  with no year). Planned: generation (stage 4) goes between trim and
  nearby years, making most of these fallbacks a same-generation pool.
* Stage 4 - generations (`vpa/trim_rules.json`, `trims.generation_of`):
  per make, `codes` (a pattern: BMW chassis E90/F30/G20, with qualifiers
  "LCI"/"LCI II") or `series` (codes with the years they were sold, one
  year's slack: every Falcon/Fairlane/LTD/Territory/Mustang series XK..FG X,
  ZA..NL, P5..DL, SX..SZ, FM..FO; FPV `same_as` Ford). The LAST code in a
  name counts (the main site writes trim then series: "XT BF MkII"), and a series
  code only where the car's year fits - or the car has no year - because
  XT/XL are 1968/1962 Falcons AND later trims. New columns `generation`,
  `trim_base` (the name without it); `body_type` filled from a BMW chassis
  code (E9x/F3x/G2x - checked against every stored body: no disagreement)
  only where none was stored; `body_type_stored` keeps the original. Real
  data: BMW 908/935 (26 "420i M Sport" carry no code), Ford family all but
  59 (older Mustangs, Territory "Turbo AWD"), 114 bodies filled. Grouping
  unchanged (same 1,101 moves as stage 2).
* The car check's ladder (laptop = phone, columns "gen"/"tb" in the copy):
  trim -> `trim_years` (the same trim of any generation within 2 years:
  a thin 428i Sport Line F36 -> every 428i Sport Line 2014-2018) ->
  `generation` (every trim of it: a thin FPV GT Cobra -> every GT BF MkII)
  -> `years` (the model within 2 years). Each only at 8; the note and the
  heading say which.
* Stage 5 - what the rules don't cover. `trims.wording_check` (every upload:
  bulk.run per file -> report "wording", so the watch folder too; Import
  with preview -> its note): of the cars whose make + model already holds
  WORDING_MIN_MODEL (20) cars, how many land on a trim name the data already
  has; under WORDING_SHARE (70%) of at least 5 -> flagged with the new words
  ("FG2", "FGX", "T"). NEVER blocks: the names are stored as they come and
  regroup when a rule is added. Read before anything is written, so a page
  is judged against the pages before it. `trims.derive_one` groups one
  unstored car exactly as the frame would (same spellings, same rules).
  The Trims page (/trims, nav button "Trims", vpa/trimspage.py, read-only):
  coverage per make, names left without a generation, and every stored
  name -> its group, searchable. `python -m vpa trims --unmatched`.
* Stage 6 - generations by year (trim_rules.json `years`, `badges`,
  `era_words`; trims.generation_by_year, applied in trims.place): Audi
  S1/S3/RS3/S4/RS4/S5/RS5/S6/RS6/S7/RS7/S8/R8, Lexus IS/LS, Corvette C1-C8,
  Mustang Gen 1..S197 (FM/FN/FO stay codes). DRAFTED from general knowledge
  (AU model years) - the Trims page lays every row out with the cars it
  places, to be checked. The MY code is used before the listing year. A
  year two generations share is NOT guessed: the car is named with both
  ("quattro B8/B9", generation "B8/B9"). A badge decides outright (LS460 =
  XF40, LS500 = XF50, IS350 Version L/S/Prestige/X = XE20). The generation
  is ADDED to the grouped name ("quattro B9"); a car with no trim takes it
  as its name ("C4"); era words go in their eras (a 1966 "Sting Ray" is
  "C2", a 1973 "Stingray" "C3"). Real data: 602 -> 637 groups, 1,182 cars
  moved (Audi quattro split by generation; Corvette base cars C1-C6;
  Lexus XE20/XE30, XF40/XF50; Mustang Gen 1), every move reviewed.
* Old names after a split: trims.resolve_all - a CURRENT grouped name is
  exactly itself (picking Mustang "GT" must not pool the GT Gen 1/SN95/S197
  its stored name also went to - the real-data phone check caught that);
  a RAW stored name ("Auto
  quattro MY24") is exactly its cars' groups; a CLEAN name from a later
  link ("Sting Ray", "quattro") is every car that cleans to it. Explore
  pools all of them (an old "Audi S5 quattro" link opens every generation);
  the car check takes the car's own group, or for a typed car the one its
  year gives; the saved-page lookup groups the page's car with
  trims.derive_one; Compare's old labels point at the largest group.
* Classics (explore.CAR_CLASSIC_AGE = 35 years): ranked like any car, but
  no mileage curve and no expected price - condition and originality, not
  kilometres, set the price; the spread of comparable cars is shown.
  Laptop = phone (subject.classic).
* WRITING trim_rules.json: use the Write tool or json.dump from a file -
  a shell heredoc turned "\b" into a backspace and broke every badge once.
* Stage 7 - shorthand and suggestions. `shorthand` in trim_rules.json:
  [{make, model (empty = every model), from (one word), to}] - applied
  first (trims.expand), whole words, any capitals. Suggestions
  (trims.suggestions, on the Trims page) come from three strict patterns,
  each only where the data already holds the long form: run together
  ("FGX" where "FG X" is written apart), code + digit ("FG2" where the
  generation "FG MkII" exists), and a 1-2 letter abbreviation ("T" after
  "XR6" where "XR6 Turbo" exists - never a letter that is a word in 3+
  cars, so BMW's "M" is safe). Each shows the cars it would move and the
  group they'd join. Real data: none (every trim word is known).
  APPROVE writes ONLY trim_rules.json: copied to data/backups/
  trim_rules_<stamp>.json first (a counter when two land in one clock tick
  - Windows' ~15 ms clock had one overwrite another), written to .tmp,
  parsed, swapped in, read back and checked - put back if it did not hold.
  Remove = the same care. "Not this" -> data/trim_dismissed.json (private).
  VPA_TRIM_RULES points tests / a scratch app at a copy of the rules.
* A name with a generation code is written trim-then-generation whatever
  order it came in ("FG MkII XR6 Turbo" -> "XR6 Turbo FG MkII"); real data:
  one rename (Fairmont "XC GXL" -> "GXL XC"). A two-word code ("FG X") is
  taken from the words regardless of the year (one 2012 car is stored as
  "XR6 Turbo FG X": its generation was "FG", now "FG X" - the name decides).
* Search with the new names: Explore's keyword matches the grouped name
  (so "B9" finds the generation; "B8/B9" cars too - they may be) and the
  title (so a stored word like "MY18" still finds); quick add lists and
  prices by GROUPED trim (it read stored names through SQL); the phone copy
  carries `alias` (phonebuild._aliases: old label -> current, stored names
  and their cleaned forms) and app.js selLabel uses it on Find, Compare and
  Check, so a link saved on the phone before a regrouping still opens.
* tests/check_savewarn: also waits for the session to say "complete" (it
  read "3 of 3, watching" once - the count lands a moment first).
* Stage 8 - duplicates on the grouped trim (merge._rows adds `group`, the
  grouped name, and `named`; _components links on `group`; rule 1 needs a
  REAL trim: words beyond the generation, or a generation the stored name
  wrote itself ("FG X") - never one the year supplied, so a trimless "C4"
  pair is "no variant", not offered). Everything else unchanged: the
  seven rules, proven / your judgement / not offered, one transaction,
  backup, exact undo. The page shows each car's trim as stored and a
  neutral "trim worded: XT BF Mk II / XT BF MkII" tag. Real data (copy,
  nothing merged by me): 0 -> 8 proven and 0 -> 8 for judgement - all the
  same story: the 28/07/2026 captures (no ad id) stored Falcon trims as
  "MkII", the 17/08 saved pages (site ad id) as "Mk II", so each such car
  looked like it left the market on 28/07; proven ones share an exact odd
  odometer (143,700 / 352,833 / 400,544 km), year and state. One more group
  refused as a relisted advert (two ad ids).
* tests/check_savewarn: the watch-folder checks now wait for the RESULT
  (inbox._take archives the file a moment before recording it - reading
  on "file gone" raced and failed once in a full run).

**Saves that went wrong - the 09/10/2026 Falcon sweep (12/10/2026)**
* WHAT HAPPENED (poor connection): the watch folder took "page 13" from a
  re-save of page 12 (every car already recorded; 0 written) - the real next
  page was never saved, so ~18 cars "vanished" that day (FG X G6E Turbo
  median of 4 cars jumped). Then a download stalled at exactly 655,360
  bytes, the size stayed still 0.8 s, and it was imported as page 20: prices
  and km right (the listings JSON is near the top), body type / state /
  seller missing - one new car stored with no body type. The clock was
  right: the pages' own server times (2026-10-08T23:15Z) = 10:15 AEDT,
  matching the laptop. The FG MkII XR6 rise is real (two cheap cars gone,
  two dearer new ones; median of 16).
* FIXED (12/10/2026) with the app's own tools, rehearsed on a copy first:
  bulk.undo(360) (the re-save; nothing written, only its run), bulk.undo(367)
  (the cut-off file), then the complete copy of that page re-imported as run
  368, dated 2026-10-09. Afterwards every (key, day, price, note, km) was
  identical; the one change was that car's body type None -> sedan.
  Backups: data/backups/vpa_backup_20261012_1153*.db.
* PREVENTED: `bulk.cut_off` refuses a saved web page with no `</html>` in
  its last 8 KB (all 236 good kept pages pass; only the two cut copies
  fail) - on Bulk upload, the watch folder and Import with preview. A page
  whose EVERY car is already recorded for its day (Store.preview's rule)
  is skipped in bulk.run - no run, nothing written - with "the page after
  the previous one was probably skipped"; the preview says the same. In the
  watch folder neither counts towards the page total. `tests/check_savewarn.py`.

**Expected asking price (05/10/2026)**
* `explore._estimate` / stats.js `estimate`, returned as `estimate` by
  car_payload / carCard for TYPED cars (stored cars: null). car_payload no
  longer refuses a typed car without a price: the rank parts then say
  nothing (raw rank gated, no adjusted/expected) and the page shows the
  estimate instead of a verdict.
* BASIS, most specific first, always named: `mileage` - the trim's mileage
  curve at that odometer (the SAME base, slope and anchor as the card's
  "expected for its kilometres", so `mid` equals expected.price) times the
  10/25/75/90th percentiles of the fit rows' residuals from the curve;
  `nearest` - the EST_NEAREST (12) cars nearest in km, on |ln km ratio|,
  when the curve is refused or the odometer is outside CAR_FIT_RANGE;
  `trim` - every car of the trim, with no odometer. Fewer than
  CAR_MIN_PEERS prices: no range. Rounded to $100.
* Year is NOT in the range (the fit has no year term); the same-model-year
  median sits under it as like-for-like context, as on the car card.
* Parity: check_phone_app's typed cases now include five price-less ones;
  a planted 0.25 -> 0.26 change in the phone's quantiles failed 48 of 70.

**Stars from the phone (05/10/2026)**
* THE FIRST PHONE WRITE, so it can reach exactly one table. The phone sends
  STATES ("star k" / "unstar k", a random id, the tap time), never toggles:
  a request applied twice changes nothing. Taps wait in the phone's
  localStorage (`vpa.starq`) and show at once (DB.stars = the copy's stars
  with the queue laid on top); every sync sends them first (POST
  /api/stars, max 25, 4 KB) and asks what became of earlier ones.
* THE LISTENER STILL NEVER OPENS THE DATABASE (check_phone's import walk now
  allows phonecfg + phoneinbox only). It validates each field and appends to
  data/phone_stars.json (lock + atomic replace; a damaged file is refused,
  never rewritten). The supervising process (phone._apply_phone_stars, the
  app or the background listener - whichever listens) applies the queue
  each tick with starred.apply_phone_ops: ONE transaction, `starred` only,
  then moves each request to `done` (kept 60 days) with its outcome:
  applied / already / kept / gone. A crash between the two re-applies -
  harmless, since each is a state.
* CONFLICTS: newest action wins. An unstar tapped before the laptop's
  starred_at leaves the star ("kept"). Phone stars take the tap time as
  starred_at. A key merged away follows merge_log to the survivor. A car
  gone from the data can still be unstarred, never newly starred. Known
  limit: the laptop keeps no record of WHEN it unstarred, so a phone star
  tapped before a laptop unstar (both since the phone's last sync) is
  applied - a star reappears, visibly, never data lost.
* An unstar removes the star's note, as on the laptop: the phone asks first
  when there is one, and the note is kept in the done log.
* The DB is backed up once, before the first change a phone ever makes
  (`backup` in the file). "Phones can star cars" on the Phone page
  (phone.json `phone_stars`, default on) switches it off: the listener then
  answers 403 and the phone keeps its taps, saying why. The Phone page lists
  recent phone changes by car name (read-only lookup).
* The phone drops a request once answered AND its copy shows the outcome
  (or the copy was built after the laptop decided); "kept" / "gone" are
  dropped with a toast. `tests/check_phone_stars.py`.

**Light and dark (05/10/2026)**
* ONE SWITCH, RESOLVED BEFORE PAINT: `ui.THEME_HEAD` (laptop, injected into
  every page's <head> by `chrome()`) and the inline script in
  `phone_app/index.html` turn the choice into `data-theme="light"|"dark"`
  on <html> before any CSS draws, so dark never flashes white. The choice:
  localStorage `vpa-theme` (laptop) / `vpa.theme` (phone); absent = auto,
  which follows `prefers-color-scheme` live. Laptop tabs follow each other
  (storage event). CSS only ever looks at `[data-theme=dark]`.
* LIGHT IS UNCHANGED: every literal colour that was in the shared or page
  CSS became a token whose light value is the exact old colour. Checked by
  screenshotting every page, new code against the committed code, light
  theme: pixel-identical below the header (the header gained the switch).
* DARK: the same palette as the phone's dark (bg #0f1218, card #181c24).
  Solid buttons use `--fill`/`--on-fill`, not `--accent`: the accent turns
  pale in dark and white text on it would be unreadable.
* CHARTS: drawn in page script with literal colours. `ui.CHART_DARK` maps
  each to a dark-card equivalent with CSS attribute selectors (a stylesheet
  rule outranks an SVG presentation attribute; `!important` outranks an
  inline legend style), in dark only. check_importui fails on a literal
  chart colour CHART_DARK does not name.
* Phone: the old `@media (prefers-color-scheme:dark)` block is now
  `:root[data-theme=dark]`; Appearance card on the Sync screen (renamed
  "Sync & settings"); theme-color follows the theme.
* Phone listener fix found by the test run: a refused POST (404, 429, 413)
  is now read before answering - unread, Windows reset the connection and
  check_phone failed about one run in five (`phonesrv._discard_body`).

**Listing photos (05/10/2026)**
* WHERE THEY COME FROM: a saved search page's schema.org object per listing
  carries the ad's `url` (ad id) AND its `image` list (5 photos) side by
  side, so photos are matched by the object itself, never by position. A
  details page: its schema.org `image` plus the gallery photos it preloads
  (`<link rel=preload as=image>`, same seller folder as the main photo);
  the "similar listings" further down are never read.
* NOTHING IS FETCHED BY VPA. Only addresses are stored; the browser showing
  a car loads that one photo from `the site's photo server` (CORS-enabled, served
  `immutable` for a year, sized by `the size query`: 160x107 ~5 KB, 480x320
  ~30 KB, 600x400 ~45 KB). Photos of ads gone since July still loaded on
  05/10/2026. Fetching listing PAGES to find photos would be scraping - ruled
  out; a car with no stored photo keeps its "open the advert" link.
* STORAGE: `data/photos.json` beside the database (`photos.store_path(db)`,
  so a test database never touches the real one), keyed by ad id - which
  survives merges and repairs of the database's own keys. Newer page's
  photos first, an older page only adds, cap 12. Lock file + atomic replace
  + `.bak`. A save that cannot read the existing file ABORTS (rewriting from
  nothing would lose every photo); a read during a Windows file lock keeps
  the last good list (`phonecfg.read_text_retry`, also used by phone.json -
  a phone's sync had read "not paired" during a replace).
* Only `^[a-z]+/[a-z]+/[a-z]+/[a-z0-9]{10,64}\.(jpg|jpeg|png|webp)$` under
  that site's own host is stored, and both front ends re-check the shape, so
  a page can never make the app load an image from anywhere else.
* RECORDED BY every import path: bulk.run (Upload + watch folder), Import
  with preview (held in memory at Preview, stored at Commit for the
  committed rows only), `vpa ingest`. Backfill: `python -m vpa photos
  --backfill` reads data/raw and the watch folder's imported/; the app runs it
  once on start when photos.json does not exist. On 05/10/2026: 196 kept
  pages -> 2,500 of 5,234 cars (the rest came through Upload before photos
  were kept, whose files were not kept, or from pasted text).
* LAPTOP: explore vehicles `ph`, car card `subject.photos` + comparables
  `ph`, watchlist `ph`; ui.py vpaThumb / vpaGallery / hover preview.
* PHONE: vehicles column `ph` (all addresses; +~350 KB on the 0.9 MB copy);
  the copy's version hashes photos.json and phone._db_stat watches it, so a
  backfill alone re-syncs phones. stats.js sends the same `ph`/`photos`
  fields as the laptop, so check_phone_app compares them too. sw.js keeps
  shown photos in its own "vpa-photos" cache (CORS copies, real size, newest
  600), surviving app updates. The gallery's class is `gstrip` - `.strip` is
  the price-position bar.
* `tests/check_photos.py`.

**Phone app (05/10/2026)**
* `vpa/phone_app/`: index.html, app.css, app.js (screens, sync, SVG charts),
  stats.js (the arithmetic), sw.js (offline shell). Served by phonesrv; sw.js
  gets a hash of the app files as its BUILD, so a changed app is a changed
  worker and phones update on the next open.
* SAME NUMBERS, BY CONSTRUCTION: `phonebuild.py` (format vpa-phone/2) runs
  `explore.detail_payload` and `compare._one` for EVERY make, line and trim,
  plus `importui.overview_payload` and `picker_payload`, and ships the
  results (minus lists the phone rebuilds: cars, movers, scatter, standouts;
  minus drawn curve grids, recomputed from mu/sigma). Trims also carry the
  car card's mileage fits: the trim's own (typed cars) and one with EACH car
  left out (stored cars), so the phone never fits a regression.
* stats.js ports the counting: prep / latest_per_vehicle, filters, headline,
  splits, capture days, histogram (numpy's binning), car list, movers,
  standouts, dup_sigs, car_payload, link_payload, schema's typed-value
  parsers, Python's round() (exact-tie detection via the float's full
  decimal expansion). Filtered views are recomputed on the phone; forecast,
  bell curve, mileage fit and turnover are whole-selection only and hidden
  when filtered (the laptop recomputes those on the filtered rows).
* Builder runs in a CHILD process (`python -m vpa phone-copy --out`): it
  copies the DB with SQLite's backup API from a read-only connection into
  output/phone/work-<pid>.db, then patches explore._frame / compare._frame /
  starred.keys / dupes.dismissed_sets to a cached frame - safe only because
  no app request can see that process. build.lock stops two builds at once.
  Real data (4,480 cars, 831 selections): first build ~80 s, 898 KB gzip;
  after an import, seconds (sel_cache.json, keyed by each selection's rows).
* STABLE SORTS: analyze.prep / latest_per_vehicle and the car-card helpers
  now sort capture_date with kind="stable". The default quicksort left
  "which of two same-day sightings of one car wins" to numpy internals; now
  the later-stored row wins, always, and stats.js does the same. Only keys
  with two prices on one day (collided_keys) can change; all suites green.
* Sync (app.js): on open, on returning to the front, on `online`, and every
  5 min while visible; /api/meta (8 s timeout) then /api/snapshot only if
  the version differs; the download is verified against the meta before it
  replaces the stored copy (IndexedDB). Firefox cannot run a closed web app,
  so there is no true background sync; Web Share Target is not supported by
  Firefox for Android, so instead pasting a link searches instantly.
* Offline: needs a secure origin (HTTPS via Tailscale; localhost for tests).
  Verified in headless Chrome: worker installed, listener stopped, app
  reopened from cache with the stored copy ("Offline · <date>" pill).
  GOTCHA: Chrome's CacheStorage fails ("Unexpected internal error") when the
  --user-data-dir path is long (MAX_PATH) - use a short path like
  %LOCALAPPDATA%\Temp\vpacr for these tests.
* `tests/check_phone_app.py` (+ phone_js_check.js, node): stats.js vs the
  Python on a deliberately awkward fixture, and every screen of app.js
  rendered against a stand-in page (no throw / NaN / undefined; proven to
  fail on planted faults). `--db data/vpa.db` runs it on a read-only copy of
  the real data: 841 views, 987 car cards, 8,590 screens, all identical.

**Phone sync, laptop side (05/10/2026)**
* Option B from the brainstorm: the phone carries its OWN copy and searches
  it offline (car yards: no signal, laptop asleep); it refreshes whenever it
  can reach the laptop. Three modules, split by what they may touch:
  * `phone.py` - builds the copy and keeps it current. Reads the database
    ONLY through `store.read_frame()` / `connect_readonly()` (SQLite
    `mode=ro`: writes are refused by SQLite itself). Forecasts come from
    `viewer_xlsx._forecast_entries/_forecast_row` on `analysis_frame()` (now
    shared with the workbook) with their own `_FcCache` file, so the phone
    and Excel can never disagree; each entry also carries its mileage fit.
  * `phonecfg.py` - settings, pairing, tokens (`data/phone.json`). Stdlib
    only. Every change is read-modify-write under a lock file + atomic
    replace, because the app and the background listener are two processes.
  * `phonesrv.py` - the listener phones talk to: `/ping`, `POST /api/pair`,
    `/api/meta`, `/api/snapshot` (gzip, ETag -> 304), the page shell. It
    imports only `phonecfg` (enforced by check_phone's import walk), so no
    request can reach vpa.db. Private/Tailscale/loopback source addresses
    only; 30 bad tokens or codes in 10 min locks everyone out.
* Version = hash(data fingerprint, starred list, layout, analysis + phone
  code). Unchanged -> nothing written (0.05 s). Snapshot written before
  meta.json, newest two kept, so a phone mid-download always gets a whole
  file. ~180 KB gzip for 3.6k cars / 6.4k sightings; cold build ~5 s.
* Rebuild trigger: the supervisor stats vpa.db every 4 s; a change that has
  settled for 2.5 s rebuilds. That covers every writer (app, watch folder,
  CLI, undo, merges) with no hooks in any of them.
* Who listens: `supervise()` in whichever process - the app while open, or
  with "keep running when VPA is closed" the background listener
  (`python -m vpa phone-serve`, a Startup shortcut "VPA phone sync.lnk").
  Exactly one at a time; they hand over through the settings file. The app
  runs phone sync ONLY when serving the real database (`DEFAULT_DB`), so a
  scratch/test app can never become what a phone downloads.
* Pairing: 6-digit code on the laptop (10 min, 5 tries, stored hashed) ->
  the phone gets a 32-byte token, stored on the laptop only as SHA-256.
  Remove = locked out on the next request.
* Binds 127.0.0.1 by default (the HTTPS route, `tailscale serve`, reaches it
  there); "Home Wi-Fi" binds 0.0.0.0 for plain-http testing. KEY CONSTRAINT:
  Android browsers only allow offline web apps (service workers) on HTTPS,
  so plain LAN http can sync but cannot open with no signal. Offline use
  needs the Tailscale HTTPS address - step 4.
* `tests/check_phone.py` (43 checks) incl. a real app -> background ->
  app hand-over with live rebuilds, and integrity_check at the end.

**Watch folder (29/09/2026)**
* `vpa/inbox.py` + a "Watch a folder" card on Bulk. You still browse and
  save every page yourself (Ctrl+S) - a recorded mouse macro that paged
  through the site was asked for and declined, as automated collection is
  exactly what the no-scraping rule excludes. The watcher only reads files.
* Each file that lands (and has stopped changing for 0.8 s) goes through
  `bulk.run` - same parser, same checks, its own undoable run - and is then
  moved to `<folder>/imported/<date>/`, so the next save under the SAME name
  finds an empty folder: no replace prompt, no renaming.
* One backup per session (`bulk.run(backup=...)` records it on each page's
  run instead of copying the database per page). Files already in the folder
  at the start are left alone; repeats (same hash) skipped; bot-check pages
  refused and not counted. Stops itself at the page count. Pace = median gap
  between saves (file mtimes). "Undo session" = bulk.undo per run, newest
  first. Folder + page count remembered in `data/inbox.json`.
* Default folder: `inbox/` in the project (gitignored, like
  data/raw). It is in OneDrive, so moving a just-saved file out is retried;
  a file that still cannot move is left, warned about, and never re-imported.
* `tests/check_inbox.py` (17 checks) runs a real session incl. undo back to
  an identical database.

**UI refresh (28/09/2026)**
* One pass over the shared stylesheet (`ui.py` CHROME_CSS) rather than per
  page: floating sticky header (brand mark + the database counts, which used
  to repeat in every page's subtitle - `.dbstat` is now hidden, `#navstat`
  fills), icon-only Help / Shut down, quieter uppercase table headers,
  segmented toggles, softer cards with a short entrance, focus rings.
  `--navh` is the header's bottom edge; anything else sticky sits below it,
  and `vpaReveal` now allows for any bar that is currently stuck.
* Wordiness: explanatory paragraphs are marked `.hint.note` and collapse in
  place to an "(i) How to read this" toggle (`vpaNotes`, wired automatically
  for content rendered later via a MutationObserver). Nothing was deleted -
  only tucked. Instructions and live counts stay visible.
* Duplicates: the per-group "Not proven: ..." sentence (100 copies) is a tag
  with the full reason on hover (payload now carries `why_codes`).
* Bulk keeps its fixed heights ("nothing moves" is deliberate there).

**Excel workbook build (28/09/2026)**
* The real viewer.xlsx was a month stale (built 23/08) - and the stale flag
  could not have been trusted anyway: it compared the file's date with the
  last IMPORT, so an edit, a merge or an undo left old numbers marked current.
  Now every build stamps the workbook (defined name `vpa_data`) with a hash of
  every value it was built from; `/api/status` and the Data check compare that
  with the database (`workbook_stamp()` reads it from workbook.xml without
  loading the sheets). The Data check also catches a change the totals cannot
  see, e.g. a merge re-pointing rows.
* Build time ~12s -> ~8s from scratch, ~5s after an import. Half was
  recomputing ~190 forecasts every time; they are now cached beside the
  workbook in `output/viewer_cache.json`, keyed by a digest of each forecast's
  exact input rows and voided by any change to viewer_xlsx/analyze/forecast.py
  or pandas/numpy/scipy/statsmodels. `tests/check_workbook.py` proves a cached
  build equals a build from scratch cell for cell. Deleting the file is safe.
  The rest is openpyxl reading and writing the 1.4 MB file; installing `lxml`
  would roughly halve that (openpyxl uses it when present) - not done.
* Excel itself opens and recalculates the real workbook in ~1.5s; the wait
  was never Excel.
* The app builds in a background job (`/api/viewer/open|build` return a job
  id at once, `/api/viewer/progress` reports stage and fraction; {"wait":
  true} blocks, for tests). Both workbook buttons fill as it runs and say
  "Building 42%" with the stage in the tooltip; a second click joins the
  running build. The CLI prints the same progress on one line.
* Safer: a workbook open in Excel is refused in under a second, before any
  work, instead of after the whole build. Saves go to viewer.building.xlsx
  and are swapped in, so a failed save can no longer corrupt the file (and
  MyView with it); the replaced file is kept as viewer.previous.xlsx.
* RichStats' dealer/private medians showed as ##### (column C was 6 wide);
  widened, and corrected on existing workbooks only where C is still that
  exact old default.

**Duplicates (28/09/2026)**
* WHAT IT IS: one car stored as two identities. Identity is the site ad id
  when a capture carried one, a fingerprint otherwise, and every capture
  before August (v2 tracker May/June, screenshots, pasted text) carried
  none - so a car captured then and again later from a saved page is two
  identities that can never collide. It keeps coming back as those older
  cars are re-captured: 50 proven pairs had built up since the last merge.
* 28/09/2026: the 50 proven pairs merged on the real database with
  `merge-duplicates --apply` (backup `vpa_backup_20260927_235512.db`),
  3,628 -> 3,578 vehicles, all 6,359 observations and the full price
  multiset unchanged, 57 observations re-pointed, verified row by row
  against a pre-merge snapshot and against a rehearsal on a copy.
* `vpa/dupes.py` + `/duplicates`: the page for the rest. PROVEN groups
  (every merge.py rule) merge in one click; FOR YOUR JUDGEMENT groups fail
  only for lack of proof (round odometer, or no site ad id at all - 100 of
  them on 28/09) and merge only when you say; groups the data contradicts
  (two prices on one day, odometer backwards, year/state conflict, no
  variant, two different ad ids = a relisted advert) are counted and never
  offered. Rules live in `merge._failures()`, shared by the CLI and the page.
* Every page merge is logged in `merge_log` (vehicle row, which observation
  ids moved, star, the survivor's prior first/last_seen) and undone EXACTLY
  (`tests/check_dupes.py` compares every table before/after). A merge
  request names groups by key set and is refused whole if any group is no
  longer offered in that shape. "Different cars" goes in `not_duplicates`
  (sorted key set - a new identity joining re-asks) and changes no data;
  Explore's "duplicate?" tag obeys it and links to the pair.
* The header's amber "N possible duplicates" button comes from
  `/api/status` (`dupes.summary()`, cached on a data version), so an import
  that re-captures a stored car lights it on every open page.
* The May v2 captures (BASE- ids) say "dealer" for 84 of 90 cars - a v2
  default, not a reading. A seller mismatch against one is shown as weak
  evidence and not held against the pair. The stored values are unchanged.

**Advert links (27/09/2026)**
* the main site's own links are bare paths (`/cars/details/.../EXA-AD-1/`) and the
  database stores them exactly as they came. Put in a page as they stood,
  they resolved against the app (127.0.0.1), so every "open" link in Explore
  went nowhere. `vpa/links.py` puts the site's address in front wherever a
  link leaves the app (Explore payloads, watchlist, turnover, the Bulk
  session list, the workbook's `_Data` url column, report CSVs). The stored
  value is never changed (`tests/check_sitepages.py` pins that).
* `vpaAdLinks(url, gone)` in the shared page script draws two links: the
  advert, and the Internet Archive's list of copies of the same address. A
  car no longer listed gets the archive link first - a site usually takes
  a sold car's page down. The archive may hold no copy; its own page says so.
* 3,118 main-site cars have a link. The rest (about 510 with no site ad id -
  BASE-/IMG- keys, classifieds-site rows, early imports) cannot be tied to one advert
  and get no links rather than a guessed one.
* A bare path from a site not listed in `links.SITES` gets no link. Adding
  a site is one line there.
* The cars with no link are every capture before August: 15/05 and 14/06
  came through the v2 tracker (its `listings.url` is empty for all 243, and
  its ids are internal hashes, not ad ids) and 28/07 was pasted text. Every
  capture since August has links. No saved page from those dates exists, so
  nothing can be recovered. Those cars get a grey "search" link instead - a
  web search for the title and odometer, labelled as a search - rather than
  a blank that looks like a fault.
* Selecting a car scrolled its card only part way up. `scrollIntoView` with
  `block:"nearest"` aligns a card TALLER than the window, lying above it, by
  its BOTTOM edge. Every card scroll now goes through `vpaReveal(el)`, which
  puts the top just below any `[data-sticky]` bar (the restart banner) and
  leaves a card already wholly in view alone. The one `scrollIntoView` left
  is that sticky banner itself.

**Speed (27/09/2026)**
* Measured on the same machine, old code against new: moving to another
  Falcon trim 4.1 s -> 0.5 s, the whole Falcon line 6.5 s -> 1.0 s, all of
  Ford 5.3 s -> 1.1 s, Compare with three trims 4.9 s -> 0.75 s. The
  database was never the cost - reading all 6,344 rows takes 0.05 s. It was
  Python-level work repeated per row or per trim:
  - `forecast._theil_sen` called `scipy.stats.theilslopes` 501 times per
    forecast (the 500-draw bootstrap), and scipy spends ~2 ms a call on
    validation for arithmetic on five numbers. Replaced by scipy's own
    algorithm in numpy - bit-identical, ~100x faster per call.
  - `turnover.by_variant` measured every sibling trim on every click (155
    turnover runs for one Falcon trim), and each run sorted every car's rows
    separately and walked the frame with `iterrows`. Now vectorised, and the
    whole result is cached keyed on a hash of every input cell, so moving
    between trims of one model computes it once.
  - Explore's vehicle table re-sorted and sliced a DataFrame per car, and
    the sibling-trim list ran ten small filters per trim (one grouped
    aggregation now).
  - The bootstrap itself: `x` is the same on all 500 draws, so the pairwise
    set-up is done once, and `rng.choice(a, size=n, replace=True)` is
    `a[rng.integers(0, n, size=n)]` draw for draw (checked over 42,000
    draws), minus its overhead. Same random stream, same intervals.
* The cache key is a CONTENT hash on purpose. The status endpoint's
  `data_version` (last import id : counts) would miss a manual correction,
  which changes a price and no count - a cache keyed on it would serve the
  pre-edit numbers. With a content key the worst case is a miss.
* Proving it changed nothing: the committed code was extracted as a second
  package (`git archive <commit> vpa`, renamed `vpa_ref`) and both were run
  against the same copy of the real database for every make, model line and
  trim, pooled trims, filters, 21 Compare sets and 16 one-car verdicts - 667
  payloads, compared as sorted JSON: 667/667 identical. Keep doing that for
  any change sold as "only faster". Fix PYTHONHASHSEED on both sides when
  you do: the first run reported 8 mismatches, and they were the OLD code
  disagreeing with itself.
* That was a real, pre-existing wobble. Turnover's list of cars that left
  was sorted by (day, price), and exact ties kept the iteration order of a
  set of strings - which Python reshuffles on every restart, so the same
  data listed tied cars in a different order each launch (four different
  orders across six seeds, on three Lexus IS350s all last seen 05/08 at
  $22,990). The car's key now breaks the tie; the new code gives
  byte-identical output under three different seeds.
* Slow is survivable; wrong is not. The server answers on threads, so two
  quick clicks could land in the wrong order and draw trim A's numbers under
  trim B's name. `vpaLatest(channel, url)` aborts the older request and
  resolves a superseded one to null; the pages render nothing for null.

**The shared stylesheet**
* A page-level class with a generic name will fight the chrome. Explore had
  `.danger{background:#a03030;color:#fff}` for one button, and it matched
  the nav's `navbtn danger` too — Shut down rendered as a solid red block
  with its label invisible. Base rules now scope colour classes to the
  element (`button.danger`); keep page rules specific to the page.
* A `.ptitle span{color:dim}` rule greys EVERY span in a title, including a
  coloured badge placed there (the "from your data" tag went dark-on-blue).
  The base rule now only touches unclassed or id'd spans.
* Compare's `scale(lo,hi,px0,px1)` draws `lo` at `px0`. It used to draw it
  at `px1` whatever was passed, which is right for a y axis and mirrored
  every x axis: the price-range and odometer charts ran right-to-left for
  their whole life, and nobody could tell until the axes got round-number
  labels. y axes pass `(bottom, top)`.
* Verify pages against a COPY of the database (SQLite backup API to the
  scratchpad, then `--db` it and check `/api/status`), and screenshot with
  headless Chrome on its own `--user-data-dir` with `--timeout`; the in-app
  browser pane often will not render a screenshot.

**Excel / openpyxl**
* Post-2007 functions need the `_xlfn.` prefix in the file
  (`_xlfn.MAXIFS`, `_xlfn.MODE.SNGL`, `_xlfn.AGGREGATE`) or Excel shows
  `#NAME?`.
* Charts render **no axis labels** unless *both* `axis.delete = False` and
  `axis.tickLblPos = "nextTo"` are set — `_show_axes()` owns this; route
  every new chart through it.
* Chart blocks must be **sized to the data**. Fixed oversized slots made
  every chart trail through dozens of empty categories (line to $0,
  unreadable axis).
* Renaming a backend sheet strands view formulas — Excel reports the
  missing sheet as a broken *external workbook*. `LAYOUT_VERSION` in
  `viewer_xlsx.py` guards this; **bump it whenever a backend sheet or named
  range that views reference is renamed or removed.**
* Excel's `MODE.SNGL` breaks ties by data order; our rule picks the lowest
  tied bucket. Same count, possibly different representative.
* Selections match by **exact equality against three disjoint columns**
  (make / model_line / sel_key). An earlier prefix-matching trick made
  "G6E Turbo FG" silently pool "G6E Turbo FG MkII" and "FG X" — 66 rows
  instead of 12. Never prefix-match human vocabulary.

* **A saved page can be a bot-protection challenge, not a capture.** It
  arrives with HTTP 200, saves without error, is ~777 bytes and parses to
  zero rows. Broad searches trip it more often, but not only them — one
  narrow trim search was also blocked while a broad `Lexus cars` search
  succeeded, so it tracks request volume, not breadth. Check the file SIZE
  first: a real results page is several hundred kilobytes.

* **A cascade has to be RECORDED, not just performed.** Deleting a vehicle's
  last sighting also deletes the vehicle. Recording only the sighting made the
  deletion un-undoable — the restore tried to re-attach a sighting to a
  vehicle that no longer existed and hit the foreign key. Whatever a write
  removes as a side effect must be stored alongside what it was asked to
  remove, and put back first on undo.
* When the shape of a stored payload changes, every reader of it changes too.
  Wrapping a deleted sighting as `{observation, vehicle}` broke the
  expected-total arithmetic that still read `before["price_aud"]`, and every
  last-sighting deletion rolled back — correctly, because the invariant caught
  the disagreement.

* **Never let a browser cache anything this app serves.** The pages carry
  their JavaScript inline, so a cached page pins an OLD copy of the client
  code against today's API: the shell renders — picker, headings, tab strip —
  then throws part-way through, leaving every panel blank with NO console
  error. The server meanwhile answers every probe perfectly, the served HTML
  is byte-identical to source, and the same URL renders fine in a different
  browser. That combination is the signature of a stale cache and nothing
  else; it cost a long diagnosis on 06/08/2026 (checked stale process, wrong
  database, payload shape and JS exceptions first). `_send` now sets
  `Cache-Control: no-store, no-cache, must-revalidate` on everything, with a
  test per page.

* **The app is a long-lived process; it does not hot-reload.** After an
  upgrade, the page it serves can call an endpoint the *running* process has
  never heard of, which surfaced as `alert("unknown endpoint")` and read like
  a broken feature rather than "restart me". Both 404 branches now return
  `{"stale": true}` with the path and the fix, and every endpoint added after
  launch goes through `apiCall()` on the page, which raises one banner with a
  Shut down button instead of an alert per click.
* `document.querySelector(".wrap,body")` returns the first match in DOCUMENT
  order, not selector order — `<body>` precedes `.wrap`, so the banner landed
  outside the page container. Use an explicit `a || b` fallback.

**Module layout**
* **`if __name__ == "__main__"` must be the LAST statement in a module whose
  body assigns anything below it.** `main()` blocks in `serve_forever()`, so
  with the block above `PAGE = _chrome(...)` the assignment never ran and
  `python -m vpa.importui` served every page except `/`, which died on
  `NameError: PAGE`. The CLI path (`python -m vpa import-ui`) *imports* the
  module, completes the body, and hid it — as did every test, which imports
  `_Handler` directly. Caught only by running the module in a subprocess.
* Page routes now split the query string off before matching, so `/?v=1`
  serves the import page instead of 404ing.

**Python / platform**
* `time_ns()` on Windows ticks in ~15 ms quanta — never use a clock as a
  uniqueness token. `quickadd.commit_payload()` uses `uuid4`.
* pandas 3: `GroupBy.last()` takes the last non-null value *per column* and
  can stitch two rows together. Use `.tail(1)`. (`analyze.prep`)
* `SystemExit` is a `BaseException` — raising it in library code sails past
  `except Exception` in the web handler and kills the request thread. Use
  `RuntimeError`.
* Money/odometer regexes must use `[ ,]` between digit groups, never `\s`,
  or they swallow the following line.
* A long-running server keeps the code it started with (lazy imports are
  cached too). `_BUILD` in `importui.py` hashes the whole package mtime so a
  new launch replaces a stale instance.

**Browser**
* `toISOString()` is UTC and shifts the date in AEST — use the `localISO()`
  helper.
* Setting `textContent` on a container destroys its child elements. This was
  the "import only works once" bug: the hidden `<input type=file>` lived
  inside the drop zone. Also clear `input.value` after each read or picking
  the same file twice fires no `change` event.

**Data**
* the classifieds site **saved HTML is an empty shell** — zero listings.
  Copy-paste is the only viable FB path.
* The main site's saved pages hide seller and state in per-listing analytics blobs
  keyed by ad id, not in the JSON-LD.
* **A saved DETAILS page put its full URL in JSON-LD `@id` and no `sku`.**
  Taking `@id` verbatim keyed that car by URL while every search-page capture
  of the SAME car was keyed `EXA-AD-20672993` — one car, two identities,
  silently. The ad id is now extracted from `sku`/`@id`/`identifier`/`url`
  before any of them is used raw.
* **`vehicleConfiguration` is not a trim.** On a details page it is
  the full spec string `FG X XR6 Turbo Sedan 4dr Spts Auto 6sp 4.0T`, which
  stored as a variant invents a trim nothing else will ever match. The title
  gives `XR6 Turbo FG X`, which is what every search capture already
  produced, so the title wins.
* **The same trim reaches the database under different names far more often
  than it reaches it with a wrong price.** A bad price is visible; a bad
  name silently halves a pool. Anything redundant with a column it already
  has — model year, body style, gearbox — must come out of the variant, or
  it becomes a pool boundary.
* **An analytics field named `colour` is not necessarily the paint.** A
  details page carries several tracking blobs that disagree: page-level
  tracking gives the exterior colour, the enquiry-form `metaData` gives the
  *interior*. Reading the convenient one reported a Kinetic Blue Falcon as
  black. When a page renders a **labelled** value a human can read
  ("Exterior colour · Kinetic"), prefer that over any machine-readable
  field with a plausible name — the label is the only thing that states
  what the value actually means. Applies to any future field lifted out of
  these blobs.
* **A per-day statistic and an all-time statistic can both be right.** The
  Falcon XR6 Turbo above is 1st cheapest of 12 on each day it was captured
  and 3rd of 44 against the all-time pool, because the pool includes cars
  last seen in earlier, dearer months. Neither is wrong; a card that shows
  one must say which.
* Re-importing the same file *after a parser upgrade* creates twins,
  because identity is derived differently. Check for duplicates after any
  change to `dedupe_key()` or the adapters.
