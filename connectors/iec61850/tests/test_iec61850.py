"""IEC 61850 connector tests: BER / ISO / MMS unit tests, and end-to-end runs against tools/simulators/iec61850_server.py
(our stack in the server role, model from tools/simulators/scl/feeder_relay.icd) over real TCP and TLS, with every
published message checked against the schemas. With OPCARE_IEC61850_INTEROP_CP set to the classpath printed by
tools/interop/build_iec61850bean.sh, the connector also runs against IEC61850bean (Apache-2.0; CI job
"iec61850-interop"; never part of the product).

Run from the repository root:  python -m pytest connectors/iec61850/tests -q
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
for p in ("connectors/sdk-python", "connectors/iec61850", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk import ALLOW, ConnectorError, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402
from iec61850_connector import Iec61850Connector  # noqa: E402
from iec61850_connector import ber as B  # noqa: E402
from iec61850_connector import iso as I  # noqa: E402
from iec61850_connector import mms as M  # noqa: E402
from iec61850_connector import model as X  # noqa: E402
from iec61850_connector.client import MmsClient  # noqa: E402
from iec61850_server import Iec61850Server  # noqa: E402

SCL = ROOT / "tools/simulators/scl/feeder_relay.icd"
SITE = "ss-giza"
LD = "FDR21LD0"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ===================================================================== unit tests
def test_ber_tags_lengths_oids_and_bitstrings():
    for n in (0, 30, 31, 127, 128, 16383):
        t, _ = B.parse(B.tlv(B.CONTEXT, True, n, b"\x01\x02"))
        assert (t.cls, t.constructed, t.number, t.value) == (B.CONTEXT, True, n, b"\x01\x02")
    big = b"x" * 70000
    assert B.parse(B.tlv(B.UNIVERSAL, False, 4, big))[0].value == big
    assert B.oid("1.0.9506.2.3")[2:] == bytes.fromhex("28ca220203")
    assert B.oid("2.2.3.1")[2:] == bytes.fromhex("520301")
    bits = [True, False, True] + [False] * 9 + [True]
    assert B.bits_of(B.bitstring_content(bits)) == bits and B.bitstring_content(bits)[0] == 3
    for v in (0, 1, 127, 128, -1, -129, 2 ** 31):
        assert B.parse(B.integer(v))[0].int() == v
    with pytest.raises(B.BerError):
        B.parse(b"\x30\x05\x01")


def test_iso_layers_and_acse_round_trip():
    ini = M.initiate_request()
    aarq = I.parse_acse(I.user_pdv(I.parse_spdu(I.session_connect(I.presentation_connect(I.aarq(ini, "s3cret"))))[1])[1])
    assert aarq["kind"] == "AARQ" and aarq["password"] == "s3cret" and aarq["mms"] == ini
    aare = I.parse_acse(I.user_pdv(I.parse_spdu(I.session_accept(I.presentation_accept(I.aare(M.initiate_response()))))[1])[1])
    assert aare["kind"] == "AARE" and aare["result"] == 0 and M.parse_pdu(aare["mms"]).kind == "initiate-response"
    refused = I.parse_acse(I.user_pdv(I.parse_spdu(I.session_accept(I.presentation_accept(I.aare(b"", False, 1))))[1])[1])
    assert refused["result"] == 1 and refused["mms"] is None
    code, user = I.parse_spdu(I.session_data(I.presentation_data(b"\xa0\x03\x02\x01\x07")))
    assert code == I.SPDU_DATA and I.user_pdv(user) == (I.CTX_MMS, b"\xa0\x03\x02\x01\x07")
    dts = I.cotp_dt(b"z" * 5000)
    assert len(dts) == 3 and dts[-1][6] == 0x80 and dts[0][6] == 0


def test_mms_values_types_and_reports():
    t = datetime(2026, 11, 28, 10, 0, 1, 500000, tzinfo=timezone.utc)
    values = [True, 42, -7, 3.25, "Opcare", b"\x01\x02", M.BitString([False, True]), M.UtcTime(t, True, False, True, 10),
              [1.5, [True, M.BitString([True] * 13)]]]
    for v in values:
        assert M.decode_data(B.parse(M.encode_data(v))[0]) == v
    assert M.decode_data(B.parse(M.encode_data(7, M.TypeSpec("unsigned")))[0]) == 7
    spec = M.TypeSpec("structure", components=[("mag", M.TypeSpec("structure", components=[("f", M.TypeSpec("floating-point", 32))])),
                                               ("q", M.TypeSpec("bit-string", 13)), ("t", M.TypeSpec("utc-time")),
                                               ("arr", M.TypeSpec("array", count=3, element=M.TypeSpec("integer", 32)))])
    back = M.decode_type(B.parse(M.encode_type(spec))[0])
    assert [n for n, _ in back.components] == ["mag", "q", "t", "arr"] and back.component("q").size == 13
    assert back.component("arr").count == 3 and back.component("mag").component("f").kind == "floating-point"
    flat = X.flatten([[230.5], M.BitString([False] * 13), M.UtcTime(t), [1, 2, 3]], back, ("TotW",))
    assert flat[("TotW", "mag", "f")] == 230.5 and flat[("TotW", "arr", "2")] == 3
    # an InformationReport as IEC 61850-8-1 lays it out
    opt = X.bits(["seqNum", "timeStamp", "dataSet", "entryID", "confRev", "reasonCode"], X.OPT, 10)
    rep = X.parse_report(["FDR21LD0/LLN0.brcbEV", opt, 5, t, "FDR21LD0/LLN0$Events", b"\x00" * 7 + b"\x09", 1,
                          M.BitString([False, True, True]), [M.BitString([True, False])], [True], M.BitString([False, True] + [False] * 5),
                          M.BitString([False, False, False, False, False, True, False])], None)
    assert (rep.seq, rep.dataset, rep.entry_id[-1], rep.conf_rev) == (5, "FDR21LD0/LLN0$Events", 9, 1)
    assert [(i, why) for i, _, why in rep.entries] == [(1, ["dchg"]), (2, ["gi"])]


def test_quality_timestamps_and_references():
    q = lambda *names: M.BitString([n in names for n in ("v0", "v1", "overflow", "outOfRange", "badReference", "oscillatory", "failure",  # noqa: E731
                                                         "oldData", "inconsistent", "inaccurate", "substituted", "test", "operatorBlocked")])
    assert X.quality(q()) == (192, [])
    assert X.quality(q("v1"))[0] == 0 and X.quality(q("v1", "failure"))[0] == 12
    assert X.quality(q("v0", "v1", "oldData")) == (68, ["questionable", "oldData"])
    assert X.quality(q("v0", "v1", "inaccurate"))[0] == 80 and X.quality(q("v0", "v1", "outOfRange"))[0] == 84
    assert X.quality(q("v0", "v1"))[0] == 64 and X.quality(q("test"))[0] == 64 and X.quality(q("operatorBlocked"))[0] == 64
    assert X.quality(q("substituted")) == (216, ["substituted"])
    t = datetime(2026, 11, 28, 10, 0, 1, 500000, tzinfo=timezone.utc)
    assert X.timestamp(M.UtcTime(t)) == ("2026-11-28T10:00:01.500Z", [])
    assert X.timestamp(M.UtcTime(t, not_synchronized=True)) == (None, ["CLOCK"])
    r = X.parse_ref("FDR21LD0/MMXU1.A.phsA.cVal.mag.f", "MX")
    assert (r.ld, r.item, r.object_ref) == (LD, "MMXU1$MX$A$phsA$cVal$mag$f", "FDR21LD0/MMXU1.A.phsA.cVal.mag.f")
    assert X.parse_rcb("FDR21LD0/LLN0.BR.brcbEV") == (LD, "LLN0$BR$brcbEV")
    with pytest.raises(X.RefError):
        X.parse_ref("noslash.TotW", "MX")
    assert X.coerce(M.BitString([True, False]), "enum") == 2 and X.coerce(M.BitString([False, True]), "string") == "off"


def test_guard_allows_only_reporting_setup_writes():
    assert {"WRITE_RCB:RptEna", "WRITE_RCB:GI", "WRITE_RCB:EntryID", "Read"} <= ALLOW["iec61850"]
    blocked = []
    c = MmsClient("127.0.0.1", 1, ReadOnlyGuard("iec61850", ALLOW["iec61850"], on_block=blocked.append))
    for item in ("XCBR1$CO$Pos$Oper", "LLN0$BR$brcbEV$PurgeBuf", "LLN0$SP$Mod$stVal", "MMXU1$MX$TotW$mag$f", "LLN0$BR$brcbEV$DatSet"):
        with pytest.raises(ConnectorError) as e:
            asyncio.run(c.write_rcb(LD, item, M.encode_data(True)))
        assert e.value.code == "OP_FORBIDDEN"
    assert blocked == ["Write:XCBR1$CO$Pos$Oper", "WRITE_RCB:PurgeBuf", "Write:LLN0$SP$Mod$stVal",
                       "Write:MMXU1$MX$TotW$mag$f", "WRITE_RCB:DatSet"]
    for name in ("select", "operate", "cancel", "define", "delete", "file", "setting"):
        assert not any(name in n.lower() for n in dir(M) if callable(getattr(M, n)) and not n.startswith("_"))


# ===================================================================== end to end with the test server
def pt(tag, ref, fc, dt):
    return {"tag_id": f"{SITE}.fdr21.{tag}", "address": {"object_ref": f"{LD}/{ref}", "fc": fc}, "data_type": dt}


POINTS = [pt("p", "MMXU1.TotW.mag.f", "MX", "float"), pt("q", "MMXU1.TotVAr.mag.f", "MX", "float"),
          pt("hz", "MMXU1.Hz.mag.f", "MX", "float"), pt("ia", "MMXU1.A.phsA.cVal.mag.f", "MX", "float"),
          pt("cb", "XCBR1.Pos.stVal", "ST", "enum"), pt("trip", "PTOC1.Op.general", "ST", "bool"),
          pt("ops", "XCBR1.OpCnt.stVal", "ST", "int"), pt("vendor", "LLN0.NamPlt.vendor", "DC", "string"),
          pt("missing", "MMXU1.Nope.mag.f", "MX", "float")]
REPORTS = [{"rcb_ref": f"{LD}/LLN0.BR.brcbEV", "buffered": True, "trigger_options": ["dchg", "qchg", "gi"]},
           {"rcb_ref": f"{LD}/LLN0.RP.urcbMX", "buffered": False, "trigger_options": ["dchg", "qchg", "gi"]}]


def cfg(port: int, conn=None, acq=None, points=None):
    return {"spec": "1.0", "revision": 1, "site": SITE, "source_id": "src-test", "type": "iec61850", "name": "FDR21",
            "enabled": True, "connection": {"host": "127.0.0.1", "port": port, "ied_name": "FDR21", "timeout_ms": 3000, **(conn or {})},
            "acquisition": {"batch_max_ms": 100, "integrity_s": 0, "default_scan_ms": 300, "reports": REPORTS, **(acq or {})},
            "points": points or POINTS}


ALLOWED_SEEN = {"Initiate", "Conclude", "GetNameList", "GetVariableAccessAttributes", "GetNamedVariableListAttributes", "Read"} | \
    {f"WRITE_RCB:{a}" for a in ("Resv", "ResvTms", "RptEna", "GI", "TrgOps", "IntgPd", "BufTm", "OptFlds", "EntryID")}


def test_end_to_end_reports_polling_quality_time_resync_and_schemas():
    async def run():
        srv = Iec61850Server(SCL)
        srv.set(f"{LD}/MMXU1.TotW.mag.f", "MX", 3.2)
        srv.set(f"{LD}/XCBR1.Pos.stVal", "ST", [True, False])               # on
        await srv.start()
        async with Harness(Iec61850Connector(), site=SITE) as h:
            h.config(cfg(srv.port))
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="running")
            await h.wait_for(lambda: len(h.mq.latest()) == 9, what="9 tags")
            v = h.mq.latest()
            assert v[f"{SITE}.fdr21.p"]["v"] == pytest.approx(3.2) and v[f"{SITE}.fdr21.cb"]["v"] == 2
            assert v[f"{SITE}.fdr21.vendor"]["v"] == "Opcare" and v[f"{SITE}.fdr21.trip"]["v"] is False
            assert v[f"{SITE}.fdr21.missing"]["q"] == 4 and v[f"{SITE}.fdr21.missing"]["f"] == ["NOT_IN_IED"]
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["reports"] == {f"{LD}/LLN0.BR.brcbEV": "enabled", f"{LD}/LLN0.RP.urcbMX": "enabled"}
            assert diag["polled_objects"] == 3                                  # A.phsA, OpCnt, NamPlt: no report carries them

            # a trip: reported by the buffered RCB with the relay's own time stamp
            t1 = datetime(2026, 11, 28, 9, 30, 0, 125000, tzinfo=timezone.utc)
            reads = srv.requests.count("Read")
            srv.set(f"{LD}/PTOC1.Op.general", "ST", True, t=t1)
            srv.set(f"{LD}/XCBR1.Pos.stVal", "ST", [False, True], t=t1)          # off
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.cb"]["v"] == 1, what="breaker open by report")
            trip = h.mq.latest()[f"{SITE}.fdr21.trip"]
            assert trip["v"] is True and trip["ts"] == "2026-11-28T09:30:00.125Z"
            # measurements by the unbuffered RCB; quality and clock flags
            srv.set(f"{LD}/MMXU1.TotW.mag.f", "MX", 3.55, quality=["questionable", "oldData"])
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.p"]["v"] == pytest.approx(3.55), what="TotW by report")
            assert h.mq.latest()[f"{SITE}.fdr21.p"]["q"] == 68 and "oldData" in h.mq.latest()[f"{SITE}.fdr21.p"]["f"]
            srv.set(f"{LD}/MMXU1.Hz.mag.f", "MX", 50.02, clock_bad=True)
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.hz"]["v"] == pytest.approx(50.02), what="Hz")
            assert h.mq.latest()[f"{SITE}.fdr21.hz"]["f"] == ["CLOCK"]
            srv.set(f"{LD}/MMXU1.TotVAr.mag.f", "MX", 1.0, quality=["invalid"])
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.q"]["q"] == 0, what="invalid quality")
            # polled values
            srv.set(f"{LD}/MMXU1.A.phsA.cVal.mag.f", "MX", 181.5)
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.ia"]["v"] == pytest.approx(181.5), what="polled current")

            # admin: browse levels, read_now, test_connection on the running association
            lds = (await h.admin("browse", {"path": ""}))["result"]["items"]
            assert [i["path"] for i in lds] == [LD]
            lns = [i["name"] for i in (await h.admin("browse", {"path": LD}))["result"]["items"]]
            assert lns[0] == "LLN0" and {"MMXU1", "XCBR1", "PTOC1", "LPHD1"} <= set(lns)
            lln0 = (await h.admin("browse", {"path": f"{LD}/LLN0"}))["result"]["items"]
            rcb = next(i for i in lln0 if i["kind"] == "report" and i["name"] == "brcbEV")
            assert rcb["report"] == {"rcb_ref": f"{LD}/LLN0.BR.brcbEV", "buffered": True}
            assert rcb["info"]["members"] == [f"{LD}/XCBR1.ST.Pos", f"{LD}/PTOC1.ST.Op", f"{LD}/PTOC1.ST.Str"]
            dos = (await h.admin("browse", {"path": f"{LD}/MMXU1.A"}))["result"]["items"]
            ia = next(i for i in dos if i["name"] == "phsA.cVal.mag.f")
            assert ia["address"] == {"object_ref": f"{LD}/MMXU1.A.phsA.cVal.mag.f", "fc": "MX"} and ia["data_type"] == "float"
            res = (await h.admin("read_now", {"tag_ids": [f"{SITE}.fdr21.ops"],
                                              "addresses": [{"address": {"object_ref": f"{LD}/LLN0.NamPlt.swRev", "fc": "DC"},
                                                             "data_type": "string"}]}))["result"]["values"]
            assert res[0]["v"] == 0 and res[1]["v"] == "1.0"
            t = (await h.admin("test_connection", {}))["result"]["devices"][0]
            assert t["ok"] and t["info"]["vendor"] == "Opcare" and t["info"]["report_control_blocks"] == 2

            # association lost: q 24; meanwhile the breaker closes; buffered report resynchronises with EntryID
            srv.kick_all()
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.cb"]["q"] == 24, what="q 24 on loss")
            t2 = datetime(2026, 11, 28, 9, 31, 0, tzinfo=timezone.utc)
            srv.set(f"{LD}/XCBR1.Pos.stVal", "ST", [True, False], t=t2)
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="reconnected")
            await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.cb"]["v"] == 2, what="breaker closed after resync")
            assert "WRITE_RCB:EntryID" in srv.requests
            assert h.schema_errors() == []
        await srv.stop()
        assert set(srv.requests) <= ALLOWED_SEEN, set(srv.requests) - ALLOWED_SEEN
    asyncio.run(run())


def test_password_tls_and_test_connection_errors():
    sys.path.insert(0, str(ROOT / "connectors/mqtt/tests"))
    pytest.importorskip("cryptography")
    from test_mqtt import _test_pki

    async def run():
        tmp = pathlib.Path(tempfile.mkdtemp())
        ca_pem, other_pem = _test_pki(tmp)
        sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        sctx.load_cert_chain(tmp / "srv.pem", tmp / "srv.key")
        srv = Iec61850Server(SCL, password="Relay#2026", ssl=sctx)
        await srv.start()
        c = Iec61850Connector()
        c.secrets_dir = tmp
        (tmp / "ca").write_text(ca_pem)
        (tmp / "pw").write_text("Relay#2026")
        (tmp / "bad").write_text("nope")
        good = cfg(srv.port, conn={"tls": {"enabled": True, "ca_cert_ref": "ca"}, "acse_auth": {"password_ref": "pw"}})
        res = (await c.test_connection(good))["devices"][0]
        assert res["ok"] and res["info"]["logical_devices"] == [LD] and res["info"]["datasets"] == 2
        bad = cfg(srv.port, conn={"tls": {"enabled": True, "ca_cert_ref": "ca"}, "acse_auth": {"password_ref": "bad"}})
        assert (await c.test_connection(bad))["devices"][0]["error"]["code"] == "AUTH_FAILED"
        other = cfg(srv.port, conn={"tls": {"enabled": True, "ca_cert_ref": "other-ca"}, "acse_auth": {"password_ref": "pw"}})
        (tmp / "other-ca").write_text(other_pem)
        assert (await c.test_connection(other))["devices"][0]["error"]["code"] == "TLS_FAILED"
        wrong_ied = cfg(srv.port, conn={"ied_name": "BAY07", "tls": {"enabled": True, "ca_cert_ref": "ca"},
                                        "acse_auth": {"password_ref": "pw"}})
        res = (await c.test_connection(wrong_ied))["devices"][0]
        assert res["error"]["code"] == "ADDR_INVALID" and "FDR21LD0" in res["error"]["message"]
        await srv.stop()
        res = (await c.test_connection(cfg(free_port())))["devices"][0]
        assert res["error"]["code"] in ("CONN_REFUSED", "CONN_TIMEOUT")
        errs = await c.validate(cfg(1, acq={"reports": [{"rcb_ref": f"{LD}/LLN0.RP.urcbMX", "buffered": True}]},
                                    points=[pt("x", "MMXU1", "MX", "float")]))
        assert any("buffered" in e for e in errs) and any("not an IEC 61850 reference" in e for e in errs)
    asyncio.run(run())


# ===================================================================== independent server (CI: iec61850-interop)
def test_interop_with_iec61850bean_server():
    cp = os.environ.get("OPCARE_IEC61850_INTEROP_CP")
    if not cp:
        pytest.skip("OPCARE_IEC61850_INTEROP_CP not set (the CI interop job builds IEC61850bean and sets it)")

    async def run():
        port = free_port()
        proc = subprocess.Popen(["java", "-cp", cp, "Iec61850BeanServer", str(SCL), str(port)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        lines: list[str] = []

        def cmd(line: str) -> None:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            while True:
                out = proc.stdout.readline().strip()
                lines.append(out)
                if out == "OK" or out.startswith(("NOTFOUND", "UNKNOWN")) or not out:
                    assert out == "OK", out
                    return
        try:
            while "READY" not in proc.stdout.readline():
                pass
            async with Harness(Iec61850Connector(), site=SITE) as h:
                h.config(cfg(port))
                await h.wait_for(lambda: h.rt.state == "running", timeout=10, what="running")
                await h.wait_for(lambda: len(h.mq.latest()) == 9, timeout=10, what="9 tags")
                assert h.mq.latest()[f"{SITE}.fdr21.vendor"]["v"] == "Opcare"
                await asyncio.to_thread(cmd, f"float {LD}/MMXU1.TotW.mag.f MX 3.75")
                await asyncio.to_thread(cmd, f"dbpos {LD}/XCBR1.Pos.stVal ST 2")
                await asyncio.to_thread(cmd, f"bool {LD}/PTOC1.Op.general ST true")
                await asyncio.to_thread(cmd, f"float {LD}/MMXU1.A.phsA.cVal.mag.f MX 181.5")
                await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.p"]["v"] == pytest.approx(3.75), timeout=10, what="report")
                await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.cb"]["v"] == 2, timeout=10, what="breaker report")
                await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.trip"]["v"] is True, timeout=10, what="trip report")
                await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.ia"]["v"] == pytest.approx(181.5), timeout=10, what="polled")
                await asyncio.to_thread(cmd, f"quality {LD}/MMXU1.Hz.q MX 0100000000000")
                await h.wait_for(lambda: h.mq.latest()[f"{SITE}.fdr21.hz"]["q"] == 0, timeout=10, what="invalid quality")
                states = (await h.admin("get_diagnostics"))["result"]["connector"]["reports"]
                assert states[f"{LD}/LLN0.RP.urcbMX"] == "enabled"
                # IEC61850bean has buffered report control blocks in its model but sends no buffered reports:
                # the connector notices (no report after GI) and polls that dataset instead
                assert "polled" in states[f"{LD}/LLN0.BR.brcbEV"]
                t = (await h.admin("test_connection", {}))["result"]["devices"][0]
                assert t["ok"] and t["info"]["logical_devices"] == [LD]
                assert h.schema_errors() == []
        finally:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
            rest = proc.communicate(timeout=10)[0]
        writes = {ln.split()[1].rsplit(".", 1)[-1] for ln in lines + rest.splitlines() if ln.startswith("WRITE")}
        assert writes <= {"Resv", "ResvTms", "RptEna", "GI", "TrgOps", "IntgPd", "BufTm", "OptFlds", "EntryID"}, writes
    asyncio.run(run())
