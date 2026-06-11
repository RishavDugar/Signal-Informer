"use strict";

// ── helpers ──────────────────────────────────────────────────────────────────
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}
function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = msg; t.className = `toast ${kind}`;
  setTimeout(() => t.classList.add("hidden"), 2600);
}
const pct = (v, dp = 1) => (v == null || isNaN(v)) ? "—" :
  `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${(v * 100).toFixed(dp)}%</span>`;
const rawpct = (v, dp = 1) => (v == null || isNaN(v)) ? "—" :
  `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${Number(v).toFixed(dp)}%</span>`;
const num = (v) => (v == null || v === "") ? "—" : v;

// ── view routing ─────────────────────────────────────────────────────────────
const VIEWS = {
  dashboard: { title: "Dashboard", load: loadDashboard },
  run: { title: "Run jobs", load: loadRun },
  signals: { title: "Signals", load: loadSignals },
  news: { title: "News & Scout picks", load: loadNews },
  setups: { title: "Setups", load: loadSetups },
  hft: { title: "HFT / Intraday", load: loadHft },
  stocks: { title: "Stocks", load: loadStocks },
  config: { title: "Configuration", load: loadConfig },
  logs: { title: "Logs", load: loadLogs },
};
let current = "dashboard";

function show(view) {
  current = view;
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach(v => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
  $("#view-title").textContent = VIEWS[view].title;
  VIEWS[view].load();
}

$$(".nav-item").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));
$("#refresh-btn").addEventListener("click", () => VIEWS[current].load());

setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString(); }, 1000);

// ── status pills (poll) ──────────────────────────────────────────────────────
async function refreshPills() {
  try {
    const s = await api("/api/status");
    const db = $("#db-pill"); db.className = "pill " + (s.db_ok ? "ok" : "bad");
    db.textContent = s.db_ok ? `db ok · ${s.db_size_mb} MB` : "db error";
    const o = $("#ollama-pill"); o.className = "pill " + (s.ollama.up ? "ok" : "bad");
    o.textContent = s.ollama.up ? `ollama · ${(s.ollama.models || []).length} model(s)` : "ollama down";
    const w = $("#whatsapp-pill"); const wa = s.whatsapp || {};
    if (wa.backend === "bridge") {
      w.className = "pill " + (wa.ready ? "ok" : "bad");
      w.textContent = wa.ready ? "whatsapp ready" : `whatsapp ${esc(wa.state || "down")}`;
      w.title = wa.ready ? "Headless bridge logged in"
        : (wa.state === "qr" ? "Bridge needs a one-time QR scan — run `node bridge.js`"
           : "Bridge not ready — see logs / run `node bridge.js`");
    } else {
      w.className = "pill"; w.textContent = `whatsapp · ${esc(wa.backend || "?")}`;
      w.title = "Legacy pywhatkit backend (only works while unlocked)";
    }
  } catch (e) { /* ignore */ }
}

