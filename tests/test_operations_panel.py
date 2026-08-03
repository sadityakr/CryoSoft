"""Behavior tests for OperationsPanel / OperationCard / OperatorDialog."""

from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QDialog, QMessageBox, QScrollArea

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station, read_cryogenics_config, read_operations_config
from cryosoft.gui import operations_panel as operations_panel_module
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.operations_panel import OperationCard, OperationsPanel
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.procedures.operations.sample_load import SampleLoadOperation
from cryosoft.procedures.operations.sample_unload import SampleUnloadOperation
from cryosoft.session.servicing_log import HeliumRecordStore, ServicingLogStore

CONFIG_PATH = "cryosoft/configs/sim_cryostat"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


@pytest.fixture
def orchestrator(station, qtbot):
    orch = Orchestrator(station, tick_interval_ms=50)
    yield orch
    orch.shutdown()


@pytest.fixture
def cryogenics_config():
    return read_cryogenics_config(CONFIG_PATH)


@pytest.fixture
def operations_config():
    return read_operations_config(CONFIG_PATH)


@pytest.fixture
def stores(tmp_path):
    helium_store = HeliumRecordStore(tmp_path / "servicing", "sim_cryostat")
    servicing_store = ServicingLogStore(tmp_path / "servicing", "sim_cryostat")
    return helium_store, servicing_store


def _fully_inside(viewport, widget) -> bool:
    """Return True if *widget* is visible AND fully inside *viewport*'s width.

    Mirrors test_gui.py's ``_fully_inside_param_viewport`` idiom exactly
    (horizontal-only): a QScrollArea legitimately clips content vertically
    (that's what scrolling is for), but a widget pushed off-screen to the
    *right* — the bug class this guards against — never should be, and a
    horizontal scrollbar should never be needed for this fixed-width column.
    """
    if not widget.isVisible():
        return False
    top_left = widget.mapTo(viewport, widget.rect().topLeft())
    bottom_right = widget.mapTo(viewport, widget.rect().bottomRight())
    return (
        top_left.x() >= 0
        and bottom_right.x() <= viewport.width()
        and bottom_right.x() > top_left.x()
    )


# ── Config-gated presence ─────────────────────────────────────────────────────


