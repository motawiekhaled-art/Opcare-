/* Opcare DAQ – in-browser stand-in for the platform server, used only by the hosted web demo.
   It answers the same /api/* requests and live stream as daq_platform (see platform/README.md) and
   simulates the demo devices of tools/demo/run_demo.py, so the real screens (app.js, config.js) run
   unchanged in a static page. Data lives in memory and resets when the page reloads.
   window.__DEMO_CATALOG__ (source types/protocols) is embedded by build_web_demo.py from catalog.py. */
(() => {
  "use strict";
  const TOKEN = "opcare-demo";
  try { sessionStorage.setItem("opcare-token", TOKEN); } catch (e) { /* storage blocked: the sign-in box offers the token */ }

  // ------------------------------------------------------------------ helpers
  const iso = (ms) => new Date(ms).toISOString();
  const now = () => Date.now();
  const rnd = (a, b) => a + Math.random() * (b - a);
  const r = (x, d = 3) => Number(x.toFixed(d));
  const ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/, TAG_RE = /^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$/;
  let ulidN = 0;
  const ulid = () => (Date.now().toString(32).toUpperCase().padStart(10, "0") + (ulidN++).toString(32).toUpperCase().padStart(16, "0")).slice(0, 26);
  const t0 = now();

  // ------------------------------------------------------------------ simulated devices
  // Substation MCC-01 (always configured) — same shapes as run_demo.drive_simulator
  function mccValue(tag, ms) {
    const t = (ms - t0) / 1000 + 600, n = (a) => rnd(-a, a);
    const p = 420 + 60 * Math.sin(t / 40) + n(4), v = 231 + 1.5 * Math.sin(t / 25) + n(0.3), i = p * 1000 / (3 * v * 0.95);
    const p2 = 180 + 25 * Math.sin(t / 30 + 1);
    const map = {
      "incomer.p-total": r(p, 2), "incomer.q-total": r(0.32 * p + n(2), 2), "incomer.voltage-l1": r(v, 1), "incomer.current-l1": r(i, 1),
      "incomer.frequency": r(50 + n(0.03), 2), "incomer.pf": r(0.95 + n(0.005), 3), "incomer.energy-import": Math.round(1250000 + (ms - t0) / 1000 * p / 3600),
      "incomer.overcurrent-alarm": i > 700, "incomer.breaker-closed": true, "feeder-2.p-total": r(p2, 2),
      "feeder-2.current-l1": r(p2 * 1000 / (3 * v * 0.93), 1), "tr1.oil-temp": r(62 + 4 * Math.sin(t / 120), 1), "tr1.winding-temp": r(71 + 5 * Math.sin(t / 110), 1),
    };
    return map[tag.split(".").slice(-2).join(".")];
  }
  const feeder2Down = (ms) => { const ph = ((ms - t0) / 1000 + 50) % 90; return ph >= 60 && ph < 80; };

  // Pump Station West (offered on the New source screen) — registers like run_demo.drive_pump_station
  const PUMP = {host: "127.0.0.1", port: 5020, unit: 1};
  function pumpRegister(table, address, ms) {
    const t = (ms - t0) / 1000, p2 = Math.floor(t / 120) % 2 === 1;
    const regs = {
      "holding_register:0": (p2 ? 310 : 165) + 12 * Math.sin(t / 15) + rnd(-2, 2),
      "holding_register:2": 4.2 + (p2 ? 0.6 : 0) + rnd(-0.05, 0.05),
      "holding_register:4": Math.round((6.5 - 0.8 * Math.sin(t / 300)) * 100),
      "holding_register:5": Math.round((24 + Math.sin(t / 200)) * 10),
      "holding_register:10": 18240 + Math.floor(t / 100),
      "coil:0": 1, "coil:1": p2 ? 1 : 0,
    };
    return regs[`${table}:${address}`];
  }
  const isPump = (conn) => conn.transport === "tcp" && String(conn.host).trim() === PUMP.host && Number(conn.port) === PUMP.port;

  // ------------------------------------------------------------------ state
  const sources = new Map();      // source_id -> {config, status, updated_by, updated_ms}
  const latest = new Map();       // tag_id -> {v, q, ts, rx, f}
  const history = new Map();      // tag_id -> [{ts, v, q}]
  const events = [];              // newest first
  const audit = [];
  const tagOwner = new Map();     // tag_id -> source_id

  function record(t, v, q, ms, f) {
    const val = {t, v, q, ts: iso(ms), rx: iso(ms)};
    if (f) val.f = f;
    latest.set(t, val);
    const h = history.get(t) || [];
    h.push({ts: val.ts, v: typeof v === "boolean" ? (v ? 1 : 0) : v, q});
    if (h.length > 4000) h.splice(0, h.length - 4000);
    history.set(t, h);
    return val;
  }
  function event(src, type, severity, message, extra = {}) {
    const e = {spec: "1.0", id: ulid(), src, type, severity, ts: iso(now()), message, ...extra};
    events.unshift(e); if (events.length > 300) events.pop();
    push({type: "event", event: e});
  }
  function scale(p, raw) {
    if (raw === undefined || raw === null) return raw;
    if (p.data_type === "bool") return Boolean(raw);
    if (p.data_type === "string") return String(raw);
    let v = raw;
    if (p.scaling) v = Number((raw * (p.scaling.gain ?? 1) + (p.scaling.offset ?? 0)).toPrecision(12));
    if (p.data_type === "int" && Number.isInteger(v)) return v;
    return p.data_type === "enum" ? Math.round(v) : v;
  }

  // ------------------------------------------------------------------ live stream (EventSource stand-in)
  const streams = new Set();
  function push(msg) { streams.forEach((s) => s._message(msg)); }
  class DemoEventSource {
    constructor(url) {
      this.url = url; this.readyState = 0; this.onmessage = null; this.onerror = null; this._l = {};
      streams.add(this);
      setTimeout(() => { this.readyState = 1; (this._l.hello || []).forEach((f) => f({data: "{}"})); }, 60);
    }
    addEventListener(type, fn) { (this._l[type] = this._l[type] || []).push(fn); }
    removeEventListener(type, fn) { this._l[type] = (this._l[type] || []).filter((f) => f !== fn); }
    close() { this.readyState = 2; streams.delete(this); }
    _message(msg) {
      const ev = {data: JSON.stringify(msg)};
      if (this.onmessage) this.onmessage(ev);
      (this._l.message || []).forEach((f) => f(ev));
    }
  }
  window.EventSource = DemoEventSource;

  // ------------------------------------------------------------------ connector simulation
  function setStatus(sid, state, devices, lastError) {
    const s = sources.get(sid); if (!s) return;
    const prev = s.status?.state;
    const cfg = s.config;
    const st = {spec: "1.0", src: sid, kind: "connector", type: cfg.type, version: "1.0.0", boot: s.boot, state,
      since: prev === state ? s.status.since : iso(now()), ts: iso(now()), uptime_s: Math.round((now() - s.started) / 1000),
      config_revision: cfg.revision, devices,
      stats: {points: cfg.points.length, values_total: s.values, values_per_s: r(s.values / Math.max(1, (now() - s.started) / 1000), 2),
        batches_total: s.batches, dropped_total: 0, scan_overruns_total: 0},
      last_error: lastError || null};
    s.status = st;
    push({type: "status", source_id: sid, state, status: st});
  }
  function deviceReachable(cfg, dev, ms) {
    if (cfg.source_id === "modbus-mcc-01") return !(dev.id === "feeder-2" && feeder2Down(ms));
    return isPump(cfg.connection) && Number(dev.unit_id) === PUMP.unit;
  }
  function pointValue(cfg, p, ms) {
    if (cfg.source_id === "modbus-mcc-01") return {v: mccValue(p.tag_id, ms), q: 192};
    const raw = pumpRegister(p.address.table, p.address.address, ms);
    if (raw === undefined) return {v: null, q: 4, f: ["EXC2"]};                 // address does not exist on the device
    return {v: scale(p, raw), q: 192};
  }
  function tick() {
    const ms = now(), batch = [];
    sources.forEach((s, sid) => {
      const cfg = s.config;
      if (!cfg.enabled || ms < s.connectAt) return;
      const devs = cfg.connection.devices.map((d) => {
        const up = deviceReachable(cfg, d, ms), was = s.devState[d.id];
        if (up && was !== "connected") event(sid, "conn.connected", "info", `Device ${d.id} connected`, {device: d.id});
        if (!up && was !== "unreachable") {
          event(sid, "conn.lost", "medium", `Device ${d.id} unreachable`, {device: d.id, attrs: {error: "CONN_TIMEOUT"}});
          cfg.points.filter((p) => p.address.device === d.id).forEach((p) => batch.push(record(p.tag_id, null, 24, ms)));
        }
        s.devState[d.id] = up ? "connected" : "unreachable";
        if (up) s.lastOk[d.id] = iso(ms);
        const e = up ? null : {code: "CONN_TIMEOUT", message: `No response from unit ${d.unit_id} within ${cfg.acquisition?.request_timeout_ms ?? 1000} ms`};
        return {id: d.id, state: s.devState[d.id], last_ok: s.lastOk[d.id] || null, ...(e ? {error: e} : {})};
      });
      cfg.points.forEach((p) => {
        if (s.devState[p.address.device] !== "connected" || p.enabled === false) return;
        const {v, q, f} = pointValue(cfg, p, ms), prev = latest.get(p.tag_id);
        const db = p.deadband?.value || 0;
        const changed = !prev || prev.q !== q || (typeof v === "number" && typeof prev.v === "number" ? Math.abs(v - prev.v) > db : v !== prev.v);
        if (changed) { batch.push(record(p.tag_id, v, q, ms, f)); s.values++; }
      });
      s.batches++;
      const up = devs.filter((d) => d.state === "connected").length;
      const state = up === devs.length ? "running" : up ? "degraded" : "error";
      const err = devs.find((d) => d.error)?.error;
      if (state !== s.status?.state || ms - (s.lastStatus || 0) > 10000) { setStatus(sid, state, devs, err ? {...err, ts: iso(ms)} : null); s.lastStatus = ms; }
    });
    if (batch.length) push({type: "values", values: batch.map(({t, v, q, ts}) => ({t, v, q, ts}))});
  }

  function startSource(sid) {
    const s = sources.get(sid);
    s.boot = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    s.started = now(); s.values = 0; s.batches = 0; s.devState = {}; s.lastOk = s.lastOk || {};
    s.connectAt = now() + 1200;                                              // the connector takes a moment to connect
    if (!s.config.enabled) { setStatus(sid, "waiting_config", [], null); return; }
    setStatus(sid, "connecting", s.config.connection.devices.map((d) => ({id: d.id, state: "connecting", last_ok: null})), null);
  }

  // ------------------------------------------------------------------ validation (mirrors the Modbus schema + connector checks)
  const REG_ENC = ["float32", "uint16", "int16", "uint32", "int32", "float64", "uint64", "int64", "bit", "string"];
  const ENC = {holding_register: REG_ENC, input_register: REG_ENC, coil: ["bool"], discrete_input: ["bool"]};
  const DT_OK = {bool: ["bool", "bit"], string: ["string"], int: REG_ENC.slice(0, 8), float: REG_ENC.slice(0, 8), enum: REG_ENC.slice(0, 8)};
  function validate(cfg) {
    const e = [], c = cfg.connection || {};
    if (!cfg.name) e.push("name: 'name' is a required property");
    if (!ID_RE.test(cfg.source_id || "")) e.push(`source_id: '${cfg.source_id || ""}' does not match the ID pattern (lower case letters, digits, dashes)`);
    if (!ID_RE.test(cfg.site || "")) e.push(`site: '${cfg.site || ""}' does not match the ID pattern (lower case letters, digits, dashes)`);
    if (c.transport === "tcp") {
      if (!c.host) e.push("connection: 'host' is a required property");
      if (!(c.port >= 1 && c.port <= 65535)) e.push("connection/port: must be between 1 and 65535");
    } else if (c.transport === "rtu") { if (!c.serial?.device) e.push("connection/serial: 'device' is a required property"); }
    else e.push("connection/transport: must be 'tcp' or 'rtu'");
    const devs = c.devices || [];
    if (!devs.length) e.push("connection/devices: at least one device is required");
    devs.forEach((d, i) => {
      if (!ID_RE.test(d.id || "")) e.push(`connection/devices/${i}/id: '${d.id}' does not match the ID pattern`);
      if (!(Number.isInteger(d.unit_id) && d.unit_id >= 0 && d.unit_id <= 247)) e.push(`connection/devices/${i}/unit_id: must be a whole number from 0 to 247`);
    });
    if (new Set(devs.map((d) => d.id)).size !== devs.length) e.push("connection/devices: duplicate device id");
    if (new Set(devs.map((d) => d.unit_id)).size !== devs.length) e.push("connection/devices: two devices share a unit_id");
    const a = cfg.acquisition || {};
    if (a.default_scan_ms !== undefined && !(a.default_scan_ms >= 100)) e.push("acquisition/default_scan_ms: must be at least 100");
    const seen = new Set();
    (cfg.points || []).forEach((p, i) => {
      const ad = p.address || {};
      if (!TAG_RE.test(p.tag_id || "")) e.push(`points/${i}/tag_id: '${p.tag_id}' does not match the tag pattern (lower case, dot-separated)`);
      if (seen.has(p.tag_id)) e.push(`points/${i}/tag_id: duplicate tag_id '${p.tag_id}'`);
      seen.add(p.tag_id);
      if (!devs.some((d) => d.id === ad.device)) e.push(`points/${i}/address/device: unknown device '${ad.device}'`);
      if (!ENC[ad.table]) e.push(`points/${i}/address/table: '${ad.table}' is not a register type`);
      else if (!ENC[ad.table].includes(ad.encoding)) e.push(`points/${i}/address/encoding: '${ad.encoding}' is not allowed for ${ad.table}`);
      if (!(Number.isInteger(ad.address) && ad.address >= 0 && ad.address <= 65535)) e.push(`points/${i}/address/address: must be a whole number from 0 to 65535`);
      if (ad.encoding === "bit" && !(ad.bit >= 0 && ad.bit <= 15)) e.push(`points/${i}/address/bit: must be 0 to 15`);
      if (ad.encoding === "string" && !(ad.length >= 1)) e.push(`points/${i}/address/length: required for text`);
      if (ENC[ad.table] && !(DT_OK[p.data_type] || []).includes(ad.encoding) && !(ad.table === "coil" || ad.table === "discrete_input"))
        e.push(`points/${i}/data_type: '${p.data_type}' does not fit encoding '${ad.encoding}'`);
    });
    return e;
  }

  // ------------------------------------------------------------------ catalog
  const SPECIFIED = new Set(["modbus", "opcua", "mqtt", "iec61850", "iec104", "dnp3", "camera", "udp", "snmp"]);
  const PUMP_TAGS = [
    {name: "flow", address: {table: "holding_register", address: 0, encoding: "float32"}, data_type: "float", unit: "m3/h"},
    {name: "discharge-pressure", address: {table: "holding_register", address: 2, encoding: "float32"}, data_type: "float", unit: "bar"},
    {name: "reservoir-level", address: {table: "holding_register", address: 4, encoding: "uint16"}, data_type: "float", unit: "m", scaling: {gain: 0.01, offset: 0}},
    {name: "water-temp", address: {table: "holding_register", address: 5, encoding: "int16"}, data_type: "float", unit: "degC", scaling: {gain: 0.1, offset: 0}},
    {name: "pump-1-running", address: {table: "coil", address: 0, encoding: "bool"}, data_type: "bool"},
    {name: "pump-2-running", address: {table: "coil", address: 1, encoding: "bool"}, data_type: "bool"},
    {name: "run-hours", address: {table: "holding_register", address: 10, encoding: "uint32"}, data_type: "int", unit: "h"},
  ];
  function catalog() {
    const types = (window.__DEMO_CATALOG__ || []).map((t) => {
      const protocols = t.protocols.map((p) => ({...p, status: p.connector === "modbus" ? "available" : SPECIFIED.has(p.connector) ? "specified" : "planned"}));
      return {...t, protocols, available: protocols.filter((p) => p.status === "available").length};
    });
    return {types, available: ["modbus"], demo: {site: "ps-west", devices: [{name: "Pump Station West", device_id: "pump-plc", ...PUMP, unit_id: PUMP.unit, tags: PUMP_TAGS}]}};
  }

  // ------------------------------------------------------------------ API
  const json = (body, status = 200) => new Response(status === 204 ? null : JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  const err = (status, code, message, details) => json({error: {code, message, ...(details ? {details} : {})}}, status);
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  function prepare(body, create, sid) {
    if (!body || typeof body !== "object") return [null, err(400, "BAD_REQUEST", "body must be a JSON object")];
    const id = sid || body.source_id, existing = sources.get(id);
    if (create && existing) return [null, err(409, "EXISTS", `source ${id} already exists`)];
    if (!create && !existing) return [null, err(404, "NOT_FOUND", `source ${id} not found`)];
    const doc = {spec: "1.0", revision: existing ? existing.config.revision + 1 : 1, enabled: true, ...JSON.parse(JSON.stringify(body)), source_id: id};
    doc.spec = "1.0"; doc.revision = existing ? existing.config.revision + 1 : 1;
    if (doc.type !== "modbus") return [null, err(422, "CONNECTOR_UNAVAILABLE", `no ${doc.type} connector is installed yet; available: ['modbus']`)];
    const errors = validate(doc);
    if (errors.length) return [null, err(422, "CFG_INVALID", "configuration is not valid", errors)];
    const taken = doc.points.map((p) => p.tag_id).filter((t) => tagOwner.has(t) && tagOwner.get(t) !== id);
    if (taken.length) return [null, err(409, "TAG_IN_USE", "tag ids already belong to another source", taken)];
    return [doc, null];
  }
  function save(doc, user, create) {
    const prev = sources.get(doc.source_id);
    if (prev) prev.config.points.forEach((p) => { if (!doc.points.some((q) => q.tag_id === p.tag_id)) { tagOwner.delete(p.tag_id); latest.delete(p.tag_id); } });
    sources.set(doc.source_id, {...(prev || {}), config: doc, updated_by: user, updated_ms: now(), status: prev?.status});
    doc.points.forEach((p) => tagOwner.set(p.tag_id, doc.source_id));
    audit.unshift({ts: iso(now()), user, action: create ? "create_source" : "update_source", target: doc.source_id, detail: `revision ${doc.revision}`});
    const restart = !prev || JSON.stringify(prev.config.connection) !== JSON.stringify(doc.connection) || prev.config.enabled !== doc.enabled;
    if (restart) startSource(doc.source_id);
    event(doc.source_id, "config.applied", "info", `Configuration revision ${doc.revision} applied`, {attrs: {revision: doc.revision}});
  }
  function readAddress(cfg, address, data_type) {
    const dev = cfg.connection.devices.find((d) => d.id === address.device);
    if (!dev || !deviceReachable(cfg, dev, now())) return {v: null, q: 24};
    return pointValue(cfg, {tag_id: "adhoc", address, data_type}, now());
  }
  async function testConnection(cfg) {
    const pump = isPump(cfg.connection);
    if (!pump && cfg.source_id !== "modbus-mcc-01") await sleep(Number(cfg.acquisition?.request_timeout_ms) || 1000);
    return {devices: cfg.connection.devices.map((d) => {
      const ok = cfg.source_id === "modbus-mcc-01" ? deviceReachable(cfg, d, now()) : pump && Number(d.unit_id) === PUMP.unit;
      return ok ? {id: d.id, ok: true, latency_ms: r(rnd(2, 9), 1)}
        : {id: d.id, ok: false, latency_ms: Number(cfg.acquisition?.request_timeout_ms) || 1000,
          error: {code: "CONN_TIMEOUT", message: pump ? `No response from unit ${d.unit_id}` : `No response from ${cfg.connection.host}:${cfg.connection.port} (in this demo, only the simulated devices answer)`}};
    })};
  }
  function listSources() {
    return [...sources.values()].map((s) => ({source_id: s.config.source_id, site: s.config.site, type: s.config.type, name: s.config.name,
      enabled: s.config.enabled, revision: s.config.revision, updated: iso(s.updated_ms), updated_by: s.updated_by,
      state: s.status?.state || "unknown", status: s.status || null})).sort((a, b) => (a.site + a.source_id).localeCompare(b.site + b.source_id));
  }
  function listTags(url) {
    const src = url.searchParams.get("source"), q = url.searchParams.get("q");
    const out = [];
    sources.forEach((s) => s.config.points.forEach((p) => {
      if (p.enabled === false || (src && s.config.source_id !== src) || (q && !p.tag_id.includes(q))) return;
      const l = latest.get(p.tag_id);
      out.push({tag_id: p.tag_id, source_id: s.config.source_id, site: s.config.site, data_type: p.data_type, unit: p.unit || null,
        v: l ? l.v : null, q: l ? l.q : null, ts: l?.ts || null, rx: l?.rx || null, f: l?.f || null});
    }));
    return out.sort((a, b) => a.tag_id.localeCompare(b.tag_id));
  }

  async function handle(method, url, body) {
    const p = url.pathname.replace(/^.*\/api\//, "/api/");
    let m;
    if (p === "/api/health") return json({status: "ok", time: iso(now()), db: "browser (demo)", login_hint: TOKEN});
    if (p === "/api/me") return json({user: "khaled", role: "admin"});
    if (p === "/api/catalog") return json(catalog());
    if (p === "/api/sources" && method === "GET") return json(listSources());
    if (p === "/api/sources" && method === "POST") { const [doc, e] = prepare(body, true); if (e) return e; save(doc, "khaled", true); return json(doc, 201); }
    if (p === "/api/sources-check") {
      const create = url.searchParams.get("mode") !== "update";
      const [doc, e] = prepare(body, create, url.searchParams.get("source_id")); return e || json({ok: true, config: doc});
    }
    if (p === "/api/sources-test") {
      const create = url.searchParams.get("mode") !== "update";
      const [doc, e] = prepare(body, create, url.searchParams.get("source_id"));
      if (e) { const b = await e.json(); return json({spec: "1.0", request_id: ulid(), src: "edge-agent", ok: false, done: true, ts: iso(now()), error: {code: b.error.code, message: [].concat(b.error.details || b.error.message).join("; ")}}, 502); }
      return json({spec: "1.0", request_id: ulid(), src: "edge-agent", ok: true, done: true, ts: iso(now()), result: await testConnection(doc)});
    }
    if ((m = p.match(/^\/api\/sources\/([^/]+)\/admin\/([a-z_]+)$/))) {
      const s = sources.get(decodeURIComponent(m[1])), op = m[2];
      if (!s) return err(404, "NOT_FOUND", "source not found");
      const res = {spec: "1.0", request_id: ulid(), src: s.config.source_id, ok: true, done: true, ts: iso(now())};
      if (op === "ping") res.result = {version: "1.0.0", state: s.status?.state};
      else if (op === "test_connection") res.result = await testConnection(s.config);
      else if (op === "read_now") res.result = {values: (body?.addresses || []).map((a) => ({address: a.address, ...readAddress(s.config, a.address, a.data_type), ts: iso(now())}))};
      else if (op === "get_diagnostics") res.result = {runtime: {state: s.status?.state, revision: s.config.revision}};
      else return json({...res, ok: false, error: {code: "OP_UNSUPPORTED", message: `${op} is not supported by the modbus connector`}}, 502);
      await sleep(rnd(80, 250));
      return json(res);
    }
    if ((m = p.match(/^\/api\/sources\/([^/]+)$/))) {
      const sid = decodeURIComponent(m[1]), s = sources.get(sid);
      if (method === "GET") return s ? json({config: s.config, status: s.status || null}) : err(404, "NOT_FOUND", `source ${sid} not found`);
      if (method === "PUT") { const [doc, e] = prepare(body, false, sid); if (e) return e; save(doc, "khaled", false); return json(doc); }
      if (method === "DELETE") {
        if (!s) return err(404, "NOT_FOUND", `source ${sid} not found`);
        s.config.points.forEach((pt) => { tagOwner.delete(pt.tag_id); latest.delete(pt.tag_id); });
        sources.delete(sid);
        audit.unshift({ts: iso(now()), user: "khaled", action: "delete_source", target: sid, detail: ""});
        return json(null, 204);
      }
    }
    if (p === "/api/tags") return json(listTags(url));
    if ((m = p.match(/^\/api\/tags\/([^/]+)\/history$/))) {
      const minutes = Number(url.searchParams.get("minutes") || 60), from = now() - minutes * 60000;
      return json((history.get(decodeURIComponent(m[1])) || []).filter((h) => Date.parse(h.ts) >= from).slice(-Number(url.searchParams.get("limit") || 2000)));
    }
    if (p === "/api/events") return json(events.slice(0, Number(url.searchParams.get("limit") || 100)));
    if (p === "/api/audit") return json(audit);
    if (p === "/api/stats") return json({ingest: {}, sequences: {}, live_clients: streams.size});
    return err(404, "NOT_FOUND", `no route ${method} ${p}`);
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, opts = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.includes("/api/")) return realFetch(input, opts);
    let body;
    try { body = opts.body ? JSON.parse(opts.body) : undefined; } catch (e) { return err(400, "BAD_REQUEST", "body is not valid JSON"); }
    await sleep(rnd(15, 60));                                                     // a little network-like delay
    return handle((opts.method || "GET").toUpperCase(), url, body);
  };

  // ------------------------------------------------------------------ the substation that is already configured
  const hr = (dev, address, encoding, extra = {}) => ({device: dev, table: "holding_register", address, encoding, ...extra});
  const mccPoints = [
    ["incomer.p-total", "float", "kW", hr("incomer", 0, "float32")], ["incomer.q-total", "float", "kvar", hr("incomer", 2, "float32")],
    ["incomer.voltage-l1", "float", "V", hr("incomer", 4, "uint16"), 0.1], ["incomer.current-l1", "float", "A", hr("incomer", 5, "uint16"), 0.1],
    ["incomer.frequency", "float", "Hz", hr("incomer", 6, "uint16"), 0.01], ["incomer.pf", "float", null, hr("incomer", 7, "int16"), 0.001],
    ["incomer.energy-import", "int", "kWh", hr("incomer", 10, "uint32")], ["incomer.overcurrent-alarm", "bool", null, hr("incomer", 12, "bit", {bit: 0})],
    ["incomer.breaker-closed", "bool", null, {device: "incomer", table: "coil", address: 0, encoding: "bool"}],
    ["feeder-2.p-total", "float", "kW", hr("feeder-2", 0, "float32")], ["feeder-2.current-l1", "float", "A", hr("feeder-2", 5, "uint16"), 0.1],
    ["tr1.oil-temp", "float", "degC", {device: "tr1-relay", table: "input_register", address: 100, encoding: "int16"}, 0.1],
    ["tr1.winding-temp", "float", "degC", {device: "tr1-relay", table: "input_register", address: 101, encoding: "int16"}, 0.1],
  ].map(([name, dt, unit, address, gain]) => ({tag_id: `ss-giza.mcc01.${name}`, data_type: dt, address, ...(unit ? {unit} : {}),
    ...(gain ? {scaling: {gain, offset: 0}} : {}), ...(dt === "float" ? {deadband: {mode: "abs", value: 0.05}} : {})}));
  const mcc = {spec: "1.0", revision: 1, site: "ss-giza-220kv", source_id: "modbus-mcc-01", type: "modbus", name: "MCC-01 energy meters", enabled: true,
    connection: {transport: "tcp", host: "10.20.1.31", port: 502, devices: [{id: "incomer", unit_id: 1, name: "Incomer meter"},
      {id: "feeder-2", unit_id: 2, name: "Feeder 2 meter"}, {id: "tr1-relay", unit_id: 3, name: "Transformer 1 relay"}]},
    acquisition: {default_scan_ms: 1000, batch_max_values: 500, batch_max_ms: 500, integrity_s: 300, request_timeout_ms: 800},
    points: mccPoints};
  sources.set(mcc.source_id, {config: mcc, updated_by: "demo-setup", updated_ms: t0 - 3600e3});
  mcc.points.forEach((p) => tagOwner.set(p.tag_id, mcc.source_id));
  for (let ms = t0 - 15 * 60e3; ms < t0; ms += 5000) {                         // 15 minutes of history for the charts
    mcc.points.forEach((p) => { if (p.address.device === "feeder-2" && feeder2Down(ms)) record(p.tag_id, null, 24, ms); else record(p.tag_id, mccValue(p.tag_id, ms), 192, ms); });
  }
  const s0 = sources.get(mcc.source_id);
  s0.boot = "demo"; s0.started = t0 - 3600e3; s0.values = 3600 * 8; s0.batches = 3600; s0.devState = {incomer: "connected", "feeder-2": "connected", "tr1-relay": "connected"};
  s0.lastOk = {}; s0.connectAt = 0;
  [["conn.connected", "info", "Device incomer connected"], ["conn.connected", "info", "Device feeder-2 connected"],
    ["conn.connected", "info", "Device tr1-relay connected"]].forEach(([t, sev, msg], i) =>
    events.unshift({spec: "1.0", id: ulid(), src: mcc.source_id, type: t, severity: sev, ts: iso(t0 - 3600e3 + i * 1000), message: msg}));
  setInterval(tick, 1000);
  setTimeout(tick, 50);
})();
