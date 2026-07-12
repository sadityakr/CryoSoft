# ---
# description: |
#   Qt-free diagnostic engine for the troubleshoot toolbox: VISA bus scanning,
#   config preflight checks with a machine-readable fault taxonomy (FaultCode),
#   and a driver bench (introspect / call / raw query) used by the troubleshoot
#   CLI and by agents debugging a setup.
# entry_point: Not run directly; used by the cryosoft.troubleshoot CLI.
# dependencies:
#   - ruamel.yaml >= 0.18 (via cryosoft.core.station helpers)
#   - pyvisa >= 1.13 (needed only for real-bus scans; tests inject a fake)
# input: |
#   Config directories (devices.yaml), VISA addresses, driver aliases. A
#   pyvisa ResourceManager (or a test fake) can be injected everywhere.
# process: |
#   scan_bus() lists VISA resources. probe_address() opens a bare resource and
#   sends an identify query. check_config() validates a config, constructs each
#   declared driver, calls get_idn(), and classifies every failure with a
#   FaultCode. DriverBench wraps one driver for introspection and calls, with
#   read-only methods separated from writing methods.
# output: |
#   ProbeResult / MethodInfo dataclasses (JSON-ready via as_dict()) and log
#   records. The engine never writes files and every operation terminates on
#   its own (bounded by VISA timeouts).
# ---

"""Diagnostic engine: bus scan, config preflight, and driver bench.

Design constraints (these are load-bearing for agent use):

* **Always terminates.** No operation waits for user input; every hardware
  interaction is bounded by a VISA timeout.
* **Machine-readable outcomes.** Every probe returns a ``ProbeResult`` whose
  ``code`` is one of the stable ``FaultCode`` values. The triage skill maps
  codes to physical-world causes, so codes are API: never rename one.
* **Read/write separation.** ``DriverBench.call`` refuses methods that change
  instrument state unless ``allow_write=True``; the CLI exposes the two paths
  as separate subcommands so the permission harness can gate them differently.
"""

from __future__ import annotations

import inspect
import logging
import typing
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from cryosoft.core.exceptions import CryoSoftCommunicationError, CryoSoftConfigError
# _import_class / validate_config_dir are the Station factory's own config
# helpers; the conformance tests already reuse them the same way.
from cryosoft.core.station import _import_class, validate_config_dir

logger = logging.getLogger(__name__)

# Raw identify probes use a short timeout: a silent instrument should cost
# 2 s, not the drivers' default 5 s, when sweeping a whole bus.
PROBE_TIMEOUT_MS = 2000

# Method-name prefixes treated as read-only (safe to call without unlock).
READ_ONLY_PREFIXES = ("get_", "read_", "is_", "ping")


class FaultCode(str, Enum):
    """Stable, machine-readable outcome codes for every probe.

    ``str, Enum`` makes each member compare and serialize as its plain string
    value, so ``as_dict()`` output is directly JSON-ready.

    The triage decision tree maps each code to likely physical causes:

    * ``OK`` — instrument present, responding, identity as expected.
    * ``CONFIG_INVALID`` — devices.yaml broken (missing file, bad YAML,
      unimportable class). Software-side fault, fix the config.
    * ``ADDRESS_NOT_ON_BUS`` — the VISA bus does not list this address.
      Points to power, cabling, or the instrument's address switch.
    * ``OPEN_FAILED`` — address exists but the resource would not open.
      Points to a port held by another process or a wrong resource type.
    * ``NO_RESPONSE`` — opened, but the identify query timed out.
      Points to baud/termination mismatch, wrong protocol, or a hung unit.
    * ``WRONG_IDN`` — responded, but the identity does not match the
      config's ``expect_idn``. Points to swapped cables/addresses.
    * ``GARBLED_RESPONSE`` — responded with something empty/unusable.
      Points to termination characters, baud rate, or a driver parsing bug.
    * ``DRIVER_ERROR`` — the driver raised a non-communication Python
      error. Software-side fault in the driver code itself.
    """

    OK = "OK"
    CONFIG_INVALID = "CONFIG_INVALID"
    ADDRESS_NOT_ON_BUS = "ADDRESS_NOT_ON_BUS"
    OPEN_FAILED = "OPEN_FAILED"
    NO_RESPONSE = "NO_RESPONSE"
    WRONG_IDN = "WRONG_IDN"
    GARBLED_RESPONSE = "GARBLED_RESPONSE"
    DRIVER_ERROR = "DRIVER_ERROR"


