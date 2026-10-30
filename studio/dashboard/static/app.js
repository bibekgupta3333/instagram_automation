"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ts = t => t ? new Date(t * 1000).toLocaleString() : "";
let tab = "pipeline", sel = null, filt = {channel: "", status: "", draft: ""};

async function get(u) { const r = await fetch(u, {cache: "no-store"}); return r.json(); }

// ---- interactive layer: state, API helpers ----
const LS = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
};
const ui = {
  approver: LS.get("dash.approver"),
  confirm: {},            // job_id -> typed confirmation (job detail)
  sel: new Set(),         // bulk-review selected job ids
  bulkConfirm: "", sample: [], sampleUrls: {}, viewed: {}, reported: {},
  plat: {},               // job_id -> {youtube,instagram,facebook}
  hintOpen: null,
};
const form = {};          // action name -> {param: value}
let token = null, actions = [], cap = null, busyLocal = false;
let track = {id: null, status: "", text: "", name: ""};
const TERMINAL = ["ok", "failed", "refused", "stopped"];
const isBusy = () => busyLocal || actions.some(a => a.status === "queued" || a.status === "running");

function errText(status, data) {
  if (status === 404) return "backend not available yet";
  const m = data && (data.error || data.message || data.detail || data.reason);
  return (m ? (typeof m === "string" ? m : JSON.stringify(m)) : "request failed") + " (HTTP " + status + ")";
}
async function getJ(u) {
  try {
    const r = await fetch(u, {cache: "no-store"});
    let d = null; try { d = await r.json(); } catch (e) { /* non-json */ }
    return {ok: r.ok, status: r.status, data: d};
  } catch (e) { return {ok: false, status: 0, data: {error: "network error"}}; }
}
async function ensureToken(force) {
  if (token && !force) return token;
  const r = await getJ("/api/session");
  token = r.ok && r.data ? r.data.token : null;
  return token;
}
async function post(u, body, retry) {
  const t = await ensureToken(!!retry);
  if (!t) return {ok: false, status: 404, data: {error: "backend not available yet"}};
  try {
    const r = await fetch(u, {method: "POST", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-Dashboard-Token": t}, body: JSON.stringify(body || {})});
    let d = null; try { d = await r.json(); } catch (e) { /* non-json */ }
    if (r.status === 403 && !retry) { token = null; return post(u, body, true); }
    return {ok: r.ok, status: r.status, data: d};
  } catch (e) { return {ok: false, status: 0, data: {error: "network error"}}; }
}
let flashT = null;
function flash(msg, kind) {
  const f = $("flash"); f.hidden = !msg; f.textContent = msg || "";
  f.className = "flash " + (kind || "");
  clearTimeout(flashT); if (msg && kind === "ok") flashT = setTimeout(() => { f.hidden = true; }, 8000);
}
const tag = (t, cls) => `<span class="tag ${esc(cls || t)}">${esc(t)}</span>`;
function table(cols, rows, rowAttr) {
  if (!rows.length) return '<div class="sub">none</div>';
  return `<div class="scroll"><table><thead><tr>${cols.map(c => `<th>${esc(c[0])}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr ${rowAttr ? rowAttr(r) : ""}>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("") +
    "</tbody></table></div>";
}
const card = (h, body) => `<div class="card"><h2>${esc(h)}</h2>${body}</div>`;
const issues = l => (l && l.length) ? `<pre>${esc(l.map(x => typeof x === "string" ? x : JSON.stringify(x)).join("\n"))}</pre>` : "";

async function live() {
  try {
    const l = await get("/api/live"), b = $("live");
    b.className = "banner " + (l.guarded_running ? "live" : "stop");
    b.textContent = l.guarded_running ? "run_guarded.py running (pid " + l.pids.join(",") + ")" : "No guarded run active";
    if (l.newest_log) b.innerHTML += ` <span class="sub">newest log ${esc(l.newest_log.name)}: ${esc((l.newest_log.tail.slice(-1)[0] || "").slice(0, 120))}</span>`;
  } catch (e) { $("live").textContent = "dashboard server unreachable"; }
  await refreshActions(); await refreshCap(); paintStatusBar();
}
async function refreshActions() {
  const r = await getJ("/api/actions");
  if (r.ok && r.data) {
    actions = Array.isArray(r.data) ? r.data : (r.data.actions || []);
    if (!track.id) { const a = actions.find(x => x.status === "running" || x.status === "queued"); if (a) setTrack(a.id, a.name); }
  } else if (r.status === 404) actions = [];
}
async function refreshCap() { const r = await getJ("/api/capacity"); if (r.ok) cap = r.data; }
let lastAct = "";
function paintStatusBar() {
  const run = actions.find(a => a.status === "running" || a.status === "queued");
  const t = run ? "Running: " + run.name + " (" + run.status + ")" : "No action running";
  if (t !== lastAct) { $("sb-act").textContent = t; lastAct = t; }
  $("sb-mem").innerHTML = cap ? "Memory pressure: " + tag(cap.memory_pressure, cap.memory_pressure) : "";
  $("sb-cap").textContent = cap ? "Daily cap: " + cap.today_count + " / " + cap.cap : "";
}

const views = {
  async pipeline() {
    if (sel) return jobDetail(sel);
    const q = new URLSearchParams(Object.entries(filt).filter(e => e[1])).toString();
    const [d, rv] = await Promise.all([get("/api/jobs?" + q), get("/api/review")]);
    const opts = (a, v) => `<option value="">all</option>` + a.map(x => `<option ${x === v ? "selected" : ""}>${esc(x)}</option>`).join("");
    let h = `<div class="card"><h2>Jobs</h2><div class="filters">
      <select id="f-channel">${opts(d.channels || [], filt.channel)}</select>
      <select id="f-status">${opts(d.statuses || [], filt.status)}</select>
      <select id="f-draft"><option value="">draft: any</option><option value="1" ${filt.draft === "1" ? "selected" : ""}>draft only</option><option value="0" ${filt.draft === "0" ? "selected" : ""}>non-draft</option></select></div>` +
      table([["Job", r => esc(r.job_id)], ["Channel", r => esc(r.channel)], ["Status", r => esc(r.status)],
        ["Stage", r => esc(r.current_stage)], ["Draft", r => r.is_draft ? "yes" : ""], ["Approval", r => tag(r.approval)],
        ["Updated", r => esc(ts(r.updated_at || r.created_at))]], d.jobs || [], r => `class="clk" data-job="${esc(r.job_id)}"`) + "</div>";
    h += reviewCard(rv);
    return h;
  },
  async topics() {
    const d = await get("/api/topics");
    const kv = (rows, k) => table([[k, r => esc(r[k] ?? "(none)")], ["Count", r => r.n]], rows);
    return `<div class="row">${card("By status", kv(d.by_status || [], "status"))}${card("By class", kv(d.by_class || [], "class"))}${card("Reject reasons", kv(d.reject_reasons || [], "reject_reason"))}</div>` +
      card("Topics", table([["Channel", r => esc(r.channel)], ["Topic", r => esc(r.topic_text)], ["Class", r => esc(r.class)], ["Status", r => tag(r.status)], ["Reject reason", r => esc(r.reject_reason)]], d.topics || [])) +
      card("Trend candidates", table([["Term", r => esc(r.term)], ["Source", r => esc(r.source)], ["Region", r => esc(r.region)], ["Volume", r => esc(r.volume)], ["Day", r => esc(r.day)]], d.candidates || []));
  },
  async gates() {
    const d = await get("/api/gates");
    return card("Rejection rate per channel and gate", table([["Channel", r => esc(r.channel)], ["Gate", r => esc(r.gate)], ["Total", r => r.total], ["Failed", r => r.failed], ["Rate", r => Math.round(r.rate * 100) + "%"]], d.rates || [])) +
      card("Failures", table([["Job", r => esc(r.job_id)], ["Channel", r => esc(r.channel)], ["Gate", r => esc(r.gate)], ["Issues", r => issues(r.issues)]], d.failures || []));
  },
  async publish() {
    const d = await get("/api/publish");
    const jb = await get("/api/jobs?").catch(() => ({}));
    const approved = (jb.jobs || []).filter(r => r.approval === "valid");
    const warn = (t, rows, cols) => rows.length ? `<div class="banner bad">${esc(t)}: ${rows.length}</div>` + table(cols, rows) : `<div class="sub">${esc(t)}: none</div>`;
    return '<div class="banner stop">Dry run only. Real posting is not available from this dashboard.</div>' +
      card("Dry-run publish (approved jobs)", approved.length ? table([["Job", r => esc(r.job_id)], ["Channel", r => esc(r.channel)], ["Approved by", r => esc(r.approved_by)],
        ["Platforms", r => ["youtube", "instagram", "facebook"].map(p => `<label class="note"><input type="checkbox" data-plat="${esc(r.job_id)}" data-p="${p}" ${platOn(r.job_id, p) ? "checked" : ""}> ${p}</label>`).join(" ")],
        ["", r => `<button class="btn" data-dry="${esc(r.job_id)}" data-idle ${isBusy() ? "disabled" : ""}>Dry-run publish</button>`]], approved) : '<div class="sub">no approved jobs</div>') +
      card("Flags", warn("Approved but not published", d.approved_not_published || [], [["Job", r => esc(r.job_id)], ["Channel", r => esc(r.channel)], ["Approved by", r => esc(r.approved_by)]]) +
      warn("Published without valid approval", d.published_without_valid_approval || [], [["Job", r => esc(r.job_id)], ["Platform", r => esc(r.platform)], ["Target", r => esc(r.target)], ["Status", r => esc(r.status)]])) +
      card("Publishes", table([["Job", r => esc(r.job_id)], ["Platform", r => esc(r.platform)], ["Target", r => esc(r.target)], ["Status", r => esc(r.status)], ["Remote id", r => esc(r.remote_id)], ["Updated", r => esc(ts(r.updated_at))]], d.publishes || []));
  },
  async capacity() {
    const d = await get("/api/capacity"), pct = Math.min(100, Math.round(100 * d.today_count / d.cap));
    const sp = d.spend ? `<div>Today $${d.spend.today_usd} / month $${d.spend.month_usd}</div>` + table([["Channel", r => esc(r.channel)], ["USD", r => r.usd], ["Calls", r => r.calls]], d.spend.month_by_channel) : '<div class="sub">no ledger.sqlite llm_spend</div>';
    return (d.warning ? `<div class="banner bad">${esc(d.warning)}</div>` : "") +
      card("Jobs today vs daily cap", `<div>${d.today_count} / ${d.cap} (STUDIO_DAILY_CAP)</div><div class="bar ${d.today_count > d.cap ? "over" : ""}"><span style="width:${pct}%"></span></div>` +
        table([["Channel", r => esc(r.channel)], ["Today", r => r.n]], d.per_channel_today || [])) +
      `<div class="row">${card("Memory pressure", tag(d.memory_pressure, d.memory_pressure) + `<div class="sub">${esc(d.swap)}</div>`)}${card("Free disk", d.disk_free_gb + " GB")}${card("LLM spend", sp)}</div>` +
      card("Jobs per day", table([["Day", r => esc(r.day)], ["Jobs", r => r.n]], d.per_day || []));
  },
  async batches() {
    const d = await get("/api/batches");
    return card("Batches", table([["Name", r => esc(r.name)], ["Status", r => esc(r.status)], ["Mock", r => r.mock ? "yes" : ""], ["Cap", r => esc(r.daily_cap)], ["Jobs", r => r.jobs], ["Config sha", r => esc((r.config_sha256 || "").slice(0, 12))], ["Started", r => esc(ts(r.started_at))]], d.batches || []));
  },
};

async function jobDetail(id) {
  const d = await get("/api/job?id=" + encodeURIComponent(id)), j = d.job;
  if (!j) return card("Job", `<a href="#" id="back">back</a> not found`);
  const sc = d.script;
  const scriptH = sc ? `<pre>${esc(typeof sc === "string" ? sc : JSON.stringify(sc, null, 2))}</pre>` : '<div class="sub">no script</div>';
  return `<div><a href="#" id="back">&larr; all jobs</a></div>` +
    card(j.job_id, `${esc(j.channel)} / ${esc(j.format)} / ${esc(j.status)} / draft ${j.is_draft ? "yes" : "no"} ${tag("approval " + j.approval, j.approval)}
      <div class="sub">${esc(j.topic)}</div><div class="sub">CLI alternative:</div>
      <code id="hint">${esc(d.approve_hint)}</code> <button class="copy" id="copy">Copy</button>`) +
    approveCard(d) +
    card("Stages", `<div class="stages">${d.stages.map(s => `<div class="st ${esc(s.status)}"><b>${esc(s.stage)}</b><i>${esc(s.status)}${s.seconds ? " " + Math.round(s.seconds) + "s" : ""}</i></div>`).join("")}</div>`) +
    card("Gates", table([["Gate", r => esc(r.gate)], ["Result", r => r.passed ? tag("passed", "passed") : tag("failed", "failed")], ["Issues", r => issues(r.issues)]], d.gates)) +
    card("Script", scriptH) +
    card("Assets and credits", table([["Beat", r => r.beat], ["Source", r => esc(r.source)], ["Creator", r => esc(r.creator)], ["Licence", r => esc(r.licence)], ["Page", r => r.page_url && /^https?:/.test(r.page_url) ? `<a href="${esc(r.page_url)}" rel="noopener noreferrer" target="_blank">link</a>` : ""]], d.assets)) +
    (d.video_url ? card("Video", `<video controls src="${esc(d.video_url)}"></video>`) : "");
}

async function render(force) {
  try {
    const m = $("view");
    if (tab === "actions") {
      if (m.dataset.built === "actions" && !force) { updateActions(); return; }
      m.innerHTML = actionsHtml(); m.dataset.built = "actions"; updateActions(); paintConsole();
      $("meta").textContent = "updated " + new Date().toLocaleTimeString(); return;
    }
    m.dataset.built = "";
    const h = await views[tab]();
    const ae = document.activeElement;
    if (!force && ((ae && m.contains(ae) && /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName)) || m.querySelector("video"))) return;
    m.innerHTML = h;
    const first = await get("/api/topics").catch(() => ({}));
    $("empty").hidden = !first.empty;
    $("empty").textContent = first.empty ? first.hint : "";
    $("meta").textContent = "updated " + new Date().toLocaleTimeString() + " (auto-refresh 3s)";
    wire(); refreshControls();
  } catch (e) { $("meta").textContent = "error: " + e; }
}
function wire() {
  document.querySelectorAll("tr[data-job]").forEach(tr => tr.onclick = () => { sel = tr.dataset.job; render(true); });
  const b = $("back"); if (b) b.onclick = e => { e.preventDefault(); sel = null; render(true); };
  const c = $("copy"); if (c) c.onclick = () => navigator.clipboard && navigator.clipboard.writeText($("hint").textContent);
  for (const k of ["channel", "status", "draft"]) { const e = $("f-" + k); if (e) e.onchange = () => { filt[k] = e.value; render(true); }; }
}
document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => {
  tab = b.dataset.t; sel = null; $("view").dataset.built = "";
  document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("on", x === b));
  render(true);
});
live().then(() => render(true));
setInterval(() => { live(); render(); }, 3000);


