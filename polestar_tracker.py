#!/usr/bin/env python3
"""
polestar_tracker.py - daily Polestar 4 inventory tracker (national, dealer-aware).

Backend (verified live 2026-09): Polestar's US stock-cars SPA is a GraphQL app;
one endpoint serves ALL US retailers:
    POST https://pc-api.polestar.com/eu-north-1/preconfigured-cars/
No auth token, no cookies, no headless browser required.

Known limitations (schema-verified by field probing):
  * The API does NOT expose VINs (StockEntry has no vin/chassis field).
    Car identity = listing `id` = pno34 (full factory spec code incl.
    paint/wheels/upholstery/packages/options) + model year + production
    structure week. pno34 is effectively unique per physical car (verified
    32/32 unique at one store). `vin` is stored as NULL for compatibility.
  * Partner queries return RADIUS-BASED sets: the same car can be visible
    under several nearby Spaces and there is no "home store" field.
    National totals therefore DEDUPE by car_id. Per-store counts are
    "visible at store" / "exclusive to store"; every (car, store, date)
    visibility pair is stored in listings.
  * Availability: earliestDeliveryDateLabel == "Immediate" => in_stock,
    otherwise in_transit (en route / allocated).

Usage:
  python polestar_tracker.py snapshot                  # all ~30 US retailers
  python polestar_tracker.py snapshot --states NJ,NY,PA,CT
  python polestar_tracker.py snapshot --partners 6US6660
  python polestar_tracker.py report                    # counts/arrivals/sold/prices
  python polestar_tracker.py report --state NJ --days 30
  python polestar_tracker.py query "SELECT * FROM v_days_on_lot LIMIT 10"

Cron example (daily 07:10):
  10 7 * * *  cd /path/to/dir && python polestar_tracker.py snapshot --log-file tracker.log >>cron.log 2>&1

Exit codes: 0 = OK (individual partner errors are logged, not fatal),
            1 = fatal (DB unusable / nothing stored).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

API_URL = "https://pc-api.polestar.com/eu-north-1/preconfigured-cars/"
ORIGIN = "https://www.polestar.com"
DEFAULT_DB = "polestar4_inventory.sqlite3"
DEFAULT_DELAY = 2.5          # seconds between API requests (2-3s per spec)
PAGE_SIZE = 50               # verified accepted by the API
HTTP_TIMEOUT = 60
MAX_RETRIES = 3

# model filter codes discovered in the site's CarPathToModel bundle
MODEL_CODES = {
    "p4": "814",      # Polestar 4 (coupe; the body style sold in the US)
    "p4suv": "815",   # Polestar 4 SUV variant (currently 0 US listings)
    "p3": "359",      # Polestar 3 (optional extension)
    "p2": "534",      # Polestar 2 (optional extension)
    "p5": "824",      # Polestar 5 (future)
}

HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/us/stock-cars/polestar-4-coupe/",
}

Q_DEALERS = """
query FetchAllDealers($market: String!, $source: SourceType = Preconfigured) {
  getAllRetailersFunction(marketMappingCode: $market, source: $source) {
    code partnerId name zipcode city distance latitude longitude address state stateCode
  }
}
"""

Q_RESULTS = """
query TrackerResults($market: String!, $includeValidFilters: Boolean!, $filters: [FilterValueGroupInput]!, $pagination: PaginationInput, $partnerId: String, $customerType: String, $source: SourceType = Preconfigured) {
  vehicles: filteredStockCars(market: $market includeValidFilters: $includeValidFilters filters: $filters pagination: $pagination partnerId: $partnerId customerType: $customerType source: $source) {
    pagination { pageSize pageNo totalRecords }
    filterResults {
      id
      pno34
      modelYear
      startStructureWeek
      earliestDeliveryDate
      earliestDeliveryDateLabel
      stockTypeDeliveryDateLabel
      cashPriceData {
        listPrice { totals { car { carTotalBasicPrice { value } carTotalPrice { value } } grandTotal { paymentTotal { paymentTotalBasicPrice { value } paymentTotalPrice { value } } } } }
        discounted { totals { car { carTotalBasicPrice { value } carTotalPrice { value } } grandTotal { paymentTotal { paymentTotalBasicPrice { value } paymentTotalPrice { value } } } } }
      }
      leasePriceData { promotedPrices { valueFormat disclaimer } }
      techData { drive engineBev_TotalHp engineBev_ElectricRangeEpaMiles }
      content { code name filterTypeId featureType shortDescription }
      packages { code name }
      isCampaignEnabled
    }
  }
}
"""

log = logging.getLogger("polestar_tracker")



# --------------------------------------------------------------------- API

def gql(variables: dict, query: str = Q_RESULTS, timeout: int = HTTP_TIMEOUT) -> dict:
    """POST one GraphQL request; retries transient failures; raises on final failure."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(API_URL, data=payload, headers=HTTP_HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "ignore"))
            errors = body.get("errors")
            if errors:
                msgs = "; ".join(str(e.get("message", "?")) for e in errors)[:300]
                last_err = RuntimeError(f"GraphQL errors: {msgs}")
                log.warning("GraphQL error (attempt %d/%d): %s", attempt, MAX_RETRIES, msgs)
            else:
                return body
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log.warning("Request failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
        if attempt < MAX_RETRIES:
            time.sleep(3 * attempt)
    raise last_err  # type: ignore[misc]


def fetch_all_retailers(delay: float, timeout: int = HTTP_TIMEOUT) -> list[dict]:
    data = gql({"market": "en-US", "source": "Preconfigured"}, query=Q_DEALERS, timeout=timeout)
    time.sleep(delay)
    return data["data"]["getAllRetailersFunction"]


def fetch_partner_cars(partner_id: str, model_code: str, customer_type: str,
                       delay: float, timeout: int = HTTP_TIMEOUT) -> list[dict]:
    """Page through one partner's stock-car listings for the given model code."""
    cars: list[dict] = []
    page = 1
    while True:
        body = gql({
            "market": "en-US",
            "includeValidFilters": False,
            "filters": [{"filterTypeId": "4",
                         "filterValues": [{"value": model_code, "featureCode": model_code}]}],
            "partnerId": partner_id,
            "customerType": customer_type,
            "pagination": {"pageNo": page, "pageSize": PAGE_SIZE},
        }, timeout=timeout)
        vehicles = (body.get("data") or {}).get("vehicles") or {}
        entries = vehicles.get("filterResults") or []
        cars.extend(entries)
        total = ((vehicles.get("pagination") or {}).get("totalRecords")) or 0
        if not entries or len(cars) >= total:
            return cars
        page += 1
        time.sleep(delay)



# ----------------------------------------------------------------- parsing

def _price(node: dict, key: str) -> float | None:
    if not node:
        return None
    v = node.get(key) or {}
    val = v.get("value")
    return float(val) if val is not None else None


def parse_entry(entry: dict) -> dict:
    """Flatten one StockEntry GraphQL object into a listings row dict."""
    cash = entry.get("cashPriceData") or {}
    lst = (cash.get("listPrice") or {}).get("totals") or {}
    disc = (cash.get("discounted") or {}).get("totals") or {}
    # promotedPrices is normally a dict {"valueFormat": ..., "disclaimer": ...};
    # tolerate a list-of-dicts shape in case other markets differ
    promo = (entry.get("leasePriceData") or {}).get("promotedPrices")
    if isinstance(promo, dict):
        lease_txt = promo.get("valueFormat")
    elif isinstance(promo, list) and promo:
        lease_txt = promo[0].get("valueFormat") if isinstance(promo[0], dict) else None
    else:
        lease_txt = None

    color = interior = rims = engine = drive = model_name = None
    options: list[str] = []
    packages: list[str] = []
    for it in entry.get("content") or []:
        ft, code, name = it.get("featureType"), it.get("code"), it.get("name")
        if ft == "Color":
            color = name
        elif ft == "Upholstery":
            interior = name
        elif ft == "Rims":
            rims = name
        elif ft == "Engine":
            engine = name
        elif ft == "Drive":
            drive = name
        elif ft == "Model":
            model_name = name
        elif ft == "Option":
            options.append(name or code or "")
        elif ft == "Packages":
            packages.append(name or code or "")
    for p in entry.get("packages") or []:
        if p.get("name"):
            pair = f"{p['name']} ({p.get('code')})"
            if pair not in packages:
                packages.append(pair)

    label = entry.get("earliestDeliveryDateLabel")
    if (label or "").strip().lower() == "immediate":
        availability = "in_stock"
    elif entry.get("earliestDeliveryDate") or label:
        availability = "in_transit"
    else:
        availability = "unknown"

    tech = entry.get("techData") or {}
    return {
        "car_id": entry["id"].strip(),
        "pno34": entry["pno34"].strip(),
        "vin": None,  # not exposed by the API; placeholder for future compatibility
        "model_name": model_name,
        "model_year": entry.get("modelYear"),
        "structure_week": entry.get("startStructureWeek"),
        "engine": engine,
        "drive": drive,
        "hp": tech.get("engineBev_TotalHp"),
        "epa_range_miles": tech.get("engineBev_ElectricRangeEpaMiles"),
        "exterior_color": color,
        "interior": interior,
        "rims": rims,
        "packages": "; ".join(packages) or None,
        "options": "; ".join(options) or None,
        "msrp": _price(lst.get("car"), "carTotalPrice"),
        "list_price": _price((lst.get("grandTotal") or {}).get("paymentTotal"), "paymentTotalPrice"),
        "price_now": _price((disc.get("grandTotal") or {}).get("paymentTotal"), "paymentTotalPrice"),
        "price_now_basic": _price((disc.get("grandTotal") or {}).get("paymentTotal"), "paymentTotalBasicPrice"),
        "is_campaign": 1 if entry.get("isCampaignEnabled") else 0,
        "delivery_date": (entry.get("earliestDeliveryDate") or "")[:10] or None,
        "delivery_label": label,
        "stock_type_label": entry.get("stockTypeDeliveryDateLabel"),
        "availability": availability,
        "lease_promo": lease_txt,
        "raw_json": json.dumps(entry, separators=(",", ":")),
    }



# ---------------------------------------------------------------- storage

DDL = """
CREATE TABLE IF NOT EXISTS retailers (
    partner_id     TEXT PRIMARY KEY,
    code           TEXT,
    name           TEXT,
    city           TEXT,
    state_code     TEXT,
    zipcode        TEXT,
    address        TEXT,
    latitude       REAL,
    longitude      REAL,
    last_refreshed TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date           TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    model_code         TEXT,
    partners_attempted INTEGER,
    partners_succeeded INTEGER,
    partners_failed    INTEGER,
    listings_stored    INTEGER,
    unique_cars        INTEGER,
    errors_json        TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    snapshot_date    TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    car_id           TEXT NOT NULL,
    partner_id       TEXT NOT NULL,
    pno34            TEXT,
    vin              TEXT,
    model_code       TEXT,
    model_name       TEXT,
    model_year       TEXT,
    structure_week   TEXT,
    engine           TEXT,
    drive            TEXT,
    hp               INTEGER,
    epa_range_miles  INTEGER,
    exterior_color   TEXT,
    interior         TEXT,
    rims             TEXT,
    packages         TEXT,
    options          TEXT,
    msrp             REAL,
    list_price       REAL,
    price_now        REAL,
    price_now_basic  REAL,
    is_campaign      INTEGER,
    delivery_date    TEXT,
    delivery_label   TEXT,
    stock_type_label TEXT,
    availability     TEXT,
    lease_promo      TEXT,
    raw_json         TEXT,
    PRIMARY KEY (snapshot_date, car_id, partner_id)
);
CREATE INDEX IF NOT EXISTS ix_listings_car ON listings(car_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_listings_date ON listings(snapshot_date);

CREATE TABLE IF NOT EXISTS cars (
    car_id            TEXT PRIMARY KEY,
    pno34             TEXT,
    first_seen        TEXT,
    last_seen         TEXT,
    is_active         INTEGER,
    sold_date         TEXT,
    last_availability TEXT,
    last_price        REAL,
    min_price         REAL,
    max_price         REAL
);
CREATE INDEX IF NOT EXISTS ix_cars_active ON cars(is_active);

CREATE TABLE IF NOT EXISTS price_changes (
    car_id        TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    field         TEXT NOT NULL,
    old_value     REAL,
    new_value     REAL,
    PRIMARY KEY (car_id, snapshot_date, field)
);

CREATE TABLE IF NOT EXISTS events (
    event_date TEXT NOT NULL,
    car_id     TEXT NOT NULL,
    pno34      TEXT,
    event_type TEXT NOT NULL,
    details    TEXT,
    PRIMARY KEY (event_date, car_id, event_type)
);
"""



VIEWS = """
DROP VIEW IF EXISTS v_current_inventory;
CREATE VIEW v_current_inventory AS
SELECT l.*
FROM listings l
JOIN (SELECT car_id, MAX(snapshot_date) AS d FROM listings GROUP BY car_id) m
  ON m.car_id = l.car_id AND m.d = l.snapshot_date;

DROP VIEW IF EXISTS v_per_store_daily;
CREATE VIEW v_per_store_daily AS
SELECT l.snapshot_date            AS date,
       r.name                     AS store,
       r.state_code               AS state,
       l.partner_id               AS partner_id,
       COUNT(DISTINCT l.car_id)   AS cars_visible,
       SUM(CASE WHEN vis.n_partners = 1 THEN 1 ELSE 0 END) AS cars_exclusive,
       SUM(CASE WHEN l.availability = 'in_stock' THEN 1 ELSE 0 END) AS in_stock,
       SUM(CASE WHEN l.availability = 'in_transit' THEN 1 ELSE 0 END) AS in_transit,
       AVG(l.price_now)           AS avg_price_now,
       MIN(l.price_now)           AS min_price_now,
       MAX(l.price_now)           AS max_price_now,
       AVG(CASE WHEN l.msrp > 0 AND l.price_now > 0
                THEN (l.msrp - l.price_now) / l.msrp END) AS avg_discount_pct
FROM listings l
JOIN retailers r ON r.partner_id = l.partner_id
JOIN (SELECT snapshot_date, car_id, COUNT(DISTINCT partner_id) AS n_partners
      FROM listings GROUP BY snapshot_date, car_id) vis
  ON vis.snapshot_date = l.snapshot_date AND vis.car_id = l.car_id
GROUP BY l.snapshot_date, l.partner_id;

DROP VIEW IF EXISTS v_national_daily;
CREATE VIEW v_national_daily AS
SELECT n.date, n.unique_cars, n.in_stock, n.in_transit, p.partners AS partners_reporting,
       n.avg_discount_pct
FROM (SELECT snapshot_date AS date,
             COUNT(*) AS unique_cars,
             SUM(CASE WHEN best_availability = 'in_stock' THEN 1 ELSE 0 END) AS in_stock,
             SUM(CASE WHEN best_availability = 'in_transit' THEN 1 ELSE 0 END) AS in_transit,
             AVG(CASE WHEN msrp > 0 AND price_now > 0
                      THEN (msrp - price_now) / msrp END) AS avg_discount_pct
      FROM (SELECT snapshot_date, car_id,
                   MIN(availability) AS best_availability,
                   AVG(msrp) AS msrp, AVG(price_now) AS price_now
            FROM listings GROUP BY snapshot_date, car_id)
      GROUP BY snapshot_date) n
JOIN (SELECT snapshot_date AS date, COUNT(DISTINCT partner_id) AS partners
      FROM listings GROUP BY snapshot_date) p ON p.date = n.date;

DROP VIEW IF EXISTS v_days_on_lot;
CREATE VIEW v_days_on_lot AS
SELECT c.car_id, c.pno34, c.first_seen, c.last_seen, c.sold_date,
       CASE WHEN c.is_active = 1
            THEN CAST(julianday('now') - julianday(c.first_seen) AS INTEGER)
            ELSE CAST(julianday(c.sold_date) - julianday(c.first_seen) AS INTEGER)
       END AS days_on_lot,
       c.is_active, c.last_availability, c.last_price, c.min_price, c.max_price
FROM cars c;

DROP VIEW IF EXISTS v_sell_through_daily;
CREATE VIEW v_sell_through_daily AS
SELECT d.date,
       (SELECT COUNT(*) FROM cars c WHERE c.first_seen <= d.date) AS cumulative_listed,
       (SELECT COUNT(*) FROM cars c WHERE c.sold_date IS NOT NULL
                                  AND c.sold_date <= d.date) AS cumulative_sold,
       ROUND(1.0 * (SELECT COUNT(*) FROM cars c WHERE c.sold_date IS NOT NULL
                                                 AND c.sold_date <= d.date)
             / NULLIF((SELECT COUNT(*) FROM cars c WHERE c.first_seen <= d.date), 0), 4)
       AS sell_through_rate
FROM (SELECT DISTINCT snapshot_date AS date FROM listings) d
ORDER BY d.date;
"""


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(DDL)
    con.executescript(VIEWS)
    return con



# ------------------------------------------------------- derived state

def recompute_derived(con: sqlite3.Connection) -> None:
    """Rebuild cars / price_changes / events from the full listings history.

    Deterministic full recompute from listings: idempotent, so re-running the
    same day or after gaps always yields consistent state.
    """
    cur = con.cursor()
    dates = [r[0] for r in cur.execute(
        "SELECT DISTINCT snapshot_date FROM listings ORDER BY snapshot_date")]
    if not dates:
        return
    latest = dates[-1]

    seen: dict = {}
    pno34_of: dict = {}
    prices: dict = {}
    avail: dict = {}
    for car_id, pno34, d, price, a in cur.execute(
            "SELECT car_id, MIN(pno34), snapshot_date, MIN(price_now), MIN(availability) "
            "FROM listings GROUP BY car_id, snapshot_date"):
        seen.setdefault(car_id, set()).add(d)
        pno34_of[car_id] = pno34
        prices.setdefault(car_id, {})[d] = price
        if d == latest:
            avail[car_id] = a

    cur.execute("DELETE FROM cars")
    cur.execute("DELETE FROM price_changes")
    cur.execute("DELETE FROM events")

    for car_id, ds in seen.items():
        first_seen, last_seen = min(ds), max(ds)
        later = [d for d in dates if d > last_seen]
        sold_date = None if last_seen == latest else (later[0] if later else None)
        p = prices.get(car_id, {})
        valid = [x for x in p.values() if x is not None]
        cur.execute("INSERT OR REPLACE INTO cars VALUES (?,?,?,?,?,?,?,?,?,?)", (
            car_id, pno34_of.get(car_id), first_seen, last_seen,
            1 if last_seen == latest else 0, sold_date, avail.get(car_id),
            p.get(latest), min(valid) if valid else None, max(valid) if valid else None))
        cur.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                    (first_seen, car_id, pno34_of.get(car_id), "first_seen", None))
        if sold_date:
            cur.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                        (sold_date, car_id, pno34_of.get(car_id), "disappeared",
                         "absent from the first snapshot after " + last_seen))
        elif len(ds) > 1 and any(d not in ds for d in dates if first_seen < d < last_seen):
            cur.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                        (last_seen, car_id, pno34_of.get(car_id), "reappeared", None))

    # price changes between consecutive snapshots (min price across partners)
    for car_id, pd_ in prices.items():
        for d1, d2 in zip(dates, dates[1:]):
            v1, v2 = pd_.get(d1), pd_.get(d2)
            if v1 is not None and v2 is not None and abs(v1 - v2) > 0.01:
                cur.execute("INSERT OR REPLACE INTO price_changes VALUES (?,?,?,?,?)",
                            (car_id, d2, "price_now", v1, v2))
    con.commit()



