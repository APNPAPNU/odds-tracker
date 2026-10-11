// odds-tracker-api - Cloudflare Worker
// Server-side port of odds_compare.py (Action Network retail books + Pinnacle reference, weighted fair price,
// arbitrage, +EV, vig by book). NOT a proxy: only fixed upstream endpoints are called and no client input
// is forwarded upstream. Heavy parsing is split into internal "part" invocations (each gets its own CPU budget)
// reached through a service binding to this same Worker; results are cached in memory for ~15 s.

const ORIGINS = ["https://apnpapnu.github.io"];
const LOCAL_ORIGIN = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
const TTL_MS = 27000;               // payload freshness (27 s so a client polling every 30 s always gets a new build)
const MATCHUP_TTL_MS = 10 * 60e3;   // Pinnacle game list (teams/start times) changes slowly
const CARRY_MS = 3 * 60e3;          // keep an Action Network book line seen in the last 3 min (feed returns random subsets)
const AN_PASSES = 2;
const UA = "odds-tracker-api/1.0 (+https://apnpapnu.github.io/odds-tracker/)";
const ET_TZ = "America/New_York";

const SPORTS = { nfl: ["football/nfl", "nfl"], ncaaf: ["football/ncaaf", "ncaaf"], nba: ["basketball/nba", "nba"],
                 mlb: ["baseball/mlb", "mlb"], nhl: ["hockey/nhl", "nhl"] };
const DAILY = ["nba", "mlb", "nhl"];
const AN_BOOKS = { 68: "DraftKings", 69: "FanDuel", 75: "BetMGM", 123: "Caesars", 79: "bet365", 71: "BetRivers" };
const AN_FINISHED = new Set(["complete", "closed", "final", "cancelled", "canceled", "postponed", "abandoned", "suspended"]);
const PIN_LEAGUES = { nfl: 889, ncaaf: 880, nba: 487, mlb: 246, nhl: 1456 };
const PIN_API = "https://guest.api.arcadia.pinnacle.com/0.1";
const WEIGHTS = { Pinnacle: 4, Circa: 2, BetOnline: 2, Bookmaker: 2, Betcris: 2, LowVig: 2, Novig: 2, ProphetX: 2, Sporttrade: 2,
                  DraftKings: 1, FanDuel: 1, BetMGM: 1, Caesars: 1, BetRivers: 1, bet365: 1 };
const DEFAULT_WEIGHT = 1;
const SHARP = new Set(["Pinnacle", "Circa", "BetOnline", "Bookmaker", "Betcris", "LowVig", "Novig", "ProphetX", "Sporttrade"]);
const CFG = { min_books: 3, stale_pp: 5, sharp_max_gap: 12, stale_guard_pp: 8, max_live_hours: 4 };
const MKT = { h2h: "moneyline", spreads: "spread", totals: "total" };
const TEAM_ALIASES = { "massachusetts": "umass", "connecticut": "uconn", "mississippi": "ole miss", "miami ohio": "miami oh",
  "ul lafayette": "louisiana", "ul monroe": "louisiana monroe", "southern california": "usc", "central florida": "ucf",
  "texas el paso": "utep", "texas san antonio": "utsa", "brigham young": "byu", "louisiana state": "lsu",
  "southern methodist": "smu", "texas christian": "tcu", "nevada las vegas": "unlv" };

// ------------------------------------------------------------------ odds math
const implied = a => a > 0 ? 100 / (a + 100) : -a / (-a + 100);
const toAmerican = p => (!p || p <= 0 || p >= 1) ? null : p < 0.5 ? Math.round(100 * (1 - p) / p) : Math.round(-100 * p / (1 - p));
const toDecimal = a => a > 0 ? 1 + a / 100 : 1 + 100 / -a;
const fmtOdds = a => a == null ? "n/a" : a > 0 ? `+${Math.trunc(a)}` : String(Math.trunc(a));
const r2 = x => Math.round(x * 100) / 100;
const fmtG = x => String(Number(x));                     // python %g for half-points
const fmtSigned = x => (x > 0 ? "+" : "") + fmtG(x);     // python %+g
const etParts = new Intl.DateTimeFormat("en-US", { timeZone: ET_TZ, weekday: "short", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", hour12: true });
const etCache = new Map();
function et(ms) {
  let v = etCache.get(ms);
  if (v) return v;
  const p = Object.fromEntries(etParts.formatToParts(new Date(ms)).map(x => [x.type, x.value]));
  v = `${p.weekday} ${p.month} ${p.day} ${p.hour}:${p.minute} ${p.dayPeriod} ET`;
  if (etCache.size > 2000) etCache.clear();
  etCache.set(ms, v);
  return v;
}
const isoZ = ms => new Date(ms).toISOString().slice(0, 19) + "Z";
const etDate = (ms) => new Intl.DateTimeFormat("en-CA", { timeZone: ET_TZ, year: "numeric", month: "2-digit", day: "2-digit" })
  .format(new Date(ms)).replaceAll("-", "");
function bookLink(book, sport) {
  const b = book.toLowerCase(), s = SPORTS[sport];
  if (b.includes("draftkings")) return s ? `https://sportsbook.draftkings.com/leagues/${s[0]}` : "https://sportsbook.draftkings.com";
  if (b.includes("fanduel")) return s ? `https://sportsbook.fanduel.com/navigation/${s[1]}` : "https://sportsbook.fanduel.com";
  if (b.includes("betmgm")) return "https://sports.betmgm.com";
  if (b.includes("betrivers")) return "https://www.betrivers.com";
  if (b.includes("caesars") || b.includes("william hill")) return "https://www.caesars.com/sportsbook-and-casino";
  if (b.includes("bet365")) return "https://www.bet365.com";
  return "";
}
async function sha1hex(s) {
  const d = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function getText(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA, "Accept": "application/json" } });
  if (r.status !== 200) throw new Error(`HTTP ${r.status} from ${new URL(url).host}`);
  return r.text();
}

