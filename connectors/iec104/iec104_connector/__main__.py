"""Container entry point: python -m iec104_connector"""
from opcare_connector_sdk import run

from .connector import Iec104Connector

if __name__ == "__main__":
    run(Iec104Connector())
