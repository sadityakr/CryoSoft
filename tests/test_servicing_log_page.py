# ---
# description: |
#   Behavior tests for cryosoft.gui.servicing_log_page: one table per
#   declared log kind with columns derived from its LogKindSpec, Add/Edit/
#   Delete round-tripping through ServicingLogStore's revision model, the
#   "edited" marker, the read-only operations table, and refresh() behavior
#   (Phase 5, docs/plans/cryogenics-logbook.md §10); plus the unified
#   "servicing" kind's dedicated timeline table — chronological sort, data-
#   derived filter chips, the recording detail dialog, and the two CSV export
#   helpers (Phase 4, docs/plans/unified-servicing-log-and-run-recording.md
#   §4).
# last_updated: 2026-07-23
# ---

"""Behavior tests for ServicingLogPage."""

import json

import pytest
from PyQt6.QtWidgets import QDialog, QPushButton, QTableWidget, QWidget

from cryosoft.gui import servicing_log_page as slp_module
from cryosoft.gui.log_panel import LogPanel
from cryosoft.gui.servicing_log_page import (
    ServicingLogPage,
    distinct_entry_kinds,
    filter_entries_by_kind,
    load_recording_sidecar,
    recording_csv_rows,
    servicing_table_csv_rows,
    sorted_servicing_entries,
    write_recording_csv,
    write_servicing_table_csv,
)
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


# ── The unified "servicing" kind: timeline, filter chips, detail, exports ───


def _servicing_values(**overrides):
    values = {
        "entry_kind": "helium_fill",
        "person": "jdoe",
        "start_utc": "2026-07-23T10:00:00+00:00",
        "end_utc": "2026-07-23T11:00:00+00:00",
        "helium_start_pct": 40.0,
        "helium_end_pct": 90.0,
        "ln2_start_pct": 60.0,
        "ln2_end_pct": 60.0,
        "notes": "",
        "recording": "",
        "origin": "manual",
    }
    values.update(overrides)
    return values


def test_unified_table_sorted_newest_first_by_start_utc(store, log_panel, qtbot):
    """Mixed entry kinds render in ONE table, sorted by parsed start_utc descending."""
    store.add_entry(
        "servicing",
        _servicing_values(entry_kind="helium_fill", start_utc="2026-07-20T08:00:00+00:00"),
    )
    store.add_entry(
        "servicing",
        _servicing_values(entry_kind="sample_change", start_utc="2026-07-22T08:00:00+00:00"),
    )
    # A manual entry with no start_utc falls back to created_utc for sorting,
    # but any manual entry created in this test necessarily sorts after both
    # machine-dated entries above (created "now", after 2026-07-22).
    store.add_entry("servicing", _servicing_values(entry_kind="manual", start_utc=""))

    page = ServicingLogPage(store, ["servicing"], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)
    table = page.findChild(QTableWidget, "servicing_table_servicing")
    assert table is not None
    assert table.rowCount() == 3

    field_names = list(DECLARED_LOG_KINDS["servicing"].fields)
    kind_col = field_names.index("entry_kind")
    # Newest first: the fallback-sorted manual entry (created "now") first,
    # then sample_change (07-22), then helium_fill (07-20).
    assert table.item(0, kind_col).text() == "manual"
    assert table.item(1, kind_col).text() == "sample_change"
    assert table.item(2, kind_col).text() == "helium_fill"


def test_filter_chips_narrow_the_table_live(store, log_panel, qtbot):
    store.add_entry("servicing", _servicing_values(entry_kind="helium_fill"))
    store.add_entry("servicing", _servicing_values(entry_kind="sample_change"))
    store.add_entry("servicing", _servicing_values(entry_kind="sample_change"))

    page = ServicingLogPage(store, ["servicing"], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)
    table = page.findChild(QTableWidget, "servicing_table_servicing")
    assert table.rowCount() == 3

    all_chip = page.findChild(QPushButton, "servicing_chip_All")
    fill_chip = page.findChild(QPushButton, "servicing_chip_helium_fill")
    sample_chip = page.findChild(QPushButton, "servicing_chip_sample_change")
    assert all_chip is not None
    assert fill_chip is not None
    assert sample_chip is not None

    fill_chip.click()
    assert table.rowCount() == 1

    sample_chip.click()
    assert table.rowCount() == 2

    all_chip.click()
    assert table.rowCount() == 3


def test_view_recording_button_enabled_only_for_rows_with_a_recording(store, log_panel, qtbot):
    store.add_entry("servicing", _servicing_values(recording=""))
    store.add_entry("servicing", _servicing_values(recording="run_1.json"))

    page = ServicingLogPage(store, ["servicing"], log_panel, get_current_person=lambda: "")
    qtbot.addWidget(page)
    table = page.findChild(QTableWidget, "servicing_table_servicing")
    view_btn = page.findChild(QPushButton, "servicing_view_recording_btn")
    assert view_btn is not None

    table.selectRow(0)  # newest-first: the one with a recording (created last)
    assert view_btn.isEnabled()

    table.selectRow(1)
    assert not view_btn.isEnabled()


def test_add_dialog_creates_manual_servicing_entry_with_manual_origin(
    store, log_panel, qtbot, monkeypatch
):
    """The Add dialog's field defaults (unedited) give origin="manual", entry_kind="manual"."""
    monkeypatch.setattr(
        slp_module, "ServicingLogEntryDialog", _make_fake_entry_dialog(_servicing_values(notes="x"))
    )
    page = ServicingLogPage(store, ["servicing"], log_panel, get_current_person=lambda: "Alice")
    qtbot.addWidget(page)
    add_btn = page.findChild(QPushButton, "servicing_add_btn_servicing")
    assert add_btn is not None

    add_btn.click()

    entries = store.entries("servicing")
    assert len(entries) == 1
    assert entries[0].values["origin"] == "manual"
    assert entries[0].values["notes"] == "x"

    # Spec default check (independent of the dialog): an untouched Add form
    # defaults BOTH origin and entry_kind to "manual".
    spec = DECLARED_LOG_KINDS["servicing"]
    assert spec.fields["origin"].default == "manual"
    assert spec.fields["entry_kind"].default == "manual"