// Pretty-printed top-level JSON array -> parse only the elements whose text contains all `need` markers
// (Pinnacle returns ~2.6 MB of player props for NFL; we need ~25 KB of it). Falls back to a full parse.
function pickTopLevel(text, need, anyOf) {
  const out = [];
  if (!text.startsWith("[\n  {")) {
    return JSON.parse(text);   // caller re-filters
  }
  let s = text.indexOf("\n  {");
  while (s >= 0) {
    const e = text.indexOf("\n  }", s);
    if (e < 0) break;
    const chunk = text.slice(s + 1, e + 4);
    if (need.every(n => chunk.includes(n)) && (!anyOf || anyOf.some(n => chunk.includes(n)))) out.push(JSON.parse(chunk));
    s = text.indexOf("\n  {", e);
  }
  return out;
}

// ------------------------------------------------------------------ Action Network part
const anCarry = new Map();   // `${sport}|${gameId}` -> {game, odds: Map(bookType -> {o, seen})}
async function partActionNetwork(sports, now) {
  const reqs = [];
  for (const sp of sports) {
    const dates = DAILY.includes(sp) ? [etDate(now), etDate(now + 864e5)] : [null];
    for (const d of dates) for (let i = 0; i < AN_PASSES; i++) {
      const u = new URL(`https://api.actionnetwork.com/web/v1/scoreboard/${sp}`);
      u.searchParams.set("period", "game"); u.searchParams.set("bookIds", Object.keys(AN_BOOKS).join(","));
      if (d) u.searchParams.set("date", d);
      reqs.push([sp, u.toString()]);
    }
  }
  const res = await Promise.allSettled(reqs.map(([sp, u]) => getText(u).then(t => [sp, JSON.parse(t)])));
  const ok = {}, errs = [];
  for (const r of res) {
    if (r.status !== "fulfilled" || !r.value[1].games) { errs.push(r.reason ? String(r.reason.message || r.reason) : "bad shape"); continue; }
    const [sp, d] = r.value; ok[sp] = true;
    for (const g of d.games) {
      const key = `${sp}|${g.id}`;
      let c = anCarry.get(key);
      if (!c) { c = { odds: new Map() }; anCarry.set(key, c); }
      c.game = { id: g.id, status: g.status, start_time: g.start_time, home_team_id: g.home_team_id, away_team_id: g.away_team_id,
                 teams: (g.teams || []).map(t => ({ id: t.id, full_name: t.full_name })) };
      c.sport = sp;
      for (const o of g.odds || []) {
        if (o.type !== "game" || !AN_BOOKS[o.book_id]) continue;
        const k = `${o.book_id}`, prev = c.odds.get(k);
        if (!prev || (o.inserted || "") >= (prev.o.inserted || "")) c.odds.set(k, { o, seen: now });
        else prev.seen = now;
      }
    }
  }
  const failed = sports.filter(s => !ok[s]);
  const events = [];
  for (const [key, c] of anCarry) {
    if (!sports.includes(c.sport)) continue;
    const start = Date.parse(c.game.start_time);
    if (start < now - 26 * 3600e3) { anCarry.delete(key); continue; }
    if (AN_FINISHED.has(String(c.game.status || "").toLowerCase())) continue;
    const teams = Object.fromEntries(c.game.teams.map(t => [t.id, t.full_name]));
    const home = teams[c.game.home_team_id] || "Home", away = teams[c.game.away_team_id] || "Away";
    const books = {};
    for (const [k, { o, seen }] of c.odds) {
      if (now - seen > CARRY_MS) { c.odds.delete(k); continue; }
      const upd = o.inserted, mk = {};
      const leg = (name, point, price) => ({ name, point, price, link: "", updated: upd });
      if (o.ml_home != null && o.ml_away != null) {
        mk.h2h = [leg(away, null, o.ml_away), leg(home, null, o.ml_home)];
        if (o.draw != null) mk.h2h.push(leg("Draw", null, o.draw));
      }
      if ([o.spread_home, o.spread_away, o.spread_home_line, o.spread_away_line].every(x => x != null))
        mk.spreads = [leg(away, o.spread_away, o.spread_away_line), leg(home, o.spread_home, o.spread_home_line)];
      if ([o.total, o.over, o.under].every(x => x != null))
        mk.totals = [leg("Over", o.total, o.over), leg("Under", o.total, o.under)];
      if (Object.keys(mk).length) books[AN_BOOKS[o.book_id]] = mk;
    }
    events.push({ id: c.game.id, sport: c.sport, start, home, away, books });
  }
  return { events, failed, errors: errs.slice(0, 5), requests: reqs.length };
}

