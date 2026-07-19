# ---
# description: |
#   Behavior tests for cryosoft.gui.servicing_log_page (Phase 5,
#   docs/plans/cryogenics-logbook.md §10): one table per declared log kind
#   with columns derived from its LogKindSpec, Add/Edit/Delete round-tripping
#   through ServicingLogStore's revision model, the "edited" marker, the
#   read-only operations table, and refresh() behavior.
# last_updated: 2026-07-19
# ---

"""Behavior tests for ServicingLogPage."""

import pytest
from PyQt6.QtWidgets import QDialog, QPushButton, QTableWidget

from cryosoft.gui import servicing_log_page as slp_module
from cryosoft.gui.log_panel import LogPanel
from cryosoft.gui.servicing_log_page import ServicingLogPage
from cryosoft.session.servicing_log import DECLARED_LOG_KINDS, ServicingLogStore


@pytest.fixture
def store(tmp_path):
    return ServicingLogStore(tmp_path / "servicing", "sim_cryostat")


@pytest.fixture
def log_panel():
    # Not registered with qtbot: ServicingLogPage reparents it into its
    # splitter, so qtbot.addWidget(page) alone owns teardown of the whole
    # tree — registering both would double-close an already-deleted widget.
    return LogPanel()


def _make_fake_entry_dialog(values: dict, revised_by: str = ""):
    """Build a stand-in for ServicingLogEntryDialog that auto-accepts fixed values."""

    class _FakeEntryDialog:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def collected_values(self):
            return dict(values)

        def revised_by(self):
            return revised_by

    return _FakeEntryDialog


def _make_fake_delete_dialog(revised_by: str = "Deleter", accept: bool = True):
    class _FakeDeleteDialog:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

        def revised_by(self):
            return revised_by

    return _FakeDeleteDialog


_CRYOGENICS_VALUES_1 = {
    "person": "Alice",
    "start_utc": "2026-07-19T08:00:00+00:00",
    "end_utc": "2026-07-19T09:00:00+00:00",
    "helium_start_pct": 20.0,
    "helium_end_pct": 90.0,
    "ln2_filled": False,
    "notes": "routine fill",
}
_CRYOGENICS_VALUES_2 = {
    "person": "Bob",
    "start_utc": "2026-07-18T08:00:00+00:00",
    "end_utc": "2026-07-18T09:00:00+00:00",
    "helium_start_pct": 15.0,
    "helium_end_pct": 88.0,
    "ln2_filled": True,
    "notes": "",
}


# ── Table rendering ─────────────────────────────────────────────────────────


def test_table_renders_one_row_per_entry_with_columns_from_spec(store, log_panel, qtbot):
    """Column headers come from the LogKindSpec's fields; rows are newest-first."""
    store.add_entry("cryogenics", _CRYOGENICS_VALUES_2, source="manual")
    store.add_entry("cryogenics", _CRYOGENICS_VALUES_1, source="manual")

    page = ServicingLogPage(store, ["cryogenics"], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)

    table = page.findChild(QTableWidget, "servicing_table_cryogenics")
    assert table is not None

    spec = DECLARED_LOG_KINDS["cryogenics"]
    field_names = list(spec.fields)
    expected_headers = [
        (f"{name} ({spec.fields[name].unit})" if spec.fields[name].unit else name)
        for name in field_names
    ] + ["Edited"]
    actual_headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
    assert actual_headers == expected_headers

    assert table.rowCount() == 2
    # created_utc ordering is entry-creation order, not the values' own
    # start_utc — both were added in this call order (values_2 then values_1),
    # so entries() (newest created_utc first) puts values_1 first.
    person_col = field_names.index("person")
    assert table.item(0, person_col).text() == "Alice"
    assert table.item(1, person_col).text() == "Bob"


def test_operations_table_has_no_action_buttons(store, log_panel, qtbot):
    """The read-only 'operations' kind gets a table but no Add/Edit/Delete buttons."""
    store.append_machine_entry(
        "operations",
        {
            "operation": "Helium Fill",
            "params": "{}",
            "started_utc": "2026-07-19T08:00:00+00:00",
            "finished_utc": "2026-07-19T09:00:00+00:00",
            "status": "done",
            "verified": True,
            "reason": "",
        },
    )
    page = ServicingLogPage(store, [], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)

    table = page.findChild(QTableWidget, "servicing_table_operations")
    assert table is not None
    assert table.rowCount() == 1
    assert page.findChild(QPushButton, "servicing_add_btn_operations") is None


