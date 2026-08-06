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
| **Import** | preview → edit → commit. Nothing is written until you say so. |
| **Explore** | what is one trim worth, and how does one specific car compare to it |
| **Compare** | several trims side by side, on identical statistics |
| **Quick add** | type a price you saw, without a full capture |

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
python -m vpa merge-duplicates             # dry run; --apply to act
python -m vpa edit --history               # manual corrections, and undo
```

## How to collect listings

1. Search on the listing site as you normally would, in your own browser.
2. **Ctrl+S** the results page, or select the results and copy them.
3. Drop the file(s) onto the Import page — several at once is fine. Each
   file keeps its own capture date, taken from when you saved it.
4. Check the preview, fix anything wrong, commit.

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
- **A watchlist**: star specific cars and see what each new import did to
  them, including the figure that matters — how a car moved *relative to its
  own trim*.
- **Excel workbook**: a full pivot-and-chart view, rebuilt on demand.

## Data integrity

The database is the point of the tool, so writing to it is deliberately hard:

- Import is preview-first; nothing is stored until you commit.
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

Thirteen suites, ~380 checks: the core pipeline, title parsing, the import UI
over live HTTP, the analysis pages, manual editing, the watchlist, identity
repair and merge, and the Excel workbook driven through real Excel via COM
(that last one needs Excel installed, and is the slow one).

## Layout

```
vpa/            the package - the only irreplaceable code
  adapters/     one module per input shape (saved HTML, pasted text, CSV/JSON)
  analyze.py    the shared statistics engine
  explore.py    the Explore page and one-car analysis
  edit.py       manual corrections, with backup / transaction / undo
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
