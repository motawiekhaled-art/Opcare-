# Build from the repository root:
#   docker build -f connectors/udp/Dockerfile -t opcare/connector-udp:1.0.0 .
# Run read-only and non-root. It listens on the one UDP port of its configuration (spec section 2 rule 4);
# map a standard port below 1024 to it, e.g. syslog:
#   docker run --read-only --tmpfs /tmp -p 514:5514/udp -v udp-data:/data --env-file source.env opcare/connector-udp:1.0.0
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ UDP connector" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas

# SDK dependencies: paho-mqtt (EPL-2.0/EDL-1.0), jsonschema + referencing (MIT). The connector itself uses the standard library only.
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "jsonschema>=4.18" "referencing>=0.30"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY connectors/udp/udp_connector /app/udp_connector
COPY docs/connector-spec/schemas /opcare/schemas
COPY docs/connector-spec/schemas/protocols/udp.config.schema.json /opcare/config.schema.json
COPY connectors/udp/opcare/manifest.json /opcare/manifest.json

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opcare && mkdir -p /data && chown 10001 /data
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "udp_connector"]
