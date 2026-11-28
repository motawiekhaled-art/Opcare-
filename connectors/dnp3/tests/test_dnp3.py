"""DNP3 connector tests: link/transport/application unit tests, and end-to-end runs against
tools/simulators/dnp3_outstation.py (our stack in the outstation role) over real TCP and TLS, with every published
message checked against the schemas. With OPCARE_DNP3_INTEROP_PY pointing to a Python 3.8-3.10 that has the
'dnp3-python' package (Apache-2.0, opendnp3), the connector also runs against that independent outstation (CI job
"dnp3-interop"; never part of the product).

Run from the repository root:  python -m pytest connectors/dnp3/tests -q
"""
import asyncio
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/dnp3", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk import ALLOW, ConnectorError, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402
from dnp3_connector import Dnp3Connector  # noqa: E402
from dnp3_connector import app as A  # noqa: E402
from dnp3_connector import link as L  # noqa: E402
from dnp3_connector.master import Dnp3Master  # noqa: E402
from dnp3_outstation import Dnp3Outstation  # noqa: E402

SITE = "ss-giza"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ===================================================================== unit tests
def test_crc_link_frames_and_transport():
    assert A and L.crc(bytes.fromhex("056405C001000004")) == bytes.fromhex("E921")       # IEEE 1815 example
    data = bytes(range(40))
    raw = L.encode(L.Frame(0xC4, 10, 1, data))
    assert len(raw) == 10 + 40 + 2 * 3 and L.decode(raw) == L.Frame(0xC4, 10, 1, data)
    bad = bytearray(raw)
    bad[20] ^= 0xFF
    with pytest.raises(L.LinkError):
        L.decode(bytes(bad))
    frag = bytes(range(256)) * 3
    segs, nxt = L.segments(frag, 62)
    assert len(segs) == 4 and nxt == (62 + 4) & 0x3F and segs[0][0] & 0x40 and segs[-1][0] & 0x80
    r = L.Reassembly()
    assert [r.add(s) for s in segs][-1] == frag
    r2 = L.Reassembly()
    assert [r2.add(s) for s in (segs[0], segs[2], segs[3])] == [None, None, None]     # lost segment: dropped


def test_objects_round_trip_and_quality_mapping():
    t = datetime(2026, 11, 28, 11, 0, 1, 250000, tzinfo=timezone.utc)
    for (g, v), (size, _) in A.FORMATS.items():
        if v == 3 and g in (2, 4):
            continue                                       # relative time: tested below with a CTO
        val = True if g in (1, 2, 10, 11) else 2 if g in (3, 4) else \
            1234 if (g, v) in A.INTEGER_VARIATIONS or g in (20, 21, 22, 23) else 12.5
        body = A.encode_points(g, v, [(5, val, A.ONLINE, t), (6, val, A.ONLINE, t)], indexed=g in A.EVENT_GROUPS)
        pts = A.parse(A.response(0xC0, A.FC_RESPONSE, 0) + body).objects
        assert [(p.index, p.value) for p in pts] == [(5, val), (6, val)], (g, v)
        assert pts[0].time in (None, t)
    for gv, bits in A.PACKED.items():
        vals = [True, False, True] if bits == 1 else [0, 1, 2, 3]
        pts = A.parse(A.response(0xC0, 129, 0) + A.encode_points(*gv, [(i, x, 0, None) for i, x in enumerate(vals)], False)).objects
        assert [p.value for p in pts] == vals and pts[0].flags is None
    import struct
    cto = bytes([51, 1, 0x07, 1]) + A.pack_time48(t) + bytes([2, 3, 0x17, 1, 9, 0x81]) + struct.pack("<H", 500)
    ev = A.parse(A.response(0xC0, 129, 0) + cto).objects[0]
    assert ev.index == 9 and ev.value is True and ev.time == datetime(2026, 11, 28, 11, 0, 1, 750000, tzinfo=timezone.utc)
    q = A.quality
    assert q("analog_input", 0x01) == 192 and q("analog_input", 0x00) == 0 and q("analog_input", 0x05) == 24
    assert q("analog_input", 0x03) == 68 and q("analog_input", 0x11) == 216 and q("analog_input", 0x09) == 216
    assert q("analog_input", 0x21) == 84 and q("counter", 0x21) == 84 and q("analog_input", 0x41) == 80
    assert q("binary_input", 0x21) == 64 and q("binary_input", 0x81) == 192 and q("binary_input", None) == 192
    assert A.flag_names("analog_input", 0x25) == ["ONLINE", "COMM_LOST", "OVER_RANGE"]


