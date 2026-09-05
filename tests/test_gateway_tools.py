# ---
# description: |
#   Tests for the agent gateway's tool surface (session/gateway/tools.py).
#   Covers rendering command tools from CommandName + the Orchestrator's
#   docstrings, capability tools from the station declaration's ParamSpecs and
#   configured limits, the schema validator that names a violated bound, and
#   Gateway.call_tool()/tool_schemas() routing — command tools through
#   submit(), session tools to their functions, everything answered and
#   nothing raised.
# last_updated: 2026-09-03
# ---

from __future__ import annotations

import json

import pytest

from cryosoft.core import events as ev
from cryosoft.core.data_reader import list_columns, open_run, summary_stats
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.session.gateway import (
    ActionClass,
    Gateway,
    Role,
    ToolContext,
    ToolSpec,
    capability_tool_name,
    render_command_tools,
    render_tools,
    validate_tool_args,
)
from cryosoft.session.manager import ExperimentManager
from cryosoft.session.models import User
from cryosoft.session.store import ExperimentStore, UserRoster

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {"sample_name": "S", "sample_id": "S-1", "comments": ""}

FULL_PARAMS = {
    "measurement_vi": "keithley_delta_mode",
    "field_start": -1.0,
    "field_end": 1.0,
    "field_steps": 21,
    "temperature": 300.0,
    "current": 1e-6,
    "n_readings": 50,
    "init_wait": 300.0,
    "step_wait": 30.0,
}


@pytest.fixture(scope="module")
def station_info():
    """The declaration snapshot of a real simulated station."""
    return build_station(CONFIG_PATH).station_info()


@pytest.fixture(scope="module")
def tools(station_info):
    """The rendered tool surface, keyed by name."""
    return {tool.name: tool for tool in render_tools(station_info)}


# ══════════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════════


def test_every_command_tool_is_rendered_from_the_engines_own_docstring(tools):
    """The text an agent reads is the text a reader of the code reads."""
    pause = tools["pause_procedure"]

    assert pause.command is ev.CommandName.PAUSE_PROCEDURE
    assert pause.action_class is ActionClass.RECOVERY
    assert pause.description == (
        "Pause the run, holding the hardware — but never mid-datapoint."
    )
    assert pause.input_schema == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_a_signature_becomes_a_schema(tools):
    """Scalar parameters render straight from the signature, with their docstring."""
    schema = tools["set_scanner_enabled"].input_schema

    assert schema["properties"]["enabled"]["type"] == "boolean"
    assert schema["properties"]["enabled"]["description"]
    assert schema["required"] == ["enabled"]
    assert tools["stop_ramp"].input_schema["properties"]["vi_name"]["type"] == "string"


def test_a_translated_command_renders_its_wire_arguments(tools):
    """The four commands whose JSON args the engine translates say so, once."""
    run = tools["run_procedure"].input_schema

    assert run["required"] == ["procedure"]
    assert run["properties"]["procedure"]["type"] == "string"
    assert run["properties"]["params"]["type"] == "object"
    assert run["properties"]["probe_spec"]["properties"]["n_points"]["minimum"] == 1
    assert tools["set_experiment_envelope"].input_schema["properties"]["envelope"][
        "type"
    ] == ["object", "null"]


def test_an_enum_parameter_renders_its_members(tools):
    """The kill switch's setting travels as its AgentGate value, and says which."""
    state = tools["set_agent_gate"].input_schema["properties"]["state"]

    assert state["enum"] == [member.value for member in ev.AgentGate]


def test_a_capability_tool_carries_its_units_bounds_and_rationale(tools):
    """A ParamSpec plus the configured limit IS the schema."""
    tool = tools[capability_tool_name("magnet_z", "set_field")]

    assert tool.command is ev.CommandName.SUBMIT_VI_ACTION
    assert tool.fixed_args == {"vi_name": "magnet_z", "method_name": "set_field"}
    assert tool.instrument == "magnet_z"
    assert tool.capability == "set_field"
    assert "largest stored energy" in tool.description  # the classification's rationale

    target = tool.input_schema["properties"]["target_T"]
    assert target["type"] == "number"
    assert target["unit"] == "T"
    # The bound is the CONFIG's, not the declaration's: limits are a setup
    # property, so the tool publishes the number the setup enforces.
    assert (target["minimum"], target["maximum"]) == (-9.0, 9.0)
    assert "field_T" in target["description"]
    assert tool.input_schema["required"] == ["target_T"]