// ------------------------------------------------------------------ Pinnacle part
const pinMatchups = {};   // league -> {ts, games}
// Pinnacle game list (teams/start) for one league; parsed in its own invocation and cached 10 min
async function partPinMatchups(sp, now) {
  const lg = PIN_LEAGUES[sp];
  let mu = pinMatchups[lg];
  if (mu && now - mu.ts <= MATCHUP_TTL_MS) return mu;
  const games = {};
  for (const m of pickTopLevel(await getText(`${PIN_API}/leagues/${lg}/matchups`), ['\n    "type": "matchup"', '\n    "parentId": null'])) {
    if (m.type !== "matchup" || m.parentId || m.isLive || (m.units || "Regular") !== "Regular") continue;
    const side = {};
    for (const p of m.participants || []) side[p.alignment] = p.name;
    if (!side.home || !side.away || !m.startTime) continue;
    games[m.id] = { home: side.home, away: side.away, start: Date.parse(m.startTime) };
  }
  return (pinMatchups[lg] = { ts: now, games });
}
async function pinGames(sp, now, env) {
  const mu = pinMatchups[PIN_LEAGUES[sp]];
  if (mu && now - mu.ts <= MATCHUP_TTL_MS) return mu;
  const r = await callPart(env, `pinmu-${sp}`);
  if (!r.ok) throw new Error(r.error);
  return (pinMatchups[PIN_LEAGUES[sp]] = r.data);
}
async function partPinnacle(sports, now, env) {
  const out = {}, errors = [];
  await Promise.all(sports.map(async sp => {
    const lg = PIN_LEAGUES[sp];
    try {
      const [mu, mkText] = await Promise.all([pinGames(sp, now, env), getText(`${PIN_API}/leagues/${lg}/markets/straight`)]);
      const g = {};
      for (const id in mu.games) g[id] = Object.assign({}, mu.games[id], { h2h: null, spreads: {}, totals: {} });
      for (const k of pickTopLevel(mkText, ['\n    "period": 0,'],
                                   ['\n    "type": "moneyline"', '\n    "type": "spread"', '\n    "type": "total"'])) {
        const x = g[k.matchupId];
        if (!x || k.period !== 0 || (k.status || "open") !== "open" || !["moneyline", "spread", "total"].includes(k.type)) continue;
        const pr = {};
        for (const p of k.prices || []) if (p.designation && p.price != null) pr[p.designation] = p;
        const alt = !!k.isAlternate;
        if (k.type === "moneyline" && pr.home && pr.away && !alt) x.h2h = { home: pr.home.price, away: pr.away.price };
        else if (k.type === "spread" && pr.home && pr.away && pr.home.points != null) {
          const hc = Number(pr.home.points), cur = x.spreads[hc];
          if (!cur || (cur.alt && !alt)) x.spreads[hc] = { home: pr.home.price, away: pr.away.price, alt };
        } else if (k.type === "total" && pr.over && pr.under && pr.over.points != null) {
          const pts = Number(pr.over.points), cur = x.totals[pts];
          if (!cur || (cur.alt && !alt)) x.totals[pts] = { Over: pr.over.price, Under: pr.under.price, alt };
        }
      }
      out[sp] = g;
    } catch (e) { errors.push(`${sp}: ${e.message}`); }
  }));
  return { leagues: out, errors };
}