def test_only_allowed_requests_exist_and_guard_blocks_others():
    built = {A.confirm(0)[1], A.read_classes(0, (0,))[1], A.read_range(0, [(30, 0, 1)])[1], A.unsolicited(0, True)[1],
             A.unsolicited(0, False)[1], A.clear_restart(0)[1]}
    assert built == {0, 1, 2, 20, 21}
    for name in ("SELECT", "OPERATE", "DIRECT_OPERATE", "COLD_RESTART", "WARM_RESTART", "freeze", "time"):
        assert not any(name.lower() in n.lower() for n in dir(A) if callable(getattr(A, n)) and not n.startswith("_")
                       and n not in ("FC_NAMES",)) or name == "time"
    assert ALLOW["dnp3"] == {"FC0", "FC1", "FC20", "FC21", "FC2_CLEAR_RESTART", "LINK_STATUS_REQUEST", "LINK_STATUS",
                             "LINK_ACK"}
    blocked = []
    m = Dnp3Master("127.0.0.1", 1, 1, 10, ReadOnlyGuard("dnp3", ALLOW["dnp3"], on_block=blocked.append))
    m.pending = asyncio.Queue()
    for frag in (bytes([0xC0, 5, 12, 1, 0x17, 1, 0, 3, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]),          # DIRECT_OPERATE g12v1
                 bytes([0xC0, 2, 50, 1, 0x07, 1, 0, 0, 0, 0, 0, 0]),                             # WRITE time (g50v1)
                 bytes([0xC0, 2, 80, 1, 0x00, 7, 7, 0x01]),                                      # WRITE restart bit = 1
                 bytes([0xC0, 13])):                                                             # COLD_RESTART
        with pytest.raises(ConnectorError) as e:
            asyncio.run(m.request(lambda s, f=frag: f))
        assert e.value.code == "OP_FORBIDDEN"
    assert blocked == ["FC5", "FC2", "FC2", "FC13"]


# ===================================================================== end to end with the test outstation
def substation(**kw) -> Dnp3Outstation:
    o = Dnp3Outstation(address=10, **kw)
    for i, v in enumerate((231.4, 230.9, 232.1)):
        o.add("analog_input", i, v)                              # phase voltages, class 2
    o.add("analog_input", 3, 152.0, static=(30, 1))              # int32 variation
    o.add("binary_input", 0, True)                               # breaker trip alarm, class 1
    o.add("double_bit_binary_input", 0, 2)                       # breaker position: on
    o.add("counter", 0, 104500)                                  # energy pulses, class 3
    o.add("binary_output_status", 0, False)
    o.add("analog_output_status", 0, 50.0)
    return o


