"""SNMP connector tests: BER and USM unit tests (RFC test vectors) and end-to-end runs against
tools/simulators/snmp_sim.py over real UDP on localhost, every published message checked against the schemas.

Run from the repository root:  python -m pytest connectors/snmp/tests -q
"""
import asyncio
import pathlib
import socket
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/snmp", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk import ALLOW, ConnectorError, Q, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402
from snmp_connector import SnmpConnector  # noqa: E402
from snmp_connector import ber  # noqa: E402
from snmp_connector.ber import COUNTER64, GAUGE32, INTEGER, OCTET_STRING, SET, Pdu, VarBind  # noqa: E402
from snmp_connector.client import SnmpClient, SnmpError, Target  # noqa: E402
from snmp_connector.messages import (V1, V2C, UsmUser, decode_community, decode_v3, decrypt_v3,  # noqa: E402
                                     encode_community, encode_v3, localize, password_to_key, verify_v3)
from snmp_sim import GEN_ERR, SnmpAgent, standard_mib, ups_mib  # noqa: E402

SITE = "site-a"


# ===================================================================== BER and USM
def test_ber_known_get_request():
    msg = encode_community(V2C, b"public", Pdu(ber.GET, 1, 0, 0, [VarBind("1.3.6.1.2.1.1.1.0")]))
    assert msg.hex() == "3026020101040670756c6963a019020101020100020100300e300c06082b060102010101000500".replace(
        "70756c6963", "7075626c6963")
    version, community, pdu = decode_community(msg)
    assert (version, community, pdu.request_id, pdu.varbinds[0].oid) == (V2C, b"public", 1, "1.3.6.1.2.1.1.1.0")


@pytest.mark.parametrize("tag,value", [(INTEGER, -1), (INTEGER, 128), (INTEGER, -129), (ber.COUNTER32, 4294967295),
                                       (COUNTER64, 18446744073709551615), (ber.TIMETICKS, 0), (OCTET_STRING, b"\x00\xffA"),
                                       (ber.OID, "1.3.6.1.4.1.311.21.13"), (ber.IP_ADDRESS, "10.20.1.31")])
def test_ber_values_round_trip(tag, value):
    enc = ber.enc_value(tag, value)
    t, content, _ = ber.read_tlv(enc)
    assert t == tag and ber.dec_value(t, content) == value


def test_long_lengths_and_large_oid_arcs():
    big = VarBind("2.999.1.4294967295", OCTET_STRING, b"x" * 300)
    pdu = ber.dec_pdu(*ber.read_tlv(Pdu(ber.RESPONSE, 7, 0, 0, [big]).encode())[:2])
    assert pdu.varbinds[0].oid == "2.999.1.4294967295" and len(pdu.varbinds[0].value) == 300


def test_setrequest_can_never_be_encoded_and_guard_allows_reads_only():
    with pytest.raises(ber.BerError):
        Pdu(SET, 1, 0, 0, [VarBind("1.3.6.1.2.1.1.5.0", OCTET_STRING, b"x")]).encode()
    assert ALLOW["snmp"] == {"GetRequest", "GetNextRequest", "GetBulkRequest", "Response(Inform)"}

    async def run():
        guard = ReadOnlyGuard("snmp", ALLOW["snmp"])
        client = SnmpClient(V2C, guard, community=b"public")
        with pytest.raises(ConnectorError) as e:
            await client.request(Target("a", "127.0.0.1", 9), SET, ["1.3.6.1.2.1.1.5.0"])
        assert e.value.code == "OP_FORBIDDEN" and guard.blocked_total == 1
    asyncio.run(run())


