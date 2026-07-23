# ---
# description: |
#   HeliumFillOperation: the first concrete OperationBase (L4) subclass —
#   forces every magnet to zero field, switches the level meter to FAST
#   refresh, samples the helium level once per sample_period_s into a
#   bounded in-memory curve, and finishes once the level holds at/above
#   fill_target_pct for fill_complete_window_s (or max_fill_duration_s
#   elapses). Restores SLOW refresh on standby/abort so an aborted fill
#   never leaves the meter in FAST, and verifies that restoration (plus that
#   the level actually rose) via postcondition_gates(). Tolerates the
#   helium_low safety flag — see docs/plans/cryogenics-logbook.md §8.1. No
#   HDF5 file: the level curve is handed to the session layer via
#   run_summary() instead (docs/plans/operation-concurrency-and-error-
#   scoping.md §4).
# entry_point: Not run directly. Constructed by the GUI's fill dialog (Phase
#   5) or a test, submitted via Orchestrator.run_operation()/queue_operation().
# dependencies:
#   - cryosoft.core.exceptions (CryoSoftConfigError)
#   - cryosoft.core.gates (Gate)
#   - cryosoft.core.operation (OperationBase)
#   - cryosoft.core.plan (Command, PhasePlan, StepPlan, Target)
#   - cryosoft.core.station (Station) — VI access only through this, never a
#     direct virtual_instruments import (contract C6)
# input: |
#   Constructor: station (positional), person (keyword, default ""), and
#   **config carrying the docs/plans/cryogenics-logbook.md §9 cryogenics:
#   keys (level_vi, fill_target_pct, fill_zero_field_eps_T,
#   fill_zero_field_window_s, fill_complete_window_s, max_fill_duration_s,
#   sample_period_s), each with a class-matching default — main.py can pass
#   **read_cryogenics_config(config_path) verbatim (its extra keys, e.g.
#   helium_warning_pct, are simply ignored — this now also covers the
#   retired data_directory kwarg some older callers may still pass).
#   magnet_vi_names() resolves the magnet list; level_vi (default
#   "level_meter") must be a registered VI.
# process: |
#   initiate() ramps every magnet to 0 T and switches the level meter to
#   FAST. initiation_gates() holds until every magnet reads |B| < eps for
#   fill_zero_field_window_s (from cached state — no extra hardware poll).
#   sample() reads the level, appends (unix_time, helium_pct) to a bounded
#   in-memory series (decimated once it exceeds _MAX_CURVE_POINTS — see
#   run_summary()'s docstring), and tracks the start level and a "stable
#   since" clock that resets whenever the level rises. step() keeps sampling
#   every sample_period_s until the level has held at/above fill_target_pct
#   and non-rising for fill_complete_window_s (done), or max_fill_duration_s
#   has elapsed (done, with a WARNING logged — the graceful-finish flag is
#   handled by the OperationBase adapter, nothing extra needed here).
#   standby()/abort() restore SLOW refresh. postcondition_gates() verifies
#   SLOW refresh and that the level did not fall below its start value.
#   run_summary() (called once by the Orchestrator on run_finished) returns
#   the accumulated curve plus start/end level.
# output: |
#   PhasePlan/StepPlan/Command/Gate objects consumed by the Orchestrator.
#   run_summary() -> dict consumed by the Orchestrator (merged into the run
#   manifest's "summary" key) and, from there, CryogenicsRecorder, which
#   writes the "recording" key as this run's recordings/<run_id>.json
#   sidecar (docs/plans/unified-servicing-log-and-run-recording.md §3) and
#   folds start_pct/end_pct into the single "servicing" entry it writes for
#   every finished run. The curve itself is now OperationBase's shared
#   _record_sample()/_recording_dict() recorder helper (Phase 3), not a
#   fill-specific field — the fill's contribution is just the one channel
#   name and the cap (via OperationBase._MAX_RECORDING_POINTS). No data
#   file: data_filepath is not defined (getattr default is None/"" on the
#   manifest), matching OperationBase's "data file is optional" contract.
# last_updated: 2026-07-23
# ---

