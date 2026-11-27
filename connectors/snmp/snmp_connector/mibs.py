"""Names of common standard MIB objects, so browse results read as `ifOperStatus.3` instead of bare numbers.

Only standard IETF MIBs (no vendor MIB files are shipped): SNMPv2-MIB, IF-MIB, IP-MIB, HOST-RESOURCES-MIB,
UPS-MIB (RFC 1628), ENTITY-SENSOR-MIB, SNMPv2 traps.
"""
from __future__ import annotations

NAMES = {
    "1.3.6.1.2.1.1.1": "sysDescr", "1.3.6.1.2.1.1.2": "sysObjectID", "1.3.6.1.2.1.1.3": "sysUpTime",
    "1.3.6.1.2.1.1.4": "sysContact", "1.3.6.1.2.1.1.5": "sysName", "1.3.6.1.2.1.1.6": "sysLocation",
    "1.3.6.1.2.1.1.7": "sysServices",
    "1.3.6.1.2.1.2.1": "ifNumber", "1.3.6.1.2.1.2.2.1.1": "ifIndex", "1.3.6.1.2.1.2.2.1.2": "ifDescr",
    "1.3.6.1.2.1.2.2.1.3": "ifType", "1.3.6.1.2.1.2.2.1.4": "ifMtu", "1.3.6.1.2.1.2.2.1.5": "ifSpeed",
    "1.3.6.1.2.1.2.2.1.6": "ifPhysAddress", "1.3.6.1.2.1.2.2.1.7": "ifAdminStatus",
    "1.3.6.1.2.1.2.2.1.8": "ifOperStatus", "1.3.6.1.2.1.2.2.1.9": "ifLastChange",
    "1.3.6.1.2.1.2.2.1.10": "ifInOctets", "1.3.6.1.2.1.2.2.1.11": "ifInUcastPkts",
    "1.3.6.1.2.1.2.2.1.13": "ifInDiscards", "1.3.6.1.2.1.2.2.1.14": "ifInErrors",
    "1.3.6.1.2.1.2.2.1.16": "ifOutOctets", "1.3.6.1.2.1.2.2.1.17": "ifOutUcastPkts",
    "1.3.6.1.2.1.2.2.1.19": "ifOutDiscards", "1.3.6.1.2.1.2.2.1.20": "ifOutErrors",
    "1.3.6.1.2.1.31.1.1.1.1": "ifName", "1.3.6.1.2.1.31.1.1.1.6": "ifHCInOctets",
    "1.3.6.1.2.1.31.1.1.1.10": "ifHCOutOctets", "1.3.6.1.2.1.31.1.1.1.15": "ifHighSpeed",
    "1.3.6.1.2.1.31.1.1.1.18": "ifAlias",
    "1.3.6.1.2.1.25.1.1": "hrSystemUptime", "1.3.6.1.2.1.25.1.6": "hrSystemProcesses",
    "1.3.6.1.2.1.25.2.2": "hrMemorySize", "1.3.6.1.2.1.25.2.3.1.3": "hrStorageDescr",
    "1.3.6.1.2.1.25.2.3.1.4": "hrStorageAllocationUnits", "1.3.6.1.2.1.25.2.3.1.5": "hrStorageSize",
    "1.3.6.1.2.1.25.2.3.1.6": "hrStorageUsed", "1.3.6.1.2.1.25.3.3.1.2": "hrProcessorLoad",
    "1.3.6.1.2.1.33.1.1.1": "upsIdentManufacturer", "1.3.6.1.2.1.33.1.1.2": "upsIdentModel",
    "1.3.6.1.2.1.33.1.2.1": "upsBatteryStatus", "1.3.6.1.2.1.33.1.2.2": "upsSecondsOnBattery",
    "1.3.6.1.2.1.33.1.2.3": "upsEstimatedMinutesRemaining", "1.3.6.1.2.1.33.1.2.4": "upsEstimatedChargeRemaining",
    "1.3.6.1.2.1.33.1.2.5": "upsBatteryVoltage", "1.3.6.1.2.1.33.1.2.7": "upsBatteryTemperature",
    "1.3.6.1.2.1.33.1.3.3.1.3": "upsInputVoltage", "1.3.6.1.2.1.33.1.4.1": "upsOutputSource",
    "1.3.6.1.2.1.33.1.4.4.1.2": "upsOutputVoltage", "1.3.6.1.2.1.33.1.4.4.1.5": "upsOutputPercentLoad",
    "1.3.6.1.2.1.99.1.1.1.4": "entPhySensorValue", "1.3.6.1.2.1.99.1.1.1.5": "entPhySensorOperStatus",
}

TRAPS = {
    "1.3.6.1.6.3.1.1.5.1": "coldStart", "1.3.6.1.6.3.1.1.5.2": "warmStart", "1.3.6.1.6.3.1.1.5.3": "linkDown",
    "1.3.6.1.6.3.1.1.5.4": "linkUp", "1.3.6.1.6.3.1.1.5.5": "authenticationFailure",
    "1.3.6.1.6.3.1.1.5.6": "egpNeighborLoss",
    "1.3.6.1.2.1.33.2.0.1": "upsTrapOnBattery", "1.3.6.1.2.1.33.2.0.2": "upsTrapTestCompleted",
    "1.3.6.1.2.1.33.2.0.3": "upsTrapAlarmEntryAdded", "1.3.6.1.2.1.33.2.0.4": "upsTrapAlarmEntryRemoved",
}
TRAP_SEVERITY = {"linkDown": "medium", "authenticationFailure": "medium", "upsTrapOnBattery": "high",
                 "upsTrapAlarmEntryAdded": "medium"}


def name_of(oid: str) -> str:
    """'1.3.6.1.2.1.2.2.1.8.3' -> 'ifOperStatus.3' (longest known prefix), else the OID."""
    parts = oid.strip(".").split(".")
    for n in range(len(parts), 5, -1):
        base = ".".join(parts[:n])
        if base in NAMES:
            rest = parts[n:]
            return NAMES[base] + ("." + ".".join(rest) if rest else "")
    return oid
