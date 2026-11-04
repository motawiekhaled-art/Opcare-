"""Test helpers shared by all Python connectors: in-memory MQTT stand-in, schema lookup, harness."""
from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib
import tempfile
from typing import Any

from .runtime import Env, JsonLog, Runtime, SchemaValidator


def find_schemas() -> pathlib.Path:
    """OPCARE_SCHEMA_DIR, else docs/connector-spec/schemas found by walking up from this file."""
    if os.environ.get("OPCARE_SCHEMA_DIR"):
        return pathlib.Path(os.environ["OPCARE_SCHEMA_DIR"])
    for parent in pathlib.Path(__file__).resolve().parents:
        cand = parent / "docs" / "connector-spec" / "schemas"
        if cand.is_dir():
            return cand
    raise RuntimeError("schemas not found; set OPCARE_SCHEMA_DIR")


class FakeMqtt:
    """Implements the five-method client interface used by Runtime; records everything."""

    def __init__(self):
        self.published: list[tuple[str, dict, bool]] = []
        self.subs: list[str] = []
        self.will = None
        self.connected = False
        self.on_message = None

    def set_will(self, topic, payload, qos, retain): self.will = (topic, payload, qos, retain)
    def connect(self): self.connected = True
    def subscribe(self, topic, qos): self.subs.append(topic)
    def unsubscribe(self, topic): pass
    def publish(self, topic, payload, qos, retain): self.published.append((topic, json.loads(payload), retain))
    def disconnect(self): self.connected = False

    def deliver(self, topic: str, payload: Any) -> None:
        self.on_message(topic, payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    def msgs(self, kind: str) -> list[dict]:
        return [m for t, m, _ in self.published if t.split("/")[3] == kind]

    def values(self) -> list[dict]:
        return [v for m in self.msgs("data") for v in m["values"]]

    def latest(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for v in self.values():
            out[v["t"]] = v
        return out


class Harness:
    """Runs a connector inside the real Runtime with FakeMqtt. Use as `async with Harness(conn) as h:`."""

    SCHEMA_OF = {"data": "value-batch", "event": "event", "status": "status", "admin": "admin-response"}

    def __init__(self, connector, site="site-a", source_id="src-test", secrets: dict | None = None):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "mqtt_pw").write_text("broker-secret")
        for k, v in (secrets or {}).items():
            (self.tmp / k).write_text(v)
        self.env = Env({"OPCARE_SITE": site, "OPCARE_EDGE_ID": "edge-test", "OPCARE_SOURCE_ID": source_id,
                        "OPCARE_MQTT_URL": "mqtt://localhost:1883", "OPCARE_MQTT_USERNAME": "test",
                        "OPCARE_MQTT_PASSWORD_FILE": str(self.tmp / "mqtt_pw"),
                        "OPCARE_SECRETS_DIR": str(self.tmp)})
        self.validate = SchemaValidator(find_schemas())
        self.mq = FakeMqtt()
        self.log = io.StringIO()
        self.rt = Runtime(connector, env=self.env, client=self.mq, validator=self.validate,
                          log=JsonLog(source_id, "debug", self.log))
        self.base = f"opcare/v1/{site}"
        self.source_id = source_id
        self._n = 0

    async def __aenter__(self):
        self.task = asyncio.create_task(self.rt.main())
        await asyncio.sleep(0.1)
        return self

    async def __aexit__(self, *exc):
        self.rt.request_stop()
        await asyncio.wait_for(self.task, 15)

    def config(self, cfg: dict) -> None:
        self.mq.deliver(f"{self.base}/config/{self.source_id}", cfg)

    async def admin(self, op: str, args: dict | None = None, timeout_ms: int = 5000) -> dict:
        self._n += 1
        rid = f"00000000-0000-4000-8000-{self._n:012d}"
        self.mq.deliver(f"{self.base}/admin/{self.source_id}/req",
                        {"spec": "1.0", "request_id": rid, "op": op, "args": args or {}, "timeout_ms": timeout_ms,
                         "requested_by": "test", "ts": "2026-11-03T18:00:00.000Z"})
        for _ in range(int(timeout_ms / 50) + 20):
            await asyncio.sleep(0.05)
            res = [m for m in self.mq.msgs("admin") if m["request_id"] == rid]
            if res:
                return res[-1]
        raise AssertionError(f"no admin response to {op}")

    async def wait_for(self, predicate, timeout: float = 5.0, what: str = "condition") -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if predicate():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def schema_errors(self) -> list:
        """Every published message (and the Last Will) checked against the spec schemas."""
        errs = []
        for topic, msg, retain in self.mq.published:
            kind = topic.split("/")[3]
            for e in self.validate(f"{self.SCHEMA_OF[kind]}.schema.json", msg):
                errs.append((topic, e))
            if retain != (kind == "status"):
                errs.append((topic, "wrong retain flag"))
        errs += [("will", e) for e in self.validate("status.schema.json", json.loads(self.mq.will[1]))]
        seqs = [m["seq"] for m in self.mq.msgs("data")]
        if seqs != list(range(1, len(seqs) + 1)):
            errs.append(("data", f"seq not continuous: {seqs[:20]}"))
        return errs
