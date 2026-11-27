"""UDP connector tests: parsing unit tests and end-to-end runs over real UDP sockets on localhost.

Run from the repository root:  python -m pytest connectors/udp/tests -q
"""
import asyncio
import json
import pathlib
import socket
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in ("connectors/sdk-python", "connectors/udp", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

from opcare_connector_sdk import ALLOW, ConnectorError, Q  # noqa: E402
from opcare_connector_sdk.testing import Harness  # noqa: E402
from udp_connector import UdpConnector  # noqa: E402
from udp_connector import parse  # noqa: E402
from udp_connector.parse import ParseError  # noqa: E402

SITE = "site-a"


def nmea_line(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}"


# ===================================================================== parsing
def test_nmea_checksum_fields_and_coordinates():
    line = nmea_line("GPRMC,123519,A,3001.2345,N,03114.5678,E,5.5,084.4,230394,003.1,W")
    kind, fields = parse.nmea(line)
    assert kind == "RMC" and fields[1] == "A" and fields[6] == "5.5"
    assert parse.nmea_coordinate(fields[2], fields[3]) == pytest.approx(30.020575)
    assert parse.nmea_coordinate("03114.5678", "W") == pytest.approx(-31.24279667)
    with pytest.raises(ParseError):
        parse.nmea(line[:-2] + "00")                              # wrong checksum
    assert parse.nmea("hello") is None
    assert parse.nmea("$PGRMZ,1234,f,3")[0] == "PGRMZ"           # proprietary sentence keeps its full name


def test_json_pointer_and_timestamps():
    doc = {"wind": {"speed": 4.2, "dir": [10, 20]}, "a/b": {"~x": 1}, "ts": 1764240000000}
    assert parse.json_pointer(doc, "/wind/speed") == 4.2
    assert parse.json_pointer(doc, "/wind/dir/1") == 20
    assert parse.json_pointer(doc, "/a~1b/~0x") == 1
    with pytest.raises(ParseError):
        parse.json_pointer(doc, "/wind/gust")
    assert parse.json_timestamp(doc["ts"], "epoch_ms") == "2025-11-27T10:40:00.000Z"
    assert parse.json_timestamp("2026-11-27T12:00:00+02:00", "rfc3339") == "2026-11-27T10:00:00.000Z"
    assert dict(parse.json_leaves(doc))["/wind/dir/0"] == 10


def test_binary_layouts():
    data = b"ST" + struct.pack(">Hf", 7, 12.5) + bytes([0b00000100]) + b"PUMP\x00\x00" + struct.pack("<i", -3)
    assert parse.binary_value(data, {"offset": 2, "encoding": "uint16"}) == 7
    assert parse.binary_value(data, {"offset": 4, "encoding": "float32"}) == 12.5
    assert parse.binary_value(data, {"offset": 8, "encoding": "bit", "bit": 2}) is True
    assert parse.binary_value(data, {"offset": 9, "encoding": "string", "length": 6}) == "PUMP"
    assert parse.binary_value(data, {"offset": 15, "encoding": "int32", "byte_order": "little"}) == -3
    with pytest.raises(ParseError):
        parse.binary_value(data, {"offset": 17, "encoding": "float64"})


def test_syslog_both_formats():
    m = parse.syslog("<165>1 2026-11-27T10:00:00.000Z core-sw-1 ifmgr 812 LINK - Interface Gi1/0/3 down")
    assert m == {"severity": 5, "facility": 20, "hostname": "core-sw-1", "app_name": "ifmgr",
                 "message": "Interface Gi1/0/3 down"}
    m = parse.syslog("<12>Nov 27 10:00:00 fw-1 kernel[42]: blocked 10.0.0.9")
    assert (m["severity"], m["hostname"], m["app_name"], m["message"]) == (4, "fw-1", "kernel", "blocked 10.0.0.9")
    assert parse.syslog("<190>plain text")["message"] == "plain text"
    assert parse.syslog("no priority") is None


def test_coerce_and_detect():
    assert parse.coerce("12", "int") == 12 and parse.coerce("12.0", "int") == 12 and parse.coerce(" 4.5 ", "float") == 4.5
    assert parse.coerce("A", "bool") is True and parse.coerce("off", "bool") is False
    assert parse.coerce({"a": 1}, "string") == '{"a":1}'
    with pytest.raises(ParseError):
        parse.coerce("n/a", "float")
    assert parse.detect(b'{"t": 1}') == "json"
    assert parse.detect(nmea_line("GPGGA,1,2").encode()) == "nmea"
    assert parse.detect(b"WX,1,2,3\r\n") == "delimited"
    assert parse.detect(b"<14>hello") == "syslog"
    assert parse.detect(bytes(range(20))) == "binary"
    assert parse.detect(b"42") == "raw"


def test_guard_allows_no_requests_at_all():
    assert ALLOW["udp"] == frozenset()
    with pytest.raises(ConnectorError) as e:
        UdpConnector().guard.check("SEND")
    assert e.value.code == "OP_FORBIDDEN"


# ===================================================================== end to end over real UDP
def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sender:
    def __init__(self, port: int, source: str = "127.0.0.1"):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((source, 0))

    def send(self, data) -> None:
        self.sock.sendto(data.encode() if isinstance(data, str) else data, ("127.0.0.1", self.port))

    def close(self):
        self.sock.close()


def udp_config(port: int, rev: int = 1, silence: int = 2, syslog: bool = True) -> dict:
    conn = {"bind_host": "127.0.0.1", "bind_port": port, "silence_timeout_s": silence,
            "senders": [{"id": "gnss", "host": "127.0.0.1", "name": "GNSS"}]}
    if syslog:
        conn["syslog"] = {"min_severity": "warning"}
    pts = [
        {"tag_id": "site-a.gnss.lat", "address": {"device": "gnss", "format": "nmea", "sentence": "RMC", "field": 3, "coordinate": True},
         "data_type": "float", "unit": "deg"},
        {"tag_id": "site-a.gnss.speed", "address": {"device": "gnss", "format": "nmea", "sentence": "RMC", "field": 7},
         "data_type": "float", "scaling": {"gain": 0.514444, "offset": 0}},
        {"tag_id": "site-a.wx.wind", "address": {"device": "gnss", "format": "json", "value_pointer": "/wind/speed",
         "timestamp_pointer": "/ts", "timestamp_format": "epoch_ms", "match_pointer": "/id", "match_value": "ws-1"}, "data_type": "float"},
        {"tag_id": "site-a.wx.temp", "address": {"device": "gnss", "format": "delimited", "prefix": "WX,", "field": 2}, "data_type": "float"},
        {"tag_id": "site-a.plc.count", "address": {"device": "gnss", "format": "binary", "prefix_hex": "5354", "offset": 2,
         "encoding": "uint32"}, "data_type": "int"},
        {"tag_id": "site-a.plc.running", "address": {"device": "gnss", "format": "binary", "prefix_hex": "5354", "offset": 6,
         "encoding": "bit", "bit": 0}, "data_type": "bool"},
        {"tag_id": "site-a.net.syslog-sev", "address": {"device": "gnss", "format": "syslog", "field": "severity"}, "data_type": "int"},
    ]
    return {"spec": "1.0", "revision": rev, "site": SITE, "source_id": "src-test", "type": "udp", "name": "UDP test",
            "enabled": True, "connection": conn,
            "acquisition": {"batch_max_ms": 100, "integrity_s": 0, "request_timeout_ms": 3000}, "points": pts}


def test_end_to_end_formats_quality_silence_and_schemas():
    async def run():
        port = free_udp_port()
        tx = Sender(port)
        try:
            stranger = Sender(port, "127.0.0.2")          # macOS has only 127.0.0.1 unless configured
        except OSError:
            stranger = None
        async with Harness(UdpConnector(), site=SITE) as h:
            h.config(udp_config(port))
            await h.wait_for(lambda: h.rt.state == "connecting", what="connecting")
            await asyncio.sleep(0.2)
            tx.send(nmea_line("GPRMC,123519,A,3001.2345,N,03114.5678,E,10.0,084.4,230394,003.1,W") + "\r\n" +
                    nmea_line("GPGGA,123519,3001.2345,N,03114.5678,E,1,08,0.9,545.4,M,46.9,M,,"))
            tx.send(json.dumps({"id": "ws-2", "wind": {"speed": 99}}))          # other station: ignored
            tx.send(json.dumps({"id": "ws-1", "ts": 1764240000000, "wind": {"speed": 4.25}}, indent=2))
            tx.send("WX,ws-1,23.5,61")
            tx.send(b"ST" + struct.pack(">I", 1234) + bytes([1]))
            tx.send("<12>Nov 27 10:00:00 fw-1 kernel: port scan from 10.0.0.9")    # warning -> event
            tx.send("<14>Nov 27 10:00:01 fw-1 kernel: just info")                  # info -> no event
            if stranger:
                stranger.send("WX,ws-1,-50,0")                                     # unknown sender: dropped
            await h.wait_for(lambda: len(h.mq.latest()) >= 7, what="7 tags")
            v = h.mq.latest()
            assert v["site-a.gnss.lat"]["v"] == pytest.approx(30.020575) and v["site-a.gnss.lat"]["q"] == 192
            assert v["site-a.gnss.speed"]["v"] == pytest.approx(5.14444)
            assert v["site-a.wx.wind"]["v"] == 4.25 and v["site-a.wx.wind"]["ts"] == "2025-11-27T10:40:00.000Z"
            assert v["site-a.wx.temp"]["v"] == 23.5
            assert v["site-a.plc.count"]["v"] == 1234 and v["site-a.plc.running"]["v"] is True
            assert v["site-a.net.syslog-sev"]["v"] == 6
            assert h.rt.state == "running"
            ev = [e for e in h.mq.msgs("event") if e["type"] == "syslog.message"]
            assert len(ev) == 1 and ev[0]["severity"] == "medium" and ev[0]["device"] == "gnss"
            assert ev[0]["attrs"]["hostname"] == "fw-1" and "port scan" in ev[0]["message"]

            tx.send("WX,ws-1,not-a-number")                                       # matches the prefix, bad value
            await h.wait_for(lambda: h.mq.latest()["site-a.wx.temp"]["q"] == 0, what="parse error")
            assert h.mq.latest()["site-a.wx.temp"]["f"] == ["PARSE_ERROR"]

            diag = await h.admin("get_diagnostics")
            assert diag["result"]["connector"]["dropped_unknown_sender"] == (1 if stranger else 0)

            await h.wait_for(lambda: h.rt.state == "error", timeout=5, what="silence -> unreachable")
            await h.wait_for(lambda: all(x["q"] == 24 for x in h.mq.latest().values()), what="q 24 for every tag")
            assert any(e["type"] == "conn.lost" for e in h.mq.msgs("event"))
            tx.send("WX,ws-1,24.0,60")                                             # back again
            await h.wait_for(lambda: h.mq.latest()["site-a.wx.temp"]["q"] == 192, what="recovery")
            await h.wait_for(lambda: h.rt.state == "running", what="running again")
            assert h.schema_errors() == []
        tx.close()
        if stranger:
            stranger.close()
    asyncio.run(run())


def test_test_connection_and_browse_without_a_running_source():
    async def run():
        port = free_udp_port()
        tx = Sender(port)
        conn = UdpConnector()
        cfg = udp_config(port)

        async def chatter():
            for _ in range(40):
                tx.send(json.dumps({"id": "ws-1", "wind": {"speed": 3.5, "dir": 270}, "ok": True}))
                await asyncio.sleep(0.05)
        task = asyncio.create_task(chatter())
        res = await conn.test_connection(cfg)
        assert res["devices"][0]["ok"] is True and res["devices"][0]["info"]["format"] == "json"
        b = await conn.browse({"config": cfg, "listen_s": 3, "depth": 2})
        await task
        items = {i["path"]: i for i in b["items"]}
        assert items["127.0.0.1"]["info"]["format"] == "json" and items["127.0.0.1"]["info"]["device"] == "gnss"
        var = items["127.0.0.1//wind/speed"]
        assert var["kind"] == "variable" and var["data_type"] == "float"
        assert var["address"] == {"device": "gnss", "format": "json", "value_pointer": "/wind/speed"}

        silent = await conn.test_connection({**cfg, "acquisition": {"request_timeout_ms": 1000}})
        assert silent["devices"][0]["ok"] is False and silent["devices"][0]["error"]["code"] == "CONN_TIMEOUT"

        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))                       # port taken: cannot listen
        busy = await conn.test_connection(cfg)
        blocker.close()
        assert busy["devices"][0]["error"]["code"] == "CONN_REFUSED"
        tx.close()
    asyncio.run(run())


def test_validation_catches_mistakes():
    async def run():
        cfg = udp_config(5514)
        cfg["connection"]["senders"].append({"id": "gnss", "host": "300.1.1.1"})
        cfg["points"].append({"tag_id": "x.y", "address": {"device": "nobody", "format": "binary", "offset": 0, "encoding": "float32"},
                              "data_type": "bool"})
        cfg["points"].append({"tag_id": "x.z", "address": {"device": "gnss", "format": "syslog", "field": "message", "match": "("},
                              "data_type": "string"})
        errs = await UdpConnector().validate(cfg)
        text = " | ".join(errs)
        for needle in ("duplicate sender id", "not an IP address", "unknown sender 'nobody'", "is a number",
                       "regular expression"):
            assert needle in text, needle
    asyncio.run(run())
