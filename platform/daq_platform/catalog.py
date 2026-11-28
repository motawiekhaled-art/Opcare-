"""Source types and standard protocols shown on the configuration screen (Opcare GUI design).

`connector` is the connector type of spec OSS-DAQ-CON-001 (it has a configuration schema). A protocol is
  available  - a connector of that type is installed (Platform.available_types)
  specified  - the connector specification and schema exist; connector in development
  planned    - on the roadmap, no specification yet
`preset` pre-fills the connection form (e.g. Modbus TCP vs RTU share the 'modbus' connector).
`devices` lists typical equipment and applications that use the protocol (shown on the configuration screen).
TEMPLATES are ready-made tag sets for SNMP (standard MIBs) and UDP telegrams (NMEA 0183).
"""
from __future__ import annotations

P = lambda name, std, connector=None, preset=None, port=None, devices=None: {  # noqa: E731
    "name": name, "standard": std, "connector": connector, "preset": preset or {}, "port": port,
    "devices": devices or []}

UDP_TELEMETRY = ["GNSS / GPS receivers and marine instruments (NMEA 0183)", "Weather stations and environmental sensors",
                 "Vibration and condition-monitoring sensors", "LiDAR, radar and level sensors with UDP output",
                 "Power-quality analysers, inverters and UPSs with UDP telemetry", "Custom IoT sensors (CSV or JSON)"]
UDP_PLC = ["Siemens S7-1200 / S7-1500 (TSEND_C / TURCV telegrams)", "Rockwell ControlLogix / CompactLogix (UDP sockets)",
           "Beckhoff TwinCAT (TCP/UDP RT, TF6310)", "Mitsubishi, Omron, Schneider PLCs (socket services)",
           "Robot and machine controllers sending status telegrams"]
SYSLOG = ["Switches and routers (Cisco, Juniper, HPE Aruba, MikroTik)", "Firewalls (Fortinet, Palo Alto, Sophos)",
          "Linux and Windows servers (rsyslog, syslog-ng, NXLog)", "Wireless controllers and access points",
          "Industrial switches (Hirschmann, Moxa, Siemens SCALANCE)", "UPS, PDU and storage appliances"]
SNMP_DEVICES = ["Network switches and routers (IF-MIB: port status, traffic, errors)",
                "Firewalls and wireless controllers", "Servers and storage (HOST-RESOURCES-MIB: CPU, memory, disks)",
                "UPSs (UPS-MIB, RFC 1628: battery, load, on-battery alarms)", "PDUs, cooling units and environmental monitors",
                "Industrial Ethernet switches and gateways (Moxa, Hirschmann, Siemens SCALANCE)", "Printers and IP phones"]
MQTT_DEVICES = ["IoT gateways and edge computers (Node-RED, AWS / Azure IoT edge, Teltonika, Moxa)",
                "Smart water, gas and electricity meters through LoRaWAN / NB-IoT network servers (ChirpStack, TTN)",
                "Solar inverters, battery systems and EV chargers with MQTT gateways",
                "Sparkplug B edge nodes (Ignition Edge, Cirrus Link, HiveMQ Edge, Opto 22 groov)",
                "Wireless sensors (temperature, vibration, pressure) and building IoT platforms"]
OPCUA_DEVICES = ["Siemens S7-1500 / S7-1200 (built-in OPC UA server), WinCC OA and WinCC Unified",
                 "Rockwell ControlLogix through FactoryTalk Linx Gateway / FactoryTalk Optix",
                 "Beckhoff TwinCAT (TF6100), B&R, Omron NX/NJ, Mitsubishi iQ-R, Schneider M580",
                 "DCS: ABB 800xA, Emerson DeltaV, Honeywell Experion, Yokogawa CENTUM",
                 "Gateways and SCADA: Kepware KEPServerEX, Ignition, Matrikon, Softing, AVEVA",
                 "Machines with OPC UA companion specifications (EUROMAP, umati, PackML)"]
OPCUA = P("OPC UA", "IEC 62541 (Part 4 services, Part 6 UA-TCP binary, Part 7 security profiles)", "opcua", {}, 4840, OPCUA_DEVICES)
MQTT = P("MQTT / Sparkplug B", "ISO/IEC 20922, OASIS MQTT 5.0, Eclipse Sparkplug 3.0", "mqtt", {}, 1883, MQTT_DEVICES)
SNMP_V2C = P("SNMP v1 / v2c", "RFC 1157, RFC 3416 (community)", "snmp", {"version": "2c"}, 161, SNMP_DEVICES)
SNMP_V3 = P("SNMPv3 (secure)", "RFC 3414 USM, RFC 3826 AES, RFC 7860", "snmp", {"version": "3"}, 161, SNMP_DEVICES)

