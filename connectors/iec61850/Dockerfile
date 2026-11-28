# Build from the repository root:
#   docker build -f connectors/iec61850/Dockerfile -t opcare/connector-iec61850:1.0.0 .
# Run read-only, non-root, no listening ports (spec OSS-DAQ-CON-001 section 2):
#   docker run --read-only --tmpfs /tmp -v iec61850-data:/data --env-file source.env opcare/connector-iec61850:1.0.0
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ IEC 61850 connector" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas

# SDK dependencies: paho-mqtt (EPL-2.0/EDL-1.0, local broker), jsonschema + referencing (MIT). The IEC 61850 stack is
# Opcare code (standard library only; TLS per IEC 62351-4 through the ssl module); no IEC 61850 library is installed.
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "jsonschema>=4.18" "referencing>=0.30"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY connectors/iec61850/iec61850_connector /app/iec61850_connector
COPY docs/connector-spec/schemas /opcare/schemas
COPY docs/connector-spec/schemas/protocols/iec61850.config.schema.json /opcare/config.schema.json
COPY connectors/iec61850/opcare/manifest.json /opcare/manifest.json

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opcare && mkdir -p /data && chown 10001 /data
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "iec61850_connector"]
