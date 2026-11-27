"""The class a protocol developer implements. Everything generic is done by Runtime (runtime.py)."""
from __future__ import annotations

import abc
import os
import pathlib
from typing import Any, Protocol

from .core import ConnectorError, PointTable


class Sink(Protocol):
    """Handed to the connector by the runtime. All methods are safe to call from the asyncio loop."""

    def value(self, tag_id: str, raw: Any, q: int = 192, ts: str | None = None,
              flags: list[str] | tuple = ()) -> None:
        """Report one value. Scaling, deadband, report-by-exception and batching are applied for you.
        ts: device timestamp (RFC 3339 UTC with ms) if the protocol gives a valid one, else None."""

    def device_state(self, device: str, state: str, error: ConnectorError | None = None) -> None:
        """state: connecting | connected | unreachable | disabled. 'unreachable' also publishes q=24 for
        every point of the device and the conn.lost event; 'connected' publishes conn.connected."""

    def event(self, type_: str, severity: str, message: str, **fields: Any) -> None:
        """Operational event (spec section 10), e.g. device.restart."""

    def scan_overrun(self, scan_ms: int) -> None:
        """Report that a poll cycle took longer than scan_ms."""


class Connector(abc.ABC):
    """Base class for a protocol connector.

    Lifecycle driven by the runtime:
        validate(config)          -> extra protocol checks (schema validation already done)
        start(config, table, sink)-> connect and acquire until stop() (run your own tasks)
        apply_points(...)         -> optional hot reload of points without reconnect
        stop()                    -> disconnect, cancel tasks; must finish within 8 s
    Admin operations: test_connection, browse, read_now, diagnostics.
    """

    type: str = ""            # must equal manifest type
    version: str = "0.0.0"    # must equal manifest version
    capabilities = {"browse": False, "read_now": False}
    secrets_dir: pathlib.Path | None = None   # set by the Runtime (OPCARE_SECRETS_DIR) or the edge agent

    def secret(self, ref: str) -> str:
        """Read a *_ref secret file (spec section 3). Raises ConnectorError SECRET_MISSING."""
        base = self.secrets_dir or pathlib.Path(os.environ.get("OPCARE_SECRETS_DIR", "/run/secrets"))
        try:
            return (pathlib.Path(base) / ref).read_text().strip()
        except OSError:
            raise ConnectorError("SECRET_MISSING", f"secret '{ref}' is missing or unreadable") from None

    def device_of(self, address: dict) -> str:
        """Which device a point address belongs to. Default: address['device'] or 'main'."""
        return str(address.get("device", "main"))

    def devices(self, config: dict) -> list[str]:
        """Device ids for the status message. Default: one device 'main'."""
        return ["main"]

    async def validate(self, config: dict) -> list[str]:
        """Protocol checks beyond the JSON Schema (spec 6.1 rule 8). Return a list of error texts."""
        return []

    @abc.abstractmethod
    async def start(self, config: dict, table: PointTable, sink: Sink) -> None:
        """Connect and begin acquisition. Return quickly; run long work in your own asyncio tasks."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop acquisition and close connections."""

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        """Apply a configuration whose connection is unchanged. Return False to request a full restart."""
        return False

    @abc.abstractmethod
    async def test_connection(self, config: dict) -> dict:
        """Return {"devices": [{"id", "ok", "latency_ms", "error"?, "info"?}]} using a separate connection."""

    async def browse(self, args: dict) -> dict:
        raise ConnectorError("OP_UNSUPPORTED", f"browse is not supported by the {self.type} connector")

    async def read_now(self, args: dict) -> dict:
        raise ConnectorError("OP_UNSUPPORTED", f"read_now is not supported by the {self.type} connector")

    async def diagnostics(self) -> dict:
        return {}
