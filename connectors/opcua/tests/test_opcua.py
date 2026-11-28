"""OPC UA connector tests: codec, security and secure-channel unit tests, and end-to-end runs against
tools/simulators/opcua_server.py (our stack in the server role) over real TCP, with every published message
checked against the schemas. If asyncua is installed (CI job "opcua-interop" installs it as a test-only
opponent; it is never part of the product) the client is also run against an independent server.

Run from the repository root:  python -m pytest connectors/opcua/tests -q
"""
import asyncio
import pathlib
import socket
import sys
import tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/opcua", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk import ALLOW, ConnectorError, Q, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402
from opcua_connector import OpcUaConnector  # noqa: E402
from opcua_connector import security as S  # noqa: E402
from opcua_connector import uabin as U  # noqa: E402
from opcua_connector.channel import SecureChannel  # noqa: E402
from opcua_connector.client import UaClient  # noqa: E402
from opcua_connector.connector import coerce, quality  # noqa: E402
from opcua_server import OpcUaServer  # noqa: E402

SITE = "site-a"
NS = "urn:opcare:test:plant"
POLICIES = [("None", "None"), ("Basic256Sha256", "Sign"), ("Basic256Sha256", "SignAndEncrypt"),
            ("Aes128_Sha256_RsaOaep", "SignAndEncrypt"), ("Aes256_Sha256_RsaPss", "SignAndEncrypt")]
_CERTS: dict = {}


def certs(name: str, key_size: int = 2048):
    if (name, key_size) not in _CERTS:
        _CERTS[(name, key_size)] = S.make_application_certificate(f"urn:opcare:{name}", name, key_size=key_size)
    return _CERTS[(name, key_size)]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ===================================================================== unit tests
def test_nodeid_text_forms_round_trip_and_binary_encodings():
    for t in ("i=85", "ns=2;s=Line1.Temp", "ns=3;i=1001", "nsu=urn:x:y;s=Temp", "ns=1;g=72962b91-fa75-4ae6-8d28-b404dc7daf63",
              "ns=1;b=AAEC"):
        assert str(U.NodeId.parse(t)) == t
    for n, size in ((U.NodeId(85), 2), (U.NodeId(1001, 3), 4), (U.NodeId(70000, 1), 7), (U.NodeId("abc", 2), 10)):
        w = U.Writer()
        w.nodeid(n)
        assert len(w.bytes()) == size and U.Reader(w.bytes()).nodeid() == n      # two-byte, four-byte, numeric, string
    with pytest.raises(U.UaError):
        U.NodeId.parse("x=1")


def test_variant_datavalue_and_message_round_trip():
    ts = datetime(2026, 11, 27, 10, 40, 0, 123000, tzinfo=timezone.utc)
    w = U.Writer()
    values = [U.Variant(U.DOUBLE, 1.5), U.Variant(U.STRING, "AUTO"), U.Variant(U.INT32, [1, 2, 3], True),
              U.Variant(U.BOOLEAN, True), U.Variant(U.DATETIME, ts), U.Variant(U.UINT64, 2 ** 63),
              U.Variant(U.LOCALIZEDTEXT, U.LocalizedText("Pump", "en"))]
    for v in values:
        w.variant(v)
    w.datavalue(U.DataValue(U.Variant(U.FLOAT, 2.5), 0x40930000, ts, ts))
    r = U.Reader(w.bytes())
    assert [r.variant() for _ in values] == values
    dv = r.datavalue()
    assert dv.value.value == 2.5 and dv.status == 0x40930000 and dv.source_ts == ts
    msg = U.encode_message("ReadRequest", {"header": {"request_handle": 9}, "max_age": 0.0, "timestamps_to_return": 2,
                                           "nodes_to_read": [{"node_id": U.NodeId("T", 2), "attribute_id": 13}]})
    name, obj = U.decode_message(msg)
    assert name == "ReadRequest" and obj["header"]["request_handle"] == 9
    assert obj["nodes_to_read"][0]["node_id"] == U.NodeId("T", 2)


def test_no_write_services_exist_and_guard_blocks_them():
    for service in ("Write", "Call", "HistoryUpdate", "AddNodes", "DeleteNodes", "AddReferences", "DeleteReferences"):
        assert service not in ALLOW["opcua"]
        assert f"{service}Request" not in U.STRUCTS and f"{service}Request" not in U.ENCODING_IDS
        with pytest.raises(U.UaError):
            U.encode_message(f"{service}Request", {})
    blocked = []
    guard = ReadOnlyGuard("opcua", ALLOW["opcua"], on_block=blocked.append)
    client = UaClient("opc.tcp://127.0.0.1:1/x", guard)
    with pytest.raises(ConnectorError) as e:
        asyncio.run(client.request("WriteRequest", {}))
    assert e.value.code == "OP_FORBIDDEN" and blocked == ["WriteRequest"]


