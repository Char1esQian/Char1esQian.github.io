"""Export the latest snapshot from the SQLite DB to docs/data.json (GitHub Pages).

Run after each snapshot (publish_to_pages.bat does this automatically):
    python export_site_data.py
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "polestar4_inventory.sqlite3")
OUT = os.path.join(ROOT, "docs", "data.json")


def main() -> int:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    latest = con.execute("SELECT MAX(snapshot_date) FROM listings").fetchone()[0]
    if not latest:
        print("No snapshots in DB - run the tracker first.")
        return 1

    nat = dict(con.execute(
        "SELECT * FROM v_national_daily WHERE date = ?", (latest,)).fetchone())

    stores = [dict(r) for r in con.execute(
        "SELECT ps.date, ps.store, ps.state, ps.partner_id, ps.cars_visible,"
        "       ps.cars_exclusive, ps.in_stock, ps.in_transit,"
        "       ps.avg_price_now, ps.min_price_now, ps.avg_discount_pct"
        "  FROM v_per_store_daily ps WHERE ps.date = ?"
        " ORDER BY ps.cars_visible DESC", (latest,))]

    # dedupe per car: prefer an in-stock listing, then lowest price_now
    best = {}
    for r in con.execute("SELECT * FROM v_current_inventory"):
        cid = r["car_id"]
        key = (0 if r["availability"] == "in_stock" else 1,
               r["price_now"] if r["price_now"] is not None else 9e9)
        if cid not in best or key < best[cid][0]:
            best[cid] = (key, dict(r))

    dol = {r["car_id"]: dict(r) for r in con.execute("SELECT * FROM v_days_on_lot")}

    store_names = {}
    store_states = {}
    for r in con.execute(
            "SELECT l.car_id, r.name, r.state_code FROM listings l"
            " JOIN retailers r ON r.partner_id = l.partner_id"
            " WHERE l.snapshot_date = ?", (latest,)):
        store_names.setdefault(r[0], []).append(r[1])
        if r[2]:
            store_states.setdefault(r[0], set()).add(r[2])

    skip = {"raw_json", "snapshot_date", "fetched_at", "partner_id", "car_id"}
    inventory = []
    for cid, (_, row) in best.items():
        d = dol.get(cid, {})
        out = {k: v for k, v in row.items() if k not in skip}
        out["car_id"] = cid
        out["first_seen"] = d.get("first_seen")
        out["days_on_lot"] = d.get("days_on_lot")
        out["store_names"] = sorted(store_names.get(cid, []))
        out["store_states"] = sorted(store_states.get(cid, ()))
        inventory.append(out)
    inventory.sort(key=lambda r: (r.get("price_now") or 0))

    sell_through = [dict(r) for r in con.execute(
        "SELECT date, cumulative_listed, cumulative_sold, sell_through_rate"
        "  FROM v_sell_through_daily ORDER BY date")]

    national_history = [dict(r) for r in con.execute(
        "SELECT date, unique_cars, in_stock, in_transit, partners_reporting,"
        "       avg_discount_pct FROM v_national_daily ORDER BY date")]

    # link the site footer to the actual repo, if this folder is pushed to GitHub
    repo_url = None
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, cwd=ROOT,
                             timeout=10).stdout.strip()
        if url:
            if url.startswith("git@github.com:"):
                url = "https://github.com/" + url[len("git@github.com:"):].removesuffix(".git")
            repo_url = url
    except Exception:
        pass

    price_changes = [dict(r) for r in con.execute(
        "SELECT car_id, snapshot_date, old_value, new_value FROM price_changes"
        " ORDER BY snapshot_date DESC, car_id LIMIT 50")]

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_date": latest,
        "national": nat,
        "stores": stores,
        "inventory": inventory,
        "sell_through": sell_through,
        "national_history": national_history,
        "price_changes": price_changes,
        "repo_url": repo_url,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(inventory)} cars, {len(stores)} stores, "
          f"snapshot {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