"""HeliumFillOperation — force all magnets to zero field and fill helium."""

from __future__ import annotations

import logging
import time
from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import NextDue, OperationBase, ReadinessCondition
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
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

    The first concrete operation (plan §8.1): a servicing action, not a
    measurement. ``initiate()`` ramps every magnet
    (``Station.magnet_vi_names()``) to 0 T and switches the configured level
    meter to FAST refresh; ``initiation_gates()`` holds the run until zero
    field is confirmed and held; ``sample()``/``step()`` poll the helium
    level once per ``sample_period_s`` until it has settled at/above
    ``fill_target_pct`` (or ``max_fill_duration_s`` elapses); ``standby()``/
    ``abort()`` restore SLOW refresh so an aborted fill never leaves the
    meter in FAST; ``postcondition_gates()`` verifies that restoration and
    that the level actually rose.

    ``tolerated_safety_flags = frozenset({"helium_low"})``: the fill's whole
    purpose is fixing low helium, so that flag must not abort it — a
    non-tolerated flag (e.g. ``quench``) still aborts the fill exactly like
    any other run (plan §7).

    Readiness / next-due (Operations panel, plan §12): ``readiness_conditions()``
    exposes one aggregate ``zero_field`` checklist row (empty if the station
    has no magnets); ``next_due()`` predicts when the level will cross the
    configured warning threshold from the measured consumption rate passed
    in via ``context``.

    No HDF5 file (docs/plans/operation-concurrency-and-error-scoping.md §4):
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
                ``fill_target_pct``, ``fill_zero_field_eps_T``,
                ``fill_zero_field_window_s``, ``fill_complete_window_s``,
                ``max_fill_duration_s``, ``sample_period_s``,
                ``helium_warning_pct`` (read by ``next_due()``, plan §12 —
                the same key the recorder's advisory warning uses) — each
                with a sane default matching §9 so this constructs from a
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
        self._fill_zero_field_eps_T: float = float(
            config.get("fill_zero_field_eps_T", 0.005)
        )
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
        # next_due()'s prediction threshold (plan §12) — the same
        # helium_warning_pct key the recorder's advisory cryo_warning signal
        # already reads (see cryosoft.session.servicing_log.CryogenicsRecorder
        # and Station._CRYOGENICS_DEFAULTS), so **cryogenics_config passed
        # verbatim wires the panel's prediction to the same threshold the
        # operator already sees the low-helium warning at.
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
    # Session hand-off (docs/plans/operation-concurrency-and-error-
    # scoping.md §4)
    # ------------------------------------------------------------------

    def run_summary(self) -> dict[str, Any]:
        """Return the bounded level curve plus start/end level, for the run manifest.

        Called once by the Orchestrator on ``run_finished`` (see
        ``OperationBase.run_summary()``); ``CryogenicsRecorder`` reads this
        back off ``manifest["summary"]`` and writes the curve as this run's
        ``recordings/<run_id>.json`` sidecar (docs/plans/unified-servicing-
        log-and-run-recording.md §3) alongside the single ``servicing`` entry
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
            "fill_zero_field_eps_T": self._fill_zero_field_eps_T,
            "fill_zero_field_window_s": self._fill_zero_field_window_s,
            "fill_complete_window_s": self._fill_complete_window_s,
            "max_fill_duration_s": self._max_fill_duration_s,
            "sample_period_s": self._sample_period_s,
        }

    # ------------------------------------------------------------------
    # Operations panel: readiness / next-due (plan §12)
    # ------------------------------------------------------------------

    def readiness_conditions(self) -> tuple[ReadinessCondition, ...]:
        """Return the aggregate ``zero_field`` checklist row.

        Mirrors ``initiation_gates()``'s zero-field check, but reads the
        state snapshot passed to ``check()``/``detail()`` (never
        ``self._station.cached_state`` directly), per the readiness-condition
        contract.

        Returns:
            One ``ReadinessCondition`` naming the worst-offending magnet in
            its detail text, or ``()`` if the station has no magnets.
        """
        if not self._magnets:
            return ()

        def _worst_offender(state: dict[str, Any]) -> tuple[str, float | None]:
            worst_name = self._magnets[0]
            worst_field: float | None = None
            worst_abs = -1.0
            for magnet in self._magnets:
                field = state.get(magnet, {}).get("get_field")
                if isinstance(field, bool) or not isinstance(field, (int, float)):
                    return magnet, None
                if abs(float(field)) > worst_abs:
                    worst_abs = abs(float(field))
                    worst_name = magnet
                    worst_field = float(field)
            return worst_name, worst_field

        def _holds(state: dict[str, Any]) -> bool:
            _name, field = _worst_offender(state)
            return field is not None and abs(field) < self._fill_zero_field_eps_T

        def _detail(state: dict[str, Any]) -> str:
            name, field = _worst_offender(state)
            if field is None:
                return f"{name} field reading unavailable"
            return f"{name} at {field:.2f} T"

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
            helium_warning_pct) / rate``; ``NextDue(None, ...)`` variants
            when the level or rate is unavailable ("consumption unknown"),
            the rate is not positive ("level not falling" — the level is
            flat or rising), or the level is already at/below the warning
            threshold ("Fill overdue …").
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
            return NextDue(None, "Fill overdue (level below warning threshold)")

        hours = (level - self._warning_pct) / rate
        now_unix = context.get("now_unix")
        due_unix = (
            float(now_unix) + hours * 3600.0
            if isinstance(now_unix, (int, float)) and not isinstance(now_unix, bool)
            else None
        )
        text = (
            f"Fill due in ~{_humanize_duration_hours(hours)} "
            f"(level {level:.1f} %, warning at {self._warning_pct:.1f} %)"
        )
        return NextDue(due_unix, text)

    # ------------------------------------------------------------------
    # OperationBase lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> PhasePlan:
        """Ramp every magnet to zero field and switch the level meter to FAST.

        Returns:
            A ``PhasePlan`` with every magnet targeted at 0 T and the level
            meter's ``set_refresh_rate(mode=FAST)`` command.
        """
        self._start_time = time.time()
        self._start_level_pct = None
        self._last_level_pct = None
        self._stable_since = None
        self._reset_recording()

        logger.info(
            "HeliumFillOperation.initiate(): %d magnet(s) to zero field, "
            "level_vi=%s FAST",
            len(self._magnets),
            self._level_vi_name,
        )
        return PhasePlan(
            targets={magnet: Target(0.0) for magnet in self._magnets},
            commands=(
                Command(self._level_vi_name, "set_refresh_rate", {"mode": _REFRESH_FAST}),
            ),
            wait_s=0.0,
        )

    def initiation_gates(self) -> tuple[Gate, ...]:
        """Hold until every magnet reads zero field, from cached state only.

        Returns:
            One ``Gate("zero_field", ...)`` checking ``|field| <
            fill_zero_field_eps_T`` on every magnet, held for
            ``fill_zero_field_window_s``.
        """

        def _all_zero_field() -> bool:
            state = self._station.cached_state
            for magnet in self._magnets:
                field = state.get(magnet, {}).get("get_field")
                if isinstance(field, bool) or not isinstance(field, (int, float)):
                    return False
                if abs(float(field)) >= self._fill_zero_field_eps_T:
                    return False
            return True

        return (
            Gate(
                "zero_field",
                check=_all_zero_field,
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
        the run ends (docs/plans/operation-concurrency-and-error-scoping.md
        §2); an unmet gate is recorded on the run manifest's
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