def test_a_choice_parameter_renders_its_values_and_labels(tools):
    """An enumerated ParamSpec becomes an enum an agent can pick from."""
    mode = tools[capability_tool_name("level_meter", "set_refresh_rate")].input_schema[
        "properties"
    ]["mode"]

    assert mode["type"] == "integer"
    assert mode["enum"] == [0, 1, 2]
    assert mode["choice_labels"]["Fast (helium fill)"] == 2


def test_a_default_the_configured_bound_refuses_is_not_published(tools):
    """A schema never offers a default its own bound would reject."""
    tool = tools[capability_tool_name("temperature_vti", "set_temperature")]
    target = tool.input_schema["properties"]["target_K"]

    assert target["minimum"] == 1.4
    assert "default" not in target


def test_a_read_capability_is_rendered_read_class(tools):
    """The one control that only reads keeps its class through the rendering."""
    assert (
        tools[capability_tool_name("keithley_dc_mode", "read_now")].action_class
        is ActionClass.READ
    )


def test_the_surface_is_json_safe_and_uniquely_named(tools):
    """Every tool survives a JSON round trip, and no two claim one name."""
    payload = json.dumps([tool.to_json() for tool in tools.values()])

    assert len(json.loads(payload)) == len(tools)
    assert len({tool.name for tool in tools.values()}) == len(tools)


def test_a_tool_is_a_command_or_a_session_function_never_both():
    """The routing is exclusive by construction, not by convention."""
    schema = {"type": "object", "properties": {}, "required": []}
    with pytest.raises(ValueError, match="exactly one"):
        ToolSpec(
            name="both",
            description="d",
            input_schema=schema,
            action_class=ActionClass.READ,
            command=ev.CommandName.RUN_QUEUE,
            session_function="read_status",
        )
    with pytest.raises(ValueError, match="exactly one"):
        ToolSpec(
            name="neither",
            description="d",
            input_schema=schema,
            action_class=ActionClass.READ,
        )


def test_command_tools_render_without_a_station():
    """The command half of the surface needs only the contract and the engine."""
    names = {tool.name for tool in render_command_tools()}

    assert names == {
        member.value
        for member in ev.CommandName
        if member is not ev.CommandName.SUBMIT_VI_ACTION
    }


# ══════════════════════════════════════════════════════════════════════════
# Argument validation
# ══════════════════════════════════════════════════════════════════════════


def test_a_bound_violation_names_the_bound_and_its_unit(tools):
    """What an agent has to read to correct itself."""
    schema = tools[capability_tool_name("magnet_z", "set_field")].input_schema

    errors = validate_tool_args({"target_T": 20.0}, schema)

    assert len(errors) == 1
    assert "9.0 T" in errors[0]
    assert "target_T" in errors[0]
    assert validate_tool_args({"target_T": 1.5}, schema) == []


def test_a_missing_or_unexpected_argument_is_refused(tools):
    """The schema is closed in both directions."""
    schema = tools[capability_tool_name("magnet_z", "set_field")].input_schema

    assert any("missing" in e for e in validate_tool_args({}, schema))
    assert any(
        "unexpected" in e
        for e in validate_tool_args({"target_T": 0.0, "rate": 1.0}, schema)
    )


def test_a_wrong_type_is_refused(tools):
    """A string where a number is declared never reaches the engine."""
    schema = tools[capability_tool_name("magnet_z", "set_field")].input_schema

    assert validate_tool_args({"target_T": "one tesla"}, schema)


# ══════════════════════════════════════════════════════════════════════════
# Gateway.call_tool — routing, refusals, and the schema export
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def engine(qtbot):
    """A real Orchestrator over a real simulated station."""
    station = build_station(CONFIG_PATH)
    orch = Orchestrator(
        station, tick_interval_ms=10, run_catalog={"FieldSweep": FieldSweep}
    )
    yield orch, station
    orch.shutdown()


def _gateway(engine, role, actor_id="agent-1", context=None):
    orch, station = engine
    return Gateway(
        orch,
        role,
        actor_id,
        station_info=station.station_info,
        tool_context=context,
    )


def test_tool_schemas_are_the_shape_a_tool_use_api_expects(engine):
    """Three keys, nothing else — so every client publishes the same list."""
    gateway = _gateway(engine, Role.SESSION)

    schemas = gateway.tool_schemas()

    assert schemas
    for schema in schemas:
        assert set(schema) == {"name", "description", "input_schema"}
        assert schema["input_schema"]["type"] == "object"