// ======== interactive layer ========
const platOn = (id, p) => !(ui.plat[id] && ui.plat[id][p] === false);
const tsAny = v => !v ? "" : typeof v === "number" ? new Date(v * (v > 1e12 ? 1 : 1000)).toLocaleString() : new Date(v).toLocaleString();
const pressure = () => cap ? cap.memory_pressure : "unknown";

const LLMS = ["opencode", "claude-code", "mock"];
const ACTIONS = [
  {name: "auto", title: "Auto pipeline", hero: true, desc: "One action: fetch trends, triage, pick, then run today's plan. Never approves or publishes; review and approve afterwards.", fields: [
    {k: "source", label: "Source", type: "select", opts: ["trends", "library", "file"], def: "trends"},
    {k: "region", label: "Region (2 letters)", type: "text", def: "US"},
    {k: "path", label: "File path (source=file, inside output/)", type: "text", def: ""},
    {k: "mock", label: "Mock (no network, no models)", type: "bool", def: true},
    {k: "llm", label: "LLM backend (required unless mock)", type: "select", opts: LLMS, def: "mock"},
    {k: "mock_llm", label: "Mock LLM for triage", type: "bool", def: false},
    {k: "limit", label: "Triage limit (1..1000, optional)", type: "number", def: ""},
    {k: "daily_cap", label: "Daily cap (1..1000, optional)", type: "number", def: ""},
    {k: "per_channel", label: "Per channel (1..50, optional)", type: "number", def: ""},
    {k: "max_jobs", label: "Max jobs (1..1000, optional)", type: "number", def: ""},
    {k: "preview", label: "Preview only (don't render)", type: "bool", def: false}]},
  {name: "track_sync", title: "Track sync", desc: "Sync pipeline job state into the tracking database.", fields: []},
  {name: "topics_fetch", title: "Fetch topics", desc: "Pull trend candidates or library topics.", fields: [
    {k: "source", label: "Source", type: "select", opts: ["trends", "library", "file"], def: "trends"},
    {k: "region", label: "Region", type: "text", def: "US"},
    {k: "path", label: "File path (source=file)", type: "text", def: ""},
    {k: "mock", label: "Mock (no network)", type: "bool", def: true}]},
  {name: "topics_triage", title: "Triage topics", desc: "Classify and safety-triage candidate topics with an LLM.", fields: [
    {k: "limit", label: "Limit (optional)", type: "number", def: ""},
    {k: "llm", label: "LLM backend", type: "select", opts: LLMS, def: "mock"},
    {k: "mock_llm", label: "Mock LLM", type: "bool", def: true}]},
  {name: "topics_pick", title: "Pick topics", desc: "Choose approved topics for the next batch within caps.", fields: [
    {k: "daily_cap", label: "Daily cap (1..1000, optional)", type: "number", def: ""},
    {k: "per_channel", label: "Per channel (optional)", type: "number", def: ""}]},
  {name: "plan_run", title: "Plan and run", desc: "Plan today's batch and run the pipeline.", fields: [
    {k: "date", label: "Date (optional)", type: "date", def: ""},
    {k: "mock", label: "Mock (no models)", type: "bool", def: true},
    {k: "llm", label: "LLM (optional)", type: "select", opts: ["", ...LLMS], def: ""},
    {k: "max_jobs", label: "Max jobs (optional)", type: "number", def: ""}]},
  {name: "run_stop", title: "Stop running action", desc: "Stop the currently running action (its process group).", fields: [], danger: true},
];
const fv = (a, f) => (form[a] && f.k in form[a]) ? form[a][f.k] : f.def;