// ── Dashboard ────────────────────────────────────────────────────────────────
async function loadDashboard() {
  const v = $("#view-dashboard");
  v.innerHTML = `<div class="loading">Loading status…</div>`;
  const [s, oc] = await Promise.all([api("/api/status"), api("/api/outcomes").catch(() => null)]);
  const c = s.counts || {};
  const ing = s.last_ingestion;

  const stat = (k, val, sub) => `<div class="card stat"><div class="k">${k}</div><div class="v">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;

  // Pick-performance scorecard (realised vs expected, net of costs)
  let scHtml = "";
  if (oc && oc.n != null) {
    if (!oc.n) {
      scHtml = `<div class="card" style="margin-top:16px"><h3>Pick performance (last ${oc.days || 30}d)</h3>
        <div class="empty">No closed picks yet${oc.pending ? ` · ${oc.pending} still open` : ""}. Results appear as picks reach their horizon.</div></div>`;
    } else {
      scHtml = `<div style="margin-top:16px"><h3 class="job-group-title">Pick performance — last ${oc.days}d (net of costs)</h3>
        <div class="grid cards">
          ${stat("Win rate", (oc.win_rate * 100).toFixed(0) + "%", `${oc.n} closed · ${oc.pending} open`)}
          ${stat("Avg realised", pct(oc.avg_realized, 2), `expected ${pct(oc.avg_expected, 2)}`)}
          ${stat("Best", pct(oc.best.ret, 1), esc(oc.best.symbol))}
          ${stat("Worst", pct(oc.worst.ret, 1), esc(oc.worst.symbol))}
        </div></div>`;
    }
  }

  v.innerHTML = `
    <div class="grid cards">
      ${stat("Stocks", num(c.stocks), `${num(c.ohlcv)} OHLCV rows`)}
      ${stat("Signals", num(c.setup_signals), `latest ${s.last_signal_date || "—"}`)}
      ${stat("News picks", num(c.news_recommendations), `scouts ${num(c.scout_recommendations)}`)}
      ${stat("Setups loaded", num(s.setups_loaded), `min avg ${(s.config.min_avg_return * 100).toFixed(2)}%`)}
      ${stat("Last OHLCV", s.last_ohlcv_date || "—", `db ${s.db_size_mb} MB`)}
      ${stat("Ingestion runs", num(c.ingestion_runs), ing ? `last: ${ing.status}` : "none yet")}
    </div>

    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h3>Calibration data</h3>
        <table>
          <tbody>
            <tr><td>Backtester weights</td><td class="num">${s.weights.exists ? `${s.weights.setups} setups` : "<span class='neg'>missing</span>"}</td></tr>
            <tr><td class="faint">generated</td><td class="num faint mono">${fmtTime(s.weights.generated_at)}</td></tr>
            <tr><td>Optimal params</td><td class="num">${s.params.exists ? `${s.params.setups} setups` : "<span class='neg'>missing</span>"}</td></tr>
            <tr><td class="faint">generated</td><td class="num faint mono">${fmtTime(s.params.generated_at)}</td></tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3>Last ingestion run</h3>
        ${ing ? `<table><tbody>
          <tr><td>Status</td><td class="num"><span class="badge ${ing.status === 'SUCCESS' ? 'green' : (ing.status === 'FAILED' ? 'red' : 'amber')}">${ing.status}</span></td></tr>
          <tr><td>Total / OK / Failed</td><td class="num mono">${ing.total_stocks} / ${ing.successful} / ${ing.failed}</td></tr>
          <tr><td class="faint">run at (UTC)</td><td class="num faint mono">${esc(ing.run_at || "")}</td></tr>
        </tbody></table>` : `<div class="empty">No ingestion runs yet</div>`}
      </div>
    </div>

    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h3>Schedule &amp; services</h3>
        <table><tbody>
          <tr><td>News pipeline</td><td class="num mono">${s.config.news_schedule} IST</td></tr>
          <tr><td>Technical pipeline</td><td class="num mono">${s.config.schedule} IST</td></tr>
          <tr><td>WhatsApp sender</td><td class="num">${whatsappBadge(s.whatsapp)}</td></tr>
          <tr><td>Ollama</td><td class="num">${s.ollama.up ? `<span class="badge green">up</span> ${esc((s.ollama.models || [])[0] || "")}` : "<span class='badge red'>down</span>"}</td></tr>
          <tr><td>Ollama model (cfg)</td><td class="num mono">${esc(s.config.ollama_model)}</td></tr>
        </tbody></table>
      </div>
      <div class="card">
        <h3>Quick actions</h3>
        <div class="job-flags">
          <button class="btn primary" onclick="runJob('pipeline')">Run technical pipeline</button>
          <button class="btn" onclick="runJob('news')">Run news + AI picks</button>
          <button class="btn" onclick="runJob('backup')">Backup DB</button>
          <button class="btn" onclick="runJob('integrity')">Integrity check</button>
        </div>
        <div class="muted" style="margin-top:14px;font-size:12.5px">
          WhatsApp: ${esc(s.config.whatsapp_phone)}
          ${(s.config.whatsapp_phones || []).length > 1 ? `· ${s.config.whatsapp_phones.length} recipients` : ""}
          · via ${esc((s.whatsapp || {}).backend || "?")}
        </div>
        ${whatsappHint(s.whatsapp)}
      </div>
    </div>
    ${scHtml}`;
}
const fmtTime = (t) => t ? esc(t).replace("T", " ").slice(0, 19) : "—";

// WhatsApp send-backend badge + actionable hint when the headless bridge needs attention.
function whatsappBadge(wa) {
  wa = wa || {};
  if (wa.backend !== "bridge")
    return `<span class="badge amber">pywhatkit</span> <span class="faint">unlocked-only</span>`;
  if (wa.ready) return `<span class="badge green">bridge ready</span>`;
  return `<span class="badge red">bridge ${esc(wa.state || "down")}</span>`;
}
function whatsappHint(wa) {
  wa = wa || {};
  if (wa.backend !== "bridge" || wa.ready) return "";
  const msg = wa.state === "qr"
    ? "WhatsApp bridge needs a one-time QR scan — run <span class='mono'>node bridge.js</span> in notifications/whatsapp_bridge and scan with WhatsApp &gt; Linked Devices."
    : "WhatsApp bridge is not ready — alerts may not send. Check Logs, or run <span class='mono'>node bridge.js</span> once to (re)link.";
  return `<div class="muted" style="margin-top:8px;font-size:12px;color:#ff8a8a">⚠ ${msg}</div>`;
}

// ── Run jobs ─────────────────────────────────────────────────────────────────
async function loadRun() {
  const v = $("#view-run");
  v.innerHTML = `<div class="loading">Loading jobs…</div>`;
  const [catalog, running] = await Promise.all([api("/api/jobs/catalog"), api("/api/jobs")]);

  const groups = {};
  catalog.forEach(j => { (groups[j.group] = groups[j.group] || []).push(j); });

  let html = "";
  for (const [group, jobs] of Object.entries(groups)) {
    html += `<div class="job-group-title">${esc(group)}</div><div class="grid two">`;
    for (const j of jobs) {
      const flags = j.flags.map(f =>
        `<label class="chk"><input type="checkbox" data-flag="${f.id}"> ${esc(f.label)}</label>`).join("");
      const extra = j.extra_placeholder
        ? `<input class="field extra-args" type="text" placeholder="${esc(j.extra_placeholder)}" style="margin-top:8px;width:100%">`
        : "";
      html += `<div class="card job-card" data-jobid="${j.id}">
        <div class="job-top">
          <div>
            <div class="job-name">${esc(j.label)}</div>
            <div class="job-desc">${esc(j.desc)}</div>
          </div>
          <button class="btn ${j.danger ? "danger" : "primary"} sm" onclick="launchFromCard('${j.id}', this, ${j.danger})">Run</button>
        </div>
        ${flags ? `<div class="job-flags">${flags}</div>` : ""}
        ${extra}
      </div>`;
    }
    html += `</div>`;
  }

  html += `<div class="job-group-title">Recent jobs</div><div class="card scroll-x">
    <table><thead><tr><th>#</th><th>Job</th><th>Status</th><th>Started</th><th>Ended</th><th>Code</th><th></th></tr></thead>
    <tbody id="job-history"></tbody></table></div>`;

  v.innerHTML = html;
  renderHistory(running);
}

function renderHistory(list) {
  const tb = $("#job-history");
  if (!tb) return;
  if (!list.length) { tb.innerHTML = `<tr><td colspan="7" class="empty">No jobs run this session</td></tr>`; return; }
  tb.innerHTML = list.map(j => `<tr>
    <td class="mono">${j.id}</td>
    <td>${esc(j.label)}</td>
    <td><span class="badge ${statusClass(j.status)}">${j.status}</span></td>
    <td class="mono faint">${j.started_at}</td>
    <td class="mono faint">${j.ended_at || "—"}</td>
    <td class="mono num">${j.returncode == null ? "—" : j.returncode}</td>
    <td><button class="btn ghost sm" onclick="openConsole(${j.id})">view</button></td>
  </tr>`).join("");
}
const statusClass = (s) => s === "done" ? "green" : (s === "failed" ? "red" : (s === "stopped" ? "amber" : "running"));

window.launchFromCard = function (id, btn, danger) {
  if (danger && !confirm("This is a destructive / heavy action. Continue?")) return;
  const card = btn.closest(".job-card");
  const flags = $$('input[data-flag]', card).filter(i => i.checked).map(i => i.dataset.flag);
  const extraInput = $(".extra-args", card);
  const extra = extraInput && extraInput.value.trim() ? extraInput.value.trim().split(/\s+/) : [];
  runJob(id, flags, extra);
};

window.runJob = async function (id, flags = [], extra = []) {
  try {
    const r = await api("/api/jobs/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, flags, extra }),
    });
    toast(`Started: ${r.label}`);
    openConsole(r.id);
    if (current === "run") api("/api/jobs").then(renderHistory);
  } catch (e) { toast("Failed to start job", "bad"); }
};

// ── Job console ──────────────────────────────────────────────────────────────
let consoleJob = null, consolePoll = null;
window.openConsole = function (jid) {
  consoleJob = jid;
  $("#console").classList.remove("hidden");
  $("#console-body").textContent = "";
  pollConsole(true);
  if (consolePoll) clearInterval(consolePoll);
  consolePoll = setInterval(() => pollConsole(false), 1000);
};
function closeConsole() {
  $("#console").classList.add("hidden");
  if (consolePoll) clearInterval(consolePoll);
  consoleJob = null;
}
$("#console-close").addEventListener("click", closeConsole);
$("#console-stop").addEventListener("click", async () => {
  if (consoleJob == null) return;
  await api(`/api/jobs/${consoleJob}/stop`, { method: "POST" });
  toast("Stop requested", "bad");
});

let consoleSince = 0;
async function pollConsole(reset) {
  if (consoleJob == null) return;
  if (reset) consoleSince = 0;
  try {
    const s = await api(`/api/jobs/${consoleJob}?since=${consoleSince}`);
    $("#console-title").textContent = `#${s.id} · ${s.label}`;
    const badge = $("#console-status");
    badge.textContent = s.status; badge.className = `badge ${statusClass(s.status)}`;
    if (s.lines.length) {
      const body = $("#console-body");
      const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
      body.textContent += s.lines.join("\n") + "\n";
      consoleSince = s.total_lines;
      if (atBottom) body.scrollTop = body.scrollHeight;
    }
    $("#console-stop").disabled = s.status !== "running";
    if (s.status !== "running" && consolePoll) {
      clearInterval(consolePoll); consolePoll = null;
      if (current === "run") api("/api/jobs").then(renderHistory);
      if (current === "dashboard") refreshPills();
    }
  } catch (e) { /* ignore transient */ }
}

