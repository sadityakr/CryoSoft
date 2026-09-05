"""The **Embedded assistant**'s runtime: a tool-use loop over the gateway (L6).

**The assistant is just another gateway client.** It holds one ``Gateway``,
its tools are EXACTLY ``Gateway.tool_schemas()``, and every one of them is
executed by ``Gateway.call_tool()``. Nothing here reaches an instrument, a
Station, a VI or the Orchestrator, and nothing here defines a capability of
its own — which is the whole point: the assistant acts under the same
**Role**, is bounded by the same session envelope, is stopped by the same
**Kill switch**, is judged by the same **Attendance** rule, and leaves the
same **Agent feed** trail as an agent in another process. A permission the
assistant had and an agent did not would be a second control path, and a
second control path is exactly what the gateway exists to prevent.

The system-prompt standard
--------------------------

``ASSISTANT_SYSTEM_PROMPT`` is a constant, in the same sense the draft prompt
is: written down here, plain text, read from nothing in the environment, and
changed deliberately. Four rules are load-bearing, and every one of them
exists because the assistant talks to a physicist about a cryostat that is
running:

1. **Say what it is.** The assistant is a client of one experiment's gateway,
   not an operator and not an oracle. It reads the station through tools and
   acts only through them.
2. **Probe before running.** ``validate_run`` answers "may I run this, and how
   long will it take?" and ``probe_run`` runs the short version. A full run is
   proposed only after those have answered, because a run that was never
   validated is a run whose refusal a human discovers hours later.
3. **Name a refusal verbatim.** The gateway answers every call, and a refusal
   carries a ``code``, a ``reason`` and a ``detail`` naming the rule. The
   assistant quotes that reason rather than paraphrasing it: "the observer
   role does not grant run_control actions" is actionable, "I could not do
   that" is not.
4. **Never claim an action without an ``OK`` verdict.** Every command tool
   answers with the engine's own verdict. If the verdict is not ``OK``, the
   action did not happen, and saying otherwise is the single most damaging
   thing an assistant at a cryostat can do.

The **Chat client** contract
----------------------------

One method, ``create_message(system, messages, tools, max_tokens)
-> ChatResult``, so the model is one injectable collaborator and every test
runs against ``FakeChatClient`` with no network, no SDK and no key — the same
shape the **Draft client** contract already has, one step wider because a
conversation has tools and a draft does not. The loop itself lives here, not
in the client, so that the ordering below is a property of this module rather
than of whichever vendor SDK is installed.

The assistant's thread rule
---------------------------

**Model calls run off the GUI thread; tool calls run on it.** A model call is
network I/O measured in seconds, and the GUI thread is the thread that drives
the tick, so ``create_message()`` is handed to a ``QThreadPool`` worker and
its answer is delivered back through a queued signal. Everything else — the
whole loop, every ``Gateway.call_tool()``, every transcript write — happens in
that slot, on the thread that received it. The worker never touches the
gateway, the engine or the transcript.

That is not a convenience: the engine's single-hardware-thread invariant is
what makes this design safe on a GPIB bus, and an assistant that called into
the gateway from a pool thread would be a second writer to the instrument.
The rule is enforced by construction (the worker is given a client, a prompt
and nothing else) and asserted in ``tests/test_assistant.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from cryosoft.session.assistant.transcript import AssistantTranscript
from cryosoft.session.eln.drafting import COST_FIELDS, cost_usd
from cryosoft.session.eln.settings import AssistantSettings
from cryosoft.session.gateway.gateway import Gateway

logger = logging.getLogger(__name__)

__all__ = [
    "ASSISTANT_SYSTEM_PROMPT",
    "STATUS_IDLE",
    "STATUS_THINKING",
    "STATUS_CALLING",
    "STATUS_REFUSED",
    "AssistantError",
    "AssistantRuntime",
    "ChatClient",
    "ChatResult",
    "ToolCall",
    "empty_cost_line",
]

#: The chip states the runtime publishes, and the whole set of them. A client
#: renders these; it never invents a fifth.
STATUS_IDLE = "idle"
STATUS_THINKING = "thinking"
STATUS_CALLING = "calling"
STATUS_REFUSED = "refused"

#: Most model calls one question may spend before the runtime stops asking.
#: A cap rather than a timeout: the loop is bounded by how many times the
#: assistant may go round, which is the quantity a physicist can reason about
#: and the one a runaway conversation actually exhausts.
DEFAULT_MAX_STEPS = 8

#: The system half of the assistant prompt standard. A constant, so a change
#: to what the assistant is allowed to claim is a change to this file and
#: shows up in review.
ASSISTANT_SYSTEM_PROMPT = (
    "You are CryoSoft's embedded assistant. You are talking to the physicist "
    "running one cryostat experiment, in the application window that is "
    "driving it right now.\n"
    "\n"
    "What you are:\n"
    "- A client of this experiment's agent gateway. You read the station and "
    "act on it ONLY through the tools you were given; you hold no instrument "
    "and you have no other way in.\n"
    "- Acting under one declared role. The role, the session envelope, "
    "whether a human is attending, and the kill switch all bound what you may "
    "do, and they are enforced outside you. You cannot widen any of them, and "
    "you must never try.\n"
    "\n"
    "Rules:\n"
    "- Read before you act. Call read_status and the declaration reads to see "
    "what the station is actually doing, rather than assuming.\n"
    "- Probe before you run. Call validate_run to ask whether a run is "
    "admissible and how long it would take, and probe_run for the short "
    "version, BEFORE proposing the full run. Never dispatch a full run that "
    "has not been validated.\n"
    "- Every tool call is answered. An answer carries 'ok', a 'code', a "
    "'reason' and a 'detail' naming the rule that decided it.\n"
    "- Quote a refusal verbatim. When a call is refused, tell the physicist "
    "the reason exactly as it was given, and name the rule from the detail. "
    "Do not paraphrase it into 'I couldn't do that', and do not retry it in a "
    "different shape hoping it passes.\n"
    "- Never take over another actor's run. Aborting, finishing, confirming "
    "or skipping a step of a run somebody else started is refused unless you "
    "pass override_owner with a reason, and doing so is recorded as a "
    "takeover — so ask the physicist first, and never override without "
    "stating plainly why.\n"
    "- Never claim an action you did not get an OK verdict for. If the code "
    "is not OK, the action did not happen. Say what was refused and why.\n"
    "- Report numbers as the tools gave them, in SI units, and never invent a "
    "reading, a limit or an instrument that is not in the answers you got.\n"
    "- If something looks unsafe or wrong, say so plainly and first."
)


class AssistantError(RuntimeError):
    """One model call could not be made, or could not be answered.

    The **Chat client** contract's single exception type, mirroring the way an
    **ELN adapter** and the **Draft client** each map every vendor failure to
    one type, so a caller has exactly one thing to catch whichever
    collaborator failed.
    """


def empty_cost_line(model: str = "") -> dict[str, Any]:
    """Return a zeroed **cost line**, in the four fields the trail records.

    Args:
        model: The model to name, or ``""`` before one has answered.

    Returns:
        ``{"model", "input_tokens", "output_tokens", "cost_usd"}`` — the same
        four fields, in the same order, that ``DraftEntry.cost_line()`` and
        the **Agent feed** already use, so one reader reads every cost this
        application reports. The key set is derived from ``COST_FIELDS``
        rather than restated, so a change there changes this too.
    """
    line: dict[str, Any] = dict.fromkeys(COST_FIELDS, 0)
    line["model"] = model
    line["cost_usd"] = 0.0
    return line


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for, as it will be routed to the gateway.

    Attributes:
        id: The vendor's identifier for this call, echoed back on the result
            so the model can match the two.
        name: The tool's name, which must be one of ``tool_schemas()``.
        args: The arguments, JSON-safe. Validated by the gateway against the
            **Tool spec**'s own schema, never here.
    """

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatResult:
    """One model answer: what it said, what it wants to call, what it cost.

    Attributes:
        text_blocks: The answer's text blocks, in order.
        tool_calls: The tools it asked for, in order. Empty means the turn is
            finished.
        model: The model that actually answered, as the vendor reported it —
            never the model that was asked for, so a substitution is visible
            in the cost line.
        input_tokens: Tokens the request consumed.
        output_tokens: Tokens the answer generated.
        stop_reason: Why the model stopped, as the vendor reported it.
        content: The answer's raw content blocks, JSON-safe, for echoing back
            unchanged on the next call. Optional: a client that cannot supply
            them leaves this empty and the runtime rebuilds an equivalent
            message from ``text_blocks`` and ``tool_calls``.
    """

    text_blocks: tuple[str, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    content: tuple[dict[str, Any], ...] = ()

    @property
    def text(self) -> str:
        """The answer's text blocks joined into one string."""
        return "\n".join(block for block in self.text_blocks if block).strip()


class ChatClient(Protocol):
    """The one model call this package makes — the **Chat client** contract.

    One synchronous method with no streaming: the loop, the tool routing and
    the accounting are the runtime's, so an implementation has exactly one job
    and a test double is a dozen lines.
    """

    def create_message(
        self,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> ChatResult:
        """Answer one conversation.

        Args:
            system: The system prompt.
            messages: The conversation so far, in the vendor's message shape.
            tools: The tool declarations — always exactly
                ``Gateway.tool_schemas()``.
            max_tokens: Cap on the generated tokens.

        Returns:
            The answer, its tool calls and its token counts.

        Raises:
            AssistantError: The model could not be reached, or refused.
        """
        ...


class _ChatSignals(QObject):
    """The queued hand-back from a pool thread to the runtime's own thread.

    A separate ``QObject`` because a ``QRunnable`` is not one and cannot carry
    a signal. Created on the runtime's thread and owned by it, so every
    emission from a worker crosses back as a queued connection — which is what
    makes the assistant's thread rule hold without a lock.

    Attributes:
        answered: One ``(ChatResult, token)`` per successful call.
        failed: One ``(message, token)`` per failure, already stringified so
            no vendor exception object crosses the boundary.
    """

    answered = pyqtSignal(object, int)
    failed = pyqtSignal(str, int)


class _ChatCall(QRunnable):
    """One ``create_message()`` on a pool thread, and nothing else.

    Deliberately given a client, a prompt and a token — no gateway, no
    transcript, no engine. The assistant's thread rule is enforced by what
    this object is handed, not by a convention about what it does with it.
    """

    def __init__(
        self,
        client: ChatClient,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        signals: _ChatSignals,
        token: int,
    ) -> None:
        """Capture everything the call needs, copied, before it leaves the thread.

        Args:
            client: The **Chat client** to ask.
            system: The system prompt.
            messages: The conversation so far (copied by the caller).
            tools: The tool declarations (copied by the caller).
            max_tokens: Cap on the generated tokens.
            signals: The hand-back object living on the runtime's thread.
            token: The turn token this call belongs to; a stale one is
                discarded on arrival.
        """
        super().__init__()
        self._client = client
        self._system = system
        self._messages = messages
        self._tools = tools
        self._max_tokens = max_tokens
        self._signals = signals
        self._token = token

    def run(self) -> None:  # noqa: D102 — Qt override, documented on the class
        try:
            result = self._client.create_message(
                self._system, self._messages, self._tools, self._max_tokens
            )
        except Exception as error:  # noqa: BLE001 — a worker never raises into Qt
            self._deliver(answered=False, payload=str(error))
            return
        self._deliver(answered=True, payload=result)

    def _deliver(self, *, answered: bool, payload: Any) -> None:
        """Hand one answer back, tolerating a runtime that has gone away.

        A model call outlives a runtime that is destroyed during it — the
        window closes, the application quits, a test tears its objects down —
        and a pool thread that so much as READS a signal off a destroyed
        ``QObject`` raises, which is why the lookup is inside the guard and not
        at the call site. There is nothing to report to and nothing to recover,
        so the answer is dropped with a DEBUG line rather than raised on a
        thread that has no handler for it.

        Args:
            answered: Whether the call produced a result or a failure.
            payload: The answer, or the failure message.
        """
        try:
            signal = self._signals.answered if answered else self._signals.failed
            signal.emit(payload, self._token)
        except RuntimeError:
            logger.debug(
                "Assistant: the runtime went away before its answer arrived"
            )


class AssistantRuntime(QObject):
    """The **Embedded assistant**: one conversation, one gateway, one loop.

    Ask it a question with ``ask()``; it runs the tool-use loop until the model
    answers without asking for another tool, the step cap is reached, or
    ``stop()`` cancels it between steps. Every model call goes to a pool
    thread; every tool call, transcript write and signal emission happens on
    this object's own thread.

    Signals:
        status_changed(str, str): ``(status, detail)`` — one of
            ``STATUS_IDLE``/``STATUS_THINKING``/``STATUS_CALLING``/
            ``STATUS_REFUSED``, with the tool's name as the detail while
            calling and the refusing rule while refused.
        message_added(dict): One transcript record, exactly as written to the
            **Assistant transcript**, so a window renders the evidence rather
            than a second rendering of it.
        cost_changed(dict, dict): ``(this turn, this session)`` — two **cost
            lines** in the four standard fields.
        turn_finished(str): The assistant's final text for this turn.
        failed(str): The turn ended because the model could not be reached.

    Attributes:
        role: The **Role** the gateway is currently connected under.
    """

    status_changed = pyqtSignal(str, str)
    message_added = pyqtSignal(dict)
    cost_changed = pyqtSignal(dict, dict)
    turn_finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        gateway: Gateway,
        client: ChatClient,
        *,
        settings: AssistantSettings | None = None,
        transcript: AssistantTranscript | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        system_prompt: str = ASSISTANT_SYSTEM_PROMPT,
        parent: QObject | None = None,
    ) -> None:
        """Wire one assistant to one gateway and one model.

        Args:
            gateway: The **Agent gateway** connection whose tools are the
                assistant's tools and whose ``call_tool()`` executes every one
                of them. Its role, its feed and its permission checks are the
                assistant's; nothing here adds to them.
            client: The **Chat client** to ask.
            settings: The assistant settings supplying the token cap and the
                price table the cost line is computed from. ``None`` uses the
                defaults.
            transcript: The **Assistant transcript** to write to, or ``None``
                to keep no evidence file (a test, or a session with no
                experiment open — the conversation still runs).
            max_steps: Most model calls one question may spend.
            system_prompt: The system half of the prompt. Defaults to the
                standard's own constant; a caller replaces it only
                deliberately.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._gateway = gateway
        self._client = client
        self._settings = settings or AssistantSettings()
        self._transcript = transcript
        self._max_steps = max(int(max_steps), 1)
        self._system_prompt = system_prompt

        self._messages: list[dict[str, Any]] = []
        self._turn = 0
        self._step = 0
        self._token = 0
        self._busy = False
        self._turn_cost = empty_cost_line()
        self._session_cost = empty_cost_line()

        self._signals = _ChatSignals(self)
        self._signals.answered.connect(self._on_answered)
        self._signals.failed.connect(self._on_failed)
        self._pool = QThreadPool.globalInstance()
        logger.info(
            "Assistant runtime ready: role %r, %d tools",
            self._gateway.role.value,
            len(self._gateway.tools()),
        )

    # ── What the client is connected as ───────────────────────────────

    @property
    def role(self) -> str:
        """The **Role** the gateway is currently connected under."""
        return self._gateway.role.value

    @property
    def gateway(self) -> Gateway:
        """The gateway this assistant acts through."""
        return self._gateway

    def set_gateway(self, gateway: Gateway) -> bool:
        """Swap the connection — how the role selector changes authority.

        A ``Gateway``'s role is fixed at its construction (that is what makes
        an actor's authority a property of the connection rather than a
        mutable field), so changing role means connecting again. Refused
        mid-turn: a conversation half of which ran under one authority and
        half under another would make the transcript unreadable as evidence.

        Args:
            gateway: The new connection, built by whoever owns the engine.

        Returns:
            ``True`` when the swap happened, ``False`` when a turn is in
            flight and it was refused.
        """
        if self._busy:
            logger.warning("Assistant: refusing to change role mid-turn")
            return False
        self._gateway = gateway
        logger.info("Assistant reconnected under role %r", gateway.role.value)
        return True

    # ── Accounting ────────────────────────────────────────────────────

    def turn_cost(self) -> dict[str, Any]:
        """Return the **cost line** of the current (or last) turn.

        Returns:
            ``{"model", "input_tokens", "output_tokens", "cost_usd"}``.
        """
        return dict(self._turn_cost)

    def session_cost(self) -> dict[str, Any]:
        """Return the **cost line** of every turn this runtime has run.

        Returns:
            ``{"model", "input_tokens", "output_tokens", "cost_usd"}``.
        """
        return dict(self._session_cost)

    def is_busy(self) -> bool:
        """Whether a turn is in flight."""
        return self._busy

    # ── The loop ──────────────────────────────────────────────────────

    def ask(self, text: str) -> bool:
        """Put one question to the assistant and start its turn.

        Args:
            text: The physicist's question, verbatim.

        Returns:
            ``True`` when the turn started, ``False`` when the text was empty
            or a turn was already in flight.
        """
        question = str(text).strip()
        if not question:
            return False
        if self._busy:
            logger.warning("Assistant: a turn is already in flight")
            return False

        self._turn += 1
        self._step = 0
        self._busy = True
        self._token += 1
        self._turn_cost = empty_cost_line(self._turn_cost["model"])
        self._messages.append(
            {"role": "user", "content": [{"type": "text", "text": question}]}
        )
        self._record_user(question)
        self._emit_cost()
        self._dispatch()
        return True

    def stop(self) -> bool:
        """Cancel the turn between steps.

        The in-flight model call is not aborted — it is network I/O on a pool
        thread and nothing is gained by tearing it down — but its answer is
        discarded on arrival, no tool is called for it, and no further model
        call is made. The conversation keeps every message up to the last
        completed step, so the next question continues from a coherent state.

        Returns:
            ``True`` when a turn was cancelled, ``False`` when none was
            running.
        """
        if not self._busy:
            return False
        logger.info("Assistant: turn cancelled by the operator")
        self._token += 1
        self._finish("")
        return True

    def _dispatch(self) -> None:
        """Hand one ``create_message()`` to a pool thread.

        The tools sent are ``Gateway.tool_schemas()``, read afresh every step:
        an instrument that connects mid-conversation brings its capability
        tools with it, and one that disconnects takes them away, with nothing
        to be told twice.
        """
        if self._step >= self._max_steps:
            logger.warning(
                "Assistant: step cap of %d reached; ending the turn",
                self._max_steps,
            )
            self._finish(
                f"I stopped after {self._max_steps} steps without reaching an "
                f"answer. Ask me again with a narrower question."
            )
            return
        self._step += 1
        self._set_status(STATUS_THINKING, "")
        self._pool.start(
            _ChatCall(
                self._client,
                self._system_prompt,
                [dict(message) for message in self._messages],
                self._gateway.tool_schemas(),
                int(self._settings.max_tokens),
                self._signals,
                self._token,
            )
        )

    def _on_answered(self, result: object, token: int) -> None:
        """Take one model answer and either finish the turn or call its tools.

        Runs on this object's own thread — the GUI thread in the application —
        which is what makes every ``call_tool()`` below happen there too.

        Args:
            result: The ``ChatResult`` the worker produced.
            token: The turn token it was dispatched under; a stale one belongs
                to a turn that was cancelled and is dropped.
        """
        if token != self._token or not isinstance(result, ChatResult):
            return
        self._add_cost(result)
        self._messages.append(
            {"role": "assistant", "content": self._assistant_content(result)}
        )
        self._record_assistant(result)

        if not result.tool_calls:
            self._finish(result.text)
            return

        blocks: list[dict[str, Any]] = []
        for call in result.tool_calls:
            blocks.append(self._call_one_tool(call))
        self._messages.append({"role": "user", "content": blocks})
        self._dispatch()

    def _call_one_tool(self, call: ToolCall) -> dict[str, Any]:
        """Route one tool call through the gateway and shape its answer.

        The gateway answers every call and raises at none, so there is no
        error path here: a refusal is DATA the model must read and quote, not
        an exception, which is why the result block is never marked as an
        error. What the gateway decided — the code, the reason and the rule —
        travels back verbatim.

        Args:
            call: The tool the model asked for.

        Returns:
            The ``tool_result`` block to send back with the next request.
        """
        self._set_status(STATUS_CALLING, call.name)
        answer = self._gateway.call_tool(call.name, call.args)
        self._record_tool(call, answer)
        if not answer.get("ok", False):
            rule = str((answer.get("detail") or {}).get("rule", ""))
            self._set_status(STATUS_REFUSED, rule or str(answer.get("code", "")))
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": json.dumps(answer, ensure_ascii=False, sort_keys=True),
        }

    def _on_failed(self, message: str, token: int) -> None:
        """End the turn because the model could not be reached.

        Args:
            message: What went wrong, already stringified by the worker.
            token: The turn token it was dispatched under.
        """
        if token != self._token:
            return
        logger.error("Assistant: the model could not be reached: %s", message)
        self._finish("")
        self.failed.emit(message)

    def _finish(self, text: str) -> None:
        """Close the turn out, whatever ended it.

        Args:
            text: The assistant's final text, or ``""`` when the turn was
                cancelled or failed.
        """
        self._busy = False
        self._set_status(STATUS_IDLE, "")
        self._emit_cost()
        self.turn_finished.emit(text)

    # ── Message shaping ───────────────────────────────────────────────

    @staticmethod
    def _assistant_content(result: ChatResult) -> list[dict[str, Any]]:
        """Return the assistant message to append for one answer.

        The vendor's own blocks when the client supplied them, so anything the
        model needs to see again (a reasoning block, a signature) travels back
        unchanged. Otherwise an equivalent message is rebuilt from the text
        and the tool calls, which is what a test double and any client that
        cannot serialise its blocks give.

        Args:
            result: The answer to shape.

        Returns:
            The content blocks of one assistant message.
        """
        if result.content:
            return [dict(block) for block in result.content]
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": text} for text in result.text_blocks if text
        ]
        blocks.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": dict(call.args),
            }
            for call in result.tool_calls
        )
        return blocks or [{"type": "text", "text": ""}]

    # ── Accounting, recording, reporting ──────────────────────────────

    def _add_cost(self, result: ChatResult) -> dict[str, Any]:
        """Add one model call to the turn and session totals.

        Args:
            result: The answer whose tokens were spent.

        Returns:
            This one call's own **cost line**.
        """
        line = {
            "model": result.model,
            "input_tokens": int(result.input_tokens),
            "output_tokens": int(result.output_tokens),
            "cost_usd": cost_usd(
                result.model,
                int(result.input_tokens),
                int(result.output_tokens),
                self._settings.prices,
            ),
        }
        for total in (self._turn_cost, self._session_cost):
            total["model"] = line["model"] or total["model"]
            total["input_tokens"] += line["input_tokens"]
            total["output_tokens"] += line["output_tokens"]
            total["cost_usd"] += line["cost_usd"]
        self._emit_cost()
        return line

    def _emit_cost(self) -> None:
        """Publish the two totals, this turn and this session."""
        self.cost_changed.emit(self.turn_cost(), self.session_cost())

    def _set_status(self, status: str, detail: str) -> None:
        """Publish the chip state.

        Args:
            status: One of the four ``STATUS_*`` values.
            detail: The tool's name while calling, the refusing rule while
                refused, ``""`` otherwise.
        """
        self.status_changed.emit(status, detail)

    def _record_user(self, text: str) -> None:
        """Write and publish the physicist's question.

        Args:
            text: The question, verbatim.
        """
        record = (
            self._transcript.record_user(self._turn, self.role, text)
            if self._transcript is not None
            else self._offline_record("user", text=text)
        )
        self.message_added.emit(record)

    def _record_assistant(self, result: ChatResult) -> None:
        """Write and publish one model answer.

        Args:
            result: The answer, whose cost line is recorded beside its text.
        """
        line = {
            "model": result.model,
            "input_tokens": int(result.input_tokens),
            "output_tokens": int(result.output_tokens),
            "cost_usd": cost_usd(
                result.model,
                int(result.input_tokens),
                int(result.output_tokens),
                self._settings.prices,
            ),
        }
        record = (
            self._transcript.record_assistant(
                self._turn, self.role, result.text, line, result.stop_reason
            )
            if self._transcript is not None
            else self._offline_record("assistant", text=result.text, cost=line)
        )
        self.message_added.emit(record)

    def _record_tool(self, call: ToolCall, answer: Mapping[str, Any]) -> None:
        """Write and publish one tool call and its answer.

        Args:
            call: The tool the model asked for.
            answer: The gateway's answer dict.
        """
        record = (
            self._transcript.record_tool(
                self._turn, self.role, call.name, call.args, dict(answer)
            )
            if self._transcript is not None
            else self._offline_record(
                "tool",
                tool=call.name,
                args=dict(call.args),
                verdict={
                    "code": str(answer.get("code", "")),
                    "reason": str(answer.get("reason", "")),
                },
                detail=dict(answer.get("detail") or {}) or None,
            )
        )
        self.message_added.emit(record)

    def _offline_record(self, record: str, **fields: Any) -> dict[str, Any]:
        """Return a transcript-shaped record for a runtime with no transcript.

        Same keys, same defaults, so a window renders one shape whether or not
        an experiment is open to write the evidence into.

        Args:
            record: The record kind.
            **fields: The fields this kind carries.

        Returns:
            A JSON-safe record dict.
        """
        payload: dict[str, Any] = {
            "schema": 0,
            "ts": 0.0,
            "seq": 0,
            "experiment_id": "",
            "turn": self._turn,
            "record": record,
            "role": self.role,
            "text": None,
            "tool": None,
            "args": None,
            "verdict": None,
            "detail": None,
            "cost": None,
        }
        payload.update(fields)
        return payload