// ------------------------------------------------------------------ matching (same rules as odds_compare.py)
function norm(name) {
  let n = name.toLowerCase().replaceAll("&", "and").replaceAll("st.", "state").replaceAll("'", "").replaceAll("\u2019", "");
  return n.replace(/[^a-z0-9 ]+/g, " ").split(/\s+/).filter(Boolean).join(" ");
}
function sameTeam(a, b) {
  a = norm(a); b = norm(b); a = TEAM_ALIASES[a] || a; b = TEAM_ALIASES[b] || b;
  return a === b || a.startsWith(b + " ") || b.startsWith(a + " ");
}
function attachPinnacle(events, sport, pin) {
  let matched = 0;
  for (const ev of events) {
    if (ev.sport !== sport) continue;
    let best = null;
    for (const g of Object.values(pin)) {
      const dt = Math.abs(g.start - ev.start) / 1000;
      if (dt > 3 * 3600) continue;
      const hh = sameTeam(g.home, ev.home), aa = sameTeam(g.away, ev.away), ha = sameTeam(g.home, ev.away), ah = sameTeam(g.away, ev.home);
      let cand;
      if (hh && aa) cand = [3, -dt, false];
      else if (ha && ah) cand = [3, -dt, true];
      else if (dt <= 15 * 60 && (hh || aa) !== (ha || ah)) cand = [1, -dt, !!(ha || ah)];
      else continue;
      if (!best || cand[0] > best[0][0] || (cand[0] === best[0][0] && cand[1] > best[0][1])) best = [cand, g];
    }
    if (!best) continue;
    const [[, , flip], g] = best;
    const [h, a] = flip ? [ev.away, ev.home] : [ev.home, ev.away];
    const ref = {};
    if (g.h2h) ref.h2h = { null: { [h]: g.h2h.home, [a]: g.h2h.away, alt: false } };
    if (Object.keys(g.spreads).length) {
      ref.spreads = {};
      for (const [hc, v] of Object.entries(g.spreads)) ref.spreads[String(flip ? -Number(hc) : Number(hc))] = { [h]: v.home, [a]: v.away, alt: v.alt };
    }
    if (Object.keys(g.totals).length) { ref.totals = {}; for (const [p, v] of Object.entries(g.totals)) ref.totals[String(Number(p))] = v; }
    ev.sharp = { Pinnacle: ref };
    matched++;
  }
  return matched;
}

// ------------------------------------------------------------------ analysis (port of analyse())
const lineKey = (mkey, o, home) => mkey === "h2h" ? null : mkey === "totals" ? Number(o.point) : (o.name === home ? Number(o.point) : -Number(o.point));
const outcomeLabel = (mkey, name, point) => point == null ? name : mkey === "spreads" ? `${name} ${fmtSigned(Number(point))}` : `${name} ${fmtG(Number(point))}`;
const sum = a => a.reduce((x, y) => x + y, 0);

async function pushArb(arbs, ev, mkey, lk, names, best, bestHold, game, startIso, startEt, live) {
        const inv = Object.fromEntries(names.map(n => [n, 1 / toDecimal(best[n][1][best[n][0]].price)]));
        const s = sum(Object.values(inv));
        const legs = []; let staleAny = false;
        for (const n of names) {
          const [bb, quotes] = best[n], q = quotes[bb];
          const others = Object.entries(quotes).filter(([b]) => b !== bb).map(([, x]) => implied(x.price));
          const cons = others.length ? sum(others) / others.length : null;
          const dev = cons != null ? (cons - implied(q.price)) * 100 : null;
          const stale = dev != null && dev > CFG.stale_pp; staleAny ||= stale;
          legs.push({ outcome: outcomeLabel(mkey, n, q.point), book: bb, odds: fmtOdds(q.price),
                      decimal: Math.round(toDecimal(q.price) * 1e4) / 1e4, stake_usd: r2(100 * inv[n] / s),
                      other_books_avg_odds: cons ? fmtOdds(toAmerican(cons)) : "n/a", deviation_pp: dev != null ? r2(dev) : null,
                      stale, line_updated: q.updated, link: q.link || bookLink(bb, ev.sport),
                      link_type: q.link ? "event" : "sportsbook/league page" });
        }
        const lkStr = lk === null ? "None" : Number.isInteger(lk) ? (Object.is(lk, -0) ? "-0.0" : lk.toFixed(1)) : String(lk);   // python str(float)
        const sig = `${ev.id}|${mkey}|${lkStr}|` + legs.map(l => `${l.outcome}@${l.book}`).join("|");
        arbs.push({ id: (await sha1hex(sig)).slice(0, 16), price_id: (await sha1hex(sig + "|" + legs.map(l => l.odds).join("|"))).slice(0, 16),
                    sport: ev.sport.toUpperCase(), game, game_id: ev.id, start_utc: startIso, start_et: startEt, market: MKT[mkey], line: lk,
                    profit_pct: r2((1 / s - 1) * 100), best_available_hold_pct: r2(bestHold), total_stake_usd: 100, payout_usd: r2(100 / s),
                    stale_warning: staleAny ? "verify before betting" : "", live, legs });
      }

