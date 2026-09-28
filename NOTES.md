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
15/05/2026 → 28/07/2026, sources carsales 708 / facebook 61, 77 ingest
batches.**

### Tables

**`vehicles`** — one row per *physical listing* (an ad), not per car model.
```
key TEXT PRIMARY KEY          -- see "Identity" below
source, listing_id            -- 'carsales'|'facebook'|'quick_add'|..., site ad id or NULL
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
  Saved carsales/bikesales HTML yields `SSE-AD-…`/`OAG-AD-…`; this is the
  reliable path.
* Otherwise a fingerprint over `source, make, model, year, variant, state`
  and odometer bucketed to 5,000 km.
* **Extra rule:** when there is neither an id nor an odometer (Facebook
  tiles, quick adds), a $500 price bucket is appended, so five same-model
  ads on one day stay distinct. Consequence: an FB re-list at a new price
  reads as a new listing. Unavoidable without stable ids.

### Derived fields — computed, never stored

| field | where | meaning |
|---|---|---|
| `detail_level` | `Store.frame()` (vpa/store.py) | `brief` if `source=='quick_add'` else `full` |
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
  assets/                  app icon
  tests/
    synthetic/             GENERATED fixtures + out/ report tree - safe to delete
  data/                    PRIVATE, irreplaceable
    vpa.db                 the database
    backups/               vpa_backup_*.db snapshots (Store.backup())
    raw/                   the 16 saved carsales/Facebook .htm captures
  output/                  GENERATED - rebuild with `python -m vpa report --all`
    reports/<segment>/     trend/forecast .html + .png, stats/listings .csv
    reports/plotly.min.js  ONE shared copy for every report page
    exports/               dataset.csv and summaries
    viewer.xlsx            the Excel workbook
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
  per-listing seller/state from carsales' analytics blobs; `detect_source()`
  matches `_SITE_HINTS`.
* `pastetext.py` — `parse()` handles both title-led and **price-led**
  (Facebook) tile layouts; `split_title()` splits make/model/variant.
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
  Facebook shell) named per file with its reason;
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
  * **The marker is the interstitial, not the vendor.** Every carsales page
    loads DataDome's ordinary tag (`dd.carsales.com.au/tags.js`); only the
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
  * **The badge line.** A carsales card puts the model on the title line and
    the trim on the line below it (`2007 Lexus LS` / `LS460 Version U I
    Package Auto R`). The paste path never read the second line, so every
    Lexus LS on a results page — LS460, LS500, LS500h, F Sport, Sports
    Luxury, three Version packages — parsed as one trimless `Lexus LS`.
    `is_badge_line()` is written as ordered REJECTIONS (price, odometer,
    engine spec, body word, state, `Dealer used • VIC`, UI furniture,
    year-led next title, Marketplace location); anything not clearly a badge
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
    Sports`, `LS500h Sports Luxury`, BMW `M Sport`; carsales writes the
    gearbox as `Spts Auto`, so only the abbreviation is a descriptor);
    `quattro`, `xDrive`, `4MATIC`, `AllGrip`; and every series/chassis code
    (`FG X`, `FG MkII`, `F32`, `PX MkIII`, `GUN126R`, `W205`) — the whole
    Falcon dataset is separated by exactly such codes.
  * **Also removed**, because each duplicates a column and otherwise splits
    one trim: `MY##` model-year codes (on either side of the gearbox), and
    body styles carsales appends AFTER the gearbox on utes (`XR6 Turbo FG X
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
    file emits `SSE-AD-20672993` 15 times and `sse-ad-20672993` 10 times; a
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
  state not conflicting, exactly one real site ad id (`SSE-AD-`/`OAG-AD-`,
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
* `tools/make_launcher.py` — `draw_icon()`, `make_shortcut()`. One-time,
  run via `Setup Import App.bat`.

---

## 3. What's done

All of the following is built **and** verified by an automated suite (see §7).

* **Ingestion** — CSV, JSON, saved HTML (JSON-LD + text fallback), pasted
  text (title-led and Facebook price-led), manual `vpa add`, and v2 import.
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
   (v2 `BASE-`/`IMG-` ids → real `SSE/OAG` ids → the 28/07 batch which was
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
5. **Windows-only launcher.** `Setup Import App.bat`, the `.lnk`, `pythonw`
   and `os.startfile` (used to open the workbook) are Windows-specific. The
   CLI and `python -m vpa import-ui` work anywhere.
6. **Facebook rows carry no trim and no odometer**, so they land under the
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
7. **Trim vocabulary is carsales-canonical** (decided 06/07/2026). Rows from
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
   requirements.txt` then `Setup Import App.bat`, which rebuilds the
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
& $py -m tests.run_all          # all fourteen suites, ~120s
& $py -m vpa verify             # cross-check the real database (seconds)
```

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
| `tests/check_perf.py` (19) | every speed-up against the code it replaced, kept verbatim in the test: numpy Theil-Sen equals `scipy.stats.theilslopes` bit for bit on 5,000 random, tied and all-x-equal inputs; the forecast bootstrap gives identical intervals to the old `rng.choice` loop; `cohort`/`_day_sets` equal the old pandas versions on None/NaN/pd.NA/int/derived ids and a missing day; the `by_variant` cache computes once for two requests, applies the highlighted trim per request, hands out copies, and MISSES after a one-dollar price edit that changes no count; Explore and Compare fetch latest-wins and show a busy state |
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
* From 27/07/2026 no carsales capture stored a seller type or state: 3,264 of
  3,564 carsales cars have neither. Carsales moved its per-listing analytics
  from prefixed keys with the fields after the ad id to flat objects with
  the state BEFORE it (`{.."state":"VIC","networkId":"SSE-AD-1",..,
  "adtype":"Private"}`). Nothing errored: the regex simply matched nothing,
  every listing still parsed, and the gap only showed as a thin "Who is
  selling" tab. `htmlpage._flat_tracking` reads the new shape from each
  object's own braces, skips an object naming two cars, and drops a field
  whose repeated copies disagree. Body type was never stored from any page:
  JSON-LD `bodyType` is blank; each tile's `keyDetailsTexts` states it but
  carries no ad id, so it is used only when the tiles provably line up (same
  count, same order, every odometer equal). Across the 61 saved pages:
  seller 12 -> 675 of 722 listings, state 9 -> 675, body 3 -> 619, and no
  previously parsed value changed. The remaining gaps are SHRM-AD showroom
  "New car" entries, left blank rather than called dealer.
* Worth a periodic check: parse the newest saved page and look at the
  per-field fill (`tests/check_carsales.py` pins both formats).

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
* carsales' own links are bare paths (`/cars/details/.../SSE-AD-1/`) and the
  database stores them exactly as they came. Put in a page as they stood,
  they resolved against the app (127.0.0.1), so every "open" link in Explore
  went nowhere. `vpa/links.py` puts the site's address in front wherever a
  link leaves the app (Explore payloads, watchlist, turnover, the Bulk
  session list, the workbook's `_Data` url column, report CSVs). The stored
  value is never changed (`tests/check_links.py` pins that).
* `vpaAdLinks(url, gone)` in the shared page script draws two links: the
  advert, and the Internet Archive's list of copies of the same address. A
  car no longer listed gets the archive link first - carsales usually takes
  a sold car's page down. The archive may hold no copy; its own page says so.
* 3,118 carsales cars have a link. The rest (about 510 with no site ad id -
  BASE-/IMG- keys, Facebook, early imports) cannot be tied to one advert
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
* Facebook Marketplace **saved HTML is an empty shell** — zero listings.
  Copy-paste is the only viable FB path.
* Carsales saved pages hide seller and state in per-listing analytics blobs
  keyed by ad id, not in the JSON-LD.
* **A saved DETAILS page put its full URL in JSON-LD `@id` and no `sku`.**
  Taking `@id` verbatim keyed that car by URL while every search-page capture
  of the SAME car was keyed `SSE-AD-20672993` — one car, two identities,
  silently. The ad id is now extracted from `sku`/`@id`/`identifier`/`url`
  before any of them is used raw.
* **`vehicleConfiguration` is not a trim.** On a carsales details page it is
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
