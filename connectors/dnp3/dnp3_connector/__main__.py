"""Container entry point: python -m dnp3_connector"""
from opcare_connector_sdk import run

from .connector import Dnp3Connector

if __name__ == "__main__":
    run(Dnp3Connector())