async function analyse(events, now) {
  const valueRows = [], arbs = [], bookVigs = {}, vigByMarket = {}, vigBySport = {};
  const cov = { markets: 0, sharp_markets: 0, pinnacle_markets: 0, games: 0, sharp_games: 0, sharp_rejected: 0, stale_dropped: 0 };
  for (const ev of events) {
    if (ev.start < now - CFG.max_live_hours * 3600e3) continue;
    const live = ev.start <= now, game = `${ev.away} @ ${ev.home}`, startEt = et(ev.start), startIso = isoZ(ev.start);
    cov.games++; if (ev.sharp) cov.sharp_games++;
    const markets = new Map();
    for (const [book, mks] of Object.entries(ev.books)) for (const [mkey, outs] of Object.entries(mks)) for (const o of outs) {
      const lk = lineKey(mkey, o, ev.home), key = `${mkey}|${lk}`;
      if (!markets.has(key)) markets.set(key, { mkey, lk, byBook: {} });
      (markets.get(key).byBook[book] ||= {})[o.name] = o;
    }
    for (const { mkey, lk, byBook } of markets.values()) {
      const names = [...new Set(Object.values(byBook).flatMap(l => Object.keys(l)))].sort();
      const complete = Object.fromEntries(Object.entries(byBook).filter(([, l]) => Object.keys(l).length === names.length && names.length >= 2));
      const vigs = {}, novig = {};
      for (const [b, legs] of Object.entries(complete)) {
        const tot = sum(Object.values(legs).map(l => implied(l.price)));
        vigs[b] = (tot - 1) * 100;
        for (const [n, l] of Object.entries(legs)) (novig[n] ||= {})[b] = implied(l.price) / tot;
      }
      for (const [b, v] of Object.entries(vigs)) {
        (bookVigs[b] ||= []).push(v);
        ((vigByMarket[b] ||= {})[MKT[mkey]] ||= []).push(v);
        ((vigBySport[b] ||= {})[ev.sport.toUpperCase()] ||= []).push(v);
      }
      const avgVig = Object.keys(vigs).length ? sum(Object.values(vigs)) / Object.keys(vigs).length : null;
      const best = {};
      for (const n of names) {
        const quotes = {};
        for (const [b, legs] of Object.entries(byBook)) if (legs[n]) quotes[b] = legs[n];
        let bb = null;
        for (const b of Object.keys(quotes)) if (bb === null || toDecimal(quotes[b].price) > toDecimal(quotes[bb].price)) bb = b;
        best[n] = [bb, quotes];
      }
      const bestHold = names.length >= 2 ? (sum(names.map(n => implied(best[n][1][best[n][0]].price))) - 1) * 100 : null;
      // CPU saver (same output): fair price / EV / coverage only matter when some outcome has >= min_books quotes
      const maxQuotes = Math.max(...names.map(n => Object.keys(best[n][1]).length));
      if (maxQuotes < CFG.min_books) {
        if (bestHold != null && bestHold < 0 && new Set(names.map(n => best[n][0])).size >= 2)
          await pushArb(arbs, ev, mkey, lk, names, best, bestHold, game, startIso, startEt, live);
        continue;
      }

      // fair price: weighted average of every book's own no-vig line
      const sharpFair = {}, sharpPx = {}; let altUsed = false, skipped = 0;
      for (const [sb, ref] of Object.entries(ev.sharp || {})) {
        const q = (ref[mkey] || {})[String(lk)];
        if (!q || names.length < 2 || names.some(n => q[n] == null)) continue;
        const tot = sum(names.map(n => implied(q[n])));
        const sf = Object.fromEntries(names.map(n => [n, implied(q[n]) / tot]));
        const cons = {};
        for (const n of names) if (novig[n]) cons[n] = sum(Object.values(novig[n])) / Object.keys(novig[n]).length;
        if (Object.keys(cons).length && Math.max(...Object.keys(cons).map(n => Math.abs(sf[n] - cons[n]))) > CFG.sharp_max_gap / 100) { skipped++; continue; }
        sharpFair[sb] = sf; sharpPx[sb] = q;
        if (sb === "Pinnacle") altUsed = !!q.alt;
      }
      let nvBook = {};
      for (const b of Object.keys(complete)) nvBook[b] = Object.fromEntries(names.map(n => [n, novig[n][b]]));
      Object.assign(nvBook, sharpFair);
      const w = b => WEIGHTS[b] ?? DEFAULT_WEIGHT;
      nvBook = Object.fromEntries(Object.entries(nvBook).filter(([b]) => w(b) > 0));
      const wavg = bs => {
        const tw = sum(bs.map(w));
        if (!bs.length || tw <= 0) return null;
        const raw = Object.fromEntries(names.map(n => [n, sum(bs.map(b => w(b) * nvBook[b][n])) / tw]));
        const t = sum(Object.values(raw));
        return Object.fromEntries(names.map(n => [n, raw[n] / t]));
      };
      const dropped = [];
      const nvBooks = Object.keys(nvBook);
      if (CFG.stale_guard_pp > 0 && nvBooks.length >= 3) {
        for (const b of nvBooks.filter(b => !SHARP.has(b))) {
          const rest = wavg(nvBooks.filter(x => x !== b));
          if (rest && Math.max(...names.map(n => Math.abs(nvBook[b][n] - rest[n]))) * 100 > CFG.stale_guard_pp) dropped.push(b);
        }
      }
      const used = nvBooks.filter(b => !dropped.includes(b));
      const wfair = wavg(used), pinFair = sharpFair.Pinnacle;
      const sharpsUsed = used.filter(b => SHARP.has(b)).sort();
      const nOther = used.length - sharpsUsed.length;
      let fairSrc = "";
      if (wfair) fairSrc = sharpsUsed.length
        ? `weighted (${sharpsUsed.join(" + ")}${altUsed && sharpsUsed.includes("Pinnacle") ? " alt line" : ""} + ${nOther} book${nOther !== 1 ? "s" : ""})`
        : `weighted (${nOther} book${nOther !== 1 ? "s" : ""}, no sharp)`;
      if (dropped.length) fairSrc += `, dropped stale: ${dropped.sort().join(", ")}`;
      if (names.some(n => Object.keys(best[n][1]).length >= CFG.min_books)) {
        cov.markets++; if (sharpsUsed.length) cov.sharp_markets++; if (sharpsUsed.includes("Pinnacle")) cov.pinnacle_markets++;
        cov.sharp_rejected += skipped; cov.stale_dropped += dropped.length;
      }

      for (const n of names) {
        const [bb, quotes] = best[n];
        if (Object.keys(quotes).length < CFG.min_books) continue;
        const probs = Object.fromEntries(Object.entries(quotes).map(([b, q]) => [b, implied(q.price)]));
        const avgP = sum(Object.values(probs)) / Object.keys(probs).length;
        const bp = quotes[bb].price;
        const ties = Object.keys(quotes).filter(b => quotes[b].price === bp).sort();
        const others = Object.entries(probs).filter(([b]) => !ties.includes(b)).map(([, p]) => p);
        const stale = others.length > 0 && (sum(others) / others.length - probs[bb]) * 100 > CFG.stale_pp;
        const nv = novig[n] || {};
        const consP = Object.keys(nv).length ? sum(Object.values(nv)) / Object.keys(nv).length : null;
        const fairP = wfair ? wfair[n] : consP, pinP = pinFair ? pinFair[n] : null, bdec = toDecimal(bp);
        valueRows.push({
          sport: ev.sport.toUpperCase(), game, start_et: startEt, start_iso: startIso, start_utc: startIso,
          market: MKT[mkey], outcome: outcomeLabel(mkey, n, quotes[bb].point),
          n_books: Object.keys(quotes).length, best_book: ties.join(" / "), best_odds: fmtOdds(bp), best_odds_num: bp,
          avg_odds: fmtOdds(toAmerican(avgP)), edge_pp: r2((avgP - probs[bb]) * 100),
          novig_fair_odds: fairP ? fmtOdds(toAmerican(fairP)) : "n/a",
          ev_vs_fair_pct: fairP ? r2((bdec * fairP - 1) * 100) : null,
          fair_source: fairP ? fairSrc : "", confidence: sharpsUsed.includes("Pinnacle") ? "high (sharp)" : "normal",
          ev_vs_pinnacle_pct: pinP ? r2((bdec * pinP - 1) * 100) : null,
          pinnacle_fair_odds: pinP ? fmtOdds(toAmerican(pinP)) : "n/a",
          sharp_odds: Object.keys(sharpPx).sort().map(b => `${b} ${fmtOdds(sharpPx[b][n])}`).join("; "),
          consensus_fair_odds: consP ? fmtOdds(toAmerican(consP)) : "n/a",
          ev_vs_consensus_pct: consP ? r2((bdec * consP - 1) * 100) : null,
          best_book_vig_pct: vigs[bb] != null ? r2(vigs[bb]) : null,
          market_avg_vig_pct: avgVig != null ? r2(avgVig) : null,
          stale_warning: stale ? "verify before betting" : "", live,
          best_book_link: quotes[bb].link || bookLink(bb, ev.sport),
          all_books: Object.entries(quotes).sort((x, y) => toDecimal(y[1].price) - toDecimal(x[1].price)).map(([b, q]) => `${b} ${fmtOdds(q.price)}`).join("; "),
        });
      }

      if (bestHold != null && bestHold < 0 && new Set(names.map(n => best[n][0])).size >= 2)
        await pushArb(arbs, ev, mkey, lk, names, best, bestHold, game, startIso, startEt, live);
    }
  }
  arbs.sort((a, b) => b.profit_pct - a.profit_pct);
  const ev = valueRows.filter(r => r.ev_vs_fair_pct != null && r.ev_vs_fair_pct > 0).sort((a, b) => b.ev_vs_fair_pct - a.ev_vs_fair_pct);
  const avg = v => v && v.length ? r2(sum(v) / v.length) : null;
  const vig = Object.entries(bookVigs).sort((a, b) => sum(a[1]) / a[1].length - sum(b[1]) / b[1].length).map(([b, v]) => ({
    book: b, avg_vig_pct: avg(v), markets: v.length,
    moneyline: avg(vigByMarket[b].moneyline), spread: avg(vigByMarket[b].spread), total: avg(vigByMarket[b].total),
    by_sport: Object.fromEntries(Object.entries(vigBySport[b]).map(([s, x]) => [s, avg(x)])) }));
  const agg = v => [sum(v), v.length];
  const vigRaw = Object.fromEntries(Object.entries(bookVigs).map(([b, v]) => [b, { all: agg(v),
    m: Object.fromEntries(Object.entries(vigByMarket[b]).map(([k, x]) => [k, agg(x)])),
    s: Object.fromEntries(Object.entries(vigBySport[b]).map(([k, x]) => [k, agg(x)])) }]));
  return { arbs, ev: ev.slice(0, 500), vig, vigRaw, cov, lines: valueRows.length };
}

