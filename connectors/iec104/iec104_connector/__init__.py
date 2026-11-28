"""Opcare DAQ IEC 60870-5-104 master connector with its own IEC 104 stack (spec OSS-DAQ-CON-001 section 15.5)."""
from .connector import Iec104Connector

__all__ = ["Iec104Connector"]