def test_an_unknown_tool_is_answered_never_raised(engine):
    """A client gets an answer to every call it makes."""
    gateway = _gateway(engine, Role.SESSION)

    answer = gateway.call_tool("no_such_tool", {"x": 1})

    assert answer["ok"] is False
    assert answer["code"] == "FAILED"
    assert answer["detail"]["rule"] == "unknown_tool"


def test_pausing_an_idle_engine_is_answered_blocked_state(engine):
    """The exit criterion: a permitted command the state machine still refuses."""
    gateway = _gateway(engine, Role.SESSION)

    answer = gateway.call_tool("pause_procedure")

    assert answer["ok"] is False
    assert answer["code"] == "BLOCKED_STATE"
    assert answer["request_id"]
    assert "no run is active" in answer["reason"]
    assert answer["verdict"]["command"] == "pause_procedure"


def test_an_out_of_range_capability_argument_is_refused_by_the_schema(engine):
    """Refused before a Command is ever built, with the bound named."""
    orch, _station = engine
    verdicts: list[ev.Verdict] = []
    orch.verdict_emitted.connect(verdicts.append)
    gateway = _gateway(engine, Role.SESSION)

    answer = gateway.call_tool(
        capability_tool_name("magnet_z", "set_field"), {"target_T": 20.0}
    )

    assert answer["ok"] is False
    assert answer["code"] == "FAILED"
    assert answer["detail"]["rule"] == "schema"
    assert "9.0 T" in answer["reason"]
    # Nothing was submitted: the refusal is the client's, before the engine.
    assert verdicts == []


def test_a_valid_capability_call_reaches_the_engine(engine):
    """The fixed args are supplied by the tool, not asked of the caller."""
    gateway = _gateway(engine, Role.SESSION)

    answer = gateway.call_tool(
        capability_tool_name("magnet_z", "set_field"), {"target_T": 0.05}
    )

    assert answer["code"] in {"OK", "PENDING"}
    assert answer["request_id"]


def test_an_observer_may_read_and_may_not_control(engine):
    """The exit criterion, over the whole rendered surface.

    Emergency standby is the one tool left out of the sweep, because it is
    the one action deliberately outside the permission matrix — the test
    below calls it on its own engine rather than shutting this one down
    halfway through the loop.
    """
    gateway = _gateway(engine, Role.OBSERVER)

    for tool in gateway.tools():
        if tool.command is ev.CommandName.EMERGENCY_STANDBY:
            continue
        answer = gateway.call_tool(tool.name, _minimal_args(tool))
        if tool.action_class is ActionClass.READ:
            assert answer["code"] != "BLOCKED_ROLE", tool.name
        else:
            assert answer["code"] == "BLOCKED_ROLE", tool.name


def test_the_emergency_stop_is_offered_to_every_role(engine):
    """An actor that can see a problem must never be unable to make it safe."""
    gateway = _gateway(engine, Role.OBSERVER)

    answer = gateway.call_tool("emergency_standby", {"reason": "smoke"})

    assert answer["code"] != "BLOCKED_ROLE"


def _minimal_args(tool: ToolSpec) -> dict:
    """Build the smallest schema-satisfying argument set for one tool.

    In-bounds on purpose: the schema is checked before the permission matrix,
    so an out-of-range value would be refused for the wrong reason and the
    sweep would prove nothing about the role.
    """
    args = {}
    for name in tool.input_schema.get("required", ()):
        node = tool.input_schema["properties"][name]
        if "default" in node:
            args[name] = node["default"]
        elif "enum" in node:
            args[name] = node["enum"][0]
        elif "minimum" in node:
            args[name] = node["minimum"]
        elif "maximum" in node:
            args[name] = node["maximum"]
        else:
            args[name] = _zero_of(node.get("type", "string"))
    return args


def _zero_of(json_type):
    """Return a harmless value of one JSON Schema type."""
    if isinstance(json_type, list):
        json_type = json_type[0]
    return {
        "string": "",
        "number": 0.0,
        "integer": 1,
        "boolean": False,
        "object": {},
        "null": None,
    }[json_type]


