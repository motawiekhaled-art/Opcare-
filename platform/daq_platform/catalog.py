"""Source types and standard protocols shown on the configuration screen (Opcare GUI design).

`connector` is the connector type of spec OSS-DAQ-CON-001 (it has a configuration schema). A protocol is
  available  - a connector of that type is installed (Platform.available_types)
  specified  - the connector specification and schema exist; connector in development
  planned    - on the roadmap, no specification yet
`preset` pre-fills the connection form (e.g. Modbus TCP vs RTU share the 'modbus' connector).
"""
from __future__ import annotations

P = lambda name, std, connector=None, preset=None, port=None: {  # noqa: E731
    "name": name, "standard": std, "connector": connector, "preset": preset or {}, "port": port}

CATALOG = [
    {"id": "energy", "name": "Energy & Utilities", "short": "Substations, IEDs, RTUs, meters",
     "description": "Protection relays, intelligent electronic devices, RTUs and meters in power substations, "
                    "generation plants and water networks.",
     "standards": ["IEC 61850", "IEC 60870-5-104", "IEEE 1815 (DNP3)", "IEC 62351"],
     "protocols": [P("IEC 61850 MMS", "IEC 61850-8-1", "iec61850", port=102),
                   P("IEC 60870-5-104", "IEC 60870-5-104", "iec104", port=2404),
                   P("DNP3", "IEEE 1815", "dnp3", port=20000),
                   P("Modbus TCP", "Modbus Application Protocol v1.1b3", "modbus", {"transport": "tcp"}, 502),
                   P("Modbus RTU", "Modbus over Serial Line v1.02", "modbus", {"transport": "rtu"})]},
    {"id": "plc", "name": "Industrial PLC / SCADA", "short": "Controllers, DCS, SCADA servers",
     "description": "PLCs, distributed control systems and SCADA servers. Process values, alarms and events.",
     "standards": ["IEC 62541", "IEC 61158", "IEC 61784", "ISA-95"],
     "protocols": [P("OPC UA", "IEC 62541", "opcua", port=4840),
                   P("Modbus TCP", "Modbus Application Protocol v1.1b3", "modbus", {"transport": "tcp"}, 502),
                   P("Modbus RTU", "Modbus over Serial Line v1.02", "modbus", {"transport": "rtu"}),
                   P("EtherNet/IP", "IEC 61158 / 61784 (CIP)"), P("PROFINET", "IEC 61158 / 61784")]},
    {"id": "cam", "name": "Video & Cameras", "short": "IP, PTZ, thermal cameras",
     "description": "IP and thermal cameras and NVRs, for live video and AI video analytics.",
     "standards": ["ONVIF Profile S / T", "RFC 7826 (RTSP 2.0)", "RFC 3550 (RTP)", "ITU-T H.264 / H.265"],
     "protocols": [P("ONVIF", "ONVIF Profile S, T", "camera", {"onvif": True}, 80),
                   P("RTSP / RTP", "RFC 2326 / 7826, RFC 3550", "camera", {"onvif": False}, 554),
                   P("SRT", "SRT Protocol"), P("WebRTC", "W3C WebRTC"), P("GigE Vision", "GigE Vision 2.x")]},
    {"id": "iot", "name": "IoT Sensors & Gateways", "short": "Edge devices, smart meters",
     "description": "Sensors, smart meters and gateways that publish telemetry over lightweight messaging.",
     "standards": ["ISO/IEC 20922 (MQTT)", "Sparkplug B", "RFC 7252 (CoAP)"],
     "protocols": [P("MQTT / Sparkplug B", "ISO/IEC 20922, OASIS MQTT 5.0", "mqtt", port=1883),
                   P("CoAP", "RFC 7252"), P("LwM2M", "OMA LwM2M 1.2"), P("AMQP", "ISO/IEC 19464")]},
    {"id": "bms", "name": "Building Automation", "short": "HVAC, lighting, access",
     "description": "Building management systems, HVAC and lighting controllers, sub-meters.",
     "standards": ["ISO 16484-5", "ASHRAE 135", "ISO/IEC 14543-3"],
     "protocols": [P("BACnet/IP", "ASHRAE 135"), P("KNX IP", "ISO/IEC 14543-3"), P("LonWorks", "ISO/IEC 14908")]},
    {"id": "db", "name": "Databases & Historians", "short": "SQL, historians",
     "description": "Relational databases and process historians holding operational records.",
     "standards": ["ISO/IEC 9075 (SQL)", "IEC 62541-11 (Historical Access)"],
     "protocols": [P("ODBC / JDBC", "ISO/IEC 9075"), P("OPC UA Historical Access", "IEC 62541-11")]},
    {"id": "api", "name": "Web & Cloud APIs", "short": "REST, streaming, SaaS",
     "description": "Business applications and cloud services exposing data over web APIs.",
     "standards": ["RFC 9110 (HTTP)", "RFC 6455", "OpenAPI 3"],
     "protocols": [P("REST / HTTPS", "RFC 9110, OpenAPI 3"), P("WebSocket", "RFC 6455"), P("gRPC", "gRPC")]},
    {"id": "file", "name": "Files & Transfers", "short": "CSV, logs, batch exports",
     "description": "Batch files from instruments, legacy systems or partners.",
     "standards": ["RFC 4180 (CSV)", "SSH (RFC 4253)"],
     "protocols": [P("SFTP", "SSH File Transfer"), P("FTPS", "RFC 959 + RFC 4217"), P("S3 object storage", "S3 API")]},
    {"id": "med", "name": "Medical & Clinical Devices", "short": "Monitors, LIS, imaging",
     "description": "Patient monitors, laboratory analysers and imaging systems.",
     "standards": ["HL7 v2.x", "HL7 FHIR R4", "DICOM", "ISO/IEEE 11073"],
     "protocols": [P("HL7 v2 (MLLP)", "HL7 v2.5.1"), P("HL7 FHIR", "HL7 FHIR R4"), P("DICOM", "DICOM PS3")]},
]


def catalog(available: set[str], specified: set[str]) -> list[dict]:
    out = []
    for t in CATALOG:
        protos = []
        for p in t["protocols"]:
            c = p["connector"]
            status = "available" if c in available else "specified" if c in specified else "planned"
            protos.append({**p, "status": status})
        out.append({**t, "protocols": protos,
                    "available": sum(p["status"] == "available" for p in protos)})
    return out