# @dataclass auto-generates __init__/__repr__/__eq__ from the field list —
# these are pure data records, so that is exactly what we want.
@dataclass
class ProbeResult:
    """Outcome of probing one instrument (or one config driver entry)."""

    alias: str                  # driver name from devices.yaml ("" for raw probes)
    address: str                # VISA resource string
    driver_class: str           # dotted class path ("" for raw probes)
    code: FaultCode
    idn: str = ""               # identify response, if any
    detail: str = ""            # human-readable specifics (exception text, hints)

    @property
    def ok(self) -> bool:
        """True if the probe fully passed."""
        return self.code is FaultCode.OK

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready plain dict (FaultCode becomes its string)."""
        data = asdict(self)
        data["code"] = self.code.value
        return data


@dataclass
class MethodInfo:
    """One public driver method, as seen by the bench."""

    name: str
    signature: str              # e.g. "(range_v: float) -> None"
    doc: str                    # first line of the docstring
    read_only: bool
    params: dict[str, str] = field(default_factory=dict)  # arg name -> type name

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready plain dict."""
        return asdict(self)


# ── Bus scanning ──────────────────────────────────────────────────────────────


def open_resource_manager() -> Any:
    """Return a real pyvisa ResourceManager.

    Kept as a tiny factory so callers (CLI) have one place that touches the
    real VISA backend, and everything else accepts an injected manager.

    Raises:
        CryoSoftCommunicationError: If pyvisa or its backend is unavailable.
    """
    try:
        import pyvisa

        return pyvisa.ResourceManager()
    except Exception as exc:  # noqa: BLE001 — backend load can fail many ways
        raise CryoSoftCommunicationError(
            f"No VISA backend available ({exc}). Install NI-VISA or pyvisa-py.",
            vi_name="troubleshoot",
        ) from exc


def scan_bus(rm: Any) -> list[str]:
    """List all VISA resource addresses the backend can see.

    Serial (ASRL) ports are listed whether or not an instrument is attached,
    so presence in this list is necessary but not sufficient — only a
    successful probe proves an instrument is there.

    Args:
        rm: A pyvisa ResourceManager (or test fake with ``list_resources()``).

    Returns:
        Sorted list of resource strings, e.g. ``["ASRL10::INSTR", ...]``.
    """
    resources = sorted(str(r) for r in rm.list_resources())
    logger.info("Bus scan found %d resources: %s", len(resources), resources)
    return resources


def probe_address(rm: Any, address: str, idn_command: str = "*IDN?") -> ProbeResult:
    """Open a bare VISA resource and send one identify query.

    This is the no-driver probe used on unknown or unconfigured addresses
    (driver development starts here). It opens the resource with a short
    timeout, queries once, and closes.

    Args:
        rm: A pyvisa ResourceManager (or test fake with ``open_resource()``).
        address: VISA resource string to probe.
        idn_command: Identify query to send; ``*IDN?`` for SCPI instruments,
            ``V`` for the pre-SCPI Oxford ISOBUS family.

    Returns:
        A ProbeResult with code OK / OPEN_FAILED / NO_RESPONSE /
        GARBLED_RESPONSE.
    """
    try:
        instr = rm.open_resource(address)
    except Exception as exc:  # noqa: BLE001 — any backend error means "won't open"
        return ProbeResult(
            alias="", address=address, driver_class="",
            code=FaultCode.OPEN_FAILED, detail=str(exc),
        )
    try:
        instr.timeout = PROBE_TIMEOUT_MS
        response = str(instr.query(idn_command)).strip()
    except Exception as exc:  # noqa: BLE001 — timeout/IO errors vary by backend
        return ProbeResult(
            alias="", address=address, driver_class="",
            code=FaultCode.NO_RESPONSE,
            detail=f"no reply to {idn_command!r}: {exc}",
        )
    finally:
        try:
            instr.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    if not response:
        return ProbeResult(
            alias="", address=address, driver_class="",
            code=FaultCode.GARBLED_RESPONSE,
            detail=f"empty reply to {idn_command!r}",
        )
    return ProbeResult(
        alias="", address=address, driver_class="",
        code=FaultCode.OK, idn=response,
    )


# ── Config preflight ──────────────────────────────────────────────────────────


