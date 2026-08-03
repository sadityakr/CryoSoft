"""HeliumFillOperation — force all magnets to zero field and fill helium."""

from __future__ import annotations

import logging
import time
from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import NextDue, OperationBase, ReadinessCondition
from cryosoft.core.plan import Command, PhasePlan, StepPlan
from cryosoft.core.station import Station

logger = logging.getLogger(__name__)

# Level-meter refresh-rate mode constants, mirroring the three-mode standard
# on CryogenLevelMeterVI (STANDBY=0, SLOW=1, FAST=2). Re-declared here rather
# than imported: an operation may not import virtual_instruments (contract
# C6), so it only ever calls set_refresh_rate(mode=...) through the Station.
_REFRESH_SLOW = 1
_REFRESH_FAST = 2

# A helium-level increase smaller than this (in percent, between consecutive
# samples) is treated as flat/noise rather than "rising" — avoids the
# completion clock resetting on floating-point/sim jitter.
_RISE_NOISE_FLOOR_PCT = 1e-6

# Default advisory helium warning threshold (%), used by next_due() when the
# config omits "helium_warning_pct" — matches read_cryogenics_config()'s own
# default (cryosoft/core/station.py's _CRYOGENICS_DEFAULTS), so a fill built
# directly (not via **read_cryogenics_config(...)) still predicts sensibly.
_DEFAULT_WARNING_PCT = 35.0


def _humanize_duration_hours(hours: float) -> str:
    """Format a positive duration in hours as a compact "X.X d"/"X.X h" string.

    Args:
        hours: Duration in hours; must be positive (callers clamp at 0
            separately — an overdue fill never reaches this helper).

    Returns:
        ``"{hours/24:.1f} d"`` when ``hours >= 24``, else ``"{hours:.1f} h"``.
    """
    if hours >= 24.0:
        return f"{hours / 24.0:.1f} d"
    return f"{hours:.1f} h"


