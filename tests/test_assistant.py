# ---
# description: |
#   Tests for the embedded assistant (session/assistant/). Covers the tool-use
#   loop over a real Gateway on a real simulated station — read_status,
#   validate_run, probe_run, a refusal quoted verbatim — the Assistant
#   transcript's record standard, the accumulating cost line, cancellation
#   between steps, and the assistant's thread rule (model calls off the
#   caller's thread, tool calls on it).
# last_updated: 2026-09-03
# ---

from __future__ import annotations

import json
import threading

import pytest

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.session.assistant import (
    ASSISTANT_SYSTEM_PROMPT,
    STATUS_CALLING,
    STATUS_IDLE,
    STATUS_REFUSED,
    STATUS_THINKING,
    AssistantError,
    AssistantRuntime,
    AssistantTranscript,
    ChatResult,
    FakeChatClient,
    ToolCall,
    read_transcript,
)
from cryosoft.session.assistant.clients import AnthropicChatClient
from cryosoft.session.gateway import Gateway, Role, ToolContext
from cryosoft.session.manager import ExperimentManager
from cryosoft.session.models import User
from cryosoft.session.store import ExperimentStore, UserRoster

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {"sample_name": "S", "sample_id": "S-1", "comments": ""}

FULL_PARAMS = {
    "measurement_vi": "dc_measurement",
    "field_start": -1.0,
    "field_end": 1.0,
    "field_steps": 5,
    "temperature": 300.0,
    "current_A": 1e-6,
    "readings_per_point": 50,
    "init_wait": 300.0,
    "step_wait": 30.0,
}


# ══════════════════════════════════════════════════════════════════════════
# Fixtures — a real engine, a real experiment, real gateways
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def bench(qtbot, tmp_path):
    """A real Orchestrator, experiment manager and store over a sim station."""
    station = build_station(CONFIG_PATH)
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []
    orch = Orchestrator(
        station, tick_interval_ms=10, run_catalog={"FieldSweep": FieldSweep}
    )
    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe", email="jdoe@example.org"))
    store = ExperimentStore(tmp_path / "experiments")
    manager = ExperimentManager(
        store=store,
        roster=roster,
        orchestrator=orch,
        config_name="sim_cryostat",
        station=station,
        run_catalog={"FieldSweep": FieldSweep},
    )
    manager.start_experiment("Assistant", "jdoe", dict(SAMPLE_INFO))
    yield station, orch, manager, tmp_path
    orch.shutdown()


def _gateway(bench, role=Role.SESSION):
    """Build one gateway over the bench's engine under *role*."""
    station, orch, manager, tmp_path = bench
    return Gateway(
        orch,
        role,
        "assistant",
        station_info=station.station_info,
        tool_context=ToolContext(
            experiments=manager,
            run_catalog={"FieldSweep": FieldSweep},
            status_log_path=tmp_path / "status.jsonl",
        ),
    )


def _transcript(bench):
    """The transcript file of the bench's open experiment."""
    _station, _orch, manager, _tmp = bench
    experiment_id = manager.current_experiment().experiment_id
    return AssistantTranscript(
        manager.store.assistant_transcript_path(experiment_id), experiment_id
    )


def _run_turn(qtbot, runtime, question):
    """Ask one question and wait for the turn to end.

    Args:
        qtbot: The pytest-qt bot.
        runtime: The ``AssistantRuntime`` under test.
        question: What to ask.

    Returns:
        The final text the turn ended with.
    """
    with qtbot.waitSignal(runtime.turn_finished, timeout=5000) as blocker:
        assert runtime.ask(question) is True
    return blocker.args[0]


def _tool_use(call_id, name, args=None):
    """Return a ``ChatResult`` asking for exactly one tool."""
    return ChatResult(
        text_blocks=(),
        tool_calls=(ToolCall(id=call_id, name=name, args=dict(args or {})),),
        model="claude-opus-5",
        input_tokens=100,
        output_tokens=10,
        stop_reason="tool_use",
    )


# ══════════════════════════════════════════════════════════════════════════
# The system-prompt standard
# ══════════════════════════════════════════════════════════════════════════


def test_the_system_prompt_carries_the_four_load_bearing_rules():
    """A change to what the assistant may claim is a change to this constant."""
    prompt = ASSISTANT_SYSTEM_PROMPT.lower()

    assert "validate_run" in prompt and "probe_run" in prompt
    assert "verbatim" in prompt
    assert "ok verdict" in prompt
    assert "read_status" in prompt


# ══════════════════════════════════════════════════════════════════════════
# The loop: the tools ARE the gateway's, the execution IS call_tool()
# ══════════════════════════════════════════════════════════════════════════


