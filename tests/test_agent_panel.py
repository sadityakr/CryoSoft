# ---
# description: |
#   Behaviour tests for the Agent panel and the takeover strip
#   (gui/agent_panel.py, gui/takeover_strip.py, and their home in
#   MonitorWindow): the panel filters the event stream down to what the
#   machines did, seeds itself from the Agent feed, renders a refusal as a
#   refusal and a pending ELN draft as a question; the strip applies the kill
#   switch, reflects it back from the mirror, keeps attendance true in both
#   places it lives, and never gates the human.
# last_updated: 2026-09-03
# ---

"""The Agent panel and the takeover strip (GUI, both instrument modes).

Built the way the application builds them: a real sim station behind an
``InstrumentHost``, the ``OrchestratorProxy`` a window is handed, and a real
``ExperimentManager``. The suite therefore runs unchanged in either
instrument mode (``tests/instrument_modes.py``):

    pytest tests/test_agent_panel.py
    CRYOSOFT_INSTRUMENT_THREAD=1 pytest tests/test_agent_panel.py
"""

from __future__ import annotations

import json
import time

import pytest
from PyQt6.QtCore import QSettings

from cryosoft.core import events as ev
from cryosoft.core.orchestrator import OrchestratorState
from cryosoft.gui.agent_panel import (
    MAX_ROWS,
    OUTCOME_PENDING,
    OUTCOME_REFUSED,
    AgentAction,
    AgentPanel,
)
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.theme import BANNER_ERROR_TEXT, build_stylesheet
from cryosoft.session.gateway import Gateway, Role
from tests.instrument_modes import build_host, engine_of, settled, shutdown_host

CONFIG_PATH = "cryosoft/configs/sim_cryostat"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Redirect the app QSettings factory to a throwaway INI file.

    The same dependency seam every GUI suite uses: a pytest run must never
    read or overwrite the user's real saved layout.
    """
    from cryosoft.gui import app_settings

    ini_path = tmp_path / "cryosoft_test_settings.ini"
    monkeypatch.setattr(
        app_settings,
        "get_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(
        app_settings, "autosave_file_path", lambda user_id=None: tmp_path / "auto.json"
    )
    return ini_path


@pytest.fixture
def instrument_host(qtbot):
    """A started host over the sim station, in this session's instrument mode."""
    host = build_host(CONFIG_PATH, tick_interval_ms=50)
    yield host
    shutdown_host(host)


@pytest.fixture
def station(instrument_host):
    """The simulated station the host built."""
    return instrument_host.station


@pytest.fixture
def orchestrator(instrument_host):
    """The client adapter the windows are handed, as ``main.py`` hands it."""
    return instrument_host.build_proxy()


@pytest.fixture
def session_manager(tmp_path, orchestrator):
    """A real ExperimentManager over a tmp store, with one roster user."""
    from cryosoft.session.manager import ExperimentManager
    from cryosoft.session.models import User
    from cryosoft.session.store import ExperimentStore, UserRoster

    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))
    return ExperimentManager(
        store=ExperimentStore(tmp_path / "experiments"),
        roster=roster,
        orchestrator=orchestrator,
        config_name="sim_cryostat",
    )


@pytest.fixture
def window(station, orchestrator, session_manager, qtbot):
    """A MonitorWindow wired to the manager, as the application builds it."""
    win = MonitorWindow(station, orchestrator, session_manager=session_manager)
    qtbot.addWidget(win)
    win.show()
    return win


@pytest.fixture
def panel(window):
    """The window's Agent panel."""
    return window._agent_panel


@pytest.fixture
def strip(window):
    """The window's takeover strip."""
    return window._takeover_strip


def _gateway(orchestrator, station, role=Role.SESSION, actor_id="runner-7"):
    """Attach one agent connection to the engine behind the client."""
    return Gateway(
        engine_of(orchestrator), role, actor_id, station_info=station.station_info
    )


def _verdict(code=ev.VerdictCode.OK, actor_id="runner-7", **kwargs):
    """Build one agent verdict off the contract, for the panel to absorb."""
    actor = ev.Actor(kind=ev.ActorKind.AGENT, id=actor_id, role="session")
    return ev.Verdict(
        request_id=kwargs.pop("request_id", "r-1"),
        command=kwargs.pop("command", ev.CommandName.PAUSE_PROCEDURE),
        code=code,
        actor=actor,
        **kwargs,
    )


# ── The panel is a filter of the event stream ─────────────────────────────────


