# Build from the repository root:
#   docker build -f connectors/iec104/Dockerfile -t opcare/connector-iec104:1.0.0 .
# Run read-only, non-root, no listening ports (spec OSS-DAQ-CON-001 section 2):
#   docker run --read-only --tmpfs /tmp -v iec104-data:/data --env-file source.env opcare/connector-iec104:1.0.0
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ IEC 60870-5-104 connector" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas

# SDK dependencies: paho-mqtt (EPL-2.0/EDL-1.0, local broker), jsonschema + referencing (MIT); tzdata (Apache-2.0)
# for device_time_zone. The IEC 104 stack is Opcare code (standard library only; TLS per IEC 62351-3 through the
# ssl module); no IEC 104 library (lib60870, j60870, c104: all GPL) is installed.
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "jsonschema>=4.18" "referencing>=0.30" "tzdata>=2024.1"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY connectors/iec104/iec104_connector /app/iec104_connector
COPY docs/connector-spec/schemas /opcare/schemas
COPY docs/connector-spec/schemas/protocols/iec104.config.schema.json /opcare/config.schema.json
COPY connectors/iec104/opcare/manifest.json /opcare/manifest.json

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opcare && mkdir -p /data && chown 10001 /data
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "iec104_connector"]
