"""SNMP agent simulator (v1, v2c, v3 with USM auth/priv) for connector tests and demos (Opcare DAQ).

Records every request type it receives so the conformance kit can prove that the connector only ever reads
(CT-13: no SetRequest). A SetRequest is answered with an error and never applied. Supports missing OIDs,
genErr, a maximum response size (tooBig), going silent (offline), restarting (engine boots) and sending
traps and informs to a manager.

    python tools/simulators/snmp_sim.py --port 10161 --community public
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import os
import pathlib
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "connectors" / "snmp"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "connectors" / "sdk-python"))

from snmp_connector.ber import (COUNTER32, COUNTER64, END_OF_MIB_VIEW, GAUGE32, GET, GET_BULK, GET_NEXT,  # noqa: E402
                                INFORM, INTEGER, NO_SUCH_INSTANCE, NO_SUCH_OBJECT, OCTET_STRING, OID, PDU_NAMES,
                                REPORT, RESPONSE, SET, TIMETICKS, TRAP_V1, TRAP_V2, BerError, Pdu, VarBind,
                                oid_tuple)
from snmp_connector.messages import (V1, V2C, V3, UsmUser, decode_community, decode_v3, decrypt_v3,  # noqa: E402
                                     encode_community, encode_v3, peek_version, verify_v3)

GEN_ERR = object()       # a MIB value that makes the agent answer genErr


class SnmpAgent:
    def __init__(self, community: str = "public", users: list[UsmUser] | None = None, engine_id: bytes | None = None,
                 allow_v1: bool = True, max_response_bytes: int = 65507):
        self.community = community.encode()
        self.users = {u.username.encode(): u for u in (users or [])}
        self.engine_id = engine_id or bytes.fromhex("80001f8880") + os.urandom(8)
        self.allow_v1 = allow_v1
        self.max_response_bytes = max_response_bytes
        self.mib: dict[str, tuple[int, Any]] = {}
        self._keys: list[str] = []
        self.log: list[str] = []                  # PDU names received, in order
        self.offline = False
        self.boots, self._t0 = 1, time.monotonic()
        self.time_offset = 0                      # tests: shift engine time to force notInTimeWindow
        self.transport = None
        self.port = 0
        self._informs: dict[int, asyncio.Future] = {}

    # ------------------------------------------------------------ MIB
    def set(self, oid: str, tag: int, value: Any) -> None:
        self.mib[oid.strip(".")] = (tag, value)
        self._keys = sorted(self.mib, key=oid_tuple)

    def remove(self, oid: str) -> None:
        self.mib.pop(oid, None)
        self._keys = sorted(self.mib, key=oid_tuple)

    def value(self, oid: str) -> tuple[int, Any]:
        tag, v = self.mib[oid]
        return tag, (v() if callable(v) else v)

    def next_oid(self, oid: str) -> str | None:
        keys = [oid_tuple(k) for k in self._keys]
        i = bisect.bisect_right(keys, oid_tuple(oid))
        return self._keys[i] if i < len(self._keys) else None

    @property
    def engine_time(self) -> int:
        return int(time.monotonic() - self._t0) + self.time_offset

    def restart(self) -> None:
        self.boots += 1
        self._t0 = time.monotonic()

    # ------------------------------------------------------------ network
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        loop = asyncio.get_running_loop()
        agent = self

        class P(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                agent._on(data, addr)
        self.transport, _ = await loop.create_datagram_endpoint(P, local_addr=(host, port))
        self.port = self.transport.get_extra_info("sockname")[1]
        return self.port

    def stop(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None

    def _on(self, data: bytes, addr) -> None:
        if self.offline:
            return
        try:
            version = peek_version(data)
            if version == V3:
                out = self._v3(data)
            else:
                out = self._community(data, version)
        except (BerError, IndexError, ValueError):
            return
        if out:
            self.transport.sendto(out, addr)

    # ------------------------------------------------------------ v1 / v2c
    def _community(self, data: bytes, version: int) -> bytes | None:
        if version == V1 and not self.allow_v1:
            return None
        _, community, pdu = decode_community(data)
        if pdu.tag == RESPONSE and pdu.request_id in self._informs:
            self.log.append("Response")
            fut = self._informs.pop(pdu.request_id)
            if not fut.done():
                fut.set_result(True)
            return None
        self.log.append(pdu.name)
        if community != self.community:
            return None                           # wrong community: silently dropped (RFC 3584)
        resp = self.process(pdu, version)
        out = encode_community(version, self.community, resp)
        if len(out) > self.max_response_bytes:
            out = encode_community(version, self.community, Pdu(RESPONSE, pdu.request_id, 1, 0, pdu.varbinds))
        return out

    def process(self, pdu: Pdu, version: int) -> Pdu:
        rid = pdu.request_id
        if pdu.tag == SET:                        # never applied; answered as not writable
            return Pdu(RESPONSE, rid, 4 if version == V1 else 17, 1, pdu.varbinds)
        if pdu.tag == GET_BULK and version == V1:
            return Pdu(RESPONSE, rid, 5, 0, pdu.varbinds)
        out: list[VarBind] = []
        try:
            if pdu.tag == GET:
                for i, vb in enumerate(pdu.varbinds):
                    if vb.oid in self.mib:
                        out.append(self._vb(vb.oid, i + 1))
                    elif version == V1:
                        return Pdu(RESPONSE, rid, 2, i + 1, pdu.varbinds)
                    else:
                        parent = vb.oid.rsplit(".", 1)[0]
                        has_obj = any(k.startswith(parent + ".") for k in self.mib)
                        out.append(VarBind(vb.oid, NO_SUCH_INSTANCE if has_obj else NO_SUCH_OBJECT))
            elif pdu.tag == GET_NEXT:
                for i, vb in enumerate(pdu.varbinds):
                    nxt = self.next_oid(vb.oid)
                    if nxt is None:
                        if version == V1:
                            return Pdu(RESPONSE, rid, 2, i + 1, pdu.varbinds)
                        out.append(VarBind(vb.oid, END_OF_MIB_VIEW))
                    else:
                        out.append(self._vb(nxt, i + 1))
            elif pdu.tag == GET_BULK:
                nr, mr = pdu.error_status, pdu.error_index
                for vb in pdu.varbinds[:nr]:
                    nxt = self.next_oid(vb.oid)
                    out.append(self._vb(nxt) if nxt else VarBind(vb.oid, END_OF_MIB_VIEW))
                cur = [vb.oid for vb in pdu.varbinds[nr:]]
                for _ in range(mr):
                    if not cur:
                        break
                    row = []
                    for o in cur:
                        nxt = self.next_oid(o)
                        out.append(self._vb(nxt) if nxt else VarBind(o, END_OF_MIB_VIEW))
                        row.append(nxt or o)
                    if all(self.next_oid(o) is None for o in cur):
                        break
                    cur = row
            else:
                return Pdu(RESPONSE, rid, 5, 0, pdu.varbinds)
        except _GenErr as e:
            return Pdu(RESPONSE, rid, 5, e.index, pdu.varbinds)
        return Pdu(RESPONSE, rid, 0, 0, out)

    def _vb(self, oid: str, index: int = 1) -> VarBind:
        tag, v = self.value(oid)
        if v is GEN_ERR:
            raise _GenErr(index)
        return VarBind(oid, tag, v)

    # ------------------------------------------------------------ v3 / USM
    def _report(self, m, stat_oid: str, count: int = 1, user: UsmUser | None = None, keys=(None, None)) -> bytes:
        pdu = Pdu(REPORT, m.pdu.request_id if m.pdu else 0, 0, 0, [VarBind(stat_oid, COUNTER32, count)])
        return encode_v3(m.msg_id, pdu, user, self.engine_id, self.boots, self.engine_time, keys, self.engine_id,
                         reportable=False)

    def _v3(self, data: bytes) -> bytes | None:
        m, whole = decode_v3(data)
        user = self.users.get(m.username)
        if not m.engine_id or m.engine_id != self.engine_id:
            if m.pdu is not None:
                self.log.append(m.pdu.name)
            return self._report(m, "1.3.6.1.6.3.15.1.1.4.0")              # unknown engine id -> discovery
        if user is None:
            return self._report(m, "1.3.6.1.6.3.15.1.1.3.0")
        keys = user.keys(self.engine_id)
        want_auth, want_priv = bool(m.flags & 1), bool(m.flags & 2)
        if want_auth != user.auth or want_priv != user.priv:
            return self._report(m, "1.3.6.1.6.3.15.1.1.1.0")
        if user.auth:
            if not verify_v3(m, whole, user, keys[0]):
                return self._report(m, "1.3.6.1.6.3.15.1.1.5.0")
            if m.boots != self.boots or abs(m.time - self.engine_time) > 150:
                return self._report(m, "1.3.6.1.6.3.15.1.1.2.0", user=_auth_only(user), keys=keys)
        if user.priv:
            try:
                decrypt_v3(m, keys[1])
            except (BerError, ValueError, IndexError):
                return self._report(m, "1.3.6.1.6.3.15.1.1.6.0")
        self.log.append(m.pdu.name)
        resp = self.process(m.pdu, V2C)
        return encode_v3(m.msg_id, resp, user, self.engine_id, self.boots, self.engine_time, keys, self.engine_id,
                         m.context_name, reportable=False)

    # ------------------------------------------------------------ notifications
    def send_trap(self, host: str, port: int, trap_oid: str, varbinds: list[VarBind] | None = None,
                  version: int = V2C, community: bytes | None = None) -> None:
        com = self.community if community is None else community
        if version == V1:
            # linkDown etc. as generic traps; anything else enterprise-specific
            generic = {"1.3.6.1.6.3.1.1.5.3": 2, "1.3.6.1.6.3.1.1.5.4": 3, "1.3.6.1.6.3.1.1.5.1": 0}.get(trap_oid, 6)
            parts = trap_oid.split(".")
            ent = ".".join(parts[:-2] if parts[-2] == "0" else parts[:-1])        # RFC 3584: enterprise.0.specific
            pdu = Pdu(TRAP_V1, 0, enterprise="1.3.6.1.4.1.99999" if generic != 6 else ent,
                      agent_addr="127.0.0.1", generic_trap=generic, specific_trap=int(parts[-1]) if generic == 6 else 0,
                      timestamp=self.engine_time * 100, varbinds=varbinds or [])
        else:
            vbs = [VarBind("1.3.6.1.2.1.1.3.0", TIMETICKS, self.engine_time * 100),
                   VarBind("1.3.6.1.6.3.1.1.4.1.0", OID, trap_oid)] + (varbinds or [])
            pdu = Pdu(TRAP_V2, int.from_bytes(os.urandom(3), "big"), 0, 0, vbs)
        self.transport.sendto(encode_community(version, com, pdu), (host, port))

    async def send_inform(self, host: str, port: int, trap_oid: str, timeout: float = 2.0) -> bool:
        rid = int.from_bytes(os.urandom(3), "big")
        vbs = [VarBind("1.3.6.1.2.1.1.3.0", TIMETICKS, self.engine_time * 100),
               VarBind("1.3.6.1.6.3.1.1.4.1.0", OID, trap_oid)]
        fut = asyncio.get_running_loop().create_future()
        self._informs[rid] = fut
        self.transport.sendto(encode_community(V2C, self.community, Pdu(INFORM, rid, 0, 0, vbs)), (host, port))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False


class _GenErr(Exception):
    def __init__(self, index: int):
        self.index = index


def _auth_only(user: UsmUser) -> UsmUser:
    """notInTimeWindow reports are authenticated but not encrypted (RFC 3414 3.2 step 7)."""
    return UsmUser(user.username, "authNoPriv", user.auth_protocol, user.auth_password)


# ------------------------------------------------------------------ ready-made devices
def standard_mib(agent: SnmpAgent, name: str, descr: str, interfaces: int = 4,
                 traffic: Callable[[int], int] | None = None) -> None:
    """System group, IF-MIB table and 64-bit counters: what every switch or server answers."""
    t0 = time.monotonic()
    agent.set("1.3.6.1.2.1.1.1.0", OCTET_STRING, descr.encode())
    agent.set("1.3.6.1.2.1.1.2.0", OID, "1.3.6.1.4.1.99999.1")
    agent.set("1.3.6.1.2.1.1.3.0", TIMETICKS, lambda: int((time.monotonic() - t0) * 100) + 8640000)
    agent.set("1.3.6.1.2.1.1.5.0", OCTET_STRING, name.encode())
    agent.set("1.3.6.1.2.1.1.6.0", OCTET_STRING, b"Server room")
    agent.set("1.3.6.1.2.1.2.1.0", INTEGER, interfaces)
    for i in range(1, interfaces + 1):
        agent.set(f"1.3.6.1.2.1.2.2.1.1.{i}", INTEGER, i)
        agent.set(f"1.3.6.1.2.1.2.2.1.2.{i}", OCTET_STRING, f"GigabitEthernet1/0/{i}".encode())
        agent.set(f"1.3.6.1.2.1.2.2.1.5.{i}", GAUGE32, 1000000000)
        agent.set(f"1.3.6.1.2.1.2.2.1.7.{i}", INTEGER, 1)
        agent.set(f"1.3.6.1.2.1.2.2.1.8.{i}", INTEGER, 1)
        rate = (lambda k: (lambda: int((time.monotonic() - t0) * (traffic(k) if traffic else 1250000 * k))))(i)
        out = (lambda f: lambda: int(f() * 0.6))(rate)
        agent.set(f"1.3.6.1.2.1.2.2.1.10.{i}", COUNTER32, (lambda f: lambda: f() % (1 << 32))(rate))
        agent.set(f"1.3.6.1.2.1.2.2.1.14.{i}", COUNTER32, (lambda k: lambda: int((time.monotonic() - t0) / (20 * k)))(i))
        agent.set(f"1.3.6.1.2.1.2.2.1.16.{i}", COUNTER32, (lambda f: lambda: f() % (1 << 32))(out))
        agent.set(f"1.3.6.1.2.1.31.1.1.1.1.{i}", OCTET_STRING, f"Gi1/0/{i}".encode())
        agent.set(f"1.3.6.1.2.1.31.1.1.1.6.{i}", COUNTER64, rate)
        agent.set(f"1.3.6.1.2.1.31.1.1.1.10.{i}", COUNTER64, out)
        agent.set(f"1.3.6.1.2.1.31.1.1.1.15.{i}", GAUGE32, 1000)


def ups_mib(agent: SnmpAgent) -> None:
    """UPS-MIB (RFC 1628) essentials."""
    t0 = time.monotonic()
    agent.set("1.3.6.1.2.1.33.1.1.1.0", OCTET_STRING, b"Opcare Demo UPS")
    agent.set("1.3.6.1.2.1.33.1.2.1.0", INTEGER, 2)                                  # batteryNormal
    agent.set("1.3.6.1.2.1.33.1.2.3.0", INTEGER, 42)                                 # minutes remaining
    agent.set("1.3.6.1.2.1.33.1.2.4.0", INTEGER, lambda: 100 - int((time.monotonic() - t0) / 60) % 5)
    agent.set("1.3.6.1.2.1.33.1.2.7.0", INTEGER, lambda: 24 + int((time.monotonic() - t0) / 30) % 3)  # temp degC
    agent.set("1.3.6.1.2.1.33.1.3.3.1.3.1", INTEGER, 230)                            # input voltage
    agent.set("1.3.6.1.2.1.33.1.4.1.0", INTEGER, 3)                                  # output source: normal
    agent.set("1.3.6.1.2.1.33.1.4.4.1.5.1", INTEGER, lambda: 35 + int(5 * abs(((time.monotonic() - t0) / 20) % 2 - 1)))


async def _main() -> None:
    ap = argparse.ArgumentParser(description="SNMP agent simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10161)
    ap.add_argument("--community", default="public")
    args = ap.parse_args()
    agent = SnmpAgent(args.community)
    standard_mib(agent, "sim-switch", "Opcare simulated switch")
    ups_mib(agent)
    port = await agent.start(args.host, args.port)
    print(f"SNMP agent on udp {args.host}:{port}, community '{args.community}'", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

__all__ = ["SnmpAgent", "standard_mib", "ups_mib", "GEN_ERR", "PDU_NAMES", "COUNTER64", "INFORM", "GET", "GET_NEXT",
           "GET_BULK", "RESPONSE"]