def test_the_panel_starts_empty_and_says_so(panel):
    """No agent has acted: an empty state, not a blank box."""
    assert panel.actions() == ()
    assert panel.row_texts() == ()
    assert panel._empty_label.isVisible()


def test_an_agent_verdict_becomes_a_row_and_an_operator_one_does_not(panel):
    """The filter IS the panel: the physicist's own actions are not shown."""
    panel.on_verdict(_verdict(reason="paused at the datapoint boundary"))
    panel.on_verdict(
        ev.Verdict(
            request_id="r-2",
            command=ev.CommandName.PAUSE_PROCEDURE,
            code=ev.VerdictCode.OK,
        )
    )

    (action,) = panel.actions()
    assert action.actor_id == "runner-7"
    (row,) = panel.row_texts()
    assert "runner-7" in row and "pause_procedure" in row and "OK" in row
    assert not panel._empty_label.isVisible()


def test_an_agent_state_change_becomes_a_row_naming_the_cause(panel):
    """A state change an agent caused is a consequence a dispute is about."""
    panel.on_event(
        ev.StateChange(
            state=OrchestratorState.PAUSED.value,
            previous=OrchestratorState.SWEEPING.value,
            cause="agent_pause",
            actor=ev.Actor(kind=ev.ActorKind.AGENT, id="runner-7", role="session"),
        )
    )

    (row,) = panel.row_texts()
    assert "SWEEPING → PAUSED" in row and "agent_pause" in row


def test_system_rows_are_hidden_until_the_filter_asks_for_them(panel):
    """System traffic is available, not always on: it is the engine, not an agent."""
    panel.on_verdict(_verdict())
    panel.on_event(
        ev.StateChange(
            state=OrchestratorState.ERROR.value,
            cause="fault",
            actor=ev.Actor(kind=ev.ActorKind.SYSTEM, id="orchestrator"),
        )
    )

    assert len(panel.actions()) == 2
    assert len(panel.row_texts()) == 1

    panel._system_checkbox.setChecked(True)

    assert len(panel.row_texts()) == 2
    assert any("fault" in row for row in panel.row_texts())


def test_the_newest_row_is_at_the_bottom_and_the_list_is_capped(panel):
    """Newest at the bottom, and a cap — the trail's real home is the feed."""
    for index in range(MAX_ROWS + 5):
        panel.on_verdict(_verdict(reason=f"call {index}", request_id=f"r-{index}"))

    rows = panel.row_texts()
    assert len(rows) == MAX_ROWS
    assert f"call {MAX_ROWS + 4}" in rows[-1]
    assert "call 0" not in " ".join(rows), "the oldest rows are dropped, not kept"


@pytest.fixture
def themed_app(qapp):
    """Apply the real application stylesheet for the test, then restore it.

    An effective-colour assertion only means anything with the real QSS in
    force, and only for a widget polished under it — so a test using this
    builds its widgets after asking for it.
    """
    qapp.setStyleSheet(build_stylesheet())
    yield qapp
    qapp.setStyleSheet("")


def test_a_refusal_row_is_visually_distinct_and_names_the_rule(themed_app, qtbot):
    """A refusal reads as one — dynamic property, and the effective colour."""
    panel = AgentPanel()
    qtbot.addWidget(panel)
    panel.show()
    panel.on_verdict(
        _verdict(
            code=ev.VerdictCode.BLOCKED_ROLE,
            reason="Agent access is revoked: 'pause_procedure' is refused.",
        )
    )

    qtbot.wait(1)  # Qt polishes a new row on the next event-loop turn
    row = panel._row_widgets[-1]
    label = row.layout().itemAt(0).widget()
    assert label.property("outcome") == OUTCOME_REFUSED
    assert "revoked" in label.text()
    assert label.palette().windowText().color().name() == BANNER_ERROR_TEXT


# ── The gateway's own refusal, end to end ─────────────────────────────────────


def test_an_agent_refused_by_the_kill_switch_shows_as_a_refusal_row(
    window, panel, strip, orchestrator, station
):
    """The exit criterion: revoke the gate, and the refusal lands in the panel."""
    gateway = _gateway(orchestrator, station)
    strip._radios[ev.AgentGate.REVOKED.value].click()
    settled(orchestrator)

    gateway.submit(ev.CommandName.START_MONITORING)
    settled(orchestrator)

    refusals = [action for action in panel.actions() if action.refused]
    assert refusals, "a refused agent command must be visible to the physicist"
    assert refusals[-1].code == ev.VerdictCode.BLOCKED_ROLE.value
    assert "revoked" in refusals[-1].reason
    assert refusals[-1].actor_id == "runner-7"


