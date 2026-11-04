"""Edge agent tests: starts a connector per configured source, stops it on delete, tests unsaved configs.

Run from the repository root:  python -m pytest edge -q
"""
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/modbus", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

from daq_platform.bus import InMemoryBus  # noqa: E402
from edge_agent import AGENT_ID, LocalAgent  # noqa: E402
from edge_agent.agent import probe_request  # noqa: E402
from modbus_connector import ModbusConnector  # noqa: E402
from modbus_sim import ModbusSimulator  # noqa: E402
from opcare_connector_sdk import SchemaValidator  # noqa: E402
from opcare_connector_sdk.testing import find_schemas  # noqa: E402

VALIDATOR = SchemaValidator(find_schemas())


def cfg(port, sid="src-a", rev=1, typ="modbus"):
    return {"spec": "1.0", "revision": rev, "site": "s1", "source_id": sid, "type": typ, "name": sid, "enabled": True,
            "connection": {"transport": "tcp", "host": "127.0.0.1", "port": port, "devices": [{"id": "d1", "unit_id": 1}]},
            "acquisition": {"default_scan_ms": 200, "batch_max_ms": 100, "request_timeout_ms": 500},
            "points": [{"tag_id": f"s1.{sid}.v", "data_type": "int",
                        "address": {"device": "d1", "table": "holding_register", "address": 0, "encoding": "uint16"}}]}


def test_agent_lifecycle_and_probe():
    asyncio.run(_scenario())


async def _scenario():
    sim = ModbusSimulator()
    sim.set_regs(1, "holding_register", 0, [7])
    port = await sim.start()
    bus = InMemoryBus()
    data, status, replies = [], {}, []
    bus.subscribe("opcare/v1/+/data/+", lambda t, p: data.extend(json.loads(p)["values"]))
    bus.subscribe("opcare/v1/+/status/+", lambda t, p: status.__setitem__(t.split("/")[4], json.loads(p)["state"] if p else None))
    bus.subscribe(f"opcare/v1/+/admin/{AGENT_ID}/res", lambda t, p: replies.append(json.loads(p)))
    agent = LocalAgent(bus, {"modbus": ModbusConnector}, VALIDATOR)
    try:
        # a configuration that existed before the agent started (retained) is picked up too
        bus.publish("opcare/v1/s1/config/src-a", json.dumps(cfg(port)), retain=True)
        await agent.start()
        bus.publish("opcare/v1/s1/config/src-b", json.dumps(cfg(port, "src-b")), retain=True)
        bus.publish("opcare/v1/s1/config/src-c", json.dumps(cfg(port, "src-c", typ="opcua")), retain=True)  # no connector
        for _ in range(60):
            await asyncio.sleep(0.1)
            if status.get("src-a") == "running" and status.get("src-b") == "running" \
                    and {"s1.src-a.v", "s1.src-b.v"} <= {v["t"] for v in data}:
                break
        assert set(agent.running) == {("s1", "src-a"), ("s1", "src-b")}
        assert {"s1.src-a.v", "s1.src-b.v"} <= {v["t"] for v in data}

        # a new revision is applied by the running connector itself, not restarted by the agent
        task_before = agent.running[("s1", "src-a")][1]
        bus.publish("opcare/v1/s1/config/src-a", json.dumps(cfg(port, rev=2)), retain=True)
        await asyncio.sleep(0.5)
        assert agent.running[("s1", "src-a")][1] is task_before

        # delete -> connector stopped, retained status cleared
        bus.publish("opcare/v1/s1/config/src-b", b"", retain=True)
        await asyncio.sleep(0.5)
        assert ("s1", "src-b") not in agent.running and bus.retained("opcare/v1/s1/status/src-b") is None

        # test before save: good, unreachable, invalid, unsupported type
        async def ask(args, op="test_connection"):
            req = probe_request(op, args, "test", 3000)
            bus.publish(f"opcare/v1/s1/admin/{AGENT_ID}/req", json.dumps(req))
            for _ in range(60):
                await asyncio.sleep(0.05)
                got = [r for r in replies if r["request_id"] == req["request_id"]]
                if got:
                    return got[0]
            raise AssertionError("agent did not answer")
        ok = await ask({"config": cfg(port, "new")})
        assert ok["ok"] and ok["result"]["devices"][0]["ok"]
        down = await ask({"config": cfg(1, "new")})
        assert down["ok"] and down["result"]["devices"][0]["error"]["code"] == "CONN_REFUSED"
        bad = cfg(port, "new")
        bad["points"][0]["address"]["device"] = "nope"
        invalid = await ask({"config": bad})
        assert not invalid["ok"] and invalid["error"]["code"] == "CFG_INVALID" and "unknown device" in invalid["error"]["message"]
        unsupported = await ask({"config": cfg(port, "new", typ="opcua")})
        assert unsupported["error"]["code"] == "OP_UNSUPPORTED"
        ping = await ask({}, op="ping")
        assert ping["result"]["types"] == ["modbus"]
        assert all(not r.get("ok") or "result" in r for r in replies)
        for r in replies:
            assert not VALIDATOR("admin-response.schema.json", r), r
    finally:
        await agent.stop()
        await sim.stop()
    assert agent.running == {}
