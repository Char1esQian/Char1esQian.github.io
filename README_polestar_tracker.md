# Polestar 4 Inventory Tracker

Daily cron-friendly CLI that snapshots Polestar 4 US inventory into SQLite
via Polestar's own stock-cars GraphQL API, and reports days-on-lot, arrivals,
sold, price changes, per-store counts and national sell-through.

## Quick start

```powershell
python polestar_tracker.py snapshot              # all 30 US retailers (~90 s)
python polestar_tracker.py report                # today's market summary
python polestar_tracker.py report --state NJ     # only NJ stores in per-store section
python polestar_tracker.py query "SELECT * FROM v_days_on_lot LIMIT 10"
```

## Cron

```cron
# daily 07:10
10 7 * * * cd /path/to/dir && python polestar_tracker.py snapshot --log-file tracker.log >>cron.log 2>&1
```

Windows Task Scheduler equivalent (Start in = working dir):

```
Program:  python.exe
Arguments: polestar_tracker.py snapshot --log-file tracker.log
```

Re-running a snapshot the same day is safe: today's listings for the tracked
model are replaced, and all derived tables are rebuilt deterministically.

## Commands

| command | purpose |
|---|---|
| `snapshot` | fetch inventory for all/selected retailers, store daily snapshot |
| `report` | console summary: per-store counts, national total, arrivals, sold, price changes, slowest movers, sell-through |
| `query` | run raw SQL against the DB (prints a table) |

Key `snapshot` flags: `--db`, `--model {p4,p4suv,p3,p2,p5}`, `--states NJ,NY,PA,CT`,
`--partners 6US6660,...`, `--customer-type {B2C,B2B}`, `--delay`, `--timeout`,
`--log-file`, `-v`.

## Database layout

- `listings` — one row per (snapshot_date, car, partner): full spec, prices,
  availability labels and the raw GraphQL JSON (`raw_json`).
- `cars` — one row per car: first_seen/last_seen/active/sold_date, last/min/max price.
- `price_changes` — old→new price between consecutive snapshots.
- `events` — first_seen / disappeared / reappeared.
- `runs` — one row per snapshot run incl. partner failures.
- `retailers` — refreshed from the API each run.

Views: `v_current_inventory`, `v_per_store_daily`, `v_national_daily`,
`v_days_on_lot`, `v_sell_through_daily`.

## Example queries

```sql
-- Longest-running cars still on the lot
SELECT car_id, first_seen, days_on_lot, last_price, last_availability
FROM v_days_on_lot WHERE is_active = 1 ORDER BY days_on_lot DESC LIMIT 20;

-- Biggest discounts right now
SELECT exterior_color, rims, packages, msrp, price_now,
       ROUND(100*(msrp-price_now)/msrp, 1) AS pct_off
FROM v_current_inventory
WHERE msrp > 0 AND price_now > 0
ORDER BY pct_off DESC LIMIT 20;

-- Weekly sales velocity: arrivals vs disappearances per ISO week
SELECT strftime('%Y-%W', event_date) AS wk,
       SUM(event_type = 'first_seen')  AS arrivals,
       SUM(event_type = 'disappeared') AS sold
FROM events GROUP BY wk ORDER BY wk;

-- Price-cut history for one car
SELECT snapshot_date, old_value, new_value
FROM price_changes WHERE car_id = '814PAPP0E13137300P03200      00000220002221003XPLUSS-2026-202517-en-US';

-- Cars visible at 3+ partners (dense metro areas)
SELECT snapshot_date, car_id, COUNT(DISTINCT partner_id) n
FROM listings GROUP BY snapshot_date, car_id HAVING n >= 3 ORDER BY n DESC LIMIT 10;
```

## GitHub Pages site (`docs/`)

The repo doubles as a website: `docs/index.html` renders `docs/data.json`
(generated from the latest snapshot by `export_site_data.py`) into a live
dashboard — KPIs, per-store panel, history and sell-through chart, and an
inventory table with:

- **location filters** — a state dropdown and a dealership dropdown (built from
  store data; the dealership list narrows to the selected state, e.g. NJ has
  Manasquan / Princeton / Short Hills, each with its visible-car count).
  Both filter the inventory table and the stores panel.
- **trim filter** (Dual motor AWD 544 hp / Rear motor RWD 272 hp — Polestar's
  API exposes no trim name, so trim = motor/drive/hp),
- **status filter** (in stock / in transit),
- **sorting** by any column via the clickable headers, plus a sort dropdown
  that includes price, days-on-lot (longest first), first-seen date, MSRP and
  biggest discount,
- **days-on-lot badges** (amber ≥14 days, red ≥30 days) with a first-seen
  tooltip; a "N of M cars" counter reflects the active filters.
