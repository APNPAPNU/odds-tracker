"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl = u => /^https:\/\//i.test(u || "") ? u : "";
const REFRESH_MS = 60000;
let D = {meta: null, arbs: [], ev: [], vig: []};
let stakes = JSON.parse(localStorage.getItem("ot_stakes") || "{}");
let pendingArbRender = false;

// ---------- tabs
function showTab() {
  const t = (location.hash || "#arb").slice(1);
  document.querySelectorAll(".tab").forEach(s => s.classList.toggle("on", s.id === t));
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.tab === t));
}
window.addEventListener("hashchange", showTab);

// ---------- data
let lastLoad = 0, timer = null;
async function load() {
  const bust = "?t=" + Date.now();
  lastLoad = Date.now();
  try {
    const [meta, arbs, ev, vig] = await Promise.all(["meta", "arbs", "ev", "vig"].map(n =>
      fetch(`data/${n}.json${bust}`, {cache: "no-store"}).then(r => { if (!r.ok) throw new Error(n + " " + r.status); return r.json(); })));
    D = {meta, arbs, ev, vig};
    renderAll();
    return true;
  } catch (e) {
    $("#upd").textContent = "Couldn't load data (" + e.message + "). Retrying in 1 min.";
    $("#upd").classList.add("old");
    return false;
  }
}
function ageText() {
  if (!D.meta) return "";
  const m = Math.round((Date.now() - Date.parse(D.meta.generated_utc)) / 60000);
  return m < 1 ? "data just published" : `data ${m} min old`;
}

// ---------- refresh button + auto-refresh (paused while the tab is hidden)
const btn = $("#refresh");
btn.addEventListener("click", async () => {
  if (btn.disabled) return;
  btn.disabled = true; btn.className = "spin"; btn.querySelector(".lbl").textContent = "Refreshing";
  const ok = await load();
  btn.className = ok ? "ok" : "err";
  btn.querySelector(".lbl").textContent = ok ? `Updated \u00b7 ${ageText()}` : "Failed - retry";
  startTimer();                       // next auto-refresh a full minute from now
  setTimeout(() => { btn.className = ""; btn.querySelector(".lbl").textContent = "Refresh"; btn.disabled = false; }, 2500);
});
function startTimer() { clearInterval(timer); timer = setInterval(load, REFRESH_MS); }
function stopTimer() { clearInterval(timer); timer = null; }
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { stopTimer(); return; }
  if (Date.now() - lastLoad >= REFRESH_MS) load(); else renderUpdated();
  startTimer();
});
function renderAll() { renderUpdated(); renderArbs(); renderEV(); renderVig(); renderAbout(); }

function renderUpdated() {
  const m = D.meta; if (!m) return;
  const ageMin = Math.round((Date.now() - Date.parse(m.generated_utc)) / 60000);
  const el = $("#upd");
  el.textContent = `Updated ${m.generated_et}` + (ageMin >= 1 ? ` (${ageMin} min ago)` : " (just now)");
  el.classList.toggle("old", ageMin > 15);
  $("#n-arb").textContent = D.arbs.length; $("#n-ev").textContent = D.ev.length;
}
setInterval(renderUpdated, 30000);