const AUTO_STEPS = ["fetch", "triage", "pick", "run"];
function actionsHtml() {
  const cards = ACTIONS.map(a => {
    const fs = a.fields.map(f => {
      const id = `a-${a.name}-${f.k}`, v = fv(a.name, f), at = `id="${id}" data-a="${a.name}" data-k="${f.k}"`;
      if (f.type === "bool") return `<div class="field chk"><input type="checkbox" ${at} ${v ? "checked" : ""}><label for="${id}">${esc(f.label)}</label></div>`;
      if (f.type === "select") return `<div class="field"><label for="${id}">${esc(f.label)}</label><select ${at}>${f.opts.map(o => `<option value="${esc(o)}" ${o === v ? "selected" : ""}>${esc(o || "(default)")}</option>`).join("")}</select></div>`;
      return `<div class="field"><label for="${id}">${esc(f.label)}</label><input type="${f.type}" ${at} value="${esc(v)}" ${f.type === "number" ? 'min="1" step="1" inputmode="numeric"' : ""}></div>`;
    }).join("");
    const extra = a.name === "auto" ? `<ol class="steps" id="auto-prog" aria-label="Auto pipeline progress">${AUTO_STEPS.map((n, i) => `<li data-s="${i + 1}"><b>${i + 1}</b> ${n}</li>`).join("")}</ol><div id="auto-sum" role="status" aria-live="polite"></div>` : "";
    return `<div class="card${a.hero ? " hero" : ""}"><h2>${esc(a.title)}</h2><div class="sub">${esc(a.desc)}</div>${a.hero ? '<div class="afields">' + fs + "</div>" : fs}` +
      (a.name === "plan_run" ? '<div class="note" id="cap-plan"></div>' : "") +
      `<div class="note" id="note-${a.name}"></div><div class="btnrow"><button class="btn ${a.danger ? "danger" : ""}" data-run="${a.name}" ${a.danger ? "" : "data-idle"}>${a.danger ? "Stop" : a.name === "auto" ? "Run auto pipeline" : "Run"}</button></div>${extra}</div>`;
  });
  const hero = cards.shift();
  const grid = cards.join("");
  return '<div class="note">Actions run one at a time. Every action is logged and can be refused by the server.</div>' + hero +
    `<div class="card"><h2>Action console</h2><div class="btnrow"><b id="con-name">no action selected</b> <span id="con-status" class="tag" role="status" aria-live="polite"></span>
      <button class="btn danger" data-run="run_stop" id="con-stop" hidden>Stop</button></div>
      <pre id="con" tabindex="0" aria-label="Action log"></pre></div>` +
    `<div class="grid">${grid}</div><div class="card"><h2>Action history</h2><div id="hist"></div></div>`;
}

