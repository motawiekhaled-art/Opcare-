"""MQTT connector tests: packet and Sparkplug B unit tests, and end-to-end runs against tools/simulators/mqtt_broker.py
(MQTT 3.1.1 and 5.0, over real TCP), every published message checked against the schemas. With OPCARE_TEST_MQTT_URL set
(CI: a real Mosquitto) the connector is also run against that broker.

Run from the repository root:  python -m pytest connectors/mqtt/tests -q
"""
import asyncio
import json
import os
import pathlib
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/mqtt", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from mqtt_broker import MqttBroker  # noqa: E402
from mqtt_connector import MqttConnector  # noqa: E402
from mqtt_connector import packets as P  # noqa: E402
from mqtt_connector import sparkplug as SP  # noqa: E402
from opcare_connector_sdk import ALLOW, ConnectorError, ReadOnlyGuard  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402

SITE = "site-a"


# ===================================================================== unit tests
def test_varint_and_topic_matching():
    for n in (0, 127, 128, 16383, 16384, 268435455):
        assert P.read_varint(P.varint(n), 0) == (n, len(P.varint(n)))
    assert P.topic_matches("meters/+/telemetry", "meters/118/telemetry")
    assert not P.topic_matches("meters/+/telemetry", "meters/118/status")
    assert P.topic_matches("spBv1.0/#", "spBv1.0/plant/NDATA/gw1")
    assert P.topic_matches("#", "a/b") and not P.topic_matches("#", "$SYS/broker/uptime")
    assert P.topic_matches("a/#", "a")                                  # '#' also matches the parent level


def test_connect_and_subscribe_encoding():
    c = P.connect("opcare-x", P.V311, 60, True, "user", "pw")
    assert c[0] == 0x10 and b"MQTT\x04\xc2\x00\x3c" in c and c.endswith(b"\x00\x04user\x00\x02pw")
    assert P.connect("id", P.V5, 30)[2:13] == b"\x00\x04MQTT\x05\x02\x00\x1e\x00"   # v5: empty properties
    s = P.subscribe(7, [("a/#", 2)], P.V311)
    assert s == b"\x82\x08\x00\x07\x00\x03a/#\x01"                     # QoS capped at 1


def test_guard_never_allows_publish():
    assert "PUBLISH" not in ALLOW["mqtt"]
    with pytest.raises(ConnectorError) as e:
        ReadOnlyGuard("mqtt", ALLOW["mqtt"]).check("PUBLISH")
    assert e.value.code == "OP_FORBIDDEN"
    assert not hasattr(P, "publish")                                    # no PUBLISH encoder exists in the connector


def test_sparkplug_round_trip_all_scalar_types():
    metrics = [SP.encode_metric("Temp", SP.FLOAT, 21.5, alias=1, timestamp=1764240000000),
               SP.encode_metric("Count", SP.INT32, -5, alias=2), SP.encode_metric("Big", SP.INT64, -7),
               SP.encode_metric("U16", SP.UINT16, 65535), SP.encode_metric("Energy", SP.DOUBLE, 1234.5678),
               SP.encode_metric("Run", SP.BOOLEAN, True), SP.encode_metric("Mode", SP.STRING, "AUTO"),
               SP.encode_metric("Gone", SP.INT32, None), SP.encode_metric(None, SP.FLOAT, 3.25, alias=1)]
    p = SP.decode(SP.encode(metrics, timestamp=1764240000123, seq=4))
    assert p.timestamp == 1764240000123 and p.seq == 4
    got = [(m.name, m.alias, m.value) for m in p.metrics]
    assert got[0] == ("Temp", 1, 21.5) and p.metrics[0].timestamp == 1764240000000
    assert got[1:7] == [("Count", 2, -5), ("Big", None, -7), ("U16", None, 65535), ("Energy", None, 1234.5678),
                        ("Run", None, True), ("Mode", None, "AUTO")]
    assert p.metrics[7].is_null and p.metrics[7].value is None
    assert got[8] == (None, 1, 3.25)
    assert SP.parse_topic("spBv1.0/plant/DDATA/gw1/inv-3") == ("plant", "DDATA", "gw1", "inv-3")
    assert SP.parse_topic("meters/1") is None
    with pytest.raises(SP.SparkplugError):
        SP.decode(b"\x12\xff\xff")


