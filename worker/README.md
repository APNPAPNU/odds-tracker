# odds-tracker-api (Cloudflare Worker)

This Worker gives the site a live data endpoint, `GET /v1/odds`. It is a server-side JavaScript port of `odds_compare.py`:
- Action Network supplies the retail books.
- Pinnacle's public guest API is the sharp reference, never shown as a bet.
- It computes the weighted no-vig fair price (Pinnacle 4×, other sharp books 2×, retail 1×) with per-book devig and the 8-point stale guard.
- It finds arbitrage with stakes, +EV lines and vig by book, keeping started games as live for up to 4 hours.

On identical input it produces the same results as the Python script.

- **Not a proxy:** it calls fixed upstream URLs only; nothing from the request is forwarded upstream.
- **CORS:** only `https://apnpapnu.github.io` and localhost are allowed.
- **Caching:** each build is held in memory for 27 s, so a page polling every 30 s gets fresh data, and many tabs don't multiply upstream calls.
- **Free-plan CPU:** parsing is split across five internal invocations (service binding to itself) so each stays within the free plan's 10 ms CPU limit:
  - Action Network: NFL, NCAAF, and NBA/MLB/NHL
  - Pinnacle: NFL, and the other leagues
- **Pinnacle parsing:** only game-level matchups and full-game moneyline/spread/total markets are parsed. Pinnacle's NFL list is about 2.6 MB, mostly player props. The game list is cached for 10 minutes.
- **Fallbacks:** if Pinnacle fails, the fair price uses the retail books only. If Action Network fails, the last good result is served for up to 5 minutes, then the endpoint returns 503 and the site falls back to the GitHub-published data.

Deploy: `./deploy.sh` (needs a logged-in `wrangler` and a verified Cloudflare account email).