function buildBody(name) {
  const A = ACTIONS.find(a => a.name === name), body = {};
  for (const f of A.fields) {
    let v = fv(name, f);
    if (f.type === "bool") { body[f.k] = !!v; continue; }
    v = String(v ?? "").trim();
    if (v === "") continue;
    if (f.type === "number") {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1) return {error: f.label + " must be a whole number >= 1"};
      if (f.k === "daily_cap" && n > 1000) return {error: "Daily cap must be 1..1000"};
      body[f.k] = n;
    } else body[f.k] = f.k === "region" ? v.toUpperCase() : v;
  }
  if (name === "topics_fetch") {
    if (!body.region) body.region = "US";
    if (body.source === "file" && !body.path) return {error: "File path is required when source=file"};
    if (body.source !== "file") delete body.path;
  }
  if (name === "auto") {
    body.run = !body.preview; delete body.preview;
    if (body.source === "file" && !body.path) return {error: "File path is required when source=file"};
    if (body.source !== "file") delete body.path;
    if (!body.region) body.region = "US";
    if (!/^[A-Z]{2}$/.test(body.region)) return {error: "Region must be 2 letters"};
    for (const [k, mx] of [["limit", 1000], ["daily_cap", 1000], ["per_channel", 50], ["max_jobs", 1000]])
      if (body[k] > mx) return {error: `${k} must be 1..${mx}`};
  }
  if (name === "plan_run" && body.date && !/^\d{4}-\d{2}-\d{2}$/.test(body.date)) return {error: "Date must be YYYY-MM-DD"};
  return {body};
}
function isReal(name) {
  const g = k => { const f = ACTIONS.find(a => a.name === name).fields.find(x => x.k === k); return fv(name, f); };
  if (name === "plan_run" || name === "auto") return !g("mock");
  if (name === "topics_triage") return !g("mock_llm") && g("llm") !== "mock";
  return false;
}
function noteFor(name) {
  let h = "";
  if (isReal(name)) {
    const p = pressure();
    h += `Real run: runs under the memory guard and is refused when memory pressure is not green. Current pressure: ${tag(p, p)}` +
      (p !== "green" ? ' <span class="note warn">It would be refused right now.</span>' : "");
  }
  if (name === "auto") {
    const dc = Number(form.auto && form.auto.daily_cap);
    if (dc > 100) h += `<div class="note warn">Daily cap ${dc} is above 100: platform limits and mass-production policy risk still apply.</div>`;
    else if (!dc && cap && cap.cap > 100) h += `<div class="note warn">Warning: the current daily cap (${cap.cap}) is above 100. Platform limits and mass-production policy risk apply.</div>`;
    if (form.auto && form.auto.preview) h += '<div class="note">Preview: fetches, triages and picks only. Nothing is rendered.</div>';
  }
  if (name === "topics_pick") {
    const n = Number(form.topics_pick && form.topics_pick.daily_cap);
    if (n > 100) h += `<div class="note warn">Daily cap ${n} is above 100: platform limits and mass-production policy risk still apply.</div>`;
  }
  if (name === "plan_run" && cap && cap.cap > 100) h += `<div class="note warn">Warning: the current daily cap (${cap.cap}) is above 100. Platform limits and mass-production policy risk apply.</div>`;
  return h;
}
function updateActions() {
  if (tab !== "actions") return;
  for (const a of ACTIONS) { const n = $("note-" + a.name); if (n) { const h = noteFor(a.name); if (n.innerHTML !== h) n.innerHTML = h; } }
  paintAuto();
  const cp = $("cap-plan");
  if (cp) cp.textContent = cap ? `Current daily cap: ${cap.cap}. Jobs created today: ${cap.today_count}.` : "Daily cap: unknown (capacity not available).";
  const h = $("hist");
  if (h) {
    const html = actions.length ? table([["Log", r => `<button class="btn ghost" data-hist="${esc(r.id)}" data-name="${esc(r.name)}">View</button>`], ["Id", r => esc(r.id)], ["Action", r => esc(r.name)],
      ["Status", r => tag(r.status)], ["Reason", r => esc(r.refused_reason)], ["Created", r => esc(tsAny(r.created_at))], ["Ended", r => esc(tsAny(r.ended_at))], ["Exit", r => esc(r.exit_code)], ["Actor", r => esc(r.actor)]], actions) : '<div class="sub">no actions yet (or backend not available yet)</div>';
    if (h.dataset.sig !== html) { h.innerHTML = html; h.dataset.sig = html; }
  }
  refreshControls();
}
function paintAuto() {
  const prog = $("auto-prog"); if (!prog) return;
  const mine = track.name === "auto" && track.text;
  const txt = mine ? track.text : "";
  let max = 0; for (const m of txt.matchAll(/\[auto\] step (\d)\/4 /g)) max = Math.max(max, +m[1]);
  track.autoMax = Math.max(track.autoMax || 0, mine ? max : 0); max = mine ? track.autoMax : 0;
  const st = mine ? track.status : "", fin = TERMINAL.includes(st), bad = st === "failed" || st === "stopped";
  prog.querySelectorAll("li").forEach(li => {
    const n = +li.dataset.s;
    const cls = n < max || (fin && !bad && n <= max) ? "done" : n === max ? (bad ? "bad" : fin ? "done" : "act") : "";
    if (li.className !== cls) li.className = cls;
    li.setAttribute("aria-current", cls === "act" ? "step" : "false");
  });
  const i = txt.indexOf("[auto] ===== summary ====="), box = $("auto-sum");
  let html = "";
  if (i >= 0) {
    const lines = txt.slice(i).split("\n").map(x => x.trim());
    const rows = lines.filter(x => /^\[auto\] [^=].*: /.test(x)).map(x => x.slice(7).split(/: (.*)/s));
    const next = lines.find(x => x.startsWith("Next:")) || "";
    html = `<div class="summary"><h3>Summary</h3><dl>${rows.map(r => `<dt>${esc(r[0])}</dt><dd>${esc(r[1])}</dd>`).join("")}</dl>` +
      (next ? `<div class="note">${esc(next)}</div>` : "") + "</div>";
  } else if (fin && track.reason) html = `<div class="summary bad"><h3>Refused</h3><div>${esc(track.reason)}</div></div>`;
  if (box.innerHTML !== html) box.innerHTML = html;
}
function refreshControls() {
  const busy = isBusy();
  document.querySelectorAll("[data-idle]").forEach(b => { b.disabled = busy; b.title = busy ? "Another action is running; one at a time" : ""; });
  const st = $("con-stop"); if (st) st.hidden = !(track.status === "running" || track.status === "queued");
  // single-job approve panel
  const ap = $("ap-approve");
  if (ap) {
    const c = ui.cur, ok = c && c.eligible && !busy && ui.approver.trim() && (ui.confirm[c.id] || "").trim() === c.prefix;
    ap.disabled = !ok;
    const rj = $("ap-reject"); if (rj) rj.disabled = !(ui.approver.trim() && !busy);
  }
  // bulk approve
  const bk = $("bk-approve");
  if (bk) {
    const n = ui.sel.size, viewedAll = ui.sample.length > 0 && ui.sample.every(i => ui.reported[i]);
    bk.disabled = !(n > 0 && viewedAll && !busy && ui.approver.trim() && ui.bulkConfirm === "APPROVE " + n);
    const bs = $("bk-status");
    if (bs) bs.textContent = n === 0 ? "Select jobs to approve." : !ui.sample.length ? `Pick a sample from the ${n} selected job(s) and watch each for at least 5 seconds.`
      : !viewedAll ? `Sample not fully viewed: ${ui.sample.filter(i => ui.reported[i]).length} / ${ui.sample.length}.` : `Sample viewed. Type APPROVE ${n} to enable approval.`;
    const bn = $("bk-count"); if (bn) bn.textContent = n;
  }
}