# ===================================================================== end to end with the test broker
def cfg(port: int, version="5", rev=1, auth=None, points=None, subs=None):
    conn = {"broker_url": f"mqtt://127.0.0.1:{port}", "protocol_version": version, "keepalive_s": 10,
            "subscriptions": subs or [{"topic_filter": "meters/+/telemetry", "qos": 1}, {"topic_filter": "plant/line1/#", "qos": 0},
                                      {"topic_filter": "spBv1.0/solar/#", "qos": 0}]}
    if auth:
        conn.update(auth="username", username=auth, password_ref="broker-pw")
    pts = points or [
        {"tag_id": "site-a.meter-118.flow", "address": {"topic": "meters/118/telemetry", "format": "json", "value_pointer": "/flow",
         "timestamp_pointer": "/ts", "timestamp_format": "epoch_ms"}, "data_type": "float", "unit": "m3/h"},
        {"tag_id": "site-a.meter-118.valve-open", "address": {"topic": "meters/118/telemetry", "format": "json",
         "value_pointer": "/valve"}, "data_type": "bool"},
        {"tag_id": "site-a.line1.speed", "address": {"topic": "plant/line1/speed", "format": "raw"}, "data_type": "float"},
        {"tag_id": "site-a.line1.state", "address": {"topic": "plant/line1/state", "format": "raw"}, "data_type": "string"},
        {"tag_id": "site-a.solar.gw-power", "address": {"format": "sparkplug_b", "group_id": "solar", "edge_node_id": "gw1",
         "metric": "Plant/Power"}, "data_type": "float", "unit": "kW"},
        {"tag_id": "site-a.solar.inv3-temp", "address": {"format": "sparkplug_b", "group_id": "solar", "edge_node_id": "gw1",
         "device_id": "inv-3", "metric": "Temperature"}, "data_type": "float"},
        {"tag_id": "site-a.solar.inv3-run", "address": {"format": "sparkplug_b", "group_id": "solar", "edge_node_id": "gw1",
         "device_id": "inv-3", "metric": "Running"}, "data_type": "bool"},
    ]
    return {"spec": "1.0", "revision": rev, "site": SITE, "source_id": "src-test", "type": "mqtt", "name": "MQTT test",
            "enabled": True, "connection": conn,
            "acquisition": {"batch_max_ms": 100, "integrity_s": 0, "request_timeout_ms": 2000}, "points": pts}


def births(broker: MqttBroker):
    broker.publish("spBv1.0/solar/NBIRTH/gw1", SP.encode([SP.encode_metric("Plant/Power", SP.FLOAT, 850.0, alias=10),
                                                          SP.encode_metric("bdSeq", SP.UINT64, 0, alias=11)], seq=0))
    broker.publish("spBv1.0/solar/DBIRTH/gw1/inv-3", SP.encode([SP.encode_metric("Temperature", SP.DOUBLE, 41.5, alias=20),
                                                                SP.encode_metric("Running", SP.BOOLEAN, True, alias=21)], seq=1))


@pytest.mark.parametrize("version", ["5", "3.1.1"])
def test_end_to_end_json_raw_sparkplug_deaths_reconnect_and_schemas(version):
    async def run():
        broker = MqttBroker()
        port = await broker.start()
        broker.publish("plant/line1/state", "RUNNING", retain=True)          # retained: delivered on subscribe
        async with Harness(MqttConnector(), site=SITE) as h:
            h.config(cfg(port, version))
            await h.wait_for(lambda: h.rt.state == "running", what="running")
            await h.wait_for(lambda: "site-a.line1.state" in h.mq.latest(), what="retained value")
            broker.publish("meters/118/telemetry", json.dumps({"flow": 12.75, "valve": "open", "ts": 1764240000000}), qos=1)
            broker.publish("meters/999/telemetry", json.dumps({"flow": 1}))         # matches the filter, not a tag
            broker.publish("plant/line1/speed", "1450.5")
            births(broker)
            broker.publish("spBv1.0/solar/NDATA/gw1", SP.encode([SP.encode_metric(None, SP.FLOAT, 901.25, alias=10,
                                                                                  timestamp=1764240001000)], seq=2))
            await h.wait_for(lambda: len(h.mq.latest()) == 7, what="7 tags")
            v = h.mq.latest()
            assert v["site-a.meter-118.flow"]["v"] == 12.75 and v["site-a.meter-118.flow"]["ts"] == "2025-11-27T10:40:00.000Z"
            assert v["site-a.meter-118.valve-open"]["v"] is True
            assert v["site-a.line1.speed"]["v"] == 1450.5 and v["site-a.line1.state"]["v"] == "RUNNING"
            assert v["site-a.solar.gw-power"]["v"] == 901.25 and v["site-a.solar.gw-power"]["ts"] == "2025-11-27T10:40:01.000Z"
            assert v["site-a.solar.inv3-temp"]["v"] == 41.5 and v["site-a.solar.inv3-run"]["v"] is True

            broker.publish("meters/118/telemetry", "not json")
            await h.wait_for(lambda: h.mq.latest()["site-a.meter-118.flow"]["q"] == 0, what="parse error")
            assert h.mq.latest()["site-a.meter-118.flow"]["f"] == ["PARSE_ERROR"]

            broker.publish("spBv1.0/solar/DDEATH/gw1/inv-3", SP.encode([], seq=3))
            await h.wait_for(lambda: h.mq.latest()["site-a.solar.inv3-temp"]["q"] == 24, what="DDEATH")
            assert h.mq.latest()["site-a.solar.gw-power"]["q"] == 192                 # the node itself is still alive
            broker.publish("spBv1.0/solar/NDEATH/gw1", SP.encode([SP.encode_metric("bdSeq", SP.UINT64, 0)]))
            await h.wait_for(lambda: h.mq.latest()["site-a.solar.gw-power"]["q"] == 24, what="NDEATH")
            births(broker)
            await h.wait_for(lambda: h.mq.latest()["site-a.solar.inv3-temp"]["q"] == 192, what="rebirth")

            diag = (await h.admin("get_diagnostics"))["result"]["connector"]
            assert diag["unmatched"] >= 1 and diag["sparkplug_nodes"]["solar/gw1"]["births"] == 2
            assert set(diag["packets_sent"]) <= {"CONNECT", "SUBSCRIBE", "PUBACK", "PINGREQ"}

            # broker goes away: every tag q=24 and conn.lost; it comes back and the connector resubscribes
            broker.kick_all()
            await h.wait_for(lambda: all(x["q"] == 24 for x in h.mq.latest().values()), what="q 24 on link loss")
            assert any(e["type"] == "conn.lost" for e in h.mq.msgs("event"))
            await h.wait_for(lambda: h.rt.state == "running", timeout=8, what="reconnected")
            broker.publish("plant/line1/speed", "1500")
            await h.wait_for(lambda: h.mq.latest()["site-a.line1.speed"]["v"] == 1500, what="values after reconnect")
            assert h.schema_errors() == []
        assert "PUBLISH" not in broker.sent_by("opcare-")                     # CT-13: the connector never publishes
        await broker.stop()
    asyncio.run(run())


