"""CLI: ingest / add / import-v2 / report / export / status.

    python -m vpa ingest listings.html --source mysite
    python -m vpa add --make Ford --model Falcon --year 2012 --price 18990
    python -m vpa import-v2
    python -m vpa report --segment "ford falcon"
    python -m vpa report --all
    python -m vpa export
    python -m vpa status

Console output is ASCII-only (Windows cp1252-safe).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .store import Store, DEFAULT_DB
from .report import DEFAULT_OUT


def _cmd_ingest(args) -> int:
    from .adapters import load_file
    path = Path(args.path)
    if not path.exists():
        print(f"File not found: {path}")
        return 1
    records, fmt = load_file(path, source=args.source, fmt=args.format,
                             capture_date=args.capture_date)
    if not records:
        print(f"Parsed 0 listings from {path.name} (format: {fmt}). "
              "Check the file, or try --format text / --format csv.")
        if fmt == "html":
            print("Note: some sites' saved pages are empty shells - "
                  "select the results text on the page and ingest it as text "
                  "instead.")
        return 1
    with Store(args.db) as store:
        counts = store.ingest(records, source_file=str(path), adapter=fmt)
    sweep = None
    if fmt == "html":
        from . import details, photos, sweeps
        html = path.read_text(encoding="utf-8", errors="replace")
        day = max((str(r.capture_date) for r in records), default=None)
        photos.remember_html(args.db, html, day)
        details.remember_html(args.db, html, day)
        sweep = sweeps.note_html(args.db, html, records)
    print(f"Ingested {path.name} [{fmt}]: parsed {counts['rows_parsed']}, "
          f"new vehicles {counts['vehicles_new']}, new observations "
          f"{counts['obs_new']}, duplicates skipped {counts['obs_duplicate']}")
    if sweep:
        from .dates import au
        print(au(f"Search ({sweep['day']}): it stated {sweep['total']:,}; "
                 f"{sweep['saved']:,} saved so far over {sweep['pages']} page(s)"
                 + (" - not all of it yet" if sweep["short"] else " - complete")))
    _warn_unpriced(records)
    return 0


def _warn_unpriced(records) -> None:
    unpriced = [r for r in records if r.price_aud is None]
    if unpriced:
        notes = {}
        for r in unpriced:
            notes[r.price_note or "missing"] = notes.get(r.price_note or "missing", 0) + 1
        detail = ", ".join(f"{k}: {v}" for k, v in notes.items())
        print(f"Note: {len(unpriced)} listing(s) had no usable price ({detail}) - "
              "stored in the dataset but excluded from stats and forecasts.")


def _cmd_add(args) -> int:
    from .schema import make_record
    raw = {"make": args.make, "model": args.model, "variant": args.variant,
           "year": args.year, "price": args.price, "odometer": args.odo,
           "state": args.state, "seller_type": args.seller,
           "vehicle_type": args.vehicle_type, "capture_date": args.date,
           "raw_title": args.title, "url": args.url, "listing_id": args.id,
           "body_type": args.body}
    rec = make_record({k: v for k, v in raw.items() if v is not None},
                      source=args.source)
    if rec is None:
        print("Need at least --make and --model.")
        return 1
    with Store(args.db) as store:
        counts = store.ingest([rec], source_file="(manual)", adapter="manual")
    status = "added" if counts["obs_new"] else "duplicate of an existing observation"
    print(f"{rec.year or '?'} {rec.make} {rec.model} {rec.variant or ''} "
          f"@ {'A$%s' % format(rec.price_aud, ',.0f') if rec.price_aud else 'no price'} "
          f"on {rec.capture_date}: {status}")
    return 0


def _cmd_import_v2(args) -> int:
    from .adapters import v2db
    try:
        records = v2db.load(args.path) if args.path else v2db.load()
    except FileNotFoundError as e:
        print(str(e))
        return 1
    with Store(args.db) as store:
        counts = store.ingest(records, source_file=str(args.path or v2db.DEFAULT_V2_DB),
                              adapter="v2db")
    print(f"Imported v2 history: parsed {counts['rows_parsed']}, new vehicles "
          f"{counts['vehicles_new']}, new observations {counts['obs_new']}, "
          f"duplicates skipped {counts['obs_duplicate']}")
    return 0


def _cmd_report(args) -> int:
    from .report import run_report
    if not args.segment and not args.all:
        print("Pick a segment (--segment \"ford falcon\") or run everything (--all).")
        return 1
    with Store(args.db) as store:
        results = run_report(
            store, query=args.segment, band=args.band, level=args.level,
            freq={"M": "MS", "W": "W"}.get(args.freq, "MS"),
            horizon=args.horizon, outdir=Path(args.out), min_n=args.min_n,
            band_width=args.band_width, make_charts=not args.no_charts,
            everything=args.all)
    return 0 if results else 1


def _cmd_export(args) -> int:
    from .report import export_all
    with Store(args.db) as store:
        paths = export_all(store, Path(args.out))
    for name, p in paths.items():
        print(f"exported {name}: {p}")
    return 0


def _cmd_repair(args) -> int:
    from .repair import main as repair_main
    argv = ["--db", args.db] + (["--apply"] if args.apply else [])
    return repair_main(argv)


def _cmd_backup(args) -> int:
    with Store(args.db) as store:
        dest = store.backup(args.out)
    print(f"backup written: {dest}")
    return 0


def _cmd_status(args) -> int:
    with Store(args.db) as store:
        s = store.status()
    print(f"database: {s['db_path']}")
    print(f"vehicles: {s['vehicles']}   observations: {s['observations']} "
          f"({s['priced_observations']} priced)")
    from .dates import au
    print(au(f"capture dates: {s['date_range'][0]} to {s['date_range'][1]}"))
    print(f"sources: " + (", ".join(f"{k}={v}" for k, v in s["sources"].items()) or "none"))
    print(f"ingest batches: {s['ingests']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vpa", description="AU used-vehicle price analysis (local, no scraping).")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("ingest", help="ingest a CSV/JSON/HTML/text file of listings")
    q.add_argument("path")
    q.add_argument("--source", help="a source label (your sites are named in data/sites.json), or other "
                                    "(auto-detected for HTML)")
    q.add_argument("--format", choices=["csv", "json", "html", "text"],
                   help="override format auto-detection")
    q.add_argument("--capture-date", help="YYYY-MM-DD; default: per-row date, "
                                          "else the file's modification date")
    q.set_defaults(fn=_cmd_ingest)

    q = sub.add_parser("add", help="add one listing by hand")
    q.add_argument("--make", required=True)
    q.add_argument("--model", required=True)
    q.add_argument("--variant")
    q.add_argument("--year", type=int)
    q.add_argument("--price", help="asking price, e.g. 18990 or '$18,990 neg'")
    q.add_argument("--odo", help="odometer km")
    q.add_argument("--state")
    q.add_argument("--seller", choices=["private", "dealer"])
    q.add_argument("--vehicle-type", choices=["car", "bike"], default=None)
    q.add_argument("--date", help="capture date YYYY-MM-DD or DD/MM/YYYY (default today)")
    q.add_argument("--source", default="manual")
    q.add_argument("--title"), q.add_argument("--url"), q.add_argument("--id")
    q.add_argument("--body", help="body or bike type")
    q.set_defaults(fn=_cmd_add)

    q = sub.add_parser("import-v2", help="import history from vehicle-price-tracker-v2")
    q.add_argument("--path", help="path to v2 tracker.db (default: sibling project)")
    q.set_defaults(fn=_cmd_import_v2)

    q = sub.add_parser("report", help="stats + forecast + charts for a segment or all")
    q.add_argument("--segment", help='e.g. "ford falcon" or "yz250f"')
    q.add_argument("--band", help="explicit year band, e.g. 2011-2014")
    q.add_argument("--all", action="store_true", help="every segment with enough data")
    q.add_argument("--level", choices=["band", "model_line", "body"], default="band",
                   help="segment granularity for --all (default: band)")
    q.add_argument("--band-width", type=int, default=3, help="years per band (default 3)")
    q.add_argument("--freq", choices=["M", "W"], default="M", help="bucket size")
    q.add_argument("--horizon", type=int, default=3, help="forecast months (default 3)")
    q.add_argument("--min-n", type=int, default=5,
                   help="skip segments with fewer vehicles than this in --all")
    q.add_argument("--no-charts", action="store_true")
    q.add_argument("--out", default=str(DEFAULT_OUT))
    q.set_defaults(fn=_cmd_report)

    q = sub.add_parser("export", help="dump dataset + summaries to CSV")
    q.add_argument("--out", default=str(DEFAULT_OUT))
    q.set_defaults(fn=_cmd_export)

    q = sub.add_parser("status", help="dataset overview")
    q.set_defaults(fn=_cmd_status)

    q = sub.add_parser("trims", help="how stored trim names are grouped; save or "
                                     "compare a record of it (read-only)")
    q.add_argument("--save", default=None, help="write a record of every car's group")
    q.add_argument("--against", default=None, help="compare with a saved record")
    q.add_argument("--unmatched", action="store_true",
                   help="list trims the brand rules place in no generation")
    q.set_defaults(fn=lambda a: __import__("vpa.trims", fromlist=["cli"]).cli(
        a.db, a.save, a.against, a.unmatched))

    q = sub.add_parser("backup", help="snapshot the database to data/backups/")
    q.add_argument("--out", default=None, help="destination directory")
    q.set_defaults(fn=_cmd_backup)

    q = sub.add_parser("repair-identities",
                       help="split vehicle identities that merged two cars "
                            "(dry run unless --apply)")
    q.add_argument("--apply", action="store_true",
                   help="perform the migration; a backup is taken first")
    q.set_defaults(fn=_cmd_repair)

    q = sub.add_parser("merge-duplicates",
                       help="fold identities that are provably the same car "
                            "into one (dry run unless --apply)")
    q.add_argument("--apply", action="store_true",
                   help="perform the merge; a backup is taken first")
    q.set_defaults(fn=_cmd_merge)

    q = sub.add_parser("verify", help="cross-check the data: SQL vs the analysis "
                                      "frame, the Excel workbook vs the database, "
                                      "identities, and value sanity")
    q.add_argument("--workbook", default=None,
                   help="workbook to check against (default output/viewer.xlsx)")
    q.set_defaults(fn=_cmd_verify)

    q = sub.add_parser("edit", help="list or undo manual corrections made in "
                                    "the app (edits are made on the Explore page)")
    q.add_argument("--history", action="store_true", help="list edit batches")
    q.add_argument("--undo", metavar="BATCH", help="put one batch back")
    q.set_defaults(fn=_cmd_edit)

    q = sub.add_parser("viewer", help="refresh the Excel viewer workbook (output/viewer.xlsx)")
    q.add_argument("--out", default=None, help="output .xlsx path")
    q.add_argument("--reset-styles", action="store_true",
                   help="also restore the default look, discarding fonts/fills/"
                        "widths you changed on the refreshed sheets")
    q.set_defaults(fn=_cmd_viewer)

    q = sub.add_parser("import-ui", help="open the local preview-before-commit import page")
    q.add_argument("--port", type=int, default=None)
    q.add_argument("--no-browser", action="store_true")
    q.set_defaults(fn=_cmd_import_ui)
    q = sub.add_parser("phone-serve", help="phone sync in the background (what the "
                       "Startup shortcut runs); only if switched on in the app")
    q.set_defaults(fn=_cmd_phone_serve)
    q = sub.add_parser("phone-copy", help="build the phone copy now (read-only on "
                       "the database; normally automatic)")
    q.add_argument("--force", action="store_true", help="rebuild even if up to date")
    q.add_argument("--out", default=None, help="output folder (default output/phone)")
    q.set_defaults(fn=_cmd_phone_copy)
    q = sub.add_parser("photos", help="how many cars have a photo; --backfill reads "
                       "the photo addresses in every kept saved page (writes only "
                       "data/photos.json, never the database)")
    q.add_argument("--backfill", action="store_true")
    q.set_defaults(fn=_cmd_photos)
    q = sub.add_parser("details", help="how many cars have listing details (condition, "
                       "engine, gearbox, price type...); --backfill reads them from every "
                       "kept saved page (writes only data/details.json, never the database)")
    q.add_argument("--backfill", action="store_true")
    q.set_defaults(fn=_cmd_details)
    return p


def _cmd_details(args) -> int:
    from . import details
    if args.backfill:
        r = details.backfill(args.db)
        print(f"Read {r['pages']} kept page(s): details for {r['ads_in_pages']:,} listing(s), "
              f"{r['changed']:,} new or updated.")
    else:
        r = details.coverage(args.db)
    print(f"{r['with_details']:,} of {r['cars']:,} cars have listing details "
          f"({r['stored_ads']:,} ads in {details.store_path(args.db)}).")
    return 0


def _cmd_photos(args) -> int:
    from . import photos
    if args.backfill:
        r = photos.backfill(args.db)
        print(f"Read {r['pages']} kept page(s): {r['ads_in_pages']:,} listing photo sets, "
              f"{r['changed']:,} new or updated.")
    else:
        r = photos.coverage(args.db)
    print(f"{r['with_photo']:,} of {r['cars']:,} cars have a photo "
          f"({r['stored_ads']:,} ads in {photos.store_path(args.db)}).")
    return 0


def _cmd_viewer(args) -> int:
    import time
    from .viewer_xlsx import generate, DEFAULT_OUT
    t0, last = time.time(), [""]

    def show(frac, text):
        # one line, rewritten in place: "[ 42%] Forecasts 80 of 190"
        line = f"\r[{frac * 100:3.0f}%] {text}"
        print(line.ljust(len(last[0])), end="", flush=True)
        last[0] = line

    try:
        out = generate(args.db, Path(args.out) if args.out else DEFAULT_OUT,
                       reset_styles=args.reset_styles, progress=show)
    except PermissionError:
        print("\nCannot write the workbook - it is open in Excel. "
              "Close viewer.xlsx and run this again. Nothing was changed.")
        return 1
    except RuntimeError as e:
        print("\n" + str(e))
        return 1
    print(f"\nviewer workbook written in {time.time() - t0:.0f}s: {out}")
    print("Sheets refreshed (your fonts/fills/widths kept). MyView and any "
          "sheets you added are untouched.")
    return 0


def _cmd_merge(args) -> int:
    from .merge import apply, plan, print_plan
    from .store import Store
    if args.apply:
        print_plan(apply(args.db), applied=True)
        return 0
    with Store(args.db) as store:
        print_plan(plan(store))
    return 0


def _cmd_edit(args) -> int:
    """Read-only unless --undo. Corrections themselves are made in the app,
    where the change is previewed field by field before it is saved."""
    from .edit import history, undo
    if args.undo:
        r = undo(args.db, args.undo)
        if not r.get("ok"):
            print(r.get("error"))
            return 1
        print(f"undone: {r['restored']} change(s) put back")
        print(f"backup taken first: {r['backup']}")
        print(f"now: {r['totals']['vehicles']:,} vehicles, "
              f"{r['totals']['observations']:,} observations, "
              f"${r['totals']['price_total']:,.0f} of prices")
        return 0
    rows = history(args.db)
    if not rows:
        print("no manual corrections have been made.")
        return 0
    print(f"{len(rows)} edit batch(es), newest first:\n")
    for h in rows:
        state = "UNDONE " if h["undone"] == h["n"] else "applied"
        print(f"  {h['ts']}  {h['batch']}  {h['n']:3d} change(s)  {state}  "
              f"{h['ops']}")
        if h["reason"]:
            print(f"      {h['reason'][:72]}")
    print("\nundo one with:  python -m vpa edit --undo <batch>")
    return 0


def _cmd_verify(args) -> int:
    """Exit 1 on a real failure so this can gate a script; warnings do not."""
    from .verify import print_report, verify_payload
    payload = verify_payload(args.db,
                             Path(args.workbook) if args.workbook else None)
    print_report(payload)
    return 0 if payload["ok"] else 1


def _cmd_phone_serve(args) -> int:
    from .importui import _open_log
    import sys
    if sys.stdout is None or sys.stderr is None:      # pythonw (Startup shortcut)
        log = _open_log()
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    from . import phone
    return phone.serve_forever(args.db)


def _cmd_phone_copy(args) -> int:
    from pathlib import Path
    from . import phonebuild, phonecfg
    return phonebuild.main(args.db, Path(args.out) if args.out else phonecfg.OUT,
                           force=args.force)


def _cmd_import_ui(args) -> int:
    from .importui import serve_quiet, DEFAULT_PORT
    serve_quiet(args.db, args.port or DEFAULT_PORT, open_browser=not args.no_browser)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
