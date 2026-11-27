"""Container entry point: python -m snmp_connector"""
from opcare_connector_sdk import run

from .connector import SnmpConnector

if __name__ == "__main__":
    run(SnmpConnector())