// ------------------------------------------------------------------ orchestration
// Each "part" runs as its own invocation (service binding to this Worker) so every one stays small in CPU:
//   sport-<g>: Action Network fetch + attach Pinnacle + analyse for a group of sports
//   pin-<g>:   Pinnacle full-game markets for that group;  pinmu-<sport>: Pinnacle game list (cached 10 min)
const GROUPS = { nfl: ["nfl"], ncaaf: ["ncaaf"], daily: DAILY };
const PARTS = {};
for (const [g, sp] of Object.entries(GROUPS)) { PARTS[`sport-${g}`] = ["sport", sp, g]; PARTS[`pin-${g}`] = ["pin", sp, g]; }
for (const sp of Object.keys(PIN_LEAGUES)) PARTS[`pinmu-${sp}`] = ["pinmu", [sp]];

async function partSport(group, sports, now, env) {
  const [an, pin] = await Promise.all([partActionNetwork(sports, now), callPart(env, `pin-${group}`)]);
  const warnings = [], pinMatch = [];
  if (an.failed.length) warnings.push(`Action Network failed for ${an.failed.join(",")}: ${an.errors.join("; ")}`);
  if (!pin.ok) warnings.push(`Pinnacle ${group}: ${pin.error} - using retail-only fair price`);
  else {
    for (const e of pin.data.errors) warnings.push(`Pinnacle ${e} - using retail-only fair price`);
    for (const [sp, games] of Object.entries(pin.data.leagues)) {
      const m = attachPinnacle(an.events, sp, games);
      pinMatch.push(`${sp} ${m}/${an.events.filter(e => e.sport === sp).length}`);
    }
  }
  const out = await analyse(an.events, Date.now());
  return { arbs: out.arbs, ev: out.ev, vigRaw: out.vigRaw, cov: out.cov, lines: out.lines, games: an.events.length,
           failed: an.failed, warnings, pinMatch };
}

