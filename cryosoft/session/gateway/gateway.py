"""The in-process client: one connection, one role, one actor identity.

``Gateway`` is what an autonomous client actually holds. It is deliberately
thin, because being thin is the design: the engine has two clients and they
must see the same system and be seen doing the same things, so this object
adds a permission check and an identity to the **Control contract** and
NOTHING else. It defines no vocabulary of its own, holds no instrument, and
opens no socket — in-process, no network, no thread.

Three jobs:

* **Stamp the identity.** Every command leaves here as
  ``Actor(kind="agent", id=..., role=...)``, so a verdict, a run record and
  the status bar all name the authority the action was taken under.
* **Authorize before forwarding.** ``roles.authorize()`` decides; a refusal
  is answered as a ``BLOCKED_ROLE`` verdict on the engine's OWN
  ``verdict_emitted`` stream, so the human's window sees the agent being
  refused exactly as it sees it being obeyed. A refusal never reaches the
  engine at all.
* **Mirror, never poll.** Every read is answered from the latest
  ``StatusSnapshot`` / ``StationInfo`` the engine broadcast, following the
  verdict standard's rule that a client answers reads locally and never
  calls into the engine for them. **Attendance** and the **Kill switch**
  come off that same mirror, which is what makes an agent's permission check
  read the same fact every other client sees.

The engine is duck-typed on purpose (see ``EngineClient``): today it is the
Orchestrator, tomorrow a proxy over a transport, and this file will not
notice the difference.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Mapping, Protocol, runtime_checkable

from cryosoft.core.events import (
    Actor,
    ActorKind,
    AgentGate,
    Command,
    CommandName,
    StationInfo,
    StatusSnapshot,
    Verdict,
)
from cryosoft.session.gateway.roles import Role, authorize

logger = logging.getLogger(__name__)


@runtime_checkable
class EngineClient(Protocol):
    """The engine surface a gateway needs — the control contract, nothing more.

    A ``Protocol`` rather than the Orchestrator itself, because the whole
    point of the contract is that a second implementation (a proxy over a
    transport) is a drop-in. Both signals are duck-typed as objects with
    ``connect``/``emit``, so this module needs no Qt import.

    Attributes:
        verdict_emitted: The stream carrying one ``Verdict`` per submitted
            command.
        event_emitted: The engine's one event stream.
    """

    verdict_emitted: Any
    event_emitted: Any

    def submit(self, command: Command) -> str:
        """Carry out one command and answer it with one verdict."""
        ...


class Gateway:
    """One authorised, identified connection to the engine.

    Attributes:
        role: The authority this connection declared for itself.
        actor: The ``Actor`` stamped on every command it submits.
    """

    def __init__(
        self,
        engine: EngineClient,
        role: Role | str,
        actor_id: str,
        *,
        station_info: StationInfo | Any | None = None,
    ) -> None:
        """Attach to an engine under a declared role.

        Args:
            engine: Anything satisfying ``EngineClient`` — the Orchestrator
                today, a transport proxy later.
            role: The ``Role`` this connection acts under, as the enum member
                or its string value.
            actor_id: A stable identifier for the client — an agent
                deployment name, a CLI invocation, a subagent's name. It goes
                into every verdict and every event this connection causes.
            station_info: The station's declaration snapshot, or a zero-
                argument callable returning it (e.g. ``station.station_info``).
                Optional: when omitted, the engine's own ``station_info()`` is
                used if it has one, and in either case the mirror is refreshed
                from every ``StationInfo`` the engine broadcasts. It is needed
                because a ``submit_vi_action``'s action class depends on the
                VI kind of the instrument it targets.

        Raises:
            ValueError: If *role* is not a known ``Role``.
        """
        self._engine = engine
        self.role = Role(role)
        self.actor = Actor(kind=ActorKind.AGENT, id=str(actor_id), role=self.role.value)

        self._status: StatusSnapshot | None = None
        self._station_info: StationInfo = self._initial_station_info(
            engine, station_info
        )
        # Sequence numbers: the highest the engine has been seen to use, and
        # this gateway's own counter for a refusal the engine never sees. The
        # local counter is kept at or above the engine's so a refusal is never
        # ordered before something that happened earlier.
        self._engine_seq = self._station_info.seq
        self._local_seq = self._engine_seq

        engine.event_emitted.connect(self._observe_event)
        engine.verdict_emitted.connect(self._observe_verdict)
        logger.info(
            "Gateway attached: actor %r under role %r", self.actor.id, self.role.value
        )

    # ── Construction helpers ──────────────────────────────────────────

    @staticmethod
    def _initial_station_info(
        engine: EngineClient, station_info: StationInfo | Any | None
    ) -> StationInfo:
        """Resolve the declaration snapshot to start the mirror from.

        Args:
            engine: The engine being attached to.
            station_info: The caller's snapshot, a callable returning one, or
                ``None``.

        Returns:
            The snapshot to start with — an empty ``StationInfo`` when
            neither the caller nor the engine can supply one, in which case
            the mirror fills in on the engine's next broadcast.
        """
        candidate = station_info
        if candidate is None:
            candidate = getattr(engine, "station_info", None)
        if callable(candidate):
            candidate = candidate()
        if isinstance(candidate, StationInfo):
            return candidate
        return StationInfo()

    # ── The mirror ────────────────────────────────────────────────────

    def _observe_event(self, event: Any) -> None:
        """Update the local mirror from one engine event.

        Guarded like every other reporting surface: a mirror that cannot be
        updated must never propagate an exception back into the engine's own
        emit.

        Args:
            event: Anything on the engine's event stream.
        """
        try:
            self._engine_seq = max(self._engine_seq, int(getattr(event, "seq", 0)))
            if isinstance(event, StatusSnapshot):
                self._status = event
            elif isinstance(event, StationInfo):
                self._station_info = event
        except Exception:  # noqa: BLE001 — mirroring must never disrupt the engine
            logger.exception("gateway mirror update failed (non-fatal)")

    def _observe_verdict(self, verdict: Verdict) -> None:
        """Keep the sequence mirror abreast of the verdict stream too.

        Args:
            verdict: Any verdict the engine emitted, for this client or another.
        """
        try:
            self._engine_seq = max(self._engine_seq, int(verdict.seq))
        except Exception:  # noqa: BLE001 — mirroring must never disrupt the engine
            logger.exception("gateway verdict mirror update failed (non-fatal)")

    def _next_seq(self) -> int:
        """Return the sequence number to stamp on a locally emitted refusal.

        Derived from the engine's own counter wherever it is reachable — the
        highest ``seq`` seen on either stream — so a refusal the engine never
        saw still orders correctly against everything it did say.

        Returns:
            A number strictly greater than every sequence number seen so far.
        """
        self._local_seq = max(self._local_seq, self._engine_seq) + 1
        return self._local_seq

    # ── Reads: answered from the mirror, never by calling the engine ──

    def status(self) -> StatusSnapshot | None:
        """Return the latest status snapshot, or ``None`` before the first tick.

        Returns:
            The engine's most recent ``StatusSnapshot``.
        """
        return self._status

    def station(self) -> StationInfo:
        """Return the station's latest declaration snapshot.

        Returns:
            The most recent ``StationInfo`` — what every instrument reads,
            can be asked to do, and within which bounds.
        """
        return self._station_info

    def state(self) -> str:
        """Return the engine's current state name.

        Returns:
            The state machine's state, or ``""`` before the first snapshot.
        """
        return self._status.state if self._status is not None else ""

    def attended(self) -> bool:
        """Return whether a human is watching, per the engine's mirror.

        Returns:
            The published **Attendance** value; ``True`` (the restrictive
            reading for an agent) before the first snapshot arrives.
        """
        return self._status.attended if self._status is not None else True

    def agent_gate(self) -> AgentGate:
        """Return the **Kill switch**'s setting, per the engine's mirror.

        Returns:
            The published ``AgentGate``; ``ACTIVE`` before the first snapshot
            arrives, since the engine enforces the gate itself regardless.
        """
        if self._status is None:
            return AgentGate.ACTIVE
        return AgentGate(self._status.agent_gate)

    # ── The one write path ────────────────────────────────────────────

    def permits(
        self, name: CommandName | str, args: Mapping[str, Any] | None = None
    ) -> Verdict | None:
        """Answer whether this connection may submit that command, without doing it.

        The same decision ``submit()`` makes, exposed so a client can render
        its own tool list, or explain a refusal, without provoking one.

        Args:
            name: The command, as a ``CommandName`` or its string value.
            args: The command's arguments, JSON-safe.

        Returns:
            ``None`` when it would be forwarded, or the ``BLOCKED_ROLE``
            verdict that would refuse it.

        Raises:
            ValueError: If *name* is not a known ``CommandName``.
            TypeError: If *args* is not a mapping of JSON-safe values.
        """
        return self._authorize(self._command(name, args))

    def submit(
        self, name: CommandName | str, args: Mapping[str, Any] | None = None
    ) -> str:
        """Submit one command under this connection's identity.

        Stamps the actor, runs the permission check, and either forwards to
        the engine or answers the command here with a ``BLOCKED_ROLE``
        verdict on the engine's own ``verdict_emitted`` stream — so exactly
        one verdict answers every request either way, and the human's window
        sees an agent being refused exactly as it sees it being obeyed. A
        refused command never reaches the engine.

        Args:
            name: The command, as a ``CommandName`` or its string value.
            args: The command's arguments, JSON-safe and shaped by the
                target's ``ParamSpec``s — see ``Orchestrator.submit()`` for
                the per-command conventions.

        Returns:
            The request id the answering verdict carries back, whether the
            command was forwarded or refused here.

        Raises:
            ValueError: If *name* is not a known ``CommandName``.
            TypeError: If *args* is not a mapping of JSON-safe values.
        """
        command = self._command(name, args)
        refusal = self._authorize(command)
        if refusal is None:
            return self._engine.submit(command)
        self._emit(refusal)
        return command.request_id

    def _command(
        self, name: CommandName | str, args: Mapping[str, Any] | None
    ) -> Command:
        """Build one command stamped with this connection's actor.

        Args:
            name: The command name.
            args: Its arguments, or ``None``.

        Returns:
            The ``Command``, validated by the contract at construction.
        """
        return Command(name=name, actor=self.actor, args=dict(args or {}))

    def _authorize(self, command: Command) -> Verdict | None:
        """Run the permission check for one command against the current mirror.

        Args:
            command: The command to judge.

        Returns:
            ``None`` when permitted, else the refusing verdict.
        """
        refusal = authorize(
            self.actor,
            command,
            self._station_info,
            self.attended(),
            self.agent_gate(),
        )
        if refusal is None:
            return None
        # Numbered only now: a permitted command is numbered by the engine
        # that carries it out, so allocating here would leave a hole for
        # every command that was never refused.
        return replace(refusal, seq=self._next_seq())

    def _emit(self, verdict: Verdict) -> None:
        """Put one locally decided verdict onto the engine's verdict stream.

        Guarded: a listener that raises must not propagate back into the
        client that submitted the command.

        Args:
            verdict: The refusal to broadcast.
        """
        try:
            self._engine.verdict_emitted.emit(verdict)
        except Exception:  # noqa: BLE001 — a signal failure must not raise at the client
            logger.exception("gateway verdict emit failed")
