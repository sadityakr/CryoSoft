"""Shared utilities for GPIB/serial connection diagnostics.

Provides non-blocking, non-locking functions to enumerate and query VISA and serial devices
without acquiring exclusive locks (unlike NIMAX). Each query opens a resource briefly,
reads identity, and releases immediately.

Includes Oxford Instruments-specific command handling for IPS, ITC, and Mercury power supplies.
"""

import logging
from typing import Optional, Dict
import json

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# Oxford Instruments command sets (from protocol research)
OXFORD_COMMANDS = {
    "ITC503": {
        "description": "Oxford Instruments ITC 503 (Temperature Controller)",
        "interface": ["GPIB (addr 24)", "RS-232 ISOBUS (addr 1, 9600 baud)"],
        "commands": {
            "identity": {
                "command": "V",
                "terminator": "\r",
                "response_format": "VITC503 X.XX",
                "description": "Firmware version",
            },
            "status": {
                "command": "X",
                "terminator": "\r",
                "response_format": "XnAnCnSnnHnLn",
                "description": "Full status byte",
            },
            "setpoint": {
                "command": "R0",
                "terminator": "\r",
                "response_format": "nnn.nnn (Kelvin)",
                "description": "Temperature setpoint",
            },
            "sensor1": {
                "command": "R1",
                "terminator": "\r",
                "response_format": "nnn.nnn (Kelvin)",
                "description": "Sensor 1 temperature reading",
            },
            "sensor2": {
                "command": "R2",
                "terminator": "\r",
                "response_format": "nnn.nnn (Kelvin)",
                "description": "Sensor 2 temperature reading",
            },
            "sensor3": {
                "command": "R3",
                "terminator": "\r",
                "response_format": "nnn.nnn (Kelvin)",
                "description": "Sensor 3 temperature reading",
            },
            "temp_error": {
                "command": "R4",
                "terminator": "\r",
                "response_format": "nnn.nnn (Kelvin)",
                "description": "Temperature error",
            },
            "heater_percent": {
                "command": "R5",
                "terminator": "\r",
                "response_format": "nn.n (%)",
                "description": "Heater output percentage",
            },
        },
        "timeout_ms": 3000,
        "isobus_prefix": "@1",  # For RS-232 only
    },
    "IPS120": {
        "description": "Oxford Instruments IPS 120-10 (Magnet Power Supply)",
        "interface": ["GPIB (addr 25)", "RS-232 (9600 baud)", "TCP-IP (port 7020)"],
        "protocol": "SCPI",
        "commands": {
            "identity": {
                "command": "*IDN?",
                "terminator": "\n",
                "response_format": "IDN:OXFORD INSTRUMENTS:IPS 120-10:SN:FW",
                "description": "Device identification",
            },
            "current": {
                "command": "READ:DEV:GRPZ:PSU:SIG:CURR?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Magnet current (actual)",
            },
            "current_setpoint": {
                "command": "READ:DEV:GRPZ:PSU:SIG:CSET?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Magnet current setpoint",
            },
            "status": {
                "command": "READ:DEV:GRPZ:PSU:ACTN?",
                "terminator": "\n",
                "response_format": "HOLD|RTOS|RTOZ|CLMP",
                "description": "PSU action state",
            },
            "persistent_current": {
                "command": "READ:DEV:GRPZ:PSU:SIG:PCUR?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Persistent (coil) current",
            },
            "switch_heater": {
                "command": "READ:DEV:GRPZ:PSU:SIG:SWHT?",
                "terminator": "\n",
                "response_format": "ON|OFF",
                "description": "Switch heater state",
            },
            "voltage": {
                "command": "READ:DEV:GRPZ:PSU:SIG:VOLT?",
                "terminator": "\n",
                "response_format": "n.nnn (Volts)",
                "description": "PSU output voltage",
            },
            "firmware": {
                "command": "READ:DEV:GRPZ:PSU:FVER?",
                "terminator": "\n",
                "response_format": "X.XX",
                "description": "Firmware version",
            },
            "serial": {
                "command": "READ:DEV:GRPZ:PSU:SERL?",
                "terminator": "\n",
                "response_format": "xxxxxxxx",
                "description": "Serial number",
            },
            "current_limit": {
                "command": "READ:DEV:GRPZ:PSU:CLIM?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Current limit setting",
            },
        },
        "timeout_ms": 10000,
    },
    "MERCURY_PSU": {
        "description": "Oxford Instruments Mercury iPS-M (Magnet Power Supply)",
        "interface": ["RS-232 (9600 baud)", "TCP-IP (port 7020)"],
        "protocol": "SCPI",
        "commands": {
            "identity": {
                "command": "*IDN?",
                "terminator": "\n",
                "response_format": "IDN:OXFORD INSTRUMENTS:MERCURY iPS:SN:FW",
                "description": "Device identification",
            },
            "current": {
                "command": "READ:DEV:GRPZ:PSU:SIG:CURR?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Magnet current (actual)",
            },
            "current_setpoint": {
                "command": "READ:DEV:GRPZ:PSU:SIG:CSET?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Magnet current setpoint",
            },
            "status": {
                "command": "READ:DEV:GRPZ:PSU:ACTN?",
                "terminator": "\n",
                "response_format": "HOLD|RTOS|RTOZ|CLMP",
                "description": "PSU action state",
            },
            "persistent_current": {
                "command": "READ:DEV:GRPZ:PSU:SIG:PCUR?",
                "terminator": "\n",
                "response_format": "nnn.nnn (Amperes)",
                "description": "Persistent (coil) current",
            },
            "switch_heater": {
                "command": "READ:DEV:GRPZ:PSU:SIG:SWHT?",
                "terminator": "\n",
                "response_format": "ON|OFF",
                "description": "Switch heater state",
            },
            "voltage": {
                "command": "READ:DEV:GRPZ:PSU:SIG:VOLT?",
                "terminator": "\n",
                "response_format": "n.nnn (Volts)",
                "description": "PSU output voltage",
            },
            "firmware": {
                "command": "READ:DEV:GRPZ:PSU:FVER?",
                "terminator": "\n",
                "response_format": "X.XX",
                "description": "Firmware version",
            },
            "serial": {
                "command": "READ:DEV:GRPZ:PSU:SERL?",
                "terminator": "\n",
                "response_format": "xxxxxxxx",
                "description": "Serial number",
            },
        },
        "timeout_ms": 10000,
    },
}


