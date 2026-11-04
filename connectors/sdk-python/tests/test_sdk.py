"""SDK tests. Run from sdk-python/:  python -m pytest -q   (or: python tests/test_sdk.py)

The runtime test uses an in-memory MQTT stand-in and validates EVERY published message against the
JSON Schemas in ../schemas, so SDK and spec cannot drift apart.
"""
import asyncio
import json
import pathlib
import re
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from opcare_connector_sdk import (Batcher, Connector, ConnectorError, Env, JsonLog, PointTable, Q,  # noqa: E402
                                  ReadOnlyGuard, Runtime, SchemaValidator, ValuePipeline, ulid, ALLOW)

from opcare_connector_sdk.testing import find_schemas  # noqa: E402
SCHEMAS = find_schemas()
validate = SchemaValidator(SCHEMAS)
BOOT = "6c1e3b9e-1f7e-4a8e-9a33-3f1c2d4b5a60"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def cfg(revision=1, points=None, enabled=True, host="10.0.0.1"):
    return {"spec": "1.0", "revision": revision, "site": "site-a", "source_id": "modbus-test", "type": "modbus",
            "name": "test", "enabled": enabled,
            "connection": {"transport": "tcp", "host": host, "devices": [{"id": "d1", "unit_id": 1},
                                                                          {"id": "d2", "unit_id": 2}]},
            "acquisition": {"default_scan_ms": 1000, "batch_max_values": 500, "batch_max_ms": 100,
                            "integrity_s": 900},
            "points": points or [
                {"tag_id": "site-a.m1.p", "data_type": "float", "scaling": {"gain": 0.1, "offset": 0},
                 "deadband": {"mode": "abs", "value": 0.5},
                 "address": {"device": "d1", "table": "holding_register", "address": 0, "encoding": "uint16"}},
                {"tag_id": "site-a.m2.run", "data_type": "bool",
                 "address": {"device": "d2", "table": "coil", "address": 0, "encoding": "bool"}}]}


# ------------------------------------------------------------------ core
def test_quality_and_ulid():
    assert Q.combine(192, 68, 216) == 68 and Q.combine(192, 24) == 24
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", ulid())


def test_point_conversion_and_deadband():
    t = PointTable(cfg())
    p = t.points["site-a.m1.p"]
    assert p.device == "d1" and p.convert(1234) == (123.4, None, [])
    assert p.convert(float("nan"))[1:] == (Q.BAD, ["NAN"])
    assert not p.changed(100.0, 100.4) and p.changed(100.0, 100.6)
    p.deadband_mode, p.deadband_value = "pct", 1.0
    assert not p.changed(100.0, 100.9) and p.changed(100.0, 101.1)


def test_pipeline_exception_integrity_and_comm_loss():
    out, clock = [], Clock()
    pl = ValuePipeline(PointTable(cfg()), 900, out.append, clock)
    assert pl.offer("site-a.m1.p", 1000) and not pl.offer("site-a.m1.p", 1003)  # 100.0 -> 100.3 inside deadband
    assert pl.offer("site-a.m1.p", 1003, q=Q.UNCERTAIN)                          # quality change is published
    assert not pl.offer("unknown.tag", 1)
    assert pl.device_lost("d1") == 1 and out[-1]["q"] == 24 and out[-1]["v"] is None
    assert pl.device_lost("d1") == 0                                              # only once
    pl.offer("site-a.m2.run", 1)
    clock.t += 901
    assert pl.tick() == 1 and out[-1]["f"] == ["INTEGRITY"]                      # bad-comm point not refreshed


def test_batcher_seq_count_time_and_size():
    sent, clock = [], Clock()
    b = Batcher("modbus-test", BOOT, sent.append, max_values=3, max_ms=1000, clock=clock)
    v = {"t": "a.b", "v": 1, "q": 192, "ts": "2026-11-03T18:00:00.000Z", "rx": "2026-11-03T18:00:00.000Z"}
    for _ in range(3):
        b.add(dict(v))
    assert len(sent) == 1 and sent[0]["seq"] == 1
    b.add(dict(v)); b.poll(); assert len(sent) == 1
    clock.t += 1.0; b.poll(); assert len(sent) == 2 and sent[1]["seq"] == 2
    b.max_values = 100000
    big = dict(v, v="x" * 1000)
    for _ in range(300):
        b.add(dict(big))
    b.flush()
    assert all(len(json.dumps(m)) < 256 * 1024 for m in sent) and [m["seq"] for m in sent] == list(range(1, len(sent) + 1))
    for m in sent:
        assert not validate("value-batch.schema.json", m)


