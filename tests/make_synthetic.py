"""Generate a synthetic messy dataset with KNOWN ground truth.

The world being simulated:
  * Ford Falcon 2012 (band 2011-2013): 10 months (Sep 2025 - Jun 2026),
    12 listings/month. True median level falls $150/month from $17,000.
    Log-price falls with odometer at BETA per km (~ -3% / 10,000 km).
    Exactly one listing per month is 'Negotiable' (no usable price).
  * Yamaha YZ250F 2020: 5 listings across 2 months -> must be REFUSED a
    forecast (the too-little-data path).
  * One Falcon listed in May (pasted text, no site id -> fingerprint dedupe)
    re-appears in June $1,000 cheaper -> must become ONE vehicle with TWO
    price observations.
  * Deliberate mess everywhere: '$17,990' / '16,500 Drive Away' / 'Negotiable',
    'FORD'/'ford'/'Ford ' casing, '145,000 km'/'98k'/missing odo,
    'New South Wales'/'nsw'/'Penrith, NSW' states, AU DD/MM/YYYY dates.

Delivery is split across all four adapters:
  months 1-6  -> CSV   (messy headers; half the rows are title-only)
  months 7-8  -> JSON  (plus the sparse YZ250F bikes)
  month  9    -> pasted text (May 2026)
  month 10    -> saved HTML with JSON-LD, examplecars-flavoured (June 2026)
                 + a second text paste holding the June re-list

Writes files to tests/synthetic/ plus truth.json with the expected numbers.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from pathlib import Path

OUT = Path(__file__).resolve().parent / "synthetic" / "selfcheck"   # its own folder: suites run side by side

START_LEVEL = 17_000.0
SLOPE_PER_MONTH = -150.0
BETA = -3e-6                      # log-price per km
ODO_CENTER = 130_000.0
NOISE_SD = 0.03                   # log-space
MONTHS = ["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
          "2026-03", "2026-04", "2026-05", "2026-06"]
PER_MONTH = 12
TEXT_MONTH = 8                    # 2026-05, delivered as pasted text
HTML_MONTH = 9                    # 2026-06, delivered as saved HTML

MAKE_MESS = ["Ford", "FORD", "ford", "Ford "]
STATE_MESS = ["NSW", "nsw", "New South Wales", "Penrith, NSW", None, "VIC", "QLD"]
VARIANTS = ["XR6", "XR6 Turbo", "G6E"]


def true_level(month_idx: int) -> float:
    return START_LEVEL + SLOPE_PER_MONTH * month_idx


def gen_falcons(rng: random.Random) -> list[dict]:
    rows = []
    for m, month in enumerate(MONTHS):
        for i in range(PER_MONTH):
            if m == TEXT_MONTH:
                # text has no site ids -> dedupe is by fingerprint, which
                # buckets odometer by 5,000 km. Distinct buckets by
                # construction so no two synthetic cars merge.
                odo = 62_000.0 + i * 11_500.0
            else:
                odo = rng.uniform(60_000, 200_000) if rng.random() > 0.15 else None
            price_odo = odo if odo is not None else ODO_CENTER
            eps = rng.gauss(0, NOISE_SD)
            price = true_level(m) * math.exp(BETA * (price_odo - ODO_CENTER)) * math.exp(eps)
            price = float(round(price / 10) * 10)
            negotiable = (i == PER_MONTH - 1)   # exactly one per month
            rows.append({
                "month_idx": m, "month": month,
                "capture_date": f"{month}-{rng.randint(2, 27):02d}",
                "make": rng.choice(MAKE_MESS),
                "model": rng.choice(["Falcon", "falcon", "FALCON"]),
                "variant": rng.choice(VARIANTS), "year": 2012,
                "true_price": None if negotiable else price,
                "odo_km": odo,
                "state": rng.choice(STATE_MESS),
                "seller": rng.choice(["Private seller", "Dealer used", "private", None]),
                "negotiable": negotiable,
                "listing_id": f"SYN-{m:02d}-{i:02d}",
            })
    return rows


def price_text(r: dict, style: int) -> str:
    if r["negotiable"]:
        return "Negotiable"
    p = r["true_price"]
    return [f"${p:,.0f}", f"{p:,.0f}", f"${p:,.0f} Drive Away", str(int(p))][style % 4]


def odo_text(r: dict, style: int) -> str:
    if r["odo_km"] is None:
        return ""
    v = r["odo_km"]
    return [f"{v:,.0f} km", f"{round(v / 1000)}k km", str(int(v))][style % 3]


def main() -> dict:
    rng = random.Random(20260703)
    OUT.mkdir(parents=True, exist_ok=True)
    falcons = gen_falcons(rng)

    # --- months 1-6 -> CSV, messy headers, half the rows title-only --------
    csv_rows = [r for r in falcons if r["month_idx"] < 6]
    with open(OUT / "falcons_1.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Ad ID", "Title", "Make", "Model", "Variant", "Year",
                    "Price ($)", "KMs", "Rego State", "Seller", "Date seen"])
        for i, r in enumerate(csv_rows):
            title_only = i % 2 == 0
            d = r["capture_date"]
            w.writerow([
                r["listing_id"],
                f"{r['year']} {r['make'].strip()} {r['model']} {r['variant']}",
                "" if title_only else r["make"],
                "" if title_only else r["model"],
                "" if title_only else r["variant"],
                "" if title_only else r["year"],
                price_text(r, i), odo_text(r, i), r["state"] or "",
                r["seller"] or "", f"{d[8:10]}/{d[5:7]}/{d[0:4]}"])  # DD/MM/YYYY

    # --- months 7-8 -> JSON, plus the sparse YZ250F bikes -------------------
    json_rows = [r for r in falcons if r["month_idx"] in (6, 7)]
    payload = [{
        "source": "examplecars",
        "listing_id": r["listing_id"], "make": r["make"], "model": r["model"],
        "variant": r["variant"], "year": r["year"],
        "price": price_text(r, i), "odometer_km": r["odo_km"],
        "state": r["state"], "seller_type": r["seller"],
        "capture_date": r["capture_date"],
        "url": f"https://example.local/{r['listing_id']}",
    } for i, r in enumerate(json_rows)]
    bikes = [{
        "source": "examplebikes",
        "listing_id": f"BIKE-{i}", "make": "yamaha", "model": "yz250f",
        "year": 2020, "price": f"${7_500 + 300 * i:,}",
        "odometer_km": None, "state": "QLD", "seller_type": "private",
        "capture_date": d, "url": f"https://example.local/bike{i}",
    } for i, d in enumerate(["2026-05-04", "2026-05-18", "2026-05-25",
                             "2026-06-07", "2026-06-21"])]
    with open(OUT / "falcons_2.json", "w", encoding="utf-8") as f:
        json.dump({"listings": payload + bikes}, f, indent=1)

    # --- month 9 -> pasted text (ingested with --capture-date 2026-05-15) --
    text_rows = [r for r in falcons if r["month_idx"] == TEXT_MONTH]
    relist = next(r for r in text_rows if r["true_price"] is not None)
    blocks = []
    for i, r in enumerate(text_rows):
        lines = [f"{r['year']} {r['make'].strip()} {r['model'].title()} {r['variant']}"]
        if r["true_price"]:
            lines.append(f"${r['true_price']:,.0f}" + (" Negotiable" if i % 5 == 0 else ""))
        else:
            lines.append("Negotiable")
        lines.append(f"{r['odo_km']:,.0f} km")
        lines.append(r["state"] or "Sydney")
        if r["seller"]:
            lines.append(r["seller"])
        blocks.append("\n".join(lines))
    (OUT / "falcons_3.txt").write_text("\n\n".join(blocks), encoding="utf-8")

    # --- month 10 -> saved HTML with JSON-LD (examplecars-flavoured) ----------
    html_rows = [r for r in falcons if r["month_idx"] == HTML_MONTH]
    items = []
    for r in html_rows:
        car = {
            "@type": "Car", "name": f"{r['year']} Ford Falcon {r['variant']}",
            "brand": {"@type": "Brand", "name": r["make"].strip()},
            "model": r["model"].title(), "vehicleModelDate": str(r["year"]),
            "vehicleConfiguration": r["variant"], "sku": r["listing_id"],
            "url": f"https://www.examplecars.example/cars/details/{r['listing_id']}",
        }
        if r["odo_km"] is not None:
            car["mileageFromOdometer"] = {"@type": "QuantitativeValue",
                                          "value": int(r["odo_km"]), "unitCode": "KMT"}
        if r["true_price"]:
            car["offers"] = {"@type": "Offer", "price": f"{r['true_price']:.0f}",
                             "priceCurrency": "AUD"}
        items.append({"@type": "ListItem", "item": car})
    ld = {"@context": "https://schema.org", "@type": "ItemList",
          "itemListElement": items}
    # examplecars-style per-listing tracking clusters: the only machine-readable
    # per-ad seller/state on real result pages — exercises the supplement join
    state_canon = {"NSW": "NSW", "nsw": "NSW", "New South Wales": "NSW",
                   "Penrith, NSW": "NSW", "VIC": "VIC", "QLD": "QLD"}
    clusters = []
    for r in html_rows:
        bits = [f'"tracking/item/networkId":"{r["listing_id"]}"']
        if r["state"]:
            bits.append(f'"tracking/item/state":"{state_canon[r["state"]]}"')
        if r["seller"]:
            adtype = "Dealer used" if "ealer" in r["seller"] else "Private"
            bits.append(f'"tracking/item/adtype":"{adtype}"')
        clusters.append("{" + ",".join(bits) + "}")
    html = ("<!DOCTYPE html><html><head><title>Used Ford Falcon - examplecars.example"
            "</title><script type=\"application/ld+json\">" + json.dumps(ld)
            + "</script></head><body><h1>Results</h1>"
            + "<script>var junk = {price: 999}; var tracking = ["
            + ",".join(clusters) + "];</script></body></html>")
    (OUT / "falcons_4.html").write_text(html, encoding="utf-8")

    # --- the June re-list of the May text car, $1,000 cheaper --------------
    relist_new_price = relist["true_price"] - 1000
    relist_text = "\n".join([
        f"{relist['year']} {relist['make'].strip()} {relist['model'].title()} {relist['variant']}",
        f"${relist_new_price:,.0f}",
        f"{relist['odo_km']:,.0f} km",
        relist["state"] or "Sydney",
        relist["seller"] or "Private seller"])
    (OUT / "falcons_5_relist.txt").write_text(relist_text, encoding="utf-8")

    # --- ground truth -------------------------------------------------------
    def month_prices(m):
        return [r["true_price"] for r in falcons if r["month"] == m and r["true_price"]]

    median_per_month = {m: statistics.median(month_prices(m)) for m in MONTHS}
    # stored June median additionally contains the re-listed car at its new price
    june = MONTHS[HTML_MONTH]
    median_stored = dict(median_per_month)
    median_stored[june] = statistics.median(month_prices(june) + [relist_new_price])
    priced_stored = {m: len(month_prices(m)) for m in MONTHS}
    priced_stored[june] += 1

    truth = {
        "n_falcon_rows": len(falcons),
        "csv_rows": len(csv_rows), "json_falcon_rows": len(json_rows),
        "text_rows": len(text_rows), "html_ld_rows": len(html_rows),
        "n_bikes": len(bikes),
        "expected_vehicles": len(falcons) + len(bikes),          # relist merges
        "expected_observations": len(falcons) + len(bikes) + 1,  # +1 relist price
        "priced_per_month_stored": priced_stored,
        "median_per_month_stored": {m: round(v) for m, v in median_stored.items()},
        "true_level_by_idx": {str(m): true_level(m) for m in range(len(MONTHS) + 3)},
        "slope_per_month": SLOPE_PER_MONTH, "beta_per_km": BETA,
        "relist": {"variant": relist["variant"], "state": relist["state"],
                   "odo": relist["odo_km"], "price_old": relist["true_price"],
                   "price_new": relist_new_price},
        "capture_date_flags": {"falcons_3.txt": "2026-05-15",
                               "falcons_4.html": "2026-06-15",
                               "falcons_5_relist.txt": "2026-06-25"},
    }
    (OUT / "truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return truth


if __name__ == "__main__":
    t = main()
    print(f"synthetic data written to {OUT}")
    print(f"falcons: {t['n_falcon_rows']} over {len(MONTHS)} months; bikes: {t['n_bikes']}; "
          f"expected vehicles {t['expected_vehicles']}, observations {t['expected_observations']}")
