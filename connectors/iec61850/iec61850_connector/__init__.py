"""Opcare DAQ IEC 61850 MMS client connector with its own MMS stack (spec OSS-DAQ-CON-001 section 15.4)."""
from .connector import Iec61850Connector

__all__ = ["Iec61850Connector"]