async function runPart(name, now, env) {
  const [kind, sports, group] = PARTS[name];
  if (kind === "sport") return partSport(group, sports, now, env);
  if (kind === "pin") return partPinnacle(sports, now, env);
  return partPinMatchups(sports[0], now);
}

async function callPart(env, name) {
  const t0 = Date.now();
  try {
    let data;
    if (env.SELF) {
      const r = await env.SELF.fetch(`https://internal/_part/${name}`, { headers: { "x-part-key": env.PART_KEY || "" } });
      if (!r.ok) throw new Error(`part ${name} HTTP ${r.status}`);
      data = await r.json();
    } else data = await runPart(name, Date.now(), env);   // no binding (local tests): run inline
    return { ok: true, data, ms: Date.now() - t0 };
  } catch (e) { return { ok: false, error: String(e.message || e), ms: Date.now() - t0 }; }
}

async function build(env) {
  const t0 = Date.now();
  const names = Object.keys(GROUPS).map(g => `sport-${g}`);
  const res = await Promise.all(names.map(n => callPart(env, n)));
  const now = Date.now();
  const arbs = [], ev = [], vigRaw = {}, warnings = [], pinMatch = [];
  const cov = { markets: 0, sharp_markets: 0, pinnacle_markets: 0, games: 0, sharp_games: 0, sharp_rejected: 0, stale_dropped: 0 };
  let lines = 0, games = 0, anOk = 0;
  res.forEach((r, i) => {
    if (!r.ok) { warnings.push(`${names[i]}: ${r.error}`); return; }
    const d = r.data;
    if (d.failed.length < GROUPS[names[i].slice(6)].length) anOk++;
    arbs.push(...d.arbs); ev.push(...d.ev); warnings.push(...d.warnings); pinMatch.push(...d.pinMatch);
    lines += d.lines; games += d.games;
    for (const k in cov) cov[k] += d.cov[k] || 0;
    for (const [b, v] of Object.entries(d.vigRaw)) {
      const t = vigRaw[b] ||= { all: [0, 0], m: {}, s: {} };
      t.all[0] += v.all[0]; t.all[1] += v.all[1];
      for (const k of ["m", "s"]) for (const [x, [sm, n]] of Object.entries(v[k])) { const y = t[k][x] ||= [0, 0]; y[0] += sm; y[1] += n; }
    }
  });
  if (!anOk) throw new Error("Action Network unavailable: " + warnings.join(" | "));
  arbs.sort((a, b) => b.profit_pct - a.profit_pct);
  ev.sort((a, b) => b.ev_vs_fair_pct - a.ev_vs_fair_pct);
  const avg = x => x && x[1] ? r2(x[0] / x[1]) : null;
  const vig = Object.entries(vigRaw).sort((a, b) => a[1].all[0] / a[1].all[1] - b[1].all[0] / b[1].all[1]).map(([b, v]) => ({
    book: b, avg_vig_pct: avg(v.all), markets: v.all[1], moneyline: avg(v.m.moneyline), spread: avg(v.m.spread), total: avg(v.m.total),
    by_sport: Object.fromEntries(Object.entries(v.s).map(([k, x]) => [k, avg(x)])) }));
  const generated = new Date(now).toISOString();
  return {
    generated_at: generated,
    meta: { generated_utc: generated, generated_et: et(now), source: "Cloudflare Worker (live: Action Network + Pinnacle)",
            sharp_source: "Pinnacle public guest API (reference only)", pinnacle_match: pinMatch, weights: WEIGHTS,
            stale_guard_pp: CFG.stale_guard_pp, counts: { arbs: arbs.length, ev: ev.length, books: vig.length, games, lines },
            coverage: cov, warnings, cache_ttl_s: TTL_MS / 1000,
            timings_ms: Object.fromEntries(res.map((r, i) => [names[i], r.ms])), build_ms: Date.now() - t0 },
    arbs, ev: ev.slice(0, 500), vig,
  };
}

