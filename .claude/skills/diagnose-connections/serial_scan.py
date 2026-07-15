#!/usr/bin/env python
"""Scan serial ports for connected devices and test connectivity.

Enumerates all available serial ports using pyserial's comports(). Optionally
sends *IDN? query to each port to check if a device is responding without
acquiring exclusive locks.

Input: Optional port filter, timeout, verbosity.
Process: List all COM ports, attempt brief *IDN? query on each.
Output: Port list with device info (human-readable or JSON), exit code reflects success.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diagnostic_utils import enumerate_serial_ports, format_human_readable


def main():
    parser = argparse.ArgumentParser(
        description="Scan serial ports for connected instruments."
    )
    parser.add_argument("--port", type=str, help="Test a specific port (e.g., COM3)")
    parser.add_argument("--timeout", type=int, default=500, help="Query timeout in ms (default: 500)")
    parser.add_argument("--no-query", action="store_true", help="Skip device queries; just list ports")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show progress for each port")

    args = parser.parse_args()

    if args.verbose:
        print("Scanning serial ports...")

    all_ports = enumerate_serial_ports(test_query=not args.no_query, timeout_ms=args.timeout)

    # Filter if a specific port was requested
    if args.port:
        ports = [p for p in all_ports if p["port"].upper() == args.port.upper()]
        if not ports:
            print(f"Port {args.port} not found. Available ports: {[p['port'] for p in all_ports]}")
            return 2
    else:
        ports = all_ports

    available_count = sum(1 for p in ports if p["available"])

    if args.json:
        output = {
            "ports": ports,
            "summary": {"total_ports": len(ports), "available": available_count},
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_human_readable(ports, resource_type="Serial"))
        if available_count == 0:
            print("\nNo available devices found. Check connections or try --no-query to just list ports.")

    return 0 if available_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
