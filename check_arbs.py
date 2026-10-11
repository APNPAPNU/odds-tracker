#!/usr/bin/env python3
"""Decide which arbs in site/arbs_latest.json are new (not yet notified).

  python3 check_arbs.py                 # print new arbs as JSON (does NOT mark them)
  python3 check_arbs.py --mark ID [ID]  # record ids as notified (run after the notification went out)
  python3 check_arbs.py --prune-only    # just prune notified_arbs.json

notified_arbs.json: {"<arb id>": {"first_notified_utc", "last_seen_utc", "game", "market",
                                  "start_utc", "profit_pct", "price_id"}}
Entries are pruned once the game has started or they haven't been seen for --keep-hours (default 48).
Filters for "new": profit_pct >= --min-profit (default 0.5) and, unless --include-stale, no stale leg.
"""
import argparse, json, os, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ARBS = os.path.join(HERE, "site", "arbs_latest.json")
NOTIFIED = os.path.join(HERE, "notified_arbs.json")

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mark", nargs="+", metavar="ID")
    ap.add_argument("--prune-only", action="store_true")
    ap.add_argument("--min-profit", type=float, default=0.5)
    ap.add_argument("--include-stale", action="store_true")
    ap.add_argument("--keep-hours", type=float, default=48)
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    arbs = load(ARBS, {"arbs": []})
    notified = load(NOTIFIED, {})
    current = {x["id"]: x for x in arbs.get("arbs", [])}

    # refresh last_seen, then prune
    for i in notified:
        if i in current:
            notified[i]["last_seen_utc"] = now.isoformat()
    def expired(e):
        start = datetime.fromisoformat(e["start_utc"]) if e.get("start_utc") else None
        seen = datetime.fromisoformat(e.get("last_seen_utc") or e["first_notified_utc"])
        return (start and start <= now) or (now - seen > timedelta(hours=a.keep_hours))
    pruned = [i for i, e in notified.items() if expired(e)]
    for i in pruned:
        del notified[i]

    if a.mark:
        for i in a.mark:
            x = current.get(i)
            if not x:
                print(f"warning: {i} not in current arbs; skipped", file=sys.stderr); continue
            notified[i] = {"first_notified_utc": now.isoformat(), "last_seen_utc": now.isoformat(),
                           "game": x["game"], "market": x["market"], "start_utc": x["start_utc"],
                           "profit_pct": x["profit_pct"], "price_id": x["price_id"]}
        save(NOTIFIED, notified)
        print(json.dumps({"marked": a.mark, "pruned": pruned}))
        return
    save(NOTIFIED, notified)
    if a.prune_only:
        print(json.dumps({"pruned": pruned})); return
    new = [x for x in current.values() if x["id"] not in notified
           and x["profit_pct"] >= a.min_profit and (a.include_stale or not x["stale_warning"])]
    skipped = [{"id": x["id"], "game": x["game"], "profit_pct": x["profit_pct"],
                "reason": "already notified" if x["id"] in notified else
                          "stale leg" if x["stale_warning"] else "below min profit"}
               for x in current.values() if x not in new]
    print(json.dumps({"generated_et": arbs.get("generated_et"), "new_count": len(new), "new": new,
                      "skipped": skipped, "pruned": pruned}, indent=1))

if __name__ == "__main__":
    main()
