# Odds Tracker

**Live site:** https://apnpapnu.github.io/odds-tracker/

A mobile-friendly static site that compares public sportsbook odds (NFL, NCAAF, NBA, MLB, NHL; moneyline, spread, total) and shows:

- **Arbitrage**: sets of bets at different books that profit whichever side wins. Each card shows the legs, book links and a live stake calculator.
- **Positive EV**: lines priced better than a weighted no-vig fair price. Pinnacle is used as a sharp reference. You can filter by sport, minimum EV, maximum odds, market and confidence.
- **Vig by Book**: average hold per book, overall and by market type and sport.

The page refreshes its data every minute. Lines that differ sharply from the other books are marked **verify before betting**.

## How it works

| Piece | What it does |
|---|---|
| `odds_compare.py` | Fetches odds, runs the analysis and writes CSV/HTML plus the website JSON (`--web-json DIR` → `meta.json`, `arbs.json`, `ev.json`, `vig.json`) |
| `web/` | The static site (`index.html`, `app.js`, `style.css`); it loads `data/*.json` |
| `.github/workflows/update.yml` | Every 5 minutes, and on manual dispatch, runs the script and force-pushes `web/` + `data/` as a single commit to `gh-pages`, which GitHub Pages serves |
| `check_arbs.py` | Optional helper that lists arbs not yet notified (dedupe by arb id) |

GitHub's scheduled workflows can be delayed or skipped when GitHub is busy. The page shows when the data was generated and turns red if it's older than 15 minutes.

### Data sources (public, no login, no keys)
- **Action Network public odds feed**: DraftKings, FanDuel, BetMGM, Caesars, BetRivers, bet365. The feed returns a random subset of books per request, so the script merges 4 parallel requests per sport.
- **Pinnacle public guest API** (`guest.api.arcadia.pinnacle.com`, the same data pinnacle.com loads for anonymous visitors). It is **reference only**: never a best book, an arb leg, or part of the vig table.
- Optional: `--source oddsapi` with a free `ODDS_API_KEY` from the-odds-api.com. Sharp books in that feed (BetOnline, LowVig, etc.) are also treated as reference only.

### Fair price and EV
1. Devig each book's own line (multiplicative), using only books that quote both sides of that exact market and number.
2. Take a weighted average of those no-vig probabilities and renormalise. Weights live in the `WEIGHTS` block at the top of `odds_compare.py` and can be overridden with `--weights "Pinnacle=5,BetMGM=0.5"`:
   - Pinnacle 4
   - other sharp books (Circa, BetOnline, Bookmaker, Betcris, LowVig, Novig, ProphetX, Sporttrade) 2
   - retail books 1
3. A retail line more than 8 points away from the weighted rest is dropped from the fair price (`--stale-guard-pp`).
4. EV % = best decimal price × fair probability − 1. "High confidence" means Pinnacle is part of the fair price.

Arbitrage: the best price on each side across bettable books, where the sum of implied probabilities is below 1 and the legs are at ≥2 books. Stakes are split ∝ 1/decimal odds. A leg is flagged "verify" if it is more than 5 points better than the other books' average (`--stale-pp`).

## Run locally
```bash
pip install requests
python3 odds_compare.py --sport nfl,nhl --web-json web/data --top 10
cd web && python3 -m http.server 8000   # open http://localhost:8000
python3 odds_compare.py --source fixture   # offline sample data
```

For information only. Odds can be stale or wrong, so always confirm at the sportsbook. 21+, gamble responsibly.
