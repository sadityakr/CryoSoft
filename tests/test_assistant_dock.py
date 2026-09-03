# ---
# description: |
#   Behaviour tests for the embedded assistant's chat dock
#   (gui/assistant_dock.py): it builds against the OrchestratorProxy over a
#   sim station, renders the transcript records the runtime publishes, walks
#   the status chip through its four states, shows the cost line, caps the role
#   selector at the deployment's ceiling, stops a turn, and degrades to the
#   one-line no-key state when there is no client.
# last_updated: 2026-09-03
# ---

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt, QSettings

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.orchestrator_proxy import OrchestratorProxy
from cryosoft.core.station import build_station
from cryosoft.gui.assistant_dock import NO_CLIENT_MESSAGE, AssistantDock
from cryosoft.gui.theme import (
    ASSISTANT_TOOL_REFUSED_TEXT,
    BANNER_ERROR_TEXT,
    BTN_PRIMARY_PRESSED,
    TEXT_MUTED,
    build_stylesheet,
)
from cryosoft.session.assistant import (
    STATUS_CALLING,
    STATUS_IDLE,
    STATUS_REFUSED,
    STATUS_THINKING,
    AssistantRuntime,
    ChatResult,
    FakeChatClient,
    ToolCall,
)
from cryosoft.session.gateway import Gateway, Role

CONFIG_PATH = "cryosoft/configs/sim_cryostat"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Redirect the app QSettings factory to a throwaway INI file.

    The same dependency seam every GUI suite uses: a pytest run must never read
    or overwrite the user's real saved layout.
    """
    from cryosoft.gui import app_settings

    ini_path = tmp_path / "cryosoft_test_settings.ini"
    monkeypatch.setattr(
        app_settings,
        "get_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    return ini_path


@pytest.fixture
def proxy(qtbot):
    """A real sim station, its engine, and the proxy a window is built against.

    The dock holds neither: it renders a runtime, and the runtime holds one
    ``Gateway``. The proxy is built here anyway because it is what the window
    around the dock holds, and the dock has to coexist with it.
    """
    station = build_station(CONFIG_PATH)
    orch = Orchestrator(station, tick_interval_ms=50)
    client = OrchestratorProxy(orch)
    assert client.state == orch.state
    yield orch, station
    orch.shutdown()


def _runtime(proxy, replies, role=Role.SESSION):
    """Build a runtime over the bench's engine with a scripted client."""
    engine, station = proxy
    gateway = Gateway(engine, role, "assistant", station_info=station.station_info)
    return AssistantRuntime(gateway, FakeChatClient(replies=list(replies)))


@pytest.fixture
def dock(proxy, qtbot):
    """A dock rendering a runtime that reads the status and then answers."""
    runtime = _runtime(
        proxy,
        [
            ChatResult(
                tool_calls=(ToolCall(id="c1", name="read_status"),),
                model="claude-opus-5",
                input_tokens=100,
                output_tokens=10,
                stop_reason="tool_use",
            ),
            ChatResult(
                text_blocks=("The station is idle.",),
                model="claude-opus-5",
                input_tokens=900,
                output_tokens=40,
                stop_reason="end_turn",
            ),
        ],
    )
    widget = AssistantDock(runtime, max_role=Role.SESSION.value)
    qtbot.addWidget(widget)
    widget.show()
    return widget


# ── Building ──────────────────────────────────────────────────────────────────


def test_the_dock_builds_against_the_proxy_with_a_sim_station(dock):
    """Every named widget exists, and the objectNames tests rely on hold."""
    assert dock.objectName() == "assistant_dock"
    for name in (
        "assistant_transcript",
        "assistant_input",
        "assistant_send_btn",
        "assistant_stop_btn",
        "assistant_cost_label",
        "assistant_role_combo",
        "assistant_status_chip",
    ):
        assert dock.findChild(object, name) is not None, name
    assert dock.runtime is not None
    assert dock._chip.property("status") == STATUS_IDLE
    assert dock._stop_btn.isEnabled() is False


