"""Opcare DAQ connector SDK (Python) for spec OSS-DAQ-CON-001 1.0.

Closed-source, for Opcare connectors in the main repository only. A connector that links GPL code must
not import this package (spec section 2, rule 7); none does today.
"""
from .connector import Connector, Sink
from .core import (ALLOW, SPEC, Batcher, ConnectorError, Point, PointTable, Q, ReadOnlyGuard, ValuePipeline,
                   iso, now_iso, ulid)
from .runtime import Env, JsonLog, PahoClient, Runtime, SchemaValidator, run

__all__ = ["Connector", "Sink", "ALLOW", "SPEC", "Batcher", "ConnectorError", "Point", "PointTable", "Q",
           "ReadOnlyGuard", "ValuePipeline", "iso", "now_iso", "ulid", "Env", "JsonLog", "PahoClient",
           "Runtime", "SchemaValidator", "run"]
__version__ = "0.1.0"