- **stock trend chart** — combo column + line graph of inventory over time:
  in-stock (green) and in-transit (amber) columns with a total-visible line,
  one group per snapshot date. Pick "All dealers (nationwide)" or any single
  dealership from the dropdown above the chart (dealerships are listed with
  their state). Column values are labeled on the chart when there are 16 or
  fewer snapshots; every element has a hover tooltip with exact figures.
- **NJ dealers card** — daily on-lot count for every New Jersey space (one
  column per dealer) plus a deduplicated NJ total row, with a per-dealer line
  chart over time. Updates automatically with each daily snapshot.
- **Column order** puts the market-relevant info first: Price, Dealers,
  On lot, First seen, then Off/MSRP, status and spec details.


Automated daily tracking (no local machine needed):

```text
GitHub Actions: .github/workflows/polestar-tracker.yml runs daily at 07:30 ET
  snapshot -> export data.json -> commit DB + docs\ -> Pages republishes
Local scripts below remain as a manual fallback.
```

Manual workflow (fallback / immediate refresh):

```text
run_tracker.bat        double-click: snapshot + report
publish_to_pages.bat   double-click: export data, refresh docs\, git commit+push
```

One-time setup (publish_to_pages.bat prints this if the folder is not a repo yet):

```powershell
# 1. create an EMPTY repo named <yourname>.github.io on github.com
git init
git remote add origin https://github.com/<yourname>/<yourname>.github.io.git
git branch -M main
# 2. run publish_to_pages.bat
# 3. enable Pages once: repo Settings - Pages - Source "Deploy from a branch",
#    Branch: main, Folder: /docs - Save
```

The site then lives at `https://<yourname>.github.io/`.

## Automated daily tracking (GitHub Actions)

`.github/workflows/polestar-tracker.yml` makes the tracker self-running in the
cloud — no PC needs to stay on:

- **Schedule:** daily at 07:30 US Eastern (`cron: "30 11 * * *"` UTC), plus a
  manual *Run workflow* button on the repo's Actions tab.
- **What it does:** checks out the repo → runs `snapshot` for all ~30 US
  retailers (std-lib Python only, ~90 s) → writes the console `report` into the
  run summary → regenerates `docs/data.json` → commits the updated SQLite DB +
  `data.json` back to `main` → the branch-based Pages deployment republishes
  the dashboard automatically.
- **DB in git:** `polestar4_inventory.sqlite3` is *tracked* in the repo so each
  run builds on the last one (`.gitattributes` marks it binary to keep diffs
  sane). Sidecar files (`-wal/-shm/-journal`), `tracker.log` and `_probe/`
  stay ignored. The history is a free daily backup; note it does expose the
  accumulated inventory data in repo history — fine for a public project.
- **Idempotent & safe:** snapshots use `INSERT OR REPLACE` keyed by
  (date, car, partner), so re-running the same day is harmless; per-partner
  API failures are logged and never abort the run; `concurrency` prevents
  overlapping runs.
- **Monitoring:** check the Actions tab — green run = fresh data; the run
  summary contains the full daily report.

## Semantics & known limitations (all verified live)

- **No VIN.** Polestar's stock API does not expose VIN/chassis numbers. Car
  identity = API `id` = `pno34` full spec code + model year + production
  structure week (e.g. `...-2026-202517-en-US`); unique per physical car in
  practice (32/32 distinct at one store; no duplicate ids within any partner).
- **Radius-based visibility.** Partner queries return overlapping sets; the
  same car is visible under several nearby Spaces and there is no home-store
  field. National totals dedupe by car_id; per-store counts are "visible" /
  "exclusive"; every (car, partner, date) pair is stored. One car was seen at
  up to 9 partners.
- **In-stock vs in-transit.** `earliestDeliveryDateLabel == "Immediate"` →
  `in_stock`; any dated label ("Early in September", "Late in September", …)
  → `in_transit`. `stockTypeDeliveryDateLabel` is always null for the US and
  is stored raw for future-proofing.
- **MSRP.** `msrp` = configured car cash total (Polestar's "Total MSRP");
  `list_price`/`price_now` are the payment grand totals (list vs discounted,
  including options/fees). Effective US inventory discount ≈ 33–37% off MSRP
  as of 2026-09.
- **One shared GraphQL endpoint** (`POST https://pc-api.polestar.com/eu-north-1/preconfigured-cars/`)
  serves all retailers; unauthenticated with browser-like headers. Retries
  with backoff; per-partner failures are logged in `runs.errors_json` and
  never abort the run.
- **Introductory data caveat.** days-on-lot and sold counts only become
  meaningful once ≥2 snapshots (ideally a few weeks) have accumulated; the
  first run makes every car "new".
- `HandoverLocations` (from `GetCarDetail` bundle) returns generic partner
  lists, not the car's physical location; per-partner polling is the only
  reliable attribution.
