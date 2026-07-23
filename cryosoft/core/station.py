# ---
# description: |
#   Station class: the runtime registry of all Virtual Instruments. Provides
#   get_state() with stale-value caching on communication failures,
#   process_system_targets() / check_ramps() / stop_ramps() used by the
#   Orchestrator, and check_safety(state) which aggregates each VI's
#   evaluate_safety() verdict from an existing snapshot (no extra poll).
#   send_measurement_commands() also enforces the capability-scope standard:
#   an "operation"-scope @control method is refused (CryoSoftSafetyError,
#   nothing dispatched) unless the caller passes allowed_scope="operation".
#   build_station() is the factory that constructs the full instrument stack
#   from a YAML config directory; it builds DEGRADED on connection failures —
#   unreachable instruments land in the offline registry (OfflineInstrument,
#   offline_vi_names(), get_offline_info()) instead of aborting the build, and
#   retry_instrument() can bring one back later from the retained build
#   recipe. magnet_vi_names() mirrors switch_vi_names()
#   for registry-system VIs whose class vi_type == "magnet". read_cryogenics_
#   config()/read_servicing_logs_config()/read_operations_config() mirror
#   read_instrument_metadata()'s GUI-safe YAML-only pattern for the optional
#   cryogenics:/servicing_logs:/operations: config blocks
#   (docs/plans/cryogenics-logbook.md §9). Runtime fault registry (plan
#   operation-concurrency-and-error-scoping.md §3): get_state() also
#   populates a structured FaultRecord per stale/disconnected VI
#   (vi_faults(), acknowledge_fault(), clear_fault() — auto-cleared on the
#   next successful poll), and retry_fault() resets the error counter and
#   forces one fresh poll (never rebuilds drivers — that is
#   retry_instrument()'s job, for a VI that never connected at all).
#   safety_flag_sources() mirrors check_safety() but names the VI(s) that
#   tripped each flag, for EMERGENCY reasons/ErrorEvents.
# entry_point: Not run directly; used by Orchestrator and GUI.
# dependencies:
#   - cryosoft.core.exceptions
#   - cryosoft.core.plan (Command, Target — the typed dispatch currency)
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
# last_updated: 2026-07-21
# ---

