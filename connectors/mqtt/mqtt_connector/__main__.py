"""Container entry point: python -m mqtt_connector"""
from opcare_connector_sdk import run

from .connector import MqttConnector

if __name__ == "__main__":
    run(MqttConnector())