// ── Signals ──────────────────────────────────────────────────────────────────
async function loadSignals() {
  const v = $("#view-signals");
  v.innerHTML = `<div class="loading">Loading signals…</div>`;
  renderSignals(await api("/api/signals"));
}

function renderSignals(d) {
  const v = $("#view-signals");
  if (!d.date) { v.innerHTML = `<div class="empty">No signals yet — run the technical pipeline.</div>`; return; }

  const opts = (d.dates || []).map(x => `<option ${x === d.date ? "selected" : ""}>${x}</option>`).join("");
  const wpct = (v) => v == null ? "—" : (v * 100).toFixed(0) + "%";
  let html = `<div class="card" style="margin-bottom:16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <label class="faint">Signal date</label>
      <select id="sig-date" class="field" style="width:auto">${opts}</select>
      <span class="muted">${d.ranked.length} ranked · ${d.ranked.filter(r => r.qualifies).length} clear ${(d.min_avg_return * 100).toFixed(2)}% net avg-return</span>
      <span class="faint" style="font-size:12px">All figures net of estimated costs · "win rate" = past hit rate · "worst case" = Wilson lower bound</span>
    </div>`;

  if (!d.ranked.length) html += `<div class="empty">No signals fired on ${d.date}.</div>`;

  for (const r of d.ranked) {
    const setups = r.setups.map(st => `<div class="setup-row">
        <span class="sname">${esc(st.friendly || st.name)} <span class="badge ${st.direction === 'buy' ? 'buy' : (st.direction === 'sell' ? 'sell' : '')}">${st.direction}</span></span>
        <span class="mono faint">net ${pct(st.avg_return)} · win ${wpct(st.confidence)} (≥${wpct(st.wr_lower)}) · PF ${st.profit_factor != null ? Number(st.profit_factor).toFixed(2) : "∞"} · SL ${wpct(st.sl_rate)} · d${num(st.best_day)} (n=${num(st.sample_size)})</span>
        <span class="sdesc">${esc(st.desc)}</span>
      </div>`).join("");
    html += `<div class="card signal-card ${r.qualifies ? "" : "dim"}">
      <div class="signal-head">
        <span class="sym">${esc(r.symbol)}</span>
        <span class="badge ${r.dominant === 'BUY' ? 'buy' : 'sell'}">${r.dominant}</span>
        ${r.qualifies ? "" : `<span class="badge amber">below threshold</span>`}
        <div class="signal-metrics">
          <div class="m"><div class="k">Score</div><div class="v mono">${r.score}</div></div>
          <div class="m"><div class="k">Net avg</div><div class="v">${pct(r.avg_return)}</div></div>
          <div class="m"><div class="k">Win rate</div><div class="v">${(r.confidence * 100).toFixed(0)}%</div></div>
          <div class="m"><div class="k">Worst case</div><div class="v">${wpct(r.wr_lower)}</div></div>
          <div class="m"><div class="k">Setups</div><div class="v mono">${r.n_setups}</div></div>
        </div>
      </div>
      ${setups}
    </div>`;
  }
  v.innerHTML = html;
  $("#sig-date").addEventListener("change", async (e) => {
    renderSignals(await api("/api/signals?date=" + encodeURIComponent(e.target.value)));
  });
}