def test_a_session_tool_is_judged_by_the_same_rules_a_command_is(engine):
    """A non-read session tool would meet `authorize()`'s rules, named the same.

    Every session tool shipped today is `read`-class, so the attendance rule
    has no tool to bite on yet. It is checked here against the tool a future
    contributor would add, because a refusal that named a different rule than
    the identical command would is exactly the drift two code paths produce.
    """
    orch, _station = engine
    gateway = _gateway(engine, Role.DEBUG)
    gateway.tools()
    gateway._tools["read_something_risky"] = ToolSpec(
        name="read_something_risky",
        description="A hypothetical recovery-class session tool.",
        input_schema={"type": "object", "properties": {}, "required": []},
        action_class=ActionClass.RECOVERY,
        session_function="read_status",
    )

    orch.set_attendance(True)
    attended = gateway.call_tool("read_something_risky")
    orch.set_attendance(False)
    unattended = gateway.call_tool("read_something_risky")

    assert attended["code"] == "BLOCKED_ROLE"
    assert attended["detail"]["rule"] == "attendance"
    assert attended["detail"]["action_class"] == "recovery"
    assert unattended["ok"] is True


def test_the_kill_switch_closes_the_read_tools_too(engine):
    """`revoked` leaves an agent nothing — the session tools included."""
    orch, _station = engine
    gateway = _gateway(engine, Role.OBSERVER)
    orch.set_agent_gate(ev.AgentGate.REVOKED)

    answer = gateway.call_tool("read_status")

    assert answer["code"] == "BLOCKED_ROLE"
    assert answer["detail"]["rule"] == "kill_switch"


def test_the_declaration_tools_answer_from_the_mirror(engine, qtbot):
    """No Station, no store, no engine call — the three reads still work."""
    orch, _station = engine
    gateway = _gateway(engine, Role.OBSERVER)
    orch._tick()

    status = gateway.call_tool("read_status")
    station = gateway.call_tool("read_station_info")
    manifest = gateway.call_tool("read_manifest")

    assert status["result"]["state"] == "IDLE"
    assert {i["name"] for i in station["result"]["instruments"]} >= {"magnet_z"}
    assert manifest["result"]["setup"] == "sim_cryostat"
    assert manifest["result"]["instruments"][0]["groups"] is not None


def test_a_tool_whose_collaborator_is_absent_is_refused_by_name(engine):
    """A gateway with no experiment layer says so, rather than answering nothing."""
    gateway = _gateway(engine, Role.SESSION)

    answer = gateway.call_tool("list_runs")

    assert answer["ok"] is False
    assert answer["detail"] == {
        "rule": "missing_collaborator",
        "collaborator": "experiments",
    }


# ══════════════════════════════════════════════════════════════════════════
# The session tools against a real experiment and a real probe run
# ══════════════════════════════════════════════════════════════════════════


def _tick_until(orchestrator, predicate, max_ticks: int = 4000):
    """Tick the engine until *predicate* holds; assert that it eventually does."""
    for _ in range(max_ticks):
        orchestrator._tick()
        if predicate():
            return
    raise AssertionError("the run never reached the expected state")


@pytest.fixture
def experiment_gateway(qtbot, tmp_path):
    """A gateway wired to a real experiment manager over a real sim station."""
    station = build_station(CONFIG_PATH)
    # The sim magnet ramps at a realistic rate; a tick-by-tick test cannot
    # wait for it, so speed it up (the same treatment the engine suite gives).
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
    manager.start_experiment("Tool surface", "jdoe", dict(SAMPLE_INFO))
    gateway = Gateway(
        orch,
        Role.SESSION,
        "runner-1",
        station_info=station.station_info,
        tool_context=ToolContext(
            experiments=manager,
            run_catalog={"FieldSweep": FieldSweep},
            status_log_path=tmp_path / "status.jsonl",
        ),
    )
    yield gateway, orch, manager, tmp_path
    orch.shutdown()


def test_validate_run_answers_findings_and_an_estimate(experiment_gateway):
    """'May I run this, and how long will it take?' — one call, nothing dispatched."""
    gateway, _orch, _manager, tmp_path = experiment_gateway

    answer = gateway.call_tool(
        "validate_run",
        {
            "procedure": "FieldSweep",
            "params": {**FULL_PARAMS, "field_steps": 5},
            "sample_info": dict(SAMPLE_INFO),
            "data_directory": str(tmp_path),
        },
    )

    assert answer["ok"] is True
    assert answer["result"]["ok"] is True
    assert answer["result"]["duration_estimate_s"] > 0
    assert answer["result"]["estimate"]["assumptions"]


