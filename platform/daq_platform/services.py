"""Central services: ingest (data, events, status), configuration service, admin relay and live hub.

Message flow (Architecture OSS-DAQ-ARC-001 section 9):
  connectors --MQTT--> opcare/v1/+/data|event|status/+  --> Platform._consume --> Store + LiveHub
  API --> Platform.save_source --> Store + retained opcare/v1/{site}/config/{source}  --> connector
  API --> Platform.admin --> opcare/v1/{site}/admin/{source}/req ... /res --> API response
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import uuid
from typing import Any, Callable

from .store import Store, ms_to_iso, now_ms

log = logging.getLogger("daq.platform")
SPEC = "1.0"
ADMIN_OPS = {"ping", "get_diagnostics", "test_connection", "browse", "read_now", "reload_config"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


class LiveHub:
    """Fan-out of live updates to Server-Sent-Events clients. A client that falls behind is dropped."""

    def __init__(self, max_queue: int = 2000):
        self.clients: set[asyncio.Queue] = set()
        self.max_queue = max_queue
        self.dropped_clients = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self.max_queue)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def publish(self, msg: dict) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.clients.discard(q)      # the SSE loop sees it is no longer subscribed and closes
                self.dropped_clients += 1


class Platform:
    def __init__(self, store: Store, bus, validator: Callable[[str, dict], list[str]],
                 available_types: set[str] | None = None, secrets_dir: str | pathlib.Path | None = None):
        """available_types: connector types that can run (installed); None = do not restrict.
        secrets_dir: where device passwords (*_ref files, spec section 3) are kept for the edge agent on this
        node. None = secrets are managed on the edge nodes themselves (the API then refuses to store them)."""
        self.secrets_dir = pathlib.Path(secrets_dir) if secrets_dir else None
        self.store, self.bus, self.validate = store, bus, validator
        self.available_types = available_types
        self.hub = LiveHub()
        self.stats = {"batches": 0, "values": 0, "events": 0, "status": 0, "missing_batches": 0, "rejected": 0}
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[int] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        for pattern in ("opcare/v1/+/data/+", "opcare/v1/+/event/+", "opcare/v1/+/status/+",
                        "opcare/v1/+/admin/+/res"):
            self._subs.append(self.bus.subscribe(pattern, self._on_message))
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        for sid in self._subs:
            self.bus.unsubscribe(sid)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _on_message(self, topic: str, payload: bytes) -> None:      # any thread
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (topic, payload))

    # ------------------------------------------------------------ ingest
    async def _consume(self) -> None:
        while True:
            items = [await self._queue.get()]
            while not self._queue.empty() and len(items) < 200:      # group work for fewer DB round trips
                items.append(self._queue.get_nowait())
            values: list[dict] = []
            for topic, payload in items:
                try:
                    values += await self._handle(topic, payload)
                except Exception as exc:                              # never let one bad message stop ingest
                    self.stats["rejected"] += 1
                    log.warning("rejected message on %s: %s", topic, exc)
            if values:
                await self.store.run(self.store.write_values, values)
                self.stats["values"] += len(values)
                self.hub.publish({"type": "values", "values": [
                    {k: v[k] for k in ("t", "v", "q", "ts") if k in v} for v in values]})

    async def _handle(self, topic: str, payload: bytes) -> list[dict]:
        parts = topic.split("/")
        kind, src = parts[3], parts[4]
        if kind == "admin":
            msg = json.loads(payload)
            fut = self._pending.pop(msg.get("request_id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return []
        if not payload:
            return []
        msg = json.loads(payload)
        if msg.get("src") != src:
            raise ValueError("src does not match topic")
        if kind == "data":
            if not isinstance(msg.get("values"), list) or not msg.get("boot") or not isinstance(msg.get("seq"), int):
                raise ValueError("malformed value batch")
            missing = await self.store.run(self.store.check_seq, src, msg["boot"], msg["seq"])
            if missing:
                self.stats["missing_batches"] += missing
                log.warning("%s: %d batch(es) missing before seq %d", src, missing, msg["seq"])
            self.stats["batches"] += 1
            return [v for v in msg["values"] if {"t", "v", "q", "ts", "rx"} <= v.keys()]
        if kind == "status":
            await self.store.run(self.store.save_status, msg)
            self.stats["status"] += 1
            self.hub.publish({"type": "status", "source_id": src, "state": msg["state"], "status": msg})
            return []
        if kind == "event":
            await self.store.run(self.store.save_event, msg)
            self.stats["events"] += 1
            self.hub.publish({"type": "event", "event": msg})
            return []
        return []

    # ------------------------------------------------------------ configuration service
    async def check_source(self, body: dict, create: bool, source_id: str | None = None) -> dict:
        """Validate a configuration exactly as saving would, without saving it. Returns the full document."""
        doc, _ = await self._prepare(body, create, source_id)
        return doc

    async def _prepare(self, body: dict, create: bool, source_id: str | None) -> tuple[dict, dict | None]:
        if not isinstance(body, dict):
            raise ApiError(400, "BAD_REQUEST", "body must be a JSON object")
        sid = source_id or body.get("source_id")
        if source_id and body.get("source_id", source_id) != source_id:
            raise ApiError(400, "BAD_REQUEST", "source_id in body does not match the URL")
        existing = await self.store.run(self.store.get_source, sid) if sid else None
        if create and existing:
            raise ApiError(409, "EXISTS", f"source {sid} already exists")
        if not create and not existing:
            raise ApiError(404, "NOT_FOUND", f"source {sid} not found")
        doc = {k: v for k, v in body.items() if k not in ("spec", "revision")}
        doc = {"spec": SPEC, "revision": (existing["revision"] + 1) if existing else 1, "source_id": sid, **doc}
        doc.setdefault("enabled", True)
        errors = self.validate(f"protocols/{doc.get('type')}.config.schema.json", doc) \
            if doc.get("type") else ["type: required"]
        if errors:
            raise ApiError(422, "CFG_INVALID", "configuration is not valid", errors)
        if self.available_types is not None and doc["type"] not in self.available_types:
            raise ApiError(422, "CONNECTOR_UNAVAILABLE",
                           f"no {doc['type']} connector is installed yet; available: {sorted(self.available_types)}")
        if self.secrets_dir is not None:
            missing = [f"{path}: secret '{ref}' is not stored yet — enter the password again"
                       for path, ref in _secret_refs(doc) if not (self.secrets_dir / ref).is_file()]
            if missing:
                raise ApiError(422, "CFG_INVALID", "configuration is not valid", missing)
        owners = {t["tag_id"]: t["source_id"] for t in await self.store.run(self.store.list_tags)}
        taken = [p["tag_id"] for p in doc["points"] if owners.get(p["tag_id"], sid) != sid]
        if taken:
            raise ApiError(409, "TAG_IN_USE", "tag ids already belong to another source", taken[:50])
        return doc, existing

    async def save_source(self, body: dict, user: str, create: bool, source_id: str | None = None) -> dict:
        doc, _ = await self._prepare(body, create, source_id)
        sid = doc["source_id"]
        await self.store.run(self.store.save_source, doc, user)
        self.bus.publish(f"opcare/v1/{doc['site']}/config/{sid}", json.dumps(doc), retain=True)
        await self.store.run(self.store.audit, user, "create_source" if create else "update_source", sid,
                             f"revision {doc['revision']}")
        return doc

    async def delete_source(self, source_id: str, user: str) -> None:
        cfg = await self.store.run(self.store.get_source, source_id)
        if not cfg:
            raise ApiError(404, "NOT_FOUND", f"source {source_id} not found")
        await self.store.run(self.store.delete_source, source_id)
        self.bus.publish(f"opcare/v1/{cfg['site']}/config/{source_id}", b"", retain=True)  # spec 5.2: deleted
        await self.store.run(self.store.audit, user, "delete_source", source_id, "")

    # ------------------------------------------------------------ admin relay
    async def admin(self, source_id: str, op: str, args: dict, user: str, timeout_ms: int = 10000) -> dict:
        if op not in ADMIN_OPS:
            raise ApiError(400, "BAD_REQUEST", f"unknown operation {op}")
        cfg = await self.store.run(self.store.get_source, source_id)
        if not cfg:
            raise ApiError(404, "NOT_FOUND", f"source {source_id} not found")
        return await self._relay(cfg["site"], source_id, op, args, user, timeout_ms)

    async def test_unsaved(self, body: dict, user: str, create: bool, source_id: str | None = None,
                           timeout_ms: int = 15000) -> dict:
        """'Test connection' before saving: the site's edge agent opens a short-lived connection."""
        doc = await self.check_source(body, create, source_id)
        return await self._relay(doc["site"], "edge-agent", "test_connection", {"config": doc}, user, timeout_ms,
                                 who="the edge agent")

    async def browse_unsaved(self, body: dict, user: str, create: bool, source_id: str | None = None,
                             timeout_ms: int = 20000) -> dict:
        """'Browse' / 'Listen' before saving (UDP senders, SNMP walk): the site's edge agent does it."""
        if not isinstance(body, dict) or not isinstance(body.get("config"), dict):
            raise ApiError(400, "BAD_REQUEST", "body must be {config, path?, listen_s?, max_items?, depth?}")
        doc = await self.check_source(body["config"], create, source_id)
        args = {k: body[k] for k in ("path", "listen_s", "max_items", "depth") if k in body}
        return await self._relay(doc["site"], "edge-agent", "browse", {**args, "config": doc}, user, timeout_ms,
                                 who="the edge agent")

    # ------------------------------------------------------------ secrets (device passwords, communities)
    def store_secret(self, name: str, value: str) -> None:
        if self.secrets_dir is None:
            raise ApiError(501, "NOT_SUPPORTED", "secrets are managed on the edge nodes of this installation")
        if not SECRET_NAME.fullmatch(name or "") or name in RESERVED_SECRETS:
            raise ApiError(400, "BAD_REQUEST", "secret name: 1-128 letters, digits, dot, dash or underscore")
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ApiError(400, "BAD_REQUEST", "value must be text of 1 to 4096 characters")
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.secrets_dir / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.replace(tmp, self.secrets_dir / name)

    def opcua_certificate(self, name: str, regenerate: bool = False, create: bool = True) -> dict:
        """Client application certificate for OPC UA sources: '<name>.cert.pem' and '<name>.key.pem' secrets.
        Returns the public part only (the key stays in the secrets folder)."""
        if self.secrets_dir is None:
            raise ApiError(501, "NOT_SUPPORTED", "secrets are managed on the edge nodes of this installation")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""):
            raise ApiError(400, "BAD_REQUEST", "certificate name: 1-64 letters, digits, dash or underscore")
        from .certs import make_opcua_certificate, summary
        cert_ref, key_ref = f"{name}.cert.pem", f"{name}.key.pem"
        cert_file = self.secrets_dir / cert_ref
        created = False
        exists = cert_file.is_file() and (self.secrets_dir / key_ref).is_file()
        if not create and not exists:
            return {"cert_ref": cert_ref, "key_ref": key_ref, "exists": False}
        if regenerate or not (cert_file.is_file() and (self.secrets_dir / key_ref).is_file()):
            cert_pem, key_pem, _ = make_opcua_certificate(name)
            self.store_secret(key_ref, key_pem)
            self.store_secret(cert_ref, cert_pem)
            created = True
        return {"cert_ref": cert_ref, "key_ref": key_ref, "exists": True, "created": created, **summary(cert_file.read_text())}

    def secret_names(self) -> list[str]:
        if self.secrets_dir is None or not self.secrets_dir.is_dir():
            return []
        return sorted(p.name for p in self.secrets_dir.iterdir()
                      if p.is_file() and not p.name.startswith(".") and p.name not in RESERVED_SECRETS)

    async def _relay(self, site: str, component: str, op: str, args: dict, user: str, timeout_ms: int,
                     who: str | None = None) -> dict:
        rid = str(uuid.uuid4())
        fut = self._loop.create_future()
        self._pending[rid] = fut
        req = {"spec": SPEC, "request_id": rid, "op": op, "args": args or {}, "timeout_ms": timeout_ms,
               "requested_by": user, "ts": _iso_now()}
        self.bus.publish(f"opcare/v1/{site}/admin/{component}/req", json.dumps(req))
        try:
            return await asyncio.wait_for(fut, timeout_ms / 1000 + 1)
        except asyncio.TimeoutError as exc:
            raise ApiError(504, "TIMEOUT", f"{who or 'connector ' + component} did not answer (offline?)") from exc
        finally:
            self._pending.pop(rid, None)


SECRET_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
RESERVED_SECRETS = {"mqtt_password"}


def _secret_refs(obj, path: str = "") -> list[tuple[str, str]]:
    """(json path, secret name) of every *_ref field."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}" if path else k
            if k.endswith("_ref") and isinstance(v, str):
                out.append((p, v))
            else:
                out += _secret_refs(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _secret_refs(v, f"{path}/{i}")
    return out


def _iso_now() -> str:
    return ms_to_iso(now_ms())