class HeliumFillOperation(OperationBase):
    """Force every magnet to zero field, then fill the helium reservoir.

    The first concrete operation: a servicing action, not a
    measurement. ``initiate()`` commands ``standby()`` on whichever magnets
    (``Station.magnet_vi_names()``) aren't already in standby, leaving one
    that already is untouched, and switches the configured level meter to
    FAST refresh; ``initiation_gates()`` holds the run until every magnet
    reports ``magnet_state() == "standby"`` and that holds; ``sample()``/
    ``step()`` poll the helium
    level once per ``sample_period_s`` until it has settled at/above
    ``fill_target_pct`` (or ``max_fill_duration_s`` elapses); ``standby()``/
    ``abort()`` restore SLOW refresh so an aborted fill never leaves the
    meter in FAST; ``postcondition_gates()`` verifies that restoration and
    that the level actually rose.

    ``tolerated_safety_flags = frozenset({"helium_low"})``: the fill's whole
    purpose is fixing low helium, so that flag must not abort it — a
    non-tolerated flag (e.g. ``quench``) still aborts the fill exactly like
    any other run.

    Readiness / next-due (Operations panel): ``readiness_conditions()``
    exposes one aggregate ``zero_field`` checklist row (empty if the station
    has no magnets); ``next_due()`` predicts when the level will cross the
    configured warning threshold from the measured consumption rate passed
    in via ``context``.

    No HDF5 file:
    ``sample()`` appends to a bounded in-memory level curve instead of
    writing a dataset, and ``run_summary()`` hands that curve to the session
    layer (``CryogenicsRecorder``) when the run ends.
    """

    name = "Helium Fill"
    description = "Force all magnets to zero field and fill the helium reservoir"
    ready_message = "Ready — helium transfer can begin"
    tolerated_safety_flags = frozenset({"helium_low"})

    def __init__(
        self,
        station: Station,
        *,
        person: str = "",
        **config: Any,
    ) -> None:
        """Resolve the magnet list and level VI, and merge the fill config.

        Args:
            station: The active Station; must have the level VI named by
                ``config["level_vi"]`` (default ``"level_meter"``).
            person: Who is performing the fill (recorded via
                ``get_params()``; the servicing-log recorder reads
                ``params["person"]`` from the run manifest).
            **config: Plan §9 ``cryogenics:`` keys — ``level_vi``,
                ``fill_target_pct``, ``fill_zero_field_window_s``,
                ``fill_complete_window_s``,
                ``max_fill_duration_s``, ``sample_period_s``,
                ``helium_warning_pct`` (read by ``next_due()`` for its
                trend-estimate threshold) — each with a sane default so
                this constructs from a
                sim station alone. Unrecognised keys (including the retired
                ``data_directory``, kept accepted-but-ignored for any caller
                still passing it) are silently ignored, so
                ``**read_cryogenics_config(config_path)`` can be passed
                verbatim.

        Raises:
            CryoSoftConfigError: If ``level_vi`` does not name a VI
                registered on this station.
        """
        super().__init__()
        self._station = station
        self._person = str(person)

        self._level_vi_name: str = str(config.get("level_vi", "level_meter"))
        self._fill_target_pct: float = float(config.get("fill_target_pct", 90.0))
        self._fill_zero_field_window_s: float = float(
            config.get("fill_zero_field_window_s", 10.0)
        )
        self._fill_complete_window_s: float = float(
            config.get("fill_complete_window_s", 120.0)
        )
        self._max_fill_duration_s: float = float(
            config.get("max_fill_duration_s", 3600.0)
        )
        self._sample_period_s: float = float(config.get("sample_period_s", 10.0))
        # next_due()'s prediction threshold — the same helium_warning_pct
        # key Station._CRYOGENICS_DEFAULTS declares, so **cryogenics_config
        # passed verbatim wires the panel's trend-estimate to the same
        # threshold used elsewhere in the config.
        self._warning_pct: float = float(
            config.get("helium_warning_pct", _DEFAULT_WARNING_PCT)
        )

        if not station.has_vi(self._level_vi_name):
            raise CryoSoftConfigError(
                f"HeliumFillOperation: level_vi={self._level_vi_name!r} is "
                f"not a registered VI on this station."
            )
        self._magnets: list[str] = station.magnet_vi_names()

        self._start_time: float | None = None
        self._start_level_pct: float | None = None
        self._last_level_pct: float | None = None
        # Wall-clock time since the level was last observed to be both
        # >= fill_target_pct and non-rising; None while either condition is
        # unmet. Reset to None on any rise (see sample()).
        self._stable_since: float | None = None
        # The level curve itself lives in OperationBase's shared recorder
        # (_record_sample()/_recording_dict(), plan unified-servicing-log-
        # and-run-recording.md §3) — reset by initiate() via
        # _reset_recording(), appended to by sample().

    # ------------------------------------------------------------------
    # Session hand-off
    # ------------------------------------------------------------------

    def run_summary(self) -> dict[str, Any]:
        """Return the bounded level curve plus start/end level, for the run manifest.

        Called once by the Orchestrator on ``run_finished`` (see
        ``OperationBase.run_summary()``); ``CryogenicsRecorder`` reads this
        back off ``manifest["summary"]`` and writes the curve as this run's
        ``recordings/<run_id>.json`` sidecar alongside the single
        ``servicing`` entry
        it writes for every finished run.

        Returns:
            ``{"recording": {"unix_time": [...], "channels":
            {"<level_vi>.helium_pct": [...]}}, "start_pct": float,
            "end_pct": float}`` — the generic recording shape every operation
            hands off (not fill-specific; ``OperationBase._recording_dict()``
            below), every value JSON-safe (plain floats and lists). The
            ``"<level_vi>.helium_pct"`` channel key is always present, even
            if ``sample()`` was never called (an empty list then). ``start_pct``/
            ``end_pct`` are ``0.0`` if the fill ended before its first sample
            (mirrors the ``or 0.0`` fallback ``CryogenicsRecorder`` already
            uses for a level it never observed).
        """
        recording = self._recording_dict()
        if not recording["channels"]:
            recording["channels"] = {f"{self._level_vi_name}.helium_pct": []}
        return {
            "recording": recording,
            "start_pct": float(self._start_level_pct or 0.0),
            "end_pct": float(self._last_level_pct or 0.0),
        }

    def claimed_vi_names(self) -> set[str]:
        """Claim the level meter and every magnet (plan's admission gate, §1).

        The fill commands the level meter (FAST/SLOW refresh) and drives
        every magnet to zero field at ``initiate()``, holding zero field as
        an invariant for the whole fill — a manual ``set_field`` mid-fill
        would silently break it, so the magnets must be claimed even though
        they are commanded via system targets rather than manual actions.
        Everything else (notably the VTI temperature) stays unclaimed and
        manually controllable while the fill runs.

        Returns:
            ``{level_vi}`` plus every ``Station.magnet_vi_names()`` entry.
        """
        return {self._level_vi_name} | set(self._magnets)

    def get_params(self) -> dict[str, Any]:
        """Return the fill's parameters, for the run manifest.

        The servicing-log recorder reads ``params["person"]`` when composing
        the ``servicing`` log entry on finish.

        Returns:
            ``person`` plus every resolved §9 config value.
        """
        return {
            "person": self._person,
            "level_vi": self._level_vi_name,
            "fill_target_pct": self._fill_target_pct,
            "fill_zero_field_window_s": self._fill_zero_field_window_s,
            "fill_complete_window_s": self._fill_complete_window_s,
            "max_fill_duration_s": self._max_fill_duration_s,
            "sample_period_s": self._sample_period_s,
        }

    # ------------------------------------------------------------------
    # Operations panel: readiness / next-due
    # ------------------------------------------------------------------

    def readiness_conditions(self) -> tuple[ReadinessCondition, ...]:
        """Return the aggregate ``zero_field`` checklist row.

        Mirrors ``initiation_gates()``'s zero-field check, but reads the
        state snapshot passed to ``check()``/``detail()`` (never
        ``self._station.cached_state`` directly), per the readiness-condition
        contract.

        Returns:
            One ``ReadinessCondition`` naming the first magnet not in
            standby in its detail text, or ``()`` if the station has no
            magnets.
        """
        if not self._magnets:
            return ()

        def _magnet_not_standby(state: dict[str, Any]) -> tuple[str | None, str | None]:
            """Return the first magnet not in standby, or (None, None) if all standby."""
            for magnet in self._magnets:
                magnet_state = state.get(magnet, {}).get("magnet_state")
                if magnet_state != "standby":
                    return magnet, magnet_state
            return None, None

        def _holds(state: dict[str, Any]) -> bool:
            name, _state = _magnet_not_standby(state)
            return name is None

        def _detail(state: dict[str, Any]) -> str:
            name, magnet_state = _magnet_not_standby(state)
            if name is None:
                return "all magnets standby"
            if magnet_state is None:
                return f"{name} state unavailable"
            return f"{name} {magnet_state}"

        return (
            ReadinessCondition(
                key="zero_field",
                label="All magnets at zero field",
                check=_holds,
                detail=_detail,
            ),
        )

    def next_due(self, context: dict[str, Any]) -> NextDue | None:
        """Predict when the next fill will be needed from the consumption rate.

        Args:
            context: ``{"state": ..., "now_unix": ..., "consumption_rate_pct_per_h":
                ...}`` — see ``OperationBase.next_due()``. Reads the current
                helium level from ``context["state"][level_vi]["helium_level"]``.

        Returns:
            ``NextDue(due_unix, text)`` with ``hours = (level -
            helium_warning_pct) / rate``, worded as a trend estimate (a
            lagging least-squares fit over a trailing window — see
            ``consumption_rate_pct_per_h()`` — not a real-time forecast, so
            it doesn't account for the current VTI temperature, switch-
            heater state, or whether a measurement is running).
            ``NextDue(None, ...)`` variants when the level or rate is
            unavailable ("consumption unknown"), the rate is not positive
            ("level not falling" — the level is flat or rising), or the
            level is already at/below the warning threshold ("Fill
            overdue …").
        """
        level: float | None = None
        state = context.get("state")
        if isinstance(state, dict):
            vi_state = state.get(self._level_vi_name)
            if isinstance(vi_state, dict):
                raw_level = vi_state.get("helium_level")
                if isinstance(raw_level, (int, float)) and not isinstance(raw_level, bool):
                    level = float(raw_level)

        rate = context.get("consumption_rate_pct_per_h")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            rate = None

        if level is None or rate is None:
            return NextDue(None, "Fill due: consumption unknown")
        if rate <= 0:
            return NextDue(None, "Fill due: level not falling")
        if level <= self._warning_pct:
            return NextDue(
                None, "Fill overdue (trend estimate; level below warning threshold)"
            )

        hours = (level - self._warning_pct) / rate
        now_unix = context.get("now_unix")
        due_unix = (
            float(now_unix) + hours * 3600.0
            if isinstance(now_unix, (int, float)) and not isinstance(now_unix, bool)
            else None
        )
        text = (
            f"Fill due in ~{_humanize_duration_hours(hours)} "
            f"(trend estimate; level {level:.1f} %, warning at "
            f"{self._warning_pct:.1f} %)"
        )
        return NextDue(due_unix, text)

    # ------------------------------------------------------------------
    # OperationBase lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> PhasePlan:
        """Standby whichever magnets aren't already, and switch the level meter to FAST.

        Checks each magnet's cached ``magnet_state()`` first: a magnet
        already in standby is left alone (no redundant command); only a
        magnet not yet in standby gets commanded into its own ``standby()``.
        That VI owns what "standby" means physically (ramp to zero, switch
        heater off where applicable) — this operation only cares that it
        ends up there, verified by ``initiation_gates()`` checking
        ``magnet_state() == "standby"``, never by picking a field value
        itself.

        Returns:
            A ``PhasePlan`` with no system targets, commanding ``standby()``
            on every magnet not already in standby, plus the level meter's
            ``set_refresh_rate(mode=FAST)``.
        """
        self._start_time = time.time()
        self._start_level_pct = None
        self._last_level_pct = None
        self._stable_since = None
        self._reset_recording()

        state = self._station.cached_state
        magnets_to_standby = [
            magnet
            for magnet in self._magnets
            if state.get(magnet, {}).get("magnet_state") != "standby"
        ]

        logger.info(
            "HeliumFillOperation.initiate(): %d/%d magnet(s) need standby, "
            "level_vi=%s FAST",
            len(magnets_to_standby),
            len(self._magnets),
            self._level_vi_name,
        )
        return PhasePlan(
            targets={},
            commands=(
                *(Command(magnet, "standby", {}) for magnet in magnets_to_standby),
                Command(self._level_vi_name, "set_refresh_rate", {"mode": _REFRESH_FAST}),
            ),
            wait_s=0.0,
        )

    def initiation_gates(self) -> tuple[Gate, ...]:
        """Hold until every magnet is in standby, from cached state only.

        Returns:
            One ``Gate("zero_field", ...)`` checking ``magnet_state() ==
            "standby"`` on every magnet, held for
            ``fill_zero_field_window_s``.
        """

        def _all_magnets_standby() -> bool:
            state = self._station.cached_state
            for magnet in self._magnets:
                if state.get(magnet, {}).get("magnet_state") != "standby":
                    return False
            return True

        return (
            Gate(
                "zero_field",
                check=_all_magnets_standby,
                window_s=self._fill_zero_field_window_s,
            ),
        )

    def sample(self) -> None:
        """Read the helium level and append it to the bounded in-memory curve.

        Tracks the start level (first sample), the last level, and the
        "stable since" clock the completion condition in ``step()`` reads:
        the clock resets on any rise and (re)starts once the level is both
        non-rising and at/above ``fill_target_pct``. Magnet fields are not
        recorded: zero field is this operation's own invariant (see
        ``claimed_vi_names()``), and the curve's purpose is level-vs-time,
        not a full station snapshot.

        Raises:
            RuntimeError: If called before ``initiate()``.
        """
        if self._start_time is None:
            raise RuntimeError("HeliumFillOperation.sample() called before initiate()")

        level_vi = self._station.get_vi(self._level_vi_name)
        helium_pct = float(level_vi.helium_level())
        now = time.time()

        if self._start_level_pct is None:
            self._start_level_pct = helium_pct

        rising = (
            self._last_level_pct is not None
            and helium_pct > self._last_level_pct + _RISE_NOISE_FLOOR_PCT
        )
        if rising:
            self._stable_since = None
        elif helium_pct >= self._fill_target_pct:
            if self._stable_since is None:
                self._stable_since = now
        else:
            self._stable_since = None
        self._last_level_pct = helium_pct

        self._record_sample(now, {f"{self._level_vi_name}.helium_pct": helium_pct})

    def step(self) -> StepPlan | None:
        """Keep sampling until the fill completes or times out.

        Returns:
            ``StepPlan(targets={}, wait_s=sample_period_s)`` to sample again,
            or ``None`` once the level has held at/above ``fill_target_pct``
            and non-rising for ``fill_complete_window_s``, or once
            ``max_fill_duration_s`` has elapsed since ``initiate()`` (logged
            at WARNING — the fill did not reach target in time).
        """
        now = time.time()
        if self._start_time is not None and (now - self._start_time) > self._max_fill_duration_s:
            logger.warning(
                "HeliumFillOperation: max_fill_duration_s (%.0f s) exceeded "
                "before reaching fill_target_pct=%.1f%% (last level %s%%).",
                self._max_fill_duration_s,
                self._fill_target_pct,
                f"{self._last_level_pct:.1f}" if self._last_level_pct is not None else "?",
            )
            return None

        if (
            self._last_level_pct is not None
            and self._last_level_pct >= self._fill_target_pct
            and self._stable_since is not None
            and (now - self._stable_since) >= self._fill_complete_window_s
        ):
            return None

        return StepPlan(targets={}, wait_s=self._sample_period_s)

    def standby(self) -> PhasePlan:
        """Restore SLOW refresh.

        Returns:
            A ``PhasePlan`` with the level meter's
            ``set_refresh_rate(mode=SLOW)`` command.
        """
        return PhasePlan(
            targets={},
            commands=(
                Command(self._level_vi_name, "set_refresh_rate", {"mode": _REFRESH_SLOW}),
            ),
            wait_s=0.0,
        )

    def abort(self) -> tuple[Command, ...]:
        """Restore SLOW refresh (never leave the level meter in FAST).

        Returns:
            The level meter's ``set_refresh_rate(mode=SLOW)`` command.
        """
        return (Command(self._level_vi_name, "set_refresh_rate", {"mode": _REFRESH_SLOW}),)

    def postcondition_gates(self) -> tuple[Gate, ...]:
        """Verify SLOW refresh is restored and the level did not fall below start.

        The Orchestrator evaluates each gate exactly once, immediately, as
        the run ends; an unmet gate is recorded on the run manifest's
        ``postconditions_unmet`` list rather than blocking completion.

        Returns:
            Two gates (``window_s=0``, matching the one-shot evaluation):
            ``refresh_slow`` (from cached state) and ``level_held_or_rose``
            (comparing the last sampled level to the first).
        """

        def _refresh_slow() -> bool:
            state = self._station.cached_state
            mode = state.get(self._level_vi_name, {}).get("get_refresh_rate")
            return mode == _REFRESH_SLOW

        def _level_held_or_rose() -> bool:
            if self._start_level_pct is None or self._last_level_pct is None:
                return True
            return self._last_level_pct >= self._start_level_pct

        return (
            Gate("refresh_slow", check=_refresh_slow, window_s=0.0),
            Gate("level_held_or_rose", check=_level_held_or_rose, window_s=0.0),
        )

    def get_progress(self) -> float:
        """Return fractional progress toward ``fill_target_pct``, clamped 0..1.

        Returns:
            ``(last_level - start_level) / (fill_target_pct - start_level)``,
            clamped to ``[0.0, 1.0]``; ``0.0`` before the first sample.
        """
        if self._start_level_pct is None or self._last_level_pct is None:
            return 0.0
        span = self._fill_target_pct - self._start_level_pct
        if span <= 0:
            return 1.0
        progress = (self._last_level_pct - self._start_level_pct) / span
        return max(0.0, min(1.0, progress))
