# ---
# description: |
#   OperationBase: the L4 contract for multi-step cryostat-servicing actions
#   (helium fill, sample change — see docs/plans/cryogenics-logbook.md §4).
#   Distinct from BaseProcedure: operation-scope command access, tolerated
#   safety flags, one-shot verified postconditions, an optional (not
#   required) data file, and higher submission priority. Drives the same
#   Orchestrator state machine as a procedure via a thin adapter
#   (measure()/change_sweep_step()) so the Orchestrator needs almost no
#   branching to support both request types — it detects an operation purely
#   by duck-typing (``command_scope == "operation"``) and never imports this
#   module (keeps import-linter contract C5 clean). Also declares the
#   readiness/next-due contract (plan §12) the GUI's Operations panel renders
#   generically: ReadinessCondition/NextDue dataclasses plus the
#   readiness_conditions()/next_due() hooks and the ready_message/config_key
#   class attributes. Finish is immediate (docs/plans/operation-concurrency-
#   and-error-scoping.md §2): postcondition_gates() is evaluated exactly once
#   as the run ends, never held or timed out. A duck-typed run_summary()
#   hook (same plan, §4) lets a subclass hand a small JSON-serialisable
#   summary (e.g. the helium fill's bounded in-memory level curve) to the
#   session layer without an HDF5 file — the Orchestrator merges it into the
#   run manifest's "summary" key on run_finished. A shared, decimating,
#   multi-channel recorder helper (_record_sample()/_recording_dict(), plan
#   unified-servicing-log-and-run-recording.md §3) is opt-in for exactly this
#   hand-off — HeliumFillOperation and SampleChangeOperation both use it.
#   hold_for_operator (plan §1) declares a "hold phase" operation whose
#   step() stays open-ended (never returns None on its own) until the
#   operator clicks Finish; the Operations panel reads it to show the ready
#   banner mid-run, once every readiness condition holds, instead of only
#   after the run ends.
# entry_point: Not run directly. Subclassed by concrete operations
#   (``cryosoft.procedures.operations.*``).
# dependencies:
#   - cryosoft.core.gates (Gate)
#   - cryosoft.core.plan (Command, PhasePlan, StepPlan)
# input: |
#   Concrete subclasses implement initiate()/step()/standby() (and optionally
#   sample()/abort()/initiation_gates()/postcondition_gates()/
#   readiness_conditions()/next_due()/run_summary()); the Orchestrator drives
#   the lifecycle exactly like a BaseProcedure, submitted via
#   Orchestrator.run_operation() / queue_operation(); the GUI's Operations
#   panel drives readiness_conditions()/next_due() against per-tick state
#   snapshots, never touching hardware.
# process: |
#   measure() (final) forwards to sample(). change_sweep_step() (final)
#   returns None once request_finish() has set the graceful-finish flag,
#   otherwise it defers to step(). Together these are the adapter that lets
#   the Orchestrator's existing MEASURING/SWEEPING states drive an operation
#   with no new states and minimal branching. A "hold phase" operation
#   (hold_for_operator = True) keeps step() returning a StepPlan
#   indefinitely once its own setup work is done, so the run stays open
#   until request_finish() arrives — the SAME mechanism a plain open-ended
#   step() already used (e.g. the helium fill's sampling loop), just held
#   past task completion instead of a fixed condition.
# output: |
#   PhasePlan / StepPlan / Command / Gate objects consumed by the
#   Orchestrator, exactly like a BaseProcedure's. readiness_conditions() /
#   next_due() output ReadinessCondition / NextDue objects consumed only by
#   the GUI (never by the Orchestrator). run_summary() outputs a plain dict
#   consumed by the Orchestrator (merged into the run manifest) and, from
#   there, the session layer (e.g. CryogenicsRecorder), which reads a
#   "recording" key in the _recording_dict() shape as this run's sidecar.
# last_updated: 2026-07-23
# ---

