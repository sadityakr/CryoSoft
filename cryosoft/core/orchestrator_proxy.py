"""OrchestratorProxy — a client's adapter of the control contract.

The engine has exactly two clients, the GUI and the agent, and the boundary
between it and them is ONE contract (``core/events.py``) rendered two ways
rather than a proxy for one and a gateway for the other. This module is the
first rendering: a ``QObject`` with

* one typed method per ``CommandName``, each building a ``Command`` with the
  operator actor and handing it to ``Orchestrator.submit()``, returning the
  ``request_id`` the answering ``Verdict`` will carry;
* one signal per event type, plus ``verdict`` and the union ``event``;
* the engine's existing per-purpose signals re-exposed under their own names,
  so a widget written against the Orchestrator keeps working when it is handed
  a proxy instead — which is what lets the whole GUI move behind this boundary
  in one step rather than widget by widget;
* every read answered from a ``StatusMirror``, never by calling the engine.

The agent gateway will render the same ``CommandName`` enumeration as JSON tool
schemas; ``tests/test_conformance.py`` diffs the three surfaces, so neither
client can offer an action the other cannot see.

**The run queue.** Waiting runs are immutable specs in the session layer's
``RunQueue``, and the engine PULLS the next one through ``next_procedure()``,
so the normal queueing path does not pass through this proxy at all: a client
adds a ``RunSpec`` through ``RunQueueHost``/``ExperimentManager``, which
validates it, and asks for one ``QueueChanged`` broadcast afterwards through
``publish_queue()``. ``queue_procedure``/``queue_operation`` remain here
because ``CommandName`` enumerates them — they are the direct-handover path
for a caller that already holds a BUILT run — and, like ``run_procedure``/
``run_operation``, they forward (see ``_forward()``) because ``Command.args``
is JSON and cannot carry an object. ``publish_queue`` and the two pull-seam
attributes are the queue's other half of this surface: not commands, and
documented as such where they are defined.

**Why this is core and not GUI.** Its clients are the GUI *and* the session
layer — ``ExperimentManager`` holds one for ``envelope_variables()`` and
``set_experiment_envelope()`` — and import contract C11 forbids the session
layer from importing ``cryosoft.gui``. A client adapter both layers hold is
core-layer material, exactly like the contract types it speaks.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QObject, pyqtSignal

from cryosoft.core import events as ev
from cryosoft.core.status_mirror import StatusMirror

if TYPE_CHECKING:
    from cryosoft.core.instrument_host import InstrumentHost
    from cryosoft.core.plan import ExperimentEnvelope

logger = logging.getLogger(__name__)

#: The engine's per-purpose signals, re-exposed 1:1 so no widget has to move
#: to the typed event stream at once. Name -> the Qt signature the engine
#: declares, which the proxy must match exactly or a connected slot breaks.
_PASSTHROUGH_SIGNALS: tuple[str, ...] = (
    "states_updated",
    "monitoring_changed",
    "state_changed",
    "procedure_progress",
    "procedure_finished",
    "run_started",
    "run_finished",
    "error_occurred",
    "error_event",
    "action_blocked",
    "action_succeeded",
    "action_failed",
    "instrument_reconnected",
    "instrument_disconnected",
    "measurement_ready",
    "operational_status",
    "ramps_updated",
    "status_message",
    "operation_status",
    "operation_progress",
)


class OrchestratorProxy(QObject):
    """The control contract, rendered for a Python client.

    Every action is a ``Command`` answered by exactly one ``Verdict``; every
    consequence arrives as an ``Event``; every read is answered from the
    ``StatusMirror`` this proxy owns. A client therefore never blocks on the
    engine, which is what makes the engine free to live on another thread.

    Signals:
        verdict: One ``Verdict`` per submitted command.
        event: Every member of the ``Event`` union, unfiltered.
        state_change_event, status_snapshot_event, station_info_event,
            readings_event, datapoint_event, run_started_event,
            run_finished_event, queue_changed_event: the same stream split by
            type, for a client that wants one kind. Suffixed so they never
            collide with the engine's legacy per-purpose signals of the same
            name (``run_started(dict)`` is not ``RunStarted``).
        states_updated, monitoring_changed, state_changed, …: the engine's own
            signals, re-emitted unchanged.

    Args:
        engine: The Orchestrator to submit to and mirror. Duck-typed rather
            than imported so a test double needs only ``submit()`` and the
            signals.
        mirror: The status mirror answering every read. When omitted one is
            built and primed from *engine*, which is the inline construction
            path; a threaded host primes its own and passes it in, because
            the priming reads must happen on the engine's thread.
        parent: Optional Qt parent.
    """

    verdict = pyqtSignal(object)  # events.Verdict
    event = pyqtSignal(object)  # the events.Event union

    state_change_event = pyqtSignal(object)
    status_snapshot_event = pyqtSignal(object)
    station_info_event = pyqtSignal(object)
    readings_event = pyqtSignal(object)
    datapoint_event = pyqtSignal(object)
    run_started_event = pyqtSignal(object)
    run_finished_event = pyqtSignal(object)
    queue_changed_event = pyqtSignal(object)

    # ── The engine's per-purpose signals, re-declared with its signatures ──
    states_updated = pyqtSignal(dict)
    monitoring_changed = pyqtSignal(bool)
    state_changed = pyqtSignal(str)
    procedure_progress = pyqtSignal(float)
    procedure_finished = pyqtSignal()
    run_started = pyqtSignal(dict)
    run_finished = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)
    error_event = pyqtSignal(object)
    action_blocked = pyqtSignal(str)
    action_succeeded = pyqtSignal(str, str)
    action_failed = pyqtSignal(str, str, str)
    instrument_reconnected = pyqtSignal(str)
    instrument_disconnected = pyqtSignal(str)
    measurement_ready = pyqtSignal(dict)
    operational_status = pyqtSignal(dict)
    ramps_updated = pyqtSignal(list)
    status_message = pyqtSignal(str)
    operation_status = pyqtSignal(str)
    operation_progress = pyqtSignal(float)

    #: Which typed signal each event type is re-emitted on. Built from the
    #: contract's own classes so a new event type is registered by adding it
    #: here and nowhere else.
    _EVENT_SIGNALS: dict[type, str] = {
        ev.StateChange: "state_change_event",
        ev.StatusSnapshot: "status_snapshot_event",
        ev.StationInfo: "station_info_event",
        ev.Readings: "readings_event",
        ev.Datapoint: "datapoint_event",
        ev.RunStarted: "run_started_event",
        ev.RunFinished: "run_finished_event",
        ev.QueueChanged: "queue_changed_event",
    }

    def __init__(
        self,
        engine: Any,
        mirror: StatusMirror | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._mirror = (
            mirror if mirror is not None else StatusMirror.for_engine(engine)
        )
        engine.verdict_emitted.connect(self.verdict)
        engine.event_emitted.connect(self._on_event)
        for name in _PASSTHROUGH_SIGNALS:
            getattr(engine, name).connect(getattr(self, name))

    @classmethod
    def for_host(
        cls, host: InstrumentHost, parent: QObject | None = None
    ) -> OrchestratorProxy:
        """Build the proxy an :class:`InstrumentHost` hands its client.

        The mirror is primed from the host's own captured client state rather
        than by reading the engine, because in threaded mode those reads
        belong to the engine's thread and the host has already taken them
        there.

        Args:
            host: A started host.
            parent: Optional Qt parent for the proxy.

        Returns:
            The proxy, mirroring that host's engine.
        """
        mirror = StatusMirror(parent=parent)
        mirror.prime(*host.client_state())
        mirror.attach(host.orchestrator)
        return cls(host.orchestrator, mirror, parent=parent)

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------

    def _on_event(self, event: object) -> None:
        """Re-emit one engine event on the union signal and its typed one.

        Args:
            event: Any member of the ``Event`` union. An unknown type still
                reaches ``event``: a client that understands it should not be
                cut off because this table has not caught up.
        """
        self.event.emit(event)
        signal_name = self._EVENT_SIGNALS.get(type(event))
        if signal_name is not None:
            getattr(self, signal_name).emit(event)

    # ------------------------------------------------------------------
    # Reads — delegated to the mirror, never to the engine
    # ------------------------------------------------------------------

    @property
    def status(self) -> StatusMirror:
        """The status mirror this proxy answers reads from."""
        return self._mirror

    @property
    def state(self) -> str:
        """The engine's state name as of the last snapshot."""
        return self._mirror.state

    def is_monitoring(self) -> bool:
        """Return whether the per-tick monitoring cycle was polling."""
        return self._mirror.is_monitoring()

    def pause_pending(self) -> bool:
        """Return whether a pause is waiting for the current datapoint."""
        return self._mirror.pause_pending()

    def active_run_kind(self) -> str | None:
        """Return ``"procedure"``/``"operation"``, or ``None`` when idle."""
        return self._mirror.active_run_kind()

    def scanner_enabled(self) -> bool:
        """Return whether scanner-sensitive procedures may use the scanner."""
        return self._mirror.scanner_enabled()

    def override_active(self, vi_name: str | None = None) -> bool:
        """Return whether a manual override is in force.

        Args:
            vi_name: A VI to ask about, or ``None`` for the station-wide
                answer.

        Returns:
            True while the override admits action on that scope.
        """
        return self._mirror.override_active(vi_name)

    def manual_override_expires_at(self) -> float | None:
        """Return the soonest-expiring override's unix time, or ``None``."""
        return self._mirror.manual_override_expires_at()

    def held_vi_names(self) -> frozenset[str]:
        """Return every VI under a hold-severity condition."""
        return self._mirror.held_vi_names()

    def active_ramps(self) -> tuple[dict[str, Any], ...]:
        """Return one JSON dict per ramp running as of the last snapshot."""
        return self._mirror.active_ramps()

    def availabilities(self) -> dict[str, dict[str, Any]]:
        """Return ``{vi_name: availability dict}`` for every configured VI."""
        return self._mirror.availabilities()

    def availability(self, vi_name: str) -> dict[str, Any]:
        """Return one VI's availability record.

        Args:
            vi_name: The VI to ask about.

        Returns:
            A copy of the record, empty for an instrument the last snapshot
            did not carry.
        """
        return self._mirror.availability(vi_name)

    def vi_faults(self) -> dict[str, dict[str, Any]]:
        """Return ``{vi_name: fault dict}`` for every faulted VI."""
        return self._mirror.vi_faults()

    def offline_reason(self, vi_name: str) -> str:
        """Return why one VI is offline, or ``""`` when it is not.

        Args:
            vi_name: The VI to ask about.

        Returns:
            The offline registry's human-readable reason.
        """
        return self._mirror.offline_reason(vi_name)

    def envelope_variables(self) -> dict[str, dict[str, Any]]:
        """Return ``{vi_name: envelope-variable dict}`` for the session envelope."""
        return self._mirror.envelope_variables()

    def get_operational_status(self) -> dict[str, Any]:
        """Return the most recent operational-status record."""
        return self._mirror.get_operational_status()

    def station_info(self) -> ev.StationInfo:
        """Return the station's declaration as last published."""
        return self._mirror.station_info()

    def instrument_info(self, vi_name: str) -> ev.InstrumentInfo | None:
        """Return one configured instrument's declaration.

        Args:
            vi_name: The configured VI's name.

        Returns:
            Its ``InstrumentInfo``, or ``None`` when the declaration names no
            such instrument.
        """
        return self._mirror.instrument_info(vi_name)

    def attended(self) -> bool:
        """Return whether a human is watching this experiment."""
        return self._mirror.attended()

    def agent_gate(self) -> str:
        """Return the kill switch's setting, one of ``AgentGate``'s values."""
        return self._mirror.agent_gate()

    # ------------------------------------------------------------------
    # Commands — one per CommandName
    # ------------------------------------------------------------------

    def _submit(self, name: ev.CommandName, **args: Any) -> str:
        """Build one operator ``Command`` and hand it to the engine.

        Args:
            name: Which command.
            **args: Its JSON-safe arguments.

        Returns:
            The ``request_id`` the answering ``Verdict`` carries.
        """
        return self._engine.submit(
            ev.Command(name=name, actor=ev.OPERATOR, args=args)
        )

    def _forward(self, name: ev.CommandName, **kwargs: Any) -> str:
        """Call an engine command whose arguments the contract cannot carry.

        Four commands take a built run OBJECT — ``run_procedure``,
        ``run_operation`` and their queueing twins — and ``Command.args`` is
        JSON by contract, so those cannot be submitted as a payload while the
        client is the one constructing the object. The engine's own
        ``submit()`` already accepts the JSON form (a class name plus params,
        resolved through its run catalog), which is the path an agent takes.

        The waiting queue already holds specs, so ``queue_procedure`` and
        ``queue_operation`` are no longer how a run is queued — a client adds
        a ``RunSpec`` through the session layer's ``RunQueueHost`` and the
        engine pulls it. What is left here is the direct-handover path, for a
        caller holding a run it built itself, and the two ``run_*`` twins that
        start one immediately.

        The actor is the same operator sentinel a submitted command carries,
        so accountability is identical — what is missing is the correlated
        verdict, not the attribution.

        Args:
            name: Which command, for the log and for symmetry with
                ``_submit()``.
            **kwargs: The engine method's own keyword arguments.

        Returns:
            A fresh ``request_id``, so every proxy method answers the same
            shape even though this one has no verdict to correlate it with.
        """
        command = ev.Command(name=name, actor=ev.OPERATOR)
        getattr(self._engine, name.value)(actor=ev.OPERATOR, **kwargs)
        return command.request_id

    # ── Runs and the queue ─────────────────────────────────────────────

    def run_procedure(self, procedure: Any) -> str:
        """Start a built procedure immediately.

        Args:
            procedure: The ready procedure instance.

        Returns:
            The command's request id (see ``_forward()``).
        """
        return self._forward(ev.CommandName.RUN_PROCEDURE, procedure=procedure)

    def run_operation(self, operation: Any) -> str:
        """Start a built operation immediately.

        Args:
            operation: The ready operation instance.

        Returns:
            The command's request id (see ``_forward()``).
        """
        return self._forward(ev.CommandName.RUN_OPERATION, operation=operation)

    def queue_procedure(self, procedure: Any) -> str:
        """Add a built procedure to the run queue.

        Args:
            procedure: The ready procedure instance.

        Returns:
            The command's request id (see ``_forward()``).
        """
        return self._forward(ev.CommandName.QUEUE_PROCEDURE, procedure=procedure)

    def queue_operation(self, operation: Any) -> str:
        """Add a built operation to the run queue.

        Args:
            operation: The ready operation instance.

        Returns:
            The command's request id (see ``_forward()``).
        """
        return self._forward(ev.CommandName.QUEUE_OPERATION, operation=operation)

    def run_queue(self) -> str:
        """Start the next queued run if the engine is free to.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.RUN_QUEUE)

    # ── The queue seam — not commands ──────────────────────────────────
    # The waiting queue is data in the session layer and the engine pulls
    # from it, so these three are how a client that OWNS that queue reaches
    # the engine: one broadcast request, and the two attributes the engine
    # asks through. They are forwarded rather than submitted because none of
    # them is an action the engine can refuse — installing a callable is
    # wiring, and a broadcast starts nothing.

    def publish_queue(self, *, actor: ev.Actor = ev.OPERATOR) -> None:
        """Ask the engine to broadcast the queue as the client now holds it.

        The queue lives outside the engine, so the engine cannot see an add,
        a removal or a reorder happen; whoever changed it calls this and the
        resulting ``QueueChanged`` goes out on the one event stream.

        Args:
            actor: Who made the change, defaulting to the operator sentinel.
        """
        self._engine.publish_queue(actor=actor)

    @property
    def next_procedure(self) -> Any:
        """The engine's **pull seam**: what it asks for the next run.

        ``None`` until a client installs one, which is how a caller can tell
        that nobody has claimed the seam yet.
        """
        return self._engine.next_procedure

    @next_procedure.setter
    def next_procedure(self, seam: Any) -> None:
        self._engine.next_procedure = seam

    @property
    def queue_snapshot(self) -> Any:
        """The callable the engine reads the waiting entries from."""
        return self._engine.queue_snapshot

    @queue_snapshot.setter
    def queue_snapshot(self, seam: Any) -> None:
        self._engine.queue_snapshot = seam

    def pause_procedure(self) -> str:
        """Pause the active run, holding the hardware.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.PAUSE_PROCEDURE)

    def resume_procedure(self) -> str:
        """Resume from PAUSED, or withdraw a pause not yet landed.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.RESUME_PROCEDURE)

    def abort_procedure(self) -> str:
        """Abort the active run, holding instruments where they are.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.ABORT_PROCEDURE)

    # ── Operation steps ────────────────────────────────────────────────

    def confirm_operation(self, key: str) -> str:
        """Confirm one of the active operation's operator confirmations.

        Args:
            key: The confirmation's declared key.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.CONFIRM_OPERATION, key=key)

    def skip_operation_step(self, key: str) -> str:
        """Skip the active operation's current, skippable step.

        Args:
            key: The step's declared key.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.SKIP_OPERATION_STEP, key=key)

    def finish_operation(self) -> str:
        """Ask the active operation to finish gracefully.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.FINISH_OPERATION)

    # ── Instrument actions ─────────────────────────────────────────────

    def submit_vi_action(self, vi_name: str, method_name: str, **kwargs: Any) -> str:
        """Ask one instrument to carry out one declared capability.

        Args:
            vi_name: The target instrument.
            method_name: The capability, an ``@control`` or a lifecycle
                action.
            **kwargs: The capability's own parameters, flat scalars shaped by
                its ``ParamSpec``s.

        Returns:
            The command's request id; the verdict arrives when the tick
            drains the action.
        """
        return self._submit(
            ev.CommandName.SUBMIT_VI_ACTION,
            vi_name=vi_name,
            method_name=method_name,
            **kwargs,
        )

    def submit_global_action(self, action: str) -> str:
        """Fan a global lifecycle action out over every instrument.

        Args:
            action: ``"initiate_all"`` or ``"standby_all"``.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.SUBMIT_GLOBAL_ACTION, action=action)

    def stop_ramp(self, vi_name: str) -> str:
        """Stop one instrument's ramp where it stands.

        Args:
            vi_name: The ramping instrument.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.STOP_RAMP, vi_name=vi_name)

    def connect_instrument(self, vi_name: str) -> str:
        """Bring an offline instrument online.

        Args:
            vi_name: The offline instrument.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.CONNECT_INSTRUMENT, vi_name=vi_name)

    def disconnect_instrument(self, vi_name: str) -> str:
        """Release a live instrument to its own front panel.

        Args:
            vi_name: The live instrument.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.DISCONNECT_INSTRUMENT, vi_name=vi_name)

    def ping_instrument(self, vi_name: str) -> str:
        """Send one instrument's identity query — the connection check.

        Args:
            vi_name: The instrument to probe.

        Returns:
            The command's request id; the answer arrives on the verdict and
            on ``action_succeeded``/``action_failed``.
        """
        return self._submit(ev.CommandName.PING_INSTRUMENT, vi_name=vi_name)

    # ── Faults, safety and recovery ────────────────────────────────────

    def emergency_standby(self, reason: str) -> str:
        """Stand every instrument down, from any state.

        Args:
            reason: Why, recorded and shown verbatim.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.EMERGENCY_STANDBY, reason=reason)

    def acknowledge(self) -> str:
        """Acknowledge an EMERGENCY, or unlock the held instruments.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.ACKNOWLEDGE)

    def acknowledge_fault(self, vi_name: str) -> str:
        """Acknowledge one instrument's active runtime fault.

        Args:
            vi_name: The faulted instrument.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.ACKNOWLEDGE_FAULT, vi_name=vi_name)

    def retry_fault(self, vi_name: str) -> str:
        """Reset one instrument's error counter and poll it once more.

        Args:
            vi_name: The faulted instrument.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.RETRY_FAULT, vi_name=vi_name)

    def recover_from_error(self) -> str:
        """Leave ERROR and return to IDLE.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.RECOVER_FROM_ERROR)

    # ── Monitoring and policy ──────────────────────────────────────────

    def start_monitoring(self) -> str:
        """Begin the per-tick monitoring cycle.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.START_MONITORING)

    def stop_monitoring(self) -> str:
        """Stop the per-tick monitoring cycle (refused outside IDLE/ERROR).

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.STOP_MONITORING)

    def set_scanner_enabled(self, enabled: bool) -> str:
        """Toggle scanner availability for scanner-sensitive procedures.

        Args:
            enabled: True to make the scanner available.

        Returns:
            The command's request id.
        """
        return self._submit(
            ev.CommandName.SET_SCANNER_ENABLED, enabled=bool(enabled)
        )

    def set_attendance(self, attended: bool) -> str:
        """Record whether a human is watching the running experiment.

        Args:
            attended: True when a human is present, False when the experiment
                runs unattended.

        Returns:
            The command's request id.
        """
        return self._submit(ev.CommandName.SET_ATTENDANCE, attended=bool(attended))

    def set_agent_gate(self, state: ev.AgentGate | str) -> str:
        """Set the kill switch: how much of the engine agents may reach.

        The gate crosses as its string value, so the command stays JSON
        exactly like every other.

        Args:
            state: An ``events.AgentGate`` member or its string value.

        Returns:
            The command's request id.

        Raises:
            ValueError: If *state* is not a known ``AgentGate``.
        """
        return self._submit(
            ev.CommandName.SET_AGENT_GATE, state=ev.AgentGate(state).value
        )

    def set_experiment_envelope(self, envelope: ExperimentEnvelope | None) -> str:
        """Install (or clear) the session envelope's per-instrument bounds.

        The envelope crosses as its plain-dict form, the one
        ``ExperimentEnvelope.from_dict()`` reads, so the command stays JSON
        exactly like every other.

        Args:
            envelope: The typed envelope, or ``None`` to clear it.

        Returns:
            The command's request id.
        """
        payload: dict[str, Any] | None = None
        if envelope is not None:
            payload = {
                vi_name: dataclasses.asdict(bound)
                for vi_name, bound in envelope.bounds.items()
            }
        return self._submit(
            ev.CommandName.SET_EXPERIMENT_ENVELOPE, envelope=payload
        )
