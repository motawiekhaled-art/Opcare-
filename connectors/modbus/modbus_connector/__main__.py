"""Container entry point: python -m modbus_connector"""
from opcare_connector_sdk import run

from .connector import ModbusConnector

if __name__ == "__main__":
    run(ModbusConnector())