def test_quality_mapping_spec_15_2():
    assert quality(0) == Q.GOOD and quality(0x00960000) == Q.GOOD_LOCAL_OVERRIDE
    assert quality(0x408F0000) == 68 and quality(0x40930000) == 80 and quality(0x40940000) == 84 and quality(0x40000000) == 64
    assert quality(0x80340000) == 4 and quality(0x80330000) == 4 and quality(0x80740000) == 4
    assert quality(0x808B0000) == 12 and quality(0x80050000) == 24 and quality(0x808A0000) == 24
    assert quality(0x808D0000) == 28 and quality(0x80010000) == 0
    assert coerce(U.Variant(U.INT16, 7), "float") == (7.0, [])
    assert coerce(U.Variant(U.DOUBLE, 3.0), "int") == (3, [])
    assert coerce(U.Variant(U.INT32, [1, 2], True), "string") == ("[1, 2]", [])
    with pytest.raises(TypeError):
        coerce(U.Variant(U.STRING, "x"), "float")


@pytest.mark.parametrize("policy,mode", POLICIES[1:] + [("Aes256_Sha256_RsaPss-4096", "SignAndEncrypt")])
def test_secure_channel_both_roles_chunking_and_tamper_detection(policy, mode):
    ks = 4096 if policy.endswith("4096") else 2048
    p = S.POLICIES[policy.split("-")[0]]
    (cc, ck), (sc, sk) = certs("client", ks), certs("server", ks)
    cli = SecureChannel(p, S.MODES[mode], S.der(S.load_cert(cc)), S.load_key(ck), S.der(S.load_cert(sc)))
    srv = SecureChannel(local_cert=S.der(S.load_cert(sc)), local_key=S.load_key(sk))
    body = b"open" * 300
    req, got, info = srv.decode_open(cli.encode_open(1, body))
    assert (req, got, info["policy"]) == (1, body, p)
    srv.policy, srv.mode, srv.remote_cert = p, S.MODES[mode], info["sender_cert"]
    assert cli.decode_open(srv.encode_open(1, b"reply"))[1] == b"reply"
    n1, n2 = S.nonce(p), S.nonce(p)
    cli.set_keys(n1, n2, 1)
    srv.set_keys(n2, n1, 1)
    big = bytes(range(256)) * 700
    chunks = cli.encode_msg(b"MSG", 2, big)
    assert len(chunks) > 1 and b"".join(srv.decode_msg(c)[1] for c in chunks) == big
    tampered = bytearray(chunks[0])
    tampered[40] ^= 1
    with pytest.raises(S.SecurityError):
        srv.decode_msg(bytes(tampered))


# ===================================================================== end to end with the test server
def plant_server(**kw) -> OpcUaServer:
    srv = OpcUaServer(NS, **kw)
    srv.add_variable("Boiler1.Temperature", 80.5, folder="Plant/Boiler1", name="Temperature")
    srv.add_variable("Boiler1.Running", True, folder="Plant/Boiler1", name="Running")
    srv.add_variable("Pump1.Speed", 1450, folder="Plant/Pump1", vtype=U.UINT16, name="Speed")
    srv.add_variable("Pump1.Mode", "AUTO", folder="Plant/Pump1", name="Mode")
    for i in range(12):
        srv.add_variable(f"Tank.Level{i}", float(i), folder="Plant/Tank", name=f"Level{i}")
    return srv