let cache = null, inflight = null;   // per-isolate cache: {ts, body}
async function getPayload(env) {
  if (cache && Date.now() - cache.ts < TTL_MS) return cache;
  if (!inflight) inflight = build(env).then(p => (cache = { ts: Date.now(), body: JSON.stringify(p) }))
                                     .finally(() => { inflight = null; });
  try { return await inflight; }
  catch (e) { if (cache && Date.now() - cache.ts < 5 * 60e3) return cache; throw e; }
}

function corsHeaders(req) {
  const o = req.headers.get("Origin");
  const h = { "Vary": "Origin" };
  if (o && (ORIGINS.includes(o) || LOCAL_ORIGIN.test(o))) { h["Access-Control-Allow-Origin"] = o; h["Access-Control-Allow-Methods"] = "GET, OPTIONS"; h["Access-Control-Max-Age"] = "86400"; }
  return h;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname.startsWith("/_part/")) {     // internal only (service binding + shared key)
      if (!env.PART_KEY || req.headers.get("x-part-key") !== env.PART_KEY) return new Response("not found", { status: 404 });
      const name = url.pathname.slice(7);
      if (!PARTS[name]) return new Response("not found", { status: 404 });
      try { return Response.json(await runPart(name, Date.now(), env)); }
      catch (e) { return new Response(String(e.message || e), { status: 502 }); }
    }
    const cors = corsHeaders(req);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (req.method !== "GET") return new Response("method not allowed", { status: 405, headers: cors });
    if (url.pathname === "/" ) return new Response("odds-tracker-api: GET /v1/odds\n", { headers: { ...cors, "content-type": "text/plain" } });
    if (url.pathname !== "/v1/odds") return new Response("not found", { status: 404, headers: cors });
    try {
      const c = await getPayload(env);
      const age = Math.floor((Date.now() - c.ts) / 1000);
      return new Response(c.body, { headers: { ...cors, "content-type": "application/json; charset=utf-8",
        "cache-control": `public, max-age=${Math.max(0, 27 - age)}`, "x-data-age": String(age) } });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e.message || e) }), { status: 503, headers: { ...cors, "content-type": "application/json" } });
    }
  },
};
export const __test = { analyse, attachPinnacle, partActionNetwork };   // used by tests only
