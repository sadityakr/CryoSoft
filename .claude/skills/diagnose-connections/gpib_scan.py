#!/usr/bin/env python
"""Scan GPIB bus for connected devices without locking them.

Probes each address on a GPIB board and reports device presence. Uses PyVISA with
short timeouts to avoid acquiring exclusive locks (unlike NIMAX). Each query opens
the resource briefly, reads identity, and closes immediately.

Input: GPIB board number (default 0), timeout, verbosity.
Process: Enumerate addresses 0-30, send *IDN? query to each, collect responses.
Output: Device list (human-readable or JSON), exit code reflects success.
"""

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path to import diagnostic_utils
sys.path.insert(0, str(Path(__file__).parent))

from diagnostic_utils import enumerate_gpib_devices, format_human_readable


def main():
    parser = argparse.ArgumentParser(
        description="Scan GPIB bus for connected instruments without device locking."
    )
    parser.add_argument("--board", type=int, default=0, help="GPIB board number (default: 0)")
    parser.add_argument("--timeout", type=int, default=500, help="Timeout per address in ms (default: 500)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show progress for each address")

    args = parser.parse_args()

    if args.verbose:
        print(f"Scanning GPIB board {args.board}...")

    devices = enumerate_gpib_devices(board=args.board, timeout_ms=args.timeout, verbose=args.verbose)

    present_count = sum(1 for d in devices.values() if d["present"])

    if args.json:
        output = {
            "board": args.board,
            "scan_result": devices,
            "summary": {"total_addresses": len(devices), "devices_found": present_count},
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_human_readable(devices, resource_type="GPIB"))
        if present_count == 0:
            print("\nNo devices found. Check connections or try a longer timeout (--timeout).")

    return 0 if present_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