def test_validate_run_refuses_an_unknown_class(experiment_gateway):
    """The run catalog is the vocabulary; a name outside it is refused by name."""
    gateway, *_ = experiment_gateway

    answer = gateway.call_tool("validate_run", {"procedure": "NoSuchSweep"})

    assert answer["ok"] is False
    assert answer["detail"]["rule"] == "unknown_run_class"


def test_a_probe_run_is_read_back_through_the_run_tools(experiment_gateway):
    """The whole session-tool path: probe, record, list, columns, stats, metadata.

    The stats the tool answers are the stats ``data_reader`` gives for the
    same file — the tool adds routing and a permission check, never an
    analysis of its own.
    """
    gateway, orch, manager, tmp_path = experiment_gateway
    orch.start_monitoring()

    answer = gateway.call_tool(
        "probe_run",
        {
            "procedure": "FieldSweep",
            "params": {**FULL_PARAMS, "field_steps": 51},
            "sample_info": dict(SAMPLE_INFO),
            "data_directory": str(manager.current_data_dir()),
            "file_prefix": "probe",
            "probe_spec": {"n_points": 3, "averaging": 2, "max_wait_s": 0.0},
        },
    )
    assert answer["code"] == "OK", answer

    _tick_until(orch, lambda: manager.current_experiment().runs[-1].status == "done")
    run_id = manager.current_experiment().runs[-1].run_id

    listed = gateway.call_tool("list_runs")
    assert [run["run_id"] for run in listed["result"]["runs"]] == [run_id]
    assert listed["result"]["runs"][0]["kind"] == "probe"

    columns = gateway.call_tool("read_run_columns", {"run_id": run_id})
    names = {column["name"] for column in columns["result"]["columns"]}
    assert {"field_T", "voltage_V"} <= names

    stats = gateway.call_tool(
        "read_run_stats", {"run_id": run_id, "column": "field_T"}
    )
    metadata = gateway.call_tool("read_run_metadata", {"run_id": run_id})
    values = gateway.call_tool(
        "read_run_slice", {"run_id": run_id, "column": "field_T"}
    )

    assert metadata["result"]["run_kind"] == "probe"
    assert values["result"]["values"] == pytest.approx([-1.0, 0.0, 1.0])

    data_file = manager.store.resolve_data_file(
        manager.current_experiment().experiment_id,
        manager.current_experiment().runs[-1].data_file,
    )
    with open_run(data_file) as handle:
        assert stats["result"] == summary_stats(handle, "field_T").to_json()
        assert names == {column.name for column in list_columns(handle)}


def test_reading_a_run_that_is_not_there_is_refused_by_name(experiment_gateway):
    """Path resolution goes through the store; an unknown run is a named refusal."""
    gateway, *_ = experiment_gateway

    answer = gateway.call_tool("read_run_stats", {"run_id": "ghost", "column": "x"})

    assert answer["ok"] is False
    assert answer["detail"]["rule"] == "unknown_run"


def test_read_experiment_answers_the_open_experiment(experiment_gateway):
    """No id given means the open one."""
    gateway, _orch, manager, _tmp = experiment_gateway

    answer = gateway.call_tool("read_experiment")

    assert answer["result"]["title"] == "Tool surface"
    assert answer["result"]["experiment_id"] == (
        manager.current_experiment().experiment_id
    )


def test_the_two_log_tails_read_jsonl_and_tolerate_a_missing_file(
    experiment_gateway,
):
    """Both audit trails are tails of an append-only JSONL, missing file included."""
    gateway, _orch, manager, tmp_path = experiment_gateway
    log = tmp_path / "status.jsonl"
    log.write_text(
        "".join(json.dumps({"seq": n, "state": "IDLE"}) + "\n" for n in range(50)),
        encoding="utf-8",
    )
    experiment_id = manager.current_experiment().experiment_id
    feed = manager.store.root / experiment_id / "agent_actions.jsonl"
    feed.parent.mkdir(parents=True, exist_ok=True)
    feed.write_text(
        json.dumps({"request_id": "r1", "tool": "pause_procedure"}) + "\n",
        encoding="utf-8",
    )

    log_answer = gateway.call_tool("read_operational_log", {"last": 3})
    feed_answer = gateway.call_tool("read_agent_feed")

    assert [record["seq"] for record in log_answer["result"]["records"]] == [47, 48, 49]
    assert feed_answer["result"]["records"] == [
        {"request_id": "r1", "tool": "pause_procedure"}
    ]
    assert feed_answer["result"]["experiment_id"] == experiment_id

    feed.unlink()
    assert gateway.call_tool("read_agent_feed")["result"]["records"] == []


