/* Opcare DAQ – in-browser stand-in for the platform server, used only by the hosted web demo.
   It answers the same /api/* requests and live stream as daq_platform (see platform/README.md) and
   simulates the demo devices of tools/demo/run_demo.py, so the real screens (app.js, config.js) run
   unchanged in a static page. Data lives in memory and resets when the page reloads.
   window.__DEMO_CATALOG__ (source types/protocols) is embedded by build_web_demo.py from catalog.py. */
(() => {
  "use strict";
  const TOKEN = "opcare-demo";
  try { sessionStorage.removeItem("opcare-token"); } catch (e) { /* the demo always starts at the sign-in screen */ }

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


  // ------------------------------------------------------------------ UDP station and SNMP agents (spec 15.8, 15.9)
  // Same devices as run_demo.py: a GNSS + weather station pushing NMEA 0183, JSON and syslog to UDP port 5514,
  // and two SNMP agents (switch on 161, UPS on 1161; community "public", v3 user opcare-ro).
  const STATION = {host: "127.0.0.1", port: 5514};
  const SNMP_V3 = {username: "opcare-ro", auth_protocol: "SHA-256", auth_password: "opcare-auth-2026", priv_password: "opcare-priv-2026"};
  const AGENTS = {161: "switch", 1161: "ups"};
  const TRAP_PORT = 10162;
  const secrets = new Map();
  const nmeaLine = (body) => { let c = 0; for (const ch of body) c ^= ch.charCodeAt(0); return `$${body}*${c.toString(16).toUpperCase().padStart(2, "0")}`; };
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  function stationFrame(ms) {
    const t = (ms - t0) / 1000, d = new Date(ms);
    const utc = `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}.00`;
    const lat = (2959.81 + 0.05 * Math.sin(t / 60)).toFixed(4), lon = (3115.42 + 0.05 * Math.cos(t / 60)).toFixed(4).padStart(9, "0");
    const knots = Math.max(0, 4 + 3 * Math.sin(t / 20)).toFixed(1);
    const date = `${pad(d.getUTCDate())}${pad(d.getUTCMonth() + 1)}${String(d.getUTCFullYear()).slice(2)}`;
    const nmea = [nmeaLine(`GPRMC,${utc},A,${lat},N,${lon},E,${knots},${((t * 3) % 360).toFixed(1)},${date},,,A`),
      nmeaLine(`GPGGA,${utc},${lat},N,${lon},E,1,${9 + Math.floor(t / 30) % 3},0.9,${(38 + Math.sin(t / 50)).toFixed(1)},M,15.0,M,,`)];
    const doc = {id: "ws-1", ts: Math.floor(ms / 1000) * 1000, temp: r(27 + 3 * Math.sin(t / 300), 2), humidity: r(40 + 10 * Math.sin(t / 200), 1),
      wind: {speed: r(Math.max(0, 5 + 2 * Math.sin(t / 15)), 2), dir: Math.round((200 + 40 * Math.sin(t / 90) + 360) % 360)}};
    const n = Math.floor(t / 30);
    const sys = `<12>${d.toUTCString().slice(8, 11)} ${pad(d.getUTCDate())} ${utc.slice(0, 2)}:${utc.slice(2, 4)}:${utc.slice(4, 6)} fw-1 kernel: blocked port scan from 10.9.8.${n % 200}`;
    return {lines: [...nmea, JSON.stringify(doc)], doc, syslog: sys, syslogNew: Math.floor(t) % 30 === 15};
  }
  function ipMatch(host, ip) {
    if (!host) return false;
    const [net, bits] = String(host).trim().split("/");
    if (bits === undefined) return net === ip;
    const toN = (x) => x.split(".").reduce((a, b) => (a << 8) + Number(b), 0) >>> 0;
    const mask = Number(bits) === 0 ? 0 : (~0 << (32 - Number(bits))) >>> 0;
    return /^\d+\.\d+\.\d+\.\d+$/.test(net) && ((toN(net) & mask) >>> 0) === ((toN(ip) & mask) >>> 0);
  }
  const stationHeard = (cfg, sender) => Number(cfg.connection.bind_port) === STATION.port && ipMatch(sender.host, STATION.host);
  function jsonPointer(doc, ptr) {
    let cur = doc;
    for (const raw of String(ptr).split("/").slice(1)) {
      const k = raw.replace(/~1/g, "/").replace(/~0/g, "~");
      if (cur === null || typeof cur !== "object" || !(k in cur)) throw new Error("missing");
      cur = cur[k];
    }
    return cur;
  }
  function parseNmea(line) {
    if (!/^[$!]/.test(line)) return null;
    const [body] = line.slice(1).split("*"), parts = body.split(","), a = parts[0];
    return [a.startsWith("P") ? a : a.slice(2), parts.slice(1)];
  }
  function parseSyslog(line) {
    const m = line.match(/^<(\d{1,3})>(?:([A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (\S+) )?(?:([^:\[\s]+)(?:\[\d+\])?: )?(.*)$/);
    if (!m) return null;
    const pri = Number(m[1]);
    return {severity: pri % 8, facility: Math.floor(pri / 8), hostname: m[3] || null, app_name: m[4] || null, message: m[5]};
  }
  function coerce(v, dt) {
    if (v === undefined || v === null || v === "") return undefined;
    if (dt === "string") return typeof v === "string" ? v : JSON.stringify(v);
    if (dt === "bool") { const x = String(v).toLowerCase(); return ["1", "true", "on", "a", "up"].includes(x) ? true : ["0", "false", "off", "v", "down"].includes(x) ? false : NaN; }
    const n = Number(v);
    return Number.isFinite(n) ? n : NaN;
  }
  function udpValue(p, ms) {
    const a = p.address, f = stationFrame(ms);
    let raw;
    try {
      if (a.format === "json") {
        if (a.match_pointer && String(jsonPointer(f.doc, a.match_pointer)) !== a.match_value) return null;
        raw = jsonPointer(f.doc, a.value_pointer);
      } else if (a.format === "nmea") {
        const got = f.lines.map(parseNmea).find((x) => x && x[0] === a.sentence);
        if (!got) return null;
        const fld = got[1][a.field - 1];
        if (fld === undefined || fld === "") return {v: null, q: 0, f: ["PARSE_ERROR"]};
        if (a.coordinate) { const v = Number(fld), deg = Math.floor(v / 100); raw = r((deg + (v - deg * 100) / 60) * (/[SW]/.test(got[1][a.field]) ? -1 : 1), 7); }
        else raw = fld;
      } else if (a.format === "syslog") {
        const m = parseSyslog(f.syslog);
        if (!m || (a.match && !new RegExp(a.match).test(m.message))) return null;
        raw = m[a.field];
      } else if (a.format === "raw") {
        const line = f.lines.find((l) => !a.prefix || l.startsWith(a.prefix));
        if (!line) return null;
        raw = line.slice((a.prefix || "").length);
      } else return null;                                  // delimited / binary: this station does not send them
    } catch (e) { return {v: null, q: 0, f: ["PARSE_ERROR"]}; }
    const v = coerce(raw, p.data_type);
    if (v === undefined || Number.isNaN(v)) return {v: null, q: 0, f: ["PARSE_ERROR"]};
    return {v: typeof v === "boolean" || typeof v === "string" ? v : scale(p, v), q: 192};
  }

  // ---- SNMP agents
  const T = {INT: "INTEGER", STR: "OCTET STRING", OID: "OBJECT IDENTIFIER", TT: "TimeTicks", C32: "Counter32", C64: "Counter64", G32: "Gauge32"};
  function mib(kind, ms) {
    const t = (ms - t0) / 1000, m = {};
    const set = (oid, type, v) => { m[oid] = [type, v]; };
    const ports = kind === "switch" ? 8 : 1;
    set("1.3.6.1.2.1.1.1.0", T.STR, kind === "switch" ? "Opcare demo switch, 8 x 1 GbE" : "Opcare demo UPS 10 kVA");
    set("1.3.6.1.2.1.1.2.0", T.OID, "1.3.6.1.4.1.99999.1");
    set("1.3.6.1.2.1.1.3.0", T.TT, Math.floor(t * 100) + 8640000);
    set("1.3.6.1.2.1.1.5.0", T.STR, kind === "switch" ? "server-room-sw" : "ups-1");
    set("1.3.6.1.2.1.1.6.0", T.STR, "Server room");
    set("1.3.6.1.2.1.2.1.0", T.INT, ports);
    for (let i = 1; i <= ports; i++) {
      const rate = 125000 * ((i * 7) % 10 + 1), up = !(kind === "switch" && i === 4 && Math.floor(t / 45) % 2 === 1);
      set(`1.3.6.1.2.1.2.2.1.1.${i}`, T.INT, i);
      set(`1.3.6.1.2.1.2.2.1.2.${i}`, T.STR, `GigabitEthernet1/0/${i}`);
      set(`1.3.6.1.2.1.2.2.1.5.${i}`, T.G32, 1000000000);
      set(`1.3.6.1.2.1.2.2.1.7.${i}`, T.INT, 1);
      set(`1.3.6.1.2.1.2.2.1.8.${i}`, T.INT, up ? 1 : 2);
      set(`1.3.6.1.2.1.2.2.1.10.${i}`, T.C32, Math.floor(t * rate) % 4294967296);
      set(`1.3.6.1.2.1.2.2.1.14.${i}`, T.C32, Math.floor(t / (20 * i)));
      set(`1.3.6.1.2.1.2.2.1.16.${i}`, T.C32, Math.floor(t * rate * 0.6) % 4294967296);
      set(`1.3.6.1.2.1.31.1.1.1.1.${i}`, T.STR, `Gi1/0/${i}`);
      set(`1.3.6.1.2.1.31.1.1.1.6.${i}`, T.C64, Math.floor(t * rate));
      set(`1.3.6.1.2.1.31.1.1.1.10.${i}`, T.C64, Math.floor(t * rate * 0.6));
      set(`1.3.6.1.2.1.31.1.1.1.15.${i}`, T.G32, 1000);
    }
    if (kind === "ups") {
      set("1.3.6.1.2.1.33.1.1.1.0", T.STR, "Opcare Demo UPS");
      set("1.3.6.1.2.1.33.1.2.1.0", T.INT, 2);
      set("1.3.6.1.2.1.33.1.2.3.0", T.INT, 42);
      set("1.3.6.1.2.1.33.1.2.4.0", T.INT, 100 - Math.floor(t / 60) % 5);
      set("1.3.6.1.2.1.33.1.2.7.0", T.INT, 24 + Math.floor(t / 30) % 3);
      set("1.3.6.1.2.1.33.1.3.3.1.3.1", T.INT, 230);
      set("1.3.6.1.2.1.33.1.4.1.0", T.INT, 3);
      set("1.3.6.1.2.1.33.1.4.4.1.5.1", T.INT, 35 + Math.round(5 * Math.abs(((t / 20) % 2) - 1)));
    }
    return m;
  }
  const oidKey = (o) => o.split(".").map((x) => x.padStart(10, "0")).join(".");
  const NAMES = {"1.3.6.1.2.1.1.1": "sysDescr", "1.3.6.1.2.1.1.2": "sysObjectID", "1.3.6.1.2.1.1.3": "sysUpTime", "1.3.6.1.2.1.1.5": "sysName",
    "1.3.6.1.2.1.1.6": "sysLocation", "1.3.6.1.2.1.2.1": "ifNumber", "1.3.6.1.2.1.2.2.1.1": "ifIndex", "1.3.6.1.2.1.2.2.1.2": "ifDescr",
    "1.3.6.1.2.1.2.2.1.5": "ifSpeed", "1.3.6.1.2.1.2.2.1.7": "ifAdminStatus", "1.3.6.1.2.1.2.2.1.8": "ifOperStatus", "1.3.6.1.2.1.2.2.1.10": "ifInOctets",
    "1.3.6.1.2.1.2.2.1.14": "ifInErrors", "1.3.6.1.2.1.2.2.1.16": "ifOutOctets", "1.3.6.1.2.1.31.1.1.1.1": "ifName", "1.3.6.1.2.1.31.1.1.1.6": "ifHCInOctets",
    "1.3.6.1.2.1.31.1.1.1.10": "ifHCOutOctets", "1.3.6.1.2.1.31.1.1.1.15": "ifHighSpeed", "1.3.6.1.2.1.33.1.1.1": "upsIdentManufacturer",
    "1.3.6.1.2.1.33.1.2.1": "upsBatteryStatus", "1.3.6.1.2.1.33.1.2.3": "upsEstimatedMinutesRemaining", "1.3.6.1.2.1.33.1.2.4": "upsEstimatedChargeRemaining",
    "1.3.6.1.2.1.33.1.2.7": "upsBatteryTemperature", "1.3.6.1.2.1.33.1.3.3.1.3": "upsInputVoltage", "1.3.6.1.2.1.33.1.4.1": "upsOutputSource",
    "1.3.6.1.2.1.33.1.4.4.1.5": "upsOutputPercentLoad"};
  function nameOf(oid) {
    const parts = oid.split(".");
    for (let n = parts.length; n > 5; n--) { const b = parts.slice(0, n).join("."); if (NAMES[b]) return NAMES[b] + (n < parts.length ? "." + parts.slice(n).join(".") : ""); }
    return oid;
  }
  function snmpAccess(cfg, dev) {
    const conn = cfg.connection, port = Number(dev.port || conn.port || 161), kind = AGENTS[port];
    if (String(dev.host).trim() !== "127.0.0.1" || !kind) return {error: {code: "CONN_TIMEOUT", message: `no response from ${dev.host}:${port} (in this demo the switch answers on 127.0.0.1:161 and the UPS on 127.0.0.1:1161)`}};
    if (conn.version === "3") {
      const v3 = conn.v3 || {};
      if (v3.username !== SNMP_V3.username) return {error: {code: "AUTH_FAILED", message: "SNMPv3: usmStatsUnknownUserNames"}};
      if (v3.security_level !== "noAuthNoPriv" && secrets.get(v3.auth_password_ref) !== SNMP_V3.auth_password) return {error: {code: "AUTH_FAILED", message: "SNMPv3: usmStatsWrongDigests"}};
      if (v3.security_level === "authPriv" && secrets.get(v3.priv_password_ref) !== SNMP_V3.priv_password) return {error: {code: "AUTH_FAILED", message: "SNMPv3: usmStatsDecryptionErrors"}};
      if (v3.security_level !== "authPriv") return {error: {code: "AUTH_FAILED", message: "SNMPv3: usmStatsUnsupportedSecLevels"}};
    } else if (secrets.get(conn.community_ref) !== "public") {
      return {error: {code: "CONN_TIMEOUT", message: `no response from ${dev.host}:${port} — check the community (agents ignore a wrong one)`}};
    }
    return {kind};
  }
  function snmpConvert(p, entry, s, ms) {
    if (!entry) return {v: null, q: 4, f: ["noSuchInstance"]};
    const [type, val] = entry, dt = p.data_type;
    if (p.address.transform === "rate") {
      if (![T.C32, T.C64, T.G32, T.INT, T.TT].includes(type)) return {v: null, q: 4, f: ["TYPE_MISMATCH"]};
      const prev = s && s.rates[p.tag_id];
      if (s) s.rates[p.tag_id] = {v: val, t: ms};
      if (!prev || ms <= prev.t) return null;
      let d = val - prev.v; if (d < 0) d += type === T.C32 ? 4294967296 : NaN;
      if (!Number.isFinite(d)) return null;
      return {v: scale(p, d / ((ms - prev.t) / 1000)), q: 192};
    }
    if (type === T.STR || type === T.OID) {
      if (dt === "string") return {v: val, q: 192};
      const n = Number(val);
      return dt !== "bool" && Number.isFinite(n) && String(val).trim() !== "" ? {v: scale(p, n), q: 192} : {v: null, q: 4, f: ["TYPE_MISMATCH"]};
    }
    if (dt === "bool") return {v: val === 1, q: 192};
    if (dt === "string") return {v: String(val), q: 192};
    return {v: scale(p, val), q: 192};
  }
  const devicesOf = (cfg) => cfg.type === "udp" ? cfg.connection.senders : cfg.connection.devices;
  function walk(cfg, path, maxItems) {
    const [devId, rest = "1.3.6.1.2.1"] = String(path || "").split(/\/(.*)/s);
    const dev = cfg.connection.devices.find((d) => d.id === (devId || cfg.connection.devices[0].id));
    if (!dev) return {error: {code: "ADDR_INVALID", message: `unknown device '${devId}'`}};
    const acc = snmpAccess(cfg, dev);
    if (acc.error) return {error: acc.error};
    const [root, after] = rest.split(">");
    const m = mib(acc.kind, now());
    const keys = Object.keys(m).filter((o) => (o + ".").startsWith(root.replace(/\.$/, "") + ".")).sort((a, b) => oidKey(a).localeCompare(oidKey(b)));
    const start = after ? keys.findIndex((o) => oidKey(o) > oidKey(after)) : 0;
    const page = start < 0 ? [] : keys.slice(start, start + maxItems);
    const items = page.map((o) => ({path: `${dev.id}/${o}`, name: nameOf(o), kind: "variable", has_children: false,
      data_type: [T.STR, T.OID].includes(m[o][0]) ? "string" : "int", address: {device: dev.id, oid: o}, info: {type: m[o][0], sample: String(m[o][1]).slice(0, 120)}}));
    const result = {items};
    if (start >= 0 && start + maxItems < keys.length) result.next_path = `${dev.id}/${root}>${page[page.length - 1]}`;
    return {result};
  }
  function listen(cfg, depth) {
    const f = stationFrame(now()), conn = cfg.connection;
    if (Number(conn.bind_port) !== STATION.port) return {items: []};
    const sender = conn.senders.find((x) => ipMatch(x.host, STATION.host));
    const dev = sender ? sender.id : "?";
    const items = [{path: STATION.host, name: STATION.host + (sender ? "" : " (not a configured sender)"), kind: "object", has_children: true,
      info: {device: sender ? sender.id : null, datagrams: 3 + Math.floor((now() - t0) / 1000) % 40, format: "json, nmea, syslog", sample: f.lines[2]}}];
    if (depth < 2) return {items};
    const v = (name, sample, address, data_type) => items.push({path: `${STATION.host}/${name}`, name, kind: "variable", has_children: false, data_type,
      address: {device: dev, ...address}, info: {sample: String(sample).slice(0, 80)}});
    const leaves = (o, ptr) => Object.entries(o).forEach(([k, x]) => (x && typeof x === "object") ? leaves(x, `${ptr}/${k}`) : v(`${ptr}/${k}`, x, {format: "json", value_pointer: `${ptr}/${k}`},
      typeof x === "string" ? "string" : Number.isInteger(x) ? "int" : "float"));
    leaves(f.doc, "");
    f.lines.map(parseNmea).filter(Boolean).forEach(([sent, fields]) => fields.slice(0, 30).forEach((x, i) => x !== "" &&
      v(`${sent}.${i + 1}`, x, {format: "nmea", sentence: sent, field: i + 1}, Number.isFinite(Number(x)) ? "float" : "string")));
    const sl = parseSyslog(f.syslog);
    ["severity", "facility", "message", "app_name", "hostname"].forEach((k) => v(k, sl[k], {format: "syslog", field: k}, k === "severity" || k === "facility" ? "int" : "string"));
    return {items};
  }
  function validateOther(cfg) {
    const e = [], c = cfg.connection || {};
    if (!cfg.name) e.push("name: 'name' is a required property");
    if (!ID_RE.test(cfg.source_id || "")) e.push(`source_id: '${cfg.source_id || ""}' does not match the ID pattern (lower case letters, digits, dashes)`);
    if (!ID_RE.test(cfg.site || "")) e.push(`site: '${cfg.site || ""}' does not match the ID pattern (lower case letters, digits, dashes)`);
    const devs = (cfg.type === "udp" ? c.senders : c.devices) || [], what = cfg.type === "udp" ? "senders" : "devices";
    if (!devs.length) e.push(`connection/${what}: at least one is required`);
    devs.forEach((d, i) => {
      if (!ID_RE.test(d.id || "")) e.push(`connection/${what}/${i}/id: '${d.id}' does not match the ID pattern`);
      if (!d.host) e.push(`connection/${what}/${i}: 'host' is a required property`);
    });
    if (new Set(devs.map((d) => d.id)).size !== devs.length) e.push(`connection/${what}: duplicate id`);
    if (cfg.type === "udp" && !(Number.isInteger(c.bind_port) && c.bind_port >= 1 && c.bind_port <= 65535)) e.push("connection/bind_port: must be a whole number from 1 to 65535");
    if (cfg.type === "snmp") {
      if (!["1", "2c", "3"].includes(c.version)) e.push("connection/version: must be '1', '2c' or '3'");
      if (c.version === "3" && !c.v3?.username) e.push("connection/v3: 'username' is a required property");
      if (c.version !== "3" && !c.community_ref) e.push("connection: 'community_ref' is a required property");
    }
    const seen = new Set();
    (cfg.points || []).forEach((p, i) => {
      const a = p.address || {};
      if (!TAG_RE.test(p.tag_id || "")) e.push(`points/${i}/tag_id: '${p.tag_id}' does not match the tag pattern (lower case, dot-separated)`);
      if (seen.has(p.tag_id)) e.push(`points/${i}/tag_id: duplicate tag_id '${p.tag_id}'`);
      seen.add(p.tag_id);
      if (!devs.some((d) => d.id === a.device)) e.push(`points/${i}/address/device: unknown ${cfg.type === "udp" ? "sender" : "device"} '${a.device}'`);
      if (cfg.type === "snmp" && !/^\.?[0-2](\.(0|[1-9]\d{0,9}))+$/.test(a.oid || "")) e.push(`points/${i}/address/oid: '${a.oid || ""}' is not a numeric OID`);
      if (cfg.type === "udp" && a.format === "json" && !/^(\/[^/]*)*$/.test(a.value_pointer || "x")) e.push(`points/${i}/address/value_pointer: must start with /`);
      if (cfg.type === "udp" && a.format === "nmea" && !(a.sentence && a.field >= 1)) e.push(`points/${i}/address: NMEA needs a sentence and a field from 1`);
    });
    const refs = JSON.stringify(c).match(/"[a-z_]*_ref":"[^"]*"/g) || [];
    refs.forEach((x) => { const [k, v] = x.replace(/"/g, "").split(":"); if (!secrets.has(v)) e.push(`connection/${k}: secret '${v}' is not stored yet — enter the password again`); });
    return e;
  }

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
    if (cfg.type === "udp") return stationHeard(cfg, dev);
    if (cfg.type === "snmp") return !snmpAccess(cfg, dev).error;
    if (cfg.source_id === "modbus-mcc-01") return !(dev.id === "feeder-2" && feeder2Down(ms));
    return isPump(cfg.connection) && Number(dev.unit_id) === PUMP.unit;
  }
  function pointValue(cfg, p, ms, s) {
    if (cfg.type === "udp") return udpValue(p, ms);
    if (cfg.type === "snmp") {
      const dev = cfg.connection.devices.find((d) => d.id === p.address.device), acc = dev ? snmpAccess(cfg, dev) : {};
      return acc.kind ? snmpConvert(p, mib(acc.kind, ms)[String(p.address.oid).replace(/^\./, "")], s, ms) : null;
    }
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
      if (cfg.type === "snmp" && ms - (s.lastPoll || 0) < (cfg.acquisition?.default_scan_ms || 10000) - 50) return;   // SNMP polls slower
      s.lastPoll = ms;
      const devs = devicesOf(cfg).map((d) => {
        const up = deviceReachable(cfg, d, ms), was = s.devState[d.id];
        if (up && was !== "connected") event(sid, "conn.connected", "info", `Device ${d.id} connected`, {device: d.id});
        if (!up && was !== "unreachable") {
          event(sid, "conn.lost", "medium", `Device ${d.id} unreachable`, {device: d.id, attrs: {error: "CONN_TIMEOUT"}});
          cfg.points.filter((p) => p.address.device === d.id).forEach((p) => batch.push(record(p.tag_id, null, 24, ms)));
        }
        s.devState[d.id] = up ? "connected" : "unreachable";
        if (up) s.lastOk[d.id] = iso(ms);
        const e = up ? null : cfg.type === "snmp" ? snmpAccess(cfg, d).error : cfg.type === "udp"
          ? {code: "CONN_TIMEOUT", message: `no datagram from sender ${d.id} for ${cfg.connection.silence_timeout_s ?? 60} s`}
          : {code: "CONN_TIMEOUT", message: `No response from unit ${d.unit_id} within ${cfg.acquisition?.request_timeout_ms ?? 1000} ms`};
        return {id: d.id, state: s.devState[d.id], last_ok: s.lastOk[d.id] || null, ...(e ? {error: e} : {})};
      });
      cfg.points.forEach((p) => {
        if (s.devState[p.address.device] !== "connected" || p.enabled === false) return;
        const pv = pointValue(cfg, p, ms, s);
        if (!pv) return;                                                         // nothing new for this point (no telegram / first rate sample)
        const {v, q, f} = pv, prev = latest.get(p.tag_id);
        const db = p.deadband?.value || 0;
        const changed = !prev || prev.q !== q || (typeof v === "number" && typeof prev.v === "number" ? Math.abs(v - prev.v) > db : v !== prev.v);
        if (changed) { batch.push(record(p.tag_id, v, q, ms, f)); s.values++; }
      });
      s.batches++;
      notifications(sid, cfg, s, ms);
      const up = devs.filter((d) => d.state === "connected").length;
      const state = up === devs.length ? "running" : up ? "degraded" : "error";
      const err = devs.find((d) => d.error)?.error;
      if (state !== s.status?.state || ms - (s.lastStatus || 0) > 10000) { setStatus(sid, state, devs, err ? {...err, ts: iso(ms)} : null); s.lastStatus = ms; }
    });
    if (batch.length) push({type: "values", values: batch.map(({t, v, q, ts}) => ({t, v, q, ts}))});
  }

  function notifications(sid, cfg, s, ms) {
    const t = Math.floor((ms - t0) / 1000);
    if (cfg.type === "udp" && cfg.connection.syslog && t % 30 === 15 && s.lastSys !== t && cfg.connection.senders.some((x) => stationHeard(cfg, x))) {
      s.lastSys = t;
      const sender = cfg.connection.senders.find((x) => stationHeard(cfg, x)), m = parseSyslog(stationFrame(ms).syslog);
      event(sid, "syslog.message", "medium", m.message, {device: sender.id, attrs: {severity: "warning", facility: m.facility, hostname: m.hostname, app_name: m.app_name}});
    }
    if (cfg.type === "snmp" && cfg.connection.traps) {
      const sw = cfg.connection.devices.find((d) => snmpAccess(cfg, d).kind === "switch"), phase = Math.floor((ms - t0) / 45000);
      if (sw && s.trapPhase !== undefined && phase !== s.trapPhase) {
        const down = phase % 2 === 1;
        event(sid, "snmp.trap", down ? "medium" : "info", `${down ? "linkDown" : "linkUp"} from ${sw.id}: ifIndex.4=4, ifDescr.4=GigabitEthernet1/0/4`,
          {device: sw.id, attrs: {trap: down ? "linkDown" : "linkUp", trap_oid: `1.3.6.1.6.3.1.1.5.${down ? 3 : 4}`}});
      }
      s.trapPhase = phase;
    }
  }

  function startSource(sid) {
    const s = sources.get(sid);
    s.boot = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    s.started = now(); s.values = 0; s.batches = 0; s.devState = {}; s.lastOk = s.lastOk || {}; s.rates = {}; s.lastPoll = 0;
    s.connectAt = now() + 1200;                                              // the connector takes a moment to connect
    if (!s.config.enabled) { setStatus(sid, "waiting_config", [], null); return; }
    setStatus(sid, "connecting", devicesOf(s.config).map((d) => ({id: d.id, state: "connecting", last_ok: null})), null);
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
      const protocols = t.protocols.map((p) => ({...p, status: ["modbus", "udp", "snmp"].includes(p.connector) ? "available" : SPECIFIED.has(p.connector) ? "specified" : "planned"}));
      return {...t, protocols, available: protocols.filter((p) => p.status === "available").length};
    });
    return {types, available: ["modbus", "snmp", "udp"], templates: window.__DEMO_TEMPLATES__ || {}, secrets: true, demo: {site: "ps-west", devices: [
      {name: "Pump Station West", device_id: "pump-plc", ...PUMP, unit_id: PUMP.unit, tags: PUMP_TAGS},
      {type: "snmp", name: "Server room switch and UPS", community: "public", v3: SNMP_V3, trap_port: TRAP_PORT,
        devices: [{id: "server-room-sw", host: "127.0.0.1", port: 161, name: "Server room switch"}, {id: "ups-1", host: "127.0.0.1", port: 1161, name: "UPS 10 kVA"}]},
      {type: "udp", name: "GNSS and weather station", bind_port: STATION.port, senders: [{id: "station", host: "127.0.0.1", name: "GNSS + weather station"}]}]}};
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
    if (!["modbus", "udp", "snmp"].includes(doc.type)) return [null, err(422, "CONNECTOR_UNAVAILABLE", `no ${doc.type} connector is installed yet; available: ['modbus', 'snmp', 'udp']`)];
    const errors = doc.type === "modbus" ? validate(doc) : validateOther(doc);
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
    const dev = devicesOf(cfg).find((d) => d.id === address.device);
    if (!dev || !deviceReachable(cfg, dev, now())) return {v: null, q: 24};
    return pointValue(cfg, {tag_id: "adhoc", address: {...address, transform: "none"}, data_type}, now()) || {v: null, q: 4};
  }
  async function testConnection(cfg) {
    if (cfg.type === "udp") {
      const heard = cfg.connection.senders.some((x) => stationHeard(cfg, x)), listen = Math.min(30, Math.max(3, (cfg.acquisition?.request_timeout_ms || 5000) / 1000));
      await sleep(heard ? 1200 : Math.min(listen * 1000, 4000));
      return {devices: cfg.connection.senders.map((x) => stationHeard(cfg, x)
        ? {id: x.id, ok: true, latency_ms: 0, info: {from: STATION.host, datagrams: 3, format: "json, nmea"}}
        : {id: x.id, ok: false, latency_ms: listen * 1000, error: {code: "CONN_TIMEOUT", message: `no datagram from ${x.host} on udp port ${cfg.connection.bind_port} within ${listen} s` +
          (Number(cfg.connection.bind_port) !== STATION.port ? " (in this demo the station sends to port 5514)" : "")}})};
    }
    if (cfg.type === "snmp") {
      await sleep(rnd(150, 400));
      return {devices: cfg.connection.devices.map((d) => {
        const acc = snmpAccess(cfg, d);
        if (acc.error) return {id: d.id, ok: false, latency_ms: cfg.acquisition?.request_timeout_ms || 2000, error: acc.error};
        const m = mib(acc.kind, now());
        return {id: d.id, ok: true, latency_ms: r(rnd(1, 4), 1), info: {sysName: m["1.3.6.1.2.1.1.5.0"][1], sysDescr: m["1.3.6.1.2.1.1.1.0"][1], uptime_s: Math.floor(m["1.3.6.1.2.1.1.3.0"][1] / 100)}};
      })};
    }
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


  // ------------------------------------------------------------------ user accounts (as daq_platform/accounts.py)
  const ROLE_RANK = {viewer: 1, operator: 2, engineer: 3, admin: 4};
  const ROLE_TEXT = {viewer: "Sees live values, history and events", operator: "Same as viewer today; will acknowledge alarms when alarms are added",
    engineer: "Operator + adds and changes data sources", admin: "Engineer + deletes sources and manages users"};
  const DEMO_LOGIN = {username: "admin", password: "Opcare@2026"};
  const users = new Map();
  const sessions = new Map();         // token -> username
  function addUser(username, name, role, password, mustChange, email = null) {
    users.set(username, {username, name, email, role, password, active: true, must_change_password: mustChange,
      created: iso(now()), updated: iso(now()), last_login: null});
  }
  addUser("admin", "Khaled (administrator)", "admin", DEMO_LOGIN.password, false);
  addUser("ahmed.hassan", "Ahmed Hassan", "engineer", "Engineer@2026", false, "ahmed@example.com");
  addUser("control.room", "Control Room", "viewer", "Viewer@2026", false);
  const publicUser = (u) => { const {password, ...rest} = u; return rest; };
  function passwordProblem(pw, username) {
    if (typeof pw !== "string" || pw.length < 10) return "the password needs at least 10 characters";
    if (!/[A-Za-z]/.test(pw) || !/\d/.test(pw)) return "the password needs letters and at least one digit";
    if (username && pw.toLowerCase().includes(username.toLowerCase())) return "the password must not contain the user name";
    return null;
  }
  const NO_LOGIN = new Set(["/api/health", "/api/login"]);
  const PW_ALLOWED = new Set(["/api/me", "/api/me/password", "/api/logout"]);
  function whoIs(token, path) {
    const u = users.get(sessions.get(token));
    if (!u || !u.active) return {error: err(401, "UNAUTHORIZED", "please sign in")};
    if (u.must_change_password && !PW_ALLOWED.has(path)) return {error: err(403, "PASSWORD_CHANGE_REQUIRED", "change your password first")};
    return {u};
  }
  function needs(path, method) {
    if (path.startsWith("/api/users") || (path.startsWith("/api/sources/") && method === "DELETE")) return "admin";
    if (path.startsWith("/api/secrets") || path === "/api/sources-check" || path === "/api/sources-test" || path === "/api/sources-browse" ||
      (path.startsWith("/api/sources") && method !== "GET" && !/\/admin\/(ping|get_diagnostics)$/.test(path))) return "engineer";
    if (path === "/api/audit") return "admin";
    return "viewer";
  }
  async function accountsApi(method, p, body, who, token) {
    let m;
    if (p === "/api/login") {
      const name = String(body?.username || "").trim().toLowerCase(), u = users.get(name);
      await sleep(250);
      if (!u || !u.active || u.password !== body?.password) {
        audit.unshift({ts: iso(now()), user: name || "?", action: "login_failed", target: name || "?", detail: ""});
        return err(401, "LOGIN_FAILED", "user name or password is not correct");
      }
      const tok = crypto.randomUUID ? crypto.randomUUID() + crypto.randomUUID() : String(Math.random()).slice(2) + Date.now();
      sessions.set(tok, name); u.last_login = iso(now());
      audit.unshift({ts: iso(now()), user: name, action: "login", target: name, detail: ""});
      return json({token: tok, expires_ms: now() + 12 * 3600e3, user: name, name: u.name, role: u.role, must_change_password: u.must_change_password, kind: "session"});
    }
    if (p === "/api/logout") { sessions.delete(token); audit.unshift({ts: iso(now()), user: who.username, action: "logout", target: who.username, detail: ""}); return json(null, 204); }
    if (p === "/api/me") return json({user: who.username, name: who.name, role: who.role, must_change_password: who.must_change_password, kind: "session"});
    if (p === "/api/me/password") {
      if (body?.current !== who.password) return err(400, "BAD_PASSWORD", "the current password is not correct");
      const prob = passwordProblem(body?.new, who.username);
      if (prob) return err(400, "WEAK_PASSWORD", prob);
      if (body.new === who.password) return err(400, "WEAK_PASSWORD", "choose a password different from the current one");
      who.password = body.new; who.must_change_password = false;
      audit.unshift({ts: iso(now()), user: who.username, action: "change_password", target: who.username, detail: ""});
      return json(null, 204);
    }
    if (p === "/api/users" && method === "GET") return json({users: [...users.values()].sort((a, b) => a.username.localeCompare(b.username)).map(publicUser), roles: ROLE_TEXT});
    if (p === "/api/users" && method === "POST") {
      const name = String(body?.username || "").trim().toLowerCase();
      if (!/^[a-z0-9][a-z0-9._-]{1,31}$/.test(name)) return err(400, "BAD_REQUEST", "user name: 2-32 lower case letters, digits, dot, dash or underscore");
      if (!ROLE_RANK[body.role]) return err(400, "BAD_REQUEST", "role must be one of viewer, operator, engineer, admin");
      const prob = passwordProblem(body.password, name);
      if (prob) return err(400, "WEAK_PASSWORD", prob);
      if (users.has(name)) return err(409, "EXISTS", `user ${name} already exists`);
      addUser(name, String(body.name || "").trim() || name, body.role, body.password, body.must_change_password !== false, body.email || null);
      audit.unshift({ts: iso(now()), user: who.username, action: "create_user", target: name, detail: `role ${body.role}`});
      return json(publicUser(users.get(name)), 201);
    }
    if ((m = p.match(/^\/api\/users\/([^/]+)(\/password)?$/))) {
      const name = decodeURIComponent(m[1]), u = users.get(name);
      if (!u) return err(404, "NOT_FOUND", `user ${name} not found`);
      const admins = [...users.values()].filter((x) => x.role === "admin" && x.active).length;
      const endSessions = () => [...sessions].forEach(([t, n]) => { if (n === name && t !== token) sessions.delete(t); });
      if (m[2]) {
        const prob = passwordProblem(body?.password, name);
        if (prob) return err(400, "WEAK_PASSWORD", prob);
        u.password = body.password; u.must_change_password = name !== who.username; endSessions();
        audit.unshift({ts: iso(now()), user: who.username, action: "reset_password", target: name, detail: ""});
        return json(null, 204);
      }
      if (method === "DELETE") {
        if (name === who.username) return err(400, "SELF", "you cannot delete your own account");
        if (u.role === "admin" && u.active && admins <= 1) return err(400, "LAST_ADMIN", "the platform must keep at least one active admin");
        users.delete(name); endSessions();
        audit.unshift({ts: iso(now()), user: who.username, action: "delete_user", target: name, detail: ""});
        return json(null, 204);
      }
      const role = body?.role ?? u.role, active = body?.active ?? u.active;
      if (!ROLE_RANK[role]) return err(400, "BAD_REQUEST", "role must be one of viewer, operator, engineer, admin");
      if (name === who.username && (role !== "admin" || !active)) return err(400, "SELF", "you cannot remove your own admin role or disable yourself");
      if (u.role === "admin" && u.active && (role !== "admin" || !active) && admins <= 1) return err(400, "LAST_ADMIN", "the platform must keep at least one active admin");
      const changes = [role !== u.role ? `role ${role}` : null, active !== u.active ? `active ${active}` : null].filter(Boolean).join(", ") || "details";
      Object.assign(u, {role, active: Boolean(active), name: String(body?.name ?? u.name).trim() || name, email: body?.email ?? u.email, updated: iso(now())});
      if (!u.active) endSessions();
      audit.unshift({ts: iso(now()), user: who.username, action: "update_user", target: name, detail: changes});
      return json(publicUser(u));
    }
    return null;
  }

  async function handle(method, url, body, token) {
    const p = url.pathname.replace(/^.*\/api\//, "/api/");
    let m;
    if (p === "/api/health") return json({status: "ok", time: iso(now()), db: "browser (demo)", login_hint: DEMO_LOGIN});
    let who = null;
    if (!NO_LOGIN.has(p)) {
      const w = whoIs(token || url.searchParams.get("token"), p);
      if (w.error) return w.error;
      who = w.u;
      const need = needs(p, method);
      if (ROLE_RANK[who.role] < ROLE_RANK[need]) return err(403, "FORBIDDEN", `role ${who.role} cannot do this (needs ${need})`);
    }
    const acc = await accountsApi(method, p, body, who, token);
    if (acc) return acc;
    if (p === "/api/catalog") return json(catalog());
    if (p === "/api/sources" && method === "GET") return json(listSources());
    if (p === "/api/sources" && method === "POST") { const [doc, e] = prepare(body, true); if (e) return e; save(doc, "khaled", true); return json(doc, 201); }
    if (p === "/api/sources-check") {
      const create = url.searchParams.get("mode") !== "update";
      const [doc, e] = prepare(body, create, url.searchParams.get("source_id")); return e || json({ok: true, config: doc});
    }
    if (p === "/api/secrets" && method === "GET") return json({names: [...secrets.keys()].sort(), managed_here: true});
    if ((m = p.match(/^\/api\/secrets\/([^/]+)$/)) && method === "PUT") {
      const name = decodeURIComponent(m[1]);
      if (!/^[A-Za-z0-9._-]{1,128}$/.test(name)) return err(400, "BAD_REQUEST", "secret name: 1-128 letters, digits, dot, dash or underscore");
      if (typeof body?.value !== "string" || !body.value) return err(400, "BAD_REQUEST", "value must be text");
      secrets.set(name, body.value);
      audit.unshift({ts: iso(now()), user: "khaled", action: "store_secret", target: name, detail: ""});
      return json(null, 204);
    }
    if (p === "/api/sources-browse") {
      const create = url.searchParams.get("mode") !== "update";
      const [doc, e] = prepare(body?.config, create, url.searchParams.get("source_id"));
      const res = {spec: "1.0", request_id: ulid(), src: "edge-agent", done: true, ts: iso(now())};
      if (e) { const b = await e.json(); return json({...res, ok: false, error: {code: b.error.code, message: [].concat(b.error.details || b.error.message).join("; ")}}, 502); }
      return browseResponse(res, doc, body);
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
      else if (op === "browse" && s.config.type !== "modbus") return browseResponse({...res, src: s.config.source_id}, s.config, body || {});
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

  async function browseResponse(res, doc, args) {
    if (doc.type === "udp") {
      await sleep(Math.min(8, args.listen_s || 10) * 250);
      return json({...res, ok: true, result: listen(doc, args.depth || 1)});
    }
    if (doc.type === "snmp") {
      await sleep(rnd(200, 500));
      const w = walk(doc, args.path, Math.min(5000, args.max_items || 500));
      return w.error ? json({...res, ok: false, error: w.error}, 502) : json({...res, ok: true, result: w.result});
    }
    return json({...res, ok: false, error: {code: "OP_UNSUPPORTED", message: "browse is not supported by the modbus connector"}}, 502);
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, opts = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.includes("/api/")) return realFetch(input, opts);
    let body;
    try { body = opts.body ? JSON.parse(opts.body) : undefined; } catch (e) { return err(400, "BAD_REQUEST", "body is not valid JSON"); }
    await sleep(rnd(15, 60));                                                     // a little network-like delay
    const auth = new Headers(opts.headers || {}).get("Authorization") || "";
    return handle((opts.method || "GET").toUpperCase(), url, body, auth.replace(/^Bearer\s+/i, ""));
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