"""OperationBase — the L4 contract for cryostat-servicing operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, final

from cryosoft.core.gates import Gate
from cryosoft.core.plan import Command, PhasePlan, StepPlan

__all__ = ["NextDue", "OperationBase", "ReadinessCondition"]


@dataclass(frozen=True)
class ReadinessCondition:
    """One live readiness check, rendered by the GUI as a checklist row (plan §12).

    An operation declares its readiness conditions via
    ``OperationBase.readiness_conditions()``; the GUI's Operations panel
    builds one checklist row per condition and re-evaluates ``check()``/
    ``detail()`` every ``on_states_updated`` tick against the latest state
    snapshot — no extra hardware poll.

    Attributes:
        key: Stable identifier, snake_case (e.g. ``"zero_field"``). Used by
            the GUI as a widget-name suffix; must be unique within one
            operation's ``readiness_conditions()`` tuple.
        label: Human-readable checklist label, e.g. ``"All magnets at zero
            field"``.
        check: ``state_snapshot -> bool`` — ``True`` when the condition
            holds. ``state_snapshot`` is the Orchestrator's per-tick
            ``{vi_name: {field: value}}`` dict (the same shape
            ``on_states_updated`` receives). Must be a pure read (cached
            state only) — never touches hardware.
        detail: Optional ``state_snapshot -> str`` giving a live detail
            string next to the label, e.g. ``lambda s: f"currently {t:.1f}
            K"``. ``None`` means the checklist row shows no detail text.
    """

    key: str
    label: str
    check: Callable[[dict[str, Any]], bool]
    detail: Callable[[dict[str, Any]], str] | None = None


@dataclass(frozen=True)
class NextDue:
    """When an operation is predicted to next be needed (plan §12).

    Returned by ``OperationBase.next_due()``; the GUI shows ``text`` in the
    operation card's header when not ``None``.

    Attributes:
        due_unix: Predicted unix time the operation will next be needed, or
            ``None`` when unknown/not predictable (the GUI still shows
            ``text`` in that case — e.g. "consumption unknown").
        text: Human-readable display string, e.g. ``"Fill due in ~2.3 d
            (level 62.0 %, warning at 30.0 %)"``.
    """

    due_unix: float | None
    text: str


class OperationBase:
    """Abstract base class for multi-step cryostat-servicing operations.

    An operation is a *different contract submitted to the same single
    writer* as a procedure (see plan §2): both speak the ``PhasePlan`` /
    ``StepPlan`` / ``Target`` / ``Command`` / ``Gate`` currency and are driven
    by the same Orchestrator tick loop, state machine, watchdog, and safety
    checks. What differs is submission priority and the EMERGENCY carve-out
    (``Orchestrator.run_operation()`` / ``queue_operation()``), the capability
    scope its plans may carry (``command_scope = "operation"`` — see the
    capability-scope standard in GLOSSARY.md), and completion: a verified
    ``postcondition_gates()`` phase and an optional (never mandatory) data
    file, instead of a procedure's required dataset.

    Orchestrator adapter (read this before overriding anything)
    -------------------------------------------------------------
    The Orchestrator's state machine already knows how to drive a
    ``BaseProcedure``-shaped object through
    INITIATING -> RAMPING -> MEASURING -> SWEEPING -> STANDBY. Rather than
    teach it a second vocabulary, this base class exposes the SAME four
    duck-typed methods a procedure does, and implements the two "loop" methods
    (``measure()`` and ``change_sweep_step()``) as **final** adapters over the
    operation-shaped lifecycle a subclass actually overrides:

    * ``measure()`` (final) — calls ``self.sample()``. A subclass overrides
      ``sample()``, not ``measure()``.
    * ``change_sweep_step()`` (final) — returns ``None`` immediately once
      ``request_finish()`` has set the graceful-finish flag (so the
      Orchestrator proceeds straight to STANDBY, exactly as when a procedure's
      ``change_sweep_step()`` returns ``None``); otherwise it defers to
      ``self.step()``. A subclass overrides ``step()``, not
      ``change_sweep_step()``.

    Do not override ``measure()`` or ``change_sweep_step()`` in a subclass —
    they are marked ``@typing.final`` for exactly this reason. This is what
    lets ``Orchestrator.run_operation()`` reuse the existing setup/dispatch
    path with essentially no new state-machine branching (plan §2, §4.2).

    Readiness / next-due contract (Operations panel, plan §12)
    -------------------------------------------------------------
    Two overridable hooks and two class attributes let the GUI's Operations
    panel render a live readiness checklist, a next-due prediction, and a
    ready banner with ZERO per-operation GUI code (the "hybrid declaration"
    standard: the operation *class* declares what to check and how to
    predict; the config supplies thresholds via ``**config``):

    * ``ready_message`` — shown in the panel's green ready banner once a run
      of this operation has finished ``done`` AND every current
      ``readiness_conditions()`` holds. Empty (the default) means "no
      banner" — the panel shows nothing, not a generic fallback string.
    * ``readiness_conditions()`` — the checklist. Default ``()`` (no
      checklist rows). Each condition's ``check``/``detail`` callables take
      the Orchestrator's per-tick state snapshot and must be pure reads, no
      hardware access (see ``ReadinessCondition``'s own docstring).
    * ``next_due(context)`` — the header's next-due prediction. Default
      ``None`` (no next-due line). ``context`` is a documented, extensible
      dict the GUI assembles fresh on every update; keys defined today:

      - ``"state"``: the latest Orchestrator state snapshot dict
        (``{vi_name: {field: value}}``).
      - ``"now_unix"``: current unix time (``float``).
      - ``"consumption_rate_pct_per_h"``: ``float | None`` — computed by
        the GUI panel, not here. An operation must NOT import the session
        layer to compute its own rate (contract C12: nothing below the GUI
        imports the session layer) — this is deliberate layering, not an
        oversight, and is why the rate arrives pre-computed in ``context``
        instead of being read from ``cryosoft.session.servicing_log``
        directly.

      A future context key is additive — an operation that does not read it
      is unaffected, so old and new operations coexist in the same panel.
    * ``config_key`` — the string a ``config: {key: block}`` mapping (e.g.
      ``operations:`` in ``devices.yaml``) uses to select this class when
      the GUI builds cards generically. Empty by default (opts out of
      generic config-block discovery — used by operations, like the helium
      fill, that are wired some other way).

    Class attributes:
        name: Human-readable display name.
        description: One-line description.
        ready_message: Shown in the Operations panel's green ready banner;
            see "Readiness / next-due contract" above. Empty by default.
        config_key: Maps a ``config:`` sub-block key (e.g.
            ``operations.sample_change:``) to this class for the GUI's
            generic card-building discovery; see "Readiness / next-due
            contract" above. Empty by default.
        run_kind: Recorded verbatim into the Orchestrator's run manifests
            (``"kind"`` field) via the existing
            ``getattr(procedure, "run_kind", "run")`` lookup — no Orchestrator
            change needed for this to flow through. Fixed at ``"operation"``.
        tolerated_safety_flags: Safety flags that do not abort *this*
            operation (plan §7) — e.g. the helium fill tolerates
            ``"helium_low"`` because its whole purpose is fixing that
            condition. A flag NOT in this set still escalates to EMERGENCY
            exactly as for any procedure. Empty by default (tolerates
            nothing).
        command_scope: Fixed at ``"operation"`` — the capability tier this
            operation's plans may carry (see
            ``Station.send_measurement_commands``). Do not override.
        hold_for_operator: ``False`` by default. ``True`` declares a "hold
            phase" operation (plan §1) whose ``step()`` keeps the run open
            (returns a ``StepPlan``, never ``None``) once its own setup work
            is done, until the operator clicks Finish
            (``request_finish()``). The Operations panel's ready banner
            reads this: for a hold-phase operation it shows mid-run, the
            instant every readiness condition holds, instead of waiting for
            the run to finish. ``SampleChangeOperation`` sets this ``True``;
            ``HeliumFillOperation`` leaves the default (its own completion
            condition, not the operator, ends the run).

    Lifecycle (override in a concrete subclass):
        initiate() -> PhasePlan: Initial targets/commands, mirroring
            ``BaseProcedure.initiate()``. A DataManager is NOT required — an
            operation that wants an HDF5 dataset may still create one and its
            manifest then carries the path like any run, but a small,
            bounded, in-memory series (e.g. the helium fill's level curve,
            handed to the session layer via ``run_summary()`` instead of a
            data file) is preferred for anything that does not need HDF5's
            random-access/column layout.
        step() -> StepPlan | None: Next tick's targets/wait, or ``None`` to
            end the operation (park via ``standby()``). Honoured only while
            ``request_finish()`` has not been called (see the adapter note
            above — once finish is requested, ``step()`` is never called
            again).
        sample() -> None: Optional per-tick observation hook (e.g. the fill
            appends one bounded in-memory level point). Default: no-op.
        standby() -> PhasePlan: Park / safe-off plan, mirroring
            ``BaseProcedure.standby()``.
        abort() -> tuple[Command, ...]: Cleanup commands on user abort or
            ERROR/EMERGENCY entry, mirroring ``BaseProcedure.abort()``.
            Default: no commands.
        initiation_gates() -> tuple[Gate, ...]: As for procedures — gates that
            must pass once, before the operation's first ``sample()``.
            Default: none.
        postcondition_gates() -> tuple[Gate, ...]: Evaluated by the
            Orchestrator exactly ONCE, immediately after ``standby()`` is
            dispatched, as the run ends (docs/plans/operation-concurrency-
            and-error-scoping.md §2 — "immediate finish"). Each gate's
            ``check()`` is read a single time (via ``Gate.check_once()``);
            there is no holding and no timeout. Unmet gates never block the
            run from finishing — they are recorded on the run manifest's
            ``postconditions_unmet`` list (gate names) and logged at
            WARNING. Default: none (an empty ``postconditions_unmet``).
        get_progress() -> float: Fractional progress, 0.0 to 1.0. Default 0.0
            (operations are not required to report progress).
        get_params() -> dict: Parameter values recorded in the run manifest,
            mirroring ``BaseProcedure.get_params()``. Default ``{}``.
        run_summary() -> dict: A small, JSON-serialisable hand-off to the
            session layer, merged into the run manifest's ``summary`` key
            when the run ends (docs/plans/operation-concurrency-and-error-
            scoping.md §4 — e.g. the helium fill's bounded in-memory level
            curve). Default ``{}`` (nothing to hand off). Read duck-typed by
            the Orchestrator via ``getattr`` — it never imports
            ``OperationBase`` (contract C5) — and guarded by a broad
            try/except there, so a broken override can never prevent the run
            from finishing.

    Graceful finish (plan §4.3; immediate finish, operation-concurrency-and-
    error-scoping.md §2):
        ``Orchestrator.finish_operation()`` calls ``request_finish()`` on the
        active operation. The very next ``change_sweep_step()`` (the adapter
        above) then returns ``None`` regardless of what ``step()`` would have
        returned, ending the open-ended loop. The Orchestrator then dispatches
        ``standby()``'s plan, evaluates ``postcondition_gates()`` once, and
        ends the run — all without waiting for any ramp (in flight, or one
        ``standby()`` itself starts) to complete; a ramp still moving after
        the run ends continues under the existing manual-ramp handling.
    """

    name: str = ""
    description: str = ""
    ready_message: str = ""
    config_key: str = ""
    run_kind: str = "operation"
    tolerated_safety_flags: frozenset[str] = frozenset()
    command_scope: str = "operation"
    #: Declares a "hold phase" operation (docs/plans/unified-servicing-log-
    #: and-run-recording.md §1): ``step()`` keeps returning a ``StepPlan``
    #: (never ``None``) once its own work is done, so the run stays active
    #: indefinitely until the operator clicks Finish
    #: (``Orchestrator.finish_operation()`` -> ``request_finish()``). The
    #: Operations panel reads this to decide WHEN the ready banner may show:
    #: ``False`` (default) keeps the existing post-run-only banner
    #: (``ready_message`` shown once the run finished ``done`` AND every
    #: readiness condition holds); ``True`` (``SampleChangeOperation``) also
    #: shows it mid-run, the instant every readiness condition holds — for a
    #: hold-phase operation "ready" means "you may act now", true well
    #: before Finish is clicked. Finish itself is unaffected either way.
    hold_for_operator: bool = False

    #: Upper bound on the shared in-memory recording (``_record_sample()``/
    #: ``_recording_dict()`` below; docs/plans/unified-servicing-log-and-
    #: run-recording.md §3). Once the recorded series would exceed this many
    #: points, it is decimated: every other point is dropped
    #: (``series[::2]``, across the shared time axis AND every channel
    #: together, so they stay the same length) and the effective sample
    #: stride doubles — memory stays bounded for an arbitrarily long run
    #: while the series still spans the whole run, never just the tail. A
    #: class attribute (not a config key) so a test can lower it to force
    #: the decimation path deterministically. Generalises
    #: ``HeliumFillOperation``'s original ``_MAX_CURVE_POINTS``.
    _MAX_RECORDING_POINTS: int = 4000

    def __init__(self) -> None:
        """Initialise the graceful-finish flag and the shared recorder.

        A concrete subclass that needs constructor arguments (a Station,
        parameters, …) should call ``super().__init__()`` from its own
        ``__init__``.
        """
        #: Set by ``request_finish()`` (plan §4.3); read only by the
        #: ``change_sweep_step()`` adapter below. Public so a test or a
        #: caller can inspect it, but a subclass should treat it as
        #: read-only — set it via ``request_finish()``, never directly.
        self.finish_requested: bool = False
        self._reset_recording()

    # ------------------------------------------------------------------
    # Override in subclass
    # ------------------------------------------------------------------

    def initiate(self) -> PhasePlan:
        """Set up the operation and return the initial plan.

        Returns:
            A ``PhasePlan`` bundling ``targets``, ``commands``, and
            ``wait_s`` — exactly as ``BaseProcedure.initiate()``.

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement initiate()")

    def step(self) -> StepPlan | None:
        """Return the next tick's plan, or ``None`` when the operation is done.

        Called by the ``change_sweep_step()`` adapter, and only while no
        finish has been requested (see the class docstring's adapter note).

        Returns:
            A ``StepPlan`` for the next step, or ``None`` to proceed to
            ``standby()``.

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement step()")

    def sample(self) -> None:
        """Optional per-tick observation hook, called by the ``measure()`` adapter.

        Default implementation does nothing. Override to record a data point
        (e.g. a helium-fill level reading) without needing a DataManager.
        """

    def standby(self) -> PhasePlan:
        """Return the safe-parking plan.

        Returns:
            A ``PhasePlan`` describing where to park the system — exactly as
            ``BaseProcedure.standby()``.

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement standby()")

    def abort(self) -> tuple[Command, ...]:
        """Return cleanup commands for a user abort or ERROR/EMERGENCY entry.

        Returns:
            An ordered ``tuple[Command, ...]``; empty by default.
        """
        return ()

    def initiation_gates(self) -> tuple[Gate, ...]:
        """Gates that must pass once, before the operation's first ``sample()``.

        Returns:
            An ordered ``tuple[Gate, ...]``; empty by default.
        """
        return ()

    def postcondition_gates(self) -> tuple[Gate, ...]:
        """Gates evaluated once, immediately, as the run ends.

        The Orchestrator reads each gate's ``check()`` exactly once (via
        ``Gate.check_once()``) right after dispatching ``standby()``'s plan —
        no holding, no timeout (docs/plans/operation-concurrency-and-error-
        scoping.md §2). An unmet gate never blocks the run; it is named in
        the run manifest's ``postconditions_unmet`` list and logged at
        WARNING.

        Returns:
            An ordered ``tuple[Gate, ...]``; empty by default (nothing to
            verify, so ``postconditions_unmet`` is always empty).
        """
        return ()

    def get_progress(self) -> float:
        """Return fractional progress from 0.0 to 1.0.

        Returns:
            0.0 by default — operations are not required to report progress.
        """
        return 0.0

    def get_params(self) -> dict[str, Any]:
        """Return this operation's parameter values, for the run manifest.

        Returns:
            ``{}`` by default.
        """
        return {}

    def run_summary(self) -> dict[str, Any]:
        """Return a small, JSON-serialisable hand-off for the session layer.

        Called once by the Orchestrator when it emits ``run_finished``
        (docs/plans/operation-concurrency-and-error-scoping.md §4), duck-typed
        via ``getattr`` — the Orchestrator never imports ``OperationBase``
        (contract C5) — and merged into the run manifest's ``summary`` key.
        The call is guarded there by a broad try/except, so a subclass
        override that raises can never prevent the run from finishing; it
        just yields an empty ``summary``. Keep the return value small and
        plain (``float``/``str``/``bool``/``list``/``dict`` only — no numpy
        arrays, no HDF5 handles) since it round-trips through the manifest
        signal and, from there, into a session-layer store.

        Returns:
            ``{}`` by default (nothing to hand off).
        """
        return {}

    def claimed_vi_names(self) -> set[str] | None:
        """Return the VI names this operation exclusively owns while running.

        Concurrency-scope hook (docs/plans/operation-concurrency-and-error-
        scoping.md §1): the Orchestrator captures this once, at run start,
        into ``_active_claims`` and consults it to decide whether a manual
        front-panel action submitted while this operation is running may be
        admitted. A VI named in the returned set is refused (the refusal
        names this operation as the owner); every VI NOT in the set stays
        under manual control exactly as in IDLE — e.g. the helium fill
        claims only its level meter, so the VTI and every other instrument
        stay manually controllable during a fill.

        Returns:
            A set of VI names, as registered on the station
            (``Station.get_vi_names()``), this operation claims — or
            ``None`` (the default) to claim every system VI. ``None`` is the
            safe default: narrowing what a run blocks is an explicit
            per-class opt-in, never assumed, so a subclass that does not
            override this behaves exactly as if it locked the whole
            instrument (unchanged behavior for every operation written
            before this hook existed).
        """
        return None

    def readiness_conditions(self) -> tuple[ReadinessCondition, ...]:
        """Return this operation's live readiness checklist (plan §12).

        Called once by the GUI, on a display instance constructed at panel
        init; the returned ``ReadinessCondition``s' ``check``/``detail``
        callables are then re-invoked every ``on_states_updated`` tick
        against the latest state snapshot — this method itself takes no
        snapshot and must not read live state directly.

        Returns:
            ``()`` by default (no checklist rows).
        """
        return ()

    def next_due(self, context: dict[str, Any]) -> NextDue | None:
        """Predict when this operation will next be needed (plan §12).

        Args:
            context: GUI-assembled, extensible dict. Keys defined today:
                ``"state"`` (the latest state snapshot dict), ``"now_unix"``
                (current unix time, ``float``), and
                ``"consumption_rate_pct_per_h"`` (``float | None``,
                computed by the GUI panel — see the class docstring's
                "Readiness / next-due contract" section for why this is
                passed in rather than computed here).

        Returns:
            ``None`` by default (no next-due line shown).
        """
        return None

    # ------------------------------------------------------------------
    # Shared recording helper (opt-in; docs/plans/unified-servicing-log-and-
    # run-recording.md §3) — a bounded, decimating, multi-channel in-memory
    # recorder every operation may use from its own ``sample()`` instead of
    # rolling its own (this generalises ``HeliumFillOperation``'s original
    # single-channel level curve). Not part of the override contract above:
    # a subclass calls these directly, it does not override them.
    # ------------------------------------------------------------------

    def _reset_recording(self) -> None:
        """Clear the shared in-memory recording.

        Call from ``initiate()`` before the first ``_record_sample()`` of a
        run (mirrors ``HeliumFillOperation.initiate()`` resetting its old
        curve fields) — also called once by ``__init__`` so a fresh instance
        starts with a valid, empty recording even if ``initiate()`` is never
        reached.
        """
        self._recording_unix_time: list[float] = []
        self._recording_channels: dict[str, list[float]] = {}
        self._recording_stride: int = 1
        self._recording_raw_count: int = 0

    def _record_sample(self, unix_time: float, values: dict[str, float]) -> None:
        """Append one multi-channel sample to the shared bounded recording.

        All channels share one time axis, so decimation (see
        ``_MAX_RECORDING_POINTS``) drops points across every channel
        together — the series never desynchronises.

        Args:
            unix_time: The sample's wall-clock time.
            values: ``{channel_name: value}`` for every channel this
                operation records, e.g. ``{"temperature_vti.temperature":
                295.1, "magnet_z.get_field": 0.0}``. The channel set must be
                the SAME on every call within one run (first call fixes it).

        Raises:
            ValueError: If *values*' channel names differ from a previous
                call's within the same run.
        """
        if self._recording_channels and set(values) != set(self._recording_channels):
            raise ValueError(
                f"{type(self).__name__}._record_sample: channel set changed "
                f"from {sorted(self._recording_channels)} to "
                f"{sorted(values)} — every call within one run must record "
                f"the same channels."
            )

        self._recording_raw_count += 1
        if self._recording_raw_count % self._recording_stride != 0:
            return

        if not self._recording_channels:
            self._recording_channels = {name: [] for name in values}

        self._recording_unix_time.append(float(unix_time))
        for name, value in values.items():
            self._recording_channels[name].append(float(value))

        if len(self._recording_unix_time) > self._MAX_RECORDING_POINTS:
            self._recording_unix_time = self._recording_unix_time[::2]
            for name in self._recording_channels:
                self._recording_channels[name] = self._recording_channels[name][::2]
            self._recording_stride *= 2

    def _recording_dict(self) -> dict[str, Any]:
        """Return the recording so far in the generic sidecar shape.

        Returns:
            ``{"unix_time": [...], "channels": {name: [...], ...}}`` — a
            fresh copy of the accumulated recording (``{"unix_time": [],
            "channels": {}}`` if ``_record_sample()`` was never called).
            The shape ``CryogenicsRecorder`` reads off a run's
            ``run_summary()["recording"]`` (docs/plans/unified-servicing-
            log-and-run-recording.md §3).
        """
        return {
            "unix_time": list(self._recording_unix_time),
            "channels": {
                name: list(series) for name, series in self._recording_channels.items()
            },
        }

    # ------------------------------------------------------------------
    # Orchestrator adapter — final; do not override (see class docstring)
    # ------------------------------------------------------------------

    @final
    def measure(self) -> None:
        """Adapter: forwards to ``sample()``. Do not override — see class docstring."""
        self.sample()

    @final
    def change_sweep_step(self) -> StepPlan | None:
        """Adapter: honours the graceful-finish flag, else defers to ``step()``.

        Do not override — see class docstring.

        Returns:
            ``None`` if ``request_finish()`` has been called; otherwise
            ``self.step()``.
        """
        if self.finish_requested:
            return None
        return self.step()

    def request_finish(self) -> None:
        """Set the graceful-finish flag (plan §4.3, ``finish_requested``).

        The next ``change_sweep_step()`` call returns ``None`` regardless of
        what ``step()`` would otherwise return, ending an open-ended
        operation and starting the normal STANDBY -> postcondition path.
        Called by ``Orchestrator.finish_operation()``; idempotent.
        """
        self.finish_requested = True