// ---------- arbitrage
function money(x) { return "$" + x.toFixed(2); }
function calc(a, total) {
  const inv = a.legs.map(l => 1 / l.decimal), s = inv.reduce((x, y) => x + y, 0);
  return {stakes: inv.map(i => total * i / s), payout: total / s, profit: total / s - total};
}
function renderArbs() {
  const box = $("#arb-list");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") { pendingArbRender = true; return; }
  pendingArbRender = false;
  if (!D.arbs.length) { box.innerHTML = `<div class="card empty">No arbitrage opportunities right now. Checked ${esc(D.meta?.generated_et || "")}.</div>`; return; }
  box.innerHTML = D.arbs.map(a => {
    const total = stakes[a.id] ?? 100, c = calc(a, total);
    const legs = a.legs.map((l, i) => {
      const url = safeUrl(l.link);
      return `<div class="leg">
        <div class="o">${esc(l.outcome)}</div><div class="px">${esc(l.odds)}</div>
        <div class="bk">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(l.book)} &#8599;</a>` : esc(l.book)}
          <span class="sm">${l.link_type === "event" ? "" : "(sportsbook page)"}</span></div>
        <div class="st" data-leg="${i}">${money(c.stakes[i])}</div>
        <div class="sm">Other books avg ${esc(l.other_books_avg_odds)}</div>
        <div class="sm" style="text-align:right">${l.stale ? '<span class="pill red">verify before betting</span>' : '<span class="pill grey">ok</span>'}</div>
      </div>`;}).join("");
    return `<div class="card arb" data-id="${esc(a.id)}">
      <h3>${esc(a.game)}</h3>
      <div class="sub"><span class="pill grey">${esc(a.sport)}</span> ${esc(a.start_et)} &middot; ${esc(a.market)}
        &middot; <span class="pill green">+${a.profit_pct.toFixed(2)}%</span>
        ${a.stale_warning ? '<span class="pill red">&#9888; verify before betting</span>' : ""}</div>
      <div class="calc"><label for="s-${esc(a.id)}">Total stake $</label>
        <input id="s-${esc(a.id)}" type="number" min="0" step="10" inputmode="decimal" value="${total}">
        <span>&rarr; payout <span class="out pay">${money(c.payout)}</span> (profit <span class="out prof">${money(c.profit)}</span>)</span></div>
      <div class="legs">${legs}</div></div>`;
  }).join("");
  box.querySelectorAll(".arb input").forEach(inp => {
    inp.addEventListener("input", () => {
      const card = inp.closest(".arb"), a = D.arbs.find(x => x.id === card.dataset.id); if (!a) return;
      const total = Math.max(0, parseFloat(inp.value) || 0), c = calc(a, total);
      stakes[a.id] = total; localStorage.setItem("ot_stakes", JSON.stringify(stakes));
      card.querySelectorAll("[data-leg]").forEach(el => el.textContent = money(c.stakes[+el.dataset.leg]));
      card.querySelector(".pay").textContent = money(c.payout); card.querySelector(".prof").textContent = money(c.profit);
    });
    inp.addEventListener("blur", () => setTimeout(() => { if (pendingArbRender) renderArbs(); }, 50));
  });
}

// ---------- positive EV
const F = JSON.parse(localStorage.getItem("ot_filters") || "{}");
["sport", "ev", "odds", "mkt"].forEach(k => { if (F[k] != null) $("#f-" + k).value = F[k]; });
["high", "stale"].forEach(k => { if (F[k]) $("#f-" + k).checked = true; });
document.querySelectorAll(".filters input,.filters select").forEach(el => el.addEventListener("input", () => {
  ["sport", "ev", "odds", "mkt"].forEach(k => F[k] = $("#f-" + k).value);
  ["high", "stale"].forEach(k => F[k] = $("#f-" + k).checked);
  localStorage.setItem("ot_filters", JSON.stringify(F)); renderEV();
}));
function renderEV() {
  const sel = $("#f-sport"), cur = sel.value || F.sport || "", sports = [...new Set(D.ev.map(r => r.sport))].sort();
  sel.innerHTML = `<option value="">All</option>` + sports.map(s => `<option${s === cur ? " selected" : ""}>${esc(s)}</option>`).join("");
  if (cur && !sports.includes(cur)) { const o = document.createElement("option"); o.textContent = cur; o.selected = true; sel.appendChild(o); }
  const minEv = parseFloat($("#f-ev").value), maxOdds = parseFloat($("#f-odds").value), mkt = $("#f-mkt").value;
  const rows = D.ev.filter(r => (!sel.value || r.sport === sel.value) && (isNaN(minEv) || r.ev_vs_fair_pct >= minEv)
    && (isNaN(maxOdds) || r.best_odds_num == null || r.best_odds_num <= maxOdds) && (!mkt || r.market === mkt)
    && (!$("#f-high").checked || String(r.confidence).startsWith("high")) && (!$("#f-stale").checked || !r.stale_warning));
  $("#ev-meta").textContent = `${rows.length} of ${D.ev.length} lines beat the weighted no-vig fair price (Pinnacle x4, other sharp books x2, retail x1).`;
  $("#ev-list").innerHTML = rows.length ? rows.map(r => {
    const url = safeUrl(r.best_book_link), high = String(r.confidence).startsWith("high");
    return `<div class="card ev">
      <div><div class="g">${esc(r.sport)} &middot; ${esc(r.game)} &middot; ${esc(r.start_et)}</div>
        <div class="pick">${esc(r.outcome)} <span class="k">${esc(r.market)}</span></div></div>
      <div class="evp">+${r.ev_vs_fair_pct.toFixed(2)}%</div>
      <div class="row">
        <span><span class="k">Best</span> <b>${esc(r.best_odds)}</b> ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(r.best_book)} &#8599;</a>` : esc(r.best_book)}</span>
        <span><span class="k">Fair</span> <b>${esc(r.novig_fair_odds)}</b></span>
        <span><span class="k">Pinnacle</span> <b>${esc((r.sharp_odds || "-").replace("Pinnacle ", ""))}</b></span>
        <span><span class="k">Avg</span> ${esc(r.avg_odds)}</span>
        <span><span class="k">EV vs Pinnacle only</span> ${r.ev_vs_pinnacle_pct == null ? "-" : r.ev_vs_pinnacle_pct.toFixed(2) + "%"}</span>
      </div>
      <div class="row">${high ? '<span class="pill green">&#9733; high confidence (Pinnacle)</span>' : '<span class="pill grey">normal confidence</span>'}
        ${r.stale_warning ? '<span class="pill red">verify before betting</span>' : ""}
        <span class="k" style="font-size:12px">${esc(r.fair_source)} &middot; ${r.n_books} books</span></div>
      <div class="row k" style="font-size:12px">${esc(r.all_books)}</div>
    </div>`;}).join("") : `<div class="card empty">No lines match these filters.</div>`;
}

// ---------- vig
let vigSort = {k: "avg_vig_pct", dir: 1};
function renderVig() {
  const sports = [...new Set(D.vig.flatMap(v => Object.keys(v.by_sport || {})))].sort();
  const cols = [["book", "Book"], ["avg_vig_pct", "Avg vig"], ["moneyline", "Moneyline"], ["spread", "Spread"], ["total", "Total"],
    ...sports.map(s => ["s:" + s, s]), ["markets", "Markets"]];
  const val = (v, k) => k.startsWith("s:") ? v.by_sport?.[k.slice(2)] : v[k];
  const rows = [...D.vig].sort((a, b) => { const x = val(a, vigSort.k), y = val(b, vigSort.k);
    return (x == null) - (y == null) || (x < y ? -1 : x > y ? 1 : 0) * vigSort.dir; });
  const best = rows.length ? Math.min(...rows.map(r => r.avg_vig_pct)) : null;
  $("#vig-table").innerHTML = `<thead><tr>${cols.map(([k, h]) => `<th data-k="${k}" class="${k === "book" ? "" : "n"}">${esc(h)}${vigSort.k === k ? (vigSort.dir > 0 ? " &#9650;" : " &#9660;") : ""}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr class="${r.avg_vig_pct === best ? "best" : ""}">${cols.map(([k]) => {
      const v = val(r, k); return k === "book" ? `<td><b>${esc(v)}</b></td>` : `<td class="n">${v == null ? "-" : k === "markets" ? v : v.toFixed(2) + "%"}</td>`;
    }).join("")}</tr>`).join("") || `<tr><td class="empty" colspan="9">No data</td></tr>`}</tbody>`;
  $("#vig-table").querySelectorAll("th").forEach(th => th.onclick = () => {
    vigSort = {k: th.dataset.k, dir: vigSort.k === th.dataset.k ? -vigSort.dir : 1}; renderVig(); });
}

// ---------- about
function renderAbout() {
  const m = D.meta || {}, w = m.weights || {}, cov = m.coverage || {};
  $("#about-body").innerHTML = `
    <h3>Where the odds come from</h3>
    <p>Bettable books (DraftKings, FanDuel, BetMGM, Caesars, BetRivers, bet365) come from the public Action Network odds feed.
    Pinnacle comes from its public guest API and is used <b>only as a reference</b>: it never appears as a best book or an arbitrage leg.</p>
    <h3>Fair price</h3>
    <p>Each book's line is devigged on its own (multiplicative). Those no-vig probabilities are then averaged with weights across every
    book quoting that exact market and number, and renormalised. Weights: ${esc(Object.entries(w).map(([b, x]) => b + " " + x).join(", "))}.
    A retail line more than ${esc(m.stale_guard_pp)} points away from the others is dropped from the fair price.
    EV % = best price &times; fair probability &minus; 1.</p>
    <h3>This update</h3>
    <p>${esc(m.generated_et || "")}. Games matched to Pinnacle: ${esc((m.pinnacle_match || []).join(", ") || "none")}.
    Markets with a Pinnacle line: ${cov.pinnacle_markets ?? "-"} of ${cov.markets ?? "-"}.</p>
    <h3>Caveats</h3>
    <p>Feeds can lag the books by minutes, and the page refreshes its data every minute. Lines flagged <span class="pill red">verify before betting</span>
    differ a lot from the other books. Limits, geography and promo terms can stop a bet, so always confirm before placing it.</p>`;
}

showTab(); load(); if (!document.hidden) startTimer();