# ══════════════════════════════════════════════════════════════════════════
# The two ELN tools: drafting an entry, and the approval gate on publishing
# ══════════════════════════════════════════════════════════════════════════


DRAFTED = "TITLE: Field sweep at 1.5 T\nSUMMARY:\nThe sweep completed cleanly."


@pytest.fixture
def eln_gateway(qtbot, tmp_path):
    """A gateway that can draft and publish: sim notebook, fake model, real records.

    The run is a real probe run over the sim station, written to a real HDF5
    file, so the drafted body carries the statistics of columns that were
    actually measured rather than of a fixture's invention.

    Yields ``(build, manager, publisher, adapter, client, run_id)`` where
    ``build(role)`` returns a gateway over the shared context, so one recorded
    run serves every role and attendance combination.
    """
    from cryosoft.session.agent_feed import AgentFeed
    from cryosoft.session.eln.drafting import FakeDraftClient
    from cryosoft.session.eln.publisher import ElnPublisher
    from cryosoft.session.eln.settings import AssistantSettings, ElnSettings
    from cryosoft.session.eln.sim_eln import SimElnAdapter

    station = build_station(CONFIG_PATH)
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []
    orch = Orchestrator(
        station, tick_interval_ms=10, run_catalog={"FieldSweep": FieldSweep}
    )
    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))
    store = ExperimentStore(tmp_path / "experiments")
    manager = ExperimentManager(
        store=store,
        roster=roster,
        orchestrator=orch,
        config_name="sim_cryostat",
        station=station,
        run_catalog={"FieldSweep": FieldSweep},
    )
    experiment = manager.start_experiment("Notebook", "jdoe", dict(SAMPLE_INFO))

    publisher = ElnPublisher(
        manager,
        ElnSettings(
            enabled=True,
            backend="sim_eln",
            base_url="https://sim.example",
            api_key="k",
            tags=("cryosoft",),
            retry_base_s=0.0,
            retry_max_s=0.0,
        ),
        adapter=SimElnAdapter({}),
    )
    manager.attach_eln_publisher(publisher)
    feed = AgentFeed(
        store.agent_feed_path(experiment.experiment_id), experiment.experiment_id
    )
    client = FakeDraftClient(DRAFTED, model="m-1", input_tokens=2000, output_tokens=400)
    context = ToolContext(
        experiments=manager,
        run_catalog={"FieldSweep": FieldSweep},
        status_log_path=tmp_path / "status.jsonl",
        draft_client=client,
        assistant_settings=AssistantSettings(
            model="m-1", prices={"m-1": {"input": 5.0, "output": 25.0}}
        ),
        publisher=publisher,
    )

    def build(role=Role.SESSION):
        return Gateway(
            orch,
            role,
            "runner-1",
            station_info=station.station_info,
            tool_context=context,
            feed=feed,
        )

    # One real probe run, dispatched the way an agent would dispatch it, so the
    # draft is written from a file the engine really wrote.
    orch.start_monitoring()
    assert build().call_tool(
        "probe_run",
        {
            "procedure": "FieldSweep",
            "params": {**FULL_PARAMS, "field_steps": 51},
            "sample_info": dict(SAMPLE_INFO),
            "data_directory": str(manager.current_data_dir()),
            "file_prefix": "probe",
            "probe_spec": {"n_points": 3, "averaging": 2, "max_wait_s": 0.0},
        },
    )["code"] == "OK"
    _tick_until(orch, lambda: manager.current_experiment().runs[-1].status == "done")
    run_id = manager.current_experiment().runs[-1].run_id

    yield build, manager, publisher, publisher._adapter, client, run_id
    publisher.stop()
    orch.shutdown()


def test_the_two_eln_tools_declare_their_class_and_their_recording(tools):
    """Drafting changes nothing; publishing puts a record into the world."""
    draft = tools["draft_eln_entry"]
    publish = tools["publish_eln_entry"]

    assert draft.action_class is ActionClass.READ
    assert publish.action_class is ActionClass.RUN_CONTROL
    assert draft.recorded is True and publish.recorded is True
    assert draft.session_function == "draft_eln_entry"
    assert {tool.name for tool in tools.values() if tool.recorded} == {
        "draft_eln_entry",
        "publish_eln_entry",
        "write_analysis_recipe",
        "run_analysis",
    }, "a tool an agent polls must not drown the accountability trail"