def cfg(url: str, rev=1, policy="None", mode="None", trusted=None, auth=None, acq=None, points=None):
    conn = {"endpoint_url": url, "security_policy": policy, "security_mode": mode, "session_timeout_ms": 60000}
    if policy != "None":
        conn.update(client_cert_ref="ua-client.pem", client_key_ref="ua-client.key",
                    trusted_server_thumbprints=trusted or [])
    if auth:
        conn.update(auth="username", username=auth, password_ref="ua-pw")
    pts = points or [
        {"tag_id": "site-a.boiler1.temp", "address": {"node_id": f"nsu={NS};s=Boiler1.Temperature"}, "data_type": "float",
         "unit": "degC"},
        {"tag_id": "site-a.boiler1.running", "address": {"node_id": f"nsu={NS};s=Boiler1.Running"}, "data_type": "bool"},
        {"tag_id": "site-a.pump1.speed", "address": {"node_id": "ns=1;s=Pump1.Speed"}, "data_type": "int"},
        {"tag_id": "site-a.pump1.mode", "address": {"node_id": f"nsu={NS};s=Pump1.Mode"}, "data_type": "string"},
        {"tag_id": "site-a.missing", "address": {"node_id": f"nsu={NS};s=Nope"}, "data_type": "float"},
        {"tag_id": "site-a.wrong-ns", "address": {"node_id": "nsu=urn:not:there;s=X"}, "data_type": "float"},
        {"tag_id": "site-a.mode-as-float", "address": {"node_id": f"nsu={NS};s=Pump1.Mode"}, "data_type": "float"},
    ]
    return {"spec": "1.0", "revision": rev, "site": SITE, "source_id": "src-test", "type": "opcua", "name": "OPC UA test",
            "enabled": True, "connection": conn,
            "acquisition": {"batch_max_ms": 100, "integrity_s": 0, "request_timeout_ms": 2000, "publishing_interval_ms": 100,
                            **(acq or {})}, "points": pts}


def secrets(password="op-secret") -> dict:
    cc, ck = certs("client")
    return {"ua-client.pem": cc, "ua-client.key": ck, "ua-pw": password}


@pytest.mark.parametrize("policy,mode", [("None", "None"), ("Basic256Sha256", "SignAndEncrypt")])
def test_end_to_end_subscribe_quality_timestamps_reconnect_and_schemas(policy, mode):
    async def run():
        srv = plant_server(policies=[(policy, mode)], users={"operator": "op-secret"}, anonymous=False)
        await srv.start()
        async with Harness(OpcUaConnector(), site=SITE, secrets=secrets()) as h:
            h.config(cfg(srv.url, policy=policy, mode=mode, trusted=[srv.thumbprint], auth="operator"))
            await h.wait_for(lambda: h.rt.state == "running", what="running")
            await h.wait_for(lambda: len(h.mq.latest()) == 7, what="7 tags")
            v = h.mq.latest()
            assert v["site-a.boiler1.temp"]["v"] == 80.5 and v["site-a.boiler1.temp"]["q"] == 192
            assert v["site-a.boiler1.running"]["v"] is True and v["site-a.pump1.speed"]["v"] == 1450
            assert v["site-a.pump1.mode"]["v"] == "AUTO"
            assert v["site-a.missing"]["q"] == 4 and v["site-a.wrong-ns"]["q"] == 4
            assert v["site-a.mode-as-float"]["q"] == 4 and v["site-a.mode-as-float"]["f"] == ["TYPE_MISMATCH"]

            ts = datetime(2026, 11, 27, 10, 40, 1, 250000, tzinfo=timezone.utc)
            srv.set("Boiler1.Temperature", 81.25, status=0x40930000, ts=ts)          # Uncertain_SensorNotAccurate
            await h.wait_for(lambda: h.mq.latest()["site-a.boiler1.temp"]["v"] == 81.25, what="data change")
            t = h.mq.latest()["site-a.boiler1.temp"]
            assert t["q"] == 80 and t["ts"] == "2026-11-27T10:40:01.250Z"
            srv.set("Pump1.Speed", status=0x808D0000)                                  # Bad_OutOfService
            await h.wait_for(lambda: h.mq.latest()["site-a.pump1.speed"]["q"] == 28, what="out of service")

            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["subscriptions"] == 1 and diag["monitored_items"] == 5 and diag["bad_items"]["site-a.missing"]

            srv.kick_all()                                                            # link loss: q 24, reconnect
            await h.wait_for(lambda: all(x["q"] == 24 for x in h.mq.latest().values()), what="q 24 on link loss")
            assert any(e["type"] == "conn.lost" for e in h.mq.msgs("event"))
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="reconnected")
            srv.set("Boiler1.Temperature", 79.0)
            await h.wait_for(lambda: h.mq.latest()["site-a.boiler1.temp"]["v"] == 79.0, timeout=8, what="after reconnect")
            assert h.schema_errors() == []
        assert set(srv.requests) <= ALLOW["opcua"], set(srv.requests) - ALLOW["opcua"]    # CT-13
        await srv.stop()
    asyncio.run(run())