// ---- console ----
function setTrack(id, name) { track = {id, name: name || "", status: "", text: "", done: false, autoMax: 0}; pollConsole(); }
async function pollConsole() {
  if (!track.id || track.done) return;
  const id = track.id;
  const [s, l] = await Promise.all([getJ("/api/actions/" + encodeURIComponent(id)), getJ("/api/actions/" + encodeURIComponent(id) + "/log?tail=1000")]);
  if (track.id !== id) return;
  if (s.status === 404 && l.status === 404) { track.text = "backend not available yet"; track.done = true; paintConsole(); return; }
  if (s.ok && s.data) { track.status = s.data.status || track.status; track.name = s.data.name || track.name; track.reason = s.data.refused_reason; }
  if (l.ok && l.data) track.text = Array.isArray(l.data.lines) ? l.data.lines.join("\n") : (l.data.text || "");
  if (TERMINAL.includes(track.status)) { track.done = true; await refreshActions(); await refreshCap(); paintStatusBar(); updateActions(); }
  paintConsole();
}
function paintConsole() {
  const c = $("con"); if (!c) return;
  const near = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
  const txt = (track.text || "") + (track.reason ? "\nrefused: " + track.reason : "");
  if (c.textContent !== txt) { c.textContent = txt; if (near) c.scrollTop = c.scrollHeight; }
  paintAuto();
  $("con-name").textContent = track.id ? `${track.name || "action"} (${track.id})` : "no action selected";
  const p = $("con-status"); p.textContent = track.status || ""; p.className = "tag " + (track.status || "");
  refreshControls();
}
setInterval(pollConsole, 2000);