CATALOG = [
    {"id": "energy", "name": "Energy & Utilities", "short": "Substations, IEDs, RTUs, meters",
     "description": "Protection relays, intelligent electronic devices, RTUs and meters in power substations, "
                    "generation plants and water networks.",
     "standards": ["IEC 61850", "IEC 60870-5-104", "IEEE 1815 (DNP3)", "IEC 62351"],
     "protocols": [P("IEC 61850 MMS", "IEC 61850-8-1", "iec61850", port=102),
                   P("IEC 60870-5-104", "IEC 60870-5-104", "iec104", port=2404),
                   P("DNP3", "IEEE 1815", "dnp3", port=20000),
                   OPCUA,
                   P("Modbus TCP", "Modbus Application Protocol v1.1b3", "modbus", {"transport": "tcp"}, 502),
                   P("Modbus RTU", "Modbus over Serial Line v1.02", "modbus", {"transport": "rtu"}),
                   MQTT,
                   P("UDP telemetry", "RFC 768 (NMEA 0183, CSV, JSON, binary)", "udp", {}, 5514,
                     ["Power-quality analysers and meters with UDP push", "Inverters and battery systems",
                      "Weather stations at solar and wind sites", "GNSS time and position receivers"]),
                   SNMP_V2C]},
    {"id": "plc", "name": "Industrial PLC / SCADA", "short": "Controllers, DCS, SCADA servers",
     "description": "PLCs, distributed control systems and SCADA servers. Process values, alarms and events.",
     "standards": ["IEC 62541", "IEC 61158", "IEC 61784", "ISA-95"],
     "protocols": [OPCUA,
                   P("Modbus TCP", "Modbus Application Protocol v1.1b3", "modbus", {"transport": "tcp"}, 502),
                   P("Modbus RTU", "Modbus over Serial Line v1.02", "modbus", {"transport": "rtu"}),
                   P("UDP telegrams", "RFC 768 (PLC send / receive)", "udp", {}, 5514, UDP_PLC),
                   P("MQTT / Sparkplug B", "OASIS MQTT 5.0, Eclipse Sparkplug 3.0", "mqtt", {}, 1883,
                     ["Sparkplug B edge nodes and SCADA (Ignition, Inductive Automation)", "PLC and drive gateways publishing MQTT",
                      "Unified Namespace (UNS) brokers in manufacturing"]),
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
     "protocols": [MQTT,
                   P("UDP datagrams", "RFC 768 (NMEA 0183, CSV, JSON, binary)", "udp", {}, 5514, UDP_TELEMETRY),
                   P("CoAP", "RFC 7252"), P("LwM2M", "OMA LwM2M 1.2"), P("AMQP", "ISO/IEC 19464")]},
    {"id": "it", "name": "IT & Network", "short": "Switches, servers, UPS, firewalls",
     "description": "IT and network infrastructure that the operation depends on: switches, routers, firewalls, servers, "
                    "UPSs, PDUs and industrial Ethernet. Health, traffic and alarms through SNMP polling, traps and syslog.",
     "standards": ["RFC 3411-3418 (SNMP)", "RFC 3414 (USM)", "RFC 2863 (IF-MIB)", "RFC 1628 (UPS-MIB)",
                   "RFC 5424 (Syslog)", "RFC 7011 (IPFIX)"],
     "protocols": [SNMP_V2C, SNMP_V3,
                   P("Syslog over UDP", "RFC 5424, RFC 3164, RFC 5426", "udp", {}, 5514, SYSLOG),
                   P("NetFlow / IPFIX", "RFC 3954, RFC 7011"), P("sFlow", "sFlow v5")]},
    {"id": "bms", "name": "Building Automation", "short": "HVAC, lighting, access",
     "description": "Building management systems, HVAC and lighting controllers, sub-meters.",
     "standards": ["ISO 16484-5", "ASHRAE 135", "ISO/IEC 14543-3"],
     "protocols": [P("BACnet/IP", "ASHRAE 135"), P("KNX IP", "ISO/IEC 14543-3"), P("LonWorks", "ISO/IEC 14908"),
                   P("SNMP (UPS, PDU, cooling)", "RFC 3416, RFC 1628 UPS-MIB", "snmp", {"version": "2c"}, 161,
                     ["UPSs and PDUs", "Precision cooling (CRAC) units", "Environmental monitors (temperature, humidity, leak)",
                      "Generator and transfer-switch controllers with SNMP cards"])]},
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


# ------------------------------------------------------------------ tag templates (configuration screen)
def _snmp(name, oid, data_type, unit=None, gain=None, transform=None):
    t = {"name": name, "address": {"oid": oid}, "data_type": data_type}
    if transform:
        t["address"]["transform"] = transform
    if unit:
        t["unit"] = unit
    if gain is not None:
        t["scaling"] = {"gain": gain, "offset": 0}
    return t


def _nmea(name, sentence, field, data_type="float", unit=None, coordinate=False, gain=None):
    t = {"name": name, "address": {"format": "nmea", "sentence": sentence, "field": field}, "data_type": data_type}
    if coordinate:
        t["address"]["coordinate"] = True
    if unit:
        t["unit"] = unit
    if gain is not None:
        t["scaling"] = {"gain": gain, "offset": 0}
    return t


TEMPLATES = {
    "snmp": [
        {"id": "system", "name": "Device health (any SNMP device)", "instance": None, "tags": [
            _snmp("sys-name", "1.3.6.1.2.1.1.5.0", "string"),
            _snmp("uptime", "1.3.6.1.2.1.1.3.0", "float", "h", 1 / 360000)]},
        {"id": "interface", "name": "Network port (IF-MIB)", "instance": "Interface index (ifIndex)", "tags": [
            _snmp("port-{n}-up", "1.3.6.1.2.1.2.2.1.8.{n}", "bool"),
            _snmp("port-{n}-enabled", "1.3.6.1.2.1.2.2.1.7.{n}", "bool"),
            _snmp("port-{n}-in", "1.3.6.1.2.1.31.1.1.1.6.{n}", "float", "Mbit/s", 0.000008, "rate"),
            _snmp("port-{n}-out", "1.3.6.1.2.1.31.1.1.1.10.{n}", "float", "Mbit/s", 0.000008, "rate"),
            _snmp("port-{n}-in-errors", "1.3.6.1.2.1.2.2.1.14.{n}", "float", "1/s", None, "rate"),
            _snmp("port-{n}-speed", "1.3.6.1.2.1.31.1.1.1.15.{n}", "int", "Mbit/s")]},
        {"id": "ups", "name": "UPS (UPS-MIB, RFC 1628)", "instance": None, "tags": [
            _snmp("battery-status", "1.3.6.1.2.1.33.1.2.1.0", "enum"),
            _snmp("battery-charge", "1.3.6.1.2.1.33.1.2.4.0", "int", "%"),
            _snmp("battery-minutes-left", "1.3.6.1.2.1.33.1.2.3.0", "int", "min"),
            _snmp("battery-temp", "1.3.6.1.2.1.33.1.2.7.0", "int", "degC"),
            _snmp("input-voltage", "1.3.6.1.2.1.33.1.3.3.1.3.1", "int", "V"),
            _snmp("output-source", "1.3.6.1.2.1.33.1.4.1.0", "enum"),
            _snmp("output-load", "1.3.6.1.2.1.33.1.4.4.1.5.1", "int", "%")]},
        {"id": "server", "name": "Server (HOST-RESOURCES-MIB)", "instance": "Processor index (hrDeviceIndex)", "tags": [
            _snmp("cpu-{n}-load", "1.3.6.1.2.1.25.3.3.1.2.{n}", "int", "%"),
            _snmp("processes", "1.3.6.1.2.1.25.1.6.0", "int"),
            _snmp("host-uptime", "1.3.6.1.2.1.25.1.1.0", "float", "h", 1 / 360000)]},
    ],
    "udp": [
        {"id": "gnss-rmc", "name": "GNSS position (NMEA RMC)", "tags": [
            _nmea("latitude", "RMC", 3, unit="deg", coordinate=True),
            _nmea("longitude", "RMC", 5, unit="deg", coordinate=True),
            _nmea("speed", "RMC", 7, unit="km/h", gain=1.852),
            _nmea("course", "RMC", 8, unit="deg"),
            _nmea("fix-valid", "RMC", 2, data_type="bool")]},
        {"id": "gnss-gga", "name": "GNSS fix quality (NMEA GGA)", "tags": [
            _nmea("fix-quality", "GGA", 6, data_type="enum"),
            _nmea("satellites", "GGA", 7, data_type="int"),
            _nmea("hdop", "GGA", 8),
            _nmea("altitude", "GGA", 9, unit="m")]},
        {"id": "wind-mwv", "name": "Wind sensor (NMEA MWV)", "tags": [
            _nmea("wind-angle", "MWV", 1, unit="deg"),
            _nmea("wind-speed", "MWV", 3)]},
        {"id": "syslog", "name": "Syslog summary", "tags": [
            {"name": "last-severity", "address": {"format": "syslog", "field": "severity"}, "data_type": "int"},
            {"name": "last-message", "address": {"format": "syslog", "field": "message"}, "data_type": "string"}]},
    ],
}


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