// ── News & scouts ────────────────────────────────────────────────────────────
async function loadNews() {
  const v = $("#view-news");
  v.innerHTML = `<div class="loading">Loading picks…</div>`;
  const [news, scout] = await Promise.all([api("/api/news"), api("/api/scout")]);

  const card = (r, tagText) => `<div class="card" style="margin-bottom:12px">
      <div class="signal-head">
        <span class="sym">${esc((r.symbol || "").replace(/\.NS|\.BO/, ""))}</span>
        ${tagText ? `<span class="badge amber">${esc(tagText)}</span>` : ""}
        ${r.catalyst ? `<span class="tag">${esc(r.catalyst)}</span>` : ""}
        ${r.whatsapp_sent ? `<span class="badge green">sent</span>` : `<span class="badge">unsent</span>`}
        <div class="signal-metrics">
          <div class="m"><div class="k">CMP</div><div class="v mono">${r.cmp != null ? "₹" + Number(r.cmp).toFixed(1) : "—"}</div></div>
          <div class="m"><div class="k">1D</div><div class="v">${rawpct(r.change_1d_pct)}</div></div>
          <div class="m"><div class="k">5D</div><div class="v">${rawpct(r.change_5d_pct)}</div></div>
          <div class="m"><div class="k">20D</div><div class="v">${rawpct(r.change_20d_pct)}</div></div>
        </div>
      </div>
      ${r.company_name ? `<div class="muted" style="margin-top:6px">${esc(r.company_name)} · ${esc(r.rec_date)}</div>` : ""}
      ${r.reasoning ? `<div class="muted" style="margin-top:8px;font-style:italic">${esc(r.reasoning)}</div>` : ""}
      <div style="margin-top:8px;line-height:1.55">${esc(r.analysis)}</div>
    </div>`;

  const scoutLabel = { hidden_gems: "HIDDEN GEM", small_cap_growth: "SMALLCAP GROWTH", smart_money: "SMART MONEY" };

  let html = `<div class="grid two">
    <div><h3 class="job-group-title">AI News Picks (${news.length})</h3>
      ${news.length ? news.map(r => card(r, "")).join("") : `<div class="empty">No news picks yet.</div>`}</div>
    <div><h3 class="job-group-title">Scout Picks (${scout.length})</h3>
      ${scout.length ? scout.map(r => card(r, scoutLabel[r.scout_type] || r.scout_type)).join("") : `<div class="empty">No scout picks yet.</div>`}</div>
  </div>`;
  v.innerHTML = html;
}