"""Station class — runtime registry of all Virtual Instruments.

The Station is Layer 2. It sits between the VI layer (L1) and the Orchestrator (L3).
It knows about all VIs, polls their state, and dispatches ramp commands.

Do NOT import from Orchestrator, Procedures, or GUI here.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cryosoft.core.exceptions import (
    CryoSoftCommunicationError,
    CryoSoftConfigError,
    CryoSoftSafetyError,
)
from cryosoft.core.plan import Command, Target
from cryosoft.virtual_instruments.base import BaseVirtualInstrument
from cryosoft.virtual_instruments.rampable import RampableVI

logger = logging.getLogger(__name__)


# `@dataclass(frozen=True)` generates an immutable value class: __init__,
# __eq__ and __repr__ come for free, and instances cannot be mutated after
# creation — updates go through `dataclasses.replace()`, which returns a new
# instance. Immutability keeps the offline registry safe to hand out to the
# GUI without defensive copies.
@dataclass(frozen=True)
class OfflineInstrument:
    """Record of a configured VI that could not be brought up at build time.

    Produced by ``build_station()`` when a driver (or the VI itself) fails to
    connect. The Station keeps these in a registry parallel to the live VIs so
    upper layers can show *what* is missing and *why*, and offer a reconnect.

    Attributes:
        vi_name: The VI's configured name (e.g. ``"magnet_z"``).
        vi_type: The registry vi_type from config (``"system"``,
            ``"measurement"``, ``"switch"``, ``"level"``).
        reason: Human-readable connection-failure description, suitable for
            direct display in the GUI.
        failed_drivers: Config aliases of the drivers that failed to
            construct. Empty when the drivers were fine but the VI's own
            construction raised a communication error.
    """

    vi_name: str
    vi_type: str
    reason: str
    failed_drivers: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class FaultRecord:
    """Record of a RUNTIME fault on a VI that DID connect (plan §3).

    The runtime sibling of :class:`OfflineInstrument`: an offline instrument
    never connected at build time; a ``FaultRecord`` describes a VI that was
    live and has since gone stale or disconnected (comm-error streak) during
    normal polling. Populated by ``get_state()`` at the same point it already
    computes ``_stale``/``_disconnected``, so no extra poll is introduced.

    Attributes:
        vi_name: The VI's registered name.
        kind: ``"stale"`` (communication errors, below the disconnect
            threshold) or ``"disconnected"`` (``max_errors`` consecutive
            failures).
        message: Human-readable description of the latest failure.
        since: Unix time this fault was first recorded. Preserved across a
            ``"stale"`` -> ``"disconnected"`` escalation of the SAME ongoing
            incident (the record is updated in place, not replaced).
        acknowledged: Whether the operator has acknowledged this fault via
            ``acknowledge_fault()``. Deliberately does NOT survive recovery:
            once the VI polls successfully again the record is removed
            entirely (see ``clear_fault()``), acknowledged or not — a
            recovered VI has nothing left to acknowledge.
    """

    vi_name: str
    kind: str
    message: str
    since: float
    acknowledged: bool = False


class Station:
    """Runtime registry and coordinator of all Virtual Instruments.

    Provides:
    - VI registration and attribute-style access (``station.magnet_z``).
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
        self._scanner_enabled: bool = False
        # Degraded-build support: VIs whose hardware failed to connect at
        # build time, plus the build recipes and live driver instances that
        # retry_instrument() needs to bring one back without a restart.
        self._offline_vis: dict[str, OfflineInstrument] = {}
        self._driver_specs: dict[str, dict] = {}   # {alias: driver config}
        self._vi_specs: dict[str, dict] = {}       # {vi_name: vi config}
        self._drivers: dict[str, Any] = {}         # {alias: live driver}
        # Runtime fault registry (plan §3) — a VI that DID connect but has
        # since gone stale/disconnected during polling. Distinct from
        # _offline_vis (never connected at build time).
        self._vi_faults: dict[str, FaultRecord] = {}

    # ------------------------------------------------------------------
    # VI registration and access
    # ------------------------------------------------------------------

    def register_vi(self, vi_name: str, vi: BaseVirtualInstrument, vi_type: str) -> None:
        """Register a Virtual Instrument with this Station.

        Args:
            vi_name: Unique name for this VI (e.g. ``"magnet_z"``).
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

    def get_vi(self, vi_name: str) -> BaseVirtualInstrument:
        """Return the registered VI instance by name.

        The named lookup counterpart to attribute access (``station.magnet_z``):
        used when the name is only known at runtime, e.g. a procedure resolving
        the measurement VI the user selected in the GUI.

        Args:
            vi_name: Name of the registered VI.

        Returns:
            The VI instance.

        Raises:
            KeyError: If no VI with that name is registered.
        """
        return self._virtual_instruments[vi_name]

    def measurement_vi_names(self) -> list[str]:
        """Return the names of all registered measurement VIs, in registration order.

        A measurement VI is one registered with ``vi_type == "measurement"``.
        The order is the order the VIs were registered (config order), so a GUI
        or procedure that defaults to "the first measurement VI" gets a stable,
        config-controlled choice.

        Returns:
            List of measurement VI names, registration order preserved.
        """
        return [
            name
            for name, vi_type in self._vi_registry.items()
            if vi_type == "measurement"
        ]

    def switch_vi_names(self) -> list[str]:
        """Return the names of all registered switch VIs, in registration order.

        A switch VI is one registered with ``vi_type == "switch"`` (a
        matrix-switch / scanner that multiplexes measurement channels by route).
        The order is config order, so a procedure that defaults to "the first
        switch VI" gets a stable, config-controlled choice — mirroring
        ``measurement_vi_names()``.

        Returns:
            List of switch VI names, registration order preserved.
        """
        return [
            name
            for name, vi_type in self._vi_registry.items()
            if vi_type == "switch"
        ]

    def magnet_vi_names(self) -> list[str]:
        """Return the names of all registered magnet VIs, in registration order.

        A magnet VI is a registry-``system`` VI whose class ``vi_type ==
        "magnet"`` (the typed VI category from ``MagnetBase`` and its
        subclasses — distinct from the registry's own "system" role string,
        see GLOSSARY.md's "vi_type (class)" / "vi_type (config/registry)"
        entries). The order is config order, so a caller that defaults to
        "every magnet" (e.g. the helium-fill operation forcing all magnets to
        zero field, plan §8.1) gets a stable, config-controlled list —
        mirrors ``switch_vi_names()``.

        Returns:
            List of magnet VI names, registration order preserved.
        """
        return [
            name
            for name, vi_type in self._vi_registry.items()
            if vi_type == "system"
            and getattr(self._virtual_instruments[name], "vi_type", "") == "magnet"
        ]

    def has_vi(self, vi_name: str) -> bool:
        """Return True if a VI with this name is registered."""
        return vi_name in self._virtual_instruments

    # ------------------------------------------------------------------
    # Offline instruments (degraded build) and reconnection
    # ------------------------------------------------------------------

    def register_offline_vi(self, info: OfflineInstrument) -> None:
        """Record a configured VI that failed to connect at build time.

        An offline VI is *not* in the live registry: ``get_vi_names()`` and
        the typed enumerators never return it, so the Orchestrator, procedures
        and safety evaluation transparently see a smaller station.

        Args:
            info: The offline record (name, type, human-readable reason).
        """
        self._offline_vis[info.vi_name] = info
        logger.warning(
            "VI '%s' registered OFFLINE (type=%s): %s",
            info.vi_name,
            info.vi_type,
            info.reason,
        )

    def offline_vi_names(self) -> list[str]:
        """Return the names of all offline VIs, in config order."""
        return list(self._offline_vis.keys())

    def get_offline_info(self, vi_name: str) -> OfflineInstrument:
        """Return the offline record for a VI.

        Args:
            vi_name: Name of the offline VI.

        Returns:
            The :class:`OfflineInstrument` record.

        Raises:
            KeyError: If no offline VI with that name exists.
        """
        return self._offline_vis[vi_name]

    def retry_instrument(self, vi_name: str) -> tuple[bool, str]:
        """Try to bring an offline VI online: rebuild its drivers, then the VI.

        Re-runs the same construction ``build_station()`` performed, from the
        retained build recipe: each of the VI's drivers that is not already
        live is constructed (its ``__init__`` opens the hardware connection),
        then the VI itself. On success the VI joins the live registry exactly
        as if it had connected at startup; on failure the offline record's
        ``reason`` is refreshed with the latest error.

        A driver brought up here is shared: another offline VI referencing the
        same alias will find it already live on its own retry.

        Args:
            vi_name: Name of the offline VI to reconnect.

        Returns:
            An explicit ``(ok, message)`` verdict for the GUI, mirroring the
            control-validation standard: ``message`` is the human-readable
            success confirmation or failure reason.
        """
        info = self._offline_vis.get(vi_name)
        if info is None:
            return False, f"'{vi_name}' is not offline"
        spec = self._vi_specs.get(vi_name)
        if spec is None:
            return False, f"No build recipe retained for '{vi_name}'"

        role_aliases = dict(spec.get("drivers") or {})
        # dict.fromkeys: order-preserving de-dup of the alias list.
        for alias in dict.fromkeys(role_aliases.values()):
            if alias in self._drivers:
                continue
            driver_cfg = self._driver_specs.get(alias, {})
            try:
                cls = _import_class(driver_cfg["class"])
                self._drivers[alias] = cls(driver_cfg.get("address", "SIM"))
            except Exception as exc:  # noqa: BLE001 — verdict, never a crash, in GUI context
                reason = f"driver '{alias}': {exc}"
                still_failed = tuple(
                    a for a in dict.fromkeys(role_aliases.values())
                    if a not in self._drivers
                )
                self._offline_vis[vi_name] = replace(
                    info, reason=reason, failed_drivers=still_failed
                )
                logger.warning("Reconnect of '%s' failed: %s", vi_name, reason)
                return False, reason

        driver_refs = {role: self._drivers[alias] for role, alias in role_aliases.items()}
        init_params = dict(spec.get("init_params", {}) or {})
        try:
            cls = _import_class(spec["class"])
            vi = cls(driver_refs, **init_params)
        except Exception as exc:  # noqa: BLE001 — verdict, never a crash, in GUI context
            self._offline_vis[vi_name] = replace(
                info, reason=str(exc), failed_drivers=()
            )
            logger.warning(
                "Reconnect of '%s' failed in VI construction: %s", vi_name, exc
            )
            return False, str(exc)

        del self._offline_vis[vi_name]
        self.register_vi(vi_name, vi, spec.get("vi_type", "system"))
        logger.info("Instrument '%s' reconnected", vi_name)
        return True, f"'{vi_name}' reconnected"

    def set_scanner_enabled(self, enabled: bool) -> None:
        """Toggle whether scanner-sensitive procedures may use the switch VI.

        A plain availability bit: it does not touch the switch VI itself.
        When disabled, a scanner-sensitive procedure behaves as if no switch
        VI exists (see ``SweepMeasureProcedure``'s route discovery).

        Args:
            enabled: True to make the scanner available to procedures.
        """
        self._scanner_enabled = bool(enabled)

    def scanner_enabled(self) -> bool:
        """Return whether scanner-sensitive procedures may use the switch VI."""
        return self._scanner_enabled

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

    def persistent_mode_magnets(self) -> list[str]:
        """Return the names of magnet VIs currently in manual persistent mode.

        Persistent mode means the user is driving that magnet's switch heater
        and PSU by hand, so the Orchestrator refuses to start a procedure while
        any magnet is in it. VIs without the ``persistent_mode_enabled``
        accessor (every non-persistent VI) are skipped.
        """
        names: list[str] = []
        for vi_name, vi in self._virtual_instruments.items():
            checker = getattr(vi, "persistent_mode_enabled", None)
            try:
                if callable(checker) and checker():
                    names.append(vi_name)
            except Exception:  # noqa: BLE001 — a flaky VI must not block the check
                continue
        return names

    def system_setpoint_meta(self, vi_name: str) -> tuple[str, str]:
        """Return ``(label, unit)`` describing a VI's ramp setpoint.

        Reads the VI class's declarative ``setpoint_label`` / ``setpoint_unit``
        (declared once per instrument category), falling back to the VI name and
        no unit. Lets the Orchestrator render human status lines like
        "Ramping field to -1 T" without reaching into VI internals.

        Args:
            vi_name: Name of the registered VI.

        Returns:
            ``(label, unit)``; ``(vi_name, "")`` if the VI is unknown or
            declares no setpoint metadata.
        """
        vi = self._virtual_instruments.get(vi_name)
        label = getattr(vi, "setpoint_label", "") or vi_name
        unit = getattr(vi, "setpoint_unit", "") or ""
        return label, unit

    def measurement_label(self, vi_name: str) -> str:
        """Return a human label for a measurement VI (e.g. "DC resistance").

        Falls back to the VI name if the VI is unknown or declares no
        ``display_label``.
        """
        vi = self._virtual_instruments.get(vi_name)
        return getattr(vi, "display_label", "") or vi_name

    def measurement_selector_label(self, vi_name: str) -> str:
        """Return the SHORT method-selection label for a measurement VI.

        Used for the GUI method-selection drop-down, where a terse name keeps
        the column narrow. Falls back to ``display_label`` (the longer
        status-line label) and then the VI name when ``selector_label`` is empty
        or the VI is unknown. See ``MeasurementInstrumentBase.selector_label``.

        Args:
            vi_name: The measurement VI's registered name.

        Returns:
            The VI's ``selector_label`` if set, else its ``display_label``, else
            ``vi_name``.
        """
        vi = self._virtual_instruments.get(vi_name)
        return (
            getattr(vi, "selector_label", "")
            or getattr(vi, "display_label", "")
            or vi_name
        )

    def __getattr__(self, name: str) -> BaseVirtualInstrument:
        """Attribute-style access to VIs: ``station.magnet_z``.

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
                # A successful poll fully clears any standing fault — see
                # clear_fault()'s docstring: an acknowledged fault does not
                # survive recovery either, it simply disappears.
                self.clear_fault(vi_name)
            except CryoSoftCommunicationError as exc:
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
                    self._record_fault(vi_name, "disconnected", str(exc))
                else:
                    logger.warning(
                        "VI '%s' communication error (attempt %d/%d)",
                        vi_name,
                        self._error_counts[vi_name],
                        self._max_errors,
                    )
                    self._record_fault(vi_name, "stale", str(exc))
                full_state[vi_name] = stale

        return full_state

    # ------------------------------------------------------------------
    # Runtime fault registry (plan §3) — the RUNTIME sibling of the
    # offline-instrument registry above: an offline instrument never
    # connected at build time, a fault record describes a VI that DID
    # connect and has since gone stale/disconnected during polling.
    # ------------------------------------------------------------------

    def _record_fault(self, vi_name: str, kind: str, message: str) -> None:
        """Record (or update in place) a runtime fault for *vi_name*.

        An existing unresolved fault has its ``kind``/``message`` updated
        (e.g. ``"stale"`` escalating to ``"disconnected"`` as the same
        ongoing incident) while ``since`` and ``acknowledged`` are
        preserved — escalating severity does not erase an operator's
        earlier acknowledgment of the same incident.

        Args:
            vi_name: The VI the fault concerns.
            kind: ``"stale"`` or ``"disconnected"``.
            message: Human-readable description of the latest failure.
        """
        existing = self._vi_faults.get(vi_name)
        since = existing.since if existing is not None else time.time()
        acknowledged = existing.acknowledged if existing is not None else False
        self._vi_faults[vi_name] = FaultRecord(vi_name, kind, message, since, acknowledged)

    def vi_faults(self) -> dict[str, FaultRecord]:
        """Return the current runtime fault registry, ``{vi_name: FaultRecord}``."""
        return dict(self._vi_faults)

    def acknowledge_fault(self, vi_name: str) -> bool:
        """Mark a VI's active fault as acknowledged (calms the operator UI).

        Args:
            vi_name: Name of the faulted VI.

        Returns:
            True if a fault record existed and was acknowledged; False if
            the VI has no active fault.
        """
        existing = self._vi_faults.get(vi_name)
        if existing is None:
            return False
        self._vi_faults[vi_name] = replace(existing, acknowledged=True)
        return True

    def clear_fault(self, vi_name: str) -> None:
        """Remove *vi_name*'s fault record, if any (called on a successful poll).

        An acknowledged-but-recovered fault simply disappears — there is
        nothing left to acknowledge once the instrument is responding
        again, so recovery is not distinguished from "never acknowledged".

        Args:
            vi_name: Name of the VI to clear.
        """
        self._vi_faults.pop(vi_name, None)

    def retry_fault(self, vi_name: str) -> tuple[bool, str]:
        """Reset *vi_name*'s error counter and force one fresh poll (plan §3).

        The runtime counterpart of ``retry_instrument()``: it does NOT
        rebuild any driver (the VI is already live) — it only resets the
        comm-error streak and re-polls once, exactly what a stale/
        disconnected but otherwise-live instrument needs to recover.

        Args:
            vi_name: Name of the (registered, live) VI to retry.

        Returns:
            An explicit ``(ok, message)`` verdict, mirroring
            ``retry_instrument()``'s style: ``message`` is a human-readable
            success confirmation or failure reason.
        """
        vi = self._virtual_instruments.get(vi_name)
        if vi is None:
            return False, f"'{vi_name}' is not a registered VI"
        self._error_counts[vi_name] = 0
        try:
            state = vi.get_state()
        except CryoSoftCommunicationError as exc:
            self._error_counts[vi_name] = 1
            message = str(exc)
            self._record_fault(vi_name, "stale", message)
            logger.warning("Retry of '%s' failed: %s", vi_name, message)
            return False, f"'{vi_name}' still not responding: {message}"
        self._error_counts[vi_name] = 0
        self._last_known_state[vi_name] = state
        self.clear_fault(vi_name)
        logger.info("Retry of '%s' succeeded — fault cleared", vi_name)
        return True, f"'{vi_name}' responded — fault cleared"

    # ------------------------------------------------------------------
    # Ramp management
    # ------------------------------------------------------------------

    def process_system_targets(self, system_targets: dict[str, Target]) -> None:
        """Dispatch ramp targets to system VIs.

        Only VIs whose ``vi_type == "system"`` are valid ramp targets. Each
        value is a ``Target``; its ``rate`` and ``persistent`` attributes are
        forwarded to ``start_ramp()`` only when not ``None``, so VIs that do
        not accept them (most VIs do not) are unaffected.

        Args:
            system_targets: Mapping of VI name → ``Target``.

        Raises:
            ValueError: If a named VI is not registered or not a system VI.
        """
        for vi_name, tgt in system_targets.items():
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
            target = tgt.target
            kwargs: dict[str, Any] = {}
            if tgt.rate is not None:
                kwargs["rate"] = tgt.rate
            if tgt.persistent is not None:
                kwargs["persistent"] = bool(tgt.persistent)
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
                    "phase": vi.ramp_phase(),
                }
            except CryoSoftCommunicationError:
                result[vi_name] = {
                    "value": None,
                    "target": None,
                    "rate": None,
                    "ramp_status": "IDLE",
                    "phase": None,
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

    def safety_flag_sources(self, state: dict[str, dict] | None = None) -> dict[str, list[str]]:
        """Map each tripped safety flag to the VI name(s) that tripped it.

        A parallel accessor to ``check_safety()`` (same OR-combination logic,
        same disconnected-level-meter special case) that additionally names
        the originating instrument(s) — used by the Orchestrator so an
        EMERGENCY reason and its ``ErrorEvent`` can name the instrument
        (plan §3), without changing ``check_safety()``'s existing
        ``{flag: bool}`` signature (other callers are unaffected).

        Args:
            state: Snapshot from ``get_state()``. ``None`` uses the last
                known state (no hardware poll).

        Returns:
            ``{flag_name: [vi_name, ...]}`` — VI names in registration
            order, each listed at most once per flag. A flag absent from
            the mapping was never tripped by any VI.
        """
        if state is None:
            state = self._last_known_state
        sources: dict[str, list[str]] = {}
        for vi_name, vi in self._virtual_instruments.items():
            vi_state = state.get(vi_name, {})
            if vi_state.get("_disconnected") and getattr(vi, "vi_type", "") == "level":
                sources.setdefault("helium_low", [])
                if vi_name not in sources["helium_low"]:
                    sources["helium_low"].append(vi_name)
            try:
                for flag, tripped in vi.evaluate_safety(vi_state).items():
                    if not tripped:
                        continue
                    names = sources.setdefault(flag, [])
                    if vi_name not in names:
                        names.append(vi_name)
            except Exception:
                logger.exception("evaluate_safety failed on VI '%s'", vi_name)
        return sources

    # ------------------------------------------------------------------
    # Measurement command dispatch
    # ------------------------------------------------------------------

    def send_measurement_commands(
        self, commands: Sequence[Command], *, allowed_scope: str = "measurement"
    ) -> None:
        """Dispatch an ordered sequence of ``Command`` calls to VIs.

        Commands are dispatched in order (order is semantically meaningful —
        e.g. a switch heater must settle before a source arms). An unknown VI
        or an unknown method is logged at WARNING and skipped; an exception
        raised by the VI method itself propagates to the caller.

        Capability-scope enforcement (the standard in GLOSSARY.md's
        "Capability scope" entry): every command's target method is resolved
        and its ``@control`` scope checked BEFORE any command in the batch is
        dispatched. A method requiring ``"operation"`` scope when
        *allowed_scope* is ``"measurement"`` rejects the whole batch — the
        plan is refused before any hardware is touched, exactly like an
        envelope violation. An undecorated method (no ``@control``, e.g. a
        measurement VI's ``initiate``/``standby`` lifecycle) defaults to
        ``"measurement"`` scope.

        Args:
            commands: Ordered sequence of ``Command`` objects to dispatch.
            allowed_scope: The submitting plan's capability scope —
                ``"measurement"`` (default, procedures) or ``"operation"``
                (operations; operation-scope plans may also carry
                measurement-scope commands).

        Raises:
            CryoSoftSafetyError: If any command's target method requires a
                capability scope not covered by *allowed_scope*. Nothing is
                dispatched.
        """
        resolved: list[tuple[Command, Any]] = []
        for cmd in commands:
            vi = self._virtual_instruments.get(cmd.vi_name)
            if vi is None:
                logger.warning("send_measurement_commands: unknown VI '%s'", cmd.vi_name)
                resolved.append((cmd, None))
                continue
            method = getattr(vi, cmd.method, None)
            if method is None:
                logger.warning(
                    "send_measurement_commands: VI '%s' has no method '%s'",
                    cmd.vi_name,
                    cmd.method,
                )
                resolved.append((cmd, None))
                continue
            required_scope = getattr(method, "_control_scope", "measurement")
            if required_scope == "operation" and allowed_scope != "operation":
                raise CryoSoftSafetyError(
                    f"send_measurement_commands: '{cmd.vi_name}.{cmd.method}' "
                    f"requires operation-scope access, but this plan is "
                    f"{allowed_scope}-scope. Command refused before dispatch."
                )
            resolved.append((cmd, method))

        # Validated as a whole batch above — now dispatch, in order.
        for cmd, method in resolved:
            if method is None:
                continue
            logger.debug("Calling %s.%s(%s)", cmd.vi_name, cmd.method, cmd.kwargs)
            method(**cmd.kwargs)

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

    Degraded build: an instrument that fails to *connect* never aborts the
    build. Each driver whose ``__init__`` raises is recorded, and every VI
    that needs it (or whose own construction raises a communication error) is
    registered offline via ``Station.register_offline_vi()`` instead of live —
    the GUI shows why and offers a reconnect. Config errors (missing files,
    unimportable classes, unknown driver references) still raise, because a
    broken config is a software fault the degraded mode cannot reason about.

    Args:
        config_path: Path to the directory containing devices.yaml and monitor.yaml.

    Returns:
        A ``Station`` instance with every connectable VI registered live,
        every unconnectable one registered offline, and error threshold set.

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
    # A driver __init__'s job is to open the hardware connection, so ANY
    # construction failure here is treated as a connection fault: recorded,
    # never raised. Import errors (config faults) still raise via _import_class.
    drivers_map: dict[str, Any] = {}
    offline_drivers: dict[str, str] = {}  # {alias: failure reason}
    for driver_name, driver_cfg in (devices_config.get("real_drivers") or {}).items():
        cls = _import_class(driver_cfg["class"])
        resource = driver_cfg.get("address", "SIM")
        station._driver_specs[driver_name] = dict(driver_cfg)
        try:
            drivers_map[driver_name] = cls(resource)
        except Exception as exc:  # noqa: BLE001 — any driver-construction failure degrades, see above
            offline_drivers[driver_name] = str(exc)
            logger.warning(
                "Driver '%s' (%s) failed to connect: %s",
                driver_name,
                driver_cfg["class"],
                exc,
            )
            continue
        logger.info("Built driver '%s' (%s)", driver_name, driver_cfg["class"])
    station._drivers = drivers_map

    # --- Build all VIs ---
    for vi_name, vi_cfg in (devices_config.get("virtual_instruments") or {}).items():
        cls = _import_class(vi_cfg["class"])
        station._vi_specs[vi_name] = dict(vi_cfg)
        vi_type = vi_cfg.get("vi_type", "system")

        # Resolve driver references. An unknown alias is a config error and
        # raises; an alias whose driver failed to connect sends this VI to
        # the offline registry instead.
        driver_refs: dict[str, Any] = {}
        failed_aliases: list[str] = []
        for role, driver_name in (vi_cfg.get("drivers") or {}).items():
            if driver_name in offline_drivers:
                failed_aliases.append(driver_name)
            elif driver_name not in drivers_map:
                raise CryoSoftConfigError(
                    f"VI '{vi_name}' references unknown driver '{driver_name}'"
                )
            else:
                driver_refs[role] = drivers_map[driver_name]

        if failed_aliases:
            unique = list(dict.fromkeys(failed_aliases))
            reason = "; ".join(
                f"driver '{alias}': {offline_drivers[alias]}" for alias in unique
            )
            station.register_offline_vi(
                OfflineInstrument(vi_name, vi_type, reason, tuple(unique))
            )
            continue

        init_params = dict(vi_cfg.get("init_params", {}) or {})
        try:
            vi = cls(driver_refs, **init_params)
        except CryoSoftCommunicationError as exc:
            # Drivers came up but the VI's own bring-up could not talk to the
            # hardware. Other exceptions (bad init_params, limit-validation
            # errors) are config/software faults and propagate.
            station.register_offline_vi(
                OfflineInstrument(vi_name, vi_type, str(exc), ())
            )
            continue
        station.register_vi(vi_name, vi, vi_type)

    offline = station.offline_vi_names()
    logger.info(
        "Station built with %d VIs (%d offline) from '%s'",
        len(station.get_vi_names()) + len(offline),
        len(offline),
        config_dir,
    )
    return station


def build_station_with_fallback(
    candidate_paths: list[str],
) -> tuple[Station, str, list[str]]:
    """Build a Station from the first usable config, falling back in order.

    Each candidate is validated (``validate_config_dir``) and then built; the
    first that succeeds wins. This is the startup safety net for *config*
    faults: a corrupted config no longer crashes the app, because a later
    candidate (ultimately the always-loadable ``sim_cryostat``) takes over.

    Unreachable instruments never trigger a fallback: ``build_station()``
    degrades them to the offline registry and still succeeds, so the user
    stays on their own setup's config with everything else working.

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


def read_instrument_metadata(config_path: str) -> dict[str, dict[str, str]]:
    """Read each VI's optional descriptive ``metadata:`` block, GUI-safe.

    A setup property, like everything else in ``devices.yaml``: free-text
    identity (manufacturer, model, role, notes — whatever the setup wants to
    record about what a VI physically is) for display and for stamping onto
    every run's metadata. Parses YAML only — never imports a driver/VI class
    or instantiates anything, so it is safe to call from the GUI thread on a
    config that may describe unreachable hardware.

    Args:
        config_path: Path to the config directory containing ``devices.yaml``.

    Returns:
        ``{vi_name: {field: value, ...}}``, string-coerced. Empty for a VI
        with no ``metadata:`` block, and ``{}`` entirely if the config
        directory, file, or YAML is unreadable — never raises.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore
    except ImportError:
        return {}

    devices_file = Path(config_path) / "devices.yaml"
    try:
        with devices_file.open("r", encoding="utf-8") as f:
            devices_config = dict(YAML().load(f) or {})
    except OSError:
        return {}
    except Exception:  # noqa: BLE001 — malformed YAML must not break the GUI
        return {}

    virtual_instruments = devices_config.get("virtual_instruments") or {}
    if not isinstance(virtual_instruments, dict):
        return {}

    result: dict[str, dict[str, str]] = {}
    for vi_name, vi_cfg in virtual_instruments.items():
        if not isinstance(vi_cfg, dict):
            continue
        metadata = vi_cfg.get("metadata")
        if isinstance(metadata, dict) and metadata:
            result[str(vi_name)] = {str(k): str(v) for k, v in metadata.items()}

    return result


# Defaults applied by read_cryogenics_config() for every key the config omits
# (docs/plans/cryogenics-logbook.md §9). ``helium_volume_l`` deliberately has
# no default: its absence means "no L/h display", not "0 L".
_CRYOGENICS_DEFAULTS: dict[str, float | str] = {
    "level_vi": "level_meter",
    "helium_warning_pct": 35.0,
    "fill_target_pct": 90.0,
    "fill_zero_field_eps_T": 0.005,
    "fill_zero_field_window_s": 10.0,
    "fill_complete_window_s": 120.0,
    "max_fill_duration_s": 3600.0,
    "sample_period_s": 10.0,
    "history_sample_s": 3600.0,
}


def _load_devices_yaml(config_path: str) -> dict[str, Any] | None:
    """Parse ``devices.yaml`` under *config_path*, GUI-safe.

    Shared by ``read_cryogenics_config`` / ``read_servicing_logs_config``:
    YAML-parse only, never imports a driver/VI class or instantiates
    anything, so it is safe to call from the GUI thread on a config that may
    describe unreachable hardware.

    Args:
        config_path: Path to the config directory containing ``devices.yaml``.

    Returns:
        The parsed mapping, or ``None`` if ruamel.yaml is unavailable, the
        file is missing/unreadable, or the YAML is malformed.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore
    except ImportError:
        return None

    devices_file = Path(config_path) / "devices.yaml"
    try:
        with devices_file.open("r", encoding="utf-8") as f:
            return dict(YAML().load(f) or {})
    except OSError:
        return None
    except Exception:  # noqa: BLE001 — malformed YAML must not break the GUI
        return None


def _load_monitor_yaml(config_path: str) -> dict[str, Any] | None:
    """Parse ``monitor.yaml`` under *config_path*, GUI-safe.

    Mirrors ``_load_devices_yaml`` for the display-side config file: YAML-parse
    only, never instantiates anything, never raises.

    Args:
        config_path: Path to the config directory containing ``monitor.yaml``.

    Returns:
        The parsed mapping, or ``None`` if ruamel.yaml is unavailable, the
        file is missing/unreadable, or the YAML is malformed.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore
    except ImportError:
        return None

    monitor_file = Path(config_path) / "monitor.yaml"
    try:
        with monitor_file.open("r", encoding="utf-8") as f:
            return dict(YAML().load(f) or {})
    except OSError:
        return None
    except Exception:  # noqa: BLE001 — malformed YAML must not break the GUI
        return None


def read_panels_config(config_path: str) -> dict[str, list[str]]:
    """Read the optional ``panels:`` block of ``monitor.yaml``, GUI-safe.

    Which controls a setup's operators use day-to-day is a display property
    of the setup, so it lives in the config: each entry allowlists the
    controls shown on that VI's compact monitor card, overriding the
    ``panel=`` defaults the VI's ``@control`` declarations carry. A VI absent
    from the block keeps its declared defaults; every control, listed or
    not, remains available in the instrument's front panel. Display-only —
    hiding a control never disables it, and safety stays with
    ``control_limits``.

    Expected shape::

        panels:
          temperature_vti:
            controls: [set_temperature]

    Args:
        config_path: Path to the config directory containing ``monitor.yaml``.

    Returns:
        ``{vi_name: [control_method_name, ...]}`` for every well-formed
        entry (a ``controls:`` list of strings). ``{}`` when the block is
        absent or the file/YAML is unreadable — never raises.
    """
    monitor_config = _load_monitor_yaml(config_path)
    if monitor_config is None:
        return {}
    block = monitor_config.get("panels")
    if not isinstance(block, dict):
        return {}
    result: dict[str, list[str]] = {}
    for vi_name, entry in block.items():
        if not isinstance(entry, dict):
            continue
        controls = entry.get("controls")
        if isinstance(controls, list):
            result[str(vi_name)] = [str(name) for name in controls]
    return result


def read_cryogenics_config(config_path: str) -> dict[str, Any]:
    """Read the optional ``cryogenics:`` block, GUI-safe, with defaults applied.

    A setup property like everything else in ``devices.yaml`` (plan §9): the
    fill target, zero-field tolerance, timing, and the level VI the
    cryogenics feature (the helium-fill operation, the consumption display,
    the automatic recorder) is built around. Parses YAML only — never
    imports a driver/VI class or instantiates anything, so it is safe to
    call from the GUI thread on a config that may describe unreachable
    hardware, mirroring ``read_instrument_metadata``'s GUI-safe pattern.

    Args:
        config_path: Path to the config directory containing ``devices.yaml``.

    Returns:
        The ``cryogenics:`` mapping with every omitted key defaulted from
        ``_CRYOGENICS_DEFAULTS``. ``{}`` when the block is absent, malformed,
        or the config directory/file/YAML is unreadable — never raises.
    """
    devices_config = _load_devices_yaml(config_path)
    if devices_config is None:
        return {}
    block = devices_config.get("cryogenics")
    if not isinstance(block, dict) or not block:
        return {}
    merged = dict(_CRYOGENICS_DEFAULTS)
    merged.update(block)
    return merged


def read_servicing_logs_config(config_path: str) -> list[str]:
    """Read the optional ``servicing_logs:`` list, GUI-safe.

    Names which declared servicing-log kinds (``cryosoft.session.
    servicing_log.DECLARED_LOG_KINDS``) this setup keeps (plan §9). Parses
    YAML only, mirroring ``read_cryogenics_config`` — never imports the
    session layer or instantiates anything.

    Args:
        config_path: Path to the config directory containing ``devices.yaml``.

    Returns:
        The declared log-kind keys, string-coerced, in config order. ``[]``
        when the block is absent, malformed, or the config is unreadable —
        never raises.
    """
    devices_config = _load_devices_yaml(config_path)
    if devices_config is None:
        return []
    block = devices_config.get("servicing_logs")
    if not isinstance(block, list):
        return []
    return [str(kind) for kind in block]


# Per-operation-kind defaults for read_operations_config()'s merge (plan §9).
# Only "sample_change" exists today (plan §11 phase 4); a future operation
# kind adds its own entry here. An operation name declared in devices.yaml
# but absent from this dict is passed through unmerged — forward-compatible
# with an operation this function does not yet know defaults for.
_OPERATIONS_DEFAULTS: dict[str, dict[str, float | str]] = {
    "sample_change": {
        "vti_vi": "temperature_vti",
        "target_temperature_K": 300.0,
        "temperature_tolerance_K": 2.0,
        "temperature_window_s": 60.0,
        "zero_field_eps_T": 0.005,
        "zero_field_window_s": 10.0,
        "needle_valve": "manual",
        "postcondition_timeout_s": 7200.0,
        # How often the hold phase (docs/plans/unified-servicing-log-and-
        # run-recording.md §1) records station state into the shared
        # recording, in seconds. Matches HeliumFillOperation's own
        # sample_period_s default.
        "sample_period_s": 10.0,
    },
}


def read_operations_config(config_path: str) -> dict[str, dict[str, Any]]:
    """Read the optional ``operations:`` block, GUI-safe, with defaults applied.

    Unlike ``cryogenics:`` (one flat mapping), ``operations:`` is a mapping
    of *named* operation configs (``sample_change:``, and future kinds) —
    see plan §9. Parses YAML only, mirroring ``read_cryogenics_config`` —
    never imports the operations layer or instantiates anything, so it is
    safe to call from the GUI thread on a config that may describe
    unreachable hardware.

    Args:
        config_path: Path to the config directory containing ``devices.yaml``.

    Returns:
        ``{operation_name: {key: value, ...}}`` for every declared operation
        sub-block, with every omitted key defaulted from
        ``_OPERATIONS_DEFAULTS[operation_name]`` (an operation name with no
        known defaults is passed through unmerged). ``{}`` when the
        ``operations:`` block is absent, malformed, or the config
        directory/file/YAML is unreadable — never raises. A caller
        constructs the concrete operation with
        ``SampleChangeOperation(station, **read_operations_config(path)
        ["sample_change"])``.
    """
    devices_config = _load_devices_yaml(config_path)
    if devices_config is None:
        return {}
    block = devices_config.get("operations")
    if not isinstance(block, dict) or not block:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for op_name, op_cfg in block.items():
        if not isinstance(op_cfg, dict):
            continue
        merged = dict(_OPERATIONS_DEFAULTS.get(op_name, {}))
        merged.update(op_cfg)
        result[str(op_name)] = merged
    return result
