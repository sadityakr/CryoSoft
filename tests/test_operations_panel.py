# ---
# description: |
#   Behavior tests for cryosoft.gui.operations_panel (Phase 6,
#   docs/plans/cryogenics-logbook.md §12): panel presence gated on
#   cryogenics/operations config, live cryogenics-status readouts from a
#   synthetic states_updated snapshot, on-screen geometry when selected in
#   MonitorWindow's bottom-right quadrant, generic OperationCard
#   construction (helium fill + sample change), the readiness checklist
#   flipping on a snapshot change, the start/finish button toggling on
#   run_started/run_finished, the operator-confirmation checkbox calling
#   confirm_operation(), the ready banner appearing only once a run is done
#   and every condition holds, and a cryogenics-less/operations-only panel
#   building only the sample-change card.
# last_updated: 2026-07-19
# ---

"""Behavior tests for OperationsPanel / OperationCard / OperatorDialog."""

from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QDialog, QLabel, QScrollArea

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station, read_cryogenics_config, read_operations_config
from cryosoft.gui import operations_panel as operations_panel_module
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.operations_panel import OperationCard, OperationsPanel
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.procedures.operations.sample_change import SampleChangeOperation
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


def test_operations_panel_without_cryogenics_but_with_operations_builds_sample_change_only(
    station, orchestrator, operations_config, qtbot
):
    """No cryogenics block, but an operations: block -> panel with only the sample-change card."""
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
    assert len(cards) == 1
    assert cards[0]._display_instance.name == SampleChangeOperation.name
    # No cryogenics status section built.
    assert win._operations_panel.findChild(QLabel, "cryo_helium_level_label") is None


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

    helium_label = win._operations_panel.findChild(QLabel, "cryo_helium_level_label")
    fill_card = win.findChild(OperationCard, "operation_card_helium_fill")
    assert helium_label is not None and fill_card is not None
    assert _fully_inside(viewport, helium_label)
    assert _fully_inside(viewport, fill_card)


# ── Live level readouts (cryogenics status section) ────────────────────────────


def test_operations_panel_shows_levels_from_synthetic_states_updated(
    station, orchestrator, cryogenics_config, stores, qtbot
):
    """on_states_updated() with a synthetic snapshot updates the He/N2 readouts."""
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

    level_vi = cryogenics_config["level_vi"]
    panel.on_states_updated({level_vi: {"helium_level": 62.5, "nitrogen_level": 44.0}})

    assert "62.5" in panel._helium_label.text()
    assert "44.0" in panel._nitrogen_label.text()


# ── Generic card construction ──────────────────────────────────────────────────


def test_cards_built_for_fill_and_sample_change_on_sim_cryostat(
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
    assert names == [HeliumFillOperation.name, SampleChangeOperation.name]
    assert panel.findChild(OperationCard, "operation_card_helium_fill") is not None
    assert panel.findChild(OperationCard, "operation_card_sample_change") is not None


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

    zero_state = {"magnet_z": {"get_field": 0.0}, "magnet_y": {"get_field": 0.0}}
    ctx = {"state": zero_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(zero_state, ctx)
    assert not icon_label.pixmap().isNull()
    assert "0.00" in detail_label.text()

    nonzero_state = {"magnet_z": {"get_field": 1.5}, "magnet_y": {"get_field": 0.0}}
    ctx = {"state": nonzero_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card.on_states_updated(nonzero_state, ctx)
    assert "magnet_z at 1.50 T" == detail_label.text()


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


# ── Operator confirmations ────────────────────────────────────────────────────


def test_confirmation_checkbox_calls_confirm_operation(
    station, operations_config, qtbot
):
    """Checking a declared confirmation checkbox calls confirm_operation(key) and disables itself."""
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
    assert card._display_instance.name == SampleChangeOperation.name

    assert card._confirmations_row.isHidden()
    card._on_run_started({"procedure": SampleChangeOperation.name})
    assert not card._confirmations_row.isHidden()

    checkbox = card._confirm_checkboxes["needle_valve"]
    assert checkbox.isEnabled()
    checkbox.setChecked(True)

    mock_orch.confirm_operation.assert_called_once_with("needle_valve")
    assert not checkbox.isEnabled()


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
        "magnet_z": {"get_field": 0.0},
        "magnet_y": {"get_field": 0.0},
        "temperature_vti": {"temperature": 300.0},
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
    card._on_run_started({"procedure": SampleChangeOperation.name})

    # The run finishes "done", but needle_valve_confirmed has never been
    # confirmed -> not all-green -> banner stays hidden.
    card._on_run_finished({"procedure": SampleChangeOperation.name, "status": "done"})
    card.on_states_updated(all_green_state, ctx)
    assert card._ready_banner.isHidden()

    # What Orchestrator.confirm_operation("needle_valve") does to the ACTIVE
    # operation — note: the running instance, not card._display_instance.
    running.confirm("needle_valve")
    card.on_states_updated(all_green_state, ctx)
    assert not card._ready_banner.isHidden()
    assert card._ready_banner.text() == f"✓ {SampleChangeOperation.ready_message}"

    # A condition stops holding -> banner clears.
    not_green_state = dict(all_green_state, magnet_z={"get_field": 1.0})
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
    card._display_instance.confirm("needle_valve")

    all_green_state = {
        "magnet_z": {"get_field": 0.0},
        "magnet_y": {"get_field": 0.0},
        "temperature_vti": {"temperature": 300.0},
    }
    ctx = {"state": all_green_state, "now_unix": 0.0, "consumption_rate_pct_per_h": None}
    card._on_run_finished({"procedure": SampleChangeOperation.name, "status": "done"})
    card.on_states_updated(all_green_state, ctx)
    assert not card._ready_banner.isHidden()

    card._on_run_started({"procedure": SampleChangeOperation.name})
    assert card._ready_banner.isHidden()
