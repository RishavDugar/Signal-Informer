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
  litigation: { title: "Litigation & legal risk", load: loadLitigation },
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
  try { history.replaceState(null, "", `#${view}`); } catch (e) { /* file:// or sandboxed */ }
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
    const o = $("#ollama-pill"); const L = s.llm || {};
    if (L.provider === "claude") {
      o.className = "pill " + (L.up ? "ok" : "bad");
      o.textContent = L.up ? `llm · ${esc(L.label || "claude")}` : "claude cli missing";
      o.title = `Claude via subscription CLI (${L.cli || "?"})` + (L.fallback ? " · falls back to Ollama" : "");
    } else {
      o.className = "pill " + (s.ollama.up ? "ok" : "bad");
      o.textContent = s.ollama.up ? `llm · ollama (${(s.ollama.models || []).length})` : "ollama down";
      o.title = "Local Ollama";
    }
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
          <tr><td>Analysis LLM</td><td class="num">${(s.llm || {}).up ? `<span class="badge green">up</span>` : "<span class='badge red'>down</span>"} ${esc((s.llm || {}).label || s.config.llm_provider)}</td></tr>
          <tr><td>Litigation gate</td><td class="num mono">${esc(s.config.litigation_gate)}</td></tr>
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
  const choices = cfg.choices || {};
  const fields = cfg.keys.map(k => {
    const val = cfg.values[k];
    const ctl = choices[k]
      ? `<select data-key="${esc(k)}">${choices[k].map(c => `<option value="${esc(c)}"${c === val ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`
      : `<input data-key="${esc(k)}" value="${esc(val)}">`;
    return `<div class="field"><label>${esc(k)}</label>${ctl}</div>`;
  }).join("");
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
    $$("#view-config [data-key]").forEach(i => upd[i.dataset.key] = i.value);
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
show(VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "dashboard");   // deep links, e.g. #litigation

// ── Litigation ───────────────────────────────────────────────────────────────
// Built for a 30-second read: one-line brief → risk chart → next 3 checks →
// case cards (plain English up top, details folded away).
let litFilter = "week";
let litScanResult = null;
let litHelpOpen = false;
try { litHelpOpen = localStorage.getItem("litHelpSeen") !== "1"; } catch (e) { litHelpOpen = true; }

const TIER = {
  material: { label: "High impact", cls: "red",
    rule: th => `Could move the stock ${litPctAbs(th.min_move, 0)}+ (or ${litPctAbs(th.materiality, 0)}+ of company value at stake). Gets WhatsApp alerts.` },
  near: { label: "Medium impact", cls: "amber",
    rule: th => `Could move the stock ${litPctAbs(th.near_move, 0)}+ (or ${litPctAbs(th.near_materiality, 0)}+ of value at stake). Tracked, no alerts.` },
  below: { label: "Low impact", cls: "",
    rule: th => `Likely move under ${litPctAbs(th.near_move, 0)}. Shown for completeness.` },
};
const VERDICT = {
  "Act now": { cls: "red", tip: "A verdict is expected within about a day — look today." },
  "Prepare": { cls: "amber", tip: "Could land soon (hearing over, or a hearing this week) with a big possible move." },
  "Watch": { cls: "blue", tip: "Meaningful but no date yet — a weekly glance is enough." },
  "Low priority": { cls: "", tip: "Small likely move and nothing imminent — safe to ignore." },
};
let litTiers = new Set(["material", "near", "below"]);
try { const s = JSON.parse(localStorage.getItem("litTiers") || "null"); if (Array.isArray(s)) litTiers = new Set(s); } catch (e) { /* storage blocked */ }

const litStat = (k, val, sub, cls = "") => `<div class="card stat ${cls}"><div class="k">${k}</div><div class="v">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
const litPct = (v, d = 0) => (v === null || v === undefined) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(d)}%`;
const litPctAbs = (v, d = 1) => (v === null || v === undefined) ? "—" : `${(v * 100).toFixed(d)}%`;
function hostOf(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } }
const fmtDT = s => {
  if (!s) return "";
  const d = new Date(s); if (isNaN(d)) return s;
  const t0 = new Date(); t0.setHours(0, 0, 0, 0);
  const d0 = new Date(d); d0.setHours(0, 0, 0, 0);
  const diff = Math.round((d0 - t0) / 86400000), hm = d.toTimeString().slice(0, 5);
  if (diff === 0) return `Today ${hm}`;
  if (diff === 1) return `Tomorrow ${hm}`;
  if (diff === -1) return `Yesterday ${hm}`;
  return d.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" }) + " " + hm;
};
const fmtD = s => {
  if (!s) return "";
  const d = new Date(s + "T00:00:00"); if (isNaN(d)) return s;
  const t0 = new Date(); t0.setHours(0, 0, 0, 0);
  const diff = Math.round((d - t0) / 86400000);
  const rel = diff === 0 ? "today" : diff === 1 ? "tomorrow" : diff > 1 ? `in ${diff} days` : `${-diff} days ago`;
  return `${d.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short", year: "numeric" })} <span class="faint">(${rel})</span>`;
};
const linkChip = l => `<a class="lit-link ${l.kind}" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer" title="${esc(l.hint || l.url)}">${esc(l.label)} ↗</a>`;
const attachBtn = (caseId, label = "📎 Attach") => `<label class="btn sm lit-attach" title="Attach a PDF / screenshot / saved page from the court website — Claude reads it and updates the dates">${label}
  <input type="file" accept=".pdf,.html,.htm,.mhtml,.txt,.csv,.png,.jpg,.jpeg,.webp" data-attach="${caseId}" hidden></label>`;
const verdictChip = v => `<span class="badge ${(VERDICT[v] || {}).cls || ""}" title="${esc((VERDICT[v] || {}).tip || "")}">${esc(v || "")}</span>`;

// ── The impact bar: what the verdict could do vs what options already expect ──
// Diverging, one shared scale per view. Red (left) = verdict against the company,
// blue (right) = verdict for it, grey band = move the options market already expects.
function impactBar(c, S, big = false) {
  // HTML bars (not SVG) so numbers never stretch. Shared scale S per view; 0 at centre.
  const i = c.insight || {};
  const pos = v => Math.max(-1, Math.min(1, v / S)) * 50;          // % offset from centre
  const adv = c.move_adverse, fav = c.move_favourable, im = c.implied_move;
  const bar = (v, cls) => {
    if (v == null || Math.abs(v) < 0.0005) return "";
    const p = pos(v), left = 50 + Math.min(0, p), w = Math.abs(p);
    const lab = v < 0 ? `<span class="ib-val l" style="left:${left}%">${litPct(v)}</span>`
                      : `<span class="ib-val r" style="left:${50 + p}%">${litPct(v)}</span>`;
    return `<div class="${cls} ${v < 0 ? "goes-l" : "goes-r"}" style="left:${left}%;width:${w}%"></div>${lab}`;
  };
  const band = im ? `<div class="ib-band" style="left:${50 - pos(im)}%;width:${2 * pos(im)}%"></div>` : "";
  const tip = `${i.headline || ""} ${i.priced || ""}`.trim();
  return `<div class="ib ${big ? "big" : ""}" role="img" aria-label="${esc(tip)}" title="${esc(tip)}">
    <div class="ib-track">${band}<div class="ib-zero"></div>${bar(adv, "ib-neg")}${bar(fav, "ib-pos")}</div></div>`;
}
const impactLegend = `<div class="ib-legend">
  <span><i class="sw neg"></i>If the court rules <b>against</b> the company</span>
  <span><i class="sw pos"></i>If it rules <b>for</b> the company</span>
  <span><i class="sw band"></i>Move the options market already expects</span>
  <span class="faint">Bar sticking out of the grey = not priced in yet</span></div>`;
const scaleFor = list => {
  const m = Math.max(0.05, ...list.map(c => Math.max(Math.abs(c.move_adverse || 0), Math.abs(c.move_favourable || 0), c.implied_move || 0)));
  return Math.ceil(m * 20) / 20;   // round up to the next 5%
};

function helpPanel(d) {
  const th = d.thresholds || {};
  return `<div class="card lit-help ${litHelpOpen ? "" : "hidden"}" id="lit-help">
    <div class="lit-help-h"><b>How to read this page</b> <button class="btn sm ghost" id="lit-help-close">Got it ✕</button></div>
    <ol>
      <li><b>Top line</b> — the one thing to know today. If it says "all quiet", you can stop there.</li>
      <li><b>The chart</b> — one row per company with a live court case. <span class="neg-t">Red</span> = how far the stock could fall if the court rules against the company, <span class="pos-t">blue</span> = how far it could rise if the court rules for it, <span class="faint">grey band</span> = the move the options market already expects. A bar poking out of the grey band means the market hasn't priced the case in. The % under the name is the chance the court rules against the company.</li>
      <li><b>Labels</b> — ${verdictChip("Act now")} verdict expected within a day · ${verdictChip("Prepare")} could land soon · ${verdictChip("Watch")} no date yet, glance weekly · ${verdictChip("Low priority")} ignore.</li>
      <li><b>Your next checks</b> — Supreme Court, Delhi High Court and NCLAT schedules are read automatically every evening. What's left are 2-minute look-ups on court pages that ask for a captcha, so you do them — each comes with exactly what to type. Open the link, save the page as PDF or screenshot, press <b>📎 Attach</b> — Claude reads it and updates the dates. WhatsApp nudges you at ${esc(d.slots?.morning || "09:00")} and ${esc(d.slots?.evening || "19:30")} when something's due.</li>
    </ol>
    <div class="faint" style="font-size:12px">Impact levels: <b>High</b> — ${esc(TIER.material.rule(th))} <b>Medium</b> — ${esc(TIER.near.rule(th))} Estimates come from Claude reading public court and news records; verdicts can go either way.</div>
  </div>`;
}

function brief(open) {
  const by = v => open.filter(c => c.insight?.verdict === v).sort((a, b) => b.insight.score - a.insight.score);
  const act = by("Act now"), prep = by("Prepare"), watch = by("Watch");
  let line;
  if (act.length) line = `⚡ <b>${act.length} case${act.length > 1 ? "s" : ""} need${act.length > 1 ? "" : "s"} you today.</b> ${esc(act[0].insight.headline)} <span class="faint">${esc(act[0].insight.when)}.</span>`;
  else if (prep.length) line = `<b>Nothing needs you today.</b> ${prep.length} case${prep.length > 1 ? "s" : ""} could move a stock soon — biggest: <b>${esc(prep[0].insight.name)}</b> (${esc(prep[0].insight.when.toLowerCase())}).`;
  else line = `✅ <b>All quiet.</b> No court decision is likely to move these stocks soon.`;
  return `<div class="card lit-brief"><div class="lit-brief-line">${line}</div>
    <div class="grid cards" style="margin-top:12px">
      ${litStat(`${verdictChip("Act now")}`, act.length, "verdict expected within a day", act.length ? "hot" : "")}
      ${litStat(`${verdictChip("Prepare")}`, prep.length, "could land soon / big move")}
      ${litStat(`${verdictChip("Watch")}`, watch.length, "no date yet — weekly glance")}
    </div></div>`;
}

function riskChart(open) {
  const rows = open.filter(c => c.insight && c.insight.verdict !== "Low priority" && c.p_adverse != null)
    .sort((a, b) => b.insight.score - a.insight.score).slice(0, 8);
  if (!rows.length) return "";
  const S = scaleFor(rows);
  const body = rows.map(c => `<div class="rc-row" data-goto="${c.id}" title="${esc(c.insight.headline)}">
      <div class="rc-name"><span class="dot tier-dot-${esc(c.tier)}"></span><b>${esc(c.symbol)}</b>
        <div class="faint">${litPctAbs(c.p_adverse, 0)} chance against</div></div>
      <div class="rc-bar">${impactBar(c, S)}</div>
      <div class="rc-when">${verdictChip(c.insight.verdict)}<div class="faint">${esc(c.insight.when)}</div></div>
    </div>`).join("");
  return `<div class="card" style="margin-top:16px">
    <h3>Where the risk is <span class="faint ttl-sub">— how much each verdict could move the stock</span></h3>
    ${impactLegend}
    <div class="rc-scale"><span></span><div class="rc-axis"><span style="left:0">−${litPctAbs(S, 0)}</span><span style="left:50%">0</span><span style="left:100%">+${litPctAbs(S, 0)}</span></div><span></span></div>
    ${body}
    <div class="faint" style="font-size:11.5px;margin-top:6px">Top ${rows.length} by attention (size of likely move × how soon × not priced in). Click a row for the full story.</div>
  </div>`;
}

// ── Search recipe: exactly what to type on the court website ──────────────────
function searchRecipe(s, opts = {}) {
  if (!s) return `<div class="recipe empty-recipe">🔎 Search details not found yet — they're looked up automatically each night.</div>`;
  const conf = { high: ["green", "case number confirmed"], medium: ["amber", "likely right — check the names match"],
                 low: ["", "number not found — search by company name"] }[s.confidence] || ["", ""];
  const steps = (s.steps || []).slice(0, opts.compact ? 1 : 3).map((st, k) => `
    <div class="recipe-step">
      <div class="recipe-route"><b>${k === 0 ? "Try this" : "Or"}:</b> ${esc(st.route)}
        ${st.url ? `<a class="btn sm" href="${esc(st.url)}" target="_blank" rel="noopener noreferrer">Open ${esc(st.page)} ↗</a>` : ""}</div>
      <table class="recipe-fill"><tbody>${(st.fill || []).map(([k2, v]) => `<tr><td>${esc(k2)}</td>
        <td><b class="mono">${esc(v)}</b> <button class="copy" data-copy="${esc(v)}" title="Copy">⧉</button></td></tr>`).join("")}</tbody></table>
    </div>`).join("");
  const ids = s.ids || {};
  const autoNote = opts.auto ? `<div class="recipe-auto">✅ The daily court schedule for this court is checked automatically — use this only to find the next date sooner or to download hearing notes.</div>` : "";
  return `<div class="recipe">${autoNote}
    <div class="recipe-h">🔎 <b>What to type</b> <span class="badge ${conf[0]}">${esc(conf[1])}</span>
      ${ids.stage_now ? `<span class="faint recipe-now">Latest known: ${esc(ids.stage_now)}</span>` : ""}</div>
    ${steps}
    ${(s.spot || []).length ? `<div class="recipe-sub"><b>How to spot the right case</b><ul>${s.spot.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${(s.look_for || []).length && !opts.compact ? `<div class="recipe-sub"><b>On the result page, look for</b><ul>${s.look_for.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${(s.sources || []).length && !opts.compact ? `<div class="faint recipe-src">Identifiers found in: ${s.sources.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(hostOf(u))}</a>`).join(" · ")}</div>` : ""}
  </div>`;
}

function nextChecks(d) {
  const ch = (d.checks || []).filter(c => litTiers.has(c.tier || "below"));
  const low = ch.filter(c => c.verdict === "Low priority");
  const main = ch.filter(c => !c.routine && c.verdict !== "Low priority");
  const routine = ch.filter(c => c.routine && c.verdict !== "Low priority");
  const row = (c, compact) => `<div class="chk-row tier-${esc(c.tier)} ${compact ? "compact" : ""}">
      <div class="chk-when"><b>${esc(fmtDT(c.due))}</b>${c.status === "overdue" ? `<div class="badge red">overdue</div>` : ""}</div>
      <div class="chk-body">
        <div>${compact ? "" : verdictChip(c.verdict) + " "}<b>${esc(c.symbol)}</b> <span class="faint">· ${esc(c.court_plain || c.court || "")}${c.auto ? " · checked automatically" : ""}</span></div>
        <div class="chk-action">${esc(c.action)}</div>
        ${compact ? "" : `<div class="chk-why">Why bother: ${esc(c.why_time || c.headline || "")}</div>`}
        ${compact ? `<div class="lit-links">${(c.links || []).slice(0, 1).map(linkChip).join("")}</div>`
                  : searchRecipe(c.search, { auto: c.auto })}
      </div>
      <div class="chk-btns">${attachBtn(c.case_id)}
        <button class="btn sm" data-done="${esc(c.key)}" data-case="${c.case_id}" title="I've checked this">✓ Done</button></div>
    </div>`;
  const top = main.slice(0, 3), more = main.slice(3);
  return `<div class="card" style="margin-top:16px">
    <h3>Your next checks <span class="faint ttl-sub">— 2-minute look-ups, most useful first</span></h3>
    ${top.length ? top.map(c => row(c, false)).join("") : `<div class="empty">Nothing time-sensitive. 🎉</div>`}
    ${more.length ? `<details class="lit-more"><summary>${more.length} more time-sensitive check(s)</summary>${more.map(c => row(c, false)).join("")}</details>` : ""}
    ${routine.length ? `<details class="lit-more"><summary>${routine.length} weekly "find the next hearing date" checks — batch them once a week</summary>${routine.map(c => row(c, true)).join("")}</details>` : ""}
    ${low.length ? `<details class="lit-more"><summary>${low.length} low-priority check(s) — optional, small likely moves</summary>${low.map(c => row(c, true)).join("")}</details>` : ""}
  </div>`;
}

function scanCard(d) {
  const r = litScanResult, plainDoc = (d.plain || {}).doc || {};
  const res = !r ? "" : r.error ? `<div class="badge red">${esc(r.error)}</div>` : `
    <div style="margin-top:10px"><b>${esc(r.file)}</b> — checked against ${r.scanned} open case(s).
      ${r.note ? `<div class="lit-analysis">${esc(r.note)}</div>` : ""}
      ${(r.matches || []).map(m => `<div class="lit-doc"><b>${esc(m.symbol)}</b> — ${esc(m.title || "")}
        ${m.case_found ? `<span class="badge green">found</span>` : `<span class="badge">name match only</span>`}
        <div>${esc(m.summary || "")}</div>${m.action ? `<div class="lit-doc-action">➡️ ${esc(m.action)}</div>` : ""}</div>`).join("")}
    </div>`;
  return `<details class="card lit-scan" ${r ? "open" : ""} style="margin-top:16px">
    <summary><b>Have a court schedule PDF?</b> <span class="faint">Drop it here and every tracked case is searched in it.</span></summary>
    <div style="margin-top:10px"><label class="btn primary sm lit-attach">📄 Choose file
      <input type="file" accept=".pdf,.html,.htm,.mhtml,.txt,.csv" data-scan="1" hidden></label>
      <span class="faint" style="font-size:12px;margin-left:8px">NCLAT schedules are checked automatically every night.</span></div>
    ${res}
    ${(d.general_docs || []).slice(0, 3).map(a => `<div class="faint" style="font-size:12px;margin-top:4px">${esc((a.uploaded || "").replace("T", " ").slice(0, 16))} · ${esc(plainDoc[a.doc_type] || "Document")} · ${esc(a.summary || "")}</div>`).join("")}
  </details>`;
}

function litCard(c, S, plain) {
  const i = c.insight || {}, t = TIER[c.tier] || TIER.below;
  const stage = (plain.stage || {})[c.stage] || c.stage;
  const srcs = (c.sources || []).filter(u => /^https?:\/\//.test(u));
  const stake = c.claim_cr ? `₹${Number(c.claim_cr).toLocaleString("en-IN", { maximumFractionDigits: 0 })} cr` : "—";
  const mat = c.materiality ? ` <span class="faint">(${litPctAbs(c.materiality, 1)} of company value)</span>` : "";
  const official = (c.links || []).filter(l => l.kind === "official"), news = (c.links || []).filter(l => l.kind === "news");
  const decoder = (c.decoder || []).length ? `<div class="lit-decoder">📖 ${c.decoder.map(g => `<b>${esc(g.abbr)}</b> = ${esc(g.meaning)}`).join(" · ")}</div>` : "";
  const docs = (c.attachments || []).map(a => {
    const an = a.analysis || {};
    const ch = an.changed && Object.keys(an.changed).length ? ` <span class="badge green">updated the case</span>` : "";
    return `<div class="lit-doc"><div><b>${esc((plain.doc || {})[an.doc_type || a.doc_type] || "Document")}</b>
        <span class="faint">· ${esc(a.filename)} · ${esc((a.uploaded || "").replace("T", " ").slice(0, 16))}</span>
        ${a.case_found ? `<span class="badge green">case found</span>` : `<span class="badge">case not in it</span>`}${ch}</div>
      ${an.summary ? `<div>${esc(an.summary)}</div>` : ""}${an.action ? `<div class="lit-doc-action">➡️ ${esc(an.action)}</div>` : ""}</div>`;
  }).join("");
  return `<div class="card lit-card tier-${esc(c.tier || "below")}" id="lit-case-${c.id}">
    <div class="lit-head">
      ${verdictChip(i.verdict)}
      <span class="sym">${esc(c.symbol)}</span><span class="faint">${esc(i.name || "")}</span>
      <span class="badge ${t.cls}" title="${esc(c.tier_reason || "")}">${t.label}</span>
      <span class="when"><b>${esc(i.when || "")}</b> ${attachBtn(c.id)}</span>
    </div>
    <div class="lit-headline">${esc(i.headline || c.title || "")}</div>
    ${c.p_adverse != null ? `<div class="lit-bar">${impactBar(c, S, true)}</div>` : ""}
    <div class="lit-why"><div>📈 ${esc(i.priced || "")}</div><div>⏱️ ${esc(i.why_time || "")}</div></div>
    <div class="lit-case-line faint">${esc(c.title || "")} · ${esc(c.court_plain || c.court || "")} · ${esc(stage)}</div>
    ${decoder}
    <details class="lit-details"><summary>How to check it yourself, dates & full analysis</summary>
      ${searchRecipe(c.search, { auto: /supreme|nclat|delhi high court/i.test(c.court || "") })}
      <div class="lit-dates">
        <div class="dt ${c.next_date ? "has" : "none"}"><div class="k">Next date</div>
          <div class="v">${c.next_date ? fmtD(c.next_date) : "Not known yet"}</div>
          ${c.listing_note ? `<div class="faint">${esc(c.listing_note)}</div>` : ""}
          ${c.date_source ? `<div class="faint">from: ${/^https?:/.test(c.date_source) ? `<a href="${esc(c.date_source)}" target="_blank" rel="noopener noreferrer">${esc(hostOf(c.date_source))}</a>` : esc(c.date_source)}</div>` : ""}</div>
        <div class="dt"><div class="k">Where it stands</div><div class="v">${esc(stage)}</div>
          ${c.stage_date ? `<div class="faint">since ${fmtD(c.stage_date)}</div>` : ""}</div>
        <div class="dt"><div class="k">Last checked</div><div class="v">${esc((c.last_checked || "—").replace("T", " ").slice(0, 16))}</div>
          <div class="faint">${(c.attachments || []).length} document(s) attached</div></div>
      </div>
      <div class="lit-where"><span class="k">Where to look (official)</span><div class="lit-links">${official.map(linkChip).join("")}</div>
        <span class="k">News</span><div class="lit-links">${news.map(linkChip).join("")}</div>
        <div class="faint" style="font-size:11.5px">Court sites ask for a captcha: open, look up the case, save as PDF or screenshot, then 📎 Attach.</div></div>
      <div class="lit-metrics">
        <div class="m"><div class="k">Money at stake</div><div class="v">${stake}${mat}</div></div>
        <div class="m"><div class="k">Chance it goes against them</div><div class="v">${c.p_adverse == null ? "—" : litPctAbs(c.p_adverse, 0)}</div></div>
        <div class="m"><div class="k">Stock move if against</div><div class="v neg">${litPct(c.move_adverse)}</div></div>
        <div class="m"><div class="k">Stock move if for</div><div class="v pos">${litPct(c.move_favourable)}</div></div>
        <div class="m"><div class="k">Typical move</div><div class="v">${litPctAbs(i.typical, 1)}</div></div>
        <div class="m"><div class="k">Options already expect</div><div class="v">${c.implied_move ? "±" + litPctAbs(c.implied_move, 1) : "no options"}</div></div>
      </div>
      <div class="lit-analysis"><b>Claude's reading.</b> ${esc(c.rationale || "Not estimated yet.")}</div>
      <div class="lit-idea">💡 <b>Possible trade:</b> ${esc(c.trade_idea || "—")}</div>
      ${docs ? `<div class="lit-docs"><span class="k">Your documents</span>${docs}</div>` : ""}
      ${srcs.length ? `<div class="lit-sources"><span class="faint">Research sources</span>${srcs.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(hostOf(u))} — ${esc(u)}</a>`).join("")}</div>` : ""}
    </details>
  </div>`;
}

async function litUpload(url, file) {
  const fd = new FormData(); fd.append("file", file);
  const r = await fetch(url, { method: "POST", body: fd });
  let j = {}; try { j = await r.json(); } catch (e) { j = { error: `HTTP ${r.status}` }; }
  if (!r.ok && !j.error) j.error = `HTTP ${r.status}`;
  return j;
}

async function loadLitigation() {
  const v = $("#view-litigation");
  if (!v.innerHTML) v.innerHTML = `<div class="loading">Loading…</div>`;
  const d = await api("/api/litigation?days=7");
  if (d.error) { v.innerHTML = `<div class="card"><div class="empty">Error: ${esc(d.error)}</div></div>`; return; }
  const plain = d.plain || {}, th = d.thresholds || {};
  const all = d.cases || [];
  const open = all.filter(c => !c.decided), week = all.filter(c => c.upcoming), decided = all.filter(c => c.decided);
  const bucket = { week, open, decided }[litFilter] || week;
  const pick = bucket.filter(c => litTiers.has(c.tier || "below"))
    .sort((a, b) => (b.insight?.score || 0) - (a.insight?.score || 0));
  const S = scaleFor(pick.filter(c => c.p_adverse != null));
  const tierCount = (list, t) => list.filter(c => (c.tier || "below") === t).length;
  const btn = (id, label, n) => `<button class="btn sm ${litFilter === id ? "active" : ""}" data-lf="${id}">${label} (${n})</button>`;
  const chip = t => `<button class="btn sm lit-chip ${litTiers.has(t) ? "on" : "off"}" data-tier="${t}" title="${esc(TIER[t].rule(th))}">
      <span class="dot tier-dot-${t}"></span>${TIER[t].label} (${tierCount(bucket, t)})</button>`;
  const ev = (d.events || []).slice(0, 20).map(e => `<tr><td class="mono">${esc((e.ts || "").replace("T", " ").slice(0, 16))}</td>
      <td><b>${esc(e.symbol)}</b></td><td>${esc({ new_case: "new case", stage_change: "moved forward", date_change: "new date", document: "your document" }[e.kind] || e.kind)}</td>
      <td>${esc(e.title || "")}</td><td>${esc((plain.stage || {})[e.new_value] || (plain.doc || {})[e.new_value] || e.new_value || "")}</td></tr>`).join("");
  v.innerHTML = `
    <div class="lit-top"><button class="btn sm" id="lit-help-btn" title="A 4-step guide to this page">ⓘ How to read this page</button>
      <span class="faint" style="font-size:12px">${d.scanned || 0} companies scanned · updated ${esc((d.last_scan || "").replace("T", " ").slice(0, 16))}</span></div>
    ${d.autoscan ? `<div class="lit-auto">✅ <b>Checked for you automatically:</b> ${Object.entries(d.autoscan.courts || {}).map(([k, n]) => `${esc(k)} (${n})`).join(", ")} court schedules, up to ${fmtD((d.autoscan.dates || []).slice(-1)[0] || "")} — last run ${esc(fmtDT(d.autoscan.ran))}. For cases in these courts you don't need to check the schedule yourself; we'll WhatsApp you if one is listed for a verdict.</div>` : ""}
    ${helpPanel(d)}
    ${brief(open)}
    ${riskChart(open)}
    ${nextChecks(d)}
    ${scanCard(d)}
    <h3 class="lit-section">All cases <span class="faint ttl-sub">— plain English first; open a card for dates, court links and the full analysis</span></h3>
    <div class="lit-filters">
      ${btn("week", "Verdict or hearing this week", week.length)}${btn("open", "All open", open.length)}${btn("decided", "Decided", decided.length)}
      <span class="lit-legend">${["material", "near", "below"].map(chip).join("")}</span>
    </div>
    ${pick.length ? `${impactLegend}${pick.map(c => litCard(c, S, plain)).join("")}` : `<div class="card"><div class="empty">${bucket.length ? "All cases here are hidden by the impact filter." : (litFilter === "week" ? "No verdicts or hearings this week." : "Nothing here yet.")}</div></div>`}
    <details class="card" style="margin-top:16px"><summary><b>Recent changes</b> <span class="faint">(${(d.events || []).length})</span></summary>
      ${ev ? `<table style="margin-top:8px"><thead><tr><th>When</th><th>Stock</th><th>What happened</th><th>Case</th><th>Now</th></tr></thead><tbody>${ev}</tbody></table>` : `<div class="empty">Nothing yet</div>`}
    </details>`;

  const help = $("#lit-help");
  $("#lit-help-btn").addEventListener("click", () => { litHelpOpen = help.classList.toggle("hidden") === false; });
  $("#lit-help-close").addEventListener("click", () => {
    help.classList.add("hidden"); litHelpOpen = false;
    try { localStorage.setItem("litHelpSeen", "1"); } catch (e) { /* storage blocked */ }
  });
  v.querySelectorAll("[data-goto]").forEach(r => r.addEventListener("click", () => {
    const el = $(`#lit-case-${r.dataset.goto}`);
    if (!el) {                      // card hidden by the current filter → show all open, then jump
      const id = r.dataset.goto; litFilter = "open"; litTiers = new Set(["material", "near", "below"]);
      loadLitigation().then(() => { const e2 = $(`#lit-case-${id}`); e2?.querySelector("details")?.setAttribute("open", ""); e2?.scrollIntoView({ behavior: "smooth" }); });
      return;
    }
    el.querySelector("details")?.setAttribute("open", "");
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  v.querySelectorAll("[data-lf]").forEach(b => b.addEventListener("click", () => { litFilter = b.dataset.lf; loadLitigation(); }));
  v.querySelectorAll("[data-tier]").forEach(b => b.addEventListener("click", () => {
    const t = b.dataset.tier;
    litTiers.has(t) ? litTiers.delete(t) : litTiers.add(t);
    try { localStorage.setItem("litTiers", JSON.stringify([...litTiers])); } catch (e) { /* storage blocked */ }
    loadLitigation();
  }));
  v.querySelectorAll("button[data-copy]").forEach(b => b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(`Copied: ${b.dataset.copy}`); }
    catch (e) { toast("Copy blocked by the browser — select the text instead", "bad"); }
  }));
  v.querySelectorAll("[data-done]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    await api("/api/litigation/check/done", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: b.dataset.done, case_id: Number(b.dataset.case) }) });
    toast("Marked done"); loadLitigation();
  }));
  v.querySelectorAll("input[data-attach]").forEach(inp => inp.addEventListener("change", async () => {
    const f = inp.files[0]; if (!f) return;
    const lbl = inp.closest("label"); lbl.classList.add("busy"); lbl.firstChild.textContent = "⏳ Claude is reading… ";
    toast(`Reading ${f.name} — usually 10-60 seconds`);
    const j = await litUpload(`/api/litigation/case/${inp.dataset.attach}/attach`, f);
    if (j.error) toast(`Failed: ${j.error}`, "bad");
    else toast(j.case_found ? `Found it — ${j.summary || "case updated"}` : `This case isn't in ${f.name} — saved anyway`);
    loadLitigation();
  }));
  v.querySelectorAll("input[data-scan]").forEach(inp => inp.addEventListener("change", async () => {
    const f = inp.files[0]; if (!f) return;
    const lbl = inp.closest("label"); lbl.classList.add("busy"); lbl.firstChild.textContent = "⏳ Searching… ";
    toast(`Searching ${f.name} for your cases…`);
    litScanResult = await litUpload("/api/litigation/scan", f);
    loadLitigation();
  }));
}