# ------------------------------------------------------------ CLI commands

LISTING_COLS = 30  # number of columns in the listings table


def cmd_snapshot(args: argparse.Namespace) -> int:
    con = open_db(args.db)
    run_date = date.today().isoformat()
    started = datetime.now(timezone.utc).isoformat()
    errors: list[str] = []

    try:
        retailers = fetch_all_retailers(args.delay)
    except Exception as e:
        log.error("Could not refresh retailer list: %s - falling back to DB copy", e)
        retailers = [{"partnerId": r[0], "name": r[1], "stateCode": r[2]} for r in
                     con.execute("SELECT partner_id, name, state_code FROM retailers")]
        if not retailers:
            log.error("No retailers available; aborting.")
            return 1
    log.info("US retailers: %d", len(retailers))
    con.executemany(
        "INSERT OR REPLACE INTO retailers VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r.get("partnerId"), r.get("code"), r.get("name"), r.get("city"), r.get("stateCode"),
          r.get("zipcode"), r.get("address"), r.get("latitude"), r.get("longitude"),
          datetime.now(timezone.utc).isoformat()) for r in retailers])
    con.commit()

    states = ({s.strip().upper() for s in args.states.split(",") if s.strip()}
              if args.states else None)
    partners = ({p.strip() for p in args.partners.split(",") if p.strip()}
                if args.partners else None)
    targets = [r for r in retailers
               if (partners is None or r.get("partnerId") in partners)
               and (states is None or (r.get("stateCode") or "").upper() in states)]
    model_code = MODEL_CODES.get(args.model, args.model)
    log.info("Querying %d retailer(s) for model code %s (customer type %s)",
             len(targets), model_code, args.customer_type)

    # idempotent same-day re-runs: wipe today's listings for this model first
    con.execute("DELETE FROM listings WHERE snapshot_date = ? AND model_code = ?",
                (run_date, model_code))

    fetched_at = datetime.now(timezone.utc).isoformat()
    parsed_rows = []  # (partner_id, row_dict)
    for i, r in enumerate(targets, 1):
        pid = r["partnerId"]
        try:
            entries = fetch_partner_cars(pid, model_code, args.customer_type,
                                         args.delay, args.timeout)
            rows = [parse_entry(e) for e in entries]
            parsed_rows.extend((pid, row) for row in rows)
            log.info("[%d/%d] %s (%s): %d car(s)", i, len(targets), r.get("name"), pid, len(rows))
        except Exception as e:
            msg = f"partner {pid} ({r.get('name')}): {e}"
            errors.append(msg)
            log.error("FAILED %s", msg)
        time.sleep(args.delay)

    con.executemany(
        "INSERT OR REPLACE INTO listings VALUES (" + ",".join(["?"] * LISTING_COLS) + ")",
        [(run_date, fetched_at,
          row["car_id"], pid, row["pno34"], row["vin"], model_code,
          row["model_name"], row["model_year"], row["structure_week"],
          row["engine"], row["drive"], row["hp"], row["epa_range_miles"],
          row["exterior_color"], row["interior"], row["rims"],
          row["packages"], row["options"], row["msrp"], row["list_price"],
          row["price_now"], row["price_now_basic"], row["is_campaign"],
          row["delivery_date"], row["delivery_label"], row["stock_type_label"],
          row["availability"], row["lease_promo"], row["raw_json"])
         for (pid, row) in parsed_rows])
    con.commit()

    n_rows, n_cars = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT car_id) FROM listings WHERE snapshot_date=?",
        (run_date,)).fetchone()
    con.execute(
        "INSERT INTO runs (run_date, started_at, finished_at, model_code, partners_attempted,"
        " partners_succeeded, partners_failed, listings_stored, unique_cars, errors_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_date, started, datetime.now(timezone.utc).isoformat(), args.model,
         len(targets), len(targets) - len(errors), len(errors), n_rows, n_cars,
         json.dumps(errors) if errors else None))
    con.commit()

    recompute_derived(con)

    log.info("Snapshot %s: %d listings, %d unique cars, %d partner failure(s)",
             run_date, n_rows, n_cars, len(errors))
    if n_rows == 0:
        log.error("No listings stored - the API contract may have changed.")
        return 1
    return 0



