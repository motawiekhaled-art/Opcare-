"""Interoperability with the real net-snmp agent and tools (CI job "SNMP interop", .github/workflows/ci.yml).

Skipped unless OPCARE_NETSNMP_PORT is set. CI starts snmpd on 127.0.0.1 with community `opcare-ci` and three
SNMPv3 users (authPriv, AES) with SHA-256, SHA and MD5, then runs this file. It proves that the connector's own
BER/USM code talks to a widely deployed agent, not only to our simulator, and that net-snmp's snmptrap and
snmpinform reach the trap receiver.
"""
import asyncio
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/snmp"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk.testing import Harness  # noqa: E402
from snmp_connector import SnmpConnector  # noqa: E402

PORT = int(os.environ.get("OPCARE_NETSNMP_PORT", "0"))
pytestmark = pytest.mark.skipif(not PORT, reason="needs a net-snmp agent (set OPCARE_NETSNMP_PORT)")
SECRETS = {"community": "opcare-ci", "auth": "auth-pass-123", "priv": "priv-pass-456", "wrong": "not-the-password"}


def cfg(version="2c", user=None, auth="SHA-256", auth_ref="auth", trap_port=None, points=None):
    conn = {"version": version, "port": PORT, "retries": 1, "devices": [{"id": "agent", "host": "127.0.0.1"}]}
    if version == "3":
        conn["v3"] = {"username": user, "security_level": "authPriv", "auth_protocol": auth, "auth_password_ref": auth_ref,
                      "priv_protocol": "AES", "priv_password_ref": "priv"}
    else:
        conn["community_ref"] = "community"
    if trap_port:
        conn["traps"] = {"bind_host": "127.0.0.1", "bind_port": trap_port, "community_ref": "community"}
    return {"spec": "1.0", "revision": 1, "site": "site-a", "source_id": "src-test", "type": "snmp", "name": "net-snmp",
            "enabled": True, "connection": conn,
            "acquisition": {"default_scan_ms": 1000, "batch_max_ms": 100, "integrity_s": 0, "request_timeout_ms": 1000},
            "points": points or [
                {"tag_id": "site-a.agent.name", "address": {"device": "agent", "oid": "1.3.6.1.2.1.1.5.0"}, "data_type": "string"},
                {"tag_id": "site-a.agent.uptime", "address": {"device": "agent", "oid": "1.3.6.1.2.1.1.3.0"}, "data_type": "float",
                 "scaling": {"gain": 0.01, "offset": 0}},
                {"tag_id": "site-a.agent.lo-up", "address": {"device": "agent", "oid": "1.3.6.1.2.1.2.2.1.8.1"}, "data_type": "bool"},
                {"tag_id": "site-a.agent.lo-in", "address": {"device": "agent", "oid": "1.3.6.1.2.1.31.1.1.1.6.1", "transform": "rate"},
                 "data_type": "float"},
                {"tag_id": "site-a.agent.nothing", "address": {"device": "agent", "oid": "1.3.6.1.2.1.1.99.0"}, "data_type": "int"}]}


def secrets_dir() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    for k, v in SECRETS.items():
        (d / k).write_text(v)
    return d


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("version,user,auth", [("2c", None, None), ("1", None, None), ("3", "opcare-sha256", "SHA-256"),
                                               ("3", "opcare-sha", "SHA"), ("3", "opcare-md5", "MD5")])
def test_test_connection_and_browse_against_snmpd(version, user, auth):
    async def run():
        c = SnmpConnector()
        c.secrets_dir = secrets_dir()
        conf = cfg(version, user, auth or "SHA-256")
        res = (await c.test_connection(conf))["devices"][0]
        assert res["ok"], res
        assert res["info"]["sysName"] == "opcare-ci-agent"
        b = await c.browse({"config": conf, "path": "agent/1.3.6.1.2.1.1", "max_items": 50})
        names = [i["name"] for i in b["items"]]
        assert "sysDescr.0" in names and "sysName.0" in names
    asyncio.run(run())


def test_wrong_v3_password_is_auth_failed():
    async def run():
        c = SnmpConnector()
        c.secrets_dir = secrets_dir()
        res = (await c.test_connection(cfg("3", "opcare-sha256", auth_ref="wrong")))["devices"][0]
        assert res["ok"] is False and res["error"]["code"] in ("AUTH_FAILED", "CONN_TIMEOUT"), res
    asyncio.run(run())


@pytest.mark.parametrize("version,user", [("2c", None), ("3", "opcare-sha256")])
def test_polling_against_snmpd(version, user):
    async def run():
        async with Harness(SnmpConnector(), site="site-a", secrets=SECRETS) as h:
            h.config(cfg(version, user))
            await h.wait_for(lambda: "site-a.agent.lo-in" in h.mq.latest(), timeout=10, what="values from snmpd")
            v = h.mq.latest()
            assert v["site-a.agent.name"]["v"] == "opcare-ci-agent"
            assert v["site-a.agent.uptime"]["q"] == 192 and v["site-a.agent.lo-up"]["v"] is True
            assert v["site-a.agent.lo-in"]["q"] == 192 and v["site-a.agent.lo-in"]["v"] >= 0
            assert v["site-a.agent.nothing"]["q"] == 4
            assert h.rt.state == "running" and h.schema_errors() == []
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("snmptrap"), reason="net-snmp tools not installed")
def test_traps_and_informs_from_net_snmp_tools():
    async def run():
        trap_port = free_udp_port()
        async with Harness(SnmpConnector(), site="site-a", secrets=SECRETS) as h:
            h.config(cfg(trap_port=trap_port))
            await h.wait_for(lambda: h.rt.state == "running", timeout=10, what="running")
            target = f"127.0.0.1:{trap_port}"
            subprocess.run(["snmptrap", "-v", "2c", "-c", "opcare-ci", target, "", "1.3.6.1.6.3.1.1.5.3",
                            "1.3.6.1.2.1.2.2.1.1.2", "i", "2"], check=True, timeout=10)
            subprocess.run(["snmptrap", "-v", "1", "-c", "opcare-ci", target, "1.3.6.1.4.1.8072", "127.0.0.1", "0", "0", ""],
                           check=True, timeout=10)
            inform = await asyncio.to_thread(subprocess.run, ["snmpinform", "-v", "2c", "-c", "opcare-ci", "-r", "1", "-t", "2",
                                                              target, "", "1.3.6.1.6.3.1.1.5.4"], timeout=15)
            assert inform.returncode == 0                  # snmpinform fails unless our Response acknowledged it
            await h.wait_for(lambda: len([e for e in h.mq.msgs("event") if e["type"] == "snmp.trap"]) >= 3, what="3 traps")
            got = {e["attrs"]["trap"] for e in h.mq.msgs("event") if e["type"] == "snmp.trap"}
            assert {"linkDown", "coldStart", "linkUp"} <= got
    asyncio.run(run())
