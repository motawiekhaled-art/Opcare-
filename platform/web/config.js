/* Opcare DAQ – source configuration screens (Opcare GUI design: source type → protocol → connection → tags).
   Views: #sources (list), #new-source, #edit-<source id>. Plain JavaScript, no build step.
   Hashes are plain names (no "#/path") so the page also works where only simple fragments are kept. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ constants
  const ICONS = {   // same drawings as the Opcare design canvas
    plc: "M5 4h14v16H5z M9 8h6 M9 12h6 M9 16h2 M2 8h3 M2 16h3 M19 8h3 M19 16h3",
    iot: "M11.5 13a.5.5 0 1 0 1 0 .5.5 0 1 0-1 0 M8.5 9.5a5 5 0 0 1 7 0 M5.5 6.5a9 9 0 0 1 13 0 M12 14v7 M9 21h6",
    energy: "M13 2 4 14h7l-1 8 9-12h-7z",
    bms: "M4 21V5l8-3v19 M12 9h8v12 M2 21h20 M7 8h2 M7 12h2 M7 16h2 M15 13h2 M15 17h2",
    med: "M3 12h4l2-5 3 10 2-5h7 M17 3v5 M14.5 5.5h5",
    db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    api: "M8 8l-5 4 5 4 M16 8l5 4-5 4 M14 4l-4 16",
    file: "M14 3H6v18h12V7z M14 3v4h4 M9 13h6 M9 17h6",
    cam: "M3 7h12v10H3z M15 10l6-3v10l-6-3 M7 11h.01",
  };
  const STATUS_LABEL = {available: "Available", specified: "In development", planned: "Planned"};
  const STATE_LABEL = {running: "Running", degraded: "Degraded", error: "Error", offline: "Offline", connecting: "Connecting",
    waiting_config: "Disabled", starting: "Starting", stopping: "Stopped", unknown: "No status"};
  const REG_ENC = ["float32", "uint16", "int16", "uint32", "int32", "float64", "uint64", "int64", "bit", "string"];
  const ENC_BY_TABLE = {holding_register: REG_ENC, input_register: REG_ENC, coil: ["bool"], discrete_input: ["bool"]};
  const TABLE_LABEL = {holding_register: "Holding register", input_register: "Input register", coil: "Coil", discrete_input: "Discrete input"};
  const dataTypeFor = (enc) => enc === "bool" || enc === "bit" ? "bool" : enc === "string" ? "string" : enc.startsWith("float") ? "float" : "int";
  const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
  const ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/, TAG_RE = /^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$/;
  const STEPS = ["Source & protocol", "Connection", "Tags", "Review & save"];

  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === false) return;
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "value") e.value = v;
      else e.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((c) => { if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : String(c)); });
    return e;
  }
  function icon(id, size = 20) {
    const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg"), p = document.createElementNS(NS, "path");
    [["width", size], ["height", size], ["viewBox", "0 0 24 24"], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "1.8"],
      ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"]].forEach(([k, v]) => s.setAttribute(k, v));
    p.setAttribute("d", ICONS[id] || ICONS.plc); s.append(p); return s;
  }
  const token = () => { try { return sessionStorage.getItem("opcare-token"); } catch (e) { return null; } };
  async function api(path, method = "GET", body) {
    const res = await fetch(path, {method, headers: {"Authorization": `Bearer ${token()}`, "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null; try { data = res.status === 204 ? null : await res.json(); } catch (e) { data = null; }
    return {status: res.status, ok: res.ok, data};
  }
  function toast(msg, kind = "ok") {
    const t = $("toast"); t.textContent = msg; t.dataset.kind = kind; t.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 5000);
  }
  const reloadLive = () => window.dispatchEvent(new Event("opcare:reload"));
  const errText = (r) => r.data?.error ? `${r.data.error.message}${r.data.error.details ? ": " + [].concat(r.data.error.details).slice(0, 5).join("; ") : ""}` : `HTTP ${r.status}`;

  let catalog = null, me = null;
  async function ensureCatalog() {
    if (!catalog) { const r = await api("/api/catalog"); if (!r.ok) throw new Error(errText(r)); catalog = r.data; }
    if (!me) { const r = await api("/api/me"); me = r.ok ? r.data : {role: "viewer"}; }
    return catalog;
  }
  const can = (role) => ({viewer: 1, operator: 2, engineer: 3, admin: 4}[me?.role] || 0) >= ({viewer: 1, operator: 2, engineer: 3, admin: 4}[role]);

  // ------------------------------------------------------------------ router
  function route() {
    const hash = location.hash || "#live";
    const view = hash === "#sources" ? "sources" : (hash === "#new-source" || hash.startsWith("#edit-")) ? "editor" : "live";
    $("view-live").hidden = view !== "live"; $("view-sources").hidden = view !== "sources"; $("view-editor").hidden = view !== "editor";
    $("tab-live").classList.toggle("active", view === "live"); $("tab-sources").classList.toggle("active", view !== "live");
    $("tab-live").setAttribute("aria-current", view === "live" ? "page" : "false");
    $("tab-sources").setAttribute("aria-current", view !== "live" ? "page" : "false");
    $("app-sub").textContent = view === "live" ? "Live values" : "Source configuration";
    if (!token()) return;
    if (view === "sources") renderList().catch((e) => toast(e.message, "error"));
    if (view === "editor") {
      const m = hash.match(/^#edit-(.+)$/);
      openEditor(m ? decodeURIComponent(m[1]) : null).catch((e) => toast(e.message, "error"));
    }
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------------ sources list
  async function renderList() {
    await ensureCatalog();
    $("new-source").hidden = !can("engineer");
    const table = $("sources-table");
    const r = await api("/api/sources");
    if (!r.ok) { table.replaceChildren(h("caption", {class: "error"}, errText(r))); return; }
    const cfgs = await Promise.all(r.data.map((s) => api(`/api/sources/${encodeURIComponent(s.source_id)}`)));
    table.replaceChildren(h("thead", {}, h("tr", {}, ["Source", "Protocol", "Site", "State", "Devices", "Tags", "Revision", ""].map((c) => h("th", {scope: "col"}, c)))));
    const body = h("tbody");
    if (!r.data.length) body.append(h("tr", {}, h("td", {colspan: 8, class: "muted"}, "No sources yet. Create the first one with New source.")));
    r.data.forEach((s, i) => {
      const cfg = cfgs[i].data?.config || {};
      const proto = cfg.type === "modbus" ? `Modbus ${(cfg.connection?.transport || "").toUpperCase()}` : (cfg.type || s.type);
      const state = s.enabled ? s.state : "waiting_config";
      const actions = h("td", {class: "actions"});
      const row = h("tr", {"data-source": s.source_id},
        h("td", {}, h("div", {class: "src-name"}, s.name), h("div", {class: "src-id"}, s.source_id)),
        h("td", {}, proto), h("td", {}, s.site),
        h("td", {}, h("span", {class: `chip st-${state}`}, STATE_LABEL[state] || state)),
        h("td", {class: "num"}, (cfg.connection?.devices || []).length), h("td", {class: "num"}, (cfg.points || []).length),
        h("td", {class: "num"}, s.revision, h("div", {class: "src-id"}, `by ${s.updated_by}`)), actions);
      if (can("engineer")) {
        actions.append(h("a", {class: "btn", href: `#edit-${encodeURIComponent(s.source_id)}`}, "Edit"),
          h("button", {type: "button", class: "btn", onclick: () => toggleEnabled(s.source_id)}, s.enabled ? "Disable" : "Enable"));
      }
      if (can("admin")) actions.append(h("button", {type: "button", class: "btn danger-ghost", onclick: () => confirmDelete(row, s)}, "Delete"));
      body.append(row);
    });
    table.append(body);
  }
  async function toggleEnabled(sid) {
    const r = await api(`/api/sources/${encodeURIComponent(sid)}`);
    if (!r.ok) return toast(errText(r), "error");
    const cfg = r.data.config; cfg.enabled = !cfg.enabled;
    const u = await api(`/api/sources/${encodeURIComponent(sid)}`, "PUT", cfg);
    toast(u.ok ? `${cfg.name} ${cfg.enabled ? "enabled" : "disabled"} (revision ${u.data.revision})` : errText(u), u.ok ? "ok" : "error");
    reloadLive(); renderList();
  }
  function confirmDelete(row, s) {
    const cell = row.lastChild;
    const keep = [...cell.childNodes];
    cell.replaceChildren(h("span", {class: "confirm"}, `Delete ${s.name}? Its configuration is removed and the connector stops; stored history stays.`),
      h("button", {type: "button", class: "btn danger", onclick: async () => {
        const r = await api(`/api/sources/${encodeURIComponent(s.source_id)}`, "DELETE");
        toast(r.status === 204 ? `${s.name} deleted` : errText(r), r.status === 204 ? "ok" : "error");
        reloadLive(); renderList();
      }}, "Delete"),
      h("button", {type: "button", class: "btn", onclick: () => cell.replaceChildren(...keep)}, "Cancel"));
  }

  // ------------------------------------------------------------------ editor state
  let st = null;   // {mode, step, typeId, protoIdx, cfg, sourceRunning, testResult, check, errors}
  function blankConfig() {
    const demo = catalog.demo;
    return {site: demo?.site || "", source_id: "", name: "", type: null, enabled: true,
      connection: {transport: "tcp", host: "", port: 502, devices: [{id: "device-1", unit_id: 1, name: ""}]},
      acquisition: {default_scan_ms: 1000, request_timeout_ms: 1000, batch_max_values: 500, batch_max_ms: 1000, integrity_s: 900},
      points: []};
  }
  async function openEditor(sourceId) {
    await ensureCatalog();
    if (!can("engineer")) { $("view-editor").replaceChildren(h("p", {class: "error"}, "Your role cannot change sources.")); return; }
    if (sourceId) {
      const r = await api(`/api/sources/${encodeURIComponent(sourceId)}`);
      if (!r.ok) { $("view-editor").replaceChildren(h("p", {class: "error"}, errText(r))); return; }
      const cfg = r.data.config;
      cfg.acquisition = {default_scan_ms: 1000, request_timeout_ms: 3000, ...(cfg.acquisition || {})};
      const typeId = (catalog.types.find((t) => t.protocols.some((p) => p.connector === cfg.type && matchesPreset(p, cfg))) || catalog.types[0]).id;
      const protoIdx = catalog.types.find((t) => t.id === typeId).protocols.findIndex((p) => p.connector === cfg.type && matchesPreset(p, cfg));
      st = {mode: "edit", step: 2, typeId, protoIdx, cfg, sourceRunning: ["running", "degraded"].includes(r.data.status?.state), errors: []};
    } else {
      st = {mode: "create", step: 1, typeId: catalog.types[0].id, protoIdx: -1, cfg: blankConfig(), idEdited: false, errors: []};
    }
    render();
  }
  const matchesPreset = (p, cfg) => Object.entries(p.preset || {}).every(([k, v]) => cfg.connection?.[k] === v);
  const curType = () => catalog.types.find((t) => t.id === st.typeId);
  const curProto = () => st.protoIdx >= 0 ? curType().protocols[st.protoIdx] : null;

  // ------------------------------------------------------------------ editor shell
  function render() {
    const root = $("view-editor");
    const title = st.mode === "edit" ? `Edit source · ${st.cfg.name}` : "New source";
    const tracker = h("ol", {class: "steps"}, STEPS.map((label, i) => {
      const n = i + 1, reachable = n <= maxReachable();
      return h("li", {class: n === st.step ? "current" : n < st.step ? "done" : ""},
        h("button", {type: "button", disabled: !reachable || (st.mode === "edit" && n === 1),
          "aria-current": n === st.step ? "step" : null, onclick: () => go(n)}, h("span", {class: "step-n"}, n), label));
    }));
    const body = [null, step1, step2, step3, step4][st.step]();
    const footer = h("div", {class: "editor-foot"},
      h("a", {class: "btn ghost", href: "#sources"}, "Cancel"),
      h("div", {class: "spacer"}),
      st.step > (st.mode === "edit" ? 2 : 1) ? h("button", {type: "button", class: "btn", onclick: () => go(st.step - 1)}, "Back") : null,
      st.step < 4 ? h("button", {type: "button", class: "btn primary", id: "next", disabled: st.step + 1 > maxReachable(), onclick: () => go(st.step + 1)},
        `Next: ${STEPS[st.step]}`) : h("button", {type: "button", class: "btn primary", id: "save", disabled: !st.check?.ok, onclick: save},
        st.mode === "edit" ? "Save changes" : "Create source"));
    root.replaceChildren(h("div", {class: "page-head"}, h("div", {}, h("h1", {}, title),
      h("p", {class: "muted"}, st.mode === "edit" ? `${st.cfg.source_id} · revision ${st.cfg.revision}` : "Add a data source in four steps."))),
      tracker, h("div", {class: "panel editor-body"}, body), footer);
  }
  function maxReachable() {
    if (!curProto() || curProto().status !== "available") return 1;
    const c = st.cfg;
    const connOk = c.name && ID_RE.test(c.source_id) && ID_RE.test(c.site) && c.connection.devices.length &&
      (c.connection.transport === "tcp" ? c.connection.host : c.connection.serial?.device);
    if (!connOk) return 2;
    return c.points.length ? 4 : 3;
  }
  function go(n) {
    st.step = Math.min(n, maxReachable()); render();
    if (st.step === 4) runCheck();
    window.scrollTo({top: 0});
  }
  const rerenderKeepFocus = () => {
    const a = document.activeElement, id = a?.id, sel = a && "selectionStart" in a ? [a.selectionStart, a.selectionEnd] : null;
    render(); if (id && $(id)) { $(id).focus(); try { if (sel) $(id).setSelectionRange(...sel); } catch (e) { /* not a text input */ } }
  };

  // ------------------------------------------------------------------ step 1: source type and protocol
  function step1() {
    const t = curType();
    const types = h("nav", {class: "type-list", "aria-label": "Source types"}, catalog.types.map((x) =>
      h("button", {type: "button", class: `type ${x.id === st.typeId ? "on" : ""}`, "aria-pressed": String(x.id === st.typeId),
        onclick: () => { st.typeId = x.id; st.protoIdx = -1; render(); }},
        h("span", {class: "tile"}, icon(x.id)), h("span", {class: "type-text"}, h("b", {}, x.name), h("small", {}, x.short)),
        h("span", {class: "count", title: "protocols available now"}, x.available))));
    const protos = h("div", {class: "proto-grid"}, t.protocols.map((p, i) => {
      const ok = p.status === "available";
      return h("button", {type: "button", class: `proto ${i === st.protoIdx ? "on" : ""}`, disabled: !ok, "aria-pressed": String(i === st.protoIdx),
        onclick: () => choose(i)},
        h("span", {class: "proto-top"}, h("b", {}, p.name), h("span", {class: `badge b-${p.status}`}, STATUS_LABEL[p.status])),
        h("span", {class: "mono"}, p.standard), p.port ? h("small", {class: "muted"}, `Default port ${p.port}`) : null);
    }));
    return h("div", {class: "step1"}, types, h("section", {class: "type-detail"},
      h("div", {class: "type-head"}, h("span", {class: "tile big"}, icon(t.id, 28)), h("div", {}, h("h2", {}, t.name), h("p", {}, t.description))),
      h("div", {class: "chips"}, h("span", {class: "muted"}, "Interface standards:"), t.standards.map((s) => h("span", {class: "std"}, s))),
      h("h3", {}, "Standard protocols"), protos,
      h("p", {class: "muted small"}, "Available: a connector is installed. In development: specified, connector being built. Planned: on the roadmap.")));
  }
  function choose(i) {
    st.protoIdx = i;
    const p = curProto();
    st.cfg.type = p.connector;
    Object.assign(st.cfg.connection, p.preset || {});
    if (p.connector === "modbus") {
      const c = st.cfg.connection;
      if (c.transport === "rtu") { delete c.host; delete c.port; c.serial = c.serial || {device: "", baud: 9600, parity: "E", data_bits: 8, stop_bits: 1}; }
      else { delete c.serial; c.port = c.port || 502; c.host = c.host || ""; }
    }
    render();
  }

  // ------------------------------------------------------------------ step 2: connection
  function field(label, input, help) {
    return h("label", {class: "field"}, h("span", {class: "label"}, label), input, help ? h("small", {class: "muted"}, help) : null);
  }
  function inp(id, value, onChange, attrs = {}) {
    return h("input", {id, value: value ?? "", ...attrs, oninput: (e) => { onChange(attrs.type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value); rerenderKeepFocus(); }});
  }
  function sel(id, value, options, onChange) {
    return h("select", {id, onchange: (e) => { onChange(e.target.value); rerenderKeepFocus(); }},
      options.map(([v, l]) => { const o = h("option", {value: v}, l); if (String(v) === String(value)) o.selected = true; return o; }));
  }
  function step2() {
    const c = st.cfg, conn = c.connection, acq = c.acquisition, edit = st.mode === "edit";
    const demo = catalog.demo;
    const basics = h("fieldset", {}, h("legend", {}, "Source"), h("div", {class: "form-grid"},
      field("Name", inp("f-name", c.name, (v) => { c.name = v; if (!edit && !st.idEdited) c.source_id = slug(v); }, {required: true, placeholder: "e.g. Pump station West"})),
      field("Source ID", inp("f-sid", c.source_id, (v) => { c.source_id = v; st.idEdited = true; }, {disabled: edit, pattern: ID_RE.source, required: true}),
        edit ? "Cannot change after creation." : "Lower case letters, digits and dashes. Used in tag names and topics."),
      field("Site ID", inp("f-site", c.site, (v) => { c.site = v; }, {disabled: edit, required: true, placeholder: "e.g. ps-west"}), "The site whose edge node runs this connector.")));
    let link;
    if (c.type === "modbus") {
      const tcp = conn.transport === "tcp";
      link = h("fieldset", {}, h("legend", {}, tcp ? "Modbus TCP connection" : "Modbus RTU (RS-485) connection"), h("div", {class: "form-grid"},
        tcp ? [field("Host / IP address", inp("f-host", conn.host, (v) => { conn.host = v; }, {placeholder: "192.168.1.10"})),
          field("Port", inp("f-port", conn.port, (v) => { conn.port = v; }, {type: "number", min: 1, max: 65535}))]
          : [field("Serial port", inp("f-serial", conn.serial.device, (v) => { conn.serial.device = v; }, {placeholder: "/dev/ttyUSB0 or COM3"})),
            field("Baud rate", sel("f-baud", conn.serial.baud, [[9600, "9600"], [19200, "19200"], [38400, "38400"], [57600, "57600"], [115200, "115200"]], (v) => { conn.serial.baud = Number(v); })),
            field("Parity", sel("f-parity", conn.serial.parity, [["E", "Even"], ["N", "None"], ["O", "Odd"]], (v) => { conn.serial.parity = v; })),
            field("Stop bits", sel("f-stop", conn.serial.stop_bits, [[1, "1"], [2, "2"]], (v) => { conn.serial.stop_bits = Number(v); }))]));
    }
    const devRows = conn.devices.map((d, i) => h("tr", {},
      h("td", {}, inp(`d-id-${i}`, d.id, (v) => { renameDevice(d.id, v); d.id = v; }, {"aria-label": `Device ${i + 1} ID`})),
      h("td", {}, inp(`d-unit-${i}`, d.unit_id, (v) => { d.unit_id = v; }, {type: "number", min: 0, max: 247, "aria-label": `Device ${i + 1} unit ID`})),
      h("td", {}, inp(`d-name-${i}`, d.name, (v) => { d.name = v; }, {"aria-label": `Device ${i + 1} name`, placeholder: "optional"})),
      h("td", {}, conn.devices.length > 1 ? h("button", {type: "button", class: "btn ghost", "aria-label": `Remove device ${d.id}`,
        onclick: () => { conn.devices.splice(i, 1); render(); }}, "Remove") : null)));
    const devices = h("fieldset", {}, h("legend", {}, "Devices on this connection"),
      h("p", {class: "muted small"}, "Each Modbus device (meter, PLC, drive) answers on its own unit ID."),
      h("table", {class: "grid compact"}, h("thead", {}, h("tr", {}, ["Device ID", "Unit ID", "Name", ""].map((x) => h("th", {}, x)))), h("tbody", {}, devRows)),
      h("button", {type: "button", class: "btn", onclick: () => { const n = conn.devices.length + 1;
        conn.devices.push({id: `device-${n}`, unit_id: Math.min(247, (Math.max(0, ...conn.devices.map((x) => Number(x.unit_id) || 0)) + 1)), name: ""}); render(); }}, "Add device"));
    const timing = h("fieldset", {}, h("legend", {}, "Polling"), h("div", {class: "form-grid"},
      field("Scan interval (ms)", inp("f-scan", acq.default_scan_ms, (v) => { acq.default_scan_ms = v; }, {type: "number", min: 100}), "How often values are read."),
      field("Response timeout (ms)", inp("f-timeout", acq.request_timeout_ms, (v) => { acq.request_timeout_ms = v; }, {type: "number", min: 100})),
      field("Registers per read", inp("f-maxregs", conn.max_registers_per_read ?? 100, (v) => { conn.max_registers_per_read = v; }, {type: "number", min: 1, max: 125}), "Lower it for devices that reject long reads.")));
    const demoBox = demo?.devices?.length && !edit ? h("aside", {class: "demo-box"}, h("b", {}, "Demo: simulated devices you can connect to"),
      demo.devices.map((d) => h("div", {class: "demo-dev"}, h("span", {}, `${d.name} — ${d.host}:${d.port}, unit ${d.unit_id}`),
        h("button", {type: "button", class: "btn", onclick: () => useDemo(d)}, "Use these settings")))) : null;
    return h("div", {class: "step2"}, demoBox, basics, link, devices, timing, testPanel());
  }
  function renameDevice(oldId, newId) { st.cfg.points.forEach((p) => { if (p.address.device === oldId) p.address.device = newId; }); }
  function useDemo(d) {
    const c = st.cfg;
    if (!c.name) { c.name = d.name; if (!st.idEdited) c.source_id = slug(d.name); }
    if (!c.site) c.site = catalog.demo.site;
    Object.assign(c.connection, {transport: "tcp", host: d.host, port: d.port, devices: [{id: d.device_id, unit_id: d.unit_id, name: d.name}]});
    delete c.connection.serial;
    st.demoDevice = d;
    render();
  }
  function testPanel() {
    const r = st.testResult;
    const out = !r ? null : r.busy ? h("p", {class: "muted"}, "Testing…") :
      r.error ? h("p", {class: "error"}, r.error) :
      h("ul", {class: "test-results"}, r.devices.map((d) => h("li", {}, h("span", {}, d.id),
        h("span", {class: `q ${d.ok ? "q-good" : "q-bad"}`}, d.ok ? `Connected · ${d.latency_ms} ms${d.info?.exception ? ` (answered with exception ${d.info.exception})` : ""}` : `${d.error?.code || "Failed"}: ${d.error?.message || ""}`))));
    return h("div", {class: "test-panel"}, h("button", {type: "button", class: "btn", id: "test-btn", disabled: r?.busy, onclick: testConnection},
      icon("api", 16), " Test connection"), h("span", {class: "muted small"}, "Opens a short connection from the site's edge node. Nothing is saved."), out);
  }
  async function testConnection() {
    st.testResult = {busy: true}; rerenderKeepFocus();
    const q = st.mode === "edit" ? `?mode=update&source_id=${encodeURIComponent(st.cfg.source_id)}` : "?mode=create";
    const r = await api(`/api/sources-test${q}`, "POST", payload());
    st.testResult = r.ok ? {devices: r.data.result.devices} : {error: r.data?.error ? `${r.data.error.code}: ${errText(r)}` : errText(r)};
    rerenderKeepFocus();
  }

  // ------------------------------------------------------------------ step 3: tags
  function step3() {
    const c = st.cfg, devs = c.connection.devices.map((d) => [d.id, d.id]);
    const prefix = `${c.site || "site"}.${c.source_id || "source"}.`;
    const rows = c.points.map((p, i) => {
      const a = p.address, encs = ENC_BY_TABLE[a.table], bad = st.errors.some((e) => e.row === i);
      const extra = a.encoding === "bit" ? inp(`p-bit-${i}`, a.bit ?? 0, (v) => { a.bit = v; }, {type: "number", min: 0, max: 15, "aria-label": "Bit"})
        : a.encoding === "string" ? inp(`p-len-${i}`, a.length ?? 4, (v) => { a.length = v; }, {type: "number", min: 1, max: 125, "aria-label": "Length in registers"}) : h("span", {class: "muted"}, "—");
      return h("tr", {class: bad ? "row-error" : ""},
        h("td", {}, inp(`p-tag-${i}`, p.tag_id, (v) => { p.tag_id = v.toLowerCase(); }, {"aria-label": "Tag ID", class: TAG_RE.test(p.tag_id) ? "" : "invalid"})),
        h("td", {}, sel(`p-dev-${i}`, a.device, devs, (v) => { a.device = v; })),
        h("td", {}, sel(`p-tbl-${i}`, a.table, Object.entries(TABLE_LABEL), (v) => { a.table = v; if (!ENC_BY_TABLE[v].includes(a.encoding)) a.encoding = ENC_BY_TABLE[v][0]; syncPoint(p); })),
        h("td", {}, inp(`p-addr-${i}`, a.address, (v) => { a.address = v; }, {type: "number", min: 0, max: 65535, "aria-label": "Address"})),
        h("td", {}, sel(`p-enc-${i}`, a.encoding, encs.map((e) => [e, e]), (v) => { a.encoding = v; syncPoint(p); })),
        h("td", {}, extra),
        ...(numeric(p) ? [
          h("td", {}, inp(`p-unit-${i}`, p.unit, (v) => { if (v) p.unit = v; else delete p.unit; }, {"aria-label": "Unit", placeholder: "unit"})),
          h("td", {}, inp(`p-gain-${i}`, p.scaling?.gain ?? 1, (v) => { setScale(p, "gain", v); }, {type: "number", step: "any", "aria-label": "Scale factor"})),
          h("td", {}, inp(`p-off-${i}`, p.scaling?.offset ?? 0, (v) => { setScale(p, "offset", v); }, {type: "number", step: "any", "aria-label": "Offset"})),
          h("td", {}, inp(`p-db-${i}`, p.deadband?.value ?? "", (v) => { if (v === "" || v === 0) delete p.deadband; else p.deadband = {mode: "abs", value: v}; }, {type: "number", step: "any", min: 0, "aria-label": "Deadband", placeholder: "0"}))]
          : [h("td", {colspan: 4, class: "na"}, p.data_type === "bool" ? "On / off value: no unit or scaling" : "Text value")]),
        h("td", {class: "read-cell"}, st.mode === "edit" && st.sourceRunning ? h("button", {type: "button", class: "btn ghost", onclick: () => readNow(i)}, p._read ?? "Read") : null),
        h("td", {}, h("button", {type: "button", class: "btn ghost", "aria-label": `Remove ${p.tag_id}`, onclick: () => { c.points.splice(i, 1); render(); }}, "✕")));
    });
    const widths = [300, 130, 160, 90, 110, 90, 90, 90, 90, 100, st.mode === "edit" && st.sourceRunning ? 110 : 10, 50];
    const table = h("div", {class: "table-wrap plain"}, h("table", {class: "grid compact tags-edit"},
      h("colgroup", {}, widths.map((w) => h("col", {style: `width:${w}px`}))),
      h("thead", {}, h("tr", {}, ["Tag ID", "Device", "Register type", "Address", "Format", "Bit / length", "Unit", "Scale ×", "Offset +", "Deadband", st.mode === "edit" && st.sourceRunning ? "Value" : "", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", {colspan: 12, class: "muted"}, "No tags yet. Add tags one by one or paste them from Excel.")))));
    const pasteBox = st.paste ? h("div", {class: "paste"}, h("label", {for: "paste-text"}, "Paste rows from Excel or CSV. Columns: tag_id, device, table, address, encoding, unit, gain, offset, deadband, bit, length (header row optional)."),
      h("textarea", {id: "paste-text", rows: 6, placeholder: `${prefix}p-total\t${c.connection.devices[0]?.id}\tholding_register\t0\tfloat32\tkW\t1\t0\t0.5`}),
      h("div", {class: "row"}, h("button", {type: "button", class: "btn primary", onclick: importPaste}, "Import rows"),
        h("button", {type: "button", class: "btn ghost", onclick: () => { st.paste = false; render(); }}, "Close"), st.pasteMsg ? h("span", {class: "muted"}, st.pasteMsg) : null)) : null;
    const demoTags = catalog.demo?.devices?.find((d) => d.host === c.connection.host && d.port === Number(c.connection.port));
    return h("div", {class: "step3"},
      h("div", {class: "toolbar"}, h("button", {type: "button", class: "btn primary", id: "add-tag", onclick: addTag}, "Add tag"),
        h("button", {type: "button", class: "btn", onclick: () => { st.paste = true; render(); }}, "Paste from Excel / CSV"),
        demoTags ? h("button", {type: "button", class: "btn", id: "demo-tags", onclick: () => addDemoTags(demoTags)}, "Add the demo device's tags") : null,
        h("span", {class: "muted small"}, `Address = Modbus protocol address from 0 (register 40001 → holding register 0). Tag IDs usually start with ${prefix}`)),
      pasteBox, errorList(), table);
  }
  const numeric = (p) => p.data_type === "float" || p.data_type === "int" || p.data_type === "enum";
  function syncPoint(p) {
    const a = p.address;
    p.data_type = dataTypeFor(a.encoding);
    if (a.encoding !== "bit") delete a.bit; else a.bit = a.bit ?? 0;
    if (a.encoding !== "string") delete a.length; else a.length = a.length ?? 4;
    if (p.data_type === "bool" || p.data_type === "string") { delete p.scaling; delete p.deadband; }
  }
  function setScale(p, k, v) {
    p.scaling = {gain: 1, offset: 0, ...(p.scaling || {}), [k]: v === "" ? (k === "gain" ? 1 : 0) : v};
    if (p.scaling.gain === 1 && p.scaling.offset === 0) delete p.scaling;
    if (p.scaling && p.data_type === "int" && (!Number.isInteger(p.scaling.gain) || !Number.isInteger(p.scaling.offset))) p.data_type = "float";
  }
  function addTag() {
    const c = st.cfg, last = c.points[c.points.length - 1];
    const a = last ? {...last.address, address: last.address.address + (last.address.encoding.match(/64/) ? 4 : last.address.encoding.match(/32/) ? 2 : 1)}
      : {device: c.connection.devices[0].id, table: "holding_register", address: 0, encoding: "float32"};
    delete a.bit; delete a.length;
    const p = {tag_id: `${c.site}.${c.source_id}.tag-${c.points.length + 1}`, address: a, data_type: dataTypeFor(a.encoding)};
    syncPoint(p); c.points.push(p); render();
    $(`p-tag-${c.points.length - 1}`)?.focus();
  }
  function addDemoTags(d) {
    const c = st.cfg, dev = c.connection.devices.find((x) => Number(x.unit_id) === d.unit_id)?.id || c.connection.devices[0].id;
    d.tags.forEach((t) => {
      const tag_id = `${c.site}.${c.source_id}.${t.name}`;
      if (c.points.some((p) => p.tag_id === tag_id)) return;
      const p = {tag_id, address: {device: dev, ...t.address}, data_type: t.data_type};
      if (t.unit) p.unit = t.unit; if (t.scaling) p.scaling = t.scaling;
      c.points.push(p);
    });
    render();
  }
  function importPaste() {
    const text = $("paste-text").value.trim(); if (!text) return;
    const lines = text.split(/\r?\n/).filter((l) => l.trim());
    const split = (l) => l.split(l.includes("\t") ? "\t" : l.includes(";") ? ";" : ",").map((x) => x.trim());
    let cols = ["tag_id", "device", "table", "address", "encoding", "unit", "gain", "offset", "deadband", "bit", "length"];
    if (split(lines[0])[0].toLowerCase() === "tag_id") cols = split(lines.shift()).map((x) => x.toLowerCase());
    let added = 0, skipped = 0;
    lines.forEach((l) => {
      const r = Object.fromEntries(split(l).map((v, i) => [cols[i], v]));
      const table = Object.keys(TABLE_LABEL).find((t) => t === r.table || TABLE_LABEL[t].toLowerCase() === (r.table || "").toLowerCase()) || "holding_register";
      const enc = ENC_BY_TABLE[table].includes(r.encoding) ? r.encoding : ENC_BY_TABLE[table][0];
      if (!r.tag_id || r.address === undefined || isNaN(Number(r.address))) { skipped++; return; }
      const p = {tag_id: r.tag_id.toLowerCase(), address: {device: r.device || st.cfg.connection.devices[0].id, table, address: Number(r.address), encoding: enc}, data_type: dataTypeFor(enc)};
      if (enc === "bit") p.address.bit = Number(r.bit || 0);
      if (enc === "string") p.address.length = Number(r.length || 4);
      if (r.unit) p.unit = r.unit;
      if ((r.gain && Number(r.gain) !== 1) || (r.offset && Number(r.offset) !== 0)) setScale(p, "gain", Number(r.gain || 1)), setScale(p, "offset", Number(r.offset || 0));
      if (r.deadband && Number(r.deadband) > 0) p.deadband = {mode: "abs", value: Number(r.deadband)};
      st.cfg.points.push(p); added++;
    });
    st.pasteMsg = `${added} row(s) imported${skipped ? `, ${skipped} skipped (missing tag_id or address)` : ""}.`;
    render();
  }
  async function readNow(i) {
    const p = st.cfg.points[i];
    p._read = "…"; rerenderKeepFocus();
    const r = await api(`/api/sources/${encodeURIComponent(st.cfg.source_id)}/admin/read_now`, "POST",
      {addresses: [{address: p.address, data_type: p.data_type}]});
    const v = r.ok && r.data.ok ? r.data.result.values[0] : null;
    p._read = v ? (v.q >= 192 ? String(v.v) : `Bad (${v.q})`) : (r.data?.error?.code || "Error");
    rerenderKeepFocus();
  }

  // ------------------------------------------------------------------ step 4: review and save
  function payload() {
    const c = JSON.parse(JSON.stringify(st.cfg));
    c.points = c.points.map((p) => { delete p._read; return p; });
    delete c.spec; delete c.revision;
    return c;
  }
  function errorList() {
    if (!st.errors.length) return null;
    return h("div", {class: "errors", role: "alert"}, h("b", {}, "Please fix:"), h("ul", {}, st.errors.slice(0, 12).map((e) =>
      h("li", {}, e.text, e.step && e.step !== st.step ? [" ", h("button", {type: "button", class: "linkish", onclick: () => go(e.step)}, `Go to ${STEPS[e.step - 1]}`)] : null))));
  }
  function mapErrors(details) {
    return [].concat(details || []).map((d) => {
      const m = String(d).match(/^points\/(\d+)\//);
      if (m) return {row: Number(m[1]), step: 3, text: `Tag ${Number(m[1]) + 1} (${st.cfg.points[Number(m[1])]?.tag_id}): ${String(d).replace(/^points\/\d+\//, "")}`};
      return {step: String(d).startsWith("connection") || String(d).startsWith("acquisition") || String(d).startsWith("(root)") ? 2 : null, text: String(d)};
    });
  }
  async function runCheck() {
    st.check = {busy: true}; render();
    const q = st.mode === "edit" ? `?mode=update&source_id=${encodeURIComponent(st.cfg.source_id)}` : "?mode=create";
    const r = await api(`/api/sources-check${q}`, "POST", payload());
    st.errors = r.ok ? [] : mapErrors(r.data?.error?.details || [r.data?.error?.message || `HTTP ${r.status}`]);
    st.check = r.ok ? {ok: true, doc: r.data.config} : {ok: false};
    if (!r.ok && r.data?.error?.code !== "CFG_INVALID") st.errors = [{text: errText(r)}];
    render();
  }
  function step4() {
    const c = st.cfg, p = curProto();
    const summary = h("dl", {class: "summary"},
      h("dt", {}, "Source"), h("dd", {}, `${c.name} (${c.source_id}) at site ${c.site}`),
      h("dt", {}, "Protocol"), h("dd", {}, p ? `${p.name} · ${p.standard}` : c.type),
      h("dt", {}, "Connection"), h("dd", {}, c.connection.transport === "tcp" ? `${c.connection.host}:${c.connection.port}` : `${c.connection.serial?.device} · ${c.connection.serial?.baud} baud`),
      h("dt", {}, "Devices"), h("dd", {}, c.connection.devices.map((d) => `${d.id} (unit ${d.unit_id})`).join(", ")),
      h("dt", {}, "Tags"), h("dd", {}, `${c.points.length} tag(s), read every ${c.acquisition.default_scan_ms} ms`),
      h("dt", {}, "Revision"), h("dd", {}, st.check?.doc ? `${st.check.doc.revision}${st.mode === "edit" ? " (the connector applies it within seconds)" : ""}` : "—"));
    const status = st.check?.busy ? h("p", {class: "muted"}, "Checking the configuration…") :
      st.check?.ok ? h("p", {class: "ok-line"}, "✓ Configuration is valid and ready to save.") : errorList();
    return h("div", {class: "step4"}, status, summary,
      h("details", {}, h("summary", {}, "Show the configuration document sent to the connector"), h("pre", {class: "json"}, JSON.stringify(st.check?.doc || payload(), null, 2))));
  }
  async function save() {
    const btn = $("save"); btn.disabled = true; btn.textContent = "Saving…";
    const r = st.mode === "edit" ? await api(`/api/sources/${encodeURIComponent(st.cfg.source_id)}`, "PUT", payload())
      : await api("/api/sources", "POST", payload());
    if (!r.ok) { st.errors = mapErrors(r.data?.error?.details || [errText(r)]); st.check = {ok: false}; render(); return; }
    toast(`${r.data.name} saved as revision ${r.data.revision}. ${st.mode === "edit" ? "The connector applies it now." : "The connector is starting at the site."}`);
    reloadLive();
    const filter = $("filter"); if (filter) { filter.value = r.data.source_id; filter.dispatchEvent(new Event("input")); }
    location.hash = "#live";
  }

  // ------------------------------------------------------------------ start
  window.addEventListener("load", () => { route(); });
  // after signing in, app.js stores the token; render the current configuration view then
  const obs = new MutationObserver(() => { if (!$("login").hidden) return; if (location.hash && location.hash !== "#live") route(); });
  obs.observe($("login"), {attributes: true, attributeFilter: ["hidden"]});
})();
