#!/usr/bin/env python3
"""
odds_compare.py - compare public sportsbook odds across books.

For every upcoming game / market (moneyline, spread, total) it computes:
  * cross-book average price (mean implied probability -> American odds)
  * no-vig fair price (each book's sides normalised to 100%, then averaged)
  * per-book vig (hold) = sum(implied probs of all outcomes) - 1, the market-average vig,
    and the "best available" hold (best price per outcome across books)
  * ARBITRAGE: best-available hold < 0 with legs at >= 2 different books
  * POSITIVE EV: best price that beats the no-vig fair price

Outputs (each run):
  output/odds_<source>_<ts>.csv      all lines, best vs average (original CSV)
  site/arbitrage.html, site/positive_ev.html   self-contained pages with shared nav
  site/arbitrage.csv, site/positive_ev.csv, site/vig.csv
  site/arbs_latest.json              machine-readable arbs (stable ids for dedupe)  [+ --arb-json PATH]

Sources (--source): actionnetwork (default, keyless, unofficial public JSON feed),
oddsapi (The Odds API v4, env ODDS_API_KEY), fixture (offline SAMPLE data).

Exit codes: 0 ok, 2 data feed failure (previous site/ files are left untouched).
"""
import argparse, csv, hashlib, html, json, os, sys, tempfile, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    requests = None
    import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (odds-compare prototype)"}
HTTP_TIMEOUT = 15

SPORTS = {  # short -> (Action Network league, The Odds API key, DraftKings path, FanDuel path)
    "nfl":   ("nfl",   "americanfootball_nfl",   "football/nfl",     "nfl"),
    "ncaaf": ("ncaaf", "americanfootball_ncaaf", "football/ncaaf",   "ncaaf"),
    "nba":   ("nba",   "basketball_nba",         "basketball/nba",   "nba"),
    "mlb":   ("mlb",   "baseball_mlb",           "baseball/mlb",     "mlb"),
    "nhl":   ("nhl",   "icehockey_nhl",          "hockey/nhl",       "nhl"),
}
DEFAULT_SPORTS = ["nfl", "ncaaf", "nba", "mlb", "nhl"]

# ---- FAIR-PRICE WEIGHTS (edit here or override with --weights "Pinnacle=5,BetMGM=0.5")
# Each book's line is devigged on its own (multiplicative); fair prob = weighted average of those no-vig
# probs across all books quoting that exact market, renormalised to sum to 1. Unlisted books get DEFAULT_WEIGHT.
WEIGHTS = {
    "Pinnacle": 4,                                                     # sharp, reference only
    "Circa": 2, "BetOnline": 2, "Bookmaker": 2, "Betcris": 2, "LowVig": 2,
    "Novig": 2, "ProphetX": 2, "Sporttrade": 2,                        # other sharp / exchanges, reference only
    "DraftKings": 1, "FanDuel": 1, "BetMGM": 1, "Caesars": 1, "BetRivers": 1, "bet365": 1,   # retail (bettable)
}
DEFAULT_WEIGHT = 1.0
SHARP_BOOKS = {"Pinnacle", "Circa", "BetOnline", "Bookmaker", "Betcris", "LowVig", "Novig", "ProphetX", "Sporttrade"}

# Action Network book ids (NJ feeds). 15 = AN consensus, 30 = opening line -> excluded.
AN_BOOKS = {68: "DraftKings", 69: "FanDuel", 75: "BetMGM", 123: "Caesars", 79: "bet365", 71: "BetRivers"}
AN_EXCLUDE = {15, 30}

def book_link(book, sport):
    """Generic sportsbook / league page. The Action Network feed has no event deep links."""
    b = book.lower()
    s = SPORTS.get(sport)
    if "draftkings" in b:
        return f"https://sportsbook.draftkings.com/leagues/{s[2]}" if s else "https://sportsbook.draftkings.com"
    if "fanduel" in b:
        return f"https://sportsbook.fanduel.com/navigation/{s[3]}" if s else "https://sportsbook.fanduel.com"
    if "betmgm" in b:
        return "https://sports.betmgm.com"
    if "betrivers" in b:
        return "https://www.betrivers.com"
    if "caesars" in b or "william hill" in b:
        return "https://www.caesars.com/sportsbook-and-casino"
    if "bet365" in b:
        return "https://www.bet365.com"
    return ""


# ------------------------------------------------------------------ odds math
def implied(a):
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)

def to_american(p):
    if not p or p <= 0 or p >= 1:
        return None
    return round(100.0 * (1 - p) / p) if p < 0.5 else round(-100.0 * p / (1 - p))

def to_decimal(a):
    a = float(a)
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / -a

def fmt_odds(a):
    return "n/a" if a is None else (f"+{int(a)}" if a > 0 else str(int(a)))

def et(dt):
    return dt.astimezone(ET).strftime("%a %b %d %I:%M %p ET").replace(" 0", " ")


# ------------------------------------------------------------------ fetching
def http_get(url, params=None):
    if requests:
        r = requests.get(url, params=params, headers=UA, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
        return r.json(), r.headers
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    with urllib.request.urlopen(urllib.request.Request(url + q, headers=UA), timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp), resp.headers

# normalised event: {"id","sport","start"(datetime),"home","away",
#   "books":{book:{market:[{"name","point","price","link","updated"}]}}}

def from_oddsapi_events(raw, sport):
    out = []
    for ev in raw:
        books, sharp = {}, {}
        for bk in ev.get("bookmakers", []):
            if bk.get("key") in ODDSAPI_SHARP:     # reference-only: never best book / arb leg
                ref = {}
                for m in bk.get("markets", []):
                    if m["key"] not in ("h2h", "spreads", "totals"):
                        continue
                    for o in m.get("outcomes", []):
                        if o.get("price") is None:
                            continue
                        lk = line_key(m["key"], o, ev["home_team"])
                        ref.setdefault(m["key"], {}).setdefault(lk, {"alt": False})[o["name"]] = o["price"]
                if ref:
                    sharp[ODDSAPI_SHARP[bk["key"]]] = ref
                continue
            mk = {}
            for m in bk.get("markets", []):
                if m["key"] in ("h2h", "spreads", "totals"):
                    mk[m["key"]] = [{"name": o["name"], "point": o.get("point"), "price": o["price"],
                                     "link": o.get("link") or m.get("link") or bk.get("link") or "",
                                     "updated": m.get("last_update") or bk.get("last_update")}
                                    for o in m.get("outcomes", []) if o.get("price") is not None]
            if mk:
                books[bk.get("title", bk.get("key"))] = mk
        out.append({"id": ev["id"], "sport": sport,
                    "start": datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")),
                    "home": ev["home_team"], "away": ev["away_team"], "books": books, "sharp": sharp})
    return out

def fetch_oddsapi(sport, args):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("ODDS_API_KEY not set (free key: https://the-odds-api.com/#get-access)")
    params = {"apiKey": key, "markets": "h2h,spreads,totals", "oddsFormat": "american", "includeLinks": "true"}
    if args.books:
        params["bookmakers"] = args.books
    else:
        params["regions"] = args.regions
    data, hdr = http_get(f"https://api.the-odds-api.com/v4/sports/{SPORTS[sport][1]}/odds", params)
    print(f"  [{sport}] The Odds API credits remaining={hdr.get('x-requests-remaining')}", file=sys.stderr)
    return from_oddsapi_events(data, sport)

def fetch_fixture(sport, args):
    path = args.fixture or os.path.join(HERE, "sample_data", "sample_oddsapi_response.json")
    with open(path) as f:
        raw = json.load(f)
    return from_oddsapi_events([e for e in raw if e.get("sport_key") == SPORTS[sport][1]], sport)

AN_FINISHED = {"complete", "closed", "final", "cancelled", "canceled", "postponed", "abandoned", "suspended"}
DAILY_SPORTS = {"nba", "mlb", "nhl"}     # AN's default slate for these is one (ET) day; NFL/NCAAF default = the week

def an_dates(sport, args):
    """Which Action Network slates to request. Dates are ET calendar days (the feed's 'date' is an ET date)."""
    if args.date:
        return [args.date.replace("-", "")]
    if sport not in DAILY_SPORTS:
        return [None]                       # default = current week, already covers today + upcoming days
    today = datetime.now(timezone.utc).astimezone(ET).date()
    return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(max(1, args.days))]