def test_with_no_client_the_dock_says_so_in_one_line_instead_of_failing(qtbot):
    """A missing key is a configuration fact, not a fault that takes the window."""
    widget = AssistantDock(None)
    qtbot.addWidget(widget)
    widget.show()

    assert widget.runtime is None
    assert widget._unavailable.isVisible() is True
    assert widget._unavailable.text() == NO_CLIENT_MESSAGE
    assert "assistant.api_key" in NO_CLIENT_MESSAGE
    assert "CRYOSOFT_ASSISTANT_APIKEY" in NO_CLIENT_MESSAGE
    assert widget._input.isVisible() is False
    assert widget._transcript.isVisible() is False


# ── The role selector ─────────────────────────────────────────────────────────


def test_the_role_selector_never_offers_more_than_the_ceiling(proxy, qtbot):
    """An observer-capped deployment offers exactly one role."""
    runtime = _runtime(proxy, [ChatResult(text_blocks=("Hi.",))], Role.OBSERVER)
    widget = AssistantDock(runtime, max_role=Role.OBSERVER.value)
    qtbot.addWidget(widget)

    offered = [widget._role_combo.itemText(i) for i in range(widget._role_combo.count())]

    assert offered == ["observer"]
    # No factory: the selector shows the role in force and cannot change it.
    assert widget._role_combo.isEnabled() is False


def test_an_unknown_ceiling_narrows_to_observer(proxy, qtbot):
    """A typo in a config must narrow authority, never widen it."""
    runtime = _runtime(proxy, [ChatResult(text_blocks=("Hi.",))], Role.OBSERVER)
    widget = AssistantDock(runtime, max_role="superuser")
    qtbot.addWidget(widget)

    offered = [widget._role_combo.itemText(i) for i in range(widget._role_combo.count())]

    assert offered == ["observer"]


def test_choosing_a_role_reconnects_the_runtime_through_the_factory(proxy, qtbot):
    """The dock never builds a gateway; it asks whoever owns the engine for one."""
    engine, station = proxy
    runtime = _runtime(proxy, [ChatResult(text_blocks=("Hi.",))], Role.OBSERVER)
    built: list[str] = []

    def _factory(role: str):
        built.append(role)
        return Gateway(engine, role, "assistant", station_info=station.station_info)

    widget = AssistantDock(
        runtime, max_role=Role.SESSION.value, role_factory=_factory
    )
    qtbot.addWidget(widget)

    widget._role_combo.setCurrentText("session")

    assert built == ["session"]
    assert runtime.role == "session"


# ── One turn, end to end, through the widget ──────────────────────────────────


def test_sending_a_question_renders_the_transcript_and_the_chip(dock, qtbot):
    """Every line shown is a transcript record the runtime published."""
    seen: list[tuple[str, str]] = []
    dock.runtime.status_changed.connect(
        lambda status, detail: seen.append((status, detail))
    )
    dock._input.setText("What is the station doing?")

    with qtbot.waitSignal(dock.runtime.turn_finished, timeout=5000):
        dock._send_btn.click()

    text = dock._transcript.toPlainText()
    assert "What is the station doing?" in text
    assert "read_status" in text and "OK" in text
    assert "The station is idle." in text
    assert dock._input.text() == ""
    assert seen == [
        (STATUS_THINKING, ""),
        (STATUS_CALLING, "read_status"),
        (STATUS_THINKING, ""),
        (STATUS_IDLE, ""),
    ]
    assert dock._chip.property("status") == STATUS_IDLE
    assert dock._input.isEnabled() is True
    assert dock._stop_btn.isEnabled() is False


def test_the_cost_line_shows_this_turn_and_this_session(dock, qtbot):
    """Two totals, always visible, taken from the runtime and never recomputed."""
    dock._input.setText("Status?")

    with qtbot.waitSignal(dock.runtime.turn_finished, timeout=5000):
        dock._send_btn.click()

    label = dock._cost_label.text()
    session = dock.runtime.session_cost()
    assert "this turn" in label and "this session" in label
    assert f"${session['cost_usd']:.4f}" in label
    assert "1,000 in" in label


def test_the_stop_button_cancels_the_turn(proxy, qtbot):
    """Stop is enabled only while a turn runs, and ends it between steps."""
    runtime = _runtime(
        proxy,
        [
            ChatResult(
                tool_calls=(ToolCall(id="c1", name="read_status"),),
                model="claude-opus-5",
            ),
            ChatResult(text_blocks=("Idle.",), model="claude-opus-5"),
        ],
    )
    widget = AssistantDock(runtime, max_role=Role.SESSION.value)
    qtbot.addWidget(widget)
    widget.show()
    widget._input.setText("Status?")

    with qtbot.waitSignal(runtime.turn_finished, timeout=5000):
        widget._send_btn.click()
        assert widget._stop_btn.isEnabled() is True
        widget._stop_btn.click()

    assert runtime.is_busy() is False
    assert widget._stop_btn.isEnabled() is False
    assert widget._input.isEnabled() is True