def test_the_operator_is_never_gated(window, orchestrator, strip):
    """The kill switch binds agents only — it can never lock the human out."""
    strip._radios[ev.AgentGate.REVOKED.value].click()
    settled(orchestrator)

    window._monitoring_btn.click()
    settled(orchestrator)

    assert orchestrator.is_monitoring(), "an operator command must still be obeyed"
    assert orchestrator.agent_gate() == ev.AgentGate.REVOKED.value


# ── The takeover strip ────────────────────────────────────────────────────────


def test_the_gate_radios_apply_and_reflect_the_engines_setting(
    strip, orchestrator
):
    """Applied through the client, reflected from the mirror — both ways."""
    assert strip._radios[ev.AgentGate.ACTIVE.value].isChecked()

    strip._radios[ev.AgentGate.READ_ONLY.value].click()
    settled(orchestrator)
    assert orchestrator.agent_gate() == ev.AgentGate.READ_ONLY.value

    # A change made anywhere else — an agent, the CLI — shows here without
    # this widget having been told.
    orchestrator.set_agent_gate(ev.AgentGate.REVOKED)
    settled(orchestrator)
    strip.sync_from_mirror()
    assert strip._radios[ev.AgentGate.REVOKED.value].isChecked()
    assert not strip._radios[ev.AgentGate.READ_ONLY.value].isChecked()


def test_attendance_reaches_both_the_record_and_the_engine(
    strip, session_manager, orchestrator
):
    """One fact, two homes: the experiment record and the engine's mirror."""
    session_manager.start_experiment("Hall bar A3", "jdoe", {"sample_name": "A3"})
    settled(orchestrator)
    assert strip._attended_checkbox.isChecked()

    strip._attended_checkbox.setChecked(False)
    settled(orchestrator)

    assert session_manager.current_experiment().attended is False
    assert orchestrator.attended() is False

    strip._attended_checkbox.setChecked(True)
    settled(orchestrator)
    assert session_manager.current_experiment().attended is True
    assert orchestrator.attended() is True


def test_attendance_without_an_experiment_still_reaches_the_engine(
    station, orchestrator, qtbot
):
    """No record to write, and the engine still has to know."""
    from cryosoft.gui.takeover_strip import TakeoverStrip

    strip = TakeoverStrip(orchestrator)
    qtbot.addWidget(strip)

    strip._attended_checkbox.setChecked(False)
    settled(orchestrator)

    assert orchestrator.attended() is False


def test_the_strip_counts_the_agents_that_have_acted(window, panel, strip):
    """"agents active" counts distinct actors, and forgets a stale one."""
    panel.on_verdict(_verdict(actor_id="runner-7"))
    panel.on_verdict(_verdict(actor_id="watcher-2"))
    panel.on_verdict(_verdict(actor_id="runner-7"))

    assert panel.active_agent_count() == 2
    assert strip._agents_active_label.text() == "agents active: 2"

    # Long enough ago that nothing is acting on the cryostat any more.
    assert panel.active_agent_count(now=time.time() + 10_000) == 0


# ── Seeding from the Agent feed ───────────────────────────────────────────────