def test_operations_panel_absent_without_any_config(station, orchestrator, qtbot):
    """No cryogenics/operations kwargs -> no panel; the quadrant shows a placeholder."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    win.show()

    assert win._operations_panel_enabled is False
    assert win._operations_panel is None
    assert win.findChild(QScrollArea, "operations_scroll") is None


def test_operations_panel_present_with_cryogenics_config(
    station, orchestrator, cryogenics_config, operations_config, stores, qtbot
):
    """A wired cryogenics config + stores + level VI builds the panel in its quadrant."""
    helium_store, servicing_store = stores
    win = MonitorWindow(
        station,
        orchestrator,
        cryogenics_config=cryogenics_config,
        operations_config=operations_config,
        helium_store=helium_store,
        servicing_store=servicing_store,
        servicing_log_kinds=["cryogenics"],
    )
    qtbot.addWidget(win)
    win.show()

    assert win._cryogenics_enabled is True
    assert win._operations_panel_enabled is True
    assert win._operations_panel is not None
    assert win.findChild(QScrollArea, "operations_scroll") is not None


def test_operations_panel_without_cryogenics_but_with_operations_builds_declared_cards_only(
    station, orchestrator, operations_config, qtbot
):
    """No cryogenics block, but an operations: block -> panel with one card per declared block."""
    win = MonitorWindow(
        station,
        orchestrator,
        operations_config=operations_config,
    )
    qtbot.addWidget(win)
    win.show()

    assert win._cryogenics_enabled is False
    assert win._operations_panel_enabled is True
    assert win._operations_panel is not None
    assert win.findChild(QScrollArea, "operations_scroll") is not None

    cards = win._operations_panel._cards
    names = [card._display_instance.name for card in cards]
    assert names == [SampleLoadOperation.name, SampleUnloadOperation.name]


def test_operations_panel_geometry_fully_visible_when_selected(
    station, orchestrator, cryogenics_config, operations_config, stores, qtbot
):
    """The Operations quadrant shows the panel fully inside its scroll viewport."""
    helium_store, servicing_store = stores
    win = MonitorWindow(
        station,
        orchestrator,
        cryogenics_config=cryogenics_config,
        operations_config=operations_config,
        helium_store=helium_store,
        servicing_store=servicing_store,
        servicing_log_kinds=["cryogenics"],
    )
    qtbot.addWidget(win)
    win.resize(1280, 900)
    win.show()
    qtbot.waitExposed(win)

    scroll = win.findChild(QScrollArea, "operations_scroll")
    assert scroll is not None
    viewport = scroll.viewport()
    assert scroll.horizontalScrollBar().maximum() == 0

    fill_card = win.findChild(OperationCard, "operation_card_helium_fill")
    assert fill_card is not None
    assert _fully_inside(viewport, fill_card)


# ── Consumption rate feeds next_due() even with no status display ──────────────


def test_states_updated_recomputes_consumption_rate_for_next_due(
    station, orchestrator, cryogenics_config, stores, qtbot
):
    """on_states_updated() still recomputes the cached rate next_due() reads."""
    helium_store, servicing_store = stores
    panel = OperationsPanel(
        station,
        orchestrator,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    panel._last_recompute_mono = None  # force the throttle to fire

    level_vi = cryogenics_config["level_vi"]
    panel.on_states_updated({level_vi: {"helium_level": 62.5, "nitrogen_level": 44.0}})

    assert panel._last_recompute_mono is not None


# ── Generic card construction ──────────────────────────────────────────────────


def test_cards_built_for_fill_and_sample_load_and_sample_unload_on_sim_cryostat(
    station, orchestrator, cryogenics_config, operations_config, stores, qtbot
):
    """sim_cryostat's config builds one card per configured operation."""
    helium_store, servicing_store = stores
    panel = OperationsPanel(
        station,
        orchestrator,
        cryogenics_config,
        operations_config,
        helium_store,
        servicing_store,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)

    names = [card._display_instance.name for card in panel._cards]
    assert names == [HeliumFillOperation.name, SampleLoadOperation.name, SampleUnloadOperation.name]
    assert panel.findChild(OperationCard, "operation_card_helium_fill") is not None
    assert panel.findChild(OperationCard, "operation_card_sample_load") is not None
    assert panel.findChild(OperationCard, "operation_card_sample_unload") is not None


def test_unknown_operations_config_key_is_skipped_with_warning(
    station, orchestrator, stores, qtbot, caplog
):
    """An operations: key with no matching discovered config_key is skipped, not fatal."""
    helium_store, servicing_store = stores
    with caplog.at_level("WARNING"):
        panel = OperationsPanel(
            station,
            orchestrator,
            None,
            {"not_a_real_operation": {}},
            helium_store,
            servicing_store,
            get_data_dir=lambda: "/tmp",
        )
    qtbot.addWidget(panel)
    assert panel._cards == []
    assert any("not_a_real_operation" in record.message for record in caplog.records)


# ── Readiness checklist ─────────────────────────────────────────────────────────


