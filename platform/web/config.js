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
    it: "M3 4h18v6H3z M3 14h18v6H3z M7 7h.01 M7 17h.01 M11 7h6 M11 17h6",
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
    const view = hash === "#sources" ? "sources" : (hash === "#new-source" || hash.startsWith("#edit-")) ? "editor" : hash === "#admin" ? "admin" : "live";
    $("view-live").hidden = view !== "live"; $("view-sources").hidden = view !== "sources"; $("view-editor").hidden = view !== "editor";
    $("view-admin").hidden = view !== "admin";
    const tab = view === "editor" ? "sources" : view;
    [["tab-live", "live"], ["tab-sources", "sources"], ["tab-admin", "admin"]].forEach(([id, v]) => {
      $(id).classList.toggle("active", tab === v); $(id).setAttribute("aria-current", tab === v ? "page" : "false");
    });
    $("app-sub").textContent = {live: "Live values", sources: "Source configuration", editor: "Source configuration", admin: "Administration"}[view];
    if (!token()) return;
    if (view === "admin") { window.OpcareAdmin?.open().catch((e) => toast(e.message, "error")); return; }
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
      const proto = protoLabel(cfg) || s.type;
      const state = s.enabled ? s.state : "waiting_config";
      const actions = h("td", {class: "actions"});
      const row = h("tr", {"data-source": s.source_id},
        h("td", {}, h("div", {class: "src-name"}, s.name), h("div", {class: "src-id"}, s.source_id)),
        h("td", {}, proto), h("td", {}, s.site),
        h("td", {}, h("span", {class: `chip st-${state}`}, STATE_LABEL[state] || state)),
        h("td", {class: "num"}, (cfg.connection?.devices || cfg.connection?.senders || []).length), h("td", {class: "num"}, (cfg.points || []).length),
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
  let st = null;   // {mode, step, typeId, protoIdx, cfg, sourceRunning, testResult, check, errors, secrets, browse}
  let storedSecrets = [];   // names of secrets already kept for the edge agent (values are never sent back)
  function blankConfig() {
    const demo = catalog.demo;
    return {site: demo?.site || "", source_id: "", name: "", type: null, enabled: true,
      connection: {transport: "tcp", host: "", port: 502, devices: [{id: "device-1", unit_id: 1, name: ""}]},
      acquisition: {default_scan_ms: 1000, request_timeout_ms: 1000, batch_max_values: 500, batch_max_ms: 1000, integrity_s: 900},
      points: []};
  }
  async function openEditor(sourceId) {
    await ensureCatalog();
    storedSecrets = [];
    if (catalog.secrets) { const r = await api("/api/secrets"); if (r.ok) storedSecrets = r.data.names; }
    if (!can("engineer")) { $("view-editor").replaceChildren(h("p", {class: "error"}, "Your role cannot change sources.")); return; }
    if (sourceId) {
      const r = await api(`/api/sources/${encodeURIComponent(sourceId)}`);
      if (!r.ok) { $("view-editor").replaceChildren(h("p", {class: "error"}, errText(r))); return; }
      const cfg = r.data.config;
      cfg.acquisition = {default_scan_ms: 1000, request_timeout_ms: 3000, ...(cfg.acquisition || {})};
      const fits = (p) => p.connector === cfg.type && matchesPreset(p, cfg), same = (p) => p.connector === cfg.type;
      const typeId = (catalog.types.find((t) => t.protocols.some(fits)) || catalog.types.find((t) => t.protocols.some(same)) || catalog.types[0]).id;
      const protos = catalog.types.find((t) => t.id === typeId).protocols;
      const protoIdx = protos.findIndex(fits) >= 0 ? protos.findIndex(fits) : protos.findIndex(same);
      st = {mode: "edit", step: 2, typeId, protoIdx, cfg, sourceRunning: ["running", "degraded"].includes(r.data.status?.state), errors: [], secrets: {}};
    } else {
      st = {mode: "create", step: 1, typeId: catalog.types[0].id, protoIdx: -1, cfg: blankConfig(), idEdited: false, errors: [], secrets: {}};
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
    const connOk = c.name && ID_RE.test(c.source_id) && ID_RE.test(c.site) && connectionComplete(c);
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
    if (st.cfg.type !== p.connector || (p.connector === "udp" && /syslog/i.test(p.name) !== Boolean(st.cfg.connection.syslog))) {
      st.cfg.connection = blankConnection(p);
      st.cfg.acquisition = blankAcquisition(p.connector);
      st.cfg.points = [];
      st.testResult = null;
    }
    st.cfg.type = p.connector;
    Object.assign(st.cfg.connection, p.preset || {});
    if (p.connector === "snmp") setSnmpVersion(st.cfg.connection.version);
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
    if (c.type === "udp") return h("div", {class: "step2"}, demoBoxFor("udp"), basics, ...udpConnection(), testPanel());
    if (c.type === "snmp") return h("div", {class: "step2"}, demoBoxFor("snmp"), basics, ...snmpConnection(), testPanel());
    if (c.type === "mqtt") return h("div", {class: "step2"}, demoBoxFor("mqtt"), basics, ...mqttConnection(), testPanel());
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
    return h("div", {class: "step2"}, demoBoxFor("modbus"), basics, link, devices, timing, testPanel());
  }
  function renameDevice(oldId, newId) { st.cfg.points.forEach((p) => { if (p.address.device === oldId) p.address.device = newId; }); }
  function useDemo(d) {
    if ((d.type || "modbus") !== "modbus") return useDemoOther(d);
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
        h("span", {class: `q ${d.ok ? "q-good" : "q-bad"}`}, d.ok ? okText(d) : `${d.error?.code || "Failed"}: ${d.error?.message || ""}`))));
    const udp = st.cfg.type === "udp";
    return h("div", {class: "test-panel"}, h("button", {type: "button", class: "btn", id: "test-btn", disabled: r?.busy, onclick: testConnection},
      icon("api", 16), udp ? " Listen for datagrams" : " Test connection"),
      h("span", {class: "muted small"}, udp ? `The edge node listens on the port for ${Math.max(3, (st.cfg.acquisition.request_timeout_ms || 5000) / 1000)} s and reports which senders were heard. Nothing is saved.`
        : "Opens a short connection from the site's edge node. Nothing is saved."), out);
  }
  function okText(d) {
    if (st.cfg.type === "mqtt") return `Connected · MQTT ${d.info?.protocol}${d.info?.tls ? " · TLS" : ""} · ${Object.entries(d.info?.subscriptions || {}).map(([f, r]) => `${f}: ${r}`).join(", ")}`;
    if (st.cfg.type === "udp") return `Heard · ${d.info?.datagrams ?? "?"} datagram(s) from ${d.info?.from} (${d.info?.format})`;
    if (st.cfg.type === "snmp") return `Connected · ${d.latency_ms} ms${d.info?.sysName ? ` · ${d.info.sysName}` : ""}${d.info?.sysDescr ? ` · ${d.info.sysDescr.slice(0, 60)}` : ""}`;
    return `Connected · ${d.latency_ms} ms${d.info?.exception ? ` (answered with exception ${d.info.exception})` : ""}`;
  }
  async function testConnection() {
    st.testResult = {busy: true}; rerenderKeepFocus();
    const se = await storeSecrets(); if (se) { st.testResult = {error: se}; rerenderKeepFocus(); return; }
    const q = st.mode === "edit" ? `?mode=update&source_id=${encodeURIComponent(st.cfg.source_id)}` : "?mode=create";
    const r = await api(`/api/sources-test${q}`, "POST", payload());
    st.testResult = r.ok ? {devices: r.data.result.devices} : {error: r.data?.error ? `${r.data.error.code}: ${errText(r)}` : errText(r)};
    rerenderKeepFocus();
  }

  // ------------------------------------------------------------------ step 3: tags
  function step3() {
    if (st.cfg.type === "udp") return step3udp();
    if (st.cfg.type === "snmp") return step3snmp();
    if (st.cfg.type === "mqtt") return step3mqtt();
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


  // ------------------------------------------------------------------ UDP and SNMP (spec 15.8, 15.9)
  const DATA_TYPES = [["float", "Number (decimal)"], ["int", "Whole number"], ["bool", "On / off"], ["string", "Text"], ["enum", "State number"]];
  const UDP_FORMATS = [["json", "JSON field"], ["delimited", "CSV / delimited field"], ["nmea", "NMEA 0183 sentence"], ["binary", "Binary bytes"], ["raw", "Whole line"], ["syslog", "Syslog field"]];
  const BIN_ENC = ["float32", "float64", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64", "bool", "bit", "string"];
  const SEVERITIES = ["emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"];
  function protoLabel(cfg) {
    const c = cfg.connection || {};
    if (cfg.type === "modbus") return `Modbus ${(c.transport || "").toUpperCase()}`;
    if (cfg.type === "udp") return `${c.syslog ? "Syslog" : "UDP"} · port ${c.bind_port}`;
    if (cfg.type === "snmp") return `SNMP ${c.version === "3" ? "v3" : "v" + c.version}`;
    if (cfg.type === "mqtt") return `MQTT · ${String(c.broker_url || "").replace(/^mqtts?:\/\//, "")}`;
    return cfg.type;
  }
  function blankConnection(p) {
    if (p.connector === "udp") {
      const c = {bind_host: "0.0.0.0", bind_port: p.port || 5514, senders: [{id: "sender-1", host: "", name: ""}], silence_timeout_s: 60};
      if (/syslog/i.test(p.name)) c.syslog = {min_severity: "warning"};
      return c;
    }
    if (p.connector === "mqtt") return {broker_url: "mqtt://", protocol_version: "5", auth: "anonymous", keepalive_s: 60,
      subscriptions: [{topic_filter: "", qos: 1}]};
    if (p.connector === "snmp") return {version: p.preset?.version || "2c", port: p.port || 161, devices: [{id: "device-1", host: "", name: ""}], retries: 1, max_oids_per_request: 20};
    return {transport: "tcp", host: "", port: 502, devices: [{id: "device-1", unit_id: 1, name: ""}]};
  }
  function blankAcquisition(type) {
    const base = {batch_max_values: 500, batch_max_ms: 1000, integrity_s: 900};
    if (type === "udp") return {...base, default_scan_ms: 1000, request_timeout_ms: 5000};
    if (type === "snmp") return {...base, default_scan_ms: 10000, request_timeout_ms: 2000};
    if (type === "mqtt") return {...base, default_scan_ms: 1000, request_timeout_ms: 5000};
    return {...base, default_scan_ms: 1000, request_timeout_ms: 1000};
  }
  const hasSecret = (ref, kind) => Boolean(st.secrets?.[kind]) || (ref && (storedSecrets.includes(ref) || !catalog.secrets));
  function connectionComplete(c) {
    const conn = c.connection;
    if (c.type === "udp") return conn.bind_port && conn.senders?.length && conn.senders.every((x) => x.id && x.host);
    if (c.type === "mqtt") {
      const auth = conn.auth || "anonymous";
      return /^mqtts?:\/\/[^/:]+(:\d+)?\/?$/.test(conn.broker_url || "") && conn.subscriptions?.length && conn.subscriptions.every((x) => x.topic_filter)
        && (auth !== "username" || (conn.username && hasSecret(conn.password_ref, "password")))
        && (auth !== "certificate" || (hasSecret(conn.client_cert_ref, "client-cert") && hasSecret(conn.client_key_ref, "client-key")));
    }
    if (c.type === "snmp") {
      if (!conn.devices?.length || !conn.devices.every((d) => d.id && d.host)) return false;
      if (conn.version !== "3") return hasSecret(conn.community_ref, "community");
      const v3 = conn.v3 || {}, lvl = v3.security_level || "authPriv";
      return Boolean(v3.username) && (lvl === "noAuthNoPriv" || hasSecret(v3.auth_password_ref, "auth")) && (lvl !== "authPriv" || hasSecret(v3.priv_password_ref, "priv"));
    }
    return conn.devices?.length && (conn.transport === "tcp" ? conn.host : conn.serial?.device);
  }
  function setSnmpVersion(v) {
    const conn = st.cfg.connection;
    conn.version = v;
    if (v === "3") { delete conn.community_ref; conn.v3 = conn.v3 || {username: "", security_level: "authPriv", auth_protocol: "SHA-256", priv_protocol: "AES"}; }
    else { delete conn.v3; }
  }
  function connectionSummary(c) {
    const conn = c.connection;
    if (c.type === "udp") return `Listening on UDP ${conn.bind_host || "0.0.0.0"}:${conn.bind_port}${conn.syslog ? ` · syslog events from ${conn.syslog.min_severity}` : ""}`;
    if (c.type === "mqtt") return `${conn.broker_url} · MQTT ${conn.protocol_version === "3.1.1" ? "3.1.1" : "5.0"} · ${conn.auth || "anonymous"}`;
    if (c.type === "snmp") return `SNMP ${conn.version === "3" ? `v3 · user ${conn.v3?.username} · ${conn.v3?.security_level}` : "v" + conn.version + " · community"} · port ${conn.port || 161}${conn.traps ? ` · traps on UDP ${conn.traps.bind_port}` : ""}`;
    return conn.transport === "tcp" ? `${conn.host}:${conn.port}` : `${conn.serial?.device} · ${conn.serial?.baud} baud`;
  }
  function devicesSummary(c) {
    const conn = c.connection;
    if (c.type === "udp") return conn.senders.map((x) => `${x.id} (${x.host})`).join(", ");
    if (c.type === "snmp") return conn.devices.map((d) => `${d.id} (${d.host}${d.port ? ":" + d.port : ""})`).join(", ");
    if (c.type === "mqtt") return `Subscribed to ${conn.subscriptions.map((x) => x.topic_filter).join(", ")}`;
    return conn.devices.map((d) => `${d.id} (unit ${d.unit_id})`).join(", ");
  }

  // ---------------- simulated devices of the demo
  function demoBoxFor(type) {
    const list = (catalog.demo?.devices || []).filter((d) => (d.type || "modbus") === type);
    if (!list.length || st.mode === "edit") return null;
    const where = (d) => type === "udp" ? `sends to UDP port ${d.bind_port} from ${d.senders.map((x) => x.host).join(", ")}`
      : type === "snmp" ? `${d.devices.map((x) => `${x.host}:${x.port}`).join(", ")} · community "${d.community}"${d.v3 ? ` · v3 user ${d.v3.username}` : ""}`
      : type === "mqtt" ? `broker ${d.broker_url} · ${d.subscriptions.map((x) => x.topic_filter).join(", ")}`
      : `${d.host}:${d.port}, unit ${d.unit_id}`;
    return h("aside", {class: "demo-box"}, h("b", {}, "Demo: simulated devices you can connect to"),
      list.map((d) => h("div", {class: "demo-dev"}, h("span", {}, `${d.name} — ${where(d)}`),
        h("button", {type: "button", class: "btn", onclick: () => useDemo(d)}, "Use these settings"))));
  }
  function useDemoOther(d) {
    const c = st.cfg, conn = c.connection;
    if (!c.name) { c.name = d.name; if (!st.idEdited) c.source_id = slug(d.name); }
    if (!c.site) c.site = catalog.demo.site;
    if (d.type === "udp") {
      Object.assign(conn, {bind_host: "127.0.0.1", bind_port: d.bind_port, senders: d.senders.map((x) => ({...x}))});
    } else if (d.type === "mqtt") {
      Object.assign(conn, {broker_url: d.broker_url, subscriptions: d.subscriptions.map((x) => ({...x}))});
    } else {
      conn.port = d.devices[0].port;
      conn.devices = d.devices.map((x) => ({id: x.id, host: x.host, ...(x.port !== conn.port ? {port: x.port} : {}), name: x.name}));
      if (d.trap_port && !conn.traps) conn.traps = {bind_host: "127.0.0.1", bind_port: d.trap_port};
      if (conn.version === "3" && d.v3) {
        conn.v3 = {username: d.v3.username, security_level: "authPriv", auth_protocol: d.v3.auth_protocol, priv_protocol: "AES"};
        st.secrets.auth = d.v3.auth_password; st.secrets.priv = d.v3.priv_password; st.secrets["trap-community"] = d.community;
      } else st.secrets.community = d.community;
    }
    st.demoDevice = d;
    render();
  }

  // ---------------- passwords: kept by the edge node, referenced by name in the configuration (spec section 3)
  function secretTargets() {
    const conn = st.cfg.connection, out = [];
    if (st.cfg.type === "mqtt") {
      if (conn.auth === "username") out.push(["password", conn, "password_ref"]);
      if (String(conn.broker_url).startsWith("mqtts")) out.push(["ca-cert", conn, "ca_cert_ref"]);
      if (conn.auth === "certificate") out.push(["client-cert", conn, "client_cert_ref"], ["client-key", conn, "client_key_ref"]);
      return out;
    }
    if (st.cfg.type !== "snmp") return out;
    if (conn.version === "3") { out.push(["auth", conn.v3, "auth_password_ref"], ["priv", conn.v3, "priv_password_ref"]); }
    else out.push(["community", conn, "community_ref"]);
    if (conn.traps && conn.version === "3") out.push(["trap-community", conn.traps, "community_ref"]);
    return out;
  }
  async function storeSecrets() {
    for (const [kind, obj, key] of secretTargets()) {
      const value = st.secrets[kind];
      if (!value) continue;
      if (!catalog.secrets) { return "This installation keeps passwords on the edge nodes: enter the secret's file name instead."; }
      const name = `${st.cfg.source_id || "source"}-${kind}`;
      const r = await api(`/api/secrets/${encodeURIComponent(name)}`, "PUT", {value});
      if (r.status !== 204) return `Could not store the ${kind} password: ${errText(r)}`;
      obj[key] = name;
      if (!storedSecrets.includes(name)) storedSecrets.push(name);
      delete st.secrets[kind];
    }
    return null;
  }
  function secretField(label, kind, obj, key, help, optional = false) {
    if (!catalog.secrets) return field(`${label} (secret file name)`, inp(`f-${kind}`, obj[key], (v) => { obj[key] = v; }, {placeholder: "file name in the edge node's secrets folder"}), help);
    const saved = obj[key] && storedSecrets.includes(obj[key]);
    return field(label, h("input", {id: `f-${kind}`, type: "password", autocomplete: "new-password", value: st.secrets[kind] || "",
      placeholder: saved ? "•••••• saved (type to replace)" : optional ? "optional" : "required",
      oninput: (e) => { st.secrets[kind] = e.target.value; rerenderKeepFocus(); }}), help);
  }

  // ---------------- step 2: UDP listener
  function udpConnection() {
    const conn = st.cfg.connection, acq = st.cfg.acquisition;
    const listen = h("fieldset", {}, h("legend", {}, "Listening (the devices send to the edge node)"), h("div", {class: "form-grid"},
      field("Listen on address", inp("f-bind-host", conn.bind_host, (v) => { conn.bind_host = v; }, {placeholder: "0.0.0.0 = all"}), "0.0.0.0 listens on every network card of the edge node."),
      field("UDP port", inp("f-bind-port", conn.bind_port, (v) => { conn.bind_port = v; }, {type: "number", min: 1, max: 65535}),
        "Use 1024 or higher (e.g. 5514); the edge node can map syslog 514 to it."),
      field("Silent sender after (s)", inp("f-silence", conn.silence_timeout_s ?? 60, (v) => { conn.silence_timeout_s = v; }, {type: "number", min: 0, max: 86400}),
        "No datagram for this long → the sender's tags turn Bad (no communication). 0 = never.")));
    const rows = conn.senders.map((x, i) => h("tr", {},
      h("td", {}, inp(`s-id-${i}`, x.id, (v) => { renameDevice(x.id, v); x.id = v; }, {"aria-label": `Sender ${i + 1} ID`})),
      h("td", {}, inp(`s-host-${i}`, x.host, (v) => { x.host = v.trim(); }, {"aria-label": `Sender ${i + 1} address`, placeholder: "10.20.1.15 or 10.20.1.0/24"})),
      h("td", {}, inp(`s-name-${i}`, x.name, (v) => { x.name = v; }, {"aria-label": `Sender ${i + 1} name`, placeholder: "optional"})),
      h("td", {}, conn.senders.length > 1 ? h("button", {type: "button", class: "btn ghost", onclick: () => { conn.senders.splice(i, 1); render(); }}, "Remove") : null)));
    const senders = h("fieldset", {}, h("legend", {}, "Senders (devices allowed to send)"),
      h("p", {class: "muted small"}, "Datagrams from any other address are dropped. A network (CIDR) covers several devices, e.g. all switches sending syslog."),
      h("table", {class: "grid compact"}, h("thead", {}, h("tr", {}, ["Sender ID", "IP address or network", "Name", ""].map((x) => h("th", {}, x)))), h("tbody", {}, rows)),
      h("button", {type: "button", class: "btn", onclick: () => { conn.senders.push({id: `sender-${conn.senders.length + 1}`, host: "", name: ""}); render(); }}, "Add sender"));
    const sys = conn.syslog;
    const syslog = h("fieldset", {}, h("legend", {}, "Syslog"), h("div", {class: "form-grid"},
      field("Syslog messages become events", h("label", {class: "check"}, h("input", {type: "checkbox", id: "f-syslog", checked: Boolean(sys),
        onchange: (e) => { if (e.target.checked) conn.syslog = {min_severity: "warning"}; else delete conn.syslog; render(); }}), " Yes, show them in the Events list")),
      sys ? field("From severity", sel("f-syslog-sev", sys.min_severity, SEVERITIES.map((x) => [x, x]), (v) => { sys.min_severity = v; }), "Warning and more severe is a good start.") : null));
    const timing = h("fieldset", {}, h("legend", {}, "Test"), h("div", {class: "form-grid"},
      field("Listen time for the test (ms)", inp("f-timeout", acq.request_timeout_ms, (v) => { acq.request_timeout_ms = v; }, {type: "number", min: 1000, max: 30000}))));
    return [listen, senders, syslog, timing];
  }

  // ---------------- step 2: SNMP manager
  function snmpConnection() {
    const conn = st.cfg.connection, acq = st.cfg.acquisition, v3 = conn.v3 || {};
    const access = h("fieldset", {}, h("legend", {}, "SNMP access (read-only)"), h("div", {class: "form-grid"},
      field("Version", sel("f-snmp-ver", conn.version, [["1", "v1"], ["2c", "v2c"], ["3", "v3 (secure)"]], (v) => { setSnmpVersion(v); }),
        conn.version === "3" ? "User name and passwords; traffic is authenticated and encrypted." : "Community string; prefer v3 where the device supports it."),
      field("Port", inp("f-snmp-port", conn.port ?? 161, (v) => { conn.port = v; }, {type: "number", min: 1, max: 65535})),
      conn.version !== "3" ? secretField("Community", "community", conn, "community_ref", "The read-only community, e.g. public. Stored on the edge node, never in the configuration.") : null),
      conn.version === "3" ? h("div", {class: "form-grid"},
        field("User name", inp("f-v3-user", v3.username, (v) => { v3.username = v; })),
        field("Security level", sel("f-v3-level", v3.security_level || "authPriv", [["authPriv", "Authentication + encryption"], ["authNoPriv", "Authentication only"], ["noAuthNoPriv", "None"]], (v) => { v3.security_level = v; })),
        v3.security_level !== "noAuthNoPriv" ? field("Authentication", sel("f-v3-auth", v3.auth_protocol || "SHA-256", ["SHA-256", "SHA-512", "SHA-384", "SHA-224", "SHA", "MD5"].map((x) => [x, x]), (v) => { v3.auth_protocol = v; })) : null,
        v3.security_level !== "noAuthNoPriv" ? secretField("Authentication password", "auth", v3, "auth_password_ref", "At least 8 characters.") : null,
        v3.security_level === "authPriv" ? field("Encryption", sel("f-v3-priv", "AES", [["AES", "AES-128"]], () => { v3.priv_protocol = "AES"; })) : null,
        v3.security_level === "authPriv" ? secretField("Encryption password", "priv", v3, "priv_password_ref") : null) : null);
    const rows = conn.devices.map((d, i) => h("tr", {},
      h("td", {}, inp(`d-id-${i}`, d.id, (v) => { renameDevice(d.id, v); d.id = v; }, {"aria-label": `Device ${i + 1} ID`})),
      h("td", {}, inp(`d-host-${i}`, d.host, (v) => { d.host = v.trim(); }, {"aria-label": `Device ${i + 1} address`, placeholder: "10.30.1.1 or switch-1.local"})),
      h("td", {}, inp(`d-port-${i}`, d.port ?? "", (v) => { if (v === "" || v === conn.port) delete d.port; else d.port = v; }, {type: "number", min: 1, max: 65535, "aria-label": `Device ${i + 1} port`, placeholder: String(conn.port ?? 161)})),
      h("td", {}, inp(`d-name-${i}`, d.name, (v) => { d.name = v; }, {"aria-label": `Device ${i + 1} name`, placeholder: "optional"})),
      h("td", {}, conn.devices.length > 1 ? h("button", {type: "button", class: "btn ghost", onclick: () => { conn.devices.splice(i, 1); render(); }}, "Remove") : null)));
    const devices = h("fieldset", {}, h("legend", {}, "Devices (switches, servers, UPSs…)"),
      h("p", {class: "muted small"}, "All devices of this source share the access settings above."),
      h("table", {class: "grid compact"}, h("thead", {}, h("tr", {}, ["Device ID", "IP address or host name", "Port", "Name", ""].map((x) => h("th", {}, x)))), h("tbody", {}, rows)),
      h("button", {type: "button", class: "btn", onclick: () => { conn.devices.push({id: `device-${conn.devices.length + 1}`, host: "", name: ""}); render(); }}, "Add device"));
    const traps = conn.traps;
    const timing = h("fieldset", {}, h("legend", {}, "Polling and traps"), h("div", {class: "form-grid"},
      field("Read every (ms)", inp("f-scan", acq.default_scan_ms, (v) => { acq.default_scan_ms = v; }, {type: "number", min: 1000}), "10–60 s is usual for IT equipment."),
      field("Response timeout (ms)", inp("f-timeout", acq.request_timeout_ms, (v) => { acq.request_timeout_ms = v; }, {type: "number", min: 100})),
      field("Retries", inp("f-retries", conn.retries ?? 1, (v) => { conn.retries = v; }, {type: "number", min: 0, max: 5})),
      field("Receive traps", h("label", {class: "check"}, h("input", {type: "checkbox", id: "f-traps", checked: Boolean(traps),
        onchange: (e) => { if (e.target.checked) conn.traps = {bind_host: "0.0.0.0", bind_port: 10162}; else delete conn.traps; render(); }}), " Alarms the devices send (link down, on battery…) become events")),
      traps ? field("Trap UDP port", inp("f-trap-port", traps.bind_port, (v) => { traps.bind_port = v; }, {type: "number", min: 1, max: 65535}), "Point the devices' trap destination here (the edge node maps 162 to it).") : null,
      traps && conn.version === "3" ? secretField("Trap community (v1/v2c traps)", "trap-community", traps, "community_ref",
        "Devices often still send v1/v2c traps; leave empty to ignore them.", true) : null));
    return [access, devices, timing];
  }


  // ---------------- step 2: MQTT subscriber
  function secretArea(label, kind, obj, key, help) {
    if (!catalog.secrets) return field(`${label} (secret file name)`, inp(`f-${kind}`, obj[key], (v) => { obj[key] = v; }), help);
    const saved = obj[key] && storedSecrets.includes(obj[key]);
    return field(label, h("textarea", {id: `f-${kind}`, rows: 3, class: "mono", spellcheck: "false", value: st.secrets[kind] || "",
      placeholder: saved ? "saved (paste to replace)" : "-----BEGIN CERTIFICATE-----",
      oninput: (e) => { st.secrets[kind] = e.target.value; rerenderKeepFocus(); }}), help);
  }
  function mqttConnection() {
    const conn = st.cfg.connection, acq = st.cfg.acquisition, tls = String(conn.broker_url).startsWith("mqtts"), auth = conn.auth || "anonymous";
    const broker = h("fieldset", {}, h("legend", {}, "Broker (the client's MQTT server)"), h("div", {class: "form-grid"},
      field("Broker address", inp("f-broker", conn.broker_url, (v) => { conn.broker_url = v.trim(); }, {placeholder: "mqtts://broker.example.com:8883"}),
        "mqtt:// (port 1883) or mqtts:// with TLS (port 8883)."),
      field("MQTT version", sel("f-mqtt-ver", conn.protocol_version || "5", [["5", "5.0"], ["3.1.1", "3.1.1"]], (v) => { conn.protocol_version = v; })),
      field("Client ID", inp("f-client-id", conn.client_id, (v) => { if (v) conn.client_id = v; else delete conn.client_id; }, {placeholder: `opcare-${st.cfg.source_id || "source"}`}),
        "Leave empty for the default. Must be unique on the broker."),
      field("Sign-in", sel("f-mqtt-auth", auth, [["anonymous", "None (anonymous)"], ["username", "User name and password"], ["certificate", "Client certificate"]],
        (v) => { conn.auth = v; if (v !== "username") { delete conn.username; delete conn.password_ref; } if (v !== "certificate") { delete conn.client_cert_ref; delete conn.client_key_ref; } })),
      auth === "username" ? field("User name", inp("f-mqtt-user", conn.username, (v) => { conn.username = v; })) : null,
      auth === "username" ? secretField("Password", "password", conn, "password_ref", "Stored on the edge node, never in the configuration.") : null),
      tls ? h("div", {class: "form-grid"},
        secretArea("Broker CA certificate (PEM)", "ca-cert", conn, "ca_cert_ref", "Only if the broker uses a private CA. Empty = the system's trusted CAs."),
        auth === "certificate" ? secretArea("Client certificate (PEM)", "client-cert", conn, "client_cert_ref") : null,
        auth === "certificate" ? secretArea("Client private key (PEM)", "client-key", conn, "client_key_ref") : null,
        field("Certificate check", h("label", {class: "check"}, h("input", {type: "checkbox", id: "f-insecure", checked: Boolean(conn.insecure_skip_verify),
          onchange: (e) => { if (e.target.checked) conn.insecure_skip_verify = true; else delete conn.insecure_skip_verify; render(); }}), " Skip (testing only — not safe)"))) : null);
    const rows = conn.subscriptions.map((x, i) => h("tr", {},
      h("td", {}, inp(`s-filter-${i}`, x.topic_filter, (v) => { x.topic_filter = v.trim(); }, {"aria-label": `Topic filter ${i + 1}`, placeholder: "meters/+/telemetry or spBv1.0/plant/#", class: "mono"})),
      h("td", {}, sel(`s-qos-${i}`, x.qos ?? 1, [[1, "QoS 1 (at least once)"], [0, "QoS 0 (at most once)"]], (v) => { x.qos = Number(v); })),
      h("td", {}, conn.subscriptions.length > 1 ? h("button", {type: "button", class: "btn ghost", onclick: () => { conn.subscriptions.splice(i, 1); render(); }}, "Remove") : null)));
    const subs = h("fieldset", {}, h("legend", {}, "Subscriptions (topics to receive)"),
      h("p", {class: "muted small"}, "+ matches one level, # everything below. For Sparkplug B use spBv1.0/<group>/#. The connector only subscribes — it never publishes to the broker."),
      h("table", {class: "grid compact"}, h("thead", {}, h("tr", {}, ["Topic filter", "Delivery", ""].map((x) => h("th", {}, x)))), h("tbody", {}, rows)),
      h("button", {type: "button", class: "btn", onclick: () => { conn.subscriptions.push({topic_filter: "", qos: 1}); render(); }}, "Add subscription"));
    const timing = h("fieldset", {}, h("legend", {}, "Connection"), h("div", {class: "form-grid"},
      field("Keep-alive (s)", inp("f-keepalive", conn.keepalive_s ?? 60, (v) => { conn.keepalive_s = v; }, {type: "number", min: 0, max: 3600})),
      field("Connect timeout (ms)", inp("f-timeout", acq.request_timeout_ms, (v) => { acq.request_timeout_ms = v; }, {type: "number", min: 500}))));
    return [broker, subs, timing];
  }

  // ---------------- step 3: MQTT tags
  const MQTT_FORMATS = [["json", "JSON field"], ["raw", "Plain value"], ["sparkplug_b", "Sparkplug B metric"]];
  function setMqttFormat(p, f) {
    const subs = st.cfg.connection.subscriptions.map((x) => x.topic_filter);
    const topic = (subs.find((x) => !x.startsWith("spBv1.0")) || "devices/1/data").replace(/\/#$/, "/value").replace(/\+/g, "1");
    const sp = (subs.find((x) => x.startsWith("spBv1.0/")) || "spBv1.0/group/#").split("/");
    p.address = f === "sparkplug_b" ? {format: f, group_id: sp[1] && sp[1] !== "#" && sp[1] !== "+" ? sp[1] : "group", edge_node_id: "node", metric: "Metric"}
      : f === "json" ? {format: f, topic, value_pointer: "/value"} : {format: f, topic};
  }
  function mqttWhere(p, i) {
    const a = p.address, set = (k, v) => { if (v === "" || v === undefined) delete a[k]; else a[k] = v; };
    if (a.format === "sparkplug_b") return [
      inp(`p-grp-${i}`, a.group_id, (v) => { a.group_id = v; }, {"aria-label": "Group", placeholder: "group", style: "width:90px"}),
      inp(`p-node-${i}`, a.edge_node_id, (v) => { a.edge_node_id = v; }, {"aria-label": "Edge node", placeholder: "edge node", style: "width:100px"}),
      inp(`p-dev-sp-${i}`, a.device_id, (v) => { set("device_id", v); }, {"aria-label": "Device", placeholder: "device (opt.)", style: "width:100px"}),
      inp(`p-metric-${i}`, a.metric, (v) => { a.metric = v; }, {"aria-label": "Metric", placeholder: "metric name"})];
    const out = [inp(`p-topic-${i}`, a.topic, (v) => { a.topic = v.trim(); }, {"aria-label": "Topic", placeholder: "meters/118/telemetry", class: "mono"})];
    if (a.format === "json") out.push(inp(`p-ptr-${i}`, a.value_pointer, (v) => { a.value_pointer = v; }, {"aria-label": "JSON pointer", placeholder: "/flow", style: "width:110px"}),
      inp(`p-tsp-${i}`, a.timestamp_pointer, (v) => { set("timestamp_pointer", v); if (v && !a.timestamp_format) a.timestamp_format = "rfc3339"; if (!v) delete a.timestamp_format; },
        {"aria-label": "Timestamp pointer", placeholder: "time /ts (opt.)", style: "width:100px"}),
      a.timestamp_pointer ? sel(`p-tsf-${i}`, a.timestamp_format || "rfc3339", [["rfc3339", "ISO text"], ["epoch_ms", "ms"], ["epoch_s", "s"]], (v) => { a.timestamp_format = v; }) : null);
    return out;
  }
  function step3mqtt() {
    const c = st.cfg;
    const rows = c.points.map((p, i) => h("tr", {class: st.errors.some((e) => e.row === i) ? "row-error" : ""},
      h("td", {}, inp(`p-tag-${i}`, p.tag_id, (v) => { p.tag_id = v.toLowerCase(); }, {"aria-label": "Tag ID", class: TAG_RE.test(p.tag_id) ? "" : "invalid"})),
      h("td", {}, sel(`p-fmt-${i}`, p.address.format, MQTT_FORMATS, (v) => { setMqttFormat(p, v); })),
      h("td", {class: "where"}, h("div", {class: "inline"}, mqttWhere(p, i))),
      typeCell(p, i), ...scaleCells(p, i), removeCell(c, p, i)));
    const tools = h("div", {class: "toolbar"},
      h("button", {type: "button", class: "btn primary", id: "add-tag", onclick: () => { const p = {tag_id: uniqueTag(`value-${c.points.length + 1}`), data_type: "float"}; setMqttFormat(p, "json"); c.points.push(p); render(); }}, "Add tag"),
      h("button", {type: "button", class: "btn", id: "discover", disabled: st.browse?.busy, onclick: () => discover({listen_s: 8})}, st.browse?.busy ? "Listening…" : "Listen & pick values"),
      h("span", {class: "muted small"}, "Listen subscribes for 8 s and lists the topics, JSON fields and Sparkplug metrics that arrive; click Add next to a value."));
    return h("div", {class: "step3"}, tools, browsePanel(), errorList(),
      tagTable(["Tag ID", "Format", "Topic / metric", "Type", "Unit", "Scale ×", "Offset +", "Deadband", ""], rows, [270, 150, 470, 120, 80, 80, 80, 80, 44]));
  }

  // ---------------- step 3: shared helpers
  function uniqueTag(name) {
    const c = st.cfg, base = `${c.site}.${c.source_id}.${slug(name) || "tag"}`.replace(/\.\.+/g, ".");
    let t = base, n = 2;
    while (c.points.some((p) => p.tag_id === t)) t = `${base}-${n++}`;
    return t;
  }
  function scaleCells(p, i) {
    return numeric(p) ? [
      h("td", {}, inp(`p-unit-${i}`, p.unit, (v) => { if (v) p.unit = v; else delete p.unit; }, {"aria-label": "Unit", placeholder: "unit"})),
      h("td", {}, inp(`p-gain-${i}`, p.scaling?.gain ?? 1, (v) => { setScale(p, "gain", v); }, {type: "number", step: "any", "aria-label": "Scale factor"})),
      h("td", {}, inp(`p-off-${i}`, p.scaling?.offset ?? 0, (v) => { setScale(p, "offset", v); }, {type: "number", step: "any", "aria-label": "Offset"})),
      h("td", {}, inp(`p-db-${i}`, p.deadband?.value ?? "", (v) => { if (v === "" || v === 0) delete p.deadband; else p.deadband = {mode: "abs", value: v}; }, {type: "number", step: "any", min: 0, "aria-label": "Deadband", placeholder: "0"}))]
      : [h("td", {colspan: 4, class: "na"}, p.data_type === "bool" ? "On / off value: no unit or scaling" : "Text value")];
  }
  function typeCell(p, i) {
    return h("td", {}, sel(`p-type-${i}`, p.data_type, DATA_TYPES, (v) => { p.data_type = v; if (!numeric(p)) { delete p.scaling; delete p.deadband; delete p.unit; } }));
  }
  function removeCell(c, p, i) {
    return h("td", {}, h("button", {type: "button", class: "btn ghost", "aria-label": `Remove ${p.tag_id}`, onclick: () => { c.points.splice(i, 1); render(); }}, "✕"));
  }
  function tagTable(head, rows, widths) {
    return h("div", {class: "table-wrap plain"}, h("table", {class: "grid compact tags-edit"},
      h("colgroup", {}, widths.map((w) => h("col", {style: `width:${w}px`}))),
      h("thead", {}, h("tr", {}, head.map((x) => h("th", {}, x)))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", {colspan: head.length, class: "muted"}, "No tags yet. Add tags, use a template or discover them from the device.")))));
  }
  async function discover(args) {
    st.browse = {busy: true, items: st.browse?.items || []}; rerenderKeepFocus();
    const se = await storeSecrets(); if (se) { st.browse = {error: se}; rerenderKeepFocus(); return; }
    const q = st.mode === "edit" ? `?mode=update&source_id=${encodeURIComponent(st.cfg.source_id)}` : "?mode=create";
    const {append, ...rest} = args;
    const r = await api(`/api/sources-browse${q}`, "POST", {config: payload(), ...rest});
    const keep = append ? (st.browse.items || []) : [];
    st.browse = r.ok && r.data?.ok ? {items: keep.concat(r.data.result.items), next: r.data.result.next_path}
      : {error: r.data?.error ? `${r.data.error.code}: ${r.data.error.message || errText(r)}` : errText(r), items: keep};
    rerenderKeepFocus();
  }
  function addFromBrowse(item) {
    const a = item.address || {};
    const where = st.cfg.type === "mqtt" ? (a.format === "sparkplug_b" ? `${a.edge_node_id}${a.device_id ? "-" + a.device_id : ""}` : a.topic) : "";
    const base = (where ? `${where}-` : "") + (st.cfg.type === "mqtt" && a.format === "raw" ? "" : item.name.replace(/^\//, "")).replace(/[/.+#|]/g, "-");
    const p = {tag_id: uniqueTag(st.cfg.type === "snmp" ? `${item.address.device}-${base}` : base), address: {...item.address},
      data_type: item.data_type === "int" ? "int" : item.data_type || "float"};
    if (st.cfg.type === "snmp" && item.info?.type?.startsWith("Counter")) { p.address.transform = "rate"; p.data_type = "float"; }
    if (p.data_type === "int" && st.cfg.type === "udp") p.data_type = "float";
    st.cfg.points.push(p); render();
  }

  // ---------------- step 3: UDP tags
  function udpWhere(p, i) {
    const a = p.address, f = a.format;
    const set = (k, v) => { if (v === "" || v === null) delete a[k]; else a[k] = v; };
    if (f === "json") return [inp(`p-ptr-${i}`, a.value_pointer, (v) => { a.value_pointer = v; }, {"aria-label": "JSON pointer", placeholder: "/wind/speed"}),
      inp(`p-match-${i}`, a.match_pointer ? `${a.match_pointer}=${a.match_value ?? ""}` : "", (v) => { const [k, ...r] = v.split("="); if (!v) { delete a.match_pointer; delete a.match_value; } else { a.match_pointer = k; a.match_value = r.join("="); } },
        {"aria-label": "Only documents where", placeholder: "only if /id=ws-1 (optional)"})];
    if (f === "delimited") return [inp(`p-field-${i}`, a.field, (v) => { a.field = v; }, {type: "number", min: 0, "aria-label": "Field number from 0"}),
      sel(`p-delim-${i}`, a.delimiter || ",", [[",", "comma"], [";", "semicolon"], ["\t", "tab"], [" ", "space"]], (v) => { a.delimiter = v; })];
    if (f === "nmea") return [inp(`p-sent-${i}`, a.sentence, (v) => { a.sentence = v.toUpperCase(); }, {"aria-label": "Sentence", placeholder: "RMC", size: 5}),
      inp(`p-field-${i}`, a.field, (v) => { a.field = v; }, {type: "number", min: 1, "aria-label": "Field number from 1"}),
      h("label", {class: "check small"}, h("input", {type: "checkbox", checked: Boolean(a.coordinate), onchange: (e) => { set("coordinate", e.target.checked || ""); if (e.target.checked) p.data_type = "float"; render(); }}), " lat/long")];
    if (f === "binary") return [inp(`p-off-b-${i}`, a.offset, (v) => { a.offset = v; }, {type: "number", min: 0, "aria-label": "Byte offset"}),
      sel(`p-benc-${i}`, a.encoding, BIN_ENC.map((x) => [x, x]), (v) => { a.encoding = v; if (v === "bit") a.bit = a.bit ?? 0; else delete a.bit; if (v === "string") a.length = a.length ?? 8; else delete a.length;
        p.data_type = v === "bool" || v === "bit" ? "bool" : v === "string" ? "string" : v.startsWith("float") ? "float" : "int"; }),
      sel(`p-bord-${i}`, a.byte_order || "big", [["big", "big-endian"], ["little", "little-endian"]], (v) => { a.byte_order = v; }),
      a.encoding === "bit" ? inp(`p-bit-${i}`, a.bit, (v) => { a.bit = v; }, {type: "number", min: 0, max: 7, "aria-label": "Bit"}) : null];
    if (f === "syslog") return [sel(`p-sfield-${i}`, a.field, ["severity", "facility", "message", "app_name", "hostname"].map((x) => [x, x]), (v) => { a.field = v; p.data_type = v === "severity" || v === "facility" ? "int" : "string"; }),
      inp(`p-smatch-${i}`, a.match, (v) => { set("match", v); }, {"aria-label": "Only messages matching", placeholder: "only if message matches (regex, optional)"})];
    return [inp(`p-prefix-${i}`, a.prefix, (v) => { set("prefix", v); }, {"aria-label": "Line starts with", placeholder: "line starts with (optional)"})];
  }
  function setUdpFormat(p, f) {
    const dev = p.address.device;
    p.address = {device: dev, format: f, ...({json: {value_pointer: "/value"}, delimited: {field: 0, delimiter: ","}, nmea: {sentence: "RMC", field: 7},
      binary: {offset: 0, encoding: "float32"}, syslog: {field: "severity"}, raw: {}})[f]};
    p.data_type = f === "syslog" ? "int" : "float";
  }
  function step3udp() {
    const c = st.cfg, devs = c.connection.senders.map((x) => [x.id, x.id]);
    const rows = c.points.map((p, i) => h("tr", {class: st.errors.some((e) => e.row === i) ? "row-error" : ""},
      h("td", {}, inp(`p-tag-${i}`, p.tag_id, (v) => { p.tag_id = v.toLowerCase(); }, {"aria-label": "Tag ID", class: TAG_RE.test(p.tag_id) ? "" : "invalid"})),
      h("td", {}, sel(`p-dev-${i}`, p.address.device, devs, (v) => { p.address.device = v; })),
      h("td", {}, sel(`p-fmt-${i}`, p.address.format, UDP_FORMATS, (v) => { setUdpFormat(p, v); })),
      h("td", {class: "where"}, h("div", {class: "inline"}, udpWhere(p, i))),
      typeCell(p, i), ...scaleCells(p, i), removeCell(c, p, i)));
    const tpl = catalog.templates?.udp || [];
    st.tplSel = st.tplSel || tpl[0]?.id;
    const tools = h("div", {class: "toolbar"},
      h("button", {type: "button", class: "btn primary", id: "add-tag", onclick: () => { const p = {tag_id: uniqueTag(`value-${c.points.length + 1}`), address: {device: c.connection.senders[0].id}, data_type: "float"}; setUdpFormat(p, "json"); c.points.push(p); render(); }}, "Add tag"),
      tpl.length ? [sel("tpl-sel", st.tplSel, tpl.map((t) => [t.id, t.name]), (v) => { st.tplSel = v; }),
        h("button", {type: "button", class: "btn", id: "tpl-add", onclick: () => addTemplate(tpl.find((t) => t.id === st.tplSel), c.connection.senders[0].id)}, "Add template tags")] : null,
      h("button", {type: "button", class: "btn", id: "discover", disabled: st.browse?.busy, onclick: () => discover({listen_s: 8, depth: 2})}, st.browse?.busy ? "Listening…" : "Listen & pick values"),
      h("span", {class: "muted small"}, "Listen shows what the devices are sending right now; click Add next to a value."));
    return h("div", {class: "step3"}, tools, browsePanel(), errorList(),
      tagTable(["Tag ID", "Sender", "Format", "Where in the telegram", "Type", "Unit", "Scale ×", "Offset +", "Deadband", ""], rows, [280, 110, 150, 330, 120, 80, 80, 80, 80, 44]));
  }

  // ---------------- step 3: SNMP tags
  function step3snmp() {
    const c = st.cfg, devs = c.connection.devices.map((d) => [d.id, d.id]), canRead = st.mode === "edit" && st.sourceRunning;
    const rows = c.points.map((p, i) => h("tr", {class: st.errors.some((e) => e.row === i) ? "row-error" : ""},
      h("td", {}, inp(`p-tag-${i}`, p.tag_id, (v) => { p.tag_id = v.toLowerCase(); }, {"aria-label": "Tag ID", class: TAG_RE.test(p.tag_id) ? "" : "invalid"})),
      h("td", {}, sel(`p-dev-${i}`, p.address.device, devs, (v) => { p.address.device = v; })),
      h("td", {}, inp(`p-oid-${i}`, p.address.oid, (v) => { p.address.oid = v.trim(); }, {"aria-label": "OID", placeholder: "1.3.6.1.2.1.1.3.0", class: /^\.?[0-2](\.\d+)+$/.test(p.address.oid || "") ? "mono" : "mono invalid"})),
      typeCell(p, i),
      h("td", {}, h("label", {class: "check small"}, h("input", {type: "checkbox", "aria-label": "Per second", checked: p.address.transform === "rate",
        onchange: (e) => { if (e.target.checked) { p.address.transform = "rate"; p.data_type = "float"; } else delete p.address.transform; render(); }}), " per s")),
      ...scaleCells(p, i),
      h("td", {class: "read-cell"}, canRead ? h("button", {type: "button", class: "btn ghost", onclick: () => readNow(i)}, p._read ?? "Read") : null),
      removeCell(c, p, i)));
    const tpl = catalog.templates?.snmp || [];
    st.tplSel = tpl.some((t) => t.id === st.tplSel) ? st.tplSel : tpl[0]?.id;
    const cur = tpl.find((t) => t.id === st.tplSel);
    st.tplDev = devs.some(([d]) => d === st.tplDev) ? st.tplDev : devs[0]?.[0];
    st.walkRoot = st.walkRoot || "1.3.6.1.2.1";
    const tools = h("div", {class: "toolbar wrap"},
      h("button", {type: "button", class: "btn primary", id: "add-tag", onclick: () => { c.points.push({tag_id: uniqueTag(`value-${c.points.length + 1}`), address: {device: c.connection.devices[0].id, oid: ""}, data_type: "int"}); render(); }}, "Add tag"),
      h("span", {class: "sep"}, "Template:"), sel("tpl-sel", st.tplSel, tpl.map((t) => [t.id, t.name]), (v) => { st.tplSel = v; }),
      sel("tpl-dev", st.tplDev, devs, (v) => { st.tplDev = v; }),
      cur?.instance ? inp("tpl-n", st.tplN ?? 1, (v) => { st.tplN = v; }, {type: "number", min: 0, "aria-label": cur.instance, title: cur.instance, style: "width:80px"}) : null,
      h("button", {type: "button", class: "btn", id: "tpl-add", onclick: () => addTemplate(cur, st.tplDev, st.tplN ?? 1)}, "Add"),
      h("span", {class: "sep"}, "Browse:"), inp("walk-root", st.walkRoot, (v) => { st.walkRoot = v.trim(); }, {"aria-label": "Subtree OID", style: "width:150px", class: "mono"}),
      h("button", {type: "button", class: "btn", id: "discover", disabled: st.browse?.busy, onclick: () => discover({path: `${st.tplDev}/${st.walkRoot}`, max_items: 200})}, st.browse?.busy ? "Reading…" : "Browse device"));
    return h("div", {class: "step3"}, tools,
      h("p", {class: "muted small"}, "Templates add the usual values of standard MIBs (port status and traffic, UPS battery, server CPU). Browse lists what the device really offers; click Add. ‘per s’ turns a counter into a rate, e.g. traffic in Mbit/s (scale 0.000008)."),
      browsePanel(), errorList(),
      tagTable(["Tag ID", "Device", "OID", "Type", "Rate", "Unit", "Scale ×", "Offset +", "Deadband", canRead ? "Value" : "", ""], rows, [260, 110, 230, 120, 70, 80, 80, 80, 80, canRead ? 100 : 10, 44]));
  }
  function addTemplate(t, device, n) {
    if (!t || !device) return;
    t.tags.forEach((x) => {
      const name = x.name.replace("{n}", n), tag_id = uniqueTag(st.cfg.type === "snmp" ? `${device}.${name}` : name);
      const address = JSON.parse(JSON.stringify(x.address).replaceAll("{n}", String(n)));
      const p = {tag_id, address: {device, ...address}, data_type: x.data_type};
      if (x.unit) p.unit = x.unit; if (x.scaling) p.scaling = {...x.scaling};
      st.cfg.points.push(p);
    });
    render();
  }
  function browsePanel() {
    const b = st.browse;
    if (!b) return null;
    const close = h("button", {type: "button", class: "btn ghost", onclick: () => { st.browse = null; render(); }}, "Close");
    if (b.error) return h("div", {class: "browse"}, h("p", {class: "error"}, b.error), close);
    const items = b.items || [];
    const have = new Set(st.cfg.points.map((p) => JSON.stringify(p.address)));
    const rows = items.slice(0, 400).map((it) => {
      if (it.kind === "object" && it.address) {
        const added = have.has(JSON.stringify(it.address));
        return h("tr", {class: "group"}, h("td", {class: "mono"}, it.name), h("td", {}, it.data_type || ""), h("td", {class: "mono small"}, it.info?.sample ?? ""),
          h("td", {}, h("button", {type: "button", class: "btn", disabled: added, onclick: () => addFromBrowse(it)}, added ? "Added" : "Add")));
      }
      if (it.kind === "object" && st.cfg.type !== "udp") {
        return h("tr", {class: "group"}, h("td", {colspan: 4}, h("b", {class: "mono"}, it.name), ` · ${it.info?.format} · ${it.info?.messages} message(s)`,
          h("div", {class: "mono small sample"}, it.info?.sample || "")));
      }
      if (it.kind === "object") {
        const senderKnown = it.info?.device;
        return h("tr", {class: "group"}, h("td", {colspan: 3}, h("b", {}, it.name), ` · ${it.info?.format} · ${it.info?.datagrams} datagram(s)`, h("div", {class: "mono small sample"}, it.info?.sample || "")),
          h("td", {}, senderKnown ? null : h("button", {type: "button", class: "btn", onclick: () => { st.cfg.connection.senders.push({id: `sender-${st.cfg.connection.senders.length + 1}`, host: it.path, name: ""}); discover({listen_s: 4, depth: 2}); }}, "Allow sender")));
      }
      const added = have.has(JSON.stringify(it.address));
      return h("tr", {}, h("td", {class: "mono"}, it.name), h("td", {}, it.info?.type || it.data_type), h("td", {class: "mono small"}, it.info?.sample ?? ""),
        h("td", {}, it.address?.device === "?" ? h("span", {class: "muted small"}, "allow the sender first") :
          h("button", {type: "button", class: "btn", disabled: added, onclick: () => addFromBrowse(it)}, added ? "Added" : "Add")));
    });
    return h("div", {class: "browse"}, h("div", {class: "row"}, h("b", {}, b.busy ? "Reading…" : `${items.filter((i) => i.kind !== "object").length} value(s) found`),
      b.next ? h("button", {type: "button", class: "btn", onclick: () => discover({path: b.next, max_items: 200, append: true})}, "More") : null, close),
      items.length ? h("div", {class: "table-wrap plain browse-list"}, h("table", {class: "grid compact"}, h("thead", {}, h("tr", {}, ["Value", "Type", "Sample", ""].map((x) => h("th", {}, x)))), h("tbody", {}, rows)))
        : b.busy ? null : h("p", {class: "muted"}, st.cfg.type === "udp" ? "Nothing arrived. Check the device's destination address and port, and the edge node's firewall." : "Nothing found under this OID."));
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
    const se = await storeSecrets(); if (se) { st.errors = [{text: se, step: 2}]; st.check = {ok: false}; render(); return; }
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
      h("dt", {}, "Connection"), h("dd", {}, connectionSummary(c)),
      h("dt", {}, c.type === "udp" ? "Senders" : "Devices"), h("dd", {}, devicesSummary(c)),
      h("dt", {}, "Tags"), h("dd", {}, c.type === "udp" ? `${c.points.length} tag(s), updated whenever a datagram arrives` : `${c.points.length} tag(s), read every ${c.acquisition.default_scan_ms} ms`),
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
  const obs = new MutationObserver(() => { if (!$("login").hidden) return; me = null; if (location.hash && location.hash !== "#live") route(); });
  obs.observe($("login"), {attributes: true, attributeFilter: ["hidden"]});
})();