def test_drafting_a_finished_run_returns_the_entry_as_data(eln_gateway):
    """The headline: the facts of one run, drafted and returned, publishing nothing."""
    build, manager, publisher, adapter, client, run_id = eln_gateway
    gateway = build()

    answer = gateway.call_tool(
        "draft_eln_entry", {"run_id": run_id, "note": "check the drift"}
    )

    assert answer["ok"] is True, answer
    draft = answer["result"]
    assert draft["title"] == "Field sweep at 1.5 T"
    for fact in ("The sweep completed cleanly.", "Field Sweep", "field_start", "-1.0"):
        assert fact in draft["body_html"]
    assert "voltage_V" in draft["body_html"], "the columns the run measured"
    assert "sim_cryostat" in draft["body_html"], "the setup it ran on"
    assert draft["model"] == "m-1"
    assert draft["cost_usd"] > 0.0
    assert len(draft["prompt_digest"]) == 64

    prompt = client.calls[0][1]
    assert "procedure: Field Sweep" in prompt
    assert "setup: sim_cryostat" in prompt, "the station the run ran on"
    assert "voltage_V: count=" in prompt, "the statistics of what was measured"
    assert "check the drift" in prompt

    assert publisher.pending_count() == 0, "drafting queues nothing"
    assert not adapter.entries, "drafting publishes nothing"
    assert manager.current_experiment().find_run(run_id).pending_eln_draft == {}


def test_two_drafts_of_one_run_are_the_same_question(eln_gateway):
    """A deterministic prompt means a reproducible digest."""
    build, _manager, _publisher, _adapter, _client, run_id = eln_gateway
    gateway = build()

    first = gateway.call_tool("draft_eln_entry", {"run_id": run_id})
    again = gateway.call_tool("draft_eln_entry", {"run_id": run_id})

    assert first["result"]["prompt_digest"] == again["result"]["prompt_digest"]


def test_a_draft_records_what_it_cost_in_the_agent_feed(eln_gateway):
    """What an autonomous client spent, in the trail beside what it asked for."""
    from cryosoft.session.agent_feed import RECORD_TOOL, read_feed

    build, _manager, _publisher, _adapter, _client, run_id = eln_gateway
    gateway = build()

    gateway.call_tool("draft_eln_entry", {"run_id": run_id})

    (record,) = [
        entry
        for entry in read_feed(gateway._feed.path)
        if entry["record"] == RECORD_TOOL
    ]
    assert record["tool"] == "draft_eln_entry"
    assert record["args"] == {"run_id": run_id}
    assert record["actor"]["role"] == "session"
    assert record["verdict"] == {"code": "OK", "reason": ""}
    assert record["detail"]["model"] == "m-1"
    assert record["detail"]["input_tokens"] == 2000
    assert record["detail"]["output_tokens"] == 400
    assert record["detail"]["cost_usd"] > 0.0


def test_an_attended_experiment_refuses_the_agent_and_parks_the_draft(eln_gateway):
    """Attended: the human approves. The refusal leaves the work where they find it."""
    build, manager, publisher, adapter, _client, run_id = eln_gateway
    manager.set_attended(True)
    gateway = build()
    draft = gateway.call_tool("draft_eln_entry", {"run_id": run_id})["result"]

    answer = gateway.call_tool(
        "publish_eln_entry", {"run_id": run_id, "draft": draft}
    )

    assert answer["ok"] is False
    assert answer["detail"]["rule"] == "approval_required"
    assert answer["detail"]["attended"] is True
    assert answer["detail"]["pending"] is True
    assert publisher.pending_count() == 0 and not adapter.entries

    pending = manager.pending_eln_draft(run_id)
    assert pending["title"] == "Field sweep at 1.5 T"

    job_id = manager.approve_eln_draft(run_id)

    assert job_id == f"publish_run:{run_id}"
    assert publisher.pending_count() == 1, "exactly one job, and only once approved"
    assert manager.pending_eln_draft(run_id) == {}