# ── Pure CSV/sort/filter helper functions (no Qt) ───────────────────────────


def test_sorted_servicing_entries_orders_by_parsed_start_utc(store):
    old = store.add_entry(
        "servicing", _servicing_values(start_utc="2026-01-01T00:00:00+00:00")
    )
    new = store.add_entry(
        "servicing", _servicing_values(start_utc="2026-06-01T00:00:00+00:00")
    )
    ordered = sorted_servicing_entries(store.entries("servicing"))
    assert [e.entry_id for e in ordered] == [new.entry_id, old.entry_id]


def test_distinct_entry_kinds_derives_from_data():
    entries = [
        _StubEntry({"entry_kind": "helium_fill"}),
        _StubEntry({"entry_kind": "sample_change"}),
        _StubEntry({"entry_kind": "helium_fill"}),
        _StubEntry({"entry_kind": ""}),
    ]
    assert distinct_entry_kinds(entries) == ["helium_fill", "sample_change"]


def test_filter_entries_by_kind():
    entries = [
        _StubEntry({"entry_kind": "helium_fill"}),
        _StubEntry({"entry_kind": "sample_change"}),
    ]
    assert filter_entries_by_kind(entries, None) == entries
    assert filter_entries_by_kind(entries, "All") == entries
    filtered = filter_entries_by_kind(entries, "helium_fill")
    assert len(filtered) == 1
    assert filtered[0].values["entry_kind"] == "helium_fill"


def test_servicing_table_csv_rows_and_write(tmp_path, store):
    store.add_entry("servicing", _servicing_values(entry_kind="helium_fill", person="Alice"))
    entries = store.entries("servicing")
    field_names = list(DECLARED_LOG_KINDS["servicing"].fields)

    header, rows = servicing_table_csv_rows(entries, field_names)
    assert header == field_names
    assert len(rows) == 1
    assert rows[0][field_names.index("person")] == "Alice"

    out = tmp_path / "table.csv"
    write_servicing_table_csv(out, entries, field_names)
    text = out.read_text(encoding="utf-8")
    assert "person" in text.splitlines()[0]
    assert "Alice" in text


def test_recording_csv_rows_and_write(tmp_path):
    recording = {
        "unix_time": [0.0, 60.0, 120.0],
        "channels": {"vti.temperature": [4.2, 4.3, 4.1], "magnet.field": [0.0, 0.1, 0.2]},
    }
    header, rows = recording_csv_rows(recording)
    assert header == ["unix_time", "utc", "magnet.field", "vti.temperature"]
    assert len(rows) == 3
    assert rows[0][0] == 0.0
    assert rows[0][1].startswith("1970-01-01T00:00:00")

    out = tmp_path / "recording.csv"
    write_recording_csv(out, recording)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # header + 3 rows


def test_load_recording_sidecar_reads_synthetic_file(tmp_path):
    root = tmp_path / "servicing"
    store = ServicingLogStore(root, "sim_cryostat")
    sidecar = store.recordings_path("run_1.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    payload = {"unix_time": [0.0, 1.0], "channels": {"vti.temperature": [4.2, 4.3]}}
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_recording_sidecar(store, "run_1.json")
    assert loaded == payload


def test_load_recording_sidecar_tolerates_missing_file(tmp_path):
    root = tmp_path / "servicing"
    store = ServicingLogStore(root, "sim_cryostat")
    assert load_recording_sidecar(store, "does_not_exist.json") is None


def test_load_recording_sidecar_tolerates_corrupt_file(tmp_path):
    root = tmp_path / "servicing"
    store = ServicingLogStore(root, "sim_cryostat")
    sidecar = store.recordings_path("bad.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{not json", encoding="utf-8")

    assert load_recording_sidecar(store, "bad.json") is None


def test_detail_dialog_plots_synthetic_sidecar(store, log_panel, qtbot):
    """Selecting a recorded row and clicking View recording loads/plots the sidecar."""
    from cryosoft.gui.servicing_log_page import ServicingRecordingDialog

    sidecar = store.recordings_path("run_1.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"unix_time": [0.0, 1.0], "channels": {"vti.temperature": [4.2, 4.3]}}),
        encoding="utf-8",
    )
    entry = store.add_entry("servicing", _servicing_values(recording="run_1.json"))

    dialog = ServicingRecordingDialog(entry, store)
    qtbot.addWidget(dialog)
    plot = dialog.findChild(QWidget, "servicing_detail_plot")
    assert plot is not None
    export_btn = dialog.findChild(QPushButton, "servicing_detail_export_recording_btn")
    assert export_btn.isEnabled()


def test_detail_dialog_tolerates_missing_sidecar(store, log_panel, qtbot):
    entry = store.add_entry("servicing", _servicing_values(recording="missing.json"))

    from cryosoft.gui.servicing_log_page import ServicingRecordingDialog

    dialog = ServicingRecordingDialog(entry, store)
    qtbot.addWidget(dialog)
    export_btn = dialog.findChild(QPushButton, "servicing_detail_export_recording_btn")
    assert export_btn is not None
    assert not export_btn.isEnabled()


class _StubEntry:
    """Minimal stand-in for ServiceLogEntry, values-only, for pure-function tests."""

    def __init__(self, values: dict) -> None:
        self.values = values