def test_checklist_flips_on_snapshot_change(
    station, orchestrator, cryogenics_config, stores, qtbot
):
    """The zero_field checklist row flips its icon/detail as the state snapshot changes."""
    helium_store, servicing_store = stores
    panel = OperationsPanel(
        station,
        orchestrator,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    icon_label, detail_label = card._condition_rows["zero_field"]

    zero_state = {"magnet_z": {"magnet_state": "standby"}, "magnet_y": {"magnet_state": "standby"}}
    ctx = {"state": zero_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(zero_state, ctx)
    assert not icon_label.pixmap().isNull()
    assert detail_label.text() == "all magnets standby"

    nonzero_state = {"magnet_z": {"magnet_state": "holding"}, "magnet_y": {"magnet_state": "standby"}}
    ctx = {"state": nonzero_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(nonzero_state, ctx)
    assert "magnet_z holding" == detail_label.text()


# ── Start / finish button ────────────────────────────────────────────────────


class _FakeOperatorDialog:
    """Stand-in for OperatorDialog that auto-accepts a fixed operator name."""

    def __init__(self, title: str = "", message: str = "", prefill: str = "", parent=None) -> None:
        self._name = prefill or "Test Operator"

    def exec(self):
        return QDialog.DialogCode.Accepted

    def operator_name(self) -> str:
        return self._name


def test_action_button_submits_run_operation_with_person(
    station, cryogenics_config, stores, qtbot, monkeypatch, tmp_path
):
    """Clicking the action button constructs a fresh HeliumFillOperation and calls run_operation."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)

    monkeypatch.setattr(operations_panel_module, "OperatorDialog", _FakeOperatorDialog)

    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
        get_current_person=lambda: "J. Doe",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._action_btn.click()

    mock_orch.run_operation.assert_called_once()
    submitted = mock_orch.run_operation.call_args[0][0]
    assert isinstance(submitted, HeliumFillOperation)
    assert submitted.get_params()["person"] == "J. Doe"
    # The display instance (used for readiness/next-due) is never the one submitted.
    assert submitted is not card._display_instance


def test_button_toggles_on_run_started_and_finished(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """Once tracked as running, the button becomes Finish <name> and calls finish_operation()."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)

    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    assert card._action_btn.text() == "Helium Fill…"

    # Simulate the run_started manifest directly — a MagicMock's .connect()
    # does not deliver a real Qt signal, matching how OperationCard connects
    # run_started/run_finished directly (not through the window).
    card._on_run_started({"procedure": HeliumFillOperation.name})
    assert card._action_btn.text() == "Finish Helium Fill"

    card._action_btn.click()
    mock_orch.finish_operation.assert_called_once()

    card._on_run_finished({"procedure": HeliumFillOperation.name, "status": "done"})
    assert card._action_btn.text() == "Helium Fill…"


def test_run_started_for_other_procedure_does_not_toggle_button(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """A run_started manifest for an unrelated procedure leaves the card's button alone."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._on_run_started({"procedure": "Field Sweep"})
    assert card._action_btn.text() == "Helium Fill…"


# ── Abort button ─────────────────────────────────────────────────────────────
# Generic OperationCard feature (every running operation gets one, not just a
# hold-phase one) — exercised here with the Helium Fill card, same as the
# Finish-button tests above.


def test_abort_button_hidden_while_idle_shown_while_running(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """Abort is hidden until a run starts, then visible and enabled."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    assert card._abort_btn.isHidden()

    card._on_run_started({"procedure": HeliumFillOperation.name})
    assert not card._abort_btn.isHidden()
    assert card._abort_btn.isEnabled()

    card._on_run_finished({"procedure": HeliumFillOperation.name, "status": "done"})
    assert card._abort_btn.isHidden()


def test_abort_button_disabled_while_finishing(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """Abort stays visible but disabled during the brief 'Finishing…' window."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._on_run_started({"procedure": HeliumFillOperation.name})
    card._action_btn.click()  # Finish
    assert card._finishing is True
    assert not card._abort_btn.isHidden()
    assert not card._abort_btn.isEnabled()


def test_abort_button_confirms_then_calls_abort_procedure(
    station, cryogenics_config, stores, qtbot, tmp_path, monkeypatch
):
    """Clicking Abort asks for confirmation, then calls orchestrator.abort_procedure() on Yes."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    card._on_run_started({"procedure": HeliumFillOperation.name})

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    card._abort_btn.click()
    mock_orch.abort_procedure.assert_called_once()


def test_abort_button_does_nothing_when_confirmation_declined(
    station, cryogenics_config, stores, qtbot, tmp_path, monkeypatch
):
    """Declining the confirmation dialog never calls abort_procedure()."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    card._on_run_started({"procedure": HeliumFillOperation.name})

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    card._abort_btn.click()
    mock_orch.abort_procedure.assert_not_called()


# ── The current-step action row (stepped operations) ─────────────────────────


def _confirm_every_step(operation) -> None:
    """Confirm every declared step, so all readiness rows hold."""
    for step in operation.steps():
        operation.confirm(step.key)


def test_step_row_hidden_until_running_then_shows_the_first_step(
    station, operations_config, qtbot
):
    """The action row is a mid-run control: the steps are things done to a running cryostat."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    assert card._display_instance.name == SampleLoadOperation.name

    assert card._step_row.isHidden()

    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})

    assert not card._step_row.isHidden()
    assert card._step_label.text() == "Now: Warm the VTI to 290 K"


def test_step_confirm_button_calls_confirm_operation_and_advances(
    station, operations_config, qtbot
):
    """Confirming the current step advances the row to the next one."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})

    card._step_confirm_btn.click()
    mock_orch.confirm_operation.assert_called_once_with("warm_vti")

    # The mock Orchestrator does not touch the operation, so mimic what the
    # real one does, then re-sync: the row must follow the operation's own
    # record, not the click.
    running.confirm("warm_vti")
    card._sync_step_row()
    assert card._step_label.text() == "Now: Close the needle valve"


def test_step_skip_button_warns_first_and_only_skips_when_accepted(
    station, operations_config, qtbot, monkeypatch
):
    """Skipping is always allowed, but never silent — the warning names the live conditions."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})

    cold_state = {
        "magnet_z": {"magnet_state": "standby"},
        "magnet_y": {"magnet_state": "standby"},
        "temperature_vti": {"temperature": 4.2},
    }
    ctx = {"state": cold_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(cold_state, ctx)

    asked: list[str] = []

    def _decline(_parent, _title, text, *args, **kwargs):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", _decline)
    card._step_skip_btn.click()

    # Declining must not skip anything.
    mock_orch.skip_operation_step.assert_not_called()
    assert asked, "the operator must be warned before a skip"
    assert "4.2 K" in asked[0], "the warning must quote the live temperature"
    assert "290 K" in asked[0]

    def _accept(_parent, _title, text, *args, **kwargs):
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", _accept)
    card._step_skip_btn.click()
    mock_orch.skip_operation_step.assert_called_once_with("warm_vti")


def test_skipped_step_row_shows_the_skip_icon_not_the_failure_icon(
    station, operations_config, qtbot
):
    """A deliberate, recorded override must not be painted as an error."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})

    running.skip_step("warm_vti")
    state = {
        "magnet_z": {"magnet_state": "standby"},
        "magnet_y": {"magnet_state": "standby"},
        "temperature_vti": {"temperature": 4.2},
    }
    card.on_states_updated(
        state, {"state": state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    )

    assert card._skipped_step_keys() == {"warm_vti"}
    _icon, detail_label = card._condition_rows["warm_vti"]
    assert detail_label.text() == "skipped by operator"
    # The row is not "met": the ready banner must not appear off a skip.
    assert card._ready_banner.isHidden()
    assert card._step_label.text() == "Now: Close the needle valve"


def test_step_row_hides_once_every_step_has_an_outcome(
    station, operations_config, qtbot
):
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})
    assert not card._step_row.isHidden()

    _confirm_every_step(running)
    card._sync_step_row()

    assert running.current_step() is None
    assert card._step_row.isHidden(), "nothing left to act on"


# ── Pre-run toggles (disarm_measurement_vis) ─────────────────────────────────
# Distinct from the current-step action row above: persistent
# (visible/editable regardless of run state), read once at Start-click time
# rather than
# confirmed against a running instance.


def test_pre_run_toggle_checkbox_visible_and_checked_by_default(
    station, operations_config, qtbot
):
    """A declared pre_run_toggles checkbox exists, is checked, and is always visible."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station, mock_orch, None, operations_config, None, None, get_data_dir=lambda: "/tmp"
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    assert card._display_instance.name == SampleLoadOperation.name

    checkbox = card._option_checkboxes["disarm_measurement_vis"]
    assert checkbox.isChecked()
    assert checkbox.isEnabled()
    assert not checkbox.isHidden()  # persistent, unlike the confirmations row


def test_pre_run_toggle_checkbox_disabled_but_not_hidden_while_running(
    station, operations_config, qtbot
):
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station, mock_orch, None, operations_config, None, None, get_data_dir=lambda: "/tmp"
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._on_run_started({"procedure": SampleLoadOperation.name})
    checkbox = card._option_checkboxes["disarm_measurement_vis"]
    assert not checkbox.isEnabled()
    assert not checkbox.isHidden()

    card._on_run_finished({"procedure": SampleLoadOperation.name, "status": "done"})
    assert checkbox.isEnabled()


def test_unchecking_pre_run_toggle_constructs_operation_with_it_off(
    station, operations_config, qtbot, monkeypatch
):
    """Unchecking the box before Start builds an operation with disarm_measurement_vis=False."""
    mock_orch = MagicMock(spec=Orchestrator)
    monkeypatch.setattr(operations_panel_module, "OperatorDialog", _FakeOperatorDialog)
    panel = OperationsPanel(
        station, mock_orch, None, operations_config, None, None, get_data_dir=lambda: "/tmp"
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._option_checkboxes["disarm_measurement_vis"].setChecked(False)
    card._action_btn.click()

    mock_orch.run_operation.assert_called_once()
    submitted = mock_orch.run_operation.call_args[0][0]
    assert submitted.get_params()["disarm_measurement_vis"] is False


def test_leaving_pre_run_toggle_checked_constructs_operation_with_default(
    station, operations_config, qtbot, monkeypatch
):
    """Leaving the box checked (the default) builds an operation with the toggle on."""
    mock_orch = MagicMock(spec=Orchestrator)
    monkeypatch.setattr(operations_panel_module, "OperatorDialog", _FakeOperatorDialog)
    panel = OperationsPanel(
        station, mock_orch, None, operations_config, None, None, get_data_dir=lambda: "/tmp"
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._action_btn.click()

    submitted = mock_orch.run_operation.call_args[0][0]
    assert submitted.get_params()["disarm_measurement_vis"] is True


def test_helium_fill_card_has_no_pre_run_toggle_checkboxes(
    station, cryogenics_config, stores, qtbot
):
    """HeliumFillOperation declares no pre_run_toggles -> zero checkboxes, unaffected call shape."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    assert card._display_instance.name == HeliumFillOperation.name
    assert card._option_checkboxes == {}


# ── Ready banner ─────────────────────────────────────────────────────────────


def test_ready_banner_appears_only_after_done_and_all_green(
    station, operations_config, qtbot
):
    """The ready banner shows only once a run finishes done AND every condition holds."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    all_green_state = {
        "magnet_z": {"magnet_state": "standby"},
        "magnet_y": {"magnet_state": "standby"},
        "temperature_vti": {"temperature": 290.0},
    }
    ctx = {"state": all_green_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}

    # Not done yet -> no banner, even though conditions currently hold.
    card.on_states_updated(all_green_state, ctx)
    assert card._ready_banner.isHidden()

    # Start a run the way the card really does: the factory-built instance is
    # what the orchestrator runs AND what confirm_operation() mutates — the
    # card must re-bind its checklist to it (regression: confirming only ever
    # lands on the running instance, never the display instance; without the
    # re-bind the needle-valve row could never turn green and the banner
    # could never show for exactly the operation that needs it).
    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})

    # The run finishes "done", but needle_valve_confirmed has never been
    # confirmed -> not all-green -> banner stays hidden.
    card._on_run_finished({"procedure": SampleLoadOperation.name, "status": "done"})
    card.on_states_updated(all_green_state, ctx)
    assert card._ready_banner.isHidden()

    # What Orchestrator.confirm_operation("needle_valve") does to the ACTIVE
    # operation — note: the running instance, not card._display_instance.
    _confirm_every_step(running)
    card.on_states_updated(all_green_state, ctx)
    assert not card._ready_banner.isHidden()
    assert card._ready_banner.text() == f"✓ {SampleLoadOperation.ready_message}"

    # A condition stops holding -> banner clears.
    not_green_state = dict(all_green_state, magnet_z={"magnet_state": "holding"})
    ctx = {"state": not_green_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(not_green_state, ctx)
    assert card._ready_banner.isHidden()


def test_ready_banner_clears_when_new_run_starts(station, operations_config, qtbot):
    """Starting a new run clears the ready banner even if the last run was done+all-green."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    _confirm_every_step(card._display_instance)

    all_green_state = {
        "magnet_z": {"magnet_state": "standby"},
        "magnet_y": {"magnet_state": "standby"},
        "temperature_vti": {"temperature": 290.0},
    }
    ctx = {"state": all_green_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card._on_run_finished({"procedure": SampleLoadOperation.name, "status": "done"})
    card.on_states_updated(all_green_state, ctx)
    assert not card._ready_banner.isHidden()

    card._on_run_started({"procedure": SampleLoadOperation.name})
    assert card._ready_banner.isHidden()


# ── Mid-run ready banner for a hold-phase operation: SampleLoadOperation
# declares hold_for_operator = True, so the banner may show WHILE the run
# is still active, not only after it finishes done. ─────────────────────────


def test_ready_banner_shows_mid_run_for_hold_phase_operation_once_conditions_hold(
    station, operations_config, qtbot
):
    """A hold-phase operation's ready banner shows mid-run, before Finish is clicked."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]
    assert card._display_instance.name == SampleLoadOperation.name
    assert card._display_instance.hold_for_operator is True

    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})
    assert card._ready_banner.isHidden()  # no state snapshot evaluated for THIS run yet

    _confirm_every_step(running)
    all_green_state = {
        "magnet_z": {"magnet_state": "standby"},
        "magnet_y": {"magnet_state": "standby"},
        "temperature_vti": {"temperature": 290.0},
    }
    ctx = {"state": all_green_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}

    # Still running (no run_finished at all) -> banner shows anyway.
    card.on_states_updated(all_green_state, ctx)
    assert not card._ready_banner.isHidden()
    assert card._ready_banner.text() == f"✓ {SampleLoadOperation.ready_message}"


def test_ready_banner_hidden_mid_run_while_conditions_unmet(
    station, operations_config, qtbot
):
    """A hold-phase operation's ready banner stays hidden mid-run while a condition fails."""
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        None,
        operations_config,
        None,
        None,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    running = card._factory("tester")
    card._pending_instance = running
    card._on_run_started({"procedure": SampleLoadOperation.name})
    # needle_valve never confirmed -> needle_valve_confirmed condition fails.
    not_green_state = {
        "magnet_z": {"get_field": 0.0},
        "magnet_y": {"get_field": 0.0},
        "temperature_vti": {"temperature": 290.0},
    }
    ctx = {"state": not_green_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(not_green_state, ctx)
    assert card._ready_banner.isHidden()


# ── Immediate finish + status line + unmet-postcondition warning (design
# doc operation-concurrency-and-error-scoping.md §2) ─────────────────────


def test_finish_click_immediately_shows_disabled_finishing_state(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """Clicking Finish disables the button into 'Finishing…' before run_finished arrives."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._on_run_started({"procedure": HeliumFillOperation.name})
    assert card._action_btn.isEnabled()

    card._action_btn.click()
    mock_orch.finish_operation.assert_called_once()
    assert card._finishing is True
    assert not card._action_btn.isEnabled()
    assert "Finishing" in card._action_btn.text()

    # run_finished flips it back to idle, whatever the terminal status.
    card._on_run_finished(
        {"procedure": HeliumFillOperation.name, "status": "done", "postconditions_unmet": []}
    )
    assert card._finishing is False
    assert card._action_btn.isEnabled()
    assert card._action_btn.text() == "Helium Fill…"


def test_status_label_shows_operation_status_only_while_running(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """on_operation_status() updates the label only for the currently-running card."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    # Not running yet -> ignored.
    panel.on_operation_status("Ramping magnet_z to 0 T")
    assert card._status_label.isHidden()

    card._on_run_started({"procedure": HeliumFillOperation.name})
    panel.on_operation_status("Ramping magnet_z to 0 T")
    assert not card._status_label.isHidden()
    assert card._status_label.toolTip() == "Ramping magnet_z to 0 T"

    card._on_run_finished({"procedure": HeliumFillOperation.name, "status": "done"})
    assert card._status_label.isHidden()


def test_unmet_postcondition_warning_shown_on_run_finished(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """A non-empty postconditions_unmet on run_finished shows the warning badge."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = OperationsPanel(
        station,
        mock_orch,
        cryogenics_config,
        {},
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)
    card = panel._cards[0]

    card._on_run_started({"procedure": HeliumFillOperation.name})
    assert card._postcondition_warning.isHidden()

    card._on_run_finished(
        {
            "procedure": HeliumFillOperation.name,
            "status": "done",
            "postconditions_unmet": ["refresh_slow"],
        }
    )
    assert not card._postcondition_warning.isHidden()
    assert "refresh_slow" in card._postcondition_warning.text()

    # A clean finish (or a fresh run) clears it.
    card._on_run_started({"procedure": HeliumFillOperation.name})
    assert card._postcondition_warning.isHidden()
    card._on_run_finished(
        {"procedure": HeliumFillOperation.name, "status": "done", "postconditions_unmet": []}
    )
    assert card._postcondition_warning.isHidden()
