#!/usr/bin/env python
"""Query a specific Oxford Instruments device with type-specific commands.

Provides detailed diagnostic queries for Oxford Instruments IPS120, ITC503, and
Mercury iPS-M. Includes identity queries, status reads, temperature reads, current
measurements, and error checking.

Input: Port, instrument type, command name (or custom command).
Process: Send Oxford-specific command with proper protocol handling (terminator).
Output: Device response with parsing (human-readable or JSON), exit code reflects success.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diagnostic_utils import query_oxford_instrument, OXFORD_COMMANDS

try:
    import serial
except ImportError:
    print("Error: pyserial not installed. Install with: pip install pyserial")
    sys.exit(3)


def send_custom_oxford_command(port: str, command: str, baudrate: int = 9600, timeout_ms: int = 1000) -> dict:
    """Send a custom command to an Oxford instrument.

    For advanced users who need to send non-standard commands.

    Args:
        port: Serial port name.
        command: Raw command string (terminator will be added).
        baudrate: Baud rate.
        timeout_ms: Read timeout in milliseconds.

    Returns:
        Dict with 'success', 'response', and 'error' keys.
    """
    try:
        ser = serial.Serial(port, baudrate=baudrate, timeout=timeout_ms / 1000.0)
        # Try both CR and LF terminators (common in Oxford instruments)
        for terminator in ["\r", "\n"]:
            command_with_terminator = command.rstrip("\r\n") + terminator
            ser.write(command_with_terminator.encode("utf-8"))
            response = ser.read(256).decode("utf-8", errors="ignore").strip()
            ser.close()
            if response:
                return {"success": True, "response": response, "error": None, "terminator": repr(terminator)}

        return {"success": False, "response": None, "error": "No response from device", "terminator": None}
    except Exception as e:
        return {"success": False, "response": None, "error": str(e), "terminator": None}


def main():
    parser = argparse.ArgumentParser(
        description="Query specific Oxford Instruments devices with detailed diagnostic commands."
    )
    parser.add_argument("port", help="Serial port (e.g., COM3)")
    parser.add_argument(
        "--type",
        type=str,
        choices=list(OXFORD_COMMANDS.keys()),
        default="IPS120",
        help=f"Instrument type (default: IPS120). Choose from: {', '.join(OXFORD_COMMANDS.keys())}",
    )
    parser.add_argument(
        "--command",
        type=str,
        help='Command name (e.g., "identity", "current", "status"). Use --list-commands to see options.',
    )
    parser.add_argument(
        "--list-commands",
        action="store_true",
        help="List all available commands for the specified instrument type",
    )
    parser.add_argument(
        "--custom",
        type=str,
        help="Send a custom command (advanced: will try CR and LF terminators)",
    )
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate (default: 9600)")
    parser.add_argument("--timeout", type=int, default=1000, help="Timeout in milliseconds (default: 1000)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--hex", action="store_true", help="Show response as hex bytes")

    args = parser.parse_args()

    # Handle list-commands
    if args.list_commands:
        cmd_set = OXFORD_COMMANDS[args.type]
        print(f"\nAvailable commands for {args.type}:")
        print(f"Description: {cmd_set['description']}")
        print(f"Interface: {', '.join(cmd_set['interface'])}")
        if "protocol" in cmd_set:
            print(f"Protocol: {cmd_set['protocol']}")
        print("\nCommands:")
        print("-" * 70)
        for cmd_name, cmd_info in cmd_set.get("commands", {}).items():
            print(f"  {cmd_name:20s} | {cmd_info['description']}")
            print(f"    Command: {cmd_info['command']}")
            print(f"    Response format: {cmd_info['response_format']}")
        return 0

    # Handle custom command
    if args.custom:
        result = send_custom_oxford_command(args.port, args.custom, args.baudrate, args.timeout)
        if args.json:
            output = {
                "port": args.port,
                "custom_command": args.custom,
                "baudrate": args.baudrate,
                "success": result["success"],
                "response": result["response"],
                "response_hex": result["response"].encode("utf-8").hex() if result["response"] else None,
                "terminator_used": result["terminator"],
                "error": result["error"],
            }
            print(json.dumps(output, indent=2))
        else:
            if result["success"]:
                print(f"✓ {args.port}")
                print(f"  Custom command: {args.custom}")
                print(f"  Terminator: {result['terminator']}")
                print(f"  Response: {result['response']}")
                if args.hex:
                    print(f"  Hex: {result['response'].encode('utf-8').hex()}")
            else:
                print(f"✗ {args.port}")
                print(f"  Custom command: {args.custom}")
                print(f"  Error: {result['error']}")
        return 0 if result["success"] else 1

    # Handle standard commands
    if not args.command:
        args.command = "identity"

    result = query_oxford_instrument(args.port, args.type, args.command, args.baudrate, args.timeout)

    if args.json:
        output = {
            "port": args.port,
            "instrument_type": args.type,
            "command": args.command,
            "baudrate": args.baudrate,
            "success": result["success"],
            "response": result["response"],
            "parsed": result["parsed"],
            "description": result.get("description"),
            "response_hex": result["response"].encode("utf-8").hex() if result["response"] else None,
            "error": result["error"],
        }
        print(json.dumps(output, indent=2))
    else:
        if result["success"]:
            print(f"✓ {args.port} — {args.type}")
            print(f"  Command: {args.command}")
            if result.get("description"):
                print(f"  Description: {result['description']}")
            print(f"  Parsed response: {result['parsed']}")
            print(f"  Raw response: {result['response']}")
            if args.hex:
                print(f"  Hex: {result['response'].encode('utf-8').hex()}")
        else:
            print(f"✗ {args.port} — {args.type}")
            print(f"  Command: {args.command}")
            print(f"  Error: {result['error']}")

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