// ── Setups ───────────────────────────────────────────────────────────────────
async function loadSetups() {
  const v = $("#view-setups");
  v.innerHTML = `<div class="loading">Loading setups…</div>`;
  const rows = await api("/api/setups");
  if (!rows.length) { v.innerHTML = `<div class="empty">No setups loaded.</div>`; return; }
  v.innerHTML = `<div class="muted" style="margin-bottom:10px;font-size:12.5px">
      Avg return is net of estimated transaction costs. <b>Lower bound</b> is the
      90% lower-confidence-bound net return (<code>ret_lower</code>) — this, not
      the raw average, drives the conviction weight (<code>1 + ret_lower×20</code>,
      clipped 0.10–2.00). <b>t-stat</b> and <b>n</b> show how much evidence backs
      that bound. Sorted by lower bound, best first. See HFT / Intraday for the
      1–15 minute intraday versions of these strategies.</div>
    <div class="card scroll-x"><table>
    <thead><tr><th>Setup</th><th class="num">Net avg</th><th class="num">Lower bound</th>
      <th class="num">t-stat</th><th class="num">n</th><th class="num">SL rate</th><th class="num">Best day</th>
      <th class="num">Long w</th><th class="num">Short w</th><th>Params</th></tr></thead>
    <tbody>${rows.map(s => `<tr>
      <td><b>${esc(s.name)}</b><div class="faint" style="font-size:11.5px;max-width:340px">${esc(s.desc)}</div></td>
      <td class="num">${pct(s.best_avg_return)}</td>
      <td class="num">${pct(s.ret_lower)}</td>
      <td class="num mono">${s.t_stat != null ? s.t_stat.toFixed(2) : "—"}</td>
      <td class="num mono">${num(s.sample_size)}</td>
      <td class="num mono">${s.best_sl_rate != null ? (s.best_sl_rate * 100).toFixed(0) + "%" : "—"}</td>
      <td class="num mono">${num(s.best_days)}</td>
      <td class="num mono">${s.long_weight != null ? s.long_weight.toFixed(2) : "—"}</td>
      <td class="num mono">${s.short_weight != null ? s.short_weight.toFixed(2) : "—"}</td>
      <td>${Object.entries(s.params || {}).map(([k, val]) => `<span class="tag">${esc(k)}=${esc(val)}</span>`).join("") || "<span class='faint'>default</span>"}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

// ── HFT / Intraday ───────────────────────────────────────────────────────────
const HFT_TF_ORDER = ["1min", "5min", "10min", "15min"];
let _hftTf = null;

async function loadHft() {
  const v = $("#view-hft");
  v.innerHTML = `<div class="loading">Loading intraday results…</div>`;
  const d = await api("/api/hft");
  if (!d.exists) {
    v.innerHTML = `<div class="empty">No db/hft_results.json yet — run the
      "HFT / intraday backtest" job from Run jobs.</div>`;
    return;
  }
  const tfs = HFT_TF_ORDER.filter(tf => d.timeframes[tf]);
  if (!tfs.length) { v.innerHTML = `<div class="empty">No timeframes in hft_results.json.</div>`; return; }
  if (!_hftTf || !tfs.includes(_hftTf)) _hftTf = tfs[0];

  const meta = d.meta || {};
  const head = `<div class="muted" style="margin-bottom:10px;font-size:12.5px">
      Intraday engine: entry next bar after signal, <b>always flat by session
      close</b> (long &amp; short), ${((d.cost ?? 0) * 100).toFixed(2)}% round-trip
      cost, screen at avg ≥ ${((d.min_avg_ret ?? 0) * 100).toFixed(2)}% &amp;
      lower bound &gt; 0. <b>Lower bound</b> (<code>ret_lower</code>) drives the
      "passes screen" verdict, same as the daily Setups table. Generated
      ${esc(d.generated_at || "—")}${meta.years ? ` · years ${esc((meta.years || []).join(", "))}` : ""}.</div>
    <div class="tabs" style="margin-bottom:12px">
      ${tfs.map(tf => `<button class="btn ghost sm hft-tab ${tf === _hftTf ? "active" : ""}" data-tf="${tf}">${tf}</button>`).join("")}
    </div>
    <div id="hft-table"></div>`;
  v.innerHTML = head;

  $$(".hft-tab", v).forEach(b => b.addEventListener("click", () => {
    _hftTf = b.dataset.tf;
    $$(".hft-tab", v).forEach(x => x.classList.toggle("active", x === b));
    renderHftTable(d.timeframes[_hftTf]);
  }));
  renderHftTable(d.timeframes[_hftTf]);
}

function renderHftTable(rows) {
  const c = $("#hft-table");
  if (!rows || !rows.length) { c.innerHTML = `<div class="empty">No setups for this timeframe.</div>`; return; }
  c.innerHTML = `<div class="card scroll-x"><table>
    <thead><tr><th>Setup</th><th class="num">Net avg</th><th class="num">Lower bound</th>
      <th class="num">t-stat</th><th class="num">n (L/S)</th><th class="num">Win rate</th>
      <th class="num">Profit factor</th><th class="num">Avg hold (bars)</th>
      <th class="num">SL rate</th><th>Screen</th></tr></thead>
    <tbody>${rows.map(s => `<tr>
      <td><b>${esc(s.name)}</b><div class="faint" style="font-size:11.5px">${esc(s.friendly || "")}</div></td>
      <td class="num">${pct(s.avg_return)}</td>
      <td class="num">${pct(s.ret_lower)}</td>
      <td class="num mono">${s.t_stat != null ? s.t_stat.toFixed(2) : "—"}</td>
      <td class="num mono">${num(s.n)} <span class="faint">(${num(s.n_long)}/${num(s.n_short)})</span></td>
      <td class="num mono">${s.win_rate != null ? (s.win_rate * 100).toFixed(1) + "%" : "—"}</td>
      <td class="num mono">${s.profit_factor != null ? s.profit_factor.toFixed(2) : "—"}</td>
      <td class="num mono">${num(s.avg_hold_bars)}</td>
      <td class="num mono">${s.sl_rate != null ? (s.sl_rate * 100).toFixed(1) + "%" : "—"}</td>
      <td>${s.passes_screen ? `<span class="tag pos">pass</span>` : `<span class="tag faint">—</span>`}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

// ── Stocks ───────────────────────────────────────────────────────────────────
async function loadStocks() {
  const v = $("#view-stocks");
  v.innerHTML = `<div class="loading">Loading stocks…</div>`;
  const rows = await api("/api/stocks");
  v.innerHTML = `<div class="card" style="margin-bottom:14px"><input id="stk-filter" class="field" placeholder="Filter symbols…" style="max-width:280px"></div>
    <div class="card scroll-x"><table>
    <thead><tr><th>Symbol</th><th>Name</th><th class="num">Rows</th><th class="num">Last date</th><th></th></tr></thead>
    <tbody id="stk-body">${rows.map(rowHtml).join("")}</tbody></table></div>`;
  function rowHtml(s) {
    return `<tr data-sym="${esc(s.symbol)}"><td class="mono">${esc(s.symbol)}</td><td>${esc(s.name || "")}</td>
      <td class="num mono">${num(s.rows)}</td><td class="num mono faint">${s.last_date || "—"}</td>
      <td><button class="btn ghost sm" onclick="viewOhlcv('${esc(s.symbol)}')">chart</button></td></tr>`;
  }
  $("#stk-filter").addEventListener("input", (e) => {
    const q = e.target.value.toUpperCase();
    $$("#stk-body tr").forEach(tr => { tr.style.display = tr.dataset.sym.includes(q) ? "" : "none"; });
  });
}

// TradingView Lightweight Charts instance for the OHLCV panel (kept so we can
// dispose it before re-rendering — createChart leaves a ResizeObserver otherwise).
let _tvChart = null;
const CHART_RANGES = [[90, "3M"], [180, "6M"], [365, "1Y"], [1100, "Max"]];

window.viewOhlcv = async function (sym, days = 180) {
  const p = $("#ohlcv-panel");
  openChartModal();
  p.innerHTML = `<div class="loading">Loading ${esc(sym)}…</div>`;
  let d;
  try {
    d = await api(`/api/ohlcv/${encodeURIComponent(sym)}?days=${days}`);
  } catch (e) {
    p.innerHTML = `<div class="empty">Failed to load ${esc(sym)}.</div>`; return;
  }
  if (!d.rows.length) { p.innerHTML = `<div class="empty">No OHLCV stored for ${esc(sym)}.</div>`; return; }

  const rangeBtns = CHART_RANGES.map(([n, lbl]) =>
    `<button class="btn ghost sm ${n === days ? "active" : ""}" onclick="viewOhlcv('${esc(sym)}', ${n})">${lbl}</button>`
  ).join("");

  const first = d.rows[0].close, last = d.rows[d.rows.length - 1].close;
  const chg = first ? (last - first) / first : 0;

  p.innerHTML = `<div class="card" style="margin-top:16px">
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px">
      <h3 style="margin:0">${esc(sym)}</h3>
      <span class="mono" style="font-size:17px;font-weight:680">₹${last.toFixed(2)}</span>
      <span style="font-size:14px">${pct(chg, 2)}</span>
      <span class="faint" style="font-size:12px">${d.rows.length} bars · ${d.rows[0].date} → ${d.rows[d.rows.length - 1].date}</span>
      <div style="margin-left:auto;display:flex;gap:6px">${rangeBtns}</div>
    </div>
    <div id="tv-chart" style="position:relative;width:100%;height:440px"></div>
    <div class="scroll-x" style="margin-top:16px;max-height:280px;overflow:auto"><table>
      <thead><tr><th>Date</th><th class="num">Open</th><th class="num">High</th><th class="num">Low</th><th class="num">Close</th><th class="num">Volume</th></tr></thead>
      <tbody>${[...d.rows].reverse().map(r => `<tr><td class="mono faint">${r.date}</td>
        <td class="num mono">${r.open.toFixed(1)}</td><td class="num mono">${r.high.toFixed(1)}</td>
        <td class="num mono">${r.low.toFixed(1)}</td><td class="num mono">${r.close.toFixed(1)}</td>
        <td class="num mono faint">${r.volume.toLocaleString()}</td></tr>`).join("")}</tbody></table></div>
  </div>`;

  renderTradingChart($("#tv-chart"), d.rows);
};

// Candlestick + volume chart via TradingView Lightweight Charts, themed to match
// the dashboard. Falls back gracefully if the vendored library didn't load.
function renderTradingChart(container, rows) {
  if (_tvChart) { try { _tvChart.remove(); } catch (e) { /* already gone */ } _tvChart = null; }
  if (!window.LightweightCharts) {
    container.innerHTML = `<div class="empty">Chart library failed to load — check webui/static/vendor/.</div>`;
    return;
  }

  const css = getComputedStyle(document.documentElement);
  const v = (n, fb) => (css.getPropertyValue(n).trim() || fb);
  const green = v("--green", "#3fd07f"), red = v("--red", "#ff6b6b");

  const chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: v("--muted", "#7d8aa0"),
      fontFamily: "ui-monospace, 'JetBrains Mono', Consolas, monospace",
    },
    grid: {
      vertLines: { color: v("--panel-2", "#1a2230") },
      horzLines: { color: v("--panel-2", "#1a2230") },
    },
    rightPriceScale: { borderColor: v("--border", "#232c3b") },
    timeScale: { borderColor: v("--border", "#232c3b"), timeVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  _tvChart = chart;

  const candle = chart.addCandlestickSeries({
    upColor: green, downColor: red, borderVisible: false,
    wickUpColor: green, wickDownColor: red,
  });
  candle.setData(rows.map(r => ({
    time: r.date, open: r.open, high: r.high, low: r.low, close: r.close,
  })));

  const vol = chart.addHistogramSeries({
    priceFormat: { type: "volume" }, priceScaleId: "",
  });
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(rows.map(r => ({
    time: r.date, value: r.volume,
    color: r.close >= r.open ? "rgba(63,208,127,.35)" : "rgba(255,107,107,.35)",
  })));

  chart.timeScale().fitContent();
}

// Chart popup show/hide. Disposing the chart on close frees its ResizeObserver.
function openChartModal() { $("#chart-modal").classList.remove("hidden"); }
function closeChartModal() {
  $("#chart-modal").classList.add("hidden");
  if (_tvChart) { try { _tvChart.remove(); } catch (e) { /* gone */ } _tvChart = null; }
  $("#ohlcv-panel").innerHTML = "";
}
$("#chart-close").addEventListener("click", closeChartModal);
$("#chart-modal").addEventListener("click", (e) => { if (e.target.id === "chart-modal") closeChartModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#chart-modal").classList.contains("hidden")) closeChartModal();
});

// ── Config ───────────────────────────────────────────────────────────────────
async function loadConfig() {
  const v = $("#view-config");
  v.innerHTML = `<div class="loading">Loading config…</div>`;
  const cfg = await api("/api/config");
  const fields = cfg.keys.map(k => `<div class="field">
      <label>${esc(k)}</label>
      <input data-key="${esc(k)}" value="${esc(cfg.values[k])}">
    </div>`).join("");
  v.innerHTML = `<div class="card">
    <h3>.env settings <span class="faint mono" style="text-transform:none;letter-spacing:0">${esc(cfg.path)}</span></h3>
    <div class="form-grid">${fields}</div>
    <div style="margin-top:18px;display:flex;gap:12px;align-items:center">
      <button class="btn primary" id="cfg-save">Save changes</button>
      <span class="muted">Changes take effect next time a pipeline / the server restarts.</span>
    </div>
  </div>`;
  $("#cfg-save").addEventListener("click", async () => {
    const upd = {};
    $$("#view-config input[data-key]").forEach(i => upd[i.dataset.key] = i.value);
    const r = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(upd) });
    toast(`Saved ${r.updated.length} setting(s)`);
  });
}

// ── Logs ─────────────────────────────────────────────────────────────────────
async function loadLogs() {
  const v = $("#view-logs");
  v.innerHTML = `<div class="loading">Loading logs…</div>`;
  const d = await api("/api/logs?lines=500");
  const lines = d.lines.map(l => {
    const low = l.toLowerCase();
    const cls = (low.includes("error") || low.includes("failed")) ? "err" : (low.includes("warn") ? "warn" : "");
    return `<div class="logline ${cls}">${esc(l)}</div>`;
  }).join("");
  v.innerHTML = `<div class="card" style="margin-bottom:12px"><span class="faint mono">${esc(d.path)}</span></div>
    <div class="logbox" id="logbox">${lines || "<span class='faint'>empty</span>"}</div>`;
  const box = $("#logbox"); box.scrollTop = box.scrollHeight;
}

// ── boot ─────────────────────────────────────────────────────────────────────
refreshPills();
setInterval(refreshPills, 8000);
show("dashboard");
