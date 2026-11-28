"""Test-only: an independent DNP3 outstation (opendnp3 through the Apache-2.0 'dnp3-python' package, Python 3.8-3.10)
for the CI job 'dnp3-interop'. Never part of the product or its images.

    python3.10 tools/interop/opendnp3_outstation.py PORT
Prints READY when listening. Reads commands on stdin, one per line:
    analog INDEX VALUE | binary INDEX 0|1 | aostatus INDEX VALUE | bostatus INDEX 0|1 | quit
Link addresses: outstation 10, master 1. Five points of each type: analog (class 2), binary (class 2), aostatus, bostatus.
"""
import sys
import time

from dnp3_python.dnp3station.outstation import OutStationApplication
from pydnp3 import opendnp3


def main() -> None:
    port = int(sys.argv[1])
    os_ = OutStationApplication(outstation_ip="127.0.0.1", port=port, master_id=1, outstation_id=10,
                                numBinary=5, numBinaryOutputStatus=5, numAnalog=5, numAnalogOutputStatus=5)
    os_.start()
    time.sleep(0.5)
    for i in range(5):
        os_.apply_update_analog_input(100.0 + i + 0.5, i)
        os_.apply_update_binary_input(i % 2 == 0, i)
        os_.apply_update_analog_output(10.0 * i, i)
        os_.apply_update_binary_output(i % 2 == 1, i)
    print("READY", flush=True)
    for line in sys.stdin:
        parts = line.split()
        if not parts or parts[0] == "quit":
            break
        kind, index, value = parts[0], int(parts[1]), parts[2]
        if kind == "analog":
            os_.apply_update_analog_input(float(value), index)
        elif kind == "binary":
            os_.apply_update_binary_input(value == "1", index)
        elif kind == "aostatus":
            os_.apply_update_analog_output(float(value), index)
        elif kind == "bostatus":
            os_.apply_update_binary_output(value == "1", index)
        print("OK", flush=True)
    os_.shutdown()
    _ = opendnp3


if __name__ == "__main__":
    main()