def cfg(port: int, rev=1, acq=None, points=None, conn=None):
    pts = points or [
        {"tag_id": "ss-giza.f1.va", "address": {"group": "analog_input", "index": 0}, "data_type": "float", "unit": "V"},
        {"tag_id": "ss-giza.f1.vb", "address": {"group": "analog_input", "index": 1}, "data_type": "float", "unit": "V"},
        {"tag_id": "ss-giza.f1.mw", "address": {"group": "analog_input", "index": 3}, "data_type": "int"},
        {"tag_id": "ss-giza.f1.trip", "address": {"group": "binary_input", "index": 0}, "data_type": "bool"},
        {"tag_id": "ss-giza.f1.cb", "address": {"group": "double_bit_binary_input", "index": 0}, "data_type": "enum"},
        {"tag_id": "ss-giza.f1.kwh", "address": {"group": "counter", "index": 0}, "data_type": "int"},
        {"tag_id": "ss-giza.f1.missing", "address": {"group": "analog_input", "index": 99}, "data_type": "float"},
    ]
    return {"spec": "1.0", "revision": rev, "site": SITE, "source_id": "src-test", "type": "dnp3", "name": "Feeder 1 RTU",
            "enabled": True,
            "connection": {"host": "127.0.0.1", "port": port, "master_address": 1, "outstation_address": 10,
                           "response_timeout_ms": 1500, "keep_alive_s": 2, **(conn or {})},
            "acquisition": {"batch_max_ms": 100, "integrity_s": 0, "integrity_on_connect": True, "integrity_period_s": 0,
                            "class_polls": {"class1_ms": 200, "class2_ms": 200, "class3_ms": 400}, **(acq or {})},
            "points": pts}


def test_end_to_end_polls_events_restart_link_loss_and_schemas():
    async def run():
        o = substation()
        await o.start()
        async with Harness(Dnp3Connector(), site=SITE) as h:
            h.config(cfg(o.port))
            await h.wait_for(lambda: h.rt.state == "running", what="running")
            await h.wait_for(lambda: len(h.mq.latest()) == 7, what="7 tags")
            v = h.mq.latest()
            assert v["ss-giza.f1.va"]["v"] == pytest.approx(231.4, abs=1e-3) and v["ss-giza.f1.va"]["q"] == 192
            assert v["ss-giza.f1.mw"]["v"] == 152 and v["ss-giza.f1.trip"]["v"] is True and v["ss-giza.f1.cb"]["v"] == 2
            assert v["ss-giza.f1.kwh"]["v"] == 104500
            assert v["ss-giza.f1.missing"]["q"] == 4 and v["ss-giza.f1.missing"]["f"] == ["NOT_IN_OUTSTATION"]
            await h.wait_for(lambda: not o.restart, what="IIN1.7 cleared")
            assert any(e["type"] == "device.restart" for e in h.mq.msgs("event"))

            # events with the outstation's own timestamps, in order, all of them
            t1 = datetime(2026, 11, 28, 9, 30, 0, 100000, tzinfo=timezone.utc)
            t2 = datetime(2026, 11, 28, 9, 30, 0, 350000, tzinfo=timezone.utc)
            o.set("binary_input", 0, False, ts=t1)
            o.set("binary_input", 0, True, ts=t2)
            o.set("analog_input", 0, 240.5, flags=A.ONLINE | A.OVER_RANGE)
            await h.wait_for(lambda: h.mq.latest()["ss-giza.f1.va"]["v"] == pytest.approx(240.5), what="class 2 event")
            trips = [x for b in h.mq.msgs("data") for x in b["values"] if x["t"] == "ss-giza.f1.trip"]
            assert [(x["v"], x.get("ts")) for x in trips[-2:]] == [(False, "2026-11-28T09:30:00.100Z"),
                                                                     (True, "2026-11-28T09:30:00.350Z")]
            assert h.mq.latest()["ss-giza.f1.va"]["q"] == 84 and "OVER_RANGE" in h.mq.latest()["ss-giza.f1.va"]["f"]
            o.set("double_bit_binary_input", 0, 1, flags=A.ONLINE | A.LOCAL_FORCED)
            await h.wait_for(lambda: h.mq.latest()["ss-giza.f1.cb"]["v"] == 1, what="breaker open")
            assert h.mq.latest()["ss-giza.f1.cb"]["q"] == 216
            o.set("analog_input", 1, flags=A.ONLINE | A.COMM_LOST)
            await h.wait_for(lambda: h.mq.latest()["ss-giza.f1.vb"]["q"] == 24, what="comm lost flag")

            # admin requests reuse the running association (one connection only)
            res = (await h.admin("read_now", {"tag_ids": ["ss-giza.f1.kwh"],
                                              "addresses": [{"address": {"group": "analog_output_status", "index": 0},
                                                             "data_type": "float"}]}))["result"]
            assert res["values"][0]["v"] == 104500 and res["values"][1]["v"] == 50.0
            b = (await h.admin("browse", {}))["result"]["items"]
            assert {"path": "analog_input/3", "address": {"group": "analog_input", "index": 3}, "data_type": "int"}.items() \
                <= next(i for i in b if i["path"] == "analog_input/3").items()
            t = (await h.admin("test_connection", {}))["result"]["devices"][0]
            assert t["ok"] and t["info"]["points"]["Analog input"] == 4
            assert o.connections == 1
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["not_in_outstation"] == ["ss-giza.f1.missing"] and diag["events"] >= 4

            # the outstation restarts: event, restart bit cleared, integrity poll
            o.restart = True
            await h.wait_for(lambda: not o.restart, timeout=6, what="restart cleared again")

            # link loss: q 24, then reconnect and fresh values
            o.kick_all()
            await h.wait_for(lambda: all(x["q"] == 24 for t_, x in h.mq.latest().items() if t_ != "ss-giza.f1.missing"),
                             what="q 24 on link loss")
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="reconnected")
            o.set("analog_input", 0, 229.0)
            await h.wait_for(lambda: h.mq.latest()["ss-giza.f1.va"]["v"] == pytest.approx(229.0), timeout=8, what="after reconnect")
            assert h.schema_errors() == []
        await o.stop()
        names = set(o.requests)
        assert names <= {"READ", "CONFIRM", "WRITE"}, names                    # no ENABLE: unsolicited is off
        assert set(o.link_requests) <= {"REQUEST_LINK_STATUS", "LINK_STATUS", "ACK"}   # busy link: no keep-alive needed
    asyncio.run(run())