def test_poll_mode_read_now_and_admin_browse():
    async def run():
        srv = plant_server()
        await srv.start()
        async with Harness(OpcUaConnector(), site=SITE) as h:
            h.config(cfg(srv.url, acq={"mode": "poll", "default_scan_ms": 200}))
            await h.wait_for(lambda: len(h.mq.latest()) == 7, what="7 tags")
            srv.set("Pump1.Speed", 1500)
            await h.wait_for(lambda: h.mq.latest()["site-a.pump1.speed"]["v"] == 1500, what="polled change")
            assert "Publish" not in srv.requests and "Read" in srv.requests
            res = (await h.admin("read_now", {"tag_ids": ["site-a.boiler1.temp"],
                                              "addresses": [{"address": {"node_id": f"nsu={NS};s=Pump1.Mode"},
                                                             "data_type": "string"}]}))["result"]
            assert res["values"][0]["t"] == "site-a.boiler1.temp" and res["values"][0]["v"] == 80.5
            assert res["values"][1]["v"] == "AUTO" and res["values"][1]["q"] == 192
            b = (await h.admin("browse", {"path": f"nsu={NS};s=Plant", "depth": 2}))["result"]
            items = {i["name"]: i for i in b["items"]}
            assert items["Boiler1"]["kind"] == "folder" and items["Boiler1"]["path"] == f"nsu={NS};s=Plant/Boiler1"
            assert items["Temperature"]["address"] == {"node_id": f"nsu={NS};s=Boiler1.Temperature"}
            assert items["Temperature"]["data_type"] == "float" and items["Speed"]["data_type"] == "int"
            assert items["Running"]["data_type"] == "bool" and items["Speed"]["info"]["data_type"] == "UInt16"
            assert items["Mode"]["info"]["sample"] == "AUTO"
            assert h.schema_errors() == []
        await srv.stop()
    asyncio.run(run())


def test_test_connection_trust_flow_auth_and_browse_continuation():
    async def run():
        srv = plant_server(policies=[("None", "None"), ("Basic256Sha256", "SignAndEncrypt")], users={"operator": "op-secret"})
        await srv.start()
        conn = OpcUaConnector()
        conn.secrets_dir = pathlib.Path(tempfile.mkdtemp())
        for k, v in secrets().items():
            (conn.secrets_dir / k).write_text(v)
        secure = cfg(srv.url, policy="Basic256Sha256", mode="SignAndEncrypt", auth="operator")

        res = (await conn.test_connection(secure))["devices"][0]                  # not trusted yet: thumbprint shown
        assert res["ok"] is False and res["error"]["code"] == "TLS_FAILED"
        assert res["info"]["server_thumbprint"] == srv.thumbprint
        assert res["info"]["server_certificate"]["application_uri"] == "urn:opcare:sim:server"
        assert "Basic256Sha256/SignAndEncrypt" in res["info"]["endpoints"]

        secure["connection"]["trusted_server_thumbprints"] = [srv.thumbprint]
        res = (await conn.test_connection(secure))["devices"][0]
        assert res["ok"], res
        assert res["info"]["security"] == "Basic256Sha256/SignAndEncrypt" and res["info"]["server_state"] == "Running"
        assert res["info"]["product"] == "Opcare OPC UA test server" and res["info"]["namespaces"] == [NS]

        (conn.secrets_dir / "ua-pw").write_text("wrong")
        res = (await conn.test_connection(secure))["devices"][0]
        assert res["error"]["code"] == "AUTH_FAILED"
        res = (await conn.test_connection(cfg(srv.url, policy="Aes256_Sha256_RsaPss", mode="SignAndEncrypt",
                                              trusted=[srv.thumbprint])))["devices"][0]
        assert res["error"]["code"] == "TLS_FAILED" and "offers" in res["error"]["message"]
        res = (await conn.test_connection(cfg(f"opc.tcp://127.0.0.1:{free_port()}/x")))["devices"][0]
        assert res["error"]["code"] in ("CONN_REFUSED", "CONN_TIMEOUT")

        srv.browse_page = 5                                                       # forces BrowseNext
        b = await conn.browse({"config": cfg(srv.url), "path": f"nsu={NS};s=Plant/Tank"})
        assert len(b["items"]) == 12 and "BrowseNext" in srv.requests
        top = {i["name"]: i for i in (await conn.browse({"config": cfg(srv.url)}))["items"]}
        assert top["Plant"]["kind"] == "folder" and "Server" in top

        bad = cfg(srv.url, points=[{"tag_id": "site-a.x", "address": {"node_id": "ns=1;q=1"}, "data_type": "float"}])
        bad["connection"]["security_mode"] = "Sign"
        errs = await conn.validate(bad)
        assert any("security_mode" in e for e in errs) and any("site-a.x" in e for e in errs)
        assert set(srv.requests) <= ALLOW["opcua"]
        await srv.stop()
    asyncio.run(run())