def test_rfc3414_key_localization_vectors():
    engine = bytes.fromhex("000000000000000000000002")
    ku = password_to_key("maplesyrup", "MD5")
    assert ku.hex() == "9faf3283884e92834ebc9847d8edd963"
    assert localize(ku, engine, "MD5").hex() == "526f5eed9fcce26f8964c2930787d82b"
    ku = password_to_key("maplesyrup", "SHA")
    assert ku.hex() == "9fb5cc0381497b3793528939ff788d5d79145211"
    assert localize(ku, engine, "SHA").hex() == "6695febc9288e36282235fc7151f128497b38f3f"


@pytest.mark.parametrize("auth", ["MD5", "SHA", "SHA-256", "SHA-512"])
def test_v3_message_auth_and_privacy_round_trip(auth):
    user = UsmUser("opcare-ro", "authPriv", auth, "auth-pass-123", "AES", "priv-pass-456")
    engine = bytes.fromhex("80001f888001020304")
    keys = user.keys(engine)
    pdu = Pdu(ber.GET, 42, 0, 0, [VarBind("1.3.6.1.2.1.1.5.0")])
    data = encode_v3(9, pdu, user, engine, 3, 1234, keys, engine, b"")
    m, whole = decode_v3(data)
    assert m.encrypted is not None and m.username == b"opcare-ro" and (m.boots, m.time) == (3, 1234)
    assert verify_v3(m, whole, user, keys[0])
    tampered = bytearray(whole)
    tampered[-1] ^= 1
    assert not verify_v3(decode_v3(bytes(tampered))[0], bytes(tampered), user, keys[0])
    decrypt_v3(m, keys[1])
    assert m.pdu.request_id == 42 and m.pdu.varbinds[0].oid == "1.3.6.1.2.1.1.5.0"