def try_visa_query(resource_string: str, query: str = "*IDN?", timeout_ms: int = 1000) -> Optional[str]:
    """Query a VISA resource without acquiring an exclusive lock.

    Opens the resource with a short timeout, sends a read-only query, and releases immediately.
    This prevents the device-locking behavior of NIMAX.

    Args:
        resource_string: VISA resource string (e.g., "gpib0::5::INSTR" or "COM3::INSTR").
        query: Query command (default "*IDN?" for device identity).
        timeout_ms: Timeout in milliseconds.

    Returns:
        Response string from the device, or None if no response or error.
    """
    try:
        import pyvisa

        rm = pyvisa.ResourceManager()
        try:
            instr = rm.open_resource(resource_string, open_timeout=timeout_ms)
            instr.timeout = timeout_ms
            response = instr.query(query).strip()
            instr.close()
            return response
        except pyvisa.VisaIOError as e:
            logger.debug(f"VISA error on {resource_string}: {e}")
            return None
        finally:
            rm.close()
    except ImportError:
        logger.error("PyVISA not installed; install with: pip install pyvisa pyvisa-py")
        return None
    except Exception as e:
        logger.debug(f"Unexpected error querying {resource_string}: {e}")
        return None


def enumerate_gpib_devices(board: int = 0, timeout_ms: int = 500, verbose: bool = False) -> dict:
    """Scan a GPIB board for connected devices without locking.

    Probes addresses 0–30 with a short timeout. Skips addresses that are currently in use
    by another process.

    Args:
        board: GPIB board number (default 0).
        timeout_ms: Timeout per address in milliseconds.
        verbose: Print progress to stdout.

    Returns:
        Dict mapping address -> {'present': bool, 'identity': Optional[str], 'error': Optional[str]}.
    """
    devices = {}
    for addr in range(31):
        resource_string = f"gpib{board}::{addr}::INSTR"
        if verbose:
            print(f"  Probing {resource_string}...", end=" ", flush=True)

        response = try_visa_query(resource_string, "*IDN?", timeout_ms)
        if response:
            devices[addr] = {"present": True, "identity": response, "error": None}
            if verbose:
                print(f"✓ {response[:50]}")
        else:
            devices[addr] = {"present": False, "identity": None, "error": "No response or timeout"}
            if verbose:
                print("✗")

    return devices