// ---- running actions ----
async function runAction(name) {
  if (name !== "run_stop" && isBusy()) { flash("Another action is running. One at a time.", "bad"); return; }
  const b = buildBody(name);
  if (b.error) { flash(b.error, "bad"); return; }
  if (isReal(name) && !confirm("Real (non-mock) run. It runs under the memory guard. Continue?")) return;
  await submit(name, "/api/actions/" + name, b.body);
}
async function submit(name, url, body) {
  busyLocal = name !== "run_stop"; refreshControls();
  const r = await post(url, body);
  busyLocal = false;
  if (!r.ok) { flash(`${name}: ${errText(r.status, r.data)}`, "bad"); await refreshActions(); refreshControls(); return false; }
  const id = r.data && r.data.id;
  flash(`${name}: ${(r.data && r.data.status) || "accepted"}${id ? " (" + id + ")" : ""}`, "ok");
  if (id) { actions.unshift({id, name, status: r.data.status || "queued", created_at: Date.now() / 1000}); setTrack(id, name); }
  paintStatusBar(); updateActions();
  return true;
}

// ---- approve panel (single job) ----
function approveCard(d) {
  const j = d.job, gates = d.gates || [];
  const why = [];
  if (j.status !== "rendered") why.push("status is " + j.status + " (must be rendered)");
  if (j.is_draft) why.push("job is a draft");
  if (j.status === "failed") why.push("job failed");
  if (!gates.length) why.push("no gate results");
  if (gates.some(g => !g.passed)) why.push("a gate failed");
  if (j.approval === "valid" || j.approved_hash) why.push("already approved");
  if (!j.content_hash) why.push("no content hash");
  const eligible = why.length === 0, hash = j.content_hash || "", prefix = hash.slice(0, 12);
  ui.cur = {id: j.job_id, prefix, eligible};
  const v = esc(ui.confirm[j.job_id] || "");
  return `<div class="card"><h2>Approve</h2>` +
    (eligible ? "" : `<div class="note warn">Approval is not available: ${esc(why.join("; "))}.</div>`) +
    `<div class="hash">Content hash: <b>${esc(prefix)}</b>${esc(hash.slice(12))}</div>
    <div class="btnrow"><button class="copy" data-copy="${esc(prefix)}" ${prefix ? "" : "disabled"}>Copy first 12 chars</button></div>
    <div class="note">Clicking Approve records <b>you</b>, under the name below, as the human approver of exactly this content hash. It does not publish anything. Type the first 12 characters of the hash to confirm.</div>
    <div class="field"><label for="ap-confirm">Confirmation (first 12 characters of hash)</label><input id="ap-confirm" data-bind="confirm" autocomplete="off" spellcheck="false" value="${v}" ${eligible ? "" : "disabled"}></div>
    <div class="field"><label for="ap-name">Approver name (you)</label><input id="ap-name" data-bind="approver" autocomplete="name" value="${esc(ui.approver)}"></div>
    <div class="btnrow"><button class="btn" id="ap-approve" data-idle disabled>Approve as me</button><button class="btn danger" id="ap-reject" data-idle disabled>Reject</button></div></div>`;
}

