#!/usr/bin/env python
"""Generate a JSON snapshot of instrument connection status.

Scans GPIB and serial connections, queries each device for identity, and writes
a JSON status file (cryosoft/logs/connection_status.json) with timestamp and
device status. Useful for agents to check connectivity before running procedures.

Input: Optional address list, output path.
Process: Scan GPIB and serial, query known instruments, write status JSON.
Output: connection_status.json with timestamp and per-device status, exit code reflects success.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diagnostic_utils import (
    enumerate_gpib_devices,
    enumerate_serial_ports,
    try_visa_query,
    query_serial_device,
    discover_oxford_instrument,
)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a JSON snapshot of instrument connection status."
    )
    parser.add_argument(
        "--addresses",
        type=str,
        help='Space-separated list of known instrument addresses (e.g., "gpib0::5 gpib0::24 COM3")',
    )
    parser.add_argument(
        "--output",
        type=str,
        default="cryosoft/logs/connection_status.json",
        help="Output file path (default: cryosoft/logs/connection_status.json)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout instead of writing to file",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Scan all GPIB/serial addresses (can be slow); default is to check known addresses only",
    )

    args = parser.parse_args()

    status = {
        "timestamp": datetime.now().isoformat(),
        "instruments": {},
        "summary": {"total_checked": 0, "connected": 0, "unavailable": 0},
    }

    # If specific addresses provided, use those; otherwise scan GPIB/serial
    if args.addresses:
        addresses = args.addresses.split()
    elif args.full_scan:
        addresses = []
        # Add all GPIB addresses
        gpib_devices = enumerate_gpib_devices(board=0, timeout_ms=500, verbose=False)
        for addr, info in gpib_devices.items():
            if info["present"]:
                addresses.append(f"gpib0::{addr}::INSTR")
        # Add all serial ports
        serial_ports = enumerate_serial_ports(test_query=False, timeout_ms=500)
        for port_info in serial_ports:
            addresses.append(port_info["port"])
    else:
        addresses = []

    # Query each address
    for address in addresses:
        is_gpib = "gpib" in address.lower() or "::" in address
        is_serial = address.upper().startswith("COM") or address.startswith("/dev/")

        status["summary"]["total_checked"] += 1

        if is_gpib:
            response = try_visa_query(address, "*IDN?", timeout_ms=1000)
            if response:
                status["instruments"][address] = {
                    "connected": True,
                    "identity": response,
                    "type": "GPIB",
                }
                status["summary"]["connected"] += 1
            else:
                status["instruments"][address] = {
                    "connected": False,
                    "identity": None,
                    "type": "GPIB",
                    "error": "No response or timeout",
                }
                status["summary"]["unavailable"] += 1

        elif is_serial:
            query_result = query_serial_device(address, "*IDN?", baudrate=9600, timeout_ms=1000)
            if not query_result["success"]:
                oxford_info = discover_oxford_instrument(address, timeout_ms=1000)
                if oxford_info:
                    status["instruments"][address] = {
                        "connected": True,
                        "identity": f"{oxford_info['instrument_type']}: {oxford_info['parsed']}",
                        "type": "Serial",
                    }
                    status["summary"]["connected"] += 1
                    continue

            if query_result["success"]:
                status["instruments"][address] = {
                    "connected": True,
                    "identity": query_result["response"],
                    "type": "Serial",
                }
                status["summary"]["connected"] += 1
            else:
                status["instruments"][address] = {
                    "connected": False,
                    "identity": None,
                    "type": "Serial",
                    "error": query_result["error"],
                }
                status["summary"]["unavailable"] += 1

    # Output
    output_text = json.dumps(status, indent=2)

    if args.stdout:
        print(output_text)
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text)
        print(f"✓ Status written to {output_path}")
        print(f"  {status['summary']['connected']} connected, {status['summary']['unavailable']} unavailable")

    return 0 if status["summary"]["connected"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
