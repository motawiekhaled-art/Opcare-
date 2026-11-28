# Build from the repository root:
#   docker build -f connectors/dnp3/Dockerfile -t opcare/connector-dnp3:1.0.0 .
# Run read-only, non-root, no listening ports (spec OSS-DAQ-CON-001 section 2):
#   docker run --read-only --tmpfs /tmp -v dnp3-data:/data --env-file source.env opcare/connector-dnp3:1.0.0
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ DNP3 connector" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas

# SDK dependencies: paho-mqtt (EPL-2.0/EDL-1.0, local broker), jsonschema + referencing (MIT). The DNP3 stack is
# Opcare code (standard library only; TLS per IEC 62351-3 through the ssl module); no DNP3 library is installed.
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "jsonschema>=4.18" "referencing>=0.30"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY connectors/dnp3/dnp3_connector /app/dnp3_connector
COPY docs/connector-spec/schemas /opcare/schemas
COPY docs/connector-spec/schemas/protocols/dnp3.config.schema.json /opcare/config.schema.json
COPY connectors/dnp3/opcare/manifest.json /opcare/manifest.json

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opcare && mkdir -p /data && chown 10001 /data
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "dnp3_connector"]