// ---- review queue (bulk) ----
function reviewCard(rv) {
  const jobs = rv.jobs || []; ui.reviewIds = jobs.map(r => r.job_id);
  for (const id of [...ui.sel]) if (!ui.reviewIds.includes(id)) ui.sel.delete(id);
  const t = jobs.length ? table([["", r => `<input type="checkbox" data-sel="${esc(r.job_id)}" aria-label="Select ${esc(r.job_id)}" ${ui.sel.has(r.job_id) ? "checked" : ""}>`],
    ["Job", r => esc(r.job_id)], ["Channel", r => esc(r.channel)], ["Topic", r => esc(r.topic)], ["Hash", r => `<span class="hash">${esc((r.content_hash || "").slice(0, 12))}</span>`]], jobs) : '<div class="sub">none</div>';
  const sample = ui.sample.map(id => `<div><div class="sub">${esc(id)}</div>` +
    (ui.sampleUrls[id] ? `<video controls preload="metadata" data-vid="${esc(id)}" src="${esc(ui.sampleUrls[id])}"></video>` : '<div class="note warn">no video available</div>') +
    `<div class="vp ${ui.reported[id] ? "done" : ""}" id="vp-${esc(id)}">${ui.reported[id] ? "viewed" : Math.floor(ui.viewed[id] || 0) + " / 5 s"}</div></div>`).join("");
  return card("Review queue", `<div class="sub">${esc(rv.note || "")} Jobs that passed every gate and are not yet approved.</div>` + t +
    `<div class="btnrow"><button class="btn ghost" id="bk-all">Select all</button><button class="btn ghost" id="bk-none">Clear</button>
      <button class="btn" id="bk-pick" data-idle>Pick sample</button><span class="note">Selected: <b id="bk-count">${ui.sel.size}</b></span></div>
    ${sample ? `<div class="samplegrid">${sample}</div>` : ""}
    <div class="note">Approving records <b>you</b> as the approver of every selected job's content hash. It does not publish.</div>
    <div class="field"><label for="bk-name">Approver name (you)</label><input id="bk-name" data-bind="approver" value="${esc(ui.approver)}" autocomplete="name"></div>
    <div class="field"><label for="bk-confirm">Type exactly: APPROVE ${ui.sel.size}</label><input id="bk-confirm" data-bind="bulkConfirm" autocomplete="off" spellcheck="false" value="${esc(ui.bulkConfirm)}"></div>
    <div class="btnrow"><button class="btn" id="bk-approve" data-idle disabled>Approve selected</button></div><div class="note" id="bk-status" role="status" aria-live="polite"></div>`);
}
async function pickSample() {
  const ids = [...ui.sel];
  if (!ids.length) { flash("Select at least one job first.", "bad"); return; }
  const r = await getJ("/api/review/sample?ids=" + encodeURIComponent(ids.join(",")));
  if (!r.ok) { flash("sample: " + errText(r.status, r.data), "bad"); return; }
  ui.sample = (r.data && r.data.sample || []).filter(i => ids.includes(i));
  ui.viewed = {}; ui.reported = {}; ui.sampleUrls = {};
  await Promise.all(ui.sample.map(async id => { const d = await getJ("/api/job?id=" + encodeURIComponent(id)); ui.sampleUrls[id] = d.ok && d.data ? d.data.video_url : null; }));
  flash(`Sample of ${ui.sample.length} picked. Watch each video for at least 5 seconds.`, "ok");
  render(true);
}
const pendingRep = new Set();
async function onTime(e) {
  const v = e.target, id = v && v.dataset && v.dataset.vid; if (!id) return;
  const t = v.currentTime, last = v._last === undefined ? t : v._last; v._last = t;
  const d = t - last;
  if (!v.paused && !v.seeking && d > 0 && d < 1.5) ui.viewed[id] = (ui.viewed[id] || 0) + d;
  const el = $("vp-" + id), sec = ui.viewed[id] || 0;
  if (el && !ui.reported[id]) el.textContent = Math.floor(sec) + " / 5 s";
  if (sec >= 5 && !ui.reported[id] && !pendingRep.has(id)) {
    pendingRep.add(id);
    const r = await post("/api/review/viewed", {job_id: id, seconds: Math.floor(sec)});
    pendingRep.delete(id);
    if (r.ok) { ui.reported[id] = true; if (el) { el.textContent = "viewed"; el.className = "vp done"; } }
    else flash("viewed: " + errText(r.status, r.data), "bad");
    refreshControls();
  }
}
document.addEventListener("timeupdate", onTime, true);