def _load_real_drivers(config_path: str) -> dict[str, dict]:
    """Return the ``real_drivers`` mapping from a config's devices.yaml.

    Args:
        config_path: Config directory containing devices.yaml.

    Returns:
        ``{alias: {"class": ..., "address": ..., "expect_idn": ...?}}``.

    Raises:
        CryoSoftConfigError: If the file is missing or not valid YAML.
    """
    from ruamel.yaml import YAML

    devices_file = Path(config_path) / "devices.yaml"
    if not devices_file.exists():
        raise CryoSoftConfigError(f"devices.yaml not found in {config_path}")
    try:
        with devices_file.open("r", encoding="utf-8") as f:
            devices = dict(YAML().load(f) or {})
    except Exception as exc:  # noqa: BLE001 — any parse failure is a config error
        raise CryoSoftConfigError(f"devices.yaml is not valid YAML: {exc}") from exc
    return dict(devices.get("real_drivers") or {})


def _is_sim_class(dotted_path: str) -> bool:
    """True if the dotted class path names a simulated driver module (sim_*)."""
    module_path = dotted_path.rsplit(".", 1)[0]
    return module_path.rsplit(".", 1)[-1].startswith("sim_")


def check_config(config_path: str, rm: Any = None) -> list[ProbeResult]:
    """Preflight every driver declared in a config directory.

    For each ``real_drivers`` entry: verify the address is on the bus,
    construct the driver class against it, call ``get_idn()``, and (if the
    entry declares ``expect_idn``) check the response contains that substring
    (case-insensitive). Every failure is classified with a FaultCode.

    Simulated drivers (``sim_*`` modules) skip the bus-presence check but are
    still constructed and identified, so the sim config preflights green.

    Args:
        config_path: Config directory (devices.yaml + monitor.yaml).
        rm: Optional ResourceManager for the bus-presence check. If None,
            presence is not checked and classification relies on open/probe
            behaviour alone.

    Returns:
        One ProbeResult per configured driver, in config order. If the config
        itself is invalid, a single CONFIG_INVALID result is returned.
    """
    errors = validate_config_dir(config_path)
    if errors:
        return [
            ProbeResult(
                alias="", address="", driver_class="",
                code=FaultCode.CONFIG_INVALID, detail="; ".join(errors),
            )
        ]

    drivers = _load_real_drivers(config_path)
    bus: list[str] | None = None
    if rm is not None:
        try:
            bus = [r.upper() for r in scan_bus(rm)]
        except Exception as exc:  # noqa: BLE001 — a dead backend must not kill preflight
            logger.warning("Bus scan unavailable: %s", exc)

    results: list[ProbeResult] = []
    for alias, cfg in drivers.items():
        results.append(_check_one_driver(alias, cfg, bus))
    return results


