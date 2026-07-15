#!/usr/bin/env python
"""Query a specific GPIB or serial device for identity without locking.

Sends a read-only query (e.g., *IDN?) to a single instrument identified by
resource string (GPIB) or port name (serial). Uses non-blocking I/O and
releases the resource immediately after reading the response.

Input: Resource string (GPIB address or serial port), query command, timeout.
Process: Open resource, send query, read response, close.
Output: Device response or error message (human-readable or JSON), exit code reflects success.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diagnostic_utils import try_visa_query, query_serial_device


def main():
    parser = argparse.ArgumentParser(
        description="Query a specific GPIB or serial device for identity or other read-only commands."
    )
    parser.add_argument(
        "resource",
        help='Resource string: GPIB (e.g., "gpib0::5::INSTR") or serial port (e.g., "COM3")',
    )
    parser.add_argument(
        "--query",
        type=str,
        default="*IDN?",
        help='Query command to send (default: "*IDN?")',
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=9600,
        help="Baud rate for serial ports (default: 9600)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1000,
        help="Response timeout in milliseconds (default: 1000)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Detect if resource is GPIB or serial
    is_gpib = "gpib" in args.resource.lower() or "::" in args.resource
    is_serial = args.resource.upper().startswith("COM") or args.resource.startswith("/dev/")

    result = {}
    if is_gpib:
        response = try_visa_query(args.resource, args.query, args.timeout)
        result = {
            "resource": args.resource,
            "type": "GPIB",
            "query": args.query,
            "success": response is not None,
            "response": response,
            "error": None if response else "No response or timeout",
        }
    elif is_serial:
        query_result = query_serial_device(args.resource, args.query, args.baudrate, args.timeout)
        result = {
            "resource": args.resource,
            "type": "Serial",
            "query": args.query,
            "baudrate": args.baudrate,
            "success": query_result["success"],
            "response": query_result["response"],
            "error": query_result["error"],
        }
    else:
        result = {
            "resource": args.resource,
            "success": False,
            "error": "Unrecognized resource format. Use 'gpibX::Y::INSTR' for GPIB or 'COMX' for serial.",
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Error: {result['error']}")
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["success"]:
            print(f"✓ {result['resource']}")
            print(f"  Query: {result['query']}")
            print(f"  Response: {result['response']}")
        else:
            print(f"✗ {result['resource']}")
            print(f"  Query: {result['query']}")
            print(f"  Error: {result['error']}")

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
