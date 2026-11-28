"""Asyncio MMS client for IEC 61850 servers (8-1 mapping): association with optional ACSE password and TLS, confirmed
requests with invoke ids, InformationReports passed to a callback.

The guard (spec 12 and 15.4) checks every request by MMS service name before it is encoded; a Write is checked per
variable as WRITE_RCB:<attribute>, and only report-control-block attributes of BR/RP control blocks pass.
"""
from __future__ import annotations

import asyncio
import re
import ssl

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

from . import mms as M
from .iso import IsoError, IsoLink

RCB_ITEM = re.compile(r"^[A-Za-z0-9_]+\$(BR|RP)\$[A-Za-z0-9_]+\$([A-Za-z]+)$")
SERVICE_NAMES = {M.SVC_GET_NAME_LIST: "GetNameList", M.SVC_READ: "Read", M.SVC_GET_VAR_ATTR: "GetVariableAccessAttributes",
                 M.SVC_GET_NVL_ATTR: "GetNamedVariableListAttributes"}


class MmsClient:
    def __init__(self, host: str, port: int, guard: ReadOnlyGuard, timeout: float = 10.0,
                 tls: ssl.SSLContext | None = None, password: str | None = None, on_report=None):
        self.host, self.port, self.guard, self.timeout = host, port, guard, timeout
        self.tls, self.password, self.on_report = tls, password, on_report
        self.link: IsoLink | None = None
        self.writer = None
        self.invoke = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.task = None
        self.closed = asyncio.Event()
        self.close_reason: ConnectorError | None = None
        self.stats = {"requests": 0, "reports": 0, "errors": 0}

    async def connect(self) -> None:
        self.guard.check("Initiate")
        try:
            reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self.tls, server_hostname=self.host if self.tls else None),
                self.timeout)
        except asyncio.TimeoutError:
            raise ConnectorError("CONN_TIMEOUT", f"no answer from {self.host}:{self.port}") from None
        except ssl.SSLError as exc:
            raise ConnectorError("TLS_FAILED", f"TLS handshake with {self.host}:{self.port} failed: {exc.reason or exc}") from None
        except OSError as exc:
            raise ConnectorError("CONN_REFUSED", f"cannot connect to {self.host}:{self.port}: {exc.strerror or exc}") from None
        self.link = IsoLink(reader, self.writer)
        try:
            resp = await self.link.associate(M.initiate_request(), self.password, self.timeout)
        except PermissionError:
            await self._close_socket()
            raise ConnectorError("AUTH_FAILED", "the IED refused the association (authentication)") from None
        except (IsoError, asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
            await self._close_socket()
            raise ConnectorError("PROTOCOL_ERROR", f"MMS association failed: {exc or 'no answer'}") from None
        pdu = M.parse_pdu(resp)
        if pdu.kind != "initiate-response":
            await self._close_socket()
            raise ConnectorError("PROTOCOL_ERROR", "the IED refused the MMS initiate")
        self.closed.clear()
        self.task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                pdu = M.parse_pdu(await self.link.recv())
                if pdu.kind in ("confirmed-response", "confirmed-error", "reject", "conclude-response"):
                    fut = self.pending.pop(pdu.invoke_id if pdu.invoke_id is not None else -1, None)
                    if fut is None and pdu.kind == "conclude-response":
                        fut = self.pending.pop(-1, None)
                    if fut and not fut.done():
                        fut.set_result(pdu)
                elif pdu.kind == "unconfirmed" and pdu.service == 0:
                    self.stats["reports"] += 1
                    if self.on_report:
                        self.on_report(*M.parse_information_report(pdu.body))
                elif pdu.kind == "confirmed-request":         # servers do not ask clients anything we support
                    await self.link.send(M.reject(pdu.invoke_id))
        except (IsoError, asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            self.close_reason = ConnectorError("CONN_CLOSED", f"association with {self.host}:{self.port} lost")
        except Exception as exc:                                   # noqa: BLE001 - malformed PDU from the IED
            self.close_reason = ConnectorError("PROTOCOL_ERROR", f"bad MMS message from the IED: {exc}")
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(self.close_reason or ConnectorError("CONN_CLOSED", "association closed"))
            self.pending.clear()
            self.closed.set()

    async def _call(self, name: str, service: bytes, timeout: float | None = None) -> M.Pdu:
        if self.link is None or self.closed.is_set():
            raise ConnectorError("CONN_CLOSED", "not connected")
        self.invoke = (self.invoke + 1) % 2147483647
        inv = self.invoke
        fut = asyncio.get_running_loop().create_future()
        self.pending[inv] = fut
        self.stats["requests"] += 1
        await self.link.send(M.confirmed(inv, service))
        try:
            pdu = await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError:
            self.pending.pop(inv, None)
            raise ConnectorError("CONN_TIMEOUT", f"{name}: no answer from the IED") from None
        if pdu.kind != "confirmed-response":
            self.stats["errors"] += 1
            raise M.MmsError(f"{name}: {pdu.error or pdu.kind}", "ADDR_INVALID" if pdu.kind == "confirmed-error" else "PROTOCOL_ERROR")
        return pdu

    # ------------------------------------------------------------ services
    async def get_name_list(self, object_class: int, domain: str | None = None) -> list[str]:
        self.guard.check("GetNameList")
        names, after = [], None
        while True:
            pdu = await self._call("GetNameList", M.get_name_list(object_class, domain, after))
            part, more = M.parse_name_list(pdu.body)
            names += part
            if not more or not part or len(names) > 100000:
                return names
            after = part[-1]

    async def read(self, names: list[tuple[str, str]], timeout: float | None = None) -> list:
        self.guard.check("Read")
        out = []
        for i in range(0, len(names), 40):
            pdu = await self._call("Read", M.read(names[i:i + 40]), timeout)
            out += M.parse_read(pdu.body)
        return out

    async def type_of(self, domain: str, item: str) -> M.TypeSpec:
        self.guard.check("GetVariableAccessAttributes")
        pdu = await self._call("GetVariableAccessAttributes", M.get_variable_access_attributes(domain, item))
        return M.parse_var_attr(pdu.body)

    async def dataset(self, domain: str, item: str) -> list[tuple[str, str]]:
        self.guard.check("GetNamedVariableListAttributes")
        pdu = await self._call("GetNamedVariableListAttributes", M.get_named_variable_list_attributes(domain, item))
        return M.parse_nvl_attr(pdu.body)

    async def write_rcb(self, domain: str, item: str, value: bytes) -> M.DataError | None:
        """Write one attribute of a report control block (spec 15.4: reporting setup only)."""
        m = RCB_ITEM.match(item)
        self.guard.check(f"WRITE_RCB:{m.group(2)}" if m else f"Write:{item}")
        pdu = await self._call("Write", M.write([(domain, item)], [value]))
        return M.parse_write(pdu.body)[0]

    async def close(self) -> None:
        if self.link is not None and not self.closed.is_set():
            try:
                self.guard.check("Conclude")
                fut = asyncio.get_running_loop().create_future()
                self.pending[-1] = fut
                await self.link.send(M.conclude_request())
                await asyncio.wait_for(fut, 2)
            except (ConnectorError, asyncio.TimeoutError, IsoError, ConnectionError, OSError):
                pass
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._close_socket()
        self.closed.set()

    async def _close_socket(self) -> None:
        if self.writer and not self.writer.is_closing():
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ssl.SSLError):
                pass
