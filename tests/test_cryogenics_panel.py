# ---
# description: |
#   Behavior tests for cryosoft.gui.cryogenics_panel (Phase 5,
#   docs/plans/cryogenics-logbook.md §10): panel presence gated on config,
#   live level readouts from a synthetic states_updated snapshot, on-screen
#   geometry when selected in MonitorWindow's bottom-right quadrant, and the
#   Fill helium / Stop filling control against a mock Orchestrator.
# last_updated: 2026-07-19
# ---

"""Behavior tests for CryogenicsPanel."""

from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QDialog, QLabel, QPushButton, QScrollArea

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station, read_cryogenics_config
from cryosoft.gui import cryogenics_panel as cryogenics_panel_module
from cryosoft.gui.cryogenics_panel import CryogenicsPanel
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
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


def test_cryogenics_panel_absent_without_config(station, orchestrator, qtbot):
    """No cryogenics kwargs -> no panel, no "Cryogenics" selector entry."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    win.show()

    assert win._cryogenics_enabled is False
    assert win._cryogenics_panel is None
    items = [win._devices_log_selector.itemText(i) for i in range(win._devices_log_selector.count())]
    assert items == ["Other Devices"]


def test_cryogenics_panel_present_with_config(station, orchestrator, cryogenics_config, stores, qtbot):
    """A wired cryogenics config + stores + level VI builds the panel and selector entry."""
    helium_store, servicing_store = stores
    win = MonitorWindow(
        station,
        orchestrator,
        cryogenics_config=cryogenics_config,
        helium_store=helium_store,
        servicing_store=servicing_store,
        servicing_log_kinds=["cryogenics"],
    )
    qtbot.addWidget(win)
    win.show()

    assert win._cryogenics_enabled is True
    assert win._cryogenics_panel is not None
    items = [win._devices_log_selector.itemText(i) for i in range(win._devices_log_selector.count())]
    assert items == ["Other Devices", "Cryogenics"]


def test_cryogenics_panel_geometry_fully_visible_when_selected(
    station, orchestrator, cryogenics_config, stores, qtbot
):
    """Selecting Cryogenics shows the panel fully inside its scroll viewport."""
    helium_store, servicing_store = stores
    win = MonitorWindow(
        station,
        orchestrator,
        cryogenics_config=cryogenics_config,
        helium_store=helium_store,
        servicing_store=servicing_store,
        servicing_log_kinds=["cryogenics"],
    )
    qtbot.addWidget(win)
    win.resize(1280, 900)
    win.show()
    qtbot.waitExposed(win)

    win._devices_log_selector.setCurrentIndex(1)
    assert win._devices_log_stack.currentIndex() == 1

    scroll = win.findChild(QScrollArea, "cryogenics_scroll")
    assert scroll is not None
    viewport = scroll.viewport()
    assert scroll.horizontalScrollBar().maximum() == 0

    helium_label = win._cryogenics_panel.findChild(QLabel, "cryo_helium_level_label")
    fill_btn = win._cryogenics_panel.findChild(QPushButton, "cryo_fill_btn")
    assert helium_label is not None and fill_btn is not None
    assert _fully_inside(viewport, helium_label)
    assert _fully_inside(viewport, fill_btn)


# ── Live level readouts ────────────────────────────────────────────────────────


def test_cryogenics_panel_shows_levels_from_synthetic_states_updated(
    station, orchestrator, cryogenics_config, stores, qtbot
):
    """on_states_updated() with a synthetic snapshot updates the He/N2 readouts."""
    helium_store, servicing_store = stores
    panel = CryogenicsPanel(
        station,
        orchestrator,
        cryogenics_config,
        helium_store,
        servicing_store,
        get_data_dir=lambda: "/tmp",
    )
    qtbot.addWidget(panel)

    level_vi = cryogenics_config["level_vi"]
    panel.on_states_updated({level_vi: {"helium_level": 62.5, "nitrogen_level": 44.0}})

    assert "62.5" in panel._helium_label.text()
    assert "44.0" in panel._nitrogen_label.text()


# ── Fill helium / Stop filling against a mock Orchestrator ────────────────────


class _FakeFillDialog:
    """Stand-in for FillOperatorDialog that auto-accepts a fixed operator name."""

    def __init__(self, prefill: str = "", parent=None) -> None:
        self._name = prefill or "Test Operator"

    def exec(self):
        return QDialog.DialogCode.Accepted

    def operator_name(self) -> str:
        return self._name


def test_fill_button_submits_run_operation_with_person(
    station, cryogenics_config, stores, qtbot, monkeypatch, tmp_path
):
    """Clicking Fill helium constructs a HeliumFillOperation and calls run_operation."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)

    monkeypatch.setattr(cryogenics_panel_module, "FillOperatorDialog", _FakeFillDialog)

    panel = CryogenicsPanel(
        station,
        mock_orch,
        cryogenics_config,
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
        get_current_person=lambda: "J. Doe",
    )
    qtbot.addWidget(panel)

    panel._fill_btn.click()

    mock_orch.run_operation.assert_called_once()
    submitted = mock_orch.run_operation.call_args[0][0]
    assert isinstance(submitted, HeliumFillOperation)
    assert submitted.get_params()["person"] == "J. Doe"


def test_stop_filling_calls_finish_operation(station, cryogenics_config, stores, qtbot, tmp_path):
    """Once a fill is tracked as running, the button becomes Stop filling and calls finish_operation."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)

    panel = CryogenicsPanel(
        station,
        mock_orch,
        cryogenics_config,
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)

    assert panel._fill_btn.text() == "Fill helium"

    # Simulate the run_started manifest directly — a MagicMock's .connect()
    # does not deliver a real Qt signal, matching how CryogenicsPanel
    # connects run_started/run_finished directly (not through the window).
    panel._on_run_started({"procedure": HeliumFillOperation.name})
    assert panel._fill_btn.text() == "Stop filling"

    panel._fill_btn.click()
    mock_orch.finish_operation.assert_called_once()

    panel._on_run_finished({"procedure": HeliumFillOperation.name})
    assert panel._fill_btn.text() == "Fill helium"


def test_run_started_for_other_procedure_does_not_toggle_button(
    station, cryogenics_config, stores, qtbot, tmp_path
):
    """A run_started manifest for an unrelated procedure leaves the button alone."""
    helium_store, servicing_store = stores
    mock_orch = MagicMock(spec=Orchestrator)
    panel = CryogenicsPanel(
        station,
        mock_orch,
        cryogenics_config,
        helium_store,
        servicing_store,
        get_data_dir=lambda: str(tmp_path),
    )
    qtbot.addWidget(panel)

    panel._on_run_started({"procedure": "Field Sweep"})
    assert panel._fill_btn.text() == "Fill helium"