def test_auth_test_connection_browse_and_validation():
    async def run():
        broker = MqttBroker(users={"opcare": "broker-secret"})
        port = await broker.start()
        conn = MqttConnector()
        conn.secrets_dir = pathlib.Path(__import__("tempfile").mkdtemp())
        (conn.secrets_dir / "broker-pw").write_text("broker-secret")
        good = cfg(port, auth="opcare")
        res = (await conn.test_connection(good))["devices"][0]
        assert res["ok"] and res["info"]["subscriptions"]["meters/+/telemetry"] == "QoS 1"
        (conn.secrets_dir / "broker-pw").write_text("wrong")
        res = (await conn.test_connection(good))["devices"][0]
        assert res["ok"] is False and res["error"]["code"] == "AUTH_FAILED"
        res = (await conn.test_connection(cfg(free_port())))["devices"][0]
        assert res["error"]["code"] in ("CONN_REFUSED", "CONN_TIMEOUT")       # Windows times out on closed ports
        (conn.secrets_dir / "broker-pw").write_text("broker-secret")

        async def chatter():
            await asyncio.sleep(0.5)
            for i in range(5):
                broker.publish("meters/7/telemetry", json.dumps({"flow": 3.5 + i, "meta": {"fw": "2.1"}}))
                broker.publish("spBv1.0/solar/NBIRTH/gw1", SP.encode([SP.encode_metric("Plant/Power", SP.FLOAT, 800.0, alias=1)]))
                broker.publish("plant/line1/speed", "99")
                await asyncio.sleep(0.2)
        task = asyncio.create_task(chatter())
        b = await conn.browse({"config": good, "listen_s": 2})
        await task
        items = {i["path"]: i for i in b["items"]}
        assert items["meters/7/telemetry"]["info"]["format"] == "json"
        assert items["meters/7/telemetry|/flow"]["address"] == {"topic": "meters/7/telemetry", "format": "json", "value_pointer": "/flow"}
        assert items["spBv1.0/solar/gw1|Plant/Power"]["address"] == {"format": "sparkplug_b", "group_id": "solar",
                                                                   "edge_node_id": "gw1", "metric": "Plant/Power"}
        assert items["spBv1.0/solar/gw1|Plant/Power"]["info"]["type"] == "Float"
        assert items["spBv1.0/solar/gw1"]["info"]["format"] == "sparkplug_b"
        assert items["plant/line1/speed"]["address"] == {"topic": "plant/line1/speed", "format": "raw"}

        bad = cfg(port, subs=[{"topic_filter": "meters/#", "qos": 1}, {"topic_filter": "a/b#", "qos": 0}])
        bad["connection"]["auth"] = "certificate"
        errs = " | ".join(await conn.validate(bad))
        for needle in ("needs client_cert_ref", "needs an mqtts://", "# must be a whole last level",
                       "'plant/line1/speed' is not covered", "no subscription covers Sparkplug node solar/gw1"):
            assert needle in errs, needle
        assert "PUBLISH" not in broker.sent_by("opcare-")
        await broker.stop()
    asyncio.run(run())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_against_real_mosquitto():
    url = os.environ.get("OPCARE_TEST_MQTT_URL")
    if not url:
        pytest.skip("set OPCARE_TEST_MQTT_URL (CI runs a real Mosquitto)")
    paho = pytest.importorskip("paho.mqtt.client")

    async def run():
        host, port = url.removeprefix("mqtt://").split(":")
        pub = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="opcare-ci-device")
        pub.connect(host, int(port))
        pub.loop_start()
        for version in ("5", "3.1.1"):
            c = cfg(int(port), version)
            c["connection"]["client_id"] = f"opcare-ci-{version}"
            async with Harness(MqttConnector(), site=SITE) as h:
                h.config(c)
                await h.wait_for(lambda: h.rt.state == "running", what="running on Mosquitto")
                await asyncio.sleep(0.3)
                pub.publish("meters/118/telemetry", json.dumps({"flow": 7.5, "valve": 0, "ts": 1764240000000}), qos=1).wait_for_publish()
                pub.publish("spBv1.0/solar/NBIRTH/gw1", SP.encode([SP.encode_metric("Plant/Power", SP.FLOAT, 777.0, alias=10)])).wait_for_publish()
                pub.publish("spBv1.0/solar/NDATA/gw1", SP.encode([SP.encode_metric(None, SP.FLOAT, 778.5, alias=10)])).wait_for_publish()
                await h.wait_for(lambda: h.mq.latest().get("site-a.solar.gw-power", {}).get("v") == 778.5, what="sparkplug via Mosquitto")
                assert h.mq.latest()["site-a.meter-118.flow"]["v"] == 7.5
                assert h.mq.latest()["site-a.meter-118.valve-open"]["v"] is False
                assert h.schema_errors() == []
            res = (await MqttConnector().test_connection(c))["devices"][0]
            assert res["ok"] and res["info"]["protocol"] == ("5.0" if version == "5" else "3.1.1")
        pub.loop_stop()
        pub.disconnect()
    asyncio.run(run())


