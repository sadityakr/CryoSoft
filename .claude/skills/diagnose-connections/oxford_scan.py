#!/usr/bin/env python
"""Scan for and query Oxford Instruments devices (IPS120, ITC503, Mercury iPS-M).

Oxford instruments use non-standard command sets and protocols:
  - ITC503: Legacy commands (V, R0-R13) with CR termination
  - IPS120/Mercury iPS-M: SCPI commands with LF termination

This tool sends type-specific commands to detect and identify Oxford devices on
serial ports without locking them.

Input: Serial port(s), instrument type(s), baud rate, query command.
Process: Send Oxford-specific commands, parse responses with protocol awareness.
Output: Device identity and diagnostic data (human-readable or JSON), exit code reflects success.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diagnostic_utils import query_oxford_instrument, OXFORD_COMMANDS, enumerate_serial_ports


def main():
    parser = argparse.ArgumentParser(
        description="Query Oxford Instruments devices (IPS, ITC, Mercury PSU) on serial ports."
    )
    parser.add_argument(
        "--port",
        type=str,
        help="Serial port to query (e.g., COM3). If not specified, scans all ports.",
    )
    parser.add_argument(
        "--type",
        type=str,
        choices=list(OXFORD_COMMANDS.keys()),
        default="IPS120",
        help=f"Oxford instrument type. Choose from: {', '.join(OXFORD_COMMANDS.keys())} (default: IPS120)",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=9600,
        help="Baud rate (default: 9600). Try 19200 or 57600 if 9600 fails.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1000,
        help="Query timeout in milliseconds (default: 1000)",
    )
    parser.add_argument(
        "--scan-all-types",
        action="store_true",
        help="Try all Oxford instrument types on each port (slow but comprehensive)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show progress for each attempt")

    args = parser.parse_args()

    results = {"scanned_ports": [], "found_devices": []}

    if args.port and args.type:
        # Query specific port and type
        if args.verbose:
            print(f"Querying {args.port} for {args.type} (identity command)...")

        result = query_oxford_instrument(args.port, args.type, "identity", args.baudrate, args.timeout)
        results["scanned_ports"].append(args.port)

        if result["success"]:
            device_info = {
                "port": args.port,
                "type": args.type,
                "identity": result["parsed"],
                "response": result["response"],
                "raw_response": result["response"],
            }
            results["found_devices"].append(device_info)
            if args.verbose:
                print(f"✓ Found {args.type} on {args.port}")
                print(f"  Identity: {result['parsed']}")
        else:
            if args.verbose:
                print(f"✗ No response on {args.port}: {result['error']}")

    elif args.port:
        # Query specific port with all types (or specified type)
        types_to_try = [args.type]
        results["scanned_ports"].append(args.port)

        for instrument_type in types_to_try:
            if args.verbose:
                print(f"  {instrument_type}...", end=" ", flush=True)

            result = query_oxford_instrument(args.port, instrument_type, "identity", args.baudrate, args.timeout)

            if result["success"]:
                device_info = {
                    "port": args.port,
                    "type": instrument_type,
                    "identity": result["parsed"],
                    "response": result["response"],
                }
                results["found_devices"].append(device_info)
                if args.verbose:
                    print("✓")
            else:
                if args.verbose:
                    print(f"✗ ({result['error'][:30]})")

    else:
        # Scan all ports
        all_ports = enumerate_serial_ports(test_query=False)
        types_to_try = list(OXFORD_COMMANDS.keys()) if args.scan_all_types else [args.type]

        if args.verbose:
            print(f"Scanning {len(all_ports)} serial port(s) for Oxford instruments...")

        for port_info in all_ports:
            port = port_info["port"]
            results["scanned_ports"].append(port)

            for instrument_type in types_to_try:
                if args.verbose:
                    print(f"  {port}: {instrument_type}...", end=" ", flush=True)

                result = query_oxford_instrument(port, instrument_type, "identity", args.baudrate, args.timeout)

                if result["success"]:
                    device_info = {
                        "port": port,
                        "type": instrument_type,
                        "identity": result["parsed"],
                        "response": result["response"],
                    }
                    results["found_devices"].append(device_info)
                    if args.verbose:
                        print("✓")
                else:
                    if args.verbose and args.scan_all_types:
                        print("✗", end=" ")

            if args.verbose and not args.scan_all_types:
                print()

    # Output
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("\nOxford Instruments Scan Results")
        print("=" * 60)
        print(f"Ports scanned: {len(results['scanned_ports'])}")
        print(f"Devices found: {len(results['found_devices'])}\n")

        if results["found_devices"]:
            for device in results["found_devices"]:
                print(f"✓ {device['port']} — {device['type']}")
                print(f"  Identity: {device['identity']}")
                print(f"  Raw response: {device['response'][:100]}")
                print()
        else:
            print("No Oxford instruments found.")
            print("\nTroubleshooting:")
            print("  • Check port connections and device power")
            print("  • Verify baud rate (try --baudrate 19200 or 57600)")
            print("  • ITC503 uses CR termination; IPS120/Mercury use LF")
            print("  • Use --scan-all-types to try all instrument types")
            print("  • Increase timeout: --timeout 2000 for slow devices")
            print("\nSupported instruments:")
            for itype, info in OXFORD_COMMANDS.items():
                print(f"  • {itype}: {info['description']}")

    return 0 if results["found_devices"] else 1


if __name__ == "__main__":
    sys.exit(main())
