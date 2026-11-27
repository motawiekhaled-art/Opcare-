# Build from the repository root:
#   docker build -f connectors/mqtt/Dockerfile -t opcare/connector-mqtt:1.0.0 .
# Run read-only, non-root, no listening ports (spec OSS-DAQ-CON-001 section 2):
#   docker run --read-only --tmpfs /tmp -v mqtt-data:/data --env-file source.env opcare/connector-mqtt:1.0.0
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ MQTT connector" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas

# SDK dependencies only: paho-mqtt (EPL-2.0/EDL-1.0, for the local broker), jsonschema + referencing (MIT).
# The client's broker is read with the connector's own MQTT and Sparkplug B code (standard library).
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "jsonschema>=4.18" "referencing>=0.30"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY connectors/mqtt/mqtt_connector /app/mqtt_connector
COPY docs/connector-spec/schemas /opcare/schemas
COPY docs/connector-spec/schemas/protocols/mqtt.config.schema.json /opcare/config.schema.json
COPY connectors/mqtt/opcare/manifest.json /opcare/manifest.json

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opcare && mkdir -p /data && chown 10001 /data
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "mqtt_connector"]
