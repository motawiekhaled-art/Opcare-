"""Container entry point: python -m udp_connector"""
from opcare_connector_sdk import run

from .connector import UdpConnector

if __name__ == "__main__":
    run(UdpConnector())
