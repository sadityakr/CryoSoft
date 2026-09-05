"""Composable scenario helpers for driving a simulated station into a target state.

The sim drivers already expose test-control knobs (``_force_helium_level``,
``_simulate_error``, ``_simulate_quench``, ...); every existing test sets
these by hand and hand-writes its own ``qtbot.waitUntil`` convergence check.
This module names those recipes once so a test — or a throwaway exploration
script — can ask for "helium low" or "magnet_z disconnected" and get back a
station already in that state, then compose several in one test to ask "what
happens when X and Y are both true".

Input: an already-built ``station`` (``build_station(config_path)``) and
``orchestrator`` (``Orchestrator(station, ...)``, monitoring started) from
the caller's own fixtures, plus pytest-qt's ``qtbot`` for the convergence
wait. Each scenario function also takes the sim driver knob it drives.
Process: set the relevant driver attribute(s), then ``qtbot.waitUntil`` the
condition actually propagating through a real tick — never assert the flag
was set, since the point is to observe what the Orchestrator's tick loop
does with it, not to shortcut past it.
Output: the scenario functions mutate state and return ``None``; ``snapshot()``
returns a plain-dict view (Orchestrator state, held VIs, hold conditions,
comm faults) cheap to print or assert against.
"""

from __future__ import annotations

from typing import Any

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.plan import PhasePlan, Target
from cryosoft.core.station import Station

_DEFAULT_TIMEOUT_MS = 3000


def snapshot(station: Station, orchestrator: Orchestrator) -> dict[str, Any]:
    """Return a plain-dict view of what the station currently allows/refuses.

    Args:
        station: The station under observation.
        orchestrator: Its Orchestrator.

    Returns:
        ``{"orchestrator_state": str, "held_vis": {vi_name: condition_key},
        "hold_conditions": [...], "faulted_vis": [...]}`` — cheap to print
        in an exploration script or assert against in a test.
    """
    held = orchestrator._held_vis()
    # get_operational_status()'s "conditions" key is populated by the tick
    # pipeline (_update_operational_status) — empty/absent before the first
    # tick, so a snapshot taken immediately after a scenario setup call
    # (before any tick has run) must not crash.
    status = orchestrator.get_operational_status()
    conditions = status.get("conditions", [])
    return {
        "orchestrator_state": orchestrator._state.name,
        "held_vis": {name: cond.key for name, cond in held.items()},
        "hold_conditions": [c for c in conditions if c["severity"] == "hold"],
        "faulted_vis": list(station.vi_faults()),
    }


def apply_helium_low(
    station: Station, *, pct: float = 10.0, level_vi: str = "level_meter"
) -> None:
    """Force the level meter's helium reading low (no wait — see ``helium_low()``).

    Args:
        station: The station; must have ``level_vi`` registered.
        pct: Forced helium level, in percent. Below the config's
            ``helium_low_threshold`` (20.0 in ``sim_cryostat``) to actually
            trip the flag once enough ticks have passed.
        level_vi: Name of the registered level-meter VI.
    """
    station.get_vi(level_vi)._driver._force_helium_level = float(pct)