// ---- delegated events ----
document.addEventListener("click", async e => {
  const t = e.target.closest("button,input"); if (!t) return;
  if (t.dataset.run) return runAction(t.dataset.run);
  if (t.dataset.hist) { setTrack(t.dataset.hist, t.dataset.name); return; }
  if (t.dataset.copy) { try { await navigator.clipboard.writeText(t.dataset.copy); flash("Copied", "ok"); } catch (x) { flash("Copy failed", "bad"); } return; }
  if (t.id === "bk-all") { ui.sel = new Set(ui.reviewIds || []); ui.sample = []; return render(true); }
  if (t.id === "bk-none") { ui.sel = new Set(); ui.sample = []; return render(true); }
  if (t.id === "bk-pick") return pickSample();
  if (t.id === "ap-approve") {
    const c = ui.cur; if (!c) return;
    if (await submit("job_approve", "/api/actions/job_approve", {job_id: c.id, confirm_prefix: (ui.confirm[c.id] || "").trim(), approver: ui.approver.trim()})) ui.confirm[c.id] = "";
    return;
  }
  if (t.id === "ap-reject") {
    const c = ui.cur; if (!c || !confirm("Reject " + c.id + " as " + ui.approver.trim() + "?")) return;
    return submit("job_reject", "/api/actions/job_reject", {job_id: c.id, approver: ui.approver.trim()});
  }
  if (t.id === "bk-approve") {
    const ids = [...ui.sel], n = ids.length;
    if (!confirm(`Approve ${n} job(s) as ${ui.approver.trim()}?`)) return;
    if (await submit("bulk_approve", "/api/actions/bulk_approve", {job_ids: ids, approver: ui.approver.trim(), confirm: "APPROVE " + n, sample_viewed: ui.sample.filter(i => ui.reported[i])})) {
      ui.sel = new Set(); ui.sample = []; ui.bulkConfirm = ""; ui.viewed = {}; ui.reported = {};
    }
    return;
  }
  if (t.dataset.dry) {
    const id = t.dataset.dry, pl = ["youtube", "instagram", "facebook"].filter(p => platOn(id, p));
    if (!pl.length) { flash("Choose at least one platform.", "bad"); return; }
    return submit("publish_dry_run", "/api/actions/publish_dry_run", {job_id: id, platforms: pl.map(p => ({youtube: "youtube_shorts", instagram: "instagram_reels", facebook: "facebook_reels"})[p])});
  }
});
function onField(e) {
  const t = e.target;
  if (t.dataset.a) { (form[t.dataset.a] = form[t.dataset.a] || {})[t.dataset.k] = t.type === "checkbox" ? t.checked : t.value; updateActions(); return; }
  if (t.dataset.sel) { t.checked ? ui.sel.add(t.dataset.sel) : ui.sel.delete(t.dataset.sel); ui.sample = []; if (e.type === "change") render(true); return; }
  if (t.dataset.plat) { (ui.plat[t.dataset.plat] = ui.plat[t.dataset.plat] || {})[t.dataset.p] = t.checked; return; }
  const b = t.dataset.bind; if (!b) return;
  if (b === "approver") { ui.approver = t.value; LS.set("dash.approver", t.value); document.querySelectorAll('[data-bind="approver"]').forEach(x => { if (x !== t) x.value = t.value; }); }
  else if (b === "bulkConfirm") ui.bulkConfirm = t.value;
  else if (b === "confirm" && ui.cur) ui.confirm[ui.cur.id] = t.value;
  refreshControls();
}
document.addEventListener("input", onField);
document.addEventListener("change", onField);
