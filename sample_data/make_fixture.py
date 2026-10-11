"""Generates sample_oddsapi_response.json: SAMPLE DATA in The Odds API v4 /odds response
shape (oddsFormat=american, markets=h2h,spreads,totals). Prices are invented, not real."""
import json, random, hashlib
random.seed(7)
BOOKS = [("draftkings","DraftKings"),("fanduel","FanDuel"),("betmgm","BetMGM"),("williamhill_us","Caesars"),
         ("espnbet","ESPN BET"),("fanatics","Fanatics"),("betrivers","BetRivers")]
GAMES = [("americanfootball_nfl","NFL","2026-10-18T17:00:00Z","Green Bay Packers","Chicago Bears",-160,-3.5,44.5),
         ("americanfootball_nfl","NFL","2026-10-18T20:25:00Z","Seattle Seahawks","San Francisco 49ers",120,2.5,47.5),
         ("americanfootball_ncaaf","NCAAF","2026-10-17T19:30:00Z","Ohio State Buckeyes","Penn State Nittany Lions",-240,-6.5,49.5),
         ("basketball_nba","NBA","2026-10-21T23:30:00Z","Boston Celtics","New York Knicks",-175,-4.5,221.5),
         ("icehockey_nhl","NHL","2026-10-17T23:00:00Z","Toronto Maple Leafs","Montreal Canadiens",-150,-1.5,6.5),
         ("baseball_mlb","MLB","2026-10-17T00:08:00Z","Los Angeles Dodgers","New York Mets",-135,-1.5,8.0)]
def am(p):
    return round(100*(1-p)/p) if p < .5 else round(-100*p/(1-p))
def ip(a):
    return 100/(a+100) if a > 0 else -a/(-a+100)
def pair(p_fair, vig=0.045, jitter=0.012):
    p = min(max(p_fair + random.uniform(-jitter, jitter), 0.03), 0.97)
    return am(p*(1+vig/2)), am((1-p)*(1+vig/2))
out = []
for sk, st, t, home, away, ml_home, sp_home, tot in GAMES:
    ph = ip(ml_home)/1.02  # rough de-vigged home win prob
    bms = []
    for key, title in BOOKS:
        h, a = pair(ph)
        sh, sa = pair(0.5, jitter=0.02)
        o, u = pair(0.5, jitter=0.02)
        bms.append({"key": key, "title": title, "last_update": "2026-10-10T12:00:00Z", "markets": [
            {"key": "h2h", "last_update": "2026-10-10T12:00:00Z", "outcomes": [{"name": away, "price": a}, {"name": home, "price": h}]},
            {"key": "spreads", "last_update": "2026-10-10T12:00:00Z", "outcomes": [{"name": away, "price": sa, "point": -sp_home}, {"name": home, "price": sh, "point": sp_home}]},
            {"key": "totals", "last_update": "2026-10-10T12:00:00Z", "outcomes": [{"name": "Over", "price": o, "point": tot}, {"name": "Under", "price": u, "point": tot}]}]})
    out.append({"id": hashlib.md5((home+away).encode()).hexdigest(), "sport_key": sk, "sport_title": st,
                "commence_time": t, "home_team": home, "away_team": away, "bookmakers": bms})
json.dump(out, open(__file__.replace("make_fixture.py", "sample_oddsapi_response.json"), "w"), indent=1)
print(len(out), "sample events written")