# ===================================================================== independent server (CI: opcua-interop)
def test_interop_with_independent_asyncua_server():
    asyncua = pytest.importorskip("asyncua", reason="asyncua not installed (installed only in the CI interop job)")
    from asyncua import Server, ua
    from asyncua.crypto.permission_rules import SimpleRoleRuleset, User, UserRole
    from asyncua.server.user_managers import UserManager

    class Users(UserManager):
        def get_user(self, iserver, username=None, password=None, certificate=None):
            if username is not None:
                return User(role=UserRole.User) if (username, password) == ("operator", "op-secret") else None
            return User(role=UserRole.User)

    async def run():
        d = pathlib.Path(tempfile.mkdtemp())
        sc, sk = certs("asyncua-server")
        (d / "s.pem").write_text(sc)
        (d / "s.key.pem").write_text(sk)
        port = free_port()
        srv = Server(user_manager=Users())
        await srv.init()
        srv.set_endpoint(f"opc.tcp://127.0.0.1:{port}/interop")
        srv.set_security_policy([ua.SecurityPolicyType.NoSecurity, ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
                                 ua.SecurityPolicyType.Aes128Sha256RsaOaep_SignAndEncrypt,
                                 ua.SecurityPolicyType.Aes256Sha256RsaPss_SignAndEncrypt],
                                permission_ruleset=SimpleRoleRuleset())
        srv.set_security_IDs(["Anonymous", "Username"])
        await srv.load_certificate(str(d / "s.pem"))
        await srv.load_private_key(str(d / "s.key.pem"))
        idx = await srv.register_namespace(NS)
        boiler = await srv.nodes.objects.add_folder(idx, "Boiler1")
        temp = await boiler.add_variable(ua.NodeId("Boiler1.Temperature", idx), "Temperature", 80.5)
        await boiler.add_variable(ua.NodeId("Boiler1.Running", idx), "Running", True)
        await boiler.add_variable(ua.NodeId("Pump1.Speed", idx), "Speed", ua.Variant(1450, ua.VariantType.UInt16))
        await boiler.add_variable(ua.NodeId("Pump1.Mode", idx), "Mode", "AUTO")
        thumb = S.thumbprint(S.der(S.load_cert(sc)))
        url = f"opc.tcp://127.0.0.1:{port}/interop"
        uri_points = [{**p, "address": {"node_id": p["address"]["node_id"].replace("ns=1;", f"nsu={NS};")}}
                      for p in cfg(url)["points"][:4]]       # index 1 is asyncua's own namespace there: use URIs
        async with srv:
            for policy, mode in (("Basic256Sha256", "SignAndEncrypt"), ("Aes256_Sha256_RsaPss", "SignAndEncrypt"),
                                 ("None", "None")):
                async with Harness(OpcUaConnector(), site=SITE, secrets=secrets()) as h:
                    h.config(cfg(url, policy=policy, mode=mode, trusted=[thumb], auth="operator", points=uri_points))
                    await h.wait_for(lambda: len(h.mq.latest()) == 4, timeout=10, what=f"4 tags over {policy}")
                    await temp.write_value(82.0)
                    await h.wait_for(lambda: h.mq.latest()["site-a.boiler1.temp"]["v"] == 82.0, timeout=10, what="change")
                    await temp.write_value(80.5)
                    assert h.mq.latest()["site-a.pump1.speed"]["v"] == 1450 and h.schema_errors() == []
            conn = OpcUaConnector()
            conn.secrets_dir = pathlib.Path(tempfile.mkdtemp())
            for k, v in secrets("wrong").items():
                (conn.secrets_dir / k).write_text(v)
            res = (await conn.test_connection(cfg(url, policy="Basic256Sha256", mode="SignAndEncrypt", trusted=[thumb],
                                                  auth="operator")))["devices"][0]
            assert res["error"]["code"] == "AUTH_FAILED"
            b = await conn.browse({"config": cfg(url), "path": f"nsu={NS};i={boiler.nodeid.Identifier}"})
            items = {i["name"]: i for i in b["items"]}
            assert items["Temperature"]["address"] == {"node_id": f"nsu={NS};s=Boiler1.Temperature"}
            assert items["Speed"]["data_type"] == "int"
    assert asyncua
    asyncio.run(run())