def enumerate_serial_ports(test_query: bool = True, timeout_ms: int = 500) -> list:
    """Enumerate serial ports and optionally test connectivity.

    Uses pyserial's comports() to list ports without opening them. If test_query is True,
    attempts a brief *IDN? read on each port to check if a device is responding.

    Args:
        test_query: If True, attempt to query each port for device identity.
        timeout_ms: Timeout for optional query in milliseconds.

    Returns:
        List of dicts: {'port': str, 'available': bool, 'device': Optional[str], 'error': Optional[str]}.
    """
    try:
        from serial.tools.list_ports import comports
    except ImportError:
        logger.error("pyserial not installed; install with: pip install pyserial")
        return []

    ports = []
    for port_info in comports():
        port_name = port_info.device
        port_data = {
            "port": port_name,
            "description": port_info.description,
            "available": True,
            "device": None,
            "error": None,
        }

        if test_query:
            # Try a brief query; if it times out or errors, mark as unavailable
            try:
                import serial

                ser = serial.Serial(port_name, timeout=timeout_ms / 1000.0)
                ser.write(b"*IDN?\n")
                response = ser.read(256).decode("utf-8", errors="ignore").strip()
                ser.close()
                if response:
                    port_data["device"] = response[:80]
                else:
                    port_data["available"] = False
                    port_data["error"] = "No response to *IDN?"
            except Exception as e:
                port_data["available"] = False
                port_data["error"] = str(e)

        ports.append(port_data)

    return ports


def query_oxford_instrument(
    port: str, instrument_type: str, command: str = "identity", baudrate: int = 9600, timeout_ms: int = None
) -> dict:
    """Query an Oxford Instruments device with type-specific commands.

    Oxford instruments (IPS120, ITC503, Mercury iPS-M) use different command sets and
    terminators. This function sends the appropriate command for the instrument type
    and parses the response.

    Args:
        port: Serial port name (e.g., "COM3").
        instrument_type: Type of Oxford instrument ("IPS120", "ITC503", "MERCURY_PSU").
        command: Command name to send (e.g., "identity", "current", "status").
        baudrate: Baud rate (default 9600).
        timeout_ms: Read timeout in milliseconds (defaults to instrument-specific timeout).

    Returns:
        Dict with 'success', 'instrument_type', 'response', 'parsed', and 'error' keys.
    """
    if instrument_type not in OXFORD_COMMANDS:
        return {
            "success": False,
            "instrument_type": instrument_type,
            "response": None,
            "parsed": None,
            "error": f"Unknown instrument type. Choose from: {list(OXFORD_COMMANDS.keys())}",
        }

    try:
        import serial

        cmd_set = OXFORD_COMMANDS[instrument_type]

        # Use instrument-specific timeout if not overridden
        if timeout_ms is None:
            timeout_ms = cmd_set.get("timeout_ms", 1000)

        # Get the specific command definition
        if command not in cmd_set.get("commands", {}):
            return {
                "success": False,
                "instrument_type": instrument_type,
                "response": None,
                "parsed": None,
                "error": f"Unknown command '{command}'. Available: {list(cmd_set.get('commands', {}).keys())}",
            }

        cmd_def = cmd_set["commands"][command]
        ser = serial.Serial(port, baudrate=baudrate, timeout=timeout_ms / 1000.0)

        # Build command with terminator
        full_command = cmd_def["command"] + cmd_def["terminator"]
        ser.write(full_command.encode("utf-8"))

        # Read response
        response = ser.read(256).decode("utf-8", errors="ignore").strip()
        ser.close()

        if response:
            parsed = parse_oxford_response(response, instrument_type, command)
            return {
                "success": True,
                "instrument_type": instrument_type,
                "command": command,
                "response": response,
                "parsed": parsed,
                "description": cmd_def["description"],
                "error": None,
            }
        else:
            return {
                "success": False,
                "instrument_type": instrument_type,
                "response": None,
                "parsed": None,
                "error": "No response from device",
            }
    except Exception as e:
        return {
            "success": False,
            "instrument_type": instrument_type,
            "response": None,
            "parsed": None,
            "error": str(e),
        }


