"""IEC 104 connector tests: APCI/ASDU unit tests and end-to-end runs against tools/simulators/iec104_station.py (our
stack in the station role) over real TCP and TLS, with every published message checked against the schemas. With
OPCARE_IEC104_INTEROP_SERVER pointing to lib60870's unmodified example server (cs104_server, GPLv3, built in the CI
job "iec104-interop" from the upstream sources and never part of this repository or the product), the connector
also runs against that independent station.

Run from the repository root:  python -m pytest connectors/iec104/tests -q
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
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/iec104", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk import ALLOW, ConnectorError, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402
from iec104_connector import Iec104Connector  # noqa: E402
from iec104_connector import apci as P  # noqa: E402
from iec104_connector import asdu as A  # noqa: E402
from iec104_connector.client import Iec104Client  # noqa: E402
from iec104_station import Iec104Station  # noqa: E402

SITE = "ss-cairo"
ALLOWED = {"STARTDT_ACT", "STOPDT_ACT", "TESTFR_ACT", "TESTFR_CON", "S", "C_IC_NA_1", "C_CI_NA_1", "C_RD_NA_1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ===================================================================== unit tests
def test_apci_frames():
    assert P.u_frame(P.STARTDT_ACT) == bytes.fromhex("680407000000")              # IEC 60870-5-104 figure 7
    assert P.u_frame(P.TESTFR_CON) == bytes.fromhex("680483000000")
    assert P.s_frame(5) == bytes.fromhex("680401000a00") and P.decode(P.s_frame(20000)[2:]).nr == 20000
    raw = P.i_frame(32767, 12345, b"\x01\x01\x03\x00\x01\x00\x01\x00\x00\x01")
    d = P.decode(raw[2:])
    assert (d.kind, d.ns, d.nr, d.asdu[0]) == ("I", 32767, 12345, 1) and raw[1] == len(raw) - 2
    assert P.seq_distance(32766, 1) == 3
    for bad in (b"\x01\x00\x00\x00\x99", b"\x47\x00\x00\x00"):
        with pytest.raises(P.ApciError):
            P.decode(bad)

    async def read(data):
        r = asyncio.StreamReader()
        r.feed_data(data)
        r.feed_eof()
        return await P.read_apdu(r)
    assert asyncio.run(read(P.u_frame(P.TESTFR_ACT))).name == "TESTFR_ACT"
    with pytest.raises(P.ApciError):
        asyncio.run(read(b"\x67\x04\x07\x00\x00\x00"))


def test_asdu_types_time_tags_and_quality():
    t = datetime(2026, 11, 28, 9, 30, 1, 250000, tzinfo=timezone.utc)
    samples = {"SP": True, "DP": 2, "ST": -5, "BO": 0xAAAA5555, "NA": -0.5, "NB": -1234, "NC": 231.4, "IT": 104500}
    for tid, (fam, _, timed) in A.LAYOUT.items():
        raw = A.monitor(tid, A.SPONTANEOUS, 7, [(1001, samples[fam], 0, t), (70000, samples[fam], 0, t)])
        a = A.parse(raw)
        assert (a.type_id, a.cot, a.ca, len(a.objects)) == (tid, 3, 7, 2), A.TYPES[tid]
        o = a.objects[1]
        assert o.ioa == 70000 and o.value == samples[fam] and o.family == fam, A.TYPES[tid]
        assert o.time == (t if timed else None) and not o.time_invalid
    seq = A.header(1, 3, A.INROGEN, 1, sq=True) + A.ioa_bytes(300) + bytes([1, 0, 0x81])          # SQ = 1
    assert [(o.ioa, o.value, o.qual) for o in A.parse(seq).objects] == [(300, True, 0), (301, False, 0), (302, True, 0x80)]
    st = A.parse(A.monitor(5, 3, 1, [(1, 63, 0, None, False, True)])).objects[0]
    assert st.value == 63 and st.transient
    # CP56Time2a in the station's local time, with summer time and the repeated hour
    berlin = ZoneInfo("Europe/Berlin")
    for when in (datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
                 datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)):
        b = A.cp56(when, berlin)
        assert A.parse_cp56(b, berlin) == (when, False)
    assert A.cp56(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), berlin)[3] == 12 | 0x80     # 12:00 CEST, SU
    assert A.parse_cp56(A.cp56(t, invalid=True), timezone.utc) == (t, True)
    assert A.parse_cp56(bytes([0, 0, 0, 0, 0, 0, 26]), timezone.utc) == (None, True)          # day 0: invalid
    q = A.quality
    assert q("NC", 0) == (192, []) and q("NC", A.IV | A.NT) == (0, ["INVALID", "NOT_TOPICAL"])
    assert q("NC", A.NT)[0] == 68 and q("NC", A.OV) == (84, ["OVERFLOW"]) and q("NC", A.BL)[0] == 64
    assert q("NC", A.SB) == (216, ["SUBSTITUTED"]) and q("SP", 0x01) == (192, []) and q("NA", None) == (192, [])
    assert q("IT", 0x05) == (192, ["SEQ:5"]) and q("IT", A.BCR_CA | A.BCR_CY | 2) == (64, ["ADJUSTED", "CARRY", "SEQ:2"])
    assert q("IT", A.BCR_IV)[0] == 0 and q("ST", 0, True) == (192, ["TRANSIENT"])


def test_only_allowed_requests_exist_and_guard_blocks_others():
    built = {A.interrogation(1)[0], A.counter_interrogation(1)[0], A.read_command(1, 5)[0]}
    assert built == {100, 101, 102} and A.counter_interrogation(1)[9] == 5                      # QCC: RQT 5, FRZ 0
    with pytest.raises(A.AsduError):
        A.counter_interrogation(1, rqt=0x45)
    assert ALLOW["iec104"] == ALLOWED
    for name in ("command", "set_point", "setpoint", "clock", "sync", "reset", "parameter", "file"):
        assert not any(name in n.lower() for n in dir(A) if callable(getattr(A, n)) and not n.startswith("_")
                       and n not in ("interrogation", "counter_interrogation", "read_command")), name
    blocked = []
    c = Iec104Client("127.0.0.1", 1, ReadOnlyGuard("iec104", ALLOW["iec104"], on_block=blocked.append))
    freeze = bytearray(A.counter_interrogation(1))
    freeze[9] |= 0x40                                                                             # FRZ 1: freeze
    for asdu in (A.header(45, 1, A.ACT, 1) + A.ioa_bytes(5000) + b"\x01",                         # C_SC_NA_1
                 A.header(50, 1, A.ACT, 1) + A.ioa_bytes(6000) + bytes(5),                        # C_SE_NC_1
                 A.header(103, 1, A.ACT, 1) + A.ioa_bytes(0) + A.cp56(datetime.now(timezone.utc)),  # C_CS_NA_1
                 A.header(105, 1, A.ACT, 1) + A.ioa_bytes(0) + b"\x01",                           # C_RP_NA_1
                 bytes(freeze)):
        with pytest.raises(ConnectorError) as e:
            asyncio.run(c.send_asdu(asdu))
        assert e.value.code == "OP_FORBIDDEN"
    assert blocked == ["TYPE_45", "TYPE_50", "TYPE_103", "TYPE_105", "C_CI_NA_1:FREEZE"]


# ===================================================================== end to end with the test station
def substation(**kw) -> Iec104Station:
    st = Iec104Station(cas=(1, 2), **kw)
    for i, v in enumerate((132.4, 131.9, 133.1)):
        st.add(1, 1001 + i, "M_ME_NC_1", v)                   # busbar voltages, kV
    st.add(1, 1010, "M_ME_NB_1", 452)                         # transformer load, scaled
    st.add(1, 1020, "M_ME_NA_1", 0.25)                        # normalised
    st.add(1, 2001, "M_DP_NA_1", 2)                           # circuit breaker: on
    st.add(1, 2101, "M_SP_NA_1", False)                       # protection trip
    st.add(1, 2201, "M_ST_NA_1", 9)                           # tap position
    st.add(1, 3001, "M_IT_NA_1", 104500)                      # energy counter
    st.add(2, 1001, "M_ME_NC_1", 66.2)                        # second common address
    return st


def cfg(ports, rev=1, acq=None, points=None, conn=None):
    pts = points or [
        {"tag_id": "ss-cairo.t1.kv-a", "address": {"ca": 1, "ioa": 1001, "type": "M_ME_NC_1"}, "data_type": "float", "unit": "kV"},
        {"tag_id": "ss-cairo.t1.kv-b", "address": {"ca": 1, "ioa": 1002}, "data_type": "float", "unit": "kV"},
        {"tag_id": "ss-cairo.t1.load", "address": {"ca": 1, "ioa": 1010}, "data_type": "float",
         "scaling": {"gain": 0.1}},
        {"tag_id": "ss-cairo.t1.norm", "address": {"ca": 1, "ioa": 1020}, "data_type": "float"},
        {"tag_id": "ss-cairo.t1.cb", "address": {"ca": 1, "ioa": 2001, "type": "M_DP_NA_1"}, "data_type": "enum"},
        {"tag_id": "ss-cairo.t1.trip", "address": {"ca": 1, "ioa": 2101}, "data_type": "bool"},
        {"tag_id": "ss-cairo.t1.tap", "address": {"ca": 1, "ioa": 2201}, "data_type": "int"},
        {"tag_id": "ss-cairo.t1.kwh", "address": {"ca": 1, "ioa": 3001, "type": "M_IT_NA_1"}, "data_type": "int"},
        {"tag_id": "ss-cairo.l2.kv", "address": {"ca": 2, "ioa": 1001}, "data_type": "float"},
        {"tag_id": "ss-cairo.t1.missing", "address": {"ca": 1, "ioa": 9999}, "data_type": "float"},
        {"tag_id": "ss-cairo.t1.wrong", "address": {"ca": 1, "ioa": 1003, "type": "M_SP_NA_1"}, "data_type": "bool"},
    ]
    ports = ports if isinstance(ports, list) else [ports]
    return {"spec": "1.0", "revision": rev, "site": SITE, "source_id": "src-test", "type": "iec104",
            "name": "Substation gateway", "enabled": True,
            "connection": {"hosts": [{"host": "127.0.0.1", "port": p} for p in ports], "common_addresses": [1, 2],
                           "t0_s": 2, "t1_s": 2, "t2_s": 1, "t3_s": 3, **(conn or {})},
            "acquisition": {"batch_max_ms": 100, "gi_on_connect": True, "gi_period_s": 0,
                            "counter_interrogation_period_s": 1, **(acq or {})},
            "points": pts}


def test_end_to_end_interrogation_spontaneous_quality_restart_link_loss_and_schemas():
    async def run():
        st = substation()
        await st.start()
        async with Harness(Iec104Connector(), site=SITE) as h:
            h.config(cfg(st.port))
            await h.wait_for(lambda: h.rt.state == "running", what="running")
            await h.wait_for(lambda: len(h.mq.latest()) == 11, what="11 tags")
            v = h.mq.latest()
            assert v["ss-cairo.t1.kv-a"]["v"] == pytest.approx(132.4) and v["ss-cairo.t1.kv-a"]["q"] == 192
            assert v["ss-cairo.t1.load"]["v"] == pytest.approx(45.2) and v["ss-cairo.t1.norm"]["v"] == 0.25
            assert v["ss-cairo.t1.cb"]["v"] == 2 and v["ss-cairo.t1.trip"]["v"] is False and v["ss-cairo.t1.tap"]["v"] == 9
            assert v["ss-cairo.t1.kwh"]["v"] == 104500 and v["ss-cairo.t1.kwh"]["f"] == ["SEQ:0"]
            assert v["ss-cairo.l2.kv"]["v"] == pytest.approx(66.2)
            assert v["ss-cairo.t1.missing"]["q"] == 4 and v["ss-cairo.t1.missing"]["f"] == ["NOT_IN_STATION"]
            assert v["ss-cairo.t1.wrong"]["q"] == 4 and v["ss-cairo.t1.wrong"]["f"] == ["TYPE_MISMATCH"]

            # spontaneous changes with the station's time tags, in order, all of them
            t1 = datetime(2026, 11, 28, 9, 30, 0, 100000, tzinfo=timezone.utc)
            t2 = datetime(2026, 11, 28, 9, 30, 0, 350000, tzinfo=timezone.utc)
            st.set(1, 2101, True, t=t1)
            st.set(1, 2101, False, t=t2)
            st.set(1, 2001, 1, t=t2)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.cb"]["v"] == 1, what="breaker open")
            trips = [x for b in h.mq.msgs("data") for x in b["values"] if x["t"] == "ss-cairo.t1.trip"]
            assert [(x["v"], x.get("ts")) for x in trips[-2:]] == [(True, "2026-11-28T09:30:00.100Z"),
                                                                     (False, "2026-11-28T09:30:00.350Z")]
            st.set(1, 1001, 140.5, qual=A.OV)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.kv-a"]["v"] == pytest.approx(140.5), what="overflow")
            assert h.mq.latest()["ss-cairo.t1.kv-a"]["q"] == 84 and h.mq.latest()["ss-cairo.t1.kv-a"]["f"] == ["OVERFLOW"]
            st.set(1, 1002, 130.0, qual=A.SB)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.kv-b"]["q"] == 216, what="substituted")
            st.set(1, 1002, qual=A.IV | A.NT)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.kv-b"]["q"] == 0, what="invalid")
            st.set(1, 2201, 10, qual=0, transient=True, time_invalid=True)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.tap"]["v"] == 10, what="tap change")
            assert set(h.mq.latest()["ss-cairo.t1.tap"]["f"]) == {"TRANSIENT", "CLOCK"}
            st.set(1, 1020, 0.5, test=True)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.norm"]["v"] == 0.5, what="test frame")
            assert h.mq.latest()["ss-cairo.t1.norm"]["q"] == 64 and "TEST" in h.mq.latest()["ss-cairo.t1.norm"]["f"]
            st.db[(1, 3001)].value = 104600                            # counters: counter interrogation every 1 s
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.kwh"]["v"] == 104600, what="counter interrogation")

            # admin requests reuse the running connection (one connection only)
            res = (await h.admin("read_now", {"tag_ids": ["ss-cairo.t1.kv-b", "ss-cairo.t1.missing"],
                                              "addresses": [{"address": {"ca": 1, "ioa": 2001}, "data_type": "enum"}]}))["result"]
            assert [(x["v"], x["q"]) for x in res["values"]] == [(None, 0), (None, 4), (1, 192)]
            b = (await h.admin("browse", {}))["result"]["items"]
            kwh = next(i for i in b if i["path"] == "1/3001")
            assert kwh["address"] == {"ca": 1, "ioa": 3001, "type": "M_IT_NA_1"} and kwh["data_type"] == "int"
            assert [i["name"] for i in b if i["kind"] == "object"][:2] == ["CA 1 · Single points", "CA 1 · Double points"]
            only = (await h.admin("browse", {"path": "2"}))["result"]["items"]
            assert [i["path"] for i in only] == ["2/NC", "2/1001"]
            t = (await h.admin("test_connection", {}))["result"]["devices"][0]
            assert t["ok"] and t["info"]["objects"]["Short float"] == 4 and t["info"]["common_addresses"] == [1, 2]
            assert st.connections == 1
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["not_in_station"] == ["ss-cairo.t1.missing"] and diag["spontaneous"] >= 8
            assert diag["link"]["unacknowledged"] <= 12

            # the station restarts: end of initialisation -> event and a new general interrogation
            gis = st.requests.count("C_IC_NA_1")
            st.restart(1)
            await h.wait_for(lambda: st.requests.count("C_IC_NA_1") >= gis + 2, what="GI after restart")
            assert any(e["type"] == "device.restart" for e in h.mq.msgs("event"))

            # link loss: q 24, then reconnect and fresh values
            st.kick_all()
            await h.wait_for(lambda: all(x["q"] == 24 for t_, x in h.mq.latest().items()
                                         if t_ not in ("ss-cairo.t1.missing", "ss-cairo.t1.wrong")), what="q 24 on link loss")
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="reconnected")
            st.set(1, 1001, 129.0)
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.kv-a"]["v"] == pytest.approx(129.0), timeout=8,
                             what="after reconnect")
            assert h.schema_errors() == []
        await st.stop()
        assert set(st.requests) <= ALLOWED, set(st.requests)
        assert "STOPDT_ACT" in st.requests
    asyncio.run(run())


def test_flow_control_many_objects():
    """600 information objects, one per ASDU: the station stops after k = 12 unacknowledged I-frames, so the GI only
    completes when the master acknowledges every w = 8 frames."""
    async def run():
        st = Iec104Station(cas=(1,), objects_per_asdu=1)
        for i in range(600):
            st.add(1, 10000 + i, "M_ME_NC_1", float(i))
        await st.start()
        c = Iec104Client("127.0.0.1", st.port, ReadOnlyGuard("iec104", ALLOW["iec104"]), t1=2, t2=1, t3=5)
        await c.connect()
        data = await c.interrogate(1, timeout=10)
        assert sorted(o.value for a in data for o in a.objects) == [float(i) for i in range(600)]
        assert c.stats["s_tx"] >= 600 // 8 - 1 and c.vr == 602            # con + 600 + term
        await c.close()
        await st.stop()
        assert set(st.requests) == {"STARTDT_ACT", "C_IC_NA_1", "S", "STOPDT_ACT"}
    asyncio.run(run())


def test_redundant_host_silent_station_and_validation():
    async def run():
        st = substation()
        await st.start()
        async with Harness(Iec104Connector(), site=SITE) as h:
            h.config(cfg([free_port(), st.port], acq={"counter_interrogation_period_s": 0}))
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="running on the second host")
            assert any(e["type"] == "conn.failover" for e in h.mq.msgs("event"))
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["host"] == f"127.0.0.1:{st.port}" and diag["failovers"] == 1
            # the station stops answering with TCP open: TESTFR after t3, no answer within t1 -> reconnect
            st.respond = False
            await h.wait_for(lambda: h.rt.state != "running", timeout=10, what="TESTFR timeout")
            st.respond = True
            await h.wait_for(lambda: h.rt.state == "running", timeout=12, what="back")
            assert "TESTFR_ACT" in st.requests and h.schema_errors() == []
        await st.stop()
        bad = cfg(1, points=[
            {"tag_id": "ss-cairo.x", "address": {"ca": 3, "ioa": 1, "type": "M_SP_NA_1"}, "data_type": "float"},
            {"tag_id": "ss-cairo.y", "address": {"ca": 3, "ioa": 1}, "data_type": "bool"}],
            conn={"w": 20, "device_time_zone": "Mars/Olympus"})
        errs = await Iec104Connector().validate(bad)
        assert any("not in connection/common_addresses" in e for e in errs) and any("cannot be 'float'" in e for e in errs)
        assert any("already tag" in e for e in errs) and any("w must not be larger" in e for e in errs)
        assert any("IANA time zone" in e for e in errs)
    asyncio.run(run())


def test_test_connection_unknown_ca_time_zone_and_tls():
    x509 = pytest.importorskip("cryptography.x509")
    sys.path.insert(0, str(ROOT / "connectors/mqtt/tests"))
    from test_mqtt import _test_pki

    async def run():
        tmp = pathlib.Path(tempfile.mkdtemp())
        ca_pem, other_pem = _test_pki(tmp)
        sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        sctx.load_cert_chain(tmp / "srv.pem", tmp / "srv.key")
        cairo = ZoneInfo("Africa/Cairo")
        st = substation(ssl=sctx, tz=cairo, read_support=False)
        await st.start()
        c = Iec104Connector()
        c.secrets_dir = tmp
        (tmp / "ca").write_text(ca_pem)
        (tmp / "other-ca").write_text(other_pem)
        tls = cfg(st.port, conn={"tls": {"enabled": True, "ca_cert_ref": "ca"}, "device_time_zone": "Africa/Cairo"})
        res = (await c.test_connection(tls))["devices"][0]
        assert res["ok"] and res["info"]["tls"] and res["info"]["objects"] == {
            "Short float": 4, "Scaled value": 1, "Normalised value": 1, "Double point": 1, "Single point": 1,
            "Step position": 1}
        wrong = cfg(st.port, conn={"tls": {"enabled": True, "ca_cert_ref": "other-ca"}})
        assert (await c.test_connection(wrong))["devices"][0]["error"]["code"] == "TLS_FAILED"
        unknown = cfg(st.port, conn={"tls": {"enabled": True, "ca_cert_ref": "ca"}, "common_addresses": [1, 7]})
        err = (await c.test_connection(unknown))["devices"][0]["error"]
        assert err["code"] == "ADDR_INVALID" and "7" in err["message"]
        b = (await c.browse({"config": unknown}))["items"]
        assert b[-1]["name"] == "CA 7 · unknown to the station"
        await st.stop()
        # station clock in Cairo local time: converted to UTC
        async with Harness(Iec104Connector(), site=SITE, secrets={"ca": ca_pem}) as h:
            st2 = substation(ssl=sctx, tz=cairo, read_support=False)
            await st2.start()
            h.config(cfg(st2.port, conn={"tls": {"enabled": True, "ca_cert_ref": "ca"}, "device_time_zone": "Africa/Cairo"}))
            await h.wait_for(lambda: h.mq.latest().get("ss-cairo.t1.trip", {}).get("v") is False, what="values over TLS")
            st2.set(1, 2101, True, t=datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc))
            await h.wait_for(lambda: h.mq.latest()["ss-cairo.t1.trip"]["v"] is True, what="trip")
            assert h.mq.latest()["ss-cairo.t1.trip"]["ts"] == "2026-07-01T09:00:00.000Z"
            res = (await h.admin("read_now", {"tag_ids": ["ss-cairo.t1.kv-a"]}))["result"]
            assert res["values"][0]["q"] == 4                          # read command not supported by this station
            await st2.stop()
        res = (await c.test_connection(cfg(free_port())))["devices"][0]
        assert res["error"]["code"] in ("CONN_REFUSED", "CONN_TIMEOUT")
    assert x509
    asyncio.run(run())


# ===================================================================== independent station (CI: iec104-interop)
def test_interop_with_lib60870_example_server():
    server = os.environ.get("OPCARE_IEC104_INTEROP_SERVER")
    if not server:
        pytest.skip("OPCARE_IEC104_INTEROP_SERVER not set (the CI interop job builds lib60870's cs104_server)")

    async def run():
        proc = subprocess.Popen([server], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 2404), 0.2).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            pts = [{"tag_id": "ss-cairo.lib.sva100", "address": {"ca": 1, "ioa": 100, "type": "M_ME_NB_1"}, "data_type": "int"},
                   {"tag_id": "ss-cairo.lib.sva102", "address": {"ca": 1, "ioa": 102}, "data_type": "float"},
                   {"tag_id": "ss-cairo.lib.sp104", "address": {"ca": 1, "ioa": 104}, "data_type": "bool"},
                   {"tag_id": "ss-cairo.lib.sp307", "address": {"ca": 1, "ioa": 307}, "data_type": "bool"},
                   {"tag_id": "ss-cairo.lib.bs500", "address": {"ca": 1, "ioa": 500, "type": "M_BO_NA_1"}, "data_type": "int"},
                   {"tag_id": "ss-cairo.lib.periodic110", "address": {"ca": 1, "ioa": 110}, "data_type": "int"}]
            conf = cfg(2404, points=pts, conn={"common_addresses": [1], "t1_s": 15, "t2_s": 10, "t3_s": 20},
                       acq={"counter_interrogation_period_s": 30})
            async with Harness(Iec104Connector(), site=SITE) as h:
                h.config(conf)
                await h.wait_for(lambda: len(h.mq.latest()) == 6, timeout=10, what="6 tags from lib60870")
                v = h.mq.latest()
                assert v["ss-cairo.lib.sva100"]["v"] == -1 and v["ss-cairo.lib.sva102"]["v"] == 2300.0
                assert v["ss-cairo.lib.sp104"]["v"] is True and v["ss-cairo.lib.sp307"]["v"] is False
                assert v["ss-cairo.lib.bs500"]["v"] == 0xAAAA
                # IOA 110 is periodic only (cause 1), not part of the interrogation: "not in station" until it comes
                await h.wait_for(lambda: h.mq.latest()["ss-cairo.lib.periodic110"]["v"] is not None, timeout=5,
                                 what="first periodic value")
                first = h.mq.latest()["ss-cairo.lib.periodic110"]["v"]
                await h.wait_for(lambda: h.mq.latest()["ss-cairo.lib.periodic110"]["v"] > first, timeout=5,
                                 what="periodic value increases")
                # this example station supports neither counter interrogation nor the read command
                assert any(e["type"] == "config.rejected" and e.get("attrs", {}).get("request") == "C_CI_NA_1"
                           for e in h.mq.msgs("event"))
                res = (await h.admin("read_now", {"tag_ids": ["ss-cairo.lib.sva100"]}))["result"]
                assert res["values"][0]["q"] == 4
                t = (await h.admin("test_connection", {}))["result"]["devices"][0]
                assert t["ok"] and t["info"]["objects"] == {"Scaled value": 3, "Single point": 10, "Bitstring 32": 1}
                b = (await h.admin("browse", {}))["result"]["items"]
                assert {"1/100", "1/307", "1/500"} <= {i["path"] for i in b}
                assert h.rt.state == "running" and h.schema_errors() == []
        finally:
            proc.terminate()
            proc.wait(10)
    asyncio.run(run())