def test_no_store_builds_page_with_no_tables(log_panel, qtbot):
    """servicing_store=None (no cryogenics: block) shows the LogPanel, no tables."""
    page = ServicingLogPage(None, [], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)

    assert page.findChild(QTableWidget, "servicing_table_cryogenics") is None
    assert page.findChild(QTableWidget, "servicing_table_operations") is None
    assert page.findChild(LogPanel, "log_panel") is log_panel


# ── Add / Edit round-trip ────────────────────────────────────────────────────


def test_add_entry_round_trips_value_into_store(store, log_panel, qtbot, monkeypatch):
    """Add entry -> the dialog's collected values land in the store and the table."""
    monkeypatch.setattr(
        slp_module, "ServicingLogEntryDialog", _make_fake_entry_dialog(_CRYOGENICS_VALUES_1)
    )

    page = ServicingLogPage(store, ["cryogenics"], log_panel, get_current_person=lambda: "Alice")
    qtbot.addWidget(page)

    table = page.findChild(QTableWidget, "servicing_table_cryogenics")
    add_btn = page.findChild(QPushButton, "servicing_add_btn_cryogenics")
    assert add_btn is not None
    assert table.rowCount() == 0

    add_btn.click()

    assert table.rowCount() == 1
    entries = store.entries("cryogenics")
    assert len(entries) == 1
    assert entries[0].values["person"] == "Alice"
    assert entries[0].values["helium_end_pct"] == 90.0


def test_edit_entry_updates_store_and_shows_edited_marker(store, log_panel, qtbot, monkeypatch):
    """Edit -> revise_entry() is called and the row gains the 'edited' marker."""
    entry = store.add_entry("cryogenics", _CRYOGENICS_VALUES_1, source="manual")

    page = ServicingLogPage(store, ["cryogenics"], log_panel, get_current_person=lambda: "Carol")
    qtbot.addWidget(page)

    table = page.findChild(QTableWidget, "servicing_table_cryogenics")
    assert table.item(0, len(DECLARED_LOG_KINDS["cryogenics"].fields)).text() == ""

    edited_values = dict(_CRYOGENICS_VALUES_1)
    edited_values["notes"] = "corrected after the fact"
    monkeypatch.setattr(
        slp_module,
        "ServicingLogEntryDialog",
        _make_fake_entry_dialog(edited_values, revised_by="Carol"),
    )

    table.selectRow(0)
    edit_btn = page.findChild(QPushButton, "servicing_edit_btn_cryogenics")
    assert edit_btn.isEnabled()
    edit_btn.click()

    assert table.rowCount() == 1  # still one live entry (a revision, not a new one)
    notes_col = list(DECLARED_LOG_KINDS["cryogenics"].fields).index("notes")
    assert table.item(0, notes_col).text() == "corrected after the fact"
    edited_col = len(DECLARED_LOG_KINDS["cryogenics"].fields)
    assert table.item(0, edited_col).text() == "edited"

    history = store.revisions("cryogenics", entry.entry_id)
    assert len(history) == 2
    assert history[-1].revised_by == "Carol"


def test_delete_entry_hides_row_but_keeps_history(store, log_panel, qtbot, monkeypatch):
    """Delete -> delete_entry() tombstones the entry; it drops out of the table."""
    entry = store.add_entry("cryogenics", _CRYOGENICS_VALUES_1, source="manual")
    page = ServicingLogPage(store, ["cryogenics"], log_panel, get_current_person=lambda: "Dan")
    qtbot.addWidget(page)

    monkeypatch.setattr(slp_module, "_DeleteConfirmDialog", _make_fake_delete_dialog("Dan"))

    table = page.findChild(QTableWidget, "servicing_table_cryogenics")
    table.selectRow(0)
    delete_btn = page.findChild(QPushButton, "servicing_delete_btn_cryogenics")
    assert delete_btn.isEnabled()
    delete_btn.click()

    assert table.rowCount() == 0
    assert store.entries("cryogenics") == []
    history = store.revisions("cryogenics", entry.entry_id)
    assert len(history) == 2
    assert history[-1].deleted is True
    assert history[-1].revised_by == "Dan"


# ── refresh() ─────────────────────────────────────────────────────────────────


def test_refresh_picks_up_entries_written_outside_the_dialog_flow(store, log_panel, qtbot):
    """refresh() re-reads the store — e.g. after a run_finished-driven write."""
    page = ServicingLogPage(store, ["cryogenics"], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)
    table = page.findChild(QTableWidget, "servicing_table_cryogenics")
    assert table.rowCount() == 0

    store.add_entry("cryogenics", _CRYOGENICS_VALUES_1, source="operation", run_id="run_1")
    assert table.rowCount() == 0  # not auto-refreshed

    page.refresh()
    assert table.rowCount() == 1
