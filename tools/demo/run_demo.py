"""One-command demo of the Opcare DAQ platform: simulated substation meters -> Modbus connector -> platform -> browser.

    pip install starlette uvicorn jsonschema referencing
    python tools/demo/run_demo.py            # then open the printed URL

Everything runs in one process: the Modbus simulator (real TCP), the real Modbus connector inside the real
SDK runtime, the platform services, SQLite storage and the web API/GUI. The in-memory bus stands in for
Mosquitto, and the edge agent starts a connector for every configured source. Meter 2 of the substation drops
off the network every 90 s for 20 s (--no-faults turns this off), to show quality, state and event handling.

A second simulated device, "Pump Station West", is NOT configured at start: add it yourself on the
Sources > New source screen (the screen offers its address and tags) to try the configuration flow.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import pathlib
import random
import secrets
import socket
import json
import urllib.request
import webbrowser
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/modbus", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from modbus_connector import ModbusConnector  # noqa: E402
from modbus_sim import ModbusSimulator  # noqa: E402
from opcare_connector_sdk import SchemaValidator  # noqa: E402
from opcare_connector_sdk.testing import find_schemas  # noqa: E402

SITE, SOURCE = "ss-giza-220kv", "modbus-mcc-01"


PUMP_TAGS = [   # what the "Pump Station West" simulator offers; shown on the configuration screen
    {"name": "flow", "address": {"table": "holding_register", "address": 0, "encoding": "float32"}, "data_type": "float", "unit": "m3/h"},
    {"name": "discharge-pressure", "address": {"table": "holding_register", "address": 2, "encoding": "float32"}, "data_type": "float", "unit": "bar"},
    {"name": "reservoir-level", "address": {"table": "holding_register", "address": 4, "encoding": "uint16"}, "data_type": "float", "unit": "m",
     "scaling": {"gain": 0.01, "offset": 0}},
    {"name": "water-temp", "address": {"table": "holding_register", "address": 5, "encoding": "int16"}, "data_type": "float", "unit": "degC",
     "scaling": {"gain": 0.1, "offset": 0}},
    {"name": "pump-1-running", "address": {"table": "coil", "address": 0, "encoding": "bool"}, "data_type": "bool"},
    {"name": "pump-2-running", "address": {"table": "coil", "address": 1, "encoding": "bool"}, "data_type": "bool"},
    {"name": "run-hours", "address": {"table": "holding_register", "address": 10, "encoding": "uint32"}, "data_type": "int", "unit": "h"},
]


async def drive_pump_station(sim: ModbusSimulator) -> None:
    hours, t0 = 18_240, time.time()
    while True:
        t = time.time() - t0
        p2 = (t // 120) % 2 == 1                        # second pump joins every other two minutes
        flow = (310 if p2 else 165) + 12 * math.sin(t / 15) + random.uniform(-2, 2)
        sim.set_value(1, "holding_register", 0, ">f", flow)
        sim.set_value(1, "holding_register", 2, ">f", 4.2 + (0.6 if p2 else 0) + random.uniform(-0.05, 0.05))
        sim.set_regs(1, "holding_register", 4, [round((6.5 - 0.8 * math.sin(t / 300)) * 100), round((24 + math.sin(t / 200)) * 10)])
        sim.set_bit(1, "coil", 0, True)
        sim.set_bit(1, "coil", 1, p2)
        hours += 1 if random.random() < 0.01 else 0
        sim.set_value(1, "holding_register", 10, ">I", hours)
        await asyncio.sleep(1)


def pick_port(host: str, wanted: int) -> int:
    """Use the wanted port, or a free one if something else already uses it."""
    with socket.socket() as s:
        try:
            s.bind((host, wanted))
            return wanted
        except OSError:
            s.bind((host, 0))
            print(f"  Port {wanted} is busy; using {s.getsockname()[1]} instead.")
            return s.getsockname()[1]


def demo_config(port: int) -> dict:
    hr = lambda dev, addr, enc, **kw: {"device": dev, "table": "holding_register", "address": addr,  # noqa: E731
                                       "encoding": enc, **kw}
    pts = [
        ("ss-giza.mcc01.incomer.p-total", "float", "kW", hr("incomer", 0, "float32"), None),
        ("ss-giza.mcc01.incomer.q-total", "float", "kvar", hr("incomer", 2, "float32"), None),
        ("ss-giza.mcc01.incomer.voltage-l1", "float", "V", hr("incomer", 4, "uint16"), 0.1),
        ("ss-giza.mcc01.incomer.current-l1", "float", "A", hr("incomer", 5, "uint16"), 0.1),
        ("ss-giza.mcc01.incomer.frequency", "float", "Hz", hr("incomer", 6, "uint16"), 0.01),
        ("ss-giza.mcc01.incomer.pf", "float", None, hr("incomer", 7, "int16"), 0.001),
        ("ss-giza.mcc01.incomer.energy-import", "int", "kWh", hr("incomer", 10, "uint32"), None),
        ("ss-giza.mcc01.incomer.overcurrent-alarm", "bool", None, hr("incomer", 12, "bit", bit=0), None),
        ("ss-giza.mcc01.incomer.breaker-closed", "bool", None,
         {"device": "incomer", "table": "coil", "address": 0, "encoding": "bool"}, None),
        ("ss-giza.mcc01.feeder-2.p-total", "float", "kW", hr("feeder-2", 0, "float32"), None),
        ("ss-giza.mcc01.feeder-2.current-l1", "float", "A", hr("feeder-2", 5, "uint16"), 0.1),
        ("ss-giza.mcc01.tr1.oil-temp", "float", "degC",
         {"device": "tr1-relay", "table": "input_register", "address": 100, "encoding": "int16"}, 0.1),
        ("ss-giza.mcc01.tr1.winding-temp", "float", "degC",
         {"device": "tr1-relay", "table": "input_register", "address": 101, "encoding": "int16"}, 0.1),
    ]
    points = []
    for tag, dt, unit, addr, gain in pts:
        p = {"tag_id": tag, "data_type": dt, "address": addr}
        if unit:
            p["unit"] = unit
        if gain:
            p["scaling"] = {"gain": gain, "offset": 0}
        if dt == "float":
            p["deadband"] = {"mode": "abs", "value": 0.05}
        points.append(p)
    return {"site": SITE, "source_id": SOURCE, "type": "modbus", "name": "MCC-01 energy meters",
            "connection": {"transport": "tcp", "host": "127.0.0.1", "port": port,
                           "devices": [{"id": "incomer", "unit_id": 1, "name": "Incomer meter"},
                                       {"id": "feeder-2", "unit_id": 2, "name": "Feeder 2 meter"},
                                       {"id": "tr1-relay", "unit_id": 3, "name": "Transformer 1 relay"}]},
            "acquisition": {"default_scan_ms": 1000, "batch_max_values": 500, "batch_max_ms": 500,
                            "integrity_s": 300, "request_timeout_ms": 800},
            "points": points}


async def drive_simulator(sim: ModbusSimulator, faults: bool) -> None:
    """Plausible, slowly changing substation values."""
    energy, t0 = 1_250_000, time.time()
    while True:
        t = time.time() - t0
        p = 420 + 60 * math.sin(t / 40) + random.uniform(-4, 4)
        q = 0.32 * p + random.uniform(-2, 2)
        v = 231.0 + 1.5 * math.sin(t / 25) + random.uniform(-0.3, 0.3)
        i = p * 1000 / (3 * v * 0.95)
        energy += int(p / 3600) or 1
        sim.set_value(1, "holding_register", 0, ">f", p)
        sim.set_value(1, "holding_register", 2, ">f", q)
        sim.set_regs(1, "holding_register", 4, [round(v * 10), round(i * 10), round((50 + random.uniform(-0.03, 0.03)) * 100),
                                                round(0.95 * 1000) + random.randint(-5, 5) & 0xFFFF])
        sim.set_value(1, "holding_register", 10, ">I", energy)
        sim.set_regs(1, "holding_register", 12, [1 if i > 700 else 0])
        sim.set_bit(1, "coil", 0, True)
        p2 = 180 + 25 * math.sin(t / 30 + 1)
        sim.set_value(2, "holding_register", 0, ">f", p2)
        sim.set_regs(2, "holding_register", 5, [round(p2 * 1000 / (3 * v * 0.93) * 10)])
        sim.set_value(3, "input_register", 100, ">h", round((62 + 4 * math.sin(t / 120)) * 10))
        sim.set_value(3, "input_register", 101, ">h", round((71 + 5 * math.sin(t / 110)) * 10))
        if faults:
            phase = t % 90
            if 60 <= phase < 80:
                sim.offline.add(2)
            else:
                sim.offline.discard(2)
        await asyncio.sleep(1)


def _read_api(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


async def self_test(base: str, token: str, seconds: int) -> bool:
    """Pass when every configured tag has a Good value and the source is running."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        await asyncio.sleep(1)
        try:
            tags = await asyncio.to_thread(_read_api, f"{base}/api/tags", token)
            sources = await asyncio.to_thread(_read_api, f"{base}/api/sources", token)
        except OSError:
            continue
        good = [t for t in tags if t["q"] == 192]
        mcc = [s for s in sources if s["source_id"] == SOURCE]
        if tags and len(good) == len(tags) and mcc and mcc[0]["state"] == "running":
            print(f"  SELF-TEST PASS: {len(good)} live values with good quality, source running", flush=True)
            return True
    print(f"  SELF-TEST FAIL: no complete set of good values within {seconds} s", flush=True)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser(description="Opcare DAQ one-process demo")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=None, help="SQLite file (default: a temporary file)")
    ap.add_argument("--token", default=None, help="admin token (default: random)")
    ap.add_argument("--no-faults", action="store_true", help="do not take meter 2 offline periodically")
    ap.add_argument("--open", action="store_true", help="open the web browser when the demo is ready")
    ap.add_argument("--self-test", type=int, metavar="SECONDS", default=0,
                    help="check that live values arrive through the API, then exit (0 = pass); used by CI")
    args = ap.parse_args()
    args.port = pick_port(args.host, args.port)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="opcare-demo-"))
    token = args.token or secrets.token_urlsafe(18)
    tokens = {token: {"user": "khaled", "role": "admin"}, "viewer-demo": {"user": "control-room", "role": "viewer"}}

    sim = ModbusSimulator(strict=True)
    sim_port = await sim.start()
    sim_task = asyncio.create_task(drive_simulator(sim, not args.no_faults))
    await asyncio.sleep(0.2)

    pump = ModbusSimulator(strict=True)
    pump_port = await pump.start()
    pump_task = asyncio.create_task(drive_pump_station(pump))
    demo_info = {"site": "ps-west", "devices": [{"name": "Pump Station West", "device_id": "pump-plc", "host": "127.0.0.1",
                                                 "port": pump_port, "unit_id": 1, "tags": PUMP_TAGS}]}

    bus = InMemoryBus()
    store = Store(f"sqlite:///{args.db or tmp / 'daq.db'}")
    validator = SchemaValidator(find_schemas())
    platform = Platform(store, bus, validator, available_types={"modbus"})
    await platform.start()
    agent = LocalAgent(bus, {"modbus": ModbusConnector}, validator, edge_id="edge-demo")
    await agent.start()

    existing = await store.run(store.get_source, SOURCE)
    cfg = demo_config(sim_port)
    if existing:
        await platform.save_source(cfg, "demo-setup", create=False, source_id=SOURCE)
    else:
        await platform.save_source(cfg, "demo-setup", create=True)

    server = uvicorn.Server(uvicorn.Config(create_app(platform, tokens, demo_info=demo_info), host=args.host, port=args.port,
                                           log_level="warning", access_log=False))
    url = f"http://{args.host}:{args.port}/?token={token}"

    async def open_when_ready():
        while not server.started:
            await asyncio.sleep(0.1)
        print(f"\n  Ready. If the browser did not open, go to:\n  {url}\n", flush=True)
        if args.self_test:
            result["ok"] = await self_test(f"http://{args.host}:{args.port}", token, args.self_test)
            server.should_exit = True
        elif args.open:
            webbrowser.open(url)

    result = {"ok": False}
    opener = asyncio.create_task(open_when_ready())
    print(f"\n  Opcare DAQ demo starting\n  Open:  http://{args.host}:{args.port}/?token={token}\n"
          f"  API:   curl -H 'Authorization: Bearer {token}' http://{args.host}:{args.port}/api/tags\n"
          f"  Stop:  Ctrl+C\n", flush=True)
    try:
        await server.serve()
    finally:
        await agent.stop()
        sim_task.cancel()
        pump_task.cancel()
        await sim.stop()
        await pump.stop()
        await platform.stop()
        store.close()
    return 0 if (result["ok"] or not args.self_test) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
