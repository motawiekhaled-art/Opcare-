"""Generates the JSON Schemas of the Opcare DAQ connector spec (spec 1.0).

Run:  python tools/gen_schemas.py      (writes schemas/ next to this tools/ folder)
Edit this file, not the generated JSON.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "schemas"
BASE = "https://schemas.opcaresystems.com/daq/connector/1.0/"
D2020 = "https://json-schema.org/draft/2020-12/schema"
C = "common.schema.json#/$defs/"          # ref prefix from schemas/
PC = "../common.schema.json#/$defs/"      # ref prefix from schemas/protocols/


def ref(name, proto=False):
    return {"$ref": (PC if proto else C) + name}


def obj(props, required=(), extra=False, **kw):
    o = {"type": "object", "properties": props}
    if required:
        o["required"] = list(required)
    if not extra:
        o["additionalProperties"] = False
    o.update(kw)
    return o


def integer(mn=None, mx=None, default=None):
    o = {"type": "integer"}
    if mn is not None:
        o["minimum"] = mn
    if mx is not None:
        o["maximum"] = mx
    if default is not None:
        o["default"] = default
    return o


def enum(values, default=None):
    o = {"enum": list(values)}
    if default is not None:
        o["default"] = default
    return o


def write(rel, schema):
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = {"$schema": D2020, "$id": BASE + rel, **schema}
    path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n")


ERROR_CODES = ["CFG_INVALID", "CFG_UNSUPPORTED_SPEC", "SECRET_MISSING", "CONN_REFUSED", "CONN_TIMEOUT",
               "CONN_CLOSED", "TLS_FAILED", "AUTH_FAILED", "ADDR_INVALID", "POINT_NOT_FOUND", "TYPE_MISMATCH",
               "DEVICE_BUSY", "OP_UNSUPPORTED", "OP_FORBIDDEN", "TIMEOUT", "PROTOCOL_ERROR", "INTERNAL"]
QUALITY = [192, 216, 64, 68, 80, 84, 0, 4, 12, 24, 28]
TYPES = ["modbus", "opcua", "mqtt", "iec61850", "iec104", "dnp3", "camera", "udp", "snmp"]

# ---------------------------------------------------------------- common
common = {
    "title": "Common definitions",
    "$defs": {
        "spec": {"type": "string", "pattern": r"^1\.[0-9]+$", "description": "Spec version MAJOR.MINOR"},
        "siteId": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,62}$"},
        "sourceId": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,62}$"},
        "tagId": {"type": "string", "maxLength": 200,
                  "pattern": r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$"},
        "uuid": {"type": "string",
                 "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"},
        "ulid": {"type": "string", "pattern": r"^[0-9A-HJKMNP-TV-Z]{26}$"},
        "timestamp": {"type": "string",
                      "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
                      "description": "RFC 3339 UTC with milliseconds"},
        "quality": {"type": "integer", "enum": QUALITY,
                    "description": "192 GOOD, 216 GOOD_LOCAL_OVERRIDE, 64 UNCERTAIN, 68 UNCERTAIN_LAST_USABLE, "
                                   "80 UNCERTAIN_SENSOR_NOT_ACCURATE, 84 UNCERTAIN_EU_EXCEEDED, 0 BAD, "
                                   "4 BAD_CONFIG_ERROR, 12 BAD_DEVICE_FAILURE, 24 BAD_COMM_FAILURE, 28 BAD_OUT_OF_SERVICE"},
        "dataType": enum(["bool", "int", "float", "string", "enum"]),
        "connectorType": enum(TYPES),
        "errorCode": enum(ERROR_CODES),
        "error": obj({"code": {"$ref": "#/$defs/errorCode"}, "message": {"type": "string", "maxLength": 1000},
                      "ts": {"$ref": "#/$defs/timestamp"}}, ["code", "message"]),
        "secretRef": {"type": "string", "pattern": r"^[A-Za-z0-9._-]{1,128}$",
                      "description": "File name inside OPCARE_SECRETS_DIR"},
        "host": {"type": "string", "minLength": 1, "maxLength": 253},
        "port": integer(1, 65535),
        "tls": obj({"enabled": {"type": "boolean", "default": False},
                    "client_cert_ref": {"$ref": "#/$defs/secretRef"},
                    "client_key_ref": {"$ref": "#/$defs/secretRef"},
                    "ca_cert_ref": {"$ref": "#/$defs/secretRef"},
                    "insecure_skip_verify": {"type": "boolean", "default": False}}),
        "acquisitionCommon": {
            "type": "object",
            "properties": {
                "default_scan_ms": integer(10, 86400000, 1000),
                "batch_max_values": integer(1, 5000, 500),
                "batch_max_ms": integer(10, 60000, 1000),
                "integrity_s": integer(0, 86400, 900),
                "request_timeout_ms": integer(100, 120000, 3000),
            },
        },
        "deadband": obj({"mode": enum(["abs", "pct"]), "value": {"type": "number", "minimum": 0}},
                        ["mode", "value"]),
        "scaling": obj({"gain": {"type": "number", "default": 1}, "offset": {"type": "number", "default": 0}}),
        "point": obj({
            "tag_id": {"$ref": "#/$defs/tagId"},
            "address": {"type": "object"},
            "data_type": {"$ref": "#/$defs/dataType"},
            "scaling": {"$ref": "#/$defs/scaling"},
            "unit": {"type": "string", "maxLength": 32},
            "deadband": {"$ref": "#/$defs/deadband"},
            "scan_ms": integer(10, 86400000),
            "enabled": {"type": "boolean", "default": True},
        }, ["tag_id", "address", "data_type"]),
    },
}
write("common.schema.json", common)

# ---------------------------------------------------------------- manifest
write("manifest.schema.json", {
    "title": "Connector manifest (/opcare/manifest.json)",
    **obj({
        "spec": ref("spec"), "type": ref("connectorType"),
        "name": {"type": "string", "maxLength": 100},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$"},
        "license": {"type": "string", "description": "SPDX expression"},
        "config_schema": {"type": "string"},
        "capabilities": obj({
            "modes": {"type": "array", "items": enum(["poll", "subscribe", "report"]), "minItems": 1,
                      "uniqueItems": True},
            "browse": {"type": "boolean"}, "read_now": {"type": "boolean"},
            "device_timestamps": {"type": "boolean"}, "tls": {"type": "boolean"},
        }, ["modes", "browse", "read_now"]),
        "limits": obj({"max_points": integer(1), "min_scan_ms": integer(10)}, ["max_points", "min_scan_ms"]),
    }, ["spec", "type", "name", "version", "license", "config_schema", "capabilities", "limits"]),
})

# ---------------------------------------------------------------- value batch
write("value-batch.schema.json", {
    "title": "Value batch (topic opcare/v1/{site}/data/{source_id})",
    **obj({
        "spec": ref("spec"), "src": ref("sourceId"), "boot": ref("uuid"),
        "seq": integer(1), "sent": ref("timestamp"),
        "values": {"type": "array", "minItems": 1, "maxItems": 5000, "items": obj({
            "t": ref("tagId"),
            "v": {"type": ["number", "integer", "boolean", "string", "null"]},
            "q": ref("quality"), "ts": ref("timestamp"), "rx": ref("timestamp"),
            "f": {"type": "array", "items": {"type": "string", "maxLength": 32}, "maxItems": 16},
        }, ["t", "v", "q", "ts", "rx"],
            **{"if": {"properties": {"q": {"enum": [0, 4, 12, 24, 28]}}},
               "else": {"properties": {"v": {"not": {"type": "null"}}}}})},
    }, ["spec", "src", "boot", "seq", "sent", "values"]),
})

# ---------------------------------------------------------------- event
write("event.schema.json", {
    "title": "Event (topic opcare/v1/{site}/event/{source_id})",
    **obj({
        "spec": ref("spec"), "id": ref("ulid"), "src": ref("sourceId"),
        "type": {"type": "string", "pattern": r"^[a-z0-9_]+(\.[a-z0-9_]+)+$"},
        "severity": enum(["info", "low", "medium", "high", "critical"]),
        "ts": ref("timestamp"),
        "message": {"type": "string", "maxLength": 1000},
        "device": {"type": "string"},
        "tag": ref("tagId"),
        "attrs": {"type": "object"},
        "camera": ref("tagId"), "zone": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "bbox": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                 "minItems": 4, "maxItems": 4, "description": "x, y, width, height as fractions of the frame"},
        "track_id": {"type": "integer"},
        "snapshot": {"type": "string"}, "clip": {"type": "string"},
    }, ["spec", "id", "src", "type", "severity", "ts", "message"]),
})

# ---------------------------------------------------------------- status
device_status = obj({"id": {"type": "string"},
                     "state": enum(["connecting", "connected", "unreachable", "disabled"]),
                     "last_ok": {"oneOf": [ref("timestamp"), {"type": "null"}]},
                     "error": ref("error")}, ["id", "state"])
write("status.schema.json", {
    "title": "Status heartbeat and Last Will (topic opcare/v1/{site}/status/{source_id}, retained)",
    "type": "object",
    "properties": {
        "spec": ref("spec"), "src": ref("sourceId"), "kind": {"const": "connector"},
        "type": ref("connectorType"), "version": {"type": "string"}, "boot": ref("uuid"),
        "state": enum(["starting", "waiting_config", "connecting", "running", "degraded", "error",
                       "stopping", "offline"]),
        "since": ref("timestamp"), "ts": ref("timestamp"), "uptime_s": integer(0),
        "config_revision": {"type": ["integer", "null"]},
        "devices": {"type": "array", "items": device_status},
        "stats": obj({"points": integer(0), "values_total": integer(0),
                      "values_per_s": {"type": "number", "minimum": 0}, "batches_total": integer(0),
                      "dropped_total": integer(0), "scan_overruns_total": integer(0)},
                     ["values_total", "batches_total", "dropped_total"]),
        "last_error": {"oneOf": [ref("error"), {"type": "null"}]},
    },
    "required": ["spec", "src", "kind", "boot", "state"],
    "additionalProperties": False,
    "if": {"properties": {"state": {"const": "offline"}}},
    "then": {"maxProperties": 5},
    "else": {"required": ["type", "version", "since", "ts", "uptime_s", "config_revision", "devices",
                          "stats", "last_error"]},
})

# ---------------------------------------------------------------- admin
OPS = ["ping", "get_diagnostics", "test_connection", "browse", "read_now", "reload_config"]
write("admin-request.schema.json", {
    "title": "Admin request (topic opcare/v1/{site}/admin/{source_id}/req)",
    **obj({
        "spec": ref("spec"), "request_id": ref("uuid"), "op": {"type": "string"},
        "args": {"type": "object", "default": {}},
        "timeout_ms": integer(100, 300000, 10000), "requested_by": {"type": "string", "maxLength": 100},
        "ts": ref("timestamp"),
    }, ["spec", "request_id", "op", "timeout_ms", "requested_by", "ts"],
        allOf=[
            {"if": {"properties": {"op": {"const": "browse"}}},
             "then": {"properties": {"args": obj({
                 "path": {"type": "string"}, "depth": integer(1, 3, 1), "max_items": integer(1, 5000, 500),
                 "cursor": {"type": "string"}, "listen_s": integer(1, 60), "config": {"type": "object"}})}}},
            {"if": {"properties": {"op": {"const": "read_now"}}},
             "then": {"properties": {"args": obj({
                 "tag_ids": {"type": "array", "items": ref("tagId"), "maxItems": 100},
                 "addresses": {"type": "array", "maxItems": 100, "items": obj(
                     {"address": {"type": "object"}, "data_type": ref("dataType")}, ["address", "data_type"])}},
                 minProperties=1)}}},
            {"if": {"properties": {"op": {"const": "test_connection"}}},
             "then": {"properties": {"args": obj({"config": {"type": "object"}})}}},
        ],
        description="Known ops: " + ", ".join(OPS) + ". Unknown ops must be answered with OP_UNSUPPORTED."),
})
write("admin-response.schema.json", {
    "title": "Admin response (topic opcare/v1/{site}/admin/{source_id}/res)",
    **obj({
        "spec": ref("spec"), "request_id": ref("uuid"), "src": ref("sourceId"),
        "ok": {"type": "boolean"}, "ts": ref("timestamp"),
        "done": {"type": "boolean", "default": True}, "cursor": {"type": "string"},
        "result": {"type": "object"}, "error": ref("error"),
    }, ["spec", "request_id", "src", "ok", "ts", "done"],
        **{"if": {"properties": {"ok": {"const": True}}}, "then": {"required": ["result"]},
           "else": {"required": ["error"]}}),
})

# ---------------------------------------------------------------- config envelope
write("config-envelope.schema.json", {
    "title": "Configuration envelope (topic opcare/v1/{site}/config/{source_id}, retained)",
    **obj({
        "spec": ref("spec"), "revision": integer(1), "site": ref("siteId"), "source_id": ref("sourceId"),
        "type": ref("connectorType"), "name": {"type": "string", "maxLength": 200},
        "enabled": {"type": "boolean"},
        "connection": {"type": "object"},
        "acquisition": ref("acquisitionCommon"),
        "points": {"type": "array", "items": ref("point"), "maxItems": 100000},
    }, ["spec", "revision", "site", "source_id", "type", "enabled", "connection", "points"]),
})


# ---------------------------------------------------------------- protocols
def protocol(type_, title, connection, address, acq_extra=None, conn_rules=None, addr_rules=None):
    conn = obj(connection[0], connection[1])
    if conn_rules:
        conn["allOf"] = conn_rules
    addr = obj(address[0], address[1])
    if addr_rules:
        addr["allOf"] = addr_rules
    acq = {"allOf": [ref("acquisitionCommon", True)], "properties": acq_extra or {},
           "unevaluatedProperties": False}
    write(f"protocols/{type_}.config.schema.json", {
        "title": title,
        "allOf": [
            {"$ref": "../config-envelope.schema.json"},
            {"properties": {
                "type": {"const": type_},
                "connection": conn,
                "acquisition": acq,
                "points": {"items": {"properties": {"address": addr}}},
            }},
        ],
    })


P = lambda n: ref(n, True)  # noqa: E731
TLS = P("tls")
SECRET = P("secretRef")

protocol("modbus", "Modbus TCP / RTU configuration",
         ({"transport": enum(["tcp", "rtu"]), "host": P("host"), "port": {**P("port"), "default": 502},
           "serial": obj({"device": {"type": "string"}, "baud": integer(300, 921600, 9600),
                          "parity": enum(["N", "E", "O"], "E"), "data_bits": enum([7, 8], 8),
                          "stop_bits": enum([1, 2], 1)}, ["device"]),
           "devices": {"type": "array", "minItems": 1, "items": obj(
               {"id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,62}$"},
                "unit_id": integer(0, 247), "name": {"type": "string"}}, ["id", "unit_id"])},
           "max_registers_per_read": integer(1, 125, 100), "max_bits_per_read": integer(1, 2000, 1000),
           "max_gap": integer(0, 100, 10), "inter_request_delay_ms": integer(0, 10000)},
          ["transport", "devices"]),
         ({"device": {"type": "string"},
           "table": enum(["coil", "discrete_input", "holding_register", "input_register"]),
           "address": integer(0, 65535),
           "encoding": enum(["bool", "bit", "int16", "uint16", "int32", "uint32", "int64", "uint64",
                             "float32", "float64", "string"]),
           "word_order": enum(["big", "little"], "big"), "byte_order": enum(["big", "little"], "big"),
           "bit": integer(0, 15), "length": integer(1, 125)},
          ["device", "table", "address", "encoding"]),
         conn_rules=[
             {"if": {"properties": {"transport": {"const": "tcp"}}}, "then": {"required": ["host"]}},
             {"if": {"properties": {"transport": {"const": "rtu"}}}, "then": {"required": ["serial"]}}],
         addr_rules=[
             {"if": {"properties": {"encoding": {"const": "bit"}}}, "then": {"required": ["bit"]}},
             {"if": {"properties": {"encoding": {"const": "string"}}}, "then": {"required": ["length"]}},
             {"if": {"properties": {"table": {"enum": ["coil", "discrete_input"]}}},
              "then": {"properties": {"encoding": {"const": "bool"}}}}])

protocol("opcua", "OPC UA client configuration",
         ({"endpoint_url": {"type": "string", "pattern": r"^opc\.tcp://"},
           "security_policy": enum(["None", "Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss"],
                                   "Basic256Sha256"),
           "security_mode": enum(["None", "Sign", "SignAndEncrypt"], "SignAndEncrypt"),
           "auth": enum(["anonymous", "username", "certificate"], "anonymous"),
           "username": {"type": "string"}, "password_ref": SECRET,
           "user_cert_ref": SECRET, "user_key_ref": SECRET,
           "client_cert_ref": SECRET, "client_key_ref": SECRET,
           "trusted_server_thumbprints": {"type": "array", "items": {"type": "string",
                                                                      "pattern": r"^[0-9A-Fa-f]{40}$"}},
           "insecure_skip_verify": {"type": "boolean", "default": False},
           "session_timeout_ms": integer(10000, 3600000, 60000)},
          ["endpoint_url"]),
         ({"node_id": {"type": "string", "pattern": r"^(ns=\d+|nsu=[^;]+);[isgb]=.+$|^i=\d+$"}}, ["node_id"]),
         acq_extra={"mode": enum(["subscribe", "poll"], "subscribe"),
                    "publishing_interval_ms": integer(50, 3600000, 1000),
                    "sampling_interval_ms": integer(-1, 3600000, -1), "queue_size": integer(1, 1000, 10),
                    "max_items_per_subscription": integer(1, 10000, 1000)},
         conn_rules=[
             {"if": {"properties": {"auth": {"const": "username"}}, "required": ["auth"]},
              "then": {"required": ["username", "password_ref"]}},
             {"if": {"properties": {"auth": {"const": "certificate"}}, "required": ["auth"]},
              "then": {"required": ["user_cert_ref", "user_key_ref"]}},
             {"if": {"not": {"properties": {"security_policy": {"const": "None"}}, "required": ["security_policy"]}},
              "then": {"required": ["client_cert_ref", "client_key_ref"]}}])

protocol("mqtt", "MQTT subscriber configuration",
         ({"broker_url": {"type": "string", "pattern": r"^mqtts?://"}, "client_id": {"type": "string"},
           "protocol_version": enum(["5", "3.1.1"], "5"),
           "auth": enum(["anonymous", "username", "certificate"], "anonymous"),
           "username": {"type": "string"}, "password_ref": SECRET, "client_cert_ref": SECRET,
           "client_key_ref": SECRET, "ca_cert_ref": SECRET,
           "insecure_skip_verify": {"type": "boolean", "default": False},
           "keepalive_s": integer(0, 3600, 60),
           "subscriptions": {"type": "array", "minItems": 1, "items": obj(
               {"topic_filter": {"type": "string", "minLength": 1}, "qos": enum([0, 1], 1)},
               ["topic_filter"])}},
          ["broker_url", "subscriptions"]),
         ({"format": enum(["json", "raw", "sparkplug_b"]), "topic": {"type": "string"},
           "value_pointer": {"type": "string", "pattern": r"^(/[^/]*)*$"},
           "timestamp_pointer": {"type": "string", "pattern": r"^(/[^/]*)*$"},
           "timestamp_format": enum(["rfc3339", "epoch_ms", "epoch_s"]),
           "group_id": {"type": "string"}, "edge_node_id": {"type": "string"},
           "device_id": {"type": "string"}, "metric": {"type": "string"}},
          ["format"]),
         addr_rules=[
             {"if": {"properties": {"format": {"const": "sparkplug_b"}}},
              "then": {"required": ["group_id", "edge_node_id", "metric"]},
              "else": {"required": ["topic"]}}])

protocol("iec61850", "IEC 61850 MMS client configuration",
         ({"host": P("host"), "port": {**P("port"), "default": 102},
           "ied_name": {"type": "string"}, "timeout_ms": integer(1000, 120000, 10000),
           "tls": TLS, "acse_auth": obj({"password_ref": SECRET}, ["password_ref"]),
           "scl_file_ref": SECRET},
          ["host", "ied_name"]),
         ({"object_ref": {"type": "string", "pattern": r"^[A-Za-z0-9_]{1,64}/[A-Za-z0-9_]+(\.[A-Za-z0-9_\[\]]+)+$"},
           "fc": enum(["MX", "ST", "CF", "DC", "EX"])},
          ["object_ref", "fc"]),
         acq_extra={"reports": {"type": "array", "items": obj({
             "rcb_ref": {"type": "string", "pattern": r"^[A-Za-z0-9_]{1,64}/[A-Za-z0-9_]+\.(BR|RP)\.[A-Za-z0-9_]+$"},
             "buffered": {"type": "boolean", "default": True},
             "trigger_options": {"type": "array", "uniqueItems": True,
                                 "items": enum(["dchg", "qchg", "dupd", "integrity", "gi"])},
             "integrity_period_ms": integer(0), "buf_time_ms": integer(0),
             "reserve": {"type": "boolean", "default": True}}, ["rcb_ref"])},
             "poll_unreported": {"type": "boolean", "default": True},
             "gi_on_connect": {"type": "boolean", "default": True}})

protocol("iec104", "IEC 60870-5-104 master configuration",
         ({"hosts": {"type": "array", "minItems": 1, "maxItems": 2, "items": obj(
             {"host": P("host"), "port": {**P("port"), "default": 2404}}, ["host"])},
           "common_addresses": {"type": "array", "minItems": 1, "items": integer(1, 65534)},
           "originator_address": integer(0, 255, 0),
           "k": integer(1, 32767, 12), "w": integer(1, 32767, 8),
           "t0_s": integer(1, 255, 30), "t1_s": integer(1, 255, 15), "t2_s": integer(1, 255, 10),
           "t3_s": integer(1, 172800, 20), "device_time_zone": {"type": "string", "default": "UTC"},
           "tls": TLS},
          ["hosts", "common_addresses"]),
         ({"ca": integer(1, 65534), "ioa": integer(1, 16777215),
           "type": enum(["M_SP_NA_1", "M_SP_TB_1", "M_DP_NA_1", "M_DP_TB_1", "M_ST_NA_1", "M_ST_TB_1",
                         "M_BO_NA_1", "M_BO_TB_1", "M_ME_NA_1", "M_ME_TD_1", "M_ME_NB_1", "M_ME_TE_1",
                         "M_ME_NC_1", "M_ME_TF_1", "M_IT_NA_1", "M_IT_TB_1"])},
          ["ca", "ioa"]),
         acq_extra={"gi_on_connect": {"type": "boolean", "default": True}, "gi_period_s": integer(0, 86400, 900),
                    "counter_interrogation_period_s": integer(0, 86400, 0)})

protocol("dnp3", "DNP3 master (TCP) configuration",
         ({"host": P("host"), "port": {**P("port"), "default": 20000},
           "master_address": integer(0, 65519, 1), "outstation_address": integer(0, 65519, 10),
           "response_timeout_ms": integer(100, 120000, 5000), "keep_alive_s": integer(0, 3600, 60),
           "tls": TLS},
          ["host", "outstation_address"]),
         ({"group": enum(["binary_input", "double_bit_binary_input", "analog_input", "counter", "frozen_counter",
                          "binary_output_status", "analog_output_status"]),
           "index": integer(0, 65535)},
          ["group", "index"]),
         acq_extra={"integrity_on_connect": {"type": "boolean", "default": True},
                    "integrity_period_s": integer(0, 86400, 3600),
                    "class_polls": obj({"class1_ms": integer(0, 3600000, 5000),
                                        "class2_ms": integer(0, 3600000, 5000),
                                        "class3_ms": integer(0, 3600000, 5000)}),
                    "unsolicited": {"type": "boolean", "default": False}})

protocol("camera", "IP camera (ONVIF / RTSP) configuration",
         ({"host": P("host"), "onvif_port": {**P("port"), "default": 80}, "onvif": {"type": "boolean"},
           "username": {"type": "string"}, "password_ref": SECRET, "profile_token": {"type": "string"},
           "rtsp_url": {"type": "string", "pattern": r"^rtsps?://"},
           "rtsp_transport": enum(["tcp", "udp"], "tcp"), "tls": TLS},
          ["host", "onvif"]),
         ({"metric": enum(["online", "fps", "bitrate_kbps", "resolution", "codec", "onvif_event"]),
           "topic": {"type": "string"}},
          ["metric"]),
         conn_rules=[{"if": {"properties": {"onvif": {"const": False}}}, "then": {"required": ["rtsp_url"]}}],
         addr_rules=[{"if": {"properties": {"metric": {"const": "onvif_event"}}}, "then": {"required": ["topic"]}}])

DEVICE_ID = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,62}$"}
IP_OR_CIDR = {"type": "string", "minLength": 2, "maxLength": 64,
              "pattern": r"^([0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(/[0-9]{1,3})?)$"}
SEVERITIES = ["emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"]
POINTER = {"type": "string", "pattern": r"^(/[^/]*)*$"}

protocol("udp", "UDP datagram listener configuration",
         ({"bind_host": {"type": "string", "default": "0.0.0.0", "maxLength": 64},
           "bind_port": P("port"),
           "senders": {"type": "array", "minItems": 1, "maxItems": 500, "items": obj(
               {"id": DEVICE_ID, "host": IP_OR_CIDR, "name": {"type": "string", "maxLength": 100}}, ["id", "host"])},
           "silence_timeout_s": integer(0, 86400, 60),
           "max_datagram_bytes": integer(64, 65507, 8192),
           "text_encoding": enum(["utf-8", "ascii", "latin-1"], "utf-8"),
           "syslog": obj({"min_severity": enum(SEVERITIES, "warning")})},
          ["bind_port", "senders"]),
         ({"device": {"type": "string"},
           "format": enum(["json", "delimited", "nmea", "binary", "raw", "syslog"]),
           "prefix": {"type": "string", "minLength": 1, "maxLength": 64},
           "prefix_hex": {"type": "string", "pattern": r"^([0-9a-fA-F]{2}){1,32}$"},
           "value_pointer": POINTER, "timestamp_pointer": POINTER,
           "timestamp_format": enum(["rfc3339", "epoch_ms", "epoch_s"]),
           "match_pointer": POINTER, "match_value": {"type": "string", "maxLength": 200},
           "delimiter": {"type": "string", "minLength": 1, "maxLength": 4, "default": ","},
           "field": {"oneOf": [integer(0, 1000),
                               enum(["severity", "facility", "message", "app_name", "hostname"])]},
           "sentence": {"type": "string", "pattern": r"^[A-Z0-9]{3,5}$"},
           "coordinate": {"type": "boolean", "default": False},
           "offset": integer(0, 65506),
           "encoding": enum(["bool", "bit", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64",
                             "float32", "float64", "string"]),
           "byte_order": enum(["big", "little"], "big"),
           "bit": integer(0, 7), "length": integer(1, 1024),
           "match": {"type": "string", "minLength": 1, "maxLength": 200}},
          ["device", "format"]),
         addr_rules=[
             {"if": {"properties": {"format": {"const": "json"}}},
              "then": {"required": ["value_pointer"],
                       "dependentRequired": {"match_pointer": ["match_value"], "match_value": ["match_pointer"]}}},
             {"if": {"properties": {"format": {"const": "delimited"}}},
              "then": {"required": ["field"], "properties": {"field": {"type": "integer"}}}},
             {"if": {"properties": {"format": {"const": "nmea"}}},
              "then": {"required": ["sentence", "field"], "properties": {"field": integer(1, 100)}}},
             {"if": {"properties": {"format": {"const": "binary"}}},
              "then": {"required": ["offset", "encoding"]}},
             {"if": {"properties": {"format": {"const": "syslog"}}},
              "then": {"required": ["field"], "properties": {"field": {"type": "string"}}}},
             {"if": {"properties": {"encoding": {"const": "bit"}}, "required": ["encoding"]},
              "then": {"required": ["bit"]}},
             {"if": {"properties": {"encoding": {"const": "string"}}, "required": ["encoding"]},
              "then": {"required": ["length"]}}])

protocol("snmp", "SNMP manager configuration",
         ({"version": enum(["1", "2c", "3"], "2c"),
           "port": {**P("port"), "default": 161},
           "community_ref": SECRET,
           "v3": obj({"username": {"type": "string", "minLength": 1, "maxLength": 32},
                      "security_level": enum(["noAuthNoPriv", "authNoPriv", "authPriv"], "authPriv"),
                      "auth_protocol": enum(["MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"], "SHA-256"),
                      "auth_password_ref": SECRET,
                      "priv_protocol": enum(["AES"], "AES"),
                      "priv_password_ref": SECRET,
                      "context_name": {"type": "string", "maxLength": 32, "default": ""}},
                     ["username"],
                     allOf=[{"if": {"not": {"properties": {"security_level": {"const": "noAuthNoPriv"}},
                                            "required": ["security_level"]}},
                             "then": {"required": ["auth_password_ref"]}},
                            {"if": {"not": {"properties": {"security_level": {"enum": ["noAuthNoPriv", "authNoPriv"]}},
                                            "required": ["security_level"]}},
                             "then": {"required": ["priv_password_ref"]}}]),
           "devices": {"type": "array", "minItems": 1, "maxItems": 500, "items": obj(
               {"id": DEVICE_ID, "host": P("host"), "port": P("port"),
                "name": {"type": "string", "maxLength": 100}}, ["id", "host"])},
           "max_oids_per_request": integer(1, 100, 20),
           "max_repetitions": integer(1, 100, 25),
           "retries": integer(0, 5, 1),
           "traps": obj({"bind_host": {"type": "string", "default": "0.0.0.0", "maxLength": 64},
                         "bind_port": P("port"), "community_ref": SECRET}, ["bind_port"])},
          ["version", "devices"]),
         ({"device": {"type": "string"},
           "oid": {"type": "string", "maxLength": 300, "pattern": r"^\.?[0-2](\.(0|[1-9][0-9]{0,9}))+$"},
           "transform": enum(["none", "rate"], "none")},
          ["device", "oid"]),
         conn_rules=[
             {"if": {"properties": {"version": {"enum": ["1", "2c"]}}}, "then": {"required": ["community_ref"]}},
             {"if": {"properties": {"version": {"const": "3"}}}, "then": {"required": ["v3"]}}])

print("schemas written to", ROOT)
