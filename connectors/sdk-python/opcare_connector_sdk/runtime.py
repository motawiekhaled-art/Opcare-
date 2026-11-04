"""Generic connector runtime: environment, MQTT, lifecycle, configuration, status, admin, shutdown.

Implements spec OSS-DAQ-CON-001 sections 3, 4, 5, 6.1, 7, 9, 10, 11 and 14 so that a protocol
developer only writes a Connector subclass (connector.py).

    import asyncio
    from opcare_connector_sdk import Runtime
    asyncio.run(Runtime(MyConnector()).main())
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import sys
import time
import uuid
from typing import Any, Callable

from .connector import Connector
from .core import (SPEC, Batcher, ConnectorError, PointTable, Q, ValuePipeline, now_iso, ulid)

HEARTBEAT_S = 10
MAX_PAYLOAD = 240 * 1024
REQUIRED_ENV = ("OPCARE_SITE", "OPCARE_EDGE_ID", "OPCARE_SOURCE_ID", "OPCARE_MQTT_URL",
                "OPCARE_MQTT_USERNAME", "OPCARE_MQTT_PASSWORD_FILE")


# ------------------------------------------------------------------ logging (section 14)
class JsonLog:
    LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

    def __init__(self, src: str, level: str = "info", stream=sys.stdout):
        self.src, self.level, self.stream, self.boot = src, self.LEVELS.get(level, 20), stream, ""

    def __call__(self, level: str, msg: str, **fields: Any) -> None:
        if self.LEVELS[level] < self.level:
            return
        rec = {"ts": now_iso(), "level": level, "src": self.src, "boot": self.boot, "msg": msg, **fields}
        self.stream.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        self.stream.flush()


# ------------------------------------------------------------------ environment (section 3)
class Env:
    def __init__(self, environ: dict | None = None):
        e = dict(os.environ if environ is None else environ)
        missing = [k for k in REQUIRED_ENV if not e.get(k)]
        if missing:
            sys.stderr.write(f"missing required environment variables: {', '.join(missing)}\n")
            raise SystemExit(2)
        self.site, self.edge_id, self.source_id = e["OPCARE_SITE"], e["OPCARE_EDGE_ID"], e["OPCARE_SOURCE_ID"]
        self.mqtt_url, self.mqtt_username = e["OPCARE_MQTT_URL"], e["OPCARE_MQTT_USERNAME"]
        self.mqtt_password_file = e["OPCARE_MQTT_PASSWORD_FILE"]
        self.secrets_dir = pathlib.Path(e.get("OPCARE_SECRETS_DIR", "/run/secrets"))
        self.data_dir = pathlib.Path(e.get("OPCARE_DATA_DIR", "/data"))
        self.log_level = e.get("OPCARE_LOG_LEVEL", "info")
        self.schema_dir = e.get("OPCARE_SCHEMA_DIR", "/opcare/schemas")

    def topic(self, kind: str) -> str:
        base = f"opcare/v1/{self.site}"
        if kind in ("admin_req", "admin_res"):
            return f"{base}/admin/{self.source_id}/{kind[-3:]}"
        return f"{base}/{kind}/{self.source_id}"

    def secret(self, name: str) -> str:
        """Read a *_ref secret file. Raises ConnectorError SECRET_MISSING."""
        try:
            return (self.secrets_dir / name).read_text().strip()
        except OSError as exc:
            raise ConnectorError("SECRET_MISSING", f"secret '{name}' not readable") from exc


def secret_refs(obj: Any) -> list[str]:
    """All values of keys ending in _ref, anywhere in the document."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_ref") and isinstance(v, str):
                out.append(v)
            else:
                out += secret_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            out += secret_refs(v)
    return out


