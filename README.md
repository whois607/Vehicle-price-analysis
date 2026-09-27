# Vehicle Price Analysis (AU)

A local, single-user price-analysis tool for the Australian used car and
motorbike market. You collect listings by hand; it normalises them, keeps
price history in SQLite, and tells you what a given trim is worth, which
individual cars are cheap for their kilometres, and where your own shortlist
sits — **with explicit uncertainty**. It says "not enough data" rather than
inventing a curve.

Everything runs on your own machine. Nothing is uploaded anywhere.

## What this is not

**It is not a scraper.** It has no fetching code, makes no requests to any
listing site, logs into nothing, and cannot get past bot protection. It reads
files *you* already have: pages you saved yourself with Ctrl+S, text you
copied, or CSV/JSON you exported.

That is a deliberate design constraint, not an oversight. The major Australian
listing sites have no public price API and their terms prohibit automated
collection, so this tool stays on the other side of that line and always
will. If you were hoping for something that harvests listings automatically,
this is the wrong repository.

It does include a detector for the case where a site served you a
bot-protection page instead of results (`vpa/adapters/challenge.py`). That
exists so the app can *tell you the file is not a listings page* and stop —
it does nothing to circumvent the challenge.

## Your data stays yours

`.gitignore` excludes `data/` and `output/` entirely. That covers your
database, your backups, your saved pages and your exported reports. Saved
listing pages in particular are copies of someone else's content and must not
be redistributed — do not commit them, and do not commit your database.

## Install

Needs Python 3.11 or newer.

```bash
python -m pip install -r requirements.txt
```

Only `pandas`, `numpy` and `openpyxl` are needed for the core. The rest cover
forecasting and the CLI report charts; the app degrades gracefully without
them and says which method it used.

## Run

```bash
python -m vpa import-ui
```

That opens the app at `http://127.0.0.1:8765` — four pages, all local:

| page | what it answers |
|---|---|
| **Overview** | what you have collected, when, and whether the numbers add up (the data check) |
| **Add data** | three modes: **Import with preview** (preview → edit → commit; nothing is written until you say so), **Bulk upload** (many files, no preview, backed up and undoable), **Quick add** (type a price you saw) |
| **Explore** | what is one trim worth, where it is heading, and how one specific car compares to it |
| **Compare** | several trims or models side by side, on identical statistics — including how fast each one clears |

On Windows, `Setup Import App.bat` creates a desktop shortcut so you can
double-click it instead.

### Command line

```bash
python -m vpa status                       # what is in the dataset
python -m vpa ingest saved_page.html       # a page you saved
python -m vpa ingest listings.csv --source carsales
python -m vpa add --make Ford --model Falcon --year 2012 --price "$18,990" --odo 145000
python -m vpa report --segment "ford falcon"
python -m vpa report --all                 # every segment
python -m vpa export                       # dataset + summaries to CSV
python -m vpa viewer                       # build the Excel workbook
python -m vpa verify                       # cross-check the data against itself
python -m vpa merge-duplicates             # dry run; --apply to act (proven only;
                                           # the app's Duplicates page does the rest)
python -m vpa edit --history               # manual corrections, and undo
```

## How to collect listings

1. Search on the listing site as you normally would, in your own browser.
2. **Ctrl+S** the results page, or select the results and copy them.
3. Drop the file(s) onto **Add data** — several at once is fine. Each
   file keeps its own capture date, taken from when you saved it.
4. Check the preview, fix anything wrong, commit.

Once a make, model or trim parses cleanly and you have checked it a few
times, switch Add data to **Bulk upload**: drop the whole week's files and they land with
no preview. It backs up first and any run can be undone in full.

Repeat weekly and you have price history. The same listing seen on three
dates is three observations sharing one identity, not three cars.

## What it works out

- **Per trim**: median, quartiles, spread, supply, and how each moves per
  capture day.
- **Mileage adjustment**: a robust (Huber) fit of log price against odometer,
  so every car can be restated at the same kilometres. Refused, with the
  reason printed, when the odometer spread is too narrow to support it.
- **One car vs its trim**: rank, percentile, expected price for its
  kilometres, nearest comparables, and like-for-like splits by seller, state
  and model year. Every panel is gated — below eight comparable vehicles it
  declines to rank rather than guessing.
- **Forecasts**: a ladder from robust trend to exponential smoothing, with
  intervals, and a stated refusal when there is not enough history.
- **Turnover**: what happened *between* the captures. Each capture is split
  into cars carried over, cars back in view, brand-new listings, and cars
  that cannot be tracked at all; cars last seen on a day hang below the line.
  From that: how fast a trim clears (as a rate per 30 days and a half-life),
  roughly how long a car sits before it goes, how old the standing stock is,
  whether the cars that left were the cheap ones for their day, and how every
  trim in the model line compares on the same measures. The Compare page
  carries the same thing across *different* vehicles: the stats side by side,
  plus a chart of what arrived and what left between each pair of captures.

  It refuses rather than guesses, which is most of the work: listings with no
  site ad id cannot be recognised across days, two captures that share no
  cars are two different searches rather than a market that emptied, and a
  capture much bigger than the one before inflates arrivals while one much
  smaller inflates exits — so those two get opposite treatment. **It never
  says "sold."** A listing can vanish by selling, being withdrawn, expiring,
  or being re-listed under a new id.
- **A watchlist**: star specific cars and see what each new import did to
  them, including the figure that matters — how a car moved *relative to its
  own trim*.
- **Excel workbook**: a full pivot-and-chart view, rebuilt on demand (a few
  seconds, with progress on the button; the button shows a dot when the
  workbook no longer matches the database).

## Data integrity

The database is the point of the tool, so writing to it is deliberately hard:

- Import is preview-first; nothing is stored until you commit.
- **Bulk** deliberately is not - that is what it is for - so it takes a backup
  before every run, refuses anything that is not a listings page, and records
  each run so it can be undone in full: exactly the sightings it added, only
  the cars it introduced, and every car that already existed put back.
- Manual corrections (`vpa/edit.py`) take a backup, run in one transaction,
  check invariants before committing, roll back completely on any failure,
  and record every change with its old value so it can be undone.
- Identity migrations (`repair.py`) and identity merges (`merge.py`) each have
  their own conditions and only act on what they can prove.
- `python -m vpa verify` re-computes the totals two independent ways and
  compares them, checks the Excel workbook against the live database, and
  reports duplicates and split trim names.

## Tests

```bash
python -m tests.run_all
```

Twenty suites, ~585 checks: the core pipeline, title parsing, carsales
pages, advert links, duplicates, building the workbook, the import UI
over live HTTP, the analysis pages, manual editing, the watchlist, turnover
(and every case where it must refuse), bulk upload and its undo, identity
repair and merge, and the Excel workbook driven through real Excel via COM (that last one needs Excel
installed, and is the slow one).

## Layout

```
vpa/            the package - the only irreplaceable code
  adapters/     one module per input shape (saved HTML, pasted text, CSV/JSON)
  analyze.py    the shared statistics engine
  explore.py    the Explore page and one-car analysis
  edit.py       manual corrections, with backup / transaction / undo
  turnover.py   what left, what arrived, and when it refuses to say
  bulk.py       direct upload: no preview, backup first, undo after
  starred.py    the watchlist
tests/          verification suites
tools/          Windows launcher
data/           YOUR data - gitignored
output/         generated reports and the workbook - gitignored
```

`NOTES.md` is the engineering log: why things are built the way they are,
what was tried and rejected, and the bugs that cost real time.

## Licence

MIT — see `LICENSE`.