def _check_one_driver(alias: str, cfg: dict, bus: list[str] | None) -> ProbeResult:
    """Probe one configured driver entry and classify the outcome."""
    class_path = str(cfg["class"])
    address = str(cfg.get("address", ""))
    expect_idn = cfg.get("expect_idn")
    is_sim = _is_sim_class(class_path)

    # Bus presence: only meaningful for real drivers with a scanned bus.
    listed: bool | None = None
    if bus is not None and not is_sim:
        listed = address.upper() in bus

    def _result(code: FaultCode, idn: str = "", detail: str = "") -> ProbeResult:
        return ProbeResult(
            alias=alias, address=address, driver_class=class_path,
            code=code, idn=idn, detail=detail,
        )

    # 1. Construct the driver (this opens the VISA resource for real drivers).
    try:
        driver = _import_class(class_path)(address)
    except CryoSoftConfigError as exc:
        return _result(FaultCode.CONFIG_INVALID, detail=str(exc))
    except CryoSoftCommunicationError as exc:
        if listed is False:
            return _result(
                FaultCode.ADDRESS_NOT_ON_BUS,
                detail=f"address absent from bus scan; open also failed: {exc}",
            )
        return _result(FaultCode.OPEN_FAILED, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — anything else is a driver bug
        return _result(
            FaultCode.DRIVER_ERROR,
            detail=f"constructor raised {type(exc).__name__}: {exc}",
        )

    # 2. Identify.
    try:
        idn = str(driver.get_idn()).strip()
    except CryoSoftCommunicationError as exc:
        if listed is False:
            return _result(
                FaultCode.ADDRESS_NOT_ON_BUS,
                detail=f"address absent from bus scan; no identify reply: {exc}",
            )
        return _result(FaultCode.NO_RESPONSE, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — non-communication error = driver bug
        return _result(
            FaultCode.DRIVER_ERROR,
            detail=f"get_idn() raised {type(exc).__name__}: {exc}",
        )
    finally:
        _close_driver(driver)

    if not idn:
        return _result(FaultCode.GARBLED_RESPONSE, detail="get_idn() returned empty")

    # 3. Identity match (optional, substring, case-insensitive).
    if expect_idn and str(expect_idn).lower() not in idn.lower():
        return _result(
            FaultCode.WRONG_IDN, idn=idn,
            detail=f"expected substring {str(expect_idn)!r} not in identity",
        )

    detail = ""
    if listed is False:
        # Everything works but the scan missed it (some adapters do not
        # enumerate) — worth surfacing, not worth failing.
        detail = "responds correctly but was not listed by the bus scan"
    return _result(FaultCode.OK, idn=idn, detail=detail)


def _close_driver(driver: Any) -> None:
    """Best-effort release of a driver's VISA session.

    Drivers have no uniform close() in their contract (the app holds them for
    its whole lifetime), so the bench closes the underlying pyvisa handle if
    the conventional ``_instr`` attribute exists.
    """
    instr = getattr(driver, "_instr", None)
    if instr is not None:
        try:
            instr.close()
        except Exception:  # noqa: BLE001 — cleanup must never raise
            pass


# ── Driver bench ──────────────────────────────────────────────────────────────


def is_read_only(method_name: str) -> bool:
    """True if a method name is classified as read-only (safe to call)."""
    return method_name.startswith(READ_ONLY_PREFIXES)


class DriverBench:
    """Wraps one driver instance for introspection, calls, and raw I/O.

    The bench is the primitive behind the CLI's ``read`` / ``write`` /
    ``query`` / ``send`` subcommands. It enforces the read/write split:
    ``call()`` refuses non-read-only methods unless ``allow_write=True``.
    """

    def __init__(self, driver: Any, alias: str, address: str, class_path: str) -> None:
        """Wrap an already-constructed driver.

        Args:
            driver: The driver instance.
            alias: Config alias (or a made-up name for ad-hoc benches).
            address: VISA resource string the driver was built with.
            class_path: Dotted class path, for reporting.
        """
        self._driver = driver
        self.alias = alias
        self.address = address
        self.class_path = class_path

    @classmethod
    def from_config(cls, config_path: str, alias: str) -> DriverBench:
        """Construct the bench for one driver declared in a config.

        Args:
            config_path: Config directory containing devices.yaml.
            alias: Driver name under ``real_drivers``.

        Returns:
            A DriverBench wrapping a freshly constructed driver.

        Raises:
            CryoSoftConfigError: If the alias is not in the config.
            CryoSoftCommunicationError: If the driver cannot open its resource.
        """
        drivers = _load_real_drivers(config_path)
        if alias not in drivers:
            raise CryoSoftConfigError(
                f"No driver '{alias}' in {config_path} "
                f"(available: {sorted(drivers)})"
            )
        cfg = drivers[alias]
        class_path = str(cfg["class"])
        address = str(cfg.get("address", ""))
        driver = _import_class(class_path)(address)
        logger.info("Bench opened '%s' (%s at %s)", alias, class_path, address)
        return cls(driver, alias, address, class_path)

    @classmethod
    def from_class(cls, class_path: str, address: str) -> DriverBench:
        """Construct the bench for an arbitrary driver class and address.

        This is the driver-development entry point: bench a driver that has
        no config entry yet.

        Args:
            class_path: Dotted path to the driver class.
            address: VISA resource string.

        Returns:
            A DriverBench wrapping a freshly constructed driver.
        """
        driver = _import_class(class_path)(address)
        return cls(driver, alias=class_path.rsplit(".", 1)[-1],
                   address=address, class_path=class_path)

    # -- Introspection ------------------------------------------------------

    def list_methods(self) -> list[MethodInfo]:
        """Return every public method with signature, doc, and read/write class."""
        infos: list[MethodInfo] = []
        for name, func in inspect.getmembers(type(self._driver), inspect.isfunction):
            if name.startswith("_"):
                continue
            sig = inspect.signature(func)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            public_sig = sig.replace(parameters=params)
            doc = (inspect.getdoc(func) or "").split("\n", 1)[0]
            hints = _safe_type_hints(func)
            infos.append(
                MethodInfo(
                    name=name,
                    signature=str(public_sig),
                    doc=doc,
                    read_only=is_read_only(name),
                    params={
                        p.name: getattr(hints.get(p.name), "__name__", "str")
                        for p in params
                    },
                )
            )
        return infos

    # -- Calling driver methods ----------------------------------------------

    def call(self, method_name: str, args: list[str] | None = None,
             allow_write: bool = False) -> Any:
        """Call one driver method with string arguments coerced via type hints.

        Args:
            method_name: Public method name (e.g. ``get_voltage``).
            args: Positional arguments as strings (CLI form); each is coerced
                to the annotated type (float/int/bool/str).
            allow_write: Must be True to call a method that is not classified
                read-only. The CLI's ``write`` subcommand sets this; ``read``
                never does.

        Returns:
            Whatever the driver method returns.

        Raises:
            ValueError: Unknown/private method, write without allow_write, or
                unparseable arguments.
        """
        if method_name.startswith("_"):
            raise ValueError(f"Private method {method_name!r} is not callable")
        method = getattr(self._driver, method_name, None)
        if not callable(method):
            available = [m.name for m in self.list_methods()]
            raise ValueError(
                f"{self.alias} has no method {method_name!r} "
                f"(available: {available})"
            )
        if not is_read_only(method_name) and not allow_write:
            raise ValueError(
                f"{method_name!r} changes instrument state — use the write "
                f"path (allow_write=True) to call it deliberately"
            )
        coerced = self._coerce_args(method, args or [])
        logger.info("Bench call: %s.%s(%s)", self.alias, method_name, coerced)
        result = method(*coerced)
        logger.info("Bench call result: %r", result)
        return result

    def _coerce_args(self, method: Any, args: list[str]) -> list[Any]:
        """Convert CLI string arguments to the method's annotated types."""
        func = getattr(method, "__func__", method)
        sig = inspect.signature(func)
        params = [p for p in sig.parameters.values() if p.name != "self"]
        if len(args) > len(params):
            raise ValueError(
                f"Too many arguments: {func.__name__} takes at most "
                f"{len(params)} ({[p.name for p in params]}), got {len(args)}"
            )
        hints = _safe_type_hints(func)
        coerced: list[Any] = []
        for raw, param in zip(args, params):
            target = hints.get(param.name, str)
            try:
                coerced.append(_coerce_one(raw, target))
            except ValueError as exc:
                raise ValueError(
                    f"Argument {param.name}={raw!r}: {exc}"
                ) from exc
        return coerced

    # -- Raw VISA I/O ---------------------------------------------------------

    def query(self, command: str) -> str:
        """Send a raw command and return the instrument's reply.

        Uses the driver's underlying pyvisa handle (the conventional
        ``_instr`` attribute). Raw I/O is for commands the driver does not
        wrap yet — the response bypasses all driver parsing.

        Args:
            command: Raw command string, e.g. ``"*IDN?"``.

        Returns:
            The stripped response string.

        Raises:
            ValueError: If the driver exposes no raw VISA handle (sim drivers
                and pymeasure-wrapped drivers do not).
        """
        instr = self._raw_handle()
        logger.info("Bench raw query: %s <- %r", self.alias, command)
        response = str(instr.query(command)).strip()
        logger.info("Bench raw reply: %r", response)
        return response

    def send(self, command: str) -> None:
        """Send a raw command with no reply expected (a bare VISA write).

        Args:
            command: Raw command string, e.g. ``":SENS:VOLT:RANG 0.1"``.

        Raises:
            ValueError: If the driver exposes no raw VISA handle.
        """
        instr = self._raw_handle()
        logger.info("Bench raw send: %s <- %r", self.alias, command)
        instr.write(command)

    def _raw_handle(self) -> Any:
        instr = getattr(self._driver, "_instr", None)
        if instr is None:
            raise ValueError(
                f"{self.alias} ({self.class_path}) exposes no raw VISA handle — "
                f"simulated and pymeasure-wrapped drivers support driver-method "
                f"calls only"
            )
        return instr

    def close(self) -> None:
        """Release the driver's VISA session (best-effort)."""
        _close_driver(self._driver)


# ── Small helpers ─────────────────────────────────────────────────────────────


def _safe_type_hints(func: Any) -> dict[str, Any]:
    """typing.get_type_hints that returns {} instead of raising."""
    try:
        return typing.get_type_hints(func)
    except Exception:  # noqa: BLE001 — unresolvable hints just disable coercion
        return {}


def _coerce_one(raw: str, target: Any) -> Any:
    """Coerce one CLI string to the annotated target type."""
    if target is bool:
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "on", "yes"):
            return True
        if lowered in ("false", "0", "off", "no"):
            return False
        raise ValueError("expected a boolean (true/false/1/0/on/off)")
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    return raw
