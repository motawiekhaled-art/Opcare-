"""Modbus connector tests: protocol unit tests and end-to-end runs against the simulator.

Run from the repository root:  python -m pytest connectors/modbus/tests -q
End-to-end tests run the connector inside the real SDK Runtime (in-memory MQTT) against
tools/simulators/modbus_sim.py over real TCP, and check every published message against the schemas.
"""
import asyncio
import math
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/modbus", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from modbus_connector import ModbusConnector  # noqa: E402
from modbus_connector.decode import decode, plan, to_bytes  # noqa: E402
from modbus_connector.protocol import (DeviceTimeout, ModbusException, RtuLink, crc16, parse_response,  # noqa: E402
                                       read_pdu, rtu_frame)
from modbus_sim import ModbusSimulator  # noqa: E402
from opcare_connector_sdk import ALLOW, ConnectorError, Point, PointTable, Q, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402

SITE = "site-a"


# ===================================================================== protocol unit tests
def test_crc_and_rtu_frame_known_vector():
    assert rtu_frame(1, bytes.fromhex("030000000A")) == bytes.fromhex("01030000000AC5CD")
    assert crc16(b"123456789") == 0x4B37           # CRC-16/MODBUS check value


def test_only_reads_can_be_encoded():
    for fc in (5, 6, 15, 16, 22, 23):
        with pytest.raises(ConnectorError) as e:
            read_pdu(fc, 0, 1)
        assert e.value.code == "OP_FORBIDDEN"
    with pytest.raises(ConnectorError):
        read_pdu(3, 0, 126)                         # more than 125 registers
    with pytest.raises(ConnectorError):
        read_pdu(3, 65535, 2)                       # beyond the address space


def test_parse_responses():
    assert parse_response(3, 2, bytes([3, 4, 0x12, 0x34, 0x56, 0x78])) == [0x1234, 0x5678]
    assert parse_response(1, 10, bytes([1, 2, 0b00000101, 0b10])) == [1, 0, 1, 0, 0, 0, 0, 0, 0, 1]
    with pytest.raises(ModbusException) as e:
        parse_response(3, 1, bytes([0x83, 2]))
    assert e.value.code == 2
    with pytest.raises(ConnectorError):
        parse_response(3, 2, bytes([3, 2, 0, 1]))   # wrong register count


@pytest.mark.parametrize("word,byte,regs", [("big", "big", [0x4499, 0xA000]), ("little", "big", [0xA000, 0x4499]),
                                            ("big", "little", [0x9944, 0x00A0]), ("little", "little", [0x00A0, 0x9944])])
def test_float_word_and_byte_orders(word, byte, regs):
    a = {"table": "holding_register", "address": 0, "encoding": "float32", "word_order": word, "byte_order": byte}
    assert decode(a, regs) == 1229.0
    assert to_bytes(regs, word, byte) == struct.pack(">f", 1229.0)


def test_other_encodings():
    hr = {"table": "holding_register", "address": 0}
    assert decode({**hr, "encoding": "int16"}, [0xFFFE]) == -2
    assert decode({**hr, "encoding": "int32"}, [0xFFFF, 0xFFFF]) == -1
    assert decode({**hr, "encoding": "uint64"}, [0, 0, 1, 0]) == 65536
    assert decode({**hr, "encoding": "bit", "bit": 2}, [0b100]) is True
    assert decode({**hr, "encoding": "string", "length": 3}, [0x4142, 0x4300, 0]) == "ABC"
    assert decode({"table": "coil", "address": 0, "encoding": "bool"}, [1]) is True