def test_unsolicited_mode_silent_link_and_validation():
    async def run():
        o = substation()
        await o.start()
        async with Harness(Dnp3Connector(), site=SITE) as h:
            h.config(cfg(o.port, acq={"unsolicited": True, "class_polls": {"class1_ms": 0, "class2_ms": 0, "class3_ms": 0}}))
            await h.wait_for(lambda: len(h.mq.latest()) == 7, what="values")
            await h.wait_for(lambda: o.unsol_enabled == {1, 2, 3}, what="unsolicited enabled")
            reads = o.requests.count("READ")
            o.set("analog_input", 2, 236.6)
            await h.wait_for(lambda: h.mq.latest().get("ss-giza.f1.va", {}).get("v") is not None and
                             any(x["t"] == "ss-giza.f1.va" for b in h.mq.msgs("data") for x in b["values"]) and
                             o.events == [], what="unsolicited event confirmed")
            assert o.requests.count("READ") == reads                           # it came without polling
            # the outstation stops answering (TCP open): the keep-alive notices
            o.respond = False
            await h.wait_for(lambda: h.rt.state != "running", timeout=10, what="link check failed")
            o.respond = True
            await h.wait_for(lambda: h.rt.state == "running", timeout=10, what="back")
            assert "ENABLE_UNSOLICITED" in o.requests and h.schema_errors() == []
        await o.stop()
        bad = cfg(1, points=[{"tag_id": "ss-giza.x", "address": {"group": "binary_input", "index": 0}, "data_type": "float"},
                             {"tag_id": "ss-giza.y", "address": {"group": "binary_input", "index": 0}, "data_type": "bool"}])
        bad["connection"]["master_address"] = 10
        errs = await Dnp3Connector().validate(bad)
        assert any("must differ" in e for e in errs) and any("cannot be 'float'" in e for e in errs)
        assert any("already tag" in e for e in errs)
    asyncio.run(run())