def helium_low(
    station: Station,
    orchestrator: Orchestrator,
    qtbot: Any,
    *,
    pct: float = 10.0,
    level_vi: str = "level_meter",
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> None:
    """``apply_helium_low()`` then wait for the resulting hold to actually trip.

    Args:
        station: The station; must have ``level_vi`` registered.
        orchestrator: Its Orchestrator, monitoring already started.
        qtbot: pytest-qt's fixture, used to wait for the debounce buffer
            (``CryogenLevelMeterVI``'s majority vote) to converge.
        pct: See ``apply_helium_low()``.
        level_vi: See ``apply_helium_low()``.
        timeout_ms: ``qtbot.waitUntil`` timeout.

    Raises:
        TimeoutError: If no magnet becomes held within ``timeout_ms`` — most
            likely because the station has no magnet (``helium_low`` only
            holds VIs that declare it in ``safety_concerns()``).
    """
    apply_helium_low(station, pct=pct, level_vi=level_vi)

    def _tripped() -> bool:
        return any(
            cond.key == "safety:helium_low" for cond in orchestrator._held_vis().values()
        )

    qtbot.waitUntil(_tripped, timeout=timeout_ms)


def apply_quench(station: Station, *, magnet_vi: str = "magnet_z") -> None:
    """Trigger a simulated quench (no wait — see ``quench()``).

    ``quench`` is a critical-severity flag (station-wide by construction —
    see ``virtual_instruments/README.md``), so this is the "everything
    stops" scenario: EMERGENCY refuses manual control of every VI, held or
    not, once the next tick observes it.

    Args:
        station: The station; must have ``magnet_vi`` registered.
        magnet_vi: Name of the registered magnet VI to quench.
    """
    station.get_vi(magnet_vi)._driver._simulate_quench = True


def quench(
    station: Station,
    orchestrator: Orchestrator,
    qtbot: Any,
    *,
    magnet_vi: str = "magnet_z",
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> None:
    """``apply_quench()`` then wait for the station to enter EMERGENCY.

    Args:
        station: The station; must have ``magnet_vi`` registered.
        orchestrator: Its Orchestrator, monitoring already started.
        qtbot: pytest-qt's fixture.
        magnet_vi: See ``apply_quench()``.
        timeout_ms: ``qtbot.waitUntil`` timeout.
    """
    from cryosoft.core.orchestrator import OrchestratorState

    apply_quench(station, magnet_vi=magnet_vi)
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=timeout_ms
    )


def apply_disconnect(
    station: Station, vi_name: str, *, driver_attr: str = "_driver"
) -> None:
    """Make a VI's driver fail every call (no wait — see ``disconnect()``).

    The same ``_simulate_error`` knob models both "the instrument dropped
    off the bus" and "a measurement instrument returned an error instead of
    data": every sim driver raises ``CryoSoftCommunicationError`` from
    every public method while it is set, regardless of the VI's role
    (system/level/measurement) — there is only one comm-fault primitive at
    the driver layer; what differs is which VI it's applied to.

    Only meaningful for a system/level VI: those are polled every tick
    (``Station.get_state()``) regardless of whether a run is active, so a
    comm error surfaces as a station-wide fault condition on its own. A
    measurement VI is *not* polled at idle — see ``measurement_error()``
    for "a measurement instrument returns an error instead of data".

    Args:
        station: The station; must have ``vi_name`` registered.
        vi_name: Name of the registered VI to fault.
        driver_attr: Name of the driver attribute on the VI — ``"_driver"``
            for a single-driver VI (the standard, e.g. every system/level
            VI).
    """
    getattr(station.get_vi(vi_name), driver_attr)._simulate_error = True


def disconnect(
    station: Station,
    orchestrator: Orchestrator,
    qtbot: Any,
    vi_name: str,
    *,
    driver_attr: str = "_driver",
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> None:
    """``apply_disconnect()`` then wait for the fault to register.

    Args:
        station: The station; must have ``vi_name`` registered.
        orchestrator: Its Orchestrator, monitoring already started.
        qtbot: pytest-qt's fixture.
        vi_name: See ``apply_disconnect()``.
        driver_attr: See ``apply_disconnect()``.
        timeout_ms: ``qtbot.waitUntil`` timeout.
    """
    apply_disconnect(station, vi_name, driver_attr=driver_attr)
    qtbot.waitUntil(lambda: vi_name in station.vi_faults(), timeout=timeout_ms)


def measurement_error(
    station: Station,
    vi_name: str,
    *,
    driver_attr: str = "_meter",
) -> None:
    """Make a measurement VI's instrument fail on its next call.

    Unlike ``disconnect()``, this needs no wait: a measurement VI is only
    touched when a run actively arms/reads it (``initiate_measurement()``/
    ``take_reading()``), so the fault fires synchronously the moment the
    caller (a running procedure's ``sample()``, or a direct call in a test)
    next exercises it — there is no debounce buffer and no station-wide
    fault condition to converge on, only an exception at the call site.

    Args:
        station: The station; must have ``vi_name`` registered.
        vi_name: Name of the registered measurement VI to fault.
        driver_attr: Name of the driver attribute to fault. Defaults to
            ``"_meter"`` (the voltmeter ``take_reading()`` actually calls
            for the standard source+voltmeter pairing, e.g.
            ``dc_measurement``); pass ``"_source"``
            to instead fault current-source setup calls, or ``"_driver"``
            for a single-instrument measurement VI.
    """
    getattr(station.get_vi(vi_name), driver_attr)._simulate_error = True


class _HeldTargetProcedure:
    """Minimal procedure claiming one VI at a nonzero target, for scenario composition.

    Not a real measurement recipe — just enough of the ``BaseProcedure``
    surface (``initiate``/``standby``/``get_progress``) for
    ``Orchestrator.run_procedure()`` to accept it, so a scenario can ask
    "what happens with a procedure running" without pulling in a concrete
    procedure module (procedures may not import from ``tests/``, and this
    keeps the dependency the other way around).
    """

    name = "Scenario Procedure"

    def __init__(self, station: Station, vi_name: str, target: float) -> None:
        self._vi_name = vi_name
        self._target = float(target)

    def initiate(self) -> PhasePlan:
        return PhasePlan(
            targets={self._vi_name: Target(self._target)}, commands=(), wait_s=0.0
        )

    def standby(self) -> PhasePlan:
        return PhasePlan(targets={self._vi_name: Target(0.0)}, commands=(), wait_s=0.0)

    def get_progress(self) -> float:
        return 0.0


def running_procedure(
    station: Station,
    orchestrator: Orchestrator,
    *,
    vi_name: str = "magnet_z",
    target: float = 1.0,
) -> _HeldTargetProcedure:
    """Start a minimal procedure ramping ``vi_name`` toward ``target``.

    Use this to compose "a procedure is running" with another scenario
    (``helium_low``, ``quench``, ``disconnect``): call this first, tick a
    few times so the run is genuinely in flight, then layer the second
    scenario on top and observe (``snapshot()``) whether the run survives,
    fails, or is refused outright.

    Args:
        station: The station; must have ``vi_name`` registered.
        orchestrator: Its Orchestrator; must be IDLE.
        vi_name: The VI the procedure ramps.
        target: The target value ``initiate()`` requests.

    Returns:
        The started ``_HeldTargetProcedure`` instance, in case the caller
        wants to inspect or abort it later.
    """
    procedure = _HeldTargetProcedure(station, vi_name, target)
    orchestrator.run_procedure(procedure)
    return procedure