def cmd_report(args: argparse.Namespace) -> int:
    con = open_db(args.db)
    con.row_factory = sqlite3.Row
    latest = con.execute("SELECT MAX(snapshot_date) FROM listings").fetchone()[0]
    if not latest:
        print("No snapshots yet. Run: python polestar_tracker.py snapshot")
        return 1
    state = args.state.upper() if args.state else None
    days = args.days
    out = [f"=== Polestar 4 inventory report (latest snapshot: {latest}) ===", ""]

    out.append("Per-store counts (cars visible / exclusive, in-stock vs in-transit):")
    q = """SELECT store, state, cars_visible, cars_exclusive, in_stock, in_transit,
                  avg_price_now, avg_discount_pct
           FROM v_per_store_daily WHERE date = ? {extra} ORDER BY cars_visible DESC"""
    for r in con.execute(q.format(extra="AND state = ?" if state else ""),
                         (latest, state) if state else (latest,)):
        out.append(f"  {(r['store'] or '?')[:28]:<28} {r['state'] or '??':<3} "
                   f"visible={r['cars_visible']:<4} excl={r['cars_exclusive'] or 0:<3} "
                   f"stock={r['in_stock'] or 0:<4} transit={r['in_transit'] or 0:<4} "
                   f"avg=${(r['avg_price_now'] or 0):<9,.0f} "
                   f"disc={(r['avg_discount_pct'] or 0) * 100:.1f}%")

    n = con.execute("SELECT * FROM v_national_daily WHERE date = ?", (latest,)).fetchone()
    out.append("")
    out.append(f"National total: {n['unique_cars']} unique cars "
               f"({n['in_stock']} in-stock, {n['in_transit']} in-transit) across "
               f"{n['partners_reporting']} partners; "
               f"avg discount {(n['avg_discount_pct'] or 0) * 100:.1f}% off MSRP")

    state_join = ("AND car_id IN (SELECT l.car_id FROM listings l "
                  "JOIN retailers r ON r.partner_id = l.partner_id "
                  "WHERE r.state_code = ?)") if state else ""
    state_params = (state,) if state else ()

    out.append(f"\nNew arrivals (last {days} day(s)):")
    rows = con.execute(
        f"SELECT first_seen, COUNT(*) FROM cars WHERE first_seen >= date('now', ?) "
        f"{state_join} GROUP BY first_seen ORDER BY first_seen DESC",
        (f"-{days} day",) + state_params).fetchall()
    out.append("  " + ("; ".join(f"{r[0]}: +{r[1]}" for r in rows) or "none"))

    out.append(f"\nDisappeared / sold (last {days} day(s)):")
    rows = con.execute(
        f"SELECT sold_date, COUNT(*) FROM cars WHERE sold_date >= date('now', ?) "
        f"AND is_active = 0 {state_join} GROUP BY sold_date ORDER BY sold_date DESC",
        (f"-{days} day",) + state_params).fetchall()
    out.append("  " + ("; ".join(f"{r[0]}: -{r[1]}" for r in rows) or "none"))

    out.append(f"\nPrice changes (last {days} day(s), latest 20):")
    for r in con.execute(
            "SELECT snapshot_date, car_id, old_value, new_value FROM price_changes "
            "WHERE snapshot_date >= date('now', ?) ORDER BY snapshot_date DESC LIMIT 20",
            (f"-{days} day",)):
        out.append(f"  {r['snapshot_date']}  {r['car_id'][:44]}...  "
                   f"${r['old_value']:,.0f} -> ${r['new_value']:,.0f}")

    out.append("\nSlowest movers currently on the lot:")
    for r in con.execute(
            "SELECT car_id, first_seen, days_on_lot, last_price, last_availability "
            "FROM v_days_on_lot WHERE is_active = 1 ORDER BY days_on_lot DESC LIMIT 10"):
        out.append(f"  {r['days_on_lot']:>3}d  {r['first_seen']}  "
                   f"${(r['last_price'] or 0):,.0f}  {r['last_availability']}  "
                   f"{r['car_id'][:40]}...")

    out.append("\nSell-through over time (latest 10 snapshots):")
    for r in con.execute(
            "SELECT date, cumulative_listed, cumulative_sold, sell_through_rate "
            "FROM v_sell_through_daily ORDER BY date DESC LIMIT 10"):
        out.append(f"  {r['date']}: listed={r['cumulative_listed']:<5} "
                   f"sold={r['cumulative_sold']:<4} rate={r['sell_through_rate']}")

    print("\n".join(out))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    con = open_db(args.db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(args.sql).fetchall()
    except sqlite3.Error as e:
        print(f"SQL error: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("(no rows)")
        return 0
    headers = list(rows[0].keys())
    print("\t".join(headers))
    for r in rows:
        print("\t".join("" if r[h] is None else str(r[h]) for h in headers))
    return 0



# ------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="polestar_tracker.py",
        description="Daily Polestar 4 inventory tracker "
                    "(Polestar stock-cars GraphQL API -> SQLite).")
    sub = p.add_subparsers(dest="command")

    ps = sub.add_parser("snapshot", help="fetch current inventory and store a daily snapshot")
    ps.add_argument("--db", default=DEFAULT_DB, help="SQLite path (default %(default)s)")
    ps.add_argument("--model", default="p4", choices=sorted(MODEL_CODES),
                    help="which model to track (default: p4)")
    ps.add_argument("--states", help="comma list of state codes, e.g. NJ,NY,PA,CT")
    ps.add_argument("--partners", help="comma list of partner IDs (default: all US retailers)")
    ps.add_argument("--customer-type", default="B2C", choices=["B2C", "B2B"])
    ps.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="seconds between API requests (default %(default)s)")
    ps.add_argument("--timeout", type=int, default=HTTP_TIMEOUT)
    ps.add_argument("--log-file", help="also append log lines to this file")
    ps.add_argument("-v", "--verbose", action="store_true")
    ps.set_defaults(func=cmd_snapshot)

    pr = sub.add_parser("report", help="print an inventory/market summary from the DB")
    pr.add_argument("--db", default=DEFAULT_DB)
    pr.add_argument("--state", help="limit per-store section to one state, e.g. NJ")
    pr.add_argument("--days", type=int, default=7, help="lookback window (default %(default)s)")
    pr.set_defaults(func=cmd_report)

    pq = sub.add_parser("query", help="run raw SQL against the DB")
    pq.add_argument("--db", default=DEFAULT_DB)
    pq.add_argument("sql")
    pq.set_defaults(func=cmd_query)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0

    handlers = [logging.StreamHandler(sys.stderr)]
    if getattr(args, "log_file", None):
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        return args.func(args)
    except Exception:
        log.exception("fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())