def fetch_actionnetwork(sport, args):
    base = {"period": "game", "bookIds": ",".join(str(i) for i in AN_BOOKS)}
    # The feed ignores bookIds and returns a varying subset of books per request, so make a few
    # requests in parallel and merge each book's line per game (newest 'inserted' wins).
    url = f"https://api.actionnetwork.com/web/v1/scoreboard/{SPORTS[sport][0]}"
    reqs = [dict(base, **({"date": d} if d else {})) for d in an_dates(sport, args) for _ in range(args.an_passes)]
    with ThreadPoolExecutor(max_workers=min(8, len(reqs))) as pool:
        futs = [pool.submit(http_get, url, params) for params in reqs]
        results, errs = [], []
        for f in futs:
            try:
                d = f.result()[0]
                if "games" not in d:
                    raise RuntimeError(f"unexpected response shape (keys: {list(d)[:5]})")
                results.append(d)
            except Exception as e:
                errs.append(str(e))
    if not results:
        raise RuntimeError("; ".join(errs))
    games = {}
    for d in results:
        for g in d["games"]:
            cur = games.setdefault(g["id"], dict(g, odds=[]))
            have = {(o.get("book_id"), o.get("type")): i for i, o in enumerate(cur["odds"])}
            for o in g.get("odds", []):
                k = (o.get("book_id"), o.get("type"))
                if k not in have:
                    have[k] = len(cur["odds"]); cur["odds"].append(o)
                elif (o.get("inserted") or "") > (cur["odds"][have[k]].get("inserted") or ""):
                    cur["odds"][have[k]] = o
    out = []
    for g in games.values():
        if str(g.get("status", "")).lower() in AN_FINISHED:     # keep scheduled + in-progress games
            continue
        teams = {t["id"]: t["full_name"] for t in g.get("teams", [])}
        home, away = teams.get(g["home_team_id"], "Home"), teams.get(g["away_team_id"], "Away")
        books = {}
        for o in g.get("odds", []):
            bid = o.get("book_id")
            if o.get("type") != "game" or bid in AN_EXCLUDE or bid not in AN_BOOKS:
                continue
            upd, mk = o.get("inserted"), {}
            def leg(name, point, price):
                return {"name": name, "point": point, "price": price, "link": "", "updated": upd}
            if o.get("ml_home") is not None and o.get("ml_away") is not None:
                mk["h2h"] = [leg(away, None, o["ml_away"]), leg(home, None, o["ml_home"])]
                if o.get("draw") is not None:
                    mk["h2h"].append(leg("Draw", None, o["draw"]))
            if None not in (o.get("spread_home"), o.get("spread_away"), o.get("spread_home_line"), o.get("spread_away_line")):
                mk["spreads"] = [leg(away, o["spread_away"], o["spread_away_line"]),
                                 leg(home, o["spread_home"], o["spread_home_line"])]
            if None not in (o.get("total"), o.get("over"), o.get("under")):
                mk["totals"] = [leg("Over", o["total"], o["over"]), leg("Under", o["total"], o["under"])]
            if mk:
                books[AN_BOOKS[bid]] = mk
        out.append({"id": g["id"], "sport": sport,
                    "start": datetime.fromisoformat(g["start_time"].replace("Z", "+00:00")),
                    "home": home, "away": away, "books": books})
    return out


# ------------------------------------------------------------------ sharp reference books (never bettable here)
# Pinnacle's public guest API (the JSON its own website loads for anonymous visitors; no key, no login).
PINNACLE_LEAGUES = {"nfl": 889, "ncaaf": 880, "nba": 487, "mlb": 246, "nhl": 1456}
PINNACLE_API = "https://guest.api.arcadia.pinnacle.com/0.1"
# The Odds API bookmaker keys treated as sharp/reference-only when --source oddsapi is used.
ODDSAPI_SHARP = {"pinnacle": "Pinnacle", "betonlineag": "BetOnline", "lowvig": "LowVig", "circasports": "Circa",
                 "novig": "Novig", "prophetx": "ProphetX", "sporttrade": "Sporttrade", "betcris": "Betcris"}

import re as _re
def _norm(name):
    n = name.lower().replace("&", "and").replace("st.", "state").replace("'", "").replace("\u2019", "")
    return " ".join(_re.sub(r"[^a-z0-9 ]+", " ", n).split())

TEAM_ALIASES = {"massachusetts": "umass", "connecticut": "uconn", "mississippi": "ole miss", "miami ohio": "miami oh",
                "ul lafayette": "louisiana", "ul monroe": "louisiana monroe", "southern california": "usc",
                "central florida": "ucf", "texas el paso": "utep", "texas san antonio": "utsa", "brigham young": "byu",
                "louisiana state": "lsu", "southern methodist": "smu", "texas christian": "tcu", "nevada las vegas": "unlv"}

def _same_team(a, b):
    a, b = _norm(a), _norm(b)
    a, b = TEAM_ALIASES.get(a, a), TEAM_ALIASES.get(b, b)
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")