def test_a_scripted_conversation_probes_before_it_runs(qtbot, bench, tmp_path):
    """read_status, then validate_run, then probe_run — all through the gateway.

    The whole loop in one test: three tool calls answered by a real gateway
    over a real engine, each verdict fed back to the model, and the final text
    naming the verdict it got.
    """
    _station, orch, manager, _tmp = bench
    orch.start_monitoring()
    gateway = _gateway(bench)
    transcript = _transcript(bench)
    data_dir = str(manager.current_data_dir())
    probe_args = {
        "procedure": "FieldSweep",
        "params": dict(FULL_PARAMS),
        "sample_info": dict(SAMPLE_INFO),
        "data_directory": data_dir,
        "file_prefix": "probe",
        "probe_spec": {"n_points": 3, "averaging": 2, "max_wait_s": 0.0},
    }
    client = FakeChatClient(
        replies=[
            _tool_use("c1", "read_status"),
            _tool_use(
                "c2",
                "validate_run",
                {
                    "procedure": "FieldSweep",
                    "params": dict(FULL_PARAMS),
                    "sample_info": dict(SAMPLE_INFO),
                    "data_directory": data_dir,
                },
            ),
            _tool_use("c3", "probe_run", probe_args),
            ChatResult(
                text_blocks=("The probe was accepted: the verdict was OK.",),
                model="claude-opus-5",
                input_tokens=400,
                output_tokens=20,
                stop_reason="end_turn",
            ),
        ]
    )
    runtime = AssistantRuntime(gateway, client, transcript=transcript)

    final = _run_turn(qtbot, runtime, "Is this sweep safe to run?")

    assert "OK" in final
    # Four model calls: three tool rounds and the answer.
    assert len(client.requests) == 4
    # Every request offered exactly the gateway's own surface.
    for request in client.requests:
        assert list(request.tools) == gateway.tool_schemas()
        assert request.system == ASSISTANT_SYSTEM_PROMPT

    records = read_transcript(transcript.path)
    kinds = [record["record"] for record in records]
    assert kinds == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    tools_called = [r["tool"] for r in records if r["record"] == "tool"]
    assert tools_called == ["read_status", "validate_run", "probe_run"]
    assert [r["verdict"]["code"] for r in records if r["record"] == "tool"] == [
        "OK",
        "OK",
        "OK",
    ]
    # Every record carries every key of the standard, null where it does not
    # apply — a reader never has to guess what an absent key means.
    for record in records:
        assert set(record) == {
            "schema",
            "ts",
            "seq",
            "experiment_id",
            "turn",
            "record",
            "role",
            "text",
            "tool",
            "args",
            "verdict",
            "detail",
            "cost",
        }
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))


def test_a_refusal_reaches_the_model_verbatim(qtbot, bench):
    """An observer asking to run a procedure gets the structured reason back.

    The gateway refuses; the refusal travels into the conversation as data,
    not as an error, and the assistant's reply names it.
    """
    gateway = _gateway(bench, Role.OBSERVER)
    client = FakeChatClient(
        replies=[
            _tool_use(
                "c1",
                "run_procedure",
                {
                    "procedure": "FieldSweep",
                    "params": dict(FULL_PARAMS),
                    "sample_info": dict(SAMPLE_INFO),
                    "data_directory": ".",
                },
            ),
            ChatResult(
                text_blocks=(
                    "Refused: The 'observer' role does not grant run_control "
                    "actions, so 'run_procedure' is refused.",
                ),
                model="claude-opus-5",
                input_tokens=200,
                output_tokens=30,
                stop_reason="end_turn",
            ),
        ]
    )
    runtime = AssistantRuntime(gateway, client)
    refusals: list[tuple[str, str]] = []
    runtime.status_changed.connect(
        lambda status, detail: refusals.append((status, detail))
    )

    final = _run_turn(qtbot, runtime, "Start the sweep.")

    # What the model was actually shown: the gateway's own answer dict.
    tool_result = client.requests[1].messages[-1]["content"][0]
    answer = json.loads(tool_result["content"])
    assert answer["ok"] is False
    assert answer["code"] == "BLOCKED_ROLE"
    assert answer["detail"]["rule"] == "role_matrix"
    assert answer["detail"]["role"] == "observer"
    assert "does not grant run_control actions" in answer["reason"]
    # A refusal is data, not an error: nothing is flagged as one.
    assert "is_error" not in tool_result
    # And the reply quotes it.
    assert answer["reason"] in final
    assert (STATUS_REFUSED, "role_matrix") in refusals


