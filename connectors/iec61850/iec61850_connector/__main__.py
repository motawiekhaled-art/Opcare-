"""Container entry point: python -m iec61850_connector"""
from opcare_connector_sdk import run

from .connector import Iec61850Connector

if __name__ == "__main__":
    run(Iec61850Connector())