def fetch_pinnacle(sport):
    """{matchup_id: {"home","away","start", "h2h":{"home":p,"away":p}, "spreads":{home_hcap:{"home":p,"away":p,"alt":bool}},
    "totals":{pts:{"Over":p,"Under":p,"alt":bool}}}} - full game, open, straight markets only."""
    lg = PINNACLE_LEAGUES[sport]
    with ThreadPoolExecutor(max_workers=2) as pool:
        fm = pool.submit(http_get, f"{PINNACLE_API}/leagues/{lg}/matchups")
        fk = pool.submit(http_get, f"{PINNACLE_API}/leagues/{lg}/markets/straight")
        matchups, markets = fm.result()[0], fk.result()[0]
    games = {}
    for m in matchups:
        if m.get("type") != "matchup" or m.get("parentId") or m.get("isLive") or (m.get("units") or "Regular") != "Regular":
            continue
        side = {p.get("alignment"): p.get("name") for p in m.get("participants", [])}
        if not side.get("home") or not side.get("away") or not m.get("startTime"):
            continue
        games[m["id"]] = {"home": side["home"], "away": side["away"],
                          "start": datetime.fromisoformat(m["startTime"].replace("Z", "+00:00")),
                          "h2h": {}, "spreads": {}, "totals": {}}
    for k in markets:
        g = games.get(k.get("matchupId"))
        if not g or k.get("period") != 0 or k.get("status", "open") != "open" or k.get("type") not in ("moneyline", "spread", "total"):
            continue
        pr = {p.get("designation"): p for p in k.get("prices", []) if p.get("designation") and p.get("price") is not None}
        alt = bool(k.get("isAlternate"))
        if k["type"] == "moneyline" and "home" in pr and "away" in pr and not alt:
            g["h2h"] = {"home": pr["home"]["price"], "away": pr["away"]["price"]}
        elif k["type"] == "spread" and "home" in pr and "away" in pr and pr["home"].get("points") is not None:
            hc = float(pr["home"]["points"])
            if hc not in g["spreads"] or (g["spreads"][hc]["alt"] and not alt):
                g["spreads"][hc] = {"home": pr["home"]["price"], "away": pr["away"]["price"], "alt": alt}
        elif k["type"] == "total" and "over" in pr and "under" in pr and pr["over"].get("points") is not None:
            pts = float(pr["over"]["points"])
            if pts not in g["totals"] or (g["totals"][pts]["alt"] and not alt):
                g["totals"][pts] = {"Over": pr["over"]["price"], "Under": pr["under"]["price"], "alt": alt}
    return games

def attach_pinnacle(events, sport, pin):
    """Match Pinnacle games to our events and store
    ev["sharp"]["Pinnacle"][mkey][line_key] = {outcome_name: price, "alt": bool}.
    Best candidate wins: both teams match (start within 3h) beats one team + same start (+-15 min)."""
    matched = 0
    for ev in events:
        if ev["sport"] != sport:
            continue
        best = None
        for g in pin.values():
            dt = abs((g["start"] - ev["start"]).total_seconds())
            if dt > 3 * 3600:
                continue
            hh, aa = _same_team(g["home"], ev["home"]), _same_team(g["away"], ev["away"])
            ha, ah = _same_team(g["home"], ev["away"]), _same_team(g["away"], ev["home"])
            if hh and aa:
                cand = (3, -dt, False)
            elif ha and ah:
                cand = (3, -dt, True)
            elif dt <= 15 * 60 and (hh or aa) != (ha or ah):   # one team + same kickoff ("Temple" vs "UConn"/"Connecticut")
                cand = (1, -dt, bool(ha or ah))
            else:
                continue
            if best is None or cand[:2] > best[0][:2]:
                best = (cand, g)
        if not best:
            continue
        (_, _, flip), g = best
        h, a = (ev["away"], ev["home"]) if flip else (ev["home"], ev["away"])
        ref = {}
        if g["h2h"]:
            ref["h2h"] = {None: {h: g["h2h"]["home"], a: g["h2h"]["away"], "alt": False}}
        if g["spreads"]:   # our line_key = handicap of OUR home team
            ref["spreads"] = {(-hc if flip else hc): {h: v["home"], a: v["away"], "alt": v["alt"]}
                              for hc, v in g["spreads"].items()}
        if g["totals"]:
            ref["totals"] = dict(g["totals"])
        ev.setdefault("sharp", {})["Pinnacle"] = ref
        ev["sharp_match"] = f"{g['away']} @ {g['home']}" + (" (home/away flipped)" if flip else "")
        matched += 1
    return matched

# ------------------------------------------------------------------ analysis
MKT = {"h2h": "moneyline", "spreads": "spread", "totals": "total"}

def line_key(mkey, o, home):
    """Identifies one complete market (all sides) at one number."""
    if mkey == "h2h":
        return None
    if mkey == "totals":
        return float(o["point"])
    p = float(o["point"])            # spreads: express as home-team handicap
    return p if o["name"] == home else -p

def outcome_label(mkey, name, point):
    if point is None:
        return name
    return f"{name} {float(point):+g}" if mkey == "spreads" else f"{name} {float(point):g}"