def test_guard():
    blocked = []
    g = ReadOnlyGuard("modbus", ALLOW["modbus"], on_block=blocked.append)
    g.check("FC3")
    try:
        g.check("FC16")
        raise AssertionError("write was not blocked")
    except ConnectorError as exc:
        assert exc.code == "OP_FORBIDDEN"
    assert blocked == ["FC16"] and g.blocked_total == 1
    assert "WRITE_RCB:RptEna" in ALLOW["iec61850"] and "Write" not in ALLOW["opcua"]


# ------------------------------------------------------------------ runtime end to end
class FakeMqtt:
    def __init__(self):
        self.published, self.subs, self.will, self.connected, self.on_message = [], [], None, False, None

    def set_will(self, topic, payload, qos, retain): self.will = (topic, payload, qos, retain)
    def connect(self): self.connected = True
    def subscribe(self, topic, qos): self.subs.append(topic)
    def unsubscribe(self, topic): pass
    def publish(self, topic, payload, qos, retain): self.published.append((topic, json.loads(payload), retain))
    def disconnect(self): self.connected = False

    def deliver(self, topic, payload):
        self.on_message(topic, payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    def msgs(self, kind):
        return [m for t, m, _ in self.published if f"/{kind}/" in t]


class DemoConnector(Connector):
    type, version = "modbus", "0.1.0"
    capabilities = {"browse": True, "read_now": True}

    def devices(self, config):
        return [d["id"] for d in config["connection"]["devices"]]

    async def start(self, config, table, sink):
        self.sink, self.started = sink, getattr(self, "started", 0) + 1
        sink.device_state("d1", "connected")
        sink.device_state("d2", "connected")
        sink.value("site-a.m1.p", 1000)
        sink.value("site-a.m2.run", 1)

    async def stop(self):
        self.stopped = True

    async def apply_points(self, config, table):
        return True

    async def test_connection(self, config):
        return {"devices": [{"id": d, "ok": True, "latency_ms": 3} for d in self.devices(config)]}

    async def browse(self, args):
        return {"items": [{"path": f"n{i}", "name": "x" * 300, "kind": "variable", "has_children": False}
                          for i in range(2000)]}


async def _settle(n=5):
    for _ in range(n):
        await asyncio.sleep(0.05)


def test_runtime_end_to_end():
    asyncio.run(_runtime_scenario())


async def _runtime_scenario():
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "pw").write_text("secret-password")
    env = Env({"OPCARE_SITE": "site-a", "OPCARE_EDGE_ID": "edge-1", "OPCARE_SOURCE_ID": "modbus-test",
               "OPCARE_MQTT_URL": "mqtt://localhost:1883", "OPCARE_MQTT_USERNAME": "u",
               "OPCARE_MQTT_PASSWORD_FILE": str(tmp / "pw"), "OPCARE_SECRETS_DIR": str(tmp)})
    mq, conn = FakeMqtt(), DemoConnector()
    logs = open(tmp / "log.jsonl", "w")
    rt = Runtime(conn, env=env, client=mq, validator=validate, log=JsonLog("modbus-test", "debug", logs))
    main = asyncio.create_task(rt.main())
    await _settle()
    base = "opcare/v1/site-a"
    assert mq.connected and f"{base}/config/modbus-test" in mq.subs
    assert json.loads(mq.will[1])["state"] == "offline" and mq.will[3] is True
    assert mq.msgs("status")[-1]["state"] == "waiting_config"

    # valid configuration -> running, values published
    mq.deliver(f"{base}/config/modbus-test", cfg(1))
    await _settle(8)
    assert rt.state == "running" and rt.revision == 1
    values = [v for m in mq.msgs("data") for v in m["values"]]
    assert {v["t"] for v in values} == {"site-a.m1.p", "site-a.m2.run"} and values[0]["v"] == 100.0

    # invalid newer revision -> rejected, old one keeps running
    bad = cfg(2)
    bad["points"][0]["address"]["encoding"] = "float99"
    mq.deliver(f"{base}/config/modbus-test", bad)
    await _settle()
    assert rt.revision == 1 and rt.state == "running" and rt.last_error["code"] == "CFG_INVALID"
    assert mq.msgs("event")[-1]["type"] == "config.rejected"

    # field not in the modbus schema -> rejected by the schema
    sec = cfg(3)
    sec["connection"]["tls"] = {"enabled": True, "ca_cert_ref": "no-such-file"}
    mq.deliver(f"{base}/config/modbus-test", sec)
    await _settle()
    assert rt.revision == 1 and "'tls' was unexpected" in rt.last_error["message"]
    # missing secret file -> rejected (schema check bypassed to reach the secret rule)
    real = rt.validate_schema
    rt.validate_schema = lambda rel, doc: []
    mq.deliver(f"{base}/config/modbus-test", sec)
    await _settle()
    rt.validate_schema = real
    assert rt.revision == 1 and "secret 'no-such-file' missing" in rt.last_error["message"]

    # same connection, new points -> hot reload without restart
    mq.deliver(f"{base}/config/modbus-test", cfg(4))
    await _settle()
    assert rt.revision == 4 and conn.started == 1
    # changed connection -> restart
    mq.deliver(f"{base}/config/modbus-test", cfg(5, host="10.0.0.2"))
    await _settle()
    assert rt.revision == 5 and conn.started == 2

    # device lost -> q=24 for its points, conn.lost event, degraded
    conn.sink.device_state("d2", "unreachable", ConnectorError("CONN_TIMEOUT", "no answer"))
    await _settle(8)
    lost = [v for m in mq.msgs("data") for v in m["values"] if v["q"] == 24]
    assert [v["t"] for v in lost] == ["site-a.m2.run"] and rt.state == "degraded"
    assert any(e["type"] == "conn.lost" for e in mq.msgs("event"))

    # admin operations
    def req(op, args=None, rid="00000000-0000-4000-8000-00000000000"):
        return {"spec": "1.0", "request_id": rid, "op": op, "args": args or {}, "timeout_ms": 5000,
                "requested_by": "test", "ts": "2026-11-03T18:00:00.000Z"}
    mq.deliver(f"{base}/admin/modbus-test/req", req("ping", rid="00000000-0000-4000-8000-000000000001"))
    mq.deliver(f"{base}/admin/modbus-test/req", req("bogus", rid="00000000-0000-4000-8000-000000000002"))
    await _settle()
    res = {m["request_id"][-1]: m for m in mq.msgs("admin")}
    assert res["1"]["ok"] and res["1"]["result"]["version"] == "0.1.0"
    assert res["2"]["error"]["code"] == "OP_UNSUPPORTED"
    mq.deliver(f"{base}/admin/modbus-test/req", req("browse", rid="00000000-0000-4000-8000-000000000003"))
    await _settle()
    first = [m for m in mq.msgs("admin") if m["request_id"].endswith("3")][-1]
    assert first["done"] is False and first["cursor"]
    pages, cursor, n = [first], first.get("cursor"), 4
    while cursor:                                  # follow the cursor chain until done
        rid = f"00000000-0000-4000-8000-00000000000{n}"
        mq.deliver(f"{base}/admin/modbus-test/req", req("browse", {"cursor": cursor}, rid=rid))
        await _settle()
        page = [m for m in mq.msgs("admin") if m["request_id"] == rid][-1]
        pages.append(page)
        cursor, n = (None if page["done"] else page["cursor"]), n + 1
    assert len(pages) >= 3 and sum(len(p["result"]["items"]) for p in pages) == 2000
    assert [p["result"]["items"][0]["path"] for p in pages][0] == "n0"

    # graceful stop -> stopping status, normal disconnect
    rt.request_stop()
    assert await asyncio.wait_for(main, 5) == 0
    assert mq.msgs("status")[-1]["state"] == "stopping" and not mq.connected and conn.stopped

    # every published message is valid against the spec schemas
    schema = {"data": "value-batch", "event": "event", "status": "status", "admin": "admin-response"}
    for topic, msg, retain in mq.published:
        kind = topic.split("/")[3]
        errs = validate(f"{schema[kind]}.schema.json", msg)
        assert not errs, (topic, errs, msg)
        assert retain == (kind == "status")
    assert not validate("status.schema.json", json.loads(mq.will[1]))
    seqs = [m["seq"] for m in mq.msgs("data")]
    assert seqs == list(range(1, len(seqs) + 1))
    log_text = (tmp / "log.jsonl").read_text()
    assert "secret-password" not in log_text and all(json.loads(line) for line in log_text.splitlines())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