def _test_pki(tmp: pathlib.Path):
    """A test CA and a server certificate for 127.0.0.1 (cryptography, as used by the SNMP connector)."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    import ipaddress
    now = datetime.datetime.now(datetime.timezone.utc)
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()  # noqa: E731

    def make(cn, issuer=None, issuer_key=None, ca=False, ip=None):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        b = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer or name).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=2))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
             .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=not ca,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                          encipher_only=False, decipher_only=False), critical=True))
        if issuer_key is not None:
            b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
            b = b.add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        if ip:
            b = b.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
        return key, b.sign(issuer_key or key, hashes.SHA256())
    ca_key, ca = make("Opcare test CA", ca=True)
    srv_key, srv = make("broker", ca.subject, ca_key, ip="127.0.0.1")
    _, other_ca = make("Some other CA", ca=True)
    (tmp / "srv.pem").write_text(pem(srv))
    (tmp / "srv.key").write_bytes(srv_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))
    return pem(ca), pem(other_ca)


def test_tls_broker_with_ca_check():
    import ssl
    import tempfile

    async def run():
        tmp = pathlib.Path(tempfile.mkdtemp())
        ca_pem, other_pem = _test_pki(tmp)
        sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        sctx.load_cert_chain(tmp / "srv.pem", tmp / "srv.key")
        broker = MqttBroker()
        port = await broker.start(ssl=sctx)
        conn = MqttConnector()
        conn.secrets_dir = tmp
        (tmp / "ca").write_text(ca_pem)
        (tmp / "other-ca").write_text(other_pem)
        c = cfg(port)
        c["connection"].update(broker_url=f"mqtts://127.0.0.1:{port}", ca_cert_ref="ca")
        res = (await conn.test_connection(c))["devices"][0]
        assert res["ok"] and res["info"]["tls"] is True, res
        c["connection"]["ca_cert_ref"] = "other-ca"
        res = (await conn.test_connection(c))["devices"][0]
        assert res["ok"] is False and res["error"]["code"] == "TLS_FAILED"            # wrong CA: refused
        c["connection"]["insecure_skip_verify"] = True
        assert (await conn.test_connection(c))["devices"][0]["ok"]                     # only when explicitly allowed
        await broker.stop()
    asyncio.run(run())