# ── Styling: the chip's four states, as effective colours ─────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (STATUS_IDLE, TEXT_MUTED),
        (STATUS_THINKING, BTN_PRIMARY_PRESSED),
        (STATUS_CALLING, BTN_PRIMARY_PRESSED),
        (STATUS_REFUSED, BANNER_ERROR_TEXT),
    ],
)
def test_the_chip_restyles_for_every_state(dock, qapp, status, expected):
    """Effective (post-QSS) colour, not just the property — the repolish rule."""
    qapp.setStyleSheet(build_stylesheet())

    dock._on_status(status, "read_status" if status == STATUS_CALLING else "")

    assert dock._chip.property("status") == status
    assert dock._chip.palette().windowText().color().name() == expected


def test_a_refusal_is_rendered_in_the_refusal_colour(dock, qtbot):
    """A refused tool call is visibly a refusal, and quotes the reason."""
    dock._on_message(
        {
            "record": "tool",
            "tool": "run_procedure",
            "verdict": {
                "code": "BLOCKED_ROLE",
                "reason": "The 'observer' role does not grant run_control actions.",
            },
        }
    )

    html_text = dock._transcript.toHtml()
    assert "BLOCKED_ROLE" in dock._transcript.toPlainText()
    assert "does not grant run_control" in dock._transcript.toPlainText()
    assert ASSISTANT_TOOL_REFUSED_TEXT.lstrip("#") in html_text.replace("#", "")


def test_markup_in_a_question_is_escaped(dock):
    """A human types a question and a model writes a reply; neither may inject."""
    dock._on_message({"record": "user", "text": "<b>bold</b> & <i>italic</i>"})

    assert "<b>bold</b> & <i>italic</i>" in dock._transcript.toPlainText()


# ── Registration in the Monitor window ────────────────────────────────────────


def test_the_window_builds_no_dock_unless_the_setup_asks_for_one(proxy, qtbot):
    """Config-gated like every optional feature: no declaration, no widget."""
    from cryosoft.gui.monitor_window import MonitorWindow

    engine, station = proxy
    window = MonitorWindow(station, OrchestratorProxy(engine))
    qtbot.addWidget(window)

    assert window._assistant_dock is None
    assert window.findChild(AssistantDock, "assistant_dock") is None


def test_a_setup_that_asks_gets_the_dock_even_with_no_client(proxy, qtbot):
    """`assistant: true` with no API key registers the dock in its no-key state."""
    from cryosoft.gui.monitor_window import MonitorWindow

    engine, station = proxy
    window = MonitorWindow(
        station,
        OrchestratorProxy(engine),
        assistant_enabled=True,
        assistant_runtime=None,
        assistant_max_role=Role.OBSERVER.value,
    )
    qtbot.addWidget(window)

    dock = window.findChild(AssistantDock, "assistant_dock")
    assert dock is not None
    assert dock.runtime is None
    assert dock._unavailable.text() == NO_CLIENT_MESSAGE
    assert window.dockWidgetArea(dock) == Qt.DockWidgetArea.RightDockWidgetArea


def test_the_window_hands_the_dock_the_runtime_and_the_ceiling(proxy, qtbot):
    """The window renders the assistant; it never builds a gateway of its own."""
    from cryosoft.gui.monitor_window import MonitorWindow

    engine, station = proxy
    runtime = _runtime(proxy, [ChatResult(text_blocks=("Hi.",))], Role.OBSERVER)
    window = MonitorWindow(
        station,
        OrchestratorProxy(engine),
        assistant_enabled=True,
        assistant_runtime=runtime,
        assistant_max_role=Role.DEBUG.value,
    )
    qtbot.addWidget(window)

    dock = window.findChild(AssistantDock, "assistant_dock")
    offered = [dock._role_combo.itemText(i) for i in range(dock._role_combo.count())]
    assert dock.runtime is runtime
    assert offered == ["observer", "debug"]