# ------------------------------------------------------------------ schema validation (section 6.1)
class SchemaValidator:
    BASE = "https://schemas.opcaresystems.com/daq/connector/1.0/"

    def __init__(self, schema_dir: str | pathlib.Path):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        docs = {}
        for p in pathlib.Path(schema_dir).rglob("*.schema.json"):
            d = json.loads(p.read_text())
            docs[d["$id"]] = d
        if not docs:
            raise RuntimeError(f"no schemas found in {schema_dir}")
        self._reg = Registry().with_resources((k, Resource.from_contents(v)) for k, v in docs.items())
        self._docs, self._cls = docs, Draft202012Validator

    def __call__(self, schema_rel: str, doc: dict) -> list[str]:
        schema = self._docs.get(self.BASE + schema_rel)
        if schema is None:
            return [f"no schema {schema_rel}"]
        v = self._cls(schema, registry=self._reg)
        return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in v.iter_errors(doc)][:20]


# ------------------------------------------------------------------ MQTT client adapter (section 4)
class PahoClient:
    """Thin adapter over paho-mqtt 2.x. Tests inject a fake with the same five methods."""

    def __init__(self, env: Env):
        import paho.mqtt.client as mqtt
        from urllib.parse import urlparse
        self._mqtt = mqtt
        self.url = urlparse(env.mqtt_url)
        self.c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"{env.site}.{env.source_id}", protocol=mqtt.MQTTv5)
        with open(env.mqtt_password_file) as fh:
            self.c.username_pw_set(env.mqtt_username, fh.read().strip())
        if self.url.scheme == "mqtts":
            self.c.tls_set()
        self.subs: list[tuple[str, int]] = []
        self.on_message: Callable[[str, bytes], None] = lambda t, p: None
        self.c.on_message = lambda c, u, m: self.on_message(m.topic, m.payload)
        self.c.on_connect = self._resubscribe

    def _resubscribe(self, client, userdata, flags, reason_code, properties):
        for topic, qos in self.subs:
            client.subscribe(topic, qos)

    def set_will(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.c.will_set(topic, payload, qos=qos, retain=retain)

    def connect(self) -> None:
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties
        props = Properties(PacketTypes.CONNECT)
        props.SessionExpiryInterval = 3600
        port = self.url.port or (8883 if self.url.scheme == "mqtts" else 1883)
        self.c.connect(self.url.hostname, port, keepalive=30, clean_start=False, properties=props)
        self.c.loop_start()

    def subscribe(self, topic: str, qos: int) -> None:
        self.subs.append((topic, qos))
        self.c.subscribe(topic, qos)

    def unsubscribe(self, topic: str) -> None:
        self.c.unsubscribe(topic)

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.c.publish(topic, payload, qos=qos, retain=retain)

    def disconnect(self) -> None:
        self.c.disconnect()   # normal DISCONNECT: the broker does not send the Last Will
        self.c.loop_stop()


# ------------------------------------------------------------------ runtime
class Runtime:
    def __init__(self, connector: Connector, env: Env | None = None, client: Any = None,
                 validator: Callable[[str, dict], list[str]] | None = None, log: JsonLog | None = None):
        self.c = connector
        self.env = env or Env()
        self.log = log or JsonLog(self.env.source_id, self.env.log_level)
        self.boot = str(uuid.uuid4())
        self.log.boot = self.boot
        self.client = client or PahoClient(self.env)
        self.validate_schema = validator or SchemaValidator(self.env.schema_dir)
        self.state, self.since, self.started = "starting", now_iso(), time.monotonic()
        self.config: dict | None = None
        self.revision: int | None = None
        self.last_error: dict | None = None
        self.devices: dict[str, dict] = {}
        self.table: PointTable | None = None
        self.pipeline: ValuePipeline | None = None
        self.batcher = Batcher(self.env.source_id, self.boot, self._publish_batch)
        self.overruns = 0
        self._overrun_times: list[float] = []
        self._last_overrun_event = 0.0
        self._admin_slots = asyncio.Semaphore(2)
        self._pages: dict[str, tuple[float, list, str | None]] = {}
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.running = False

    # ---------------------------------------------------------- publishing helpers
    def _pub(self, kind: str, msg: dict | str, retain: bool = False) -> None:
        payload = msg if isinstance(msg, str) else json.dumps(msg, separators=(",", ":"))
        self.client.publish(self.env.topic(kind), payload, 1, retain)

    def _publish_batch(self, msg: dict) -> None:
        self._pub("data", msg)

    def event(self, type_: str, severity: str, message: str, **fields: Any) -> None:
        msg = {"spec": SPEC, "id": ulid(), "src": self.env.source_id, "type": type_, "severity": severity,
               "ts": now_iso(), "message": message[:1000]}
        msg.update({k: v for k, v in fields.items() if v is not None})
        self._pub("event", msg)

    def status(self) -> dict:
        b = self.batcher
        elapsed = max(time.monotonic() - self.started, 1e-6)
        return {
            "spec": SPEC, "src": self.env.source_id, "kind": "connector", "type": self.c.type,
            "version": self.c.version, "boot": self.boot, "state": self.state, "since": self.since,
            "ts": now_iso(), "uptime_s": int(elapsed), "config_revision": self.revision,
            "devices": [{"id": k, **v} for k, v in self.devices.items()],
            "stats": {"points": len(self.table.points) if self.table else 0, "values_total": b.values_total,
                      "values_per_s": round(b.values_total / elapsed, 2), "batches_total": b.batches_total,
                      "dropped_total": 0, "scan_overruns_total": self.overruns},
            "last_error": self.last_error,
        }

    def _publish_status(self) -> None:
        self._pub("status", self.status(), retain=True)

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.log("info", "state change", state=state, previous=self.state)
            self.state, self.since = state, now_iso()
        self._publish_status()

    def _error(self, err: ConnectorError) -> None:
        self.last_error = err.as_dict(with_ts=True)

    def _derive_state(self) -> str:
        states = [d["state"] for d in self.devices.values() if d["state"] != "disabled"]
        recent_overruns = [t for t in self._overrun_times if time.monotonic() - t < 60]
        if not states:
            return "connecting"
        if all(s == "connected" for s in states):
            return "degraded" if len(recent_overruns) >= 3 else "running"
        if any(s == "connected" for s in states):
            return "degraded"
        if all(s == "connecting" for s in states):
            return "connecting"
        return "error"

    # ---------------------------------------------------------- sink given to the connector
    def value(self, tag_id: str, raw: Any, q: int = Q.GOOD, ts: str | None = None, flags=()) -> None:
        if self.pipeline:
            self.pipeline.offer(tag_id, raw, q, ts, flags)

    def device_state(self, device: str, state: str, error: ConnectorError | None = None) -> None:
        prev = self.devices.get(device, {}).get("state")
        entry = self.devices.setdefault(device, {"state": state, "last_ok": None})
        entry["state"] = state
        entry.pop("error", None)
        if state == "connected":
            entry["last_ok"] = now_iso()
            if prev != "connected":
                self.event("conn.connected", "info", f"Device {device} connected", device=device)
        elif error is not None:
            entry["error"] = error.as_dict()
            self._error(error)
        if state == "unreachable" and prev != "unreachable":
            if self.pipeline:
                self.pipeline.device_lost(device)
            sev = "high" if error and error.code in ("AUTH_FAILED", "TLS_FAILED") else "medium"
            typ = "conn.auth_failed" if sev == "high" else "conn.lost"
            self.event(typ, sev, f"Device {device} unreachable", device=device,
                       attrs={"error": error.code} if error else None)
        self.set_state(self._derive_state())

    def device_ok(self, device: str) -> None:
        """Call after each successful exchange to keep last_ok fresh (cheap; no status publish)."""
        if device in self.devices:
            self.devices[device]["last_ok"] = now_iso()

    def scan_overrun(self, scan_ms: int) -> None:
        self.overruns += 1
        now = time.monotonic()
        self._overrun_times = [t for t in self._overrun_times if now - t < 60] + [now]
        if now - self._last_overrun_event >= 60:
            self._last_overrun_event = now
            self.event("scan.overrun", "low", f"Poll cycle longer than {scan_ms} ms", attrs={"scan_ms": scan_ms})

    # ---------------------------------------------------------- configuration (sections 5.2, 6.1)
    def check_config(self, cfg: Any) -> list[str]:
        if not isinstance(cfg, dict):
            return ["configuration is not a JSON object"]
        errs = []
        spec = str(cfg.get("spec", ""))
        if spec.split(".")[0] != SPEC.split(".")[0]:
            raise ConnectorError("CFG_UNSUPPORTED_SPEC", f"spec {spec} not supported")
        errs += self.validate_schema(f"protocols/{self.c.type}.config.schema.json", cfg)
        if errs:
            return errs
        if cfg["site"] != self.env.site:
            errs.append(f"site {cfg['site']} does not match {self.env.site}")
        if cfg["source_id"] != self.env.source_id:
            errs.append(f"source_id {cfg['source_id']} does not match {self.env.source_id}")
        if cfg["type"] != self.c.type:
            errs.append(f"type {cfg['type']} is not {self.c.type}")
        tags = [p["tag_id"] for p in cfg["points"]]
        dupes = sorted({t for t in tags if tags.count(t) > 1})
        if dupes:
            errs.append(f"duplicate tag_id: {', '.join(dupes[:10])}")
        for ref in secret_refs(cfg):
            if not (self.env.secrets_dir / ref).is_file():
                errs.append(f"secret '{ref}' missing")
        return errs

    async def on_config(self, payload: bytes) -> None:
        if not payload:
            self.log("info", "configuration deleted")
            await self._stop_connector()
            self.config, self.revision, self.devices = None, None, {}
            self.set_state("waiting_config")
            return
        rev = None
        try:
            cfg = json.loads(payload)
            rev = cfg.get("revision") if isinstance(cfg, dict) else None
            errs = self.check_config(cfg)
            if not errs:
                errs = await self.c.validate(cfg)
        except ConnectorError as exc:
            errs, code = [exc.message], exc.code
        except ValueError as exc:
            errs, code = [f"invalid JSON: {exc}"], "CFG_INVALID"
        else:
            code = "CFG_INVALID"
        if errs:
            self.last_error = {"code": code, "message": "; ".join(errs)[:1000], "ts": now_iso()}
            self.log("warn", "configuration rejected", code=code, revision=rev, errors=errs[:5])
            self.event("config.rejected", "medium", f"Configuration revision {rev} rejected",
                       attrs={"revision": rev, "errors": errs[:20]})
            self.set_state(self.state if self.running else "error")
            return
        if self.revision is not None and cfg["revision"] <= self.revision:
            self.log("debug", "configuration revision not newer; ignored", revision=cfg["revision"])
            return
        await self._apply(cfg)

    async def _apply(self, cfg: dict) -> None:
        old = self.config
        self.log("info", "applying configuration", revision=cfg["revision"])
        if not cfg["enabled"]:
            await self._stop_connector()
            self.config, self.revision, self.devices = cfg, cfg["revision"], {}
            self.event("config.applied", "info", f"Configuration revision {cfg['revision']} applied (disabled)",
                       attrs={"revision": cfg["revision"]})
            self.set_state("waiting_config")
            return
        acq = cfg.get("acquisition") or {}
        self.batcher.max_values = acq.get("batch_max_values", 500)
        self.batcher.max_ms = acq.get("batch_max_ms", 1000)
        table = PointTable(cfg, self.c.device_of)
        same_conn = bool(old and self.running and old.get("connection") == cfg["connection"]
                         and old.get("acquisition") == cfg.get("acquisition"))
        hot = False
        if same_conn:
            self.table = table
            self.pipeline = self._new_pipeline(table, acq, keep=True)
            hot = await self.c.apply_points(cfg, table)
        if not hot:
            await self._stop_connector()
            self.table = table
            self.pipeline = self._new_pipeline(table, acq, keep=False)
            self.devices = {d: {"state": "connecting", "last_ok": None} for d in self.c.devices(cfg)}
            self.set_state("connecting")
            try:
                await self.c.start(cfg, table, self)
                self.running = True
            except ConnectorError as exc:
                self._error(exc)
                self.log("error", "start failed", code=exc.code, message=exc.message)
                self.set_state("error")
        self.config, self.revision = cfg, cfg["revision"]
        self.event("config.applied", "info", f"Configuration revision {cfg['revision']} applied",
                   attrs={"revision": cfg["revision"], "hot_reload": hot})
        self.set_state(self._derive_state() if self.running else self.state)

    def _new_pipeline(self, table: PointTable, acq: dict, keep: bool) -> ValuePipeline:
        p = ValuePipeline(table, acq.get("integrity_s", 900), self.batcher.add)
        if keep and self.pipeline:
            p.last = {k: v for k, v in self.pipeline.last.items() if k in table.points}
        return p

    async def _stop_connector(self) -> None:
        if self.running:
            self.running = False
            try:
                await asyncio.wait_for(self.c.stop(), 8)
            except Exception as exc:  # noqa: BLE001
                self.log("error", "connector stop failed", error=str(exc))
            self.batcher.flush()

    # ---------------------------------------------------------- admin (section 11)
    async def on_admin(self, payload: bytes) -> None:
        try:
            req = json.loads(payload)
            rid, op = req["request_id"], req["op"]
        except (ValueError, KeyError, TypeError):
            self.log("warn", "malformed admin request ignored")
            return
        self.log("info", "admin request", op=op, request_id=rid, requested_by=req.get("requested_by"))
        if self._admin_slots.locked():
            self._respond(rid, error=ConnectorError("DEVICE_BUSY", "two admin operations already running"))
            return
        cursor = (req.get("args") or {}).get("cursor") if op == "browse" else None
        if cursor:
            page = self._pages.pop(cursor, None)
            if page is None:
                self._respond(rid, error=ConnectorError("CFG_INVALID", "unknown or expired cursor"))
            else:
                self._respond(rid, {"items": page[1]}, done=page[2] is None, cursor=page[2])
            return
        async with self._admin_slots:
            timeout = max(req.get("timeout_ms", 10000) / 1000 - 0.2, 0.1)
            try:
                result = await asyncio.wait_for(self._dispatch(op, req.get("args") or {}), timeout)
            except asyncio.TimeoutError:
                self._respond(rid, error=ConnectorError("TIMEOUT", f"{op} did not finish in time"))
            except ConnectorError as exc:
                self._respond(rid, error=exc)
            except Exception as exc:  # noqa: BLE001
                self.log("error", "admin operation failed", op=op, error=repr(exc))
                self._respond(rid, error=ConnectorError("INTERNAL", f"{op} failed"))
            else:
                self._respond_paged(rid, result)

    async def _dispatch(self, op: str, args: dict) -> dict:
        if op == "ping":
            return {"version": self.c.version, "state": self.state}
        if op == "get_diagnostics":
            return {"runtime": {"state": self.state, "revision": self.revision, "devices": self.devices,
                                "seq": self.batcher.seq, "overruns": self.overruns},
                    "connector": await self.c.diagnostics()}
        if op == "test_connection":
            cfg = args.get("config") or self.config
            if cfg is None:
                raise ConnectorError("CFG_INVALID", "no configuration to test")
            errs = self.check_config(cfg) if "config" in args else []
            if errs:
                raise ConnectorError("CFG_INVALID", "; ".join(errs)[:1000])
            return await self.c.test_connection(cfg)
        if op == "browse":
            return await self.c.browse(args)
        if op == "read_now":
            return await self.c.read_now(args)
        if op == "reload_config":
            topic = self.env.topic("config")
            self.client.unsubscribe(topic)
            self.client.subscribe(topic, 1)
            return {"revision": self.revision}
        raise ConnectorError("OP_UNSUPPORTED", f"operation '{op}' is not supported")

    def _respond(self, rid: str, result: dict | None = None, error: ConnectorError | None = None,
                 done: bool = True, cursor: str | None = None) -> None:
        msg = {"spec": SPEC, "request_id": rid, "src": self.env.source_id, "ok": error is None,
               "ts": now_iso(), "done": done}
        if error is None:
            msg["result"] = result or {}
        else:
            msg["error"] = error.as_dict()
        if cursor:
            msg["cursor"] = cursor
        self._pub("admin_res", msg)

    def _respond_paged(self, rid: str, result: dict) -> None:
        """Results over the payload limit are split by 'items' (rule 11.4); the rest wait for a cursor."""
        if len(json.dumps(result)) <= MAX_PAYLOAD or not isinstance(result.get("items"), list):
            self._respond(rid, result)
            return
        items, pages, cur, size = result["items"], [], [], 0
        for it in items:
            s = len(json.dumps(it)) + 1
            if cur and size + s > MAX_PAYLOAD - 4096:
                pages.append(cur)
                cur, size = [], 0
            cur.append(it)
            size += s
        pages.append(cur)
        now = time.monotonic()
        self._pages = {k: v for k, v in self._pages.items() if now - v[0] < 300}
        cursor = None
        for page in reversed(pages[1:]):          # chain: each stored page knows the next page's cursor
            key = uuid.uuid4().hex
            self._pages[key] = (now, page, cursor)
            cursor = key
        self._respond(rid, {"items": pages[0]}, done=False, cursor=cursor)

    # ---------------------------------------------------------- main loop
    def _on_message(self, topic: str, payload: bytes) -> None:   # called from the MQTT thread
        if self._loop and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (topic, payload))

    def request_stop(self) -> None:
        self._stop.set()

    async def main(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        try:
            self._loop.add_signal_handler(signal.SIGTERM, self.request_stop)
        except (NotImplementedError, RuntimeError):
            pass
        offline = {"spec": SPEC, "src": self.env.source_id, "kind": "connector", "boot": self.boot,
                   "state": "offline"}
        self.client.on_message = self._on_message
        self.client.set_will(self.env.topic("status"), json.dumps(offline, separators=(",", ":")), 1, True)
        self.client.connect()
        self.set_state("starting")
        self.client.subscribe(self.env.topic("config"), 1)
        self.client.subscribe(self.env.topic("admin_req"), 1)
        self.set_state("waiting_config")
        tasks = [asyncio.create_task(self._intake()), asyncio.create_task(self._timers())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await self._stop_connector()
        self.set_state("stopping")
        self.client.disconnect()
        self.log("info", "stopped")
        return 0

    async def _intake(self) -> None:
        cfg_topic, adm_topic = self.env.topic("config"), self.env.topic("admin_req")
        while True:
            topic, payload = await self._queue.get()
            if topic == cfg_topic:
                await self.on_config(payload)
            elif topic == adm_topic:
                asyncio.create_task(self.on_admin(payload))

    async def _timers(self) -> None:
        last_hb = 0.0
        while True:
            await asyncio.sleep(0.2)
            self.batcher.poll()
            if self.pipeline:
                self.pipeline.tick()
            if time.monotonic() - last_hb >= HEARTBEAT_S:
                last_hb = time.monotonic()
                self.set_state(self._derive_state() if self.running else self.state)


def run(connector: Connector) -> None:
    """Entry point for a connector container: python -m my_connector"""
    sys.exit(asyncio.run(Runtime(connector).main()))