def test_test_connection_browse_and_tls():
    x509 = pytest.importorskip("cryptography.x509")
    sys.path.insert(0, str(ROOT / "connectors/mqtt/tests"))
    from test_mqtt import _test_pki

    async def run():
        tmp = pathlib.Path(tempfile.mkdtemp())
        ca_pem, other_pem = _test_pki(tmp)
        sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        sctx.load_cert_chain(tmp / "srv.pem", tmp / "srv.key")
        o = substation(ssl=sctx)
        await o.start()
        c = Dnp3Connector()
        c.secrets_dir = tmp
        (tmp / "ca").write_text(ca_pem)
        (tmp / "other-ca").write_text(other_pem)
        tls = cfg(o.port, conn={"tls": {"enabled": True, "ca_cert_ref": "ca"}})
        res = (await c.test_connection(tls))["devices"][0]
        assert res["ok"] and res["info"]["tls"] and res["info"]["points"] == {
            "Analog input": 4, "Binary input": 1, "Double-bit input": 1, "Counter": 1, "Binary output status": 1,
            "Analog output status": 1}
        assert "DEVICE_RESTART" in res["info"]["iin"]
        wrong = cfg(o.port, conn={"tls": {"enabled": True, "ca_cert_ref": "other-ca"}})
        assert (await c.test_connection(wrong))["devices"][0]["error"]["code"] == "TLS_FAILED"
        b = (await c.browse({"config": tls}))["items"]
        heads = [i["name"] for i in b if i["kind"] == "object"]
        assert heads[0] == "Binary inputs" and "Analog inputs" in heads
        cb = next(i for i in b if i["path"] == "double_bit_binary_input/0")
        assert cb["data_type"] == "enum" and cb["info"]["sample"] == "on" and cb["info"]["variation"] == "g3v2"
        only = (await c.browse({"config": tls, "path": "counter"}))["items"]
        assert [i["path"] for i in only] == ["counter", "counter/0"]
        await o.stop()
        res = (await c.test_connection(cfg(free_port())))["devices"][0]
        assert res["error"]["code"] in ("CONN_REFUSED", "CONN_TIMEOUT")
        assert set(o.requests) <= {"READ", "CONFIRM"}
    assert x509
    asyncio.run(run())


# ===================================================================== independent outstation (CI: dnp3-interop)
def test_interop_with_opendnp3_outstation():
    py = os.environ.get("OPCARE_DNP3_INTEROP_PY")
    if not py:
        pytest.skip("OPCARE_DNP3_INTEROP_PY not set (the CI interop job sets it to a Python with dnp3-python)")

    async def run():
        port = free_port()
        proc = subprocess.Popen([py, str(ROOT / "tools/interop/opendnp3_outstation.py"), str(port)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        try:
            for _ in range(200):
                if "READY" in proc.stdout.readline():
                    break
            pts = [{"tag_id": "ss-giza.od.ai2", "address": {"group": "analog_input", "index": 2}, "data_type": "float"},
                   {"tag_id": "ss-giza.od.bi1", "address": {"group": "binary_input", "index": 1}, "data_type": "bool"},
                   {"tag_id": "ss-giza.od.ao3", "address": {"group": "analog_output_status", "index": 3}, "data_type": "float"},
                   {"tag_id": "ss-giza.od.bo1", "address": {"group": "binary_output_status", "index": 1}, "data_type": "bool"}]
            for unsol in (False, True):
                async with Harness(Dnp3Connector(), site=SITE) as h:
                    h.config(cfg(port, points=pts, acq={"unsolicited": unsol}))
                    await h.wait_for(lambda: len(h.mq.latest()) == 4, timeout=10, what="4 tags from opendnp3")
                    v = h.mq.latest()
                    assert v["ss-giza.od.ao3"]["v"] == 30.0 and v["ss-giza.od.bo1"]["v"] is True
                    new = 300.0 + unsol
                    proc.stdin.write(f"analog 2 {new}\nbinary 1 {int(not unsol)}\n")
                    proc.stdin.flush()
                    await h.wait_for(lambda: h.mq.latest()["ss-giza.od.ai2"]["v"] == new, timeout=10, what="event")
                    assert h.mq.latest()["ss-giza.od.bi1"]["v"] is (not unsol) and h.schema_errors() == []
                    t = (await h.admin("test_connection", {}))["result"]["devices"][0]
                    assert t["ok"] and t["info"]["points"]["Analog input"] == 5
        finally:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
            proc.wait(10)
    asyncio.run(run())