def parse_oxford_response(response: str, instrument_type: str, command: str) -> str:
    """Parse Oxford instrument response and extract readable info.

    Args:
        response: Raw response from device.
        instrument_type: Type of instrument ("IPS120", "ITC503", "MERCURY_PSU").
        command: Command that was sent.

    Returns:
        Cleaned/parsed response string.
    """
    # Remove common prefixes and artifacts
    cleaned = response.strip().rstrip("\r\n")

    # ITC503 responses are typically echoed and may have extra formatting
    if instrument_type == "ITC503":
        # Remove ISOBUS echo (@1 prefix) if present
        if cleaned.startswith("@1"):
            cleaned = cleaned[2:].strip()
        return cleaned

    # SCPI responses (IPS120, MERCURY_PSU) may have STAT: prefix
    elif instrument_type in ("IPS120", "MERCURY_PSU"):
        # Remove STAT: prefix if present
        if cleaned.startswith("STAT:"):
            cleaned = cleaned[5:].strip()
        return cleaned

    return cleaned


def query_serial_device(port: str, query: str, baudrate: int = 9600, timeout_ms: int = 500) -> dict:
    """Query a serial device without acquiring an exclusive lock.

    Args:
        port: Serial port name (e.g., "COM3").
        query: Query string to send (e.g., "*IDN?").
        baudrate: Baud rate (default 9600).
        timeout_ms: Read timeout in milliseconds.

    Returns:
        Dict with 'success', 'response', and 'error' keys.
    """
    try:
        import serial

        ser = serial.Serial(port, baudrate=baudrate, timeout=timeout_ms / 1000.0)
        ser.write((query + "\n").encode("utf-8"))
        response = ser.read(256).decode("utf-8", errors="ignore").strip()
        ser.close()

        if response:
            return {"success": True, "response": response, "error": None}
        else:
            return {"success": False, "response": None, "error": "No response from device"}
    except Exception as e:
        return {"success": False, "response": None, "error": str(e)}


def format_human_readable(devices_or_ports, resource_type: str = "GPIB") -> str:
    """Format device/port enumeration results as human-readable text.

    Args:
        devices_or_ports: Dict (GPIB) or list (serial).
        resource_type: "GPIB" or "Serial" for formatting.

    Returns:
        Formatted string.
    """
    if resource_type == "GPIB":
        lines = []
        present_count = sum(1 for d in devices_or_ports.values() if d["present"])
        lines.append(f"\nGPIB scan: {present_count} device(s) found\n")
        for addr, info in devices_or_ports.items():
            if info["present"]:
                lines.append(f"  [{addr:2d}] {info['identity']}")
            else:
                lines.append(f"  [{addr:2d}] (no response)")
        return "\n".join(lines)
    else:  # Serial
        lines = [f"\nSerial ports: {len(devices_or_ports)} port(s) found\n"]
        for port_info in devices_or_ports:
            status = "✓" if port_info["available"] else "✗"
            lines.append(f"  {status} {port_info['port']:8s} - {port_info.get('description', 'N/A')}")
            if port_info["device"]:
                lines.append(f"       Device: {port_info['device']}")
            if port_info["error"]:
                lines.append(f"       Error: {port_info['error']}")
        return "\n".join(lines)
