# Build from the repository root:
#   docker build -f platform/Dockerfile -t opcare/daq-platform:0.1.0 .
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Opcare DAQ central platform" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="LicenseRef-Opcare-Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPCARE_SCHEMA_DIR=/opcare/schemas PYTHONPATH=/app

# starlette, uvicorn (BSD); paho-mqtt (EPL-2.0/EDL-1.0); psycopg 3 (LGPL-3.0, used unmodified as a library);
# jsonschema, referencing (MIT); cryptography (Apache-2.0 OR BSD-3-Clause, OPC UA client certificates)
RUN pip install --no-cache-dir "starlette>=0.40" "uvicorn>=0.30" "paho-mqtt>=2.1,<3" "psycopg[binary]>=3.2" \
    "jsonschema>=4.18" "referencing>=0.30" "cryptography>=42"

COPY connectors/sdk-python/opcare_connector_sdk /app/opcare_connector_sdk
COPY platform/daq_platform /app/daq_platform
COPY platform/web /app/web
COPY docs/connector-spec/schemas /opcare/schemas

RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin opcare
USER 10002
WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["python", "-m", "daq_platform.server"]