def test_read_planner_merges_and_splits():
    def pt(tag, addr, enc="uint16", dev="d1", table="holding_register"):
        return Point(tag, {"device": dev, "table": table, "address": addr, "encoding": enc}, "float", device=dev)
    pts = [pt("a", 0), pt("b", 1, "float32"), pt("c", 8), pt("d", 30), pt("e", 0, "bool", table="coil"),
           pt("f", 0, dev="d2")]
    blocks = plan(pts, {"d1": 1, "d2": 2}, max_regs=100, max_gap=10)
    hr1 = [b for b in blocks if b.device == "d1" and b.table == "holding_register"]
    assert [(b.start, b.count) for b in hr1] == [(0, 9), (30, 1)]       # gap 8->30 is more than 10
    assert len(blocks) == 4
    small = plan([pt(str(i), i * 2) for i in range(10)], {"d1": 1}, max_regs=5, max_gap=10)
    assert all(b.count <= 5 for b in small)


def test_rtu_over_stream_with_crc():
    asyncio.run(_rtu_scenario())


async def _rtu_scenario():
    async def slave(reader, writer):                # minimal RTU slave over TCP (serial-server style)
        while True:
            try:
                req = await reader.readexactly(8)
            except asyncio.IncompleteReadError:
                return
            assert struct.unpack("<H", req[-2:])[0] == crc16(req[:-2])
            unit, fc, start, qty = struct.unpack(">BBHH", req[:6])
            if unit == 9:                            # corrupt CRC on purpose
                writer.write(bytes([9, fc, 2, 0, 1, 0, 0]))
            else:
                writer.write(rtu_frame(unit, bytes([fc, 2 * qty]) + b"".join(
                    struct.pack(">H", start + i) for i in range(qty))))
            await writer.drain()
    server = await asyncio.start_server(slave, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    link = RtuLink({"device": "test"}, ReadOnlyGuard("modbus", ALLOW["modbus"]), inter_request_delay_ms=0)
    link.attach(*await asyncio.open_connection("127.0.0.1", port))
    assert await link.read(3, "holding_register", 40, 3, 1.0) == [40, 41, 42]
    with pytest.raises(DeviceTimeout):
        await link.read(9, "holding_register", 0, 1, 0.5)
    await link.close()
    server.close()


# ===================================================================== end to end with the simulator
def config(port, revision=1, extra_points=(), scan_ms=300, units=(1, 2)):
    pts = [
        {"tag_id": "site-a.m1.p-total", "data_type": "float", "unit": "kW",
         "address": {"device": "m1", "table": "holding_register", "address": 0, "encoding": "float32"}},
        {"tag_id": "site-a.m1.voltage", "data_type": "float", "unit": "V", "scaling": {"gain": 0.1, "offset": 0},
         "address": {"device": "m1", "table": "holding_register", "address": 2, "encoding": "uint16"}},
        {"tag_id": "site-a.m1.alarm", "data_type": "bool",
         "address": {"device": "m1", "table": "holding_register", "address": 3, "encoding": "bit", "bit": 2}},
        {"tag_id": "site-a.m1.energy", "data_type": "int",
         "address": {"device": "m1", "table": "holding_register", "address": 10, "encoding": "int32",
                     "word_order": "little"}},
        {"tag_id": "site-a.m1.model", "data_type": "string",
         "address": {"device": "m1", "table": "holding_register", "address": 20, "encoding": "string", "length": 2}},
        {"tag_id": "site-a.m1.breaker", "data_type": "bool",
         "address": {"device": "m1", "table": "coil", "address": 0, "encoding": "bool"}},
        {"tag_id": "site-a.m1.failing", "data_type": "float",
         "address": {"device": "m1", "table": "holding_register", "address": 50, "encoding": "uint16"}},
        {"tag_id": "site-a.m1.gap-a", "data_type": "int",
         "address": {"device": "m1", "table": "holding_register", "address": 60, "encoding": "uint16"}},
        {"tag_id": "site-a.m1.gap-b", "data_type": "int",
         "address": {"device": "m1", "table": "holding_register", "address": 62, "encoding": "uint16"}},
        {"tag_id": "site-a.m2.temp", "data_type": "float", "unit": "degC", "scaling": {"gain": 0.1, "offset": 0},
         "address": {"device": "m2", "table": "input_register", "address": 100, "encoding": "int16"}},
    ] + list(extra_points)
    return {"spec": "1.0", "revision": revision, "site": SITE, "source_id": "modbus-test", "type": "modbus",
            "name": "test meters", "enabled": True,
            "connection": {"transport": "tcp", "host": "127.0.0.1", "port": port,
                           "devices": [{"id": f"m{u}", "unit_id": u} for u in units], "max_gap": 10},
            "acquisition": {"default_scan_ms": scan_ms, "batch_max_values": 500, "batch_max_ms": 100,
                            "integrity_s": 900, "request_timeout_ms": 400},
            "points": pts}


def make_sim():
    sim = ModbusSimulator(strict=True)
    sim.set_value(1, "holding_register", 0, ">f", 412.5)
    sim.set_regs(1, "holding_register", 2, [2305, 0b100])              # 230.5 V, alarm bit 2 set
    sim.set_value(1, "holding_register", 10, ">i", -123456, word_order="little")
    sim.set_regs(1, "holding_register", 20, [0x4142, 0x4344])         # "ABCD"
    sim.set_regs(1, "holding_register", 50, [7])
    sim.exceptions[(1, "holding_register", 50)] = 4                  # device failure on this register
    sim.set_regs(1, "holding_register", 60, [11])
    sim.set_regs(1, "holding_register", 62, [22])                     # address 61 does not exist
    sim.set_bit(1, "coil", 0, True)
    sim.set_value(2, "input_register", 100, ">h", -52)                # -5.2 degC
    return sim


def test_end_to_end():
    asyncio.run(_e2e())


async def _e2e():
    sim = make_sim()
    port = await sim.start()
    conn = ModbusConnector()
    async with Harness(conn, site=SITE, source_id="modbus-test") as h:
        h.config(config(port))
        latest = h.mq.latest
        await h.wait_for(lambda: len(latest()) >= 10 and h.rt.state == "running", what="first values")
        v = latest()
        assert v["site-a.m1.p-total"]["v"] == 412.5 and v["site-a.m1.p-total"]["q"] == Q.GOOD
        assert v["site-a.m1.voltage"]["v"] == pytest.approx(230.5)
        assert v["site-a.m1.alarm"]["v"] is True and v["site-a.m1.breaker"]["v"] is True
        assert v["site-a.m1.energy"]["v"] == -123456 and v["site-a.m1.model"]["v"] == "ABCD"
        assert v["site-a.m2.temp"]["v"] == pytest.approx(-5.2)
        assert v["site-a.m1.failing"]["q"] == Q.BAD_DEVICE_FAILURE and v["site-a.m1.failing"]["f"] == ["EXC4"]
        await h.wait_for(lambda: latest()["site-a.m1.gap-a"]["q"] == Q.GOOD
                         and latest()["site-a.m1.gap-b"]["v"] == 22, what="gap fallback")

        # merged reads: one cycle needs far fewer requests than points
        diag = (await h.admin("get_diagnostics"))["result"]["connector"]
        cls = diag["scan_classes"]["300"]
        assert cls["points"] == 10 and cls["blocks"] == 4 and cls["split_blocks"] == 2
        n0 = len(sim.log)
        await asyncio.sleep(0.9)                                       # 3 cycles of 300 ms
        per_cycle = (len(sim.log) - n0) / 3
        assert 7 <= per_cycle <= 9, per_cycle   # 0-3, 10-11, 20-21, coil, 50, 60, 62, unit 2 = 8 reads

        # unit 2 stops answering -> its point q=24, state degraded; then recovers
        sim.offline.add(2)
        await h.wait_for(lambda: latest()["site-a.m2.temp"]["q"] == Q.BAD_COMM_FAILURE, what="unit 2 loss")
        await h.wait_for(lambda: h.rt.state == "degraded", what="degraded")
        assert latest()["site-a.m1.p-total"]["q"] == Q.GOOD
        sim.offline.discard(2)
        await h.wait_for(lambda: latest()["site-a.m2.temp"]["q"] == Q.GOOD and h.rt.state == "running",
                         what="unit 2 recovery")

        # whole device gone -> every point q=24, state error; comes back -> fresh values
        await sim.stop()
        await h.wait_for(lambda: all(x["q"] == Q.BAD_COMM_FAILURE for x in latest().values()), what="link loss")
        await h.wait_for(lambda: h.rt.state == "error", what="error state")
        sim.set_value(1, "holding_register", 0, ">f", 500.0)
        await sim.start(port=port)
        await h.wait_for(lambda: latest()["site-a.m1.p-total"]["v"] == 500.0, timeout=8, what="reconnect")
        await h.wait_for(lambda: h.rt.state == "running", what="running again")

        # admin: test_connection and read_now
        tc = (await h.admin("test_connection"))["result"]["devices"]
        assert [d["ok"] for d in tc] == [True, True]
        rn = (await h.admin("read_now", {"tag_ids": ["site-a.m1.voltage"], "addresses": [
            {"address": {"device": "m1", "table": "holding_register", "address": 0, "encoding": "float32"},
             "data_type": "float"}]}))["result"]["values"]
        assert rn[0]["v"] == pytest.approx(230.5) and rn[1]["v"] == 500.0
        bad = await h.admin("browse")
        assert bad["ok"] is False and bad["error"]["code"] == "OP_UNSUPPORTED"

        # hot reload: new point, same connection -> no reconnect
        accepted = sim.accepted
        sim.set_regs(1, "holding_register", 70, [99])
        h.config(config(port, revision=2, extra_points=[
            {"tag_id": "site-a.m1.new", "data_type": "int",
             "address": {"device": "m1", "table": "holding_register", "address": 70, "encoding": "uint16"}}]))
        await h.wait_for(lambda: latest().get("site-a.m1.new", {}).get("v") == 99, what="hot reload value")
        assert sim.accepted == accepted and h.rt.revision == 2

        # the read-only guard
        with pytest.raises(ConnectorError) as e:
            conn.guard.check("FC16")
        assert e.value.code == "OP_FORBIDDEN"
        await h.wait_for(lambda: any(m["type"] == "guard.blocked" for m in h.mq.msgs("event")), what="guard event")

    # after shutdown: only read function codes ever reached the device; every message valid
    assert sim.function_codes() <= {1, 2, 3, 4}, sim.function_codes()
    assert h.mq.msgs("status")[-1]["state"] == "stopping"
    assert h.schema_errors() == []
    await sim.stop()


def test_load_2000_points():
    asyncio.run(_load())


async def _load():
    sim = ModbusSimulator(strict=False)
    sim.set_regs(1, "holding_register", 0, list(range(2000)))
    port = await sim.start()
    pts = [{"tag_id": f"site-a.load.r{i}", "data_type": "int",
            "address": {"device": "m1", "table": "holding_register", "address": i, "encoding": "uint16"}}
           for i in range(2000)]
    cfg = config(port, extra_points=[], scan_ms=1000, units=(1,))
    cfg["points"] = pts
    cfg["connection"]["max_registers_per_read"] = 100
    async with Harness(ModbusConnector(), site=SITE, source_id="modbus-test") as h:
        h.config(cfg)
        await h.wait_for(lambda: len(h.mq.latest()) == 2000, timeout=5, what="2000 values")
        n0 = len(sim.log)
        await asyncio.sleep(2.05)
        per_cycle = (len(sim.log) - n0) / 2
        assert 19 <= per_cycle <= 21, per_cycle                       # 2000 registers / 100 per read
        assert h.rt.overruns == 0
        assert all(v["v"] == int(v["t"].rsplit("r", 1)[1]) for v in h.mq.latest().values())
    assert h.schema_errors() == []
    await sim.stop()
