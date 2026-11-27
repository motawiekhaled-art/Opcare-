# Build from the repository root:
#   docker build -f connectors/snmp/Dockerfile -t opcare/connector-snmp:1.0.0 .
# Run read-only and non-root. Only when traps are configured does it listen, on that one UDP port
# (spec section 2 rule 4); map the standard trap port to it:  -p 162:10162/udp
#   docker run --read-only --tmpfs /tmp -v snmp-data:/data --env-file source.env opcare/connector-snmp:1.0.0
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ SNMP connector" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas

# SDK dependencies: paho-mqtt (EPL-2.0/EDL-1.0), jsonschema + referencing (MIT).
# cryptography (Apache-2.0 OR BSD-3-Clause) provides AES for SNMPv3 privacy; the SNMP stack itself is our own code.
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "jsonschema>=4.18" "referencing>=0.30" "cryptography>=42"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY connectors/snmp/snmp_connector /app/snmp_connector
COPY docs/connector-spec/schemas /opcare/schemas
COPY docs/connector-spec/schemas/protocols/snmp.config.schema.json /opcare/config.schema.json
COPY connectors/snmp/opcare/manifest.json /opcare/manifest.json

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opcare && mkdir -p /data && chown 10001 /data
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "snmp_connector"]
