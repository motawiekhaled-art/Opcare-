"""Container entry point: python -m opcua_connector"""
from opcare_connector_sdk import run

from .connector import OpcUaConnector

if __name__ == "__main__":
    run(OpcUaConnector())
