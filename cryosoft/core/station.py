# ---
# description: |
#   Station class: the runtime registry of all Virtual Instruments. Provides
#   get_state() with stale-value caching on communication failures,
#   process_system_targets() / check_ramps() / stop_ramps() used by the
#   Orchestrator, and check_safety(state) which aggregates each VI's
#   evaluate_safety() verdict from an existing snapshot (no extra poll).
#   build_station() is the factory that constructs the full instrument stack
#   from a YAML config directory.
# entry_point: Not run directly; used by Orchestrator and GUI.
# dependencies:
#   - cryosoft.core.exceptions
#   - cryosoft.virtual_instruments.base (BaseVirtualInstrument)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - ruamel.yaml >= 0.18
# input: |
#   build_station() takes a config_path (directory containing devices.yaml and
#   monitor.yaml). Station itself can be constructed directly with register_vi().
# process: |
#   get_state() polls each VI's get_state() method, caching the last known value.
#   On CryoSoftCommunicationError, the stale cache is returned with _stale=True.
#   After max_errors consecutive failures, _disconnected=True is added.
# output: |
#   Full station state dict {vi_name: {field: value, ...}} every poll cycle.
# last_updated: 2026-04-06
# ---

"""Station class — runtime registry of all Virtual Instruments.

The Station is Layer 2. It sits between the VI layer (L1) and the Orchestrator (L3).
It knows about all VIs, polls their state, and dispatches ramp commands.

Do NOT import from Orchestrator, Procedures, or GUI here.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from cryosoft.core.exceptions import CryoSoftCommunicationError, CryoSoftConfigError
from cryosoft.virtual_instruments.base import BaseVirtualInstrument
from cryosoft.virtual_instruments.rampable import RampableVI

logger = logging.getLogger(__name__)


class Station:
    """Runtime registry and coordinator of all Virtual Instruments.

    Provides:
    - VI registration and attribute-style access (``station.magnet_x``).
    - Polled state snapshot with stale-value caching.
    - Ramp dispatch and progress tracking.
    - Safety status aggregation.
    - Measurement command dispatch.
    - Bulk initiate / standby.
    """

    def __init__(self) -> None:
        self._vi_registry: dict[str, str] = {}            # {vi_name: vi_type}
        self._virtual_instruments: dict[str, BaseVirtualInstrument] = {}
        self._last_known_state: dict[str, dict] = {}       # Stale value cache
        self._error_counts: dict[str, int] = {}
        self._max_errors: int = 3

    # ------------------------------------------------------------------
    # VI registration and access
    # ------------------------------------------------------------------

    def register_vi(self, vi_name: str, vi: BaseVirtualInstrument, vi_type: str) -> None:
        """Register a Virtual Instrument with this Station.

        Args:
            vi_name: Unique name for this VI (e.g. ``"magnet_x"``).
            vi: The VI instance.
            vi_type: Category string (``"system"`` or ``"measurement"``).
        """
        vi.vi_name = vi_name
        self._vi_registry[vi_name] = vi_type
        self._virtual_instruments[vi_name] = vi
        self._error_counts[vi_name] = 0
        logger.info("Registered VI '%s' (type=%s)", vi_name, vi_type)

    def get_vi_names(self) -> list[str]:
        """Return a list of all registered VI names."""
        return list(self._virtual_instruments.keys())

    def has_vi(self, vi_name: str) -> bool:
        """Return True if a VI with this name is registered."""
        return vi_name in self._virtual_instruments

    def get_vi_type(self, vi_name: str) -> str:
        """Return the vi_type for the given VI name.

        Args:
            vi_name: Name of the registered VI.

        Returns:
            The vi_type string (e.g. ``"system"`` or ``"measurement"``).

        Raises:
            KeyError: If no VI with that name exists.
        """
        return self._vi_registry[vi_name]

    def __getattr__(self, name: str) -> BaseVirtualInstrument:
        """Attribute-style access to VIs: ``station.magnet_x``.

        Args:
            name: VI name.

        Returns:
            The VI instance.

        Raises:
            AttributeError: If no VI with that name is registered.
        """
        # Guard against infinite recursion during __init__ before
        # _virtual_instruments is set.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            vis = object.__getattribute__(self, "_virtual_instruments")
            if name in vis:
                return vis[name]
        except AttributeError:
            pass
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'. "
            f"No VI named '{name}' is registered."
        )

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------

    @property
    def cached_state(self) -> dict[str, dict]:
        """Return the last known state from the most recent monitor tick.

        No hardware poll. Safe to call from within a procedure's measure().

        Returns:
            ``{vi_name: {field: value, ...}}`` — same structure as get_state().
        """
        return dict(self._last_known_state)

    def last_state_flat(self) -> dict[str, float]:
        """Return cached system VI state as a flat ``{vi_name_field: value}`` dict.

        Reads from the monitor-tick cache — no hardware poll. Only numeric scalar
        values from system VIs are included. Metadata keys (``_stale``,
        ``_disconnected``) and string values (e.g. ``ramp_status``) are excluded.

        Returns:
            Flat dict keyed ``{vi_name}_{monitored_field}`` for all numeric fields.
        """
        result: dict[str, float] = {}
        for vi_name, state in self._last_known_state.items():
            if self._vi_registry.get(vi_name) == "measurement":
                continue
            for key, value in state.items():
                if key.startswith("_"):
                    continue
                if not isinstance(value, (int, float)):
                    continue
                result[f"{vi_name}_{key}"] = float(value)
        return result

    def get_state(self) -> dict[str, dict]:
        """Poll all VIs and return a full state snapshot.

        On ``CryoSoftCommunicationError``: increment the error counter for
        that VI and return the last known values with ``_stale: True``.
        After ``max_errors`` consecutive failures, also add ``_disconnected: True``.

        Returns:
            ``{vi_name: {field: value, ...}}`` — one sub-dict per VI.
        """
        full_state: dict[str, dict] = {}

        for vi_name, vi in self._virtual_instruments.items():
            try:
                state = vi.get_state()
                self._error_counts[vi_name] = 0
                self._last_known_state[vi_name] = state
                full_state[vi_name] = state
            except CryoSoftCommunicationError:
                self._error_counts[vi_name] += 1
                stale = dict(self._last_known_state.get(vi_name, {}))
                stale["_stale"] = True
                if self._error_counts[vi_name] >= self._max_errors:
                    stale["_disconnected"] = True
                    logger.error(
                        "VI '%s' disconnected after %d consecutive errors",
                        vi_name,
                        self._error_counts[vi_name],
                    )
                else:
                    logger.warning(
                        "VI '%s' communication error (attempt %d/%d)",
                        vi_name,
                        self._error_counts[vi_name],
                        self._max_errors,
                    )
                full_state[vi_name] = stale

        return full_state

    # ------------------------------------------------------------------
    # Ramp management
    # ------------------------------------------------------------------

    def process_system_targets(self, system_targets: dict[str, dict]) -> None:
        """Dispatch ramp targets to system VIs.

        Only VIs whose ``vi_type == "system"`` are valid ramp targets.
        Each entry in *system_targets* must be ``{vi_name: {"target": float}}``,
        optionally with a ``"rate"`` and/or ``"persistent"`` key — both are
        forwarded to ``start_ramp()`` only if present, so VIs that do not
        accept them (most VIs do not) are unaffected.

        Args:
            system_targets: Mapping of VI name → target dict.

        Raises:
            ValueError: If a named VI is not registered or not a system VI.
        """
        for vi_name, params in system_targets.items():
            if vi_name not in self._virtual_instruments:
                raise ValueError(f"process_system_targets: unknown VI '{vi_name}'")
            if self._vi_registry[vi_name] != "system":
                raise ValueError(
                    f"process_system_targets: VI '{vi_name}' is not a system VI "
                    f"(type={self._vi_registry[vi_name]})"
                )
            vi = self._virtual_instruments[vi_name]
            if not isinstance(vi, RampableVI):
                raise ValueError(
                    f"process_system_targets: VI '{vi_name}' does not implement RampableVI"
                )
            target = float(params["target"])
            kwargs: dict[str, Any] = {}
            if params.get("rate") is not None:
                kwargs["rate"] = float(params["rate"])
            if "persistent" in params:
                kwargs["persistent"] = bool(params["persistent"])
            logger.info("Starting ramp on '%s' to target=%s", vi_name, target)
            vi.start_ramp(target, **kwargs)  # type: ignore[call-arg]

    def check_ramps(self) -> bool:
        """Advance all active system VI ramps and report completion.

        Calls ``advance_ramp()`` on every system VI that implements ``RampableVI``
        and is currently in ``"RAMPING"`` state.

        Returns:
            ``True`` if all system VIs have reached their targets (or are IDLE).
            ``False`` if any system VI is still ramping.
        """
        all_done = True
        for vi_name, vi_type in self._vi_registry.items():
            if vi_type != "system":
                continue
            vi = self._virtual_instruments[vi_name]
            if not isinstance(vi, RampableVI):
                continue
            status = vi.ramp_status()
            if status == "RAMPING":
                vi.advance_ramp()
                all_done = False
            elif status == "IDLE":
                # No ramp active — this VI is done (or never started).
                pass
            else:
                # TARGET_REACHED
                pass
        return all_done

    def stop_ramps(self, vi_names: set[str] | None = None) -> None:
        """Stop active ramps and hold hardware where it is.

        Calls ``stop_ramp()`` on every system VI implementing ``RampableVI``
        (or only those in *vi_names* if given). Each call is individually
        guarded: a dead instrument must not prevent the others from stopping.

        Args:
            vi_names: Restrict to these VI names; ``None`` means all system VIs.
        """
        for vi_name, vi_type in self._vi_registry.items():
            if vi_type != "system":
                continue
            if vi_names is not None and vi_name not in vi_names:
                continue
            vi = self._virtual_instruments[vi_name]
            if not isinstance(vi, RampableVI):
                continue
            try:
                vi.stop_ramp()
            except Exception:
                logger.exception("stop_ramp failed on VI '%s'", vi_name)

    def get_ramp_status(self) -> dict[str, dict]:
        """Aggregate each system VI's ramp target, rate, and status.

        For operational-status display ("what is the run waiting on, and how far
        from setpoint?"). For every system VI implementing ``RampableVI``,
        collects ``ramp_target()`` / ``ramp_rate()`` (user units — tesla,
        kelvin; ``None`` if the VI does not expose them) and its
        ``ramp_status()`` string. Each VI is guarded individually: a
        communication error on one instrument yields a stale entry rather than
        breaking the whole snapshot.

        Returns:
            ``{vi_name: {"target": float|None, "rate": float|None,
            "ramp_status": str}}`` for every system VI. A VI that raised on read
            also carries ``"_stale": True``.
        """
        result: dict[str, dict] = {}
        for vi_name, vi_type in self._vi_registry.items():
            if vi_type != "system":
                continue
            vi = self._virtual_instruments[vi_name]
            if not isinstance(vi, RampableVI):
                continue
            try:
                result[vi_name] = {
                    "value": vi.ramp_value(),
                    "target": vi.ramp_target(),
                    "rate": vi.ramp_rate(),
                    "ramp_status": vi.ramp_status(),
                }
            except CryoSoftCommunicationError:
                result[vi_name] = {
                    "value": None,
                    "target": None,
                    "rate": None,
                    "ramp_status": "IDLE",
                    "_stale": True,
                }
        return result

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def check_safety(self, state: dict[str, dict] | None = None) -> dict[str, bool]:
        """Aggregate every VI's safety verdict from a state snapshot.

        Each VI judges its own state fragment via ``evaluate_safety()`` (the
        level meter reports its debounced ``helium_low``, magnet VIs report
        ``quench``). No hardware is polled here — pass the snapshot the
        monitor tick already collected, or omit it to use the cached one.

        A disconnected level meter also trips ``helium_low``: if the helium
        level cannot be monitored, it must be assumed unsafe.

        Args:
            state: Snapshot from ``get_state()``. ``None`` uses the last
                known state (no hardware poll).

        Returns:
            ``{flag_name: bool}`` — a flag is True if ANY VI tripped it.
        """
        if state is None:
            state = self._last_known_state
        flags: dict[str, bool] = {}
        for vi_name, vi in self._virtual_instruments.items():
            vi_state = state.get(vi_name, {})
            if vi_state.get("_disconnected") and getattr(vi, "vi_type", "") == "level":
                flags["helium_low"] = True
            try:
                for flag, tripped in vi.evaluate_safety(vi_state).items():
                    flags[flag] = flags.get(flag, False) or bool(tripped)
            except Exception:
                logger.exception("evaluate_safety failed on VI '%s'", vi_name)
        return flags

    # ------------------------------------------------------------------
    # Measurement command dispatch
    # ------------------------------------------------------------------

    def send_measurement_commands(self, measurement_commands: dict[str, dict]) -> None:
        """Dispatch method calls to measurement VIs.

        Args:
            measurement_commands: ``{vi_name: {method_name: kwargs}}``.
        """
        for vi_name, commands in measurement_commands.items():
            if vi_name not in self._virtual_instruments:
                logger.warning("send_measurement_commands: unknown VI '%s'", vi_name)
                continue
            vi = self._virtual_instruments[vi_name]
            for method_name, kwargs in commands.items():
                method = getattr(vi, method_name, None)
                if method is None:
                    logger.warning(
                        "send_measurement_commands: VI '%s' has no method '%s'",
                        vi_name,
                        method_name,
                    )
                    continue
                logger.debug("Calling %s.%s(%s)", vi_name, method_name, kwargs)
                method(**kwargs)

    # ------------------------------------------------------------------
    # VI action dispatch
    # ------------------------------------------------------------------

    def execute_vi_action(self, vi_name: str, method_name: str, **kwargs: Any) -> Any:
        """Call a @control method on a named VI directly.

        Args:
            vi_name: Name of the target VI.
            method_name: Name of the method to call.
            **kwargs: Keyword arguments forwarded to the method.

        Returns:
            Return value of the method (if any).

        Raises:
            AttributeError: If the VI or method does not exist.
        """
        vi = self._virtual_instruments[vi_name]
        method = getattr(vi, method_name)
        return method(**kwargs)

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------

    def initiate_all(self) -> None:
        """Call ``initiate()`` on every registered VI."""
        for vi_name, vi in self._virtual_instruments.items():
            logger.info("Initiating VI '%s'", vi_name)
            try:
                vi.initiate()
            except Exception:
                logger.exception("Error initiating VI '%s'", vi_name)

    def standby_all(self) -> None:
        """Call ``standby()`` on every registered VI."""
        for vi_name, vi in self._virtual_instruments.items():
            logger.info("Putting VI '%s' into standby", vi_name)
            try:
                vi.standby()
            except Exception:
                logger.exception("Error during standby of VI '%s'", vi_name)


# ── Factory ───────────────────────────────────────────────────────────────────


def _import_class(dotted_path: str) -> type:
    """Import and return a class from a dotted module path.

    Args:
        dotted_path: E.g. ``"cryosoft.virtual_instruments.magnet.superconducting_magnet.SuperconductingMagnetVI"``.

    Returns:
        The class object.

    Raises:
        CryoSoftConfigError: If the import fails.
    """
    try:
        module_path, class_name = dotted_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise CryoSoftConfigError(
            f"Cannot import '{dotted_path}': {exc}"
        ) from exc


def build_station(config_path: str) -> Station:
    """Construct a fully populated Station from a YAML config directory.

    Expected directory layout::

        config_path/
          devices.yaml   — driver and VI definitions
          monitor.yaml   — tick interval and error threshold

    Args:
        config_path: Path to the directory containing devices.yaml and monitor.yaml.

    Returns:
        A ``Station`` instance with all VIs registered and error threshold set.

    Raises:
        CryoSoftConfigError: If required config keys are missing or imports fail.
        FileNotFoundError: If the config directory or files are missing.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore
    except ImportError as exc:
        raise CryoSoftConfigError("ruamel.yaml is required but not installed") from exc

    config_dir = Path(config_path)
    devices_file = config_dir / "devices.yaml"
    monitor_file = config_dir / "monitor.yaml"

    if not devices_file.exists():
        raise FileNotFoundError(f"devices.yaml not found in {config_dir}")
    if not monitor_file.exists():
        raise FileNotFoundError(f"monitor.yaml not found in {config_dir}")

    yaml = YAML()

    with devices_file.open("r", encoding="utf-8") as f:
        devices_config: dict = dict(yaml.load(f))

    with monitor_file.open("r", encoding="utf-8") as f:
        monitor_config: dict = dict(yaml.load(f))

    station = Station()

    # Apply monitor config
    mon = monitor_config.get("monitor", {})
    station._max_errors = int(mon.get("max_vi_errors", 3))

    # --- Build all real drivers ---
    drivers_map: dict[str, Any] = {}
    for driver_name, driver_cfg in devices_config.get("real_drivers", {}).items():
        cls = _import_class(driver_cfg["class"])
        resource = driver_cfg.get("address", "SIM")
        drivers_map[driver_name] = cls(resource)
        logger.info("Built driver '%s' (%s)", driver_name, driver_cfg["class"])

    # --- Build all VIs ---
    for vi_name, vi_cfg in devices_config.get("virtual_instruments", {}).items():
        cls = _import_class(vi_cfg["class"])

        # Resolve driver references
        driver_refs: dict[str, Any] = {}
        for role, driver_name in vi_cfg.get("drivers", {}).items():
            if driver_name not in drivers_map:
                raise CryoSoftConfigError(
                    f"VI '{vi_name}' references unknown driver '{driver_name}'"
                )
            driver_refs[role] = drivers_map[driver_name]

        init_params = dict(vi_cfg.get("init_params", {}) or {})
        vi = cls(driver_refs, **init_params)
        vi_type = vi_cfg.get("vi_type", "system")
        station.register_vi(vi_name, vi, vi_type)

    logger.info(
        "Station built with %d VIs from '%s'",
        len(station.get_vi_names()),
        config_dir,
    )
    return station


def build_station_with_fallback(
    candidate_paths: list[str],
) -> tuple[Station, str, list[str]]:
    """Build a Station from the first usable config, falling back in order.

    Each candidate is validated (``validate_config_dir``) and then built; the
    first that succeeds wins. This is the startup safety net: a corrupted or
    hardware-missing active config no longer crashes the app, because a later
    candidate (ultimately the always-loadable ``sim_cryostat``) takes over.

    Args:
        candidate_paths: Config directories to try, most-preferred first.
            Callers should end the list with a guaranteed-safe config.

    Returns:
        A ``(station, used_path, warnings)`` tuple. ``warnings`` describes each
        candidate that was skipped, for surfacing to the user.

    Raises:
        CryoSoftConfigError: If no candidate could be built.
    """
    warnings: list[str] = []
    for path in candidate_paths:
        errors = validate_config_dir(path)
        if errors:
            warnings.append(f"Config '{path}' is invalid ({errors[0]}); skipped.")
            continue
        try:
            station = build_station(path)
            return station, path, warnings
        except Exception as exc:  # noqa: BLE001 — fallback must catch any build failure
            warnings.append(f"Config '{path}' failed to load ({exc}); skipped.")
    raise CryoSoftConfigError(
        f"No usable config among {candidate_paths}: {'; '.join(warnings)}"
    )


def validate_config_dir(config_path: str) -> list[str]:
    """Check a config directory without instantiating any driver or VI.

    A dry-run for the config editor and startup fallback: it parses both YAML
    files and verifies that every declared class is importable and that every
    VI's driver references resolve to a defined driver. It deliberately does
    **not** call any class constructor, so validating a real-hardware config
    never opens a VISA session or touches an instrument.

    Args:
        config_path: Path to the config directory (containing devices.yaml and
            monitor.yaml).

    Returns:
        A list of human-readable error strings. An empty list means the config
        is structurally valid and safe to load.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore
    except ImportError:
        return ["ruamel.yaml is required but not installed"]

    config_dir = Path(config_path)
    devices_file = config_dir / "devices.yaml"
    monitor_file = config_dir / "monitor.yaml"

    errors: list[str] = []
    if not devices_file.exists():
        errors.append(f"devices.yaml not found in {config_dir}")
    if not monitor_file.exists():
        errors.append(f"monitor.yaml not found in {config_dir}")
    if errors:
        return errors

    yaml = YAML()
    try:
        with devices_file.open("r", encoding="utf-8") as f:
            devices_config = dict(yaml.load(f) or {})
    except Exception as exc:  # noqa: BLE001 — any YAML parse failure is a config error
        return [f"devices.yaml is not valid YAML: {exc}"]
    try:
        with monitor_file.open("r", encoding="utf-8") as f:
            yaml.load(f)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"monitor.yaml is not valid YAML: {exc}")

    real_drivers = devices_config.get("real_drivers") or {}
    if not isinstance(real_drivers, dict):
        return errors + ["'real_drivers' must be a mapping"]
    for driver_name, driver_cfg in real_drivers.items():
        if not isinstance(driver_cfg, dict) or "class" not in driver_cfg:
            errors.append(f"driver '{driver_name}' is missing a 'class'")
            continue
        try:
            _import_class(driver_cfg["class"])
        except CryoSoftConfigError as exc:
            errors.append(f"driver '{driver_name}': {exc}")

    virtual_instruments = devices_config.get("virtual_instruments") or {}
    if not isinstance(virtual_instruments, dict):
        return errors + ["'virtual_instruments' must be a mapping"]
    for vi_name, vi_cfg in virtual_instruments.items():
        if not isinstance(vi_cfg, dict) or "class" not in vi_cfg:
            errors.append(f"VI '{vi_name}' is missing a 'class'")
            continue
        try:
            _import_class(vi_cfg["class"])
        except CryoSoftConfigError as exc:
            errors.append(f"VI '{vi_name}': {exc}")
        for role, driver_name in (vi_cfg.get("drivers") or {}).items():
            if driver_name not in real_drivers:
                errors.append(
                    f"VI '{vi_name}' role '{role}' references unknown driver "
                    f"'{driver_name}'"
                )

    return errors
