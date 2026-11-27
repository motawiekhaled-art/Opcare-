"""Edge agent (Architecture OSS-DAQ-ARC-001 section 6.1).

Watches the retained configuration topics of its site(s) and keeps one connector running per
configured source; stops it when the configuration is deleted. It also answers `test_connection`
for configurations that are not saved yet (the configuration screen's "Test connection" button),
on the admin topic of component `edge-agent`.

This version runs connectors in-process (development, tests, demo, single-box sites). The production
agent applies the same rules but starts one container per source (`opcare/connector-{type}`) with the
environment of spec section 3; only `_start`/`_stop` differ.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import tempfile
import uuid
from typing import Callable

from opcare_connector_sdk import ConnectorError, Env, JsonLog, Runtime, SchemaValidator, now_iso

log = logging.getLogger("daq.agent")
AGENT_ID = "edge-agent"


class LocalAgent:
    def __init__(self, bus, connectors: dict[str, Callable[[], object]], validator: SchemaValidator,
                 site: str = "+", edge_id: str = "edge-local", log_level: str = "warn",
                 secrets_dir: str | pathlib.Path | None = None):
        """bus: InMemoryBus-like (publish/subscribe/client). connectors: {type: factory}.
        secrets_dir: OPCARE_SECRETS_DIR given to every connector (device passwords); default a private temp dir."""
        self.bus, self.factories, self.validate = bus, connectors, validator
        self.site, self.edge_id, self.log_level = site, edge_id, log_level
        self.secrets = pathlib.Path(secrets_dir) if secrets_dir else pathlib.Path(tempfile.mkdtemp(prefix="opcare-agent-"))
        self.secrets.mkdir(parents=True, exist_ok=True)
        (self.secrets / "mqtt_password").write_text("local")
        self.running: dict[tuple[str, str], tuple[Runtime, asyncio.Task]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._subs: list[int] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop, self._queue = asyncio.get_running_loop(), asyncio.Queue()
        push = lambda t, p: self._loop.call_soon_threadsafe(self._queue.put_nowait, (t, p))  # noqa: E731
        self._task = asyncio.create_task(self._consume())
        self._subs.append(self.bus.subscribe(f"opcare/v1/{self.site}/config/+", push))
        self._subs.append(self.bus.subscribe(f"opcare/v1/{self.site}/admin/{AGENT_ID}/req", push))

    async def stop(self) -> None:
        for sid in self._subs:
            self.bus.unsubscribe(sid)
        for key in list(self.running):
            await self._stop(key)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _consume(self) -> None:
        while True:
            topic, payload = await self._queue.get()
            parts = topic.split("/")
            try:
                if parts[3] == "config":
                    await self._on_config(parts[2], parts[4], payload)
                elif parts[3] == "admin":
                    asyncio.create_task(self._on_admin(parts[2], payload))
            except Exception:
                log.exception("agent failed handling %s", topic)

    # ------------------------------------------------------------ connectors
    async def _on_config(self, site: str, source_id: str, payload: bytes) -> None:
        key = (site, source_id)
        if not payload:                                   # deleted (spec 5.2)
            await self._stop(key)
            return
        if key in self.running:                           # the running connector applies new revisions itself
            return
        try:
            ctype = json.loads(payload).get("type")
        except ValueError:
            return
        if ctype not in self.factories:
            log.warning("no connector available for type %s (source %s)", ctype, source_id)
            return
        self._start(key, ctype)

    def _start(self, key: tuple[str, str], ctype: str) -> None:
        site, source_id = key
        env = Env({"OPCARE_SITE": site, "OPCARE_EDGE_ID": self.edge_id, "OPCARE_SOURCE_ID": source_id,
                   "OPCARE_MQTT_URL": "mqtt://local", "OPCARE_MQTT_USERNAME": f"src-{source_id}",
                   "OPCARE_MQTT_PASSWORD_FILE": str(self.secrets / "mqtt_password"),
                   "OPCARE_SECRETS_DIR": str(self.secrets), "OPCARE_LOG_LEVEL": self.log_level})
        rt = Runtime(self.factories[ctype](), env=env, client=self.bus.client(), validator=self.validate,
                     log=JsonLog(source_id, self.log_level))
        self.running[key] = (rt, asyncio.create_task(rt.main()))
        log.info("started %s connector for %s/%s", ctype, site, source_id)

    async def _stop(self, key: tuple[str, str]) -> None:
        entry = self.running.pop(key, None)
        if entry:
            rt, task = entry
            rt.request_stop()
            await asyncio.gather(task, return_exceptions=True)
            # remove the retained status so a deleted source does not linger as "stopped"
            self.bus.publish(f"opcare/v1/{key[0]}/status/{key[1]}", b"", retain=True)
            log.info("stopped connector for %s/%s", *key)

    # ------------------------------------------------------------ test before save
    async def _on_admin(self, site: str, payload: bytes) -> None:
        try:
            req = json.loads(payload)
            rid, op = req["request_id"], req["op"]
        except (ValueError, KeyError, TypeError):
            return
        res = {"spec": "1.0", "request_id": rid, "src": AGENT_ID, "ts": now_iso(), "done": True}
        try:
            if op == "ping":
                result = {"types": sorted(self.factories), "running": len(self.running)}
            elif op in ("test_connection", "browse"):
                args = req.get("args") or {}
                result = await asyncio.wait_for(self._probe(args.get("config"), op, args),
                                                max(req.get("timeout_ms", 10000) / 1000 - 0.2, 0.5))
            else:
                raise ConnectorError("OP_UNSUPPORTED", f"agent operation '{op}' is not supported")
            res.update(ok=True, result=result)
        except ConnectorError as exc:
            res.update(ok=False, error=exc.as_dict())
        except asyncio.TimeoutError:
            res.update(ok=False, error={"code": "TIMEOUT", "message": "test did not finish in time"})
        except Exception as exc:                          # noqa: BLE001
            log.exception("probe failed")
            res.update(ok=False, error={"code": "INTERNAL", "message": str(exc)[:300]})
        self.bus.publish(f"opcare/v1/{site}/admin/{AGENT_ID}/res", json.dumps(res))

    async def _probe(self, cfg: dict | None, op: str = "test_connection", args: dict | None = None) -> dict:
        if not isinstance(cfg, dict) or cfg.get("type") not in self.factories:
            raise ConnectorError("OP_UNSUPPORTED", f"no connector available for type {cfg and cfg.get('type')}")
        errors = self.validate(f"protocols/{cfg['type']}.config.schema.json", cfg)
        connector = self.factories[cfg["type"]]()
        connector.secrets_dir = self.secrets
        if not errors:
            errors = await connector.validate(cfg)
        if errors:
            raise ConnectorError("CFG_INVALID", "; ".join(errors)[:1000])
        running = self.running.get((cfg["site"], cfg["source_id"]))
        if running and running[0].running and running[0].c.type == cfg["type"]:
            connector = running[0].c        # the saved source runs: reuse it (a UDP port or serial line opens once)
        if op == "browse":
            if not getattr(connector, "capabilities", {}).get("browse"):
                raise ConnectorError("OP_UNSUPPORTED", f"the {cfg['type']} connector cannot browse")
            return await connector.browse({**(args or {}), "config": cfg})
        return await connector.test_connection(cfg)       # opens its own short-lived connection


def probe_request(op: str, args: dict, user: str, timeout_ms: int = 10000) -> dict:
    return {"spec": "1.0", "request_id": str(uuid.uuid4()), "op": op, "args": args, "timeout_ms": timeout_ms,
            "requested_by": user, "ts": now_iso()}