def analyse(events, args):
    now = datetime.now(timezone.utc)
    value_rows, vig_rows, arbs = [], [], []
    book_vigs = defaultdict(list)
    cov = {"markets": 0, "sharp_markets": 0, "pinnacle_markets": 0, "games": 0, "sharp_games": 0}
    for ev in events:
        # keep upcoming and live games; drop games that started more than --max-live-hours ago (clearly over).
        # all datetimes are tz-aware UTC
        if ev["start"] < now - timedelta(hours=args.max_live_hours) and not args.include_started:
            continue
        live = ev["start"] <= now
        game = f"{ev['away']} @ {ev['home']}"
        cov["games"] += 1
        cov["sharp_games"] += bool(ev.get("sharp"))
        # markets[(mkey, line)][book] = {outcome_name: leg}
        markets = defaultdict(lambda: defaultdict(dict))
        for book, mks in ev["books"].items():
            for mkey, outs in mks.items():
                for o in outs:
                    markets[(mkey, line_key(mkey, o, ev["home"]))][book][o["name"]] = o
        for (mkey, lk), by_book in markets.items():
            # per-book vig + no-vig probs (only books quoting every side)
            all_names = sorted({n for legs in by_book.values() for n in legs})
            complete = {b: legs for b, legs in by_book.items() if len(legs) == len(all_names) and len(all_names) >= 2}
            vigs, novig = {}, defaultdict(dict)
            for b, legs in complete.items():
                tot = sum(implied(l["price"]) for l in legs.values())
                vigs[b] = (tot - 1) * 100
                for n, l in legs.items():
                    novig[n][b] = implied(l["price"]) / tot
            avg_vig = sum(vigs.values()) / len(vigs) if vigs else None
            for b, v in vigs.items():
                book_vigs[b].append(v)
                vig_rows.append({"sport": ev["sport"].upper(), "game": game, "start_et": et(ev["start"]),
                                 "market": MKT[mkey], "line": "" if lk is None else f"{lk:+g}" if mkey == "spreads" else f"{lk:g}",
                                 "book": b, "vig_pct": round(v, 2), "market_avg_vig_pct": round(avg_vig, 2)})
            # best price per outcome
            best = {}
            for n in all_names:
                quotes = {b: legs[n] for b, legs in by_book.items() if n in legs}
                bb = max(quotes, key=lambda b: to_decimal(quotes[b]["price"]))
                best[n] = (bb, quotes)
            best_hold = None
            if len(all_names) >= 2:
                best_hold = (sum(implied(best[n][1][best[n][0]]["price"]) for n in all_names) - 1) * 100

            # --- fair price: weighted average of EVERY book's own no-vig line (sharp books weighted more)
            sharp_fair, sharp_px, alt_used, sharp_skipped = {}, {}, False, [0]
            for sb, ref in (ev.get("sharp") or {}).items():
                q = (ref.get(mkey) or {}).get(lk)
                if not q or len(all_names) < 2 or any(n not in q for n in all_names):
                    continue
                tot = sum(implied(q[n]) for n in all_names)
                sf = {n: implied(q[n]) / tot for n in all_names}
                cons = {n: sum(novig[n].values()) / len(novig[n]) for n in all_names if novig.get(n)}
                if cons and max(abs(sf[n] - cons[n]) for n in cons) > args.sharp_max_gap / 100:
                    sharp_skipped[0] += 1     # probably a mismatched game/line - don't trust it
                    continue
                sharp_fair[sb] = sf
                sharp_px[sb] = q
                if sb == "Pinnacle":
                    alt_used = q.get("alt", False)
            # per-book no-vig: retail books quoting every side + accepted sharp lines
            nv_book = {b: {n: novig[n][b] for n in all_names} for b in complete}
            nv_book.update(sharp_fair)
            w = {b: args.weights.get(b, DEFAULT_WEIGHT) for b in nv_book}
            nv_book = {b: v for b, v in nv_book.items() if w[b] > 0}
            def wavg(bs):
                tw = sum(w[b] for b in bs)
                if not bs or tw <= 0:
                    return None
                raw = {n: sum(w[b] * nv_book[b][n] for b in bs) / tw for n in all_names}
                t = sum(raw.values())
                return {n: raw[n] / t for n in all_names}
            dropped = []
            if args.stale_guard_pp > 0 and len(nv_book) >= 3:
                for b in [b for b in nv_book if b not in SHARP_BOOKS]:
                    rest = wavg([x for x in nv_book if x != b])
                    if rest and max(abs(nv_book[b][n] - rest[n]) for n in all_names) * 100 > args.stale_guard_pp:
                        dropped.append(b)
            used = [b for b in nv_book if b not in dropped]
            wfair = wavg(used)
            pin_fair = sharp_fair.get("Pinnacle")
            sharps_used = sorted(b for b in used if b in SHARP_BOOKS)
            n_other = len([b for b in used if b not in sharps_used])
            if wfair is None:
                fair_src = ""
            elif sharps_used:
                fair_src = (f"weighted ({' + '.join(sharps_used)}{' alt line' if alt_used and 'Pinnacle' in sharps_used else ''}"
                            f" + {n_other} book{'s' if n_other != 1 else ''})")
            else:
                fair_src = f"weighted ({n_other} book{'s' if n_other != 1 else ''}, no sharp)"
            if dropped:
                fair_src += f", dropped stale: {', '.join(sorted(dropped))}"
            fair_books = sharps_used
            if any(len(best[n][1]) >= args.min_books for n in all_names):
                cov["markets"] += 1
                cov["sharp_markets"] += bool(sharps_used)
                cov["pinnacle_markets"] += "Pinnacle" in sharps_used
                cov["sharp_rejected"] = cov.get("sharp_rejected", 0) + sharp_skipped[0]
                cov["stale_dropped"] = cov.get("stale_dropped", 0) + len(dropped)

            # --- value / EV rows per outcome
            for n in all_names:
                bb, quotes = best[n]
                if len(quotes) < args.min_books:
                    continue
                probs = {b: implied(q["price"]) for b, q in quotes.items()}
                avg_p = sum(probs.values()) / len(probs)
                bp = quotes[bb]["price"]
                ties = sorted(b for b in quotes if quotes[b]["price"] == bp)
                others = [p for b, p in probs.items() if b not in ties]
                stale = bool(others) and (sum(others) / len(others) - probs[bb]) * 100 > args.stale_pp
                nv = novig.get(n, {})
                cons_p = sum(nv.values()) / len(nv) if nv else None
                fair_p = wfair[n] if wfair else cons_p
                pin_p = pin_fair[n] if pin_fair else None
                bdec = to_decimal(bp)
                value_rows.append({
                    "sport": ev["sport"].upper(), "game": game, "start_et": et(ev["start"]),
                    "start_utc": ev["start"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "start_iso": ev["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "market": MKT[mkey], "outcome": outcome_label(mkey, n, quotes[bb]["point"]),
                    "n_books": len(quotes), "best_book": " / ".join(ties), "best_odds": fmt_odds(bp),
                    "avg_odds": fmt_odds(to_american(avg_p)),
                    "avg_implied_pct": round(avg_p * 100, 2), "best_implied_pct": round(probs[bb] * 100, 2),
                    "edge_pp": round((avg_p - probs[bb]) * 100, 2),
                    "payout_diff_pct": round((bdec * avg_p - 1) * 100, 2),
                    "novig_fair_odds": fmt_odds(to_american(fair_p)) if fair_p else "n/a",
                    "ev_vs_fair_pct": round((bdec * fair_p - 1) * 100, 2) if fair_p else None,
                    "fair_source": fair_src if fair_p else "",
                    "confidence": "high (sharp)" if "Pinnacle" in fair_books else "normal",
                    "ev_vs_pinnacle_pct": round((bdec * pin_p - 1) * 100, 2) if pin_p else None,
                    "pinnacle_fair_odds": fmt_odds(to_american(pin_p)) if pin_p else "n/a",
                    "sharp_odds": "; ".join(f"{b} {fmt_odds(sharp_px[b][n])}" for b in sorted(sharp_px)),
                    "consensus_fair_odds": fmt_odds(to_american(cons_p)) if cons_p else "n/a",
                    "ev_vs_consensus_pct": round((bdec * cons_p - 1) * 100, 2) if cons_p else None,
                    "best_book_vig_pct": round(vigs[bb], 2) if bb in vigs else None,
                    "market_avg_vig_pct": round(avg_vig, 2) if avg_vig is not None else None,
                    "best_available_hold_pct": round(best_hold, 2) if best_hold is not None else None,
                    "stale_warning": "verify before betting" if stale else "",
                    "live": live,
                    "best_book_link": quotes[bb]["link"] or book_link(bb, ev["sport"]),
                    "all_books": "; ".join(f"{b} {fmt_odds(q['price'])}" for b, q in
                                           sorted(quotes.items(), key=lambda x: -to_decimal(x[1]["price"]))),
                })

            # --- arbitrage
            if best_hold is not None and best_hold < 0 and len({best[n][0] for n in all_names}) >= 2:
                inv = {n: 1 / to_decimal(best[n][1][best[n][0]]["price"]) for n in all_names}
                s = sum(inv.values())
                legs, stale_any = [], False
                for n in all_names:
                    bb, quotes = best[n]
                    q = quotes[bb]
                    others = [implied(x["price"]) for b, x in quotes.items() if b != bb]
                    cons = sum(others) / len(others) if others else None
                    dev = (cons - implied(q["price"])) * 100 if cons is not None else None
                    stale = dev is not None and dev > args.stale_pp
                    stale_any |= stale
                    legs.append({"outcome": outcome_label(mkey, n, q["point"]), "book": bb,
                                 "odds": fmt_odds(q["price"]), "decimal": round(to_decimal(q["price"]), 4),
                                 "stake_usd": round(100 * inv[n] / s, 2),
                                 "other_books_avg_odds": fmt_odds(to_american(cons)) if cons else "n/a",
                                 "deviation_pp": round(dev, 2) if dev is not None else None,
                                 "stale": stale, "line_updated": q.get("updated"),
                                 "link": q["link"] or book_link(bb, ev["sport"]),
                                 "link_type": "event" if q["link"] else "sportsbook/league page"})
                sig = f"{ev['id']}|{mkey}|{lk}|" + "|".join(f"{l['outcome']}@{l['book']}" for l in legs)
                arbs.append({
                    "id": hashlib.sha1(sig.encode()).hexdigest()[:16],
                    "price_id": hashlib.sha1((sig + "|" + "|".join(l["odds"] for l in legs)).encode()).hexdigest()[:16],
                    "sport": ev["sport"].upper(), "game": game, "game_id": ev["id"],
                    "start_utc": ev["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "start_et": et(ev["start"]),
                    "market": MKT[mkey], "line": lk,
                    "profit_pct": round((1 / s - 1) * 100, 2), "best_available_hold_pct": round(best_hold, 2),
                    "total_stake_usd": 100, "payout_usd": round(100 / s, 2),
                    "stale_warning": "verify before betting" if stale_any else "", "live": live, "legs": legs})
    value_rows.sort(key=lambda r: r["edge_pp"], reverse=True)
    arbs.sort(key=lambda a: a["profit_pct"], reverse=True)
    ev_rows = sorted([r for r in value_rows if r["ev_vs_fair_pct"] is not None and r["ev_vs_fair_pct"] > 0],
                     key=lambda r: r["ev_vs_fair_pct"], reverse=True)
    book_avg = {b: (round(sum(v) / len(v), 2), len(v)) for b, v in sorted(book_vigs.items(), key=lambda x: sum(x[1]) / len(x[1]))}
    STATS.update(cov)
    return value_rows, ev_rows, vig_rows, arbs, book_avg

STATS = {}


# ------------------------------------------------------------------ writers
def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_")
    with os.fdopen(fd, "w", newline="") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)

def csv_text(rows, fields=None):
    import io
    buf = io.StringIO()
    fields = fields or (list(rows[0].keys()) if rows else ["(no rows)"])
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader(); w.writerows(rows)
    return buf.getvalue()

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f6f8;color:#1d2330}
nav{position:sticky;top:0;z-index:5;background:#14213d;display:flex;gap:4px;padding:8px 12px;flex-wrap:wrap}
nav a{color:#cfd8ea;text-decoration:none;padding:8px 14px;border-radius:8px;font-weight:600;font-size:15px}
nav a.on{background:#fca311;color:#14213d}
main{max-width:1200px;margin:0 auto;padding:14px}
h1{font-size:22px;margin:6px 0}.meta{color:#5b6475;font-size:13px;margin-bottom:10px}
.note{background:#fff7e0;border:1px solid #f2d58a;border-radius:8px;padding:10px 12px;font-size:14px;margin:10px 0}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin:12px 0;padding:12px 14px}
.card h3{margin:0 0 4px;font-size:17px}.pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:12px;font-weight:700}
.green{background:#d9f5e3;color:#11643a}.red{background:#fde2e1;color:#9b1c1c}.grey{background:#e9ecf2;color:#444}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:14px;background:#fff}
th,td{padding:8px 9px;border-bottom:1px solid #eceef2;text-align:left;white-space:nowrap}
th{background:#eef1f6;position:sticky;top:0;cursor:pointer;user-select:none;font-size:13px}
th:after{content:" \\2195";color:#9aa3b5;font-size:11px}tr:hover td{background:#fafbfd}
td.num{text-align:right;font-variant-numeric:tabular-nums}a{color:#1f5fbf}
.empty{padding:24px;text-align:center;color:#5b6475}
"""
SORT_JS = """
document.querySelectorAll('table.sortable').forEach(function(t){
 t.querySelectorAll('th').forEach(function(th,i){th.addEventListener('click',function(){
  var b=t.tBodies[0],rows=[].slice.call(b.rows),asc=th.dataset.dir!=='asc';th.dataset.dir=asc?'asc':'desc';
  function v(r){var c=r.cells[i],s=(c.dataset.v!==undefined?c.dataset.v:c.textContent).trim();
   var n=parseFloat(s.replace(/[$,%+]/g,''));return isNaN(n)?s.toLowerCase():n;}
  rows.sort(function(a,c){var x=v(a),y=v(c);return (x>y?1:x<y?-1:0)*(asc?1:-1);});
  rows.forEach(function(r){b.appendChild(r);});});});});
"""
NOTE = ("Odds move fast. Prices come from a public feed and may be minutes old or already gone. "
        "Always confirm the price on the sportsbook before betting. Links go to each book's site or league "
        "page (the feed has no event-level links). Informational only, not betting advice.")

NAV_LOCAL = {"arbitrage.html": "arbitrage.html", "positive_ev.html": "positive_ev.html", "arbitrage.csv": "arbitrage.csv",
             "positive_ev.csv": "positive_ev.csv", "vig.csv": "vig.csv"}
NAV = dict(NAV_LOCAL)

def page(title, active, body, updated, source_label):
    e = html.escape
    tgt = ' target="_top"' if NAV is not NAV_LOCAL and NAV.get("_drive") else ""
    nav = "".join(f'<a href="{e(NAV[href])}"{tgt} class="{"on" if key == active else ""}">{e(lbl)}</a>'
                  for key, href, lbl in [("arb", "arbitrage.html", "Arbitrage"), ("ev", "positive_ev.html", "Positive EV")])
    nav += "".join(f'<a href="{e(NAV[f])}"{tgt} style="font-weight:400;font-size:13px">{f}</a>'
                   for f in ("arbitrage.csv", "positive_ev.csv", "vig.csv"))
    nav += "".join(f'<a href="{e(NAV[k])}"{tgt} style="font-weight:400;font-size:13px">&#128202; {lbl}</a>'
                   for k, lbl in (("sheet:Arbitrage", "Arbitrage sheet"), ("sheet:Positive EV", "Positive EV sheet"),
                                  ("sheet:Vig by Book", "Vig sheet")) if k in NAV)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title><style>{CSS}</style></head>
<body><nav>{nav}</nav><main><h1>{e(title)}</h1>
<div class="meta">Last updated: <b>{e(updated)}</b> &middot; Source: {e(source_label)}</div>
<div class="note">&#9888; {e(NOTE)}</div>{body}</main><script>{SORT_JS}</script></body></html>"""

def link(url, text):
    e = html.escape
    return f'<a href="{e(url)}" target="_blank" rel="noopener">{e(text)}</a>' if url else e(text)

def vig_table(book_avg):
    e = html.escape
    rows = "".join(f"<tr><td>{e(b)}</td><td class='num'>{v:.2f}%</td><td class='num'>{n}</td></tr>"
                   for b, (v, n) in book_avg.items())
    return (f"<div class='card'><h3>Average vig (hold) by book</h3><div class='wrap'><table class='sortable'>"
            f"<thead><tr><th>Book</th><th>Avg vig</th><th>Markets</th></tr></thead><tbody>{rows}</tbody></table></div></div>")

def arb_html(arbs, book_avg, updated, src):
    e = html.escape
    if not arbs:
        body = "<div class='card empty'>No arbitrage opportunities right now.</div>"
    else:
        body = ""
        for a in arbs:
            warn = f"<span class='pill red'>&#9888; {e(a['stale_warning'])}</span>" if a["stale_warning"] else ""
            legs = "".join(
                f"<tr><td>{e(l['outcome'])}</td><td>{link(l['link'], l['book'])}</td><td class='num'>{e(l['odds'])}</td>"
                f"<td class='num'>{e(l['other_books_avg_odds'])}</td><td class='num'>${l['stake_usd']:.2f}</td>"
                f"<td>{'<span class=\"pill red\">verify before betting</span>' if l['stale'] else '<span class=\"pill grey\">ok</span>'}</td></tr>"
                for l in a["legs"])
            body += (f"<div class='card'><h3>{e(a['game'])} <span class='pill grey'>{e(a['sport'])}</span></h3>"
                     f"<div class='meta'>{e(a['start_et'])} &middot; {e(a['market'])} &middot; "
                     f"<span class='pill green'>+{a['profit_pct']:.2f}% profit</span> {warn}<br>"
                     f"$100 total stake &rarr; ${a['payout_usd']:.2f} payout whichever side wins</div>"
                     f"<div class='wrap'><table><thead><tr><th>Leg</th><th>Book</th><th>Odds</th><th>Other books avg</th>"
                     f"<th>Stake</th><th>Check</th></tr></thead><tbody>{legs}</tbody></table></div></div>")
    body += vig_table(book_avg)
    return page("Arbitrage opportunities", "arb", body, updated, src)

def ev_html(ev_rows, book_avg, updated, src, limit):
    e = html.escape
    cols = ["Sport", "Game", "Start (ET)", "Market", "Outcome", "Best book", "Best", "Sharp ref", "Fair", "Fair source",
            "EV %", "Confidence", "Avg", "Edge pp", "Book vig", "Check"]
    trs = ""
    for r in ev_rows[:limit]:
        start_v = r["start_utc"]
        trs += ("<tr>" + f"<td>{e(r['sport'])}</td><td>{e(r['game'])}</td><td data-v='{start_v}'>{e(r['start_et'])}</td>"
                f"<td>{e(r['market'])}</td><td>{e(r['outcome'])}</td><td>{link(r['best_book_link'], r['best_book'])}</td>"
                f"<td class='num'>{e(r['best_odds'])}</td><td class='num'>{e(r['sharp_odds'] or '-')}</td>"
                f"<td class='num'>{e(r['novig_fair_odds'])}</td><td>{e(r['fair_source'])}</td>"
                f"<td class='num'><b>{r['ev_vs_fair_pct']:.2f}%</b></td>"
                f"<td>{'<span class=\"pill green\">&#9733; high (sharp)</span>' if r['confidence'].startswith('high') else '<span class=\"pill grey\">normal</span>'}</td>"
                f"<td class='num'>{e(r['avg_odds'])}</td><td class='num'>{r['edge_pp']:.2f}</td>"
                f"<td class='num'>{'' if r['best_book_vig_pct'] is None else f'{r['best_book_vig_pct']:.2f}%'}</td>"
                f"<td>{'<span class=\"pill red\">verify before betting</span>' if r['stale_warning'] else ''}</td></tr>")
    table = (f"<div class='card'><div class='meta'>{len(ev_rows)} lines beat the fair price "
             f"(fair = weighted average of every book's own no-vig line on that exact market: Pinnacle x4, other sharp books x2, "
             f"retail x1; retail lines >8pp off the rest are dropped; "
             f"sharp books are reference only, never a 'best book'; &#9733; = Pinnacle included in the fair price) "
             f"(showing top {min(limit, len(ev_rows))}). Tap a column header to sort.</div><div class='wrap'><table class='sortable'><thead><tr>"
             + "".join(f"<th>{c}</th>" for c in cols) + f"</tr></thead><tbody>{trs}</tbody></table></div></div>") if ev_rows \
        else "<div class='card empty'>No positive-EV lines right now.</div>"
    return page("Positive EV lines (vs weighted no-vig fair price)", "ev", table + vig_table(book_avg), updated, src)



# ------------------------------------------------------------------ compact CSVs for the Google Sheets
def write_sheet_csvs(d, arbs, ev_rows, vig_rows, book_avg, updated, src, ev_limit=25):
    """Small, phone-friendly CSVs (first line = 'Last updated (ET)') pushed into native Google Sheets."""
    head = [["Last updated (ET)", updated, src], []]
    arb = head + [["Game", "Start (ET)", "Market", "Profit %", "Leg", "Book", "Odds", "Stake of $100", "Payout", "Check", "Book link"]]
    for a in arbs:
        for l in a["legs"]:
            arb.append([a["game"], a["start_et"], a["market"], a["profit_pct"], l["outcome"], l["book"], l["odds"],
                        l["stake_usd"], a["payout_usd"], "VERIFY - possible stale line" if l["stale"] else "ok", l["link"]])
    if not arbs:
        arb.append(["No arbitrage opportunities right now"])
    ev = head + [["EV %", "Confidence", "Game", "Start (ET)", "Market", "Pick", "Best book", "Best odds", "Sharp ref",
                  "Fair odds (weighted no-vig)", "Fair source", "EV % vs Pinnacle only", "Avg odds", "Books", "Check"]]
    for r in ev_rows[:ev_limit]:
        ev.append([r["ev_vs_fair_pct"], "HIGH (sharp)" if r["confidence"].startswith("high") else "normal", r["game"],
                   r["start_et"], r["market"], r["outcome"], r["best_book"], r["best_odds"], r["sharp_odds"] or "-",
                   r["novig_fair_odds"], r["fair_source"], "" if r["ev_vs_pinnacle_pct"] is None else r["ev_vs_pinnacle_pct"], r["avg_odds"], r["n_books"], "VERIFY" if r["stale_warning"] else ""])
    if not ev_rows:
        ev.append(["No positive-EV lines right now"])
    by_book = defaultdict(lambda: defaultdict(list))
    for r in vig_rows:
        by_book[r["book"]][r["market"]].append(r["vig_pct"])
    avg = lambda v: round(sum(v) / len(v), 2) if v else ""
    vig = head + [["Book", "Avg vig % (all)", "Moneyline", "Spread", "Total", "Markets"]]
    for b, (v, n) in book_avg.items():
        m = by_book[b]
        vig.append([b, v, avg(m["moneyline"]), avg(m["spread"]), avg(m["total"]), n])
    vig += [[], ["Vig = sum of implied probabilities - 1. Lower is better for bettors."]]
    for name, rows in (("arbitrage_sheet.csv", arb), ("positive_ev_sheet.csv", ev), ("vig_by_book_sheet.csv", vig)):
        import io
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        atomic_write(os.path.join(d, name), buf.getvalue())

# ------------------------------------------------------------------ website JSON (GitHub Pages)
def _num(a):
    try:
        return int(str(a).replace("+", ""))
    except (TypeError, ValueError):
        return None

def write_web_json(d, now, updated, src, arbs, ev_rows, vig_rows, book_avg, args, sharp_note):
    os.makedirs(d, exist_ok=True)
    dump = lambda name, obj: atomic_write(os.path.join(d, name), json.dumps(obj, separators=(",", ":"), default=str))
    ev_keep = ["sport", "game", "start_et", "start_utc", "market", "outcome", "n_books", "best_book", "best_odds",
               "avg_odds", "novig_fair_odds", "ev_vs_fair_pct", "fair_source", "confidence", "sharp_odds",
               "pinnacle_fair_odds", "ev_vs_pinnacle_pct", "consensus_fair_odds", "ev_vs_consensus_pct", "live",
               "edge_pp", "best_book_vig_pct", "market_avg_vig_pct", "stale_warning", "best_book_link", "all_books"]
    ev = [dict({k: r.get(k) for k in ev_keep}, best_odds_num=_num(r["best_odds"]), start_iso=r["start_iso"]) for r in ev_rows[:500]]
    by_book = defaultdict(lambda: defaultdict(list))
    for r in vig_rows:
        by_book[r["book"]][r["market"]].append(r["vig_pct"])
        by_book[r["book"]]["_sport_" + r["sport"]].append(r["vig_pct"])
    avg = lambda v: round(sum(v) / len(v), 2) if v else None
    vig = [{"book": b, "avg_vig_pct": v, "markets": n,
            "moneyline": avg(by_book[b]["moneyline"]), "spread": avg(by_book[b]["spread"]), "total": avg(by_book[b]["total"]),
            "by_sport": {k[7:]: avg(x) for k, x in by_book[b].items() if k.startswith("_sport_")}}
           for b, (v, n) in book_avg.items()]
    dump("arbs.json", arbs)
    dump("ev.json", ev)
    dump("vig.json", vig)
    dump("meta.json", {"generated_utc": now.isoformat(), "generated_et": updated, "source": src,
                       "sharp_source": "Pinnacle public guest API (reference only)",
                       "pinnacle_match": sharp_note, "weights": args.weights, "stale_guard_pp": args.stale_guard_pp,
                       "counts": {"arbs": len(arbs), "ev": len(ev_rows), "books": len(book_avg)},
                       "coverage": STATS})

# ------------------------------------------------------------------ main
def main():
    t0 = time.time()
    ap = argparse.ArgumentParser(description="Sportsbook odds: averages, vig, arbitrage, +EV.")
    ap.add_argument("--sport", default=",".join(DEFAULT_SPORTS), help=f"comma list of {','.join(SPORTS)}")
    ap.add_argument("--source", choices=["actionnetwork", "oddsapi", "fixture"], default="actionnetwork")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-books", type=int, default=3, help="min books quoting a line for avg/EV rows (default 3)")
    ap.add_argument("--stale-pp", type=float, default=5.0, help="flag legs this many implied-prob points better than other books (default 5)")
    ap.add_argument("--out", default=None, help="main CSV path (default output/odds_<src>_<ts>.csv)")
    ap.add_argument("--arb-json", default=None, help="also write arbs JSON here (site/arbs_latest.json is always written)")
    ap.add_argument("--site-dir", default=SITE)
    ap.add_argument("--nav-links", choices=["local", "drive"], default="local",
                    help="drive = also write site/drive/*.html whose nav links point at the Drive files in drive_ids.json")
    ap.add_argument("--date", help="Action Network: YYYY-MM-DD slate")
    ap.add_argument("--an-passes", type=int, default=4, help="Action Network: parallel requests merged per sport (default 4)")
    ap.add_argument("--regions", default="us"); ap.add_argument("--books", default=None)
    ap.add_argument("--fixture"); ap.add_argument("--include-started", action="store_true")
    ap.add_argument("--days", type=int, default=2, help="NBA/MLB/NHL: number of ET days to fetch starting today (default 2)")
    ap.add_argument("--max-live-hours", type=float, default=4.0,
                    help="keep games that already started for up to this many hours (live), then drop them (default 4)")
    ap.add_argument("--no-sharp", action="store_true", help="skip Pinnacle reference lines (fair = consensus)")
    ap.add_argument("--web-json", default=None, help="also write website data (meta/arbs/ev/vig .json) into this directory")
    ap.add_argument("--weights", default="", help='fair-price weight overrides, e.g. "Pinnacle=5,BetMGM=0.5" (0 = exclude)')
    ap.add_argument("--stale-guard-pp", type=float, default=8.0,
                    help="drop a retail book from the fair calc if its no-vig prob is this many pp from the weighted rest (0 = off; default 8)")
    ap.add_argument("--sharp-max-gap", type=float, default=12.0,
                    help="ignore a sharp line whose no-vig prob differs from the book consensus by more than this many pp (default 12)")
    ap.add_argument("--sharp-timeout", type=float, default=20.0, help="max seconds to wait for Pinnacle before falling back (default 20)")
    args = ap.parse_args()
    wts = dict(WEIGHTS)
    for part in filter(None, (x.strip() for x in args.weights.split(","))):
        k, _, v = part.partition("=")
        try:
            wts[k.strip()] = float(v)
        except ValueError:
            sys.exit(f"bad --weights entry {part!r} (use Book=number)")
    args.weights = wts

    fetch = {"actionnetwork": fetch_actionnetwork, "oddsapi": fetch_oddsapi, "fixture": fetch_fixture}[args.source]
    src = {"actionnetwork": "Action Network public feed (live)", "oddsapi": "The Odds API (live)",
           "fixture": "SAMPLE DATA - not live"}[args.source]
    if args.source == "fixture":
        args.include_started = True
    sports = [s.strip().lower() for s in args.sport.split(",") if s.strip()]
    bad = [s for s in sports if s not in SPORTS]
    if bad:
        sys.exit(f"unknown sport(s) {bad}; choose from {','.join(SPORTS)}")

    events, errors = [], []
    sharp_pool = ThreadPoolExecutor(max_workers=len(sports))
    pin_futs = {} if args.no_sharp or args.source == "fixture" else \
        {s: sharp_pool.submit(fetch_pinnacle, s) for s in sports if s in PINNACLE_LEAGUES}
    with ThreadPoolExecutor(max_workers=len(sports)) as pool:
        futs = {s: pool.submit(fetch, s, args) for s in sports}
        for s, f in futs.items():
            try:
                ev = f.result(timeout=HTTP_TIMEOUT + 10)
                print(f"  [{s}] {len(ev)} upcoming games", file=sys.stderr)
                events += ev
            except Exception as e:
                errors.append(f"{s}: {e}")
    if errors:
        print("ERROR: data feed failed - previous site/ files left unchanged.\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(2)

    # sharp reference lines: any failure -> warn and fall back to consensus for that sport
    sharp_note, deadline = [], time.time() + args.sharp_timeout
    for s, f in pin_futs.items():
        try:
            pin = f.result(timeout=max(0.1, deadline - time.time()))
            m = attach_pinnacle(events, s, pin)
            sharp_note.append(f"{s} {m}/{sum(e['sport'] == s for e in events)}")
            print(f"  [{s}] Pinnacle: {len(pin)} games, matched {m}", file=sys.stderr)
        except Exception as e:
            sharp_note.append(f"{s} FAILED")
            print(f"  [{s}] WARNING Pinnacle reference unavailable ({str(e)[:120]}) - using consensus fair price", file=sys.stderr)
    sharp_pool.shutdown(wait=False, cancel_futures=True)

    value_rows, ev_rows, vig_rows, arbs, book_avg = analyse(events, args)
    now = datetime.now(timezone.utc)
    updated = et(now)
    ts = now.strftime("%Y%m%d_%H%M%S")

    out = args.out or os.path.join(HERE, "output", f"odds_{args.source}_{ts}.csv")
    atomic_write(out, csv_text(value_rows))
    sd = args.site_dir
    arb_flat = [{"id": a["id"], "price_id": a["price_id"], "sport": a["sport"], "game": a["game"], "start_et": a["start_et"],
                 "market": a["market"], "profit_pct": a["profit_pct"], "payout_usd": a["payout_usd"],
                 "stale_warning": a["stale_warning"], "leg_n": i + 1, **{k: l[k] for k in
                 ("outcome", "book", "odds", "stake_usd", "other_books_avg_odds", "deviation_pp", "stale", "link")}}
                for a in arbs for i, l in enumerate(a["legs"])]
    atomic_write(os.path.join(sd, "arbitrage.csv"), csv_text(arb_flat, None if arb_flat else
                 ["id", "price_id", "sport", "game", "start_et", "market", "profit_pct", "payout_usd", "stale_warning",
                  "leg_n", "outcome", "book", "odds", "stake_usd", "other_books_avg_odds", "deviation_pp", "stale", "link"]))
    atomic_write(os.path.join(sd, "positive_ev.csv"), csv_text(ev_rows, list(value_rows[0].keys()) if value_rows else None))
    # vig.csv = compact wide table (one row per game/market/line, one column per book);
    # vig_long.csv = one row per book (local only).
    atomic_write(os.path.join(sd, "vig_long.csv"), csv_text(vig_rows))
    wide, order = {}, []
    for r in vig_rows:
        k = (r["sport"], r["game"], r["start_et"], r["market"], r["line"])
        if k not in wide:
            wide[k] = {"sport": k[0], "game": k[1], "market": k[3], "line": k[4],
                       "avg": round(r["market_avg_vig_pct"], 1)}
            order.append(k)
        wide[k][r["book"]] = round(r["vig_pct"], 1)
    books = sorted({r["book"] for r in vig_rows})
    atomic_write(os.path.join(sd, "vig.csv"), csv_text([wide[k] for k in order],
                 ["sport", "game", "market", "line", "avg"] + books))
    write_sheet_csvs(os.path.join(sd, "sheets"), arbs, ev_rows, vig_rows, book_avg, updated, src)
    payload = json.dumps({"generated_utc": now.isoformat(), "generated_et": updated, "source": src,
                          "count": len(arbs), "arbs": arbs}, indent=1, default=str)
    atomic_write(os.path.join(sd, "arbs_latest.json"), payload)
    if args.arb_json:
        atomic_write(os.path.abspath(args.arb_json), payload)
    if args.web_json:
        write_web_json(args.web_json, now, updated, src, arbs, ev_rows, vig_rows, book_avg, args, sharp_note)
    global NAV
    NAV = NAV_LOCAL
    atomic_write(os.path.join(sd, "arbitrage.html"), arb_html(arbs, book_avg, updated, src))
    atomic_write(os.path.join(sd, "positive_ev.html"), ev_html(ev_rows, book_avg, updated, src, 200))
    if args.nav_links == "drive":
        # Drive copies: same pages, nav points at the Drive file URLs from drive_ids.json
        with open(os.path.join(HERE, "drive_ids.json")) as f:
            dj = json.load(f)
        NAV = {name: meta["webViewLink"] for name, meta in dj["files"].items()}
        NAV.update({f"sheet:{name}": meta["webViewLink"] for name, meta in dj.get("sheets", {}).items()})
        NAV["_drive"] = True
        atomic_write(os.path.join(sd, "drive", "arbitrage.html"), arb_html(arbs, book_avg, updated, src))
        atomic_write(os.path.join(sd, "drive", "positive_ev.html"), ev_html(ev_rows, book_avg, updated, src, 200))
        NAV = NAV_LOCAL

    # ---- console summary
    print(f"\n[{src}] updated {updated}: {len(events)} games, {len(value_rows)} lines, "
          f"{len(arbs)} arbs, {len(ev_rows)} +EV lines")
    print("\nARBITRAGE:" + ("" if arbs else " none"))
    for a in arbs:
        print(f"  +{a['profit_pct']}%  {a['game']} | {a['start_et']} | {a['market']} | $100 -> ${a['payout_usd']} {a['stale_warning']}")
        for l in a["legs"]:
            print(f"      {l['outcome']:<32} {l['book']:<11} {l['odds']:>6}  stake ${l['stake_usd']:.2f}"
                  f"  (others avg {l['other_books_avg_odds']}){'  STALE?' if l['stale'] else ''}")
    print("\nWEIGHTS: " + ", ".join(f"{b}={v:g}" for b, v in args.weights.items()) + f" (others={DEFAULT_WEIGHT:g})")
    print(f"\nTOP {args.top} +EV (vs weighted no-vig fair):")
    for i, r in enumerate(ev_rows[:args.top], 1):
        print(f"  {i:>2}. EV {r['ev_vs_fair_pct']:>5.2f}%  {r['game'][:40]:<40} {r['market']:<9} {r['outcome'][:28]:<28} "
              f"{r['best_book']:<22} {r['best_odds']:>6} (sharp {r['sharp_odds'] or '-'}, fair {r['novig_fair_odds']} "
              f"[{r['fair_source']}], EV vs Pinnacle-only {r['ev_vs_pinnacle_pct']}%, old consensus EV {r['ev_vs_consensus_pct']}%) {r['stale_warning']}")
    c = STATS
    if c:
        print(f"\nSHARP COVERAGE: games with a Pinnacle line {c['sharp_games']}/{c['games']}; "
              f"markets with sharp fair {c['sharp_markets']}/{c['markets']} (Pinnacle {c['pinnacle_markets']}, "
              f"rejected as mismatched {c.get('sharp_rejected', 0)}); stale retail lines dropped from fair {c.get('stale_dropped', 0)}; "
              f"+EV rows high-confidence {sum(r['confidence'].startswith('high') for r in ev_rows)}/{len(ev_rows)}")
    print("\nAVG VIG BY BOOK:")
    for b, (v, n) in book_avg.items():
        print(f"  {b:<12} {v:5.2f}%  ({n} markets)")
    print(f"\nFiles: {out}\n       {sd}/{{arbitrage,positive_ev}}.html, arbitrage.csv, positive_ev.csv, vig.csv, arbs_latest.json")
    print(f"Runtime: {time.time() - t0:.1f}s")

if __name__ == "__main__":
    main()