# ===================================================================== end to end with the simulator
def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def snmp_config(agent_port: int, rev: int = 1, version: str = "2c", trap_port: int | None = None, points=None,
                v3: dict | None = None, extra_devices=()) -> dict:
    conn = {"version": version, "port": agent_port, "retries": 0,
            "devices": [{"id": "core-sw", "host": "127.0.0.1", "name": "Core switch"}, *extra_devices]}
    if version == "3":
        conn["v3"] = v3
    else:
        conn["community_ref"] = "community"
    if trap_port:
        conn["traps"] = {"bind_host": "127.0.0.1", "bind_port": trap_port}
    pts = points if points is not None else [
        {"tag_id": "site-a.sw.uptime", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.1.3.0"}, "data_type": "float",
         "scaling": {"gain": 0.01, "offset": 0}, "unit": "s", "deadband": {"mode": "abs", "value": 1000000}},
        {"tag_id": "site-a.sw.name", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.1.5.0"}, "data_type": "string"},
        {"tag_id": "site-a.sw.port-2-up", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.2.2.1.8.2"}, "data_type": "bool"},
        {"tag_id": "site-a.sw.port-2-in", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.31.1.1.1.6.2", "transform": "rate"},
         "data_type": "float", "unit": "Mbit/s", "scaling": {"gain": 0.000008, "offset": 0}},
        {"tag_id": "site-a.sw.missing", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.2.2.1.8.99"}, "data_type": "bool"},
        {"tag_id": "site-a.sw.broken", "address": {"device": "core-sw", "oid": "1.3.6.1.4.1.99999.9.0"}, "data_type": "int"},
        {"tag_id": "site-a.sw.descr-as-number", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.1.1.0"}, "data_type": "float"},
    ]
    return {"spec": "1.0", "revision": rev, "site": SITE, "source_id": "src-test", "type": "snmp", "name": "SNMP test",
            "enabled": True, "connection": conn,
            "acquisition": {"default_scan_ms": 1000, "batch_max_ms": 100, "integrity_s": 0, "request_timeout_ms": 500},
            "points": pts}


async def make_agent(version_v1=True, **kw) -> SnmpAgent:
    agent = SnmpAgent("public", allow_v1=version_v1, **kw)
    standard_mib(agent, "core-sw-1", "Opcare simulated switch", interfaces=4, traffic=lambda i: 1250000)  # 10 Mbit/s
    ups_mib(agent)
    agent.set("1.3.6.1.4.1.99999.9.0", INTEGER, GEN_ERR)
    await agent.start()
    return agent


def test_v2c_polling_quality_rate_traps_admin_and_schemas():
    async def run():
        agent = await make_agent()
        trap_port = free_udp_port()
        async with Harness(SnmpConnector(), site=SITE, secrets={"community": "public\n"}) as h:
            h.config(snmp_config(agent.port, trap_port=trap_port))
            await h.wait_for(lambda: "site-a.sw.port-2-in" in h.mq.latest(), timeout=6, what="rate value")
            v = h.mq.latest()
            assert v["site-a.sw.name"]["v"] == "core-sw-1" and v["site-a.sw.name"]["q"] == 192
            assert v["site-a.sw.port-2-up"]["v"] is True
            assert v["site-a.sw.uptime"]["v"] >= 86400
            assert v["site-a.sw.port-2-in"]["v"] == pytest.approx(10, rel=0.15)              # 1.25 MB/s = 10 Mbit/s
            assert v["site-a.sw.missing"]["q"] == 4 and v["site-a.sw.missing"]["f"] == ["noSuchInstance"]
            assert v["site-a.sw.broken"]["q"] == 12 and v["site-a.sw.broken"]["f"] == ["genErr"]
            assert v["site-a.sw.descr-as-number"]["q"] == 4 and v["site-a.sw.descr-as-number"]["f"] == ["TYPE_MISMATCH"]
            assert h.rt.state == "running"
            # the genErr OID was isolated: other values keep coming in the same scan
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["isolated_oids"] == 1

            # traps: v2c, v1, inform (acknowledged), wrong community (dropped)
            agent.send_trap("127.0.0.1", trap_port, "1.3.6.1.6.3.1.1.5.3",
                            [VarBind("1.3.6.1.2.1.2.2.1.1.2", INTEGER, 2), VarBind("1.3.6.1.2.1.2.2.1.2.2", OCTET_STRING, b"Gi1/0/2")])
            agent.send_trap("127.0.0.1", trap_port, "1.3.6.1.6.3.1.1.5.1", version=V1)
            agent.send_trap("127.0.0.1", trap_port, "1.3.6.1.6.3.1.1.5.4", community=b"wrong")
            assert await agent.send_inform("127.0.0.1", trap_port, "1.3.6.1.2.1.33.2.0.1") is True
            await h.wait_for(lambda: len([e for e in h.mq.msgs("event") if e["type"] == "snmp.trap"]) == 3, what="3 traps")
            traps = {e["attrs"]["trap"]: e for e in h.mq.msgs("event") if e["type"] == "snmp.trap"}
            assert set(traps) == {"linkDown", "coldStart", "upsTrapOnBattery"}
            assert traps["linkDown"]["severity"] == "medium" and traps["linkDown"]["device"] == "core-sw"
            assert "ifDescr.2=Gi1/0/2" in traps["linkDown"]["message"]
            assert traps["upsTrapOnBattery"]["severity"] == "high"
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["traps_dropped"] == 1 and diag["informs_acked"] == 1

            # admin: test_connection, read_now, browse with continuation
            t = (await h.admin("test_connection"))["result"]["devices"][0]
            assert t["ok"] and t["info"]["sysName"] == "core-sw-1"
            r = (await h.admin("read_now", {"addresses": [{"address": {"device": "core-sw", "oid": "1.3.6.1.2.1.33.1.2.4.0"},
                                                           "data_type": "int"}]}))["result"]["values"][0]
            assert r["q"] == 192 and 95 <= r["v"] <= 100
            b = (await h.admin("browse", {"path": "core-sw/1.3.6.1.2.1.2.2.1", "max_items": 10}))["result"]
            assert [i["name"] for i in b["items"][:2]] == ["ifIndex.1", "ifIndex.2"] and "next_path" in b
            b2 = (await h.admin("browse", {"path": b["next_path"], "max_items": 500}))["result"]
            names = [i["name"] for i in b2["items"]]
            assert names[0] == "ifSpeed.3" and "ifOperStatus.4" in names and "next_path" not in b2
            assert all(i["address"]["oid"].startswith("1.3.6.1.2.1.2.2.1.") for i in b2["items"])

            # the agent goes silent: every tag q=24, conn.lost; then it comes back
            agent.offline = True
            await h.wait_for(lambda: all(x["q"] == 24 for x in h.mq.latest().values()), timeout=6, what="q 24")
            assert h.rt.state == "error" and any(e["type"] == "conn.lost" for e in h.mq.msgs("event"))
            agent.offline = False
            await h.wait_for(lambda: h.mq.latest()["site-a.sw.name"]["q"] == 192, timeout=8, what="recovery")
            assert h.schema_errors() == []
        assert "SetRequest" not in agent.log and set(agent.log) <= {"GetRequest", "GetBulkRequest", "Response"}
        agent.stop()
    asyncio.run(run())


def test_v1_isolates_missing_oids_and_walks_with_getnext():
    async def run():
        agent = await make_agent()
        pts = [{"tag_id": f"site-a.sw.if-{i}", "address": {"device": "core-sw", "oid": f"1.3.6.1.2.1.2.2.1.8.{i}"},
                "data_type": "bool"} for i in (1, 2, 77, 3)]
        async with Harness(SnmpConnector(), site=SITE, secrets={"community": "public"}) as h:
            h.config(snmp_config(agent.port, version="1", points=pts))
            await h.wait_for(lambda: len(h.mq.latest()) == 4, timeout=6, what="4 tags")
            v = h.mq.latest()
            assert [v[f"site-a.sw.if-{i}"]["q"] for i in (1, 2, 77, 3)] == [192, 192, 4, 192]
            assert v["site-a.sw.if-77"]["f"] == ["noSuchName"]
            b = (await h.admin("browse", {"path": "core-sw/1.3.6.1.2.1.1"}))["result"]["items"]
            assert [i["name"] for i in b] == ["sysDescr.0", "sysObjectID.0", "sysUpTime.0", "sysName.0", "sysLocation.0"]
            assert h.schema_errors() == []
        assert "GetBulkRequest" not in agent.log            # v1 has no GetBulk
        agent.stop()
    asyncio.run(run())


def test_too_big_halves_requests():
    async def run():
        agent = await make_agent(max_response_bytes=300)
        pts = [{"tag_id": f"site-a.sw.descr-{i}", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.1.1.0"}, "data_type": "string"}
               for i in range(8)]
        async with Harness(SnmpConnector(), site=SITE, secrets={"community": "public"}) as h:
            h.config(snmp_config(agent.port, points=pts))
            await h.wait_for(lambda: len(h.mq.latest()) == 8, timeout=6, what="8 tags")
            assert all(x["q"] == 192 for x in h.mq.latest().values())
            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["too_big"] >= 1 and diag["devices"]["core-sw"]["max_oids_per_request"] < 8
        agent.stop()
    asyncio.run(run())


V3_USER = {"username": "opcare-ro", "security_level": "authPriv", "auth_protocol": "SHA-256",
           "auth_password_ref": "auth", "priv_protocol": "AES", "priv_password_ref": "priv"}


@pytest.mark.parametrize("auth", ["SHA-256", "SHA", "MD5"])
def test_v3_auth_priv_end_to_end_with_time_resync(auth):
    async def run():
        agent = await make_agent(users=[UsmUser("opcare-ro", "authPriv", auth, "auth-pass-123", "AES", "priv-pass-456")])
        agent.time_offset = 5000                          # far from what discovery said: forces notInTimeWindow once
        pts = [{"tag_id": "site-a.fw.name", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.1.5.0"}, "data_type": "string"}]
        async with Harness(SnmpConnector(), site=SITE, secrets={"auth": "auth-pass-123", "priv": "priv-pass-456"}) as h:
            h.config(snmp_config(agent.port, version="3", points=pts, v3={**V3_USER, "auth_protocol": auth}))
            agent.time_offset = 0
            await h.wait_for(lambda: "site-a.fw.name" in h.mq.latest(), timeout=6, what="v3 value")
            assert h.mq.latest()["site-a.fw.name"] == {**h.mq.latest()["site-a.fw.name"], "v": "core-sw-1", "q": 192}
            agent.restart()                               # engine boots +1: the connector re-synchronises
            await asyncio.sleep(1.5)
            assert h.rt.state == "running"
            t = (await h.admin("test_connection"))["result"]["devices"][0]
            assert t["ok"] and t["info"]["sysName"] == "core-sw-1"
            assert h.schema_errors() == []
        agent.stop()
    asyncio.run(run())


def test_v3_wrong_password_is_auth_failed_and_secrets_never_leak():
    async def run():
        agent = await make_agent(users=[UsmUser("opcare-ro", "authPriv", "SHA-256", "auth-pass-123", "AES", "priv-pass-456")])
        pts = [{"tag_id": "site-a.fw.name", "address": {"device": "core-sw", "oid": "1.3.6.1.2.1.1.5.0"}, "data_type": "string"}]
        async with Harness(SnmpConnector(), site=SITE, secrets={"auth": "WRONG-pass-123", "priv": "priv-pass-456"}) as h:
            h.config(snmp_config(agent.port, version="3", points=pts, v3=V3_USER))
            await h.wait_for(lambda: h.rt.state == "error", timeout=6, what="error state")
            dev = h.rt.devices["core-sw"]
            assert dev["error"]["code"] == "AUTH_FAILED" and "usmStatsWrongDigests" in dev["error"]["message"]
            assert any(e["type"] == "conn.auth_failed" and e["severity"] == "high" for e in h.mq.msgs("event"))
            res = await h.admin("test_connection")
            assert res["result"]["devices"][0]["error"]["code"] == "AUTH_FAILED"
            everything = str(h.mq.published) + h.log.getvalue()
            assert "WRONG-pass-123" not in everything and "priv-pass-456" not in everything     # CT-16
        agent.stop()
    asyncio.run(run())


def test_test_connection_unreachable_and_validation():
    async def run():
        conn = SnmpConnector()
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snmp-secrets-"))
        (tmp / "community").write_text("public")
        (tmp / "short").write_text("abc")
        conn.secrets_dir = tmp
        cfg = snmp_config(free_udp_port())                 # nothing listens there
        res = await conn.test_connection(cfg)
        assert res["devices"][0]["ok"] is False and res["devices"][0]["error"]["code"] == "CONN_TIMEOUT"
        bad = snmp_config(161, version="3", v3={**V3_USER, "auth_password_ref": "short"},
                          extra_devices=[{"id": "core-sw", "host": "10.0.0.2"}])
        bad["points"].append({"tag_id": "x.y", "address": {"device": "ghost", "oid": "1.3.6.1.2.1.1.1.0", "transform": "rate"},
                              "data_type": "string"})
        errs = " | ".join(await conn.validate(bad))
        for needle in ("duplicate device id", "unknown device 'ghost'", "use data_type float", "at least 8 characters"):
            assert needle in errs, needle
        missing = await SnmpConnector().test_connection({**cfg, "connection": {**cfg["connection"], "community_ref": "nope"}})
        assert missing["devices"][0]["error"]["code"] == "SECRET_MISSING"
    asyncio.run(run())


def test_error_class_isolation_logic():
    err = SnmpError(2, 3)
    assert (err.status, err.index) == (2, 3)
    assert GAUGE32 == 0x42
