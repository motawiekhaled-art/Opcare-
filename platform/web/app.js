/* Opcare DAQ – live values screen. Plain JavaScript, no build step.
   Data: REST /api/* with a bearer token, live updates from /api/live (Server-Sent Events). */
(() => {
  "use strict";
  const QUALITY = {192: "Good", 216: "Good (forced)", 64: "Uncertain", 68: "Uncertain (old)", 80: "Uncertain (inaccurate)",
    84: "Uncertain (over range)", 0: "Bad", 4: "Bad (config)", 12: "Bad (device failure)", 24: "Bad (no comms)", 28: "Bad (out of service)"};
  const qClass = (q) => (q === null || q === undefined) ? "bad" : q >= 192 ? "good" : q >= 64 ? "uncertain" : "bad";
  const STATE_LABEL = {running: "Running", degraded: "Degraded", error: "Error", offline: "Offline", connecting: "Connecting",
    waiting_config: "Waiting config", starting: "Starting", stopping: "Stopped", unknown: "No status"};
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

  let token = null;
  const tags = new Map();        // tag_id -> {meta, v, q, ts, row}
  const sources = new Map();
  let selected = null, chartData = [], es = null, received = 0, lastRateCheck = Date.now();
  let loading = false, buffered = [];   // live messages that arrive while a snapshot is loading

  // ------------------------------------------------------------------ auth
  function initToken() {
    const url = new URL(location.href);
    const fromUrl = url.searchParams.get("token");
    if (fromUrl) { try { sessionStorage.setItem("opcare-token", fromUrl); } catch (e) {} url.searchParams.delete("token"); history.replaceState(null, "", url); }
    try { token = fromUrl || sessionStorage.getItem("opcare-token"); } catch (e) { token = fromUrl; }
  }
  async function api(path, opts = {}) {
    const res = await fetch(path, {...opts, headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json", ...(opts.headers || {})}});
    if (res.status === 401) { showLogin(); throw new Error("unauthorized"); }
    const body = res.status === 204 ? null : await res.json();
    if (!res.ok && res.status !== 502) throw Object.assign(new Error(body?.error?.message || res.statusText), {body});
    return body;
  }
  function showLogin(failed) {
    $("login").hidden = false; $("login-error").hidden = !failed; $("token").focus();
    if (!failed) fetch("/api/health").then((r) => r.json()).then((h) => {   // demo only: offer the demo token
      if (h.login_hint && !$("token").value) { $("token").value = h.login_hint; $("login-hint").hidden = false; }
    }).catch(() => {});
  }
  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    token = $("token").value.trim();
    try { const me = await api("/api/me"); try { sessionStorage.setItem("opcare-token", token); } catch (e) {} $("login").hidden = true; start(me); }
    catch (e) { showLogin(true); }
  });
  $("signout").addEventListener("click", () => { try { sessionStorage.removeItem("opcare-token"); } catch (e) {} if (es) es.close(); location.reload(); });

  // ------------------------------------------------------------------ sources
  function renderSource(s) {
    let card = sources.get(s.source_id)?.card;
    if (!card) { card = el("article", "source"); $("source-list").appendChild(card); }
    sources.set(s.source_id, {...s, card});
    card.replaceChildren();
    const st = s.status || {};
    const state = s.state || "unknown";
    const head = el("div", "source-head");
    const left = el("div");
    left.append(el("div", "source-name", s.name), el("div", "source-meta", `${s.type.toUpperCase()} · ${s.site} · rev ${s.revision}`));
    head.append(left, el("span", `chip st-${state}`, STATE_LABEL[state] || state));
    card.append(head);
    if (st.devices?.length) {
      const devs = el("div", "devices");
      st.devices.forEach((d) => { const x = el("span", `device ${d.state}`); x.append(el("span", "dot"), document.createTextNode(`${d.id}`)); x.title = d.error ? `${d.state}: ${d.error.message}` : d.state; devs.append(x); });
      card.append(devs);
    }
    if (st.stats) {
      const stats = el("div", "source-stats");
      [["points", st.stats.points], ["values/s", st.stats.values_per_s], ["batches", st.stats.batches_total]].forEach(([k, v]) => {
        const s2 = el("span"); s2.append(el("b", null, String(v ?? "–")), document.createTextNode(` ${k}`)); stats.append(s2); });
      card.append(stats);
    }
    if (st.last_error && state !== "running") card.append(el("div", "err-line", `${st.last_error.code}: ${st.last_error.message}`));
    const btn = el("button", "btn", "Test connection"); btn.type = "button";
    btn.addEventListener("click", () => testConnection(s.source_id, btn));
    card.append(btn);
  }
  async function loadSources() {
    const list = await api("/api/sources");
    $("source-list").replaceChildren();
    sources.clear();
    if (!list.length) $("source-list").append(el("p", "muted", "No sources configured yet."));
    list.forEach(renderSource);
  }
  async function testConnection(sid, btn) {
    btn.disabled = true; btn.textContent = "Testing…";
    const body = $("t-body"); body.replaceChildren();
    try {
      const res = await api(`/api/sources/${encodeURIComponent(sid)}/admin/test_connection`, {method: "POST", body: "{}"});
      if (!res.ok) body.append(el("p", "error", `${res.error.code}: ${res.error.message}`));
      else res.result.devices.forEach((d) => {
        const r = el("div", "result");
        r.append(el("span", null, d.id), el("span", `q ${d.ok ? "q-good" : "q-bad"}`, d.ok ? `OK · ${d.latency_ms} ms` : `${d.error?.code || "FAILED"}`));
        body.append(r);
      });
    } catch (e) { body.append(el("p", "error", e.message)); }
    $("t-title").textContent = `Test connection · ${sid}`;
    $("test-modal").hidden = false; $("t-close").focus();
    btn.disabled = false; btn.textContent = "Test connection";
  }
  $("t-close").addEventListener("click", () => { $("test-modal").hidden = true; });

  // ------------------------------------------------------------------ tags
  function fmtValue(v, dt) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "ON" : "OFF";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-US") : v.toLocaleString("en-US", {maximumFractionDigits: 3});
    return String(v);
  }
  const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString("en-GB", {hour12: false}) + "." + iso.slice(20, 23) : "—";
  function fmtAge(iso) {
    if (!iso) return "—";
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    return s < 60 ? `${s.toFixed(0)} s` : s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h`;
  }
  function makeRow(t) {
    const tr = el("tr");
    tr.tabIndex = 0;
    const c1 = el("td"); c1.append(el("div", "tag-id", t.tag_id.replaceAll(".", ".\u200B")), el("div", "tag-src", t.source_id));  // break lines at dots
    const c2 = el("td", "num"); const c3 = el("td"); const c4 = el("td", "time"); const c5 = el("td", "num time");
    tr.append(c1, c2, c3, c4, c5);
    tr.addEventListener("click", () => select(t.tag_id));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") select(t.tag_id); });
    return tr;
  }
  function paintRow(t, flash) {
    const [, c2, c3, c4, c5] = t.row.children;
    c2.replaceChildren(el("span", "val", fmtValue(t.v, t.data_type)));
    if (t.unit && t.v !== null && t.v !== undefined) c2.firstChild.append(el("span", "unit", t.unit));
    const cls = qClass(t.q);
    c3.replaceChildren(el("span", `q q-${cls}`, QUALITY[t.q] ?? (t.q === null ? "No data" : `Code ${t.q}`)));
    c3.firstChild.title = `Quality code ${t.q}${t.f ? " · " + t.f.join(", ") : ""}`;
    c4.textContent = fmtTime(t.ts); c5.textContent = fmtAge(t.ts);
    if (flash) { t.row.classList.remove("flash"); void t.row.offsetWidth; t.row.classList.add("flash"); }
  }
  async function loadTags() {
    const list = await api("/api/tags");
    const body = $("tag-rows"); body.replaceChildren(); tags.clear();
    list.forEach((t) => { t.row = makeRow(t); tags.set(t.tag_id, t); paintRow(t, false); body.append(t.row); });
    applyFilter(); updateKpis();
  }
  function applyFilter() {
    const f = $("filter").value.trim().toLowerCase(), qf = $("quality-filter").value;
    let shown = 0;
    tags.forEach((t) => { const ok = (!f || t.tag_id.includes(f) || t.source_id.includes(f)) && (!qf || qClass(t.q) === qf); t.row.hidden = !ok; shown += ok; });
    $("no-tags").hidden = shown > 0;
  }
  $("filter").addEventListener("input", applyFilter);
  $("quality-filter").addEventListener("change", applyFilter);
  function updateKpis() {
    let good = 0, bad = 0; tags.forEach((t) => { const c = qClass(t.q); if (c === "good") good++; else if (c === "bad") bad++; });
    $("k-tags").textContent = tags.size.toLocaleString("en-US");
    $("k-good").textContent = tags.size ? `${Math.round(100 * good / tags.size)}%` : "–";
    $("k-bad").textContent = String(bad);
  }

  // ------------------------------------------------------------------ history chart
  async function select(tagId) {
    selected = tagId;
    tags.forEach((t) => t.row.classList.toggle("selected", t.tag_id === tagId));
    const t = tags.get(tagId);
    $("detail").hidden = false;
    $("d-title").textContent = tagId;
    $("d-sub").textContent = `${t.source_id} · ${t.data_type}${t.unit ? " · " + t.unit : ""}`;
    chartData = await api(`/api/tags/${encodeURIComponent(tagId)}/history?minutes=${$("d-range").value}&limit=5000`);
    drawChart();
  }
  $("d-range").addEventListener("change", () => selected && select(selected));
  $("d-close").addEventListener("click", () => { selected = null; $("detail").hidden = true; tags.forEach((t) => t.row.classList.remove("selected")); });
  function drawChart() {
    const box = $("chart"); box.replaceChildren();
    const pts = chartData.filter((p) => typeof p.v === "number");
    if (pts.length < 2) { box.append(el("p", "muted", "Not enough numeric history yet.")); return; }
    const W = 800, H = 220, L = 56, R = 12, T = 12, B = 28, NS = "http://www.w3.org/2000/svg";
    const xs = pts.map((p) => Date.parse(p.ts)), ys = pts.map((p) => p.v);
    let y0 = Math.min(...ys), y1 = Math.max(...ys); if (y0 === y1) { y0 -= 1; y1 += 1; }
    const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const X = (x) => L + (x - x0) / Math.max(1, x1 - x0) * (W - L - R), Y = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
    const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none");
    const add = (name, attrs, text) => { const n = document.createElementNS(NS, name); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); if (text) n.textContent = text; svg.append(n); return n; };
    for (let i = 0; i <= 4; i++) {
      const yv = y0 + (y1 - y0) * i / 4, yy = Y(yv);
      add("line", {x1: L, x2: W - R, y1: yy, y2: yy, stroke: "#EEF1F4"});
      add("text", {x: L - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: "#555F6B"}, yv.toLocaleString("en-US", {maximumFractionDigits: 2}));
    }
    [x0, (x0 + x1) / 2, x1].forEach((x, i) => add("text", {x: X(x), y: H - 8, "text-anchor": ["start", "middle", "end"][i], "font-size": 11, fill: "#555F6B"}, new Date(x).toLocaleTimeString("en-GB", {hour12: false})));
    add("polyline", {points: pts.map((p, i) => `${X(xs[i]).toFixed(1)},${Y(p.v).toFixed(1)}`).join(" "), fill: "none", stroke: "#0B57B8", "stroke-width": 2, "vector-effect": "non-scaling-stroke"});
    pts.forEach((p, i) => { if (p.q < 192) add("circle", {cx: X(xs[i]), cy: Y(p.v), r: 3, fill: p.q < 64 ? "#A61B1B" : "#8A4B00"}); });
    $("chart").setAttribute("aria-label", `History of ${selected}: ${pts.length} points from ${ys.length ? Math.min(...ys) : ""} to ${ys.length ? Math.max(...ys) : ""}`);
    box.append(svg);
  }

  // ------------------------------------------------------------------ events
  function addEvent(ev, top = true) {
    const li = el("li", "event");
    li.append(el("span", `sev sev-${ev.severity}`));
    const body = el("div");
    body.append(el("div", "event-msg", ev.message), el("div", "event-meta", `${fmtTime(ev.ts)} · ${ev.type} · ${ev.src}`));
    li.append(body);
    const list = $("event-list");
    if (top) list.prepend(li); else list.append(li);
    while (list.children.length > 200) list.lastChild.remove();
  }
  async function loadEvents() { $("event-list").replaceChildren(); (await api("/api/events?limit=100")).forEach((e) => addEvent(e, false)); }

  // ------------------------------------------------------------------ live stream
  function setLive(state, text) { $("live").dataset.state = state; $("live-text").textContent = text; }
  function connectLive() {
    if (es) es.close();
    es = new EventSource(`/api/live?token=${encodeURIComponent(token)}`);
    // On every (re)connect: buffer live messages, load a fresh snapshot, then replay the buffer.
    // The platform stores values before announcing them, so nothing falls between snapshot and stream.
    es.addEventListener("hello", async () => { await refresh(); setLive("live", "Live"); });
    es.onerror = () => setLive("down", "Reconnecting…");
    es.onmessage = (m) => { const msg = JSON.parse(m.data); if (loading) buffered.push(msg); else handle(msg); };
  }
  async function refresh() {
    loading = true; buffered = [];
    try { await Promise.all([loadSources(), loadTags()]); } finally {
      loading = false; const replay = buffered; buffered = []; replay.forEach(handle);
    }
  }
  // the configuration screens announce saved/deleted sources so the live view picks up new tags
  window.addEventListener("opcare:reload", () => { if (token) refresh(); });
  function handle(msg) {
    {
      if (msg.type === "values") {
        received += msg.values.length;
        msg.values.forEach((v) => {
          const t = tags.get(v.t); if (!t) return;
          if (t.ts && v.ts < t.ts) return;
          Object.assign(t, {v: v.v, q: v.q, ts: v.ts, f: v.f}); paintRow(t, true);
          if (v.t === selected && typeof v.v === "number") { chartData.push(v); drawChart(); }
        });
        applyFilter(); updateKpis();
      } else if (msg.type === "status") {
        const s = sources.get(msg.source_id);
        if (!s && !loading && msg.state !== "stopping") { refresh(); return; }   // a source we have not seen yet
        if (s) renderSource({...s, state: msg.state, status: msg.status, revision: msg.status.config_revision ?? s.revision});
      } else if (msg.type === "event") {
        addEvent(msg.event);
      }
    }
  }
  setInterval(() => {
    tags.forEach((t) => { if (!t.row.hidden) t.row.children[4].textContent = fmtAge(t.ts); });
    const now = Date.now(); $("k-rate").textContent = `${Math.round(received / ((now - lastRateCheck) / 1000))}/s`;
    received = 0; lastRateCheck = now;
  }, 1000);

  async function start(me) {
    $("user").textContent = `${me.user} · ${me.role}`;
    await loadEvents();
    connectLive();            // sources and tags load on the stream's hello, then live updates apply
  }
  initToken();
  if (!token) showLogin(); else api("/api/me").then(start).catch(() => showLogin(true));
})();