def _write_feed(path, records):
    """Write an Agent feed file by hand, one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )


def test_the_panel_seeds_from_the_experiments_agent_feed(
    window, panel, session_manager, tmp_path
):
    """History survives a restart: the trail is on disk, not in this process."""
    experiment = session_manager.start_experiment(
        "Hall bar A3", "jdoe", {"sample_name": "A3"}
    )
    feed_path = session_manager.store.agent_feed_path(experiment.experiment_id)
    _write_feed(
        feed_path,
        [
            {
                "schema": 2,
                "ts": 1_760_000_000.0,
                "seq": 1,
                "record": "verdict",
                "actor": {"kind": "agent", "id": "runner-7", "role": "session"},
                "request_id": "r-1",
                "command": "run_procedure",
                "verdict": {"code": "BLOCKED_ENVELOPE", "reason": "outside 2 T"},
            },
            {
                "schema": 2,
                "ts": 1_760_000_001.0,
                "seq": 2,
                "record": "command",
                "actor": {"kind": "agent", "id": "runner-7", "role": "session"},
                "request_id": "r-2",
                "command": "run_procedure",
                "args": {"procedure": "FieldSweep"},
            },
            {
                "schema": 2,
                "ts": 1_760_000_002.0,
                "seq": 3,
                "record": "event",
                "actor": {"kind": "agent", "id": "runner-7", "role": "session"},
                "request_id": "r-2",
                "event": "state_change",
                "detail": {"state": "RAMPING", "previous": "IDLE", "cause": "run"},
            },
        ],
    )

    panel.reload_experiment()

    rows = panel.row_texts()
    assert len(rows) == 2, "a command record is not shown twice as its verdict"
    assert "BLOCKED_ENVELOPE" in rows[0] and "outside 2 T" in rows[0]
    assert "IDLE → RAMPING" in rows[1]
    assert panel.actions()[0].refused


def test_a_missing_feed_seeds_nothing_and_raises_nothing(
    window, panel, session_manager
):
    """Nothing has acted yet: no file, no rows, no exception."""
    session_manager.start_experiment("Fresh", "jdoe", {})

    panel.reload_experiment()

    assert panel.row_texts() == ()
    assert panel._empty_label.isVisible()


# ── The pending ELN draft: the one row that asks a question ───────────────────


@pytest.fixture
def publisher(session_manager, orchestrator, tmp_path):
    """A real ELN publisher over the simulated notebook, attached and armed."""
    from cryosoft.session.eln.publisher import ElnPublisher
    from cryosoft.session.eln.settings import ElnSettings
    from cryosoft.session.eln.sim_eln import SimElnAdapter

    experiment = session_manager.start_experiment("Sample A", "jdoe", {})
    data_file = (
        session_manager.store.data_dir(experiment.experiment_id) / "run-0001.h5"
    )
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_bytes(b"\x89HDF\r\n\x1a\n")
    session_manager._on_run_started(
        {
            "run_id": "run-0001",
            "procedure": "Field Sweep",
            "kind": "run",
            "params": {"field_T": 1.5},
            "data_file": str(data_file),
            "started_utc": "2026-01-01T10:00:00+00:00",
        }
    )
    settings = ElnSettings(
        enabled=True,
        backend="sim_eln",
        base_url="https://sim.example",
        api_key="k",
        retry_base_s=0.0,
        retry_max_s=0.0,
    )
    publisher = ElnPublisher(session_manager, settings, adapter=SimElnAdapter({}))
    session_manager.attach_eln_publisher(publisher)
    yield publisher
    publisher.stop()


def test_a_pending_draft_becomes_an_approve_row_that_queues_one_job(
    window, panel, session_manager, publisher, qtbot
):
    """The approval gate, from the panel: one click, one queued job, row gone."""
    from PyQt6.QtWidgets import QPushButton

    session_manager.set_pending_eln_draft(
        "run-0001", {"title": "Awaiting a human", "body_html": "<p>prose</p>"}
    )

    button = window.findChild(QPushButton, "agent_approve_run-0001")
    assert button is not None, "a draft waiting on a human is a row with a button"
    assert list(panel._draft_rows) == ["run-0001"]
    label = panel._draft_rows["run-0001"].layout().itemAt(0).widget()
    assert label.property("outcome") == OUTCOME_PENDING
    assert publisher.pending_count() == 0, "a pending draft publishes nothing"

    button.click()

    assert publisher.pending_count() == 1
    assert session_manager.pending_eln_draft("run-0001") == {}
    assert panel._draft_rows == {}, "an approved draft stops asking"
    qtbot.wait(10)  # the retired row is deleted on the next event-loop turn
    assert window.findChild(QPushButton, "agent_approve_run-0001") is None


# ── The row model itself ──────────────────────────────────────────────────────


def test_the_row_model_renders_one_line_per_action():
    """Time, who, what, verdict, reason — and nothing invented."""
    action = AgentAction(
        ts=1_760_000_000.0,
        actor_id="runner-7",
        actor_role="session",
        what="run_procedure",
        code="BLOCKED_ENVELOPE",
        reason="outside the sample envelope",
    )

    line = action.text()
    assert "runner-7 (session)" in line
    assert "run_procedure → BLOCKED_ENVELOPE" in line
    assert line.endswith("outside the sample envelope")
    assert action.refused and action.outcome == OUTCOME_REFUSED


def test_a_panel_without_a_session_layer_is_a_pure_view(qtbot):
    """No manager: the live stream still renders, nothing else is reached for."""
    panel = AgentPanel()
    qtbot.addWidget(panel)

    panel.on_verdict(_verdict())

    assert len(panel.row_texts()) == 1
    assert panel.active_agent_count() == 1
