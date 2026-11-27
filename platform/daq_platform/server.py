"""Production entry point: python -m daq_platform.server

Environment:
  OPCARE_DB_URL        postgresql://opcare@localhost/opcare   (or sqlite:///var/lib/opcare/daq.db)
  OPCARE_MQTT_URL      mqtts://broker:8883
  OPCARE_MQTT_USERNAME / OPCARE_MQTT_PASSWORD_FILE / OPCARE_MQTT_CA / OPCARE_MQTT_CERT / OPCARE_MQTT_KEY
  OPCARE_API_TOKENS_FILE  optional: JSON {"<token>": {"user": "scada-gw", "role": "viewer"}} for machines and scripts
  OPCARE_ADMIN_USER / OPCARE_ADMIN_PASSWORD_FILE  first admin, created only when the database has no users yet
                       (no file: a random password is generated and written once to the log)
  OPCARE_SCHEMA_DIR    directory with the connector spec schemas
  OPCARE_HTTP_HOST / OPCARE_HTTP_PORT (default 0.0.0.0:8080; put HTTPS in front with the site's reverse proxy)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets

import uvicorn

from opcare_connector_sdk import SchemaValidator

from .accounts import Accounts
from .api import create_app
from .bus import PahoBus
from .services import Platform
from .store import Store


async def serve(platform: Platform, tokens: dict, host: str, port: int) -> None:
    await platform.start()
    accounts = Accounts(platform.store)
    e = os.environ
    admin = e.get("OPCARE_ADMIN_USER", "admin")
    if e.get("OPCARE_ADMIN_PASSWORD_FILE"):
        with open(e["OPCARE_ADMIN_PASSWORD_FILE"]) as fh:
            password, generated = fh.read().strip(), False
    else:
        password, generated = secrets.token_urlsafe(12) + "7", True
    if await accounts.ensure_admin(admin, password) and generated:
        logging.getLogger("daq.platform").warning("first admin '%s' created with password %s - sign in and change it",
                                                  admin, password)
    server = uvicorn.Server(uvicorn.Config(create_app(platform, tokens, accounts=accounts), host=host, port=port,
                                           log_level="warning", access_log=False))
    try:
        await server.serve()
    finally:
        await platform.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    e = os.environ
    with open(e["OPCARE_MQTT_PASSWORD_FILE"]) as fh:
        password = fh.read().strip()
    bus = PahoBus(e["OPCARE_MQTT_URL"], "opcare-platform", e["OPCARE_MQTT_USERNAME"], password,
                  e.get("OPCARE_MQTT_CA"), e.get("OPCARE_MQTT_CERT"), e.get("OPCARE_MQTT_KEY"))
    tokens = {}
    if e.get("OPCARE_API_TOKENS_FILE"):
        with open(e["OPCARE_API_TOKENS_FILE"]) as fh:
            tokens = json.load(fh)
    platform = Platform(Store(e["OPCARE_DB_URL"]), bus, SchemaValidator(e.get("OPCARE_SCHEMA_DIR", "/opcare/schemas")))
    asyncio.run(serve(platform, tokens, e.get("OPCARE_HTTP_HOST", "0.0.0.0"), int(e.get("OPCARE_HTTP_PORT", "8080"))))


if __name__ == "__main__":
    main()