def test_the_status_chip_walks_idle_thinking_calling_idle(qtbot, bench):
    """The four chip states are published in the order they happen."""
    gateway = _gateway(bench)
    client = FakeChatClient(
        replies=[
            _tool_use("c1", "read_status"),
            ChatResult(text_blocks=("Idle.",), model="claude-opus-5"),
        ]
    )
    runtime = AssistantRuntime(gateway, client)
    seen: list[tuple[str, str]] = []
    runtime.status_changed.connect(lambda status, detail: seen.append((status, detail)))

    _run_turn(qtbot, runtime, "What is the station doing?")

    assert seen == [
        (STATUS_THINKING, ""),
        (STATUS_CALLING, "read_status"),
        (STATUS_THINKING, ""),
        (STATUS_IDLE, ""),
    ]


# ══════════════════════════════════════════════════════════════════════════
# The assistant's thread rule
# ══════════════════════════════════════════════════════════════════════════


class _ThreadRecordingGateway(Gateway):
    """A gateway that remembers which thread each tool call ran on."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.call_threads: list[int] = []

    def call_tool(self, name, args=None):  # noqa: D102 — see the base class
        self.call_threads.append(threading.get_ident())
        return super().call_tool(name, args)


def test_the_model_call_leaves_the_thread_and_the_tool_call_does_not(
    qtbot, bench
):
    """The assistant's thread rule, asserted from both sides.

    ``create_message()`` runs on a pool thread — the engine is never touched
    from there — and every ``Gateway.call_tool()`` runs on the thread that
    received the answer, which in the application is the GUI thread that drives
    the tick.
    """
    station, orch, manager, tmp_path = bench
    gateway = _ThreadRecordingGateway(
        orch,
        Role.SESSION,
        "assistant",
        station_info=station.station_info,
        tool_context=ToolContext(
            experiments=manager, run_catalog={"FieldSweep": FieldSweep}
        ),
    )
    client = FakeChatClient(
        replies=[
            _tool_use("c1", "read_status"),
            ChatResult(text_blocks=("Idle.",), model="claude-opus-5"),
        ]
    )
    runtime = AssistantRuntime(gateway, client)
    caller = threading.get_ident()

    _run_turn(qtbot, runtime, "Status?")

    assert client.requests, "the model was never called"
    for request in client.requests:
        assert request.thread != caller
    assert gateway.call_threads == [caller]


# ══════════════════════════════════════════════════════════════════════════
# Cost, cancellation, bounds and failure
# ══════════════════════════════════════════════════════════════════════════


def test_the_cost_line_accumulates_over_the_turn_and_the_session(qtbot, bench):
    """Two turns, four model calls: the turn resets, the session does not."""
    gateway = _gateway(bench)
    client = FakeChatClient(
        replies=[
            _tool_use("c1", "read_status"),
            ChatResult(
                text_blocks=("Idle.",),
                model="claude-opus-5",
                input_tokens=1000,
                output_tokens=100,
                stop_reason="end_turn",
            ),
        ]
    )
    runtime = AssistantRuntime(gateway, client)

    _run_turn(qtbot, runtime, "Status?")
    first_turn = runtime.turn_cost()
    first_session = runtime.session_cost()

    assert first_turn["input_tokens"] == 1100  # 100 for the tool round + 1000
    assert first_turn["output_tokens"] == 110
    assert first_turn["model"] == "claude-opus-5"
    # Priced off the settings' own table, never guessed.
    assert first_turn["cost_usd"] == pytest.approx(
        1100 * 5.0 / 1e6 + 110 * 25.0 / 1e6
    )
    assert first_session == first_turn

    # The script is exhausted, so the second turn is one call and no tool: the
    # turn total resets to it, the session total keeps both.
    _run_turn(qtbot, runtime, "And now?")

    assert runtime.turn_cost()["input_tokens"] == 1000
    assert runtime.session_cost()["input_tokens"] == 2100
    assert runtime.session_cost()["output_tokens"] == 210


def test_an_unpriced_model_reports_no_cost_rather_than_a_guess(qtbot, bench):
    """The price table is config; a model with no row costs 0.0 and warns."""
    gateway = _gateway(bench)
    client = FakeChatClient(
        replies=[
            ChatResult(
                text_blocks=("Hello.",),
                model="some-unpriced-model",
                input_tokens=10_000,
                output_tokens=1_000,
            )
        ]
    )
    runtime = AssistantRuntime(gateway, client)

    _run_turn(qtbot, runtime, "Hello?")

    assert runtime.session_cost()["cost_usd"] == 0.0
    assert runtime.session_cost()["input_tokens"] == 10_000


def test_stop_cancels_between_steps(qtbot, bench):
    """Stop discards the in-flight answer: no tool runs, no second call."""
    gateway = _ThreadRecordingGateway(
        bench[1],
        Role.SESSION,
        "assistant",
        station_info=bench[0].station_info,
    )
    client = FakeChatClient(
        replies=[
            _tool_use("c1", "read_status"),
            ChatResult(text_blocks=("Idle.",), model="claude-opus-5"),
        ]
    )
    runtime = AssistantRuntime(gateway, client)

    with qtbot.waitSignal(runtime.turn_finished, timeout=5000):
        assert runtime.ask("Status?") is True
        assert runtime.stop() is True

    assert runtime.is_busy() is False
    assert runtime.stop() is False
    # The in-flight answer arrives and is dropped: no tool ever ran, and the
    # loop never went round again.
    qtbot.wait(100)
    assert gateway.call_threads == []
    assert len(client.requests) == 1


def test_the_turn_is_bounded_by_a_step_cap(qtbot, bench):
    """A model that only ever asks for tools stops after the cap, saying so."""
    gateway = _gateway(bench)
    client = FakeChatClient(replies=[_tool_use("c1", "read_status")])
    runtime = AssistantRuntime(gateway, client, max_steps=3)

    final = _run_turn(qtbot, runtime, "Loop forever.")

    assert "3 steps" in final
    assert len(client.requests) == 3


def test_an_unreachable_model_ends_the_turn_without_raising(qtbot, bench):
    """The one failure mode that matters, answered as a signal not a crash."""
    gateway = _gateway(bench)
    runtime = AssistantRuntime(gateway, FakeChatClient(offline=True))

    with qtbot.waitSignal(runtime.failed, timeout=5000) as blocker:
        runtime.ask("Status?")

    assert "offline" in blocker.args[0]
    assert runtime.is_busy() is False


def test_a_second_question_is_refused_while_a_turn_is_in_flight(qtbot, bench):
    """One conversation at a time; an empty question is not a question."""
    gateway = _gateway(bench)
    client = FakeChatClient(replies=[ChatResult(text_blocks=("Hi.",))])
    runtime = AssistantRuntime(gateway, client)

    assert runtime.ask("   ") is False
    with qtbot.waitSignal(runtime.turn_finished, timeout=5000):
        assert runtime.ask("Status?") is True
        assert runtime.ask("Again?") is False


def test_the_role_cannot_change_mid_turn(qtbot, bench):
    """A transcript half under one authority and half under another is not evidence."""
    gateway = _gateway(bench, Role.OBSERVER)
    client = FakeChatClient(replies=[ChatResult(text_blocks=("Hi.",))])
    runtime = AssistantRuntime(gateway, client)
    assert runtime.role == "observer"

    with qtbot.waitSignal(runtime.turn_finished, timeout=5000):
        runtime.ask("Status?")
        assert runtime.set_gateway(_gateway(bench, Role.SESSION)) is False

    assert runtime.set_gateway(_gateway(bench, Role.SESSION)) is True
    assert runtime.role == "session"


# ══════════════════════════════════════════════════════════════════════════
# The transcript, standing alone
# ══════════════════════════════════════════════════════════════════════════


def test_the_transcript_continues_a_file_an_earlier_process_started(tmp_path):
    """The sequence picks up where it left off, so the trail stays orderable."""
    path = tmp_path / "assistant_transcript.jsonl"
    first = AssistantTranscript(path, "exp-1")
    first.record_user(1, "session", "Hello")
    first.record_assistant(1, "session", "Hi", {"model": "m"}, "end_turn")

    second = AssistantTranscript(path, "exp-1")
    second.record_user(2, "session", "Again")

    assert [record["seq"] for record in read_transcript(path)] == [1, 2, 3]
    assert read_transcript(path, since_seq=2) == read_transcript(path)[2:]


def test_a_corrupt_line_never_strands_the_rest_of_the_conversation(tmp_path):
    """One mangled line is skipped with a warning, like every other journal."""
    path = tmp_path / "assistant_transcript.jsonl"
    transcript = AssistantTranscript(path, "exp-1")
    transcript.record_user(1, "session", "Hello")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    transcript.record_user(2, "session", "Still here")

    records = read_transcript(path)

    assert [record["text"] for record in records] == ["Hello", "Still here"]


def test_recording_never_raises_at_its_caller(tmp_path):
    """A transcript that cannot be written must not swallow the answer."""
    blocked = tmp_path / "file.txt"
    blocked.write_text("not a directory", encoding="utf-8")
    transcript = AssistantTranscript(blocked / "assistant_transcript.jsonl", "exp-1")

    record = transcript.record_user(1, "session", "Hello")

    assert record["text"] == "Hello"


# ══════════════════════════════════════════════════════════════════════════
# The real client, without the optional extra installed
# ══════════════════════════════════════════════════════════════════════════


def test_the_vendor_client_names_the_command_that_installs_it(monkeypatch):
    """A missing optional dependency is one clear error, never a stack trace."""
    import builtins

    real_import = builtins.__import__

    def _no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_anthropic)

    with pytest.raises(AssistantError, match=r"cryosoft\[assistant\]"):
        AnthropicChatClient()