def test_an_unattended_session_agent_publishes_straight_to_the_outbox(eln_gateway):
    """Unattended, the session role is what the experiment is being run by."""
    build, manager, publisher, adapter, _client, run_id = eln_gateway
    manager.set_attended(False)
    gateway = build(Role.SESSION)
    draft = gateway.call_tool("draft_eln_entry", {"run_id": run_id})["result"]

    answer = gateway.call_tool(
        "publish_eln_entry", {"run_id": run_id, "draft": draft}
    )

    assert answer["ok"] is True, answer
    assert answer["result"]["job_id"] == f"publish_run:{run_id}"
    assert publisher.pending_count() == 1
    assert manager.pending_eln_draft(run_id) == {}, "nothing waits on a human"

    from cryosoft.session.eln.outbox import DRAIN_PUBLISHED

    assert publisher.drain_once().state == DRAIN_PUBLISHED
    (entry,) = adapter.entries.values()
    assert entry["title"] == "Field sweep at 1.5 T"


def test_publishing_is_refused_to_the_roles_that_do_not_run_the_experiment(
    eln_gateway,
):
    """run_control is the session role's; a debug or observer agent reports instead."""
    build, manager, publisher, _adapter, _client, run_id = eln_gateway
    manager.set_attended(False)

    for role in (Role.DEBUG, Role.OBSERVER):
        gateway = build(role)
        answer = gateway.call_tool(
            "publish_eln_entry", {"run_id": run_id, "draft": {"title": "t"}}
        )
        assert answer["ok"] is False, role
        assert answer["code"] == "BLOCKED_ROLE"
        assert answer["detail"]["rule"] == "role_matrix"
        assert answer["detail"]["action_class"] == "run_control"

    assert publisher.pending_count() == 0


def test_a_debug_agent_may_still_draft(eln_gateway):
    """Drafting reads and changes nothing, so every role may ask for one."""
    build, manager, _publisher, _adapter, _client, run_id = eln_gateway
    manager.set_attended(False)

    answer = build(Role.DEBUG).call_tool("draft_eln_entry", {"run_id": run_id})

    assert answer["ok"] is True, answer


def test_the_eln_tools_refuse_by_name_when_their_collaborator_is_absent(
    station_info, tmp_path
):
    """A gateway wired without a model or a publisher says which one is missing."""
    orch = Orchestrator(build_station(CONFIG_PATH), tick_interval_ms=10)
    gateway = Gateway(orch, Role.SESSION, "runner-1", station_info=station_info)

    for name, args in (
        ("draft_eln_entry", {"run_id": "run-0001"}),
        ("publish_eln_entry", {"run_id": "run-0001", "draft": {}}),
    ):
        answer = gateway.call_tool(name, args)
        assert answer["ok"] is False
        assert answer["detail"]["rule"] == "missing_collaborator"
    orch.shutdown()


def test_drafting_an_unknown_run_is_refused_by_name(eln_gateway):
    """The run must be one this experiment recorded — no run, no draft."""
    build, *_ = eln_gateway

    answer = build().call_tool("draft_eln_entry", {"run_id": "ghost"})

    assert answer["ok"] is False
    assert answer["detail"]["rule"] == "unknown_run"


def test_a_model_that_cannot_be_reached_is_one_named_refusal(eln_gateway):
    """The draft client's one exception type becomes the tool's one refusal."""
    build, _manager, _publisher, _adapter, client, run_id = eln_gateway
    client.offline = True

    answer = build().call_tool("draft_eln_entry", {"run_id": run_id})

    assert answer["ok"] is False
    assert answer["detail"]["rule"] == "draft_failed"


def test_a_run_whose_file_cannot_be_read_is_still_drafted(eln_gateway):
    """A corrupt file costs the draft its statistics, never the whole draft."""
    build, manager, _publisher, _adapter, _client, _run_id = eln_gateway
    orch = manager._orchestrator
    broken = manager.current_data_dir() / "broken.h5"
    broken.write_bytes(b"not an HDF5 file")
    started = {
        "run_id": "run-broken",
        "procedure": "Field Sweep",
        "kind": "run",
        "params": {"field_start": -1.0},
        "data_file": str(broken),
        "started_utc": "2026-01-01T10:00:00+00:00",
    }
    orch.run_started.emit(started)
    orch.run_finished.emit(
        dict(started, finished_utc="2026-01-01T11:00:00+00:00", status="failed", reason="x")
    )

    answer = build().call_tool("draft_eln_entry", {"run_id": "run-broken"})

    assert answer["ok"] is True, answer
    assert "Field Sweep" in answer["result"]["body_html"]
