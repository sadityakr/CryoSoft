# ---
# description: |
#   ServicingLogPage: MonitorWindow's page 2 ("Logs") — one table per
#   configured servicing-log kind (columns derived from its LogKindSpec,
#   newest first), the read-only "operations" audit-trail table, and the
#   relocated application LogPanel. Editable kinds (e.g. "cryogenics") get
#   Add entry / Edit / Delete / History buttons acting on the selected row;
#   dialogs are rendered from the kind's ParamSpecs via param_form.py, the
#   same machinery the procedure parameter form uses. One generic engine, N
#   declared kinds (docs/plans/cryogenics-logbook.md §6.1): a future log kind
#   gets a table here automatically, with zero new GUI code. The unified
#   "servicing" kind (docs/plans/unified-servicing-log-and-run-recording.md
#   §4, Phase 4) gets a dedicated `_ServicingLogTable` instead of the generic
#   per-kind table: one chronological timeline (sorted by parsed start_utc,
#   falling back to created_utc, newest first), filter chips derived from the
#   data's entry_kind values, a row-detail dialog with a lazily-loaded
#   recording plot, and two CSV exports (table + recording).
# entry_point: Not run directly. Hosted as MonitorWindow's page 2 (Logs),
#   built once in MonitorWindow._build_ui.
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13 (recording plot in the servicing detail dialog)
#   - cryosoft.gui.param_form (ParamSpec -> Qt widget mapping)
#   - cryosoft.gui.log_panel (LogPanel — composed, not created, here)
#   - cryosoft.session.servicing_log (LogKindSpec, DECLARED_LOG_KINDS,
#     ServicingLogStore)
#   - cryosoft.session.models (ServiceLogEntry)
# input: |
#   A ServicingLogStore (or None when the setup has no cryogenics: block),
#   the ordered list of log-kind keys this setup declares (from
#   Station.read_servicing_logs_config, forwarded by main.py), the
#   already-constructed LogPanel to host, and an optional callable returning
#   the "current person" string for attribution prefill.
# process: |
#   Builds one _LogKindTable per declared editable kind (except "servicing",
#   which gets `_ServicingLogTable` instead), plus the non-editable
#   "operations" table whenever a store is present (the operations stream has
#   no config toggle — it is on whenever any operation exists, i.e. whenever
#   the store exists). Add/Edit/Delete act on the store's revision-model API
#   directly and call refresh() afterwards; refresh() is also called by the
#   host on log-page-shown and run_finished — never on a timer. The CSV
#   export helpers (`sorted_servicing_entries`, `distinct_entry_kinds`,
#   `filter_entries_by_kind`, `write_servicing_table_csv`,
#   `load_recording_sidecar`, `write_recording_csv`) are plain functions with
#   no Qt dependency, so tests exercise them without driving a file dialog.
# output: |
#   A QWidget (page 2) with one QGroupBox+QTableWidget per log kind and the
#   hosted LogPanel beneath, in a resizable QSplitter. The "servicing" kind's
#   table additionally writes CSV files (table export, recording export) via
#   QFileDialog save prompts.
# last_updated: 2026-07-23
# ---

"""ServicingLogPage — MonitorWindow's page 2: servicing-log tables + the app log."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.gui import param_form
from cryosoft.gui.theme import (
    BTN_CLASS_DANGER,
    BTN_CLASS_PRIMARY,
    BTN_CLASS_SECONDARY,
    PLOT_SERIES,
)
from cryosoft.session.servicing_log import DECLARED_LOG_KINDS, LogKindSpec, ServicingLogStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cryosoft.gui.log_panel import LogPanel
    from cryosoft.session.models import ServiceLogEntry

logger = logging.getLogger(__name__)

__all__ = [
    "ServicingLogPage",
    "ServicingLogEntryDialog",
    "RevisionHistoryDialog",
    "ServicingRecordingDialog",
    "sorted_servicing_entries",
    "distinct_entry_kinds",
    "filter_entries_by_kind",
    "write_servicing_table_csv",
    "load_recording_sidecar",
    "write_recording_csv",
]

_ALL_KINDS_CHIP = "All"


# ── Pure helpers: sorting, filtering, CSV export (no Qt dependency) ─────────


def _parse_utc_to_unix(value: str) -> float | None:
    """Parse an ISO 8601 UTC string to a unix timestamp, or ``None``.

    Args:
        value: A candidate ISO 8601 string (may be empty or malformed — hand
            edits are not guaranteed well-formed).

    Returns:
        The unix timestamp, or ``None`` if ``value`` is empty or cannot be
        parsed.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _servicing_sort_key(entry: ServiceLogEntry) -> float:
    """Sort key for one ``servicing`` entry: parsed ``start_utc``, else ``created_utc``.

    Args:
        entry: A ``servicing``-kind entry.

    Returns:
        A unix timestamp usable for descending sort; ``float("-inf")`` when
        neither timestamp parses, so unparseable entries sink to the bottom
        instead of silently comparing equal to everything.
    """
    start = _parse_utc_to_unix(str(entry.values.get("start_utc", "")))
    if start is not None:
        return start
    created = _parse_utc_to_unix(entry.created_utc)
    return created if created is not None else float("-inf")


def sorted_servicing_entries(entries: Sequence[ServiceLogEntry]) -> list[ServiceLogEntry]:
    """Sort ``servicing`` entries newest-first by parsed timestamp.

    ISO 8601 UTC strings in the same offset format do sort correctly as
    plain strings, but hand-edited entries are not guaranteed to share one
    format, so this parses defensively via ``_servicing_sort_key`` rather
    than relying on string order.

    Args:
        entries: The entries to sort (not mutated).

    Returns:
        A new list, ordered by ``start_utc``/``created_utc`` descending.
    """
    return sorted(entries, key=_servicing_sort_key, reverse=True)


def distinct_entry_kinds(entries: Sequence[ServiceLogEntry]) -> list[str]:
    """Return the sorted, de-duplicated ``entry_kind`` values present in ``entries``.

    Args:
        entries: The entries to scan.

    Returns:
        Sorted unique ``entry_kind`` strings (empty values excluded). Used to
        derive the filter-chip set from the data, never hardcoded.
    """
    kinds = {str(entry.values.get("entry_kind", "")) for entry in entries}
    kinds.discard("")
    return sorted(kinds)


def filter_entries_by_kind(
    entries: Sequence[ServiceLogEntry], kind: str | None
) -> list[ServiceLogEntry]:
    """Filter ``servicing`` entries to one ``entry_kind`` (or pass all through).

    Args:
        entries: The entries to filter.
        kind: The ``entry_kind`` to keep, or ``None``/``_ALL_KINDS_CHIP`` for
            no filtering.

    Returns:
        The matching entries, preserving order.
    """
    if kind is None or kind == _ALL_KINDS_CHIP:
        return list(entries)
    return [entry for entry in entries if entry.values.get("entry_kind") == kind]


def servicing_table_csv_rows(
    entries: Sequence[ServiceLogEntry], field_names: Sequence[str]
) -> tuple[list[str], list[list[str]]]:
    """Build the header + row values for a "servicing" table CSV export.

    Args:
        entries: The (already filtered/sorted) entries to export.
        field_names: The kind's field names, in display column order.

    Returns:
        ``(header, rows)``: ``header`` is ``field_names`` as a list; each row
        is one entry's values, stringified in the same order.
    """
    header = list(field_names)
    rows = [[str(entry.values.get(name, "")) for name in field_names] for entry in entries]
    return header, rows


def write_servicing_table_csv(
    path: str | Path, entries: Sequence[ServiceLogEntry], field_names: Sequence[str]
) -> None:
    """Write a "servicing" table CSV export to ``path``.

    Args:
        path: Destination file path (overwritten if it exists).
        entries: The (already filtered/sorted) entries to export.
        field_names: The kind's field names, in display column order.

    Raises:
        OSError: If ``path`` cannot be written.
    """
    header, rows = servicing_table_csv_rows(entries, field_names)
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def load_recording_sidecar(store: ServicingLogStore, filename: str) -> dict[str, Any] | None:
    """Lazily load a recording sidecar, tolerant of a missing/corrupt file.

    Args:
        store: The active ``ServicingLogStore`` (resolves the sidecar path).
        filename: The entry's ``recording`` field (a bare basename).

    Returns:
        The parsed ``{"unix_time": [...], "channels": {...}}`` dict, or
        ``None`` if the file is missing, unreadable, or not valid JSON — never
        raises, so a stale/deleted sidecar never crashes the detail view.
    """
    path = store.recordings_path(filename)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("load_recording_sidecar: could not load %s (%s)", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("load_recording_sidecar: %s is not a JSON object", path)
        return None
    unix_time = data.get("unix_time")
    channels = data.get("channels")
    if not isinstance(unix_time, list) or not isinstance(channels, dict):
        logger.warning("load_recording_sidecar: %s has an unexpected shape", path)
        return None
    return data


def recording_csv_rows(recording: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Build the header + row values for a recording CSV export.

    Args:
        recording: A ``{"unix_time": [...], "channels": {name: [...]}}`` dict
            (as returned by ``load_recording_sidecar``).

    Returns:
        ``(header, rows)``: header is ``["unix_time", "utc"] + sorted(channel
        names)``; each row is one sample's unix time, its derived ISO 8601
        UTC string, and each channel's value at that index (``""`` if that
        channel's series is shorter than the sample count).
    """
    unix_time = recording.get("unix_time", [])
    channels = recording.get("channels", {})
    channel_names = sorted(channels)
    header = ["unix_time", "utc"] + channel_names
    rows: list[list[Any]] = []
    for i, t in enumerate(unix_time):
        try:
            iso = datetime.fromtimestamp(float(t), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            iso = ""
        row: list[Any] = [t, iso]
        for name in channel_names:
            series = channels.get(name, [])
            row.append(series[i] if i < len(series) else "")
        rows.append(row)
    return header, rows


def write_recording_csv(path: str | Path, recording: dict[str, Any]) -> None:
    """Write a recording CSV export (time column + one column per channel) to ``path``.

    Args:
        path: Destination file path (overwritten if it exists).
        recording: A ``{"unix_time": [...], "channels": {...}}`` dict.

    Raises:
        OSError: If ``path`` cannot be written.
    """
    header, rows = recording_csv_rows(recording)
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class ServicingLogEntryDialog(QDialog):
    """Add/Edit dialog rendered from a log kind's ``ParamSpec`` fields.

    Reuses ``param_form.build_form_layout`` — the same ParamSpec -> Qt-widget
    machinery the procedure parameter form uses (plan §10 / gui/README.md
    standard: "if a small extension is needed... it goes in param_form.py and
    nowhere else"). In edit mode an extra "Edited by" field is prepended,
    separate from the kind's own fields, to attribute the revision.

    Args:
        kind_spec: The log kind's declaration.
        initial_values: Existing field values to seed the form with (edit
            mode); ``None`` for a blank add form.
        is_edit: Whether this is an edit (adds the "Edited by" field and
            changes the window title/button label).
        revised_by_default: Prefill for the "Edited by" field.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        kind_spec: LogKindSpec,
        initial_values: dict[str, Any] | None = None,
        *,
        is_edit: bool = False,
        revised_by_default: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind_spec = kind_spec
        self.setWindowTitle(
            f"Edit {kind_spec.title} entry" if is_edit else f"Add {kind_spec.title} entry"
        )

        layout = QVBoxLayout(self)

        self._revised_by_edit: QLineEdit | None = None
        if is_edit:
            attribution_row = QHBoxLayout()
            attribution_row.addWidget(QLabel("Edited by:"))
            self._revised_by_edit = QLineEdit(revised_by_default)
            self._revised_by_edit.setObjectName("servicing_dialog_revised_by_input")
            attribution_row.addWidget(self._revised_by_edit)
            layout.addLayout(attribution_row)

        form, self._widgets = param_form.build_form_layout(kind_spec.fields)
        layout.addLayout(form)

        for name, widget in self._widgets.items():
            spec = kind_spec.fields[name]
            value = (
                initial_values[name]
                if initial_values is not None and name in initial_values
                else spec.default
            )
            param_form.set_widget_raw(widget, str(value))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def collected_values(self) -> dict[str, Any]:
        """Read every field's current value, typed per its ``ParamSpec``.

        Returns:
            ``{field_name: value}`` covering every field in the kind.

        Raises:
            ValueError: If a text field cannot be parsed as its declared type.
        """
        return {
            name: param_form.collect_value(widget, self._kind_spec.fields[name])
            for name, widget in self._widgets.items()
        }

    def revised_by(self) -> str:
        """Return the "Edited by" attribution text (edit mode only).

        Returns:
            The stripped attribution text, or ``""`` in add mode.
        """
        return self._revised_by_edit.text().strip() if self._revised_by_edit is not None else ""


class RevisionHistoryDialog(QDialog):
    """Read-only view of one entry's full revision history.

    Args:
        kind_spec: The log kind's declaration (for field labels).
        history: The entry's revisions, oldest first (as returned by
            ``ServicingLogStore.revisions``).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self, kind_spec: LogKindSpec, history: list[ServiceLogEntry], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{kind_spec.title} — revision history")
        self.resize(480, 360)

        layout = QVBoxLayout(self)
        text = QTextEdit()
        text.setObjectName("servicing_history_text")
        text.setReadOnly(True)

        lines: list[str] = []
        for entry in history:
            header = f"Revision {entry.revision}"
            if entry.deleted:
                header += " (deleted)"
            if entry.revised_utc:
                header += f" — {entry.revised_utc} by {entry.revised_by or 'unknown'}"
            else:
                header += f" — created {entry.created_utc} ({entry.source})"
            lines.append(header)
            for name in kind_spec.fields:
                lines.append(f"    {name}: {entry.values.get(name, '')}")
            lines.append("")
        text.setPlainText("\n".join(lines))
        layout.addWidget(text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)


class _DeleteConfirmDialog(QDialog):
    """Confirm-with-attribution dialog for deleting a servicing-log entry."""

    def __init__(self, kind_title: str, prefill: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Delete {kind_title} entry")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Delete this entry? Its revision history is kept, never erased.")
        )
        row = QHBoxLayout()
        row.addWidget(QLabel("Deleted by:"))
        self._revised_by_edit = QLineEdit(prefill)
        self._revised_by_edit.setObjectName("servicing_delete_revised_by_input")
        row.addWidget(self._revised_by_edit)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def revised_by(self) -> str:
        return self._revised_by_edit.text().strip()


class ServicingRecordingDialog(QDialog):
    """Row-detail view for one "servicing" entry: summary + recording plot.

    Lazily loads the sidecar named by the entry's ``recording`` field via
    ``load_recording_sidecar`` and plots each channel vs time with a themed
    ``pyqtgraph`` curve (colours cycle through ``PLOT_SERIES``, matching
    ``trend_plot_panel.py``'s pattern). Tolerant of a missing/corrupt sidecar:
    shows a plain message instead of a plot, never raises. Offers "Export
    recording as CSV" whenever a recording loaded successfully.

    Args:
        entry: The selected ``servicing`` entry.
        store: The active ``ServicingLogStore`` (resolves the sidecar path).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self, entry: ServiceLogEntry, store: ServicingLogStore, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Servicing entry — {entry.values.get('entry_kind', '')}")
        self.resize(640, 440)
        self._recording: dict[str, Any] | None = None

        layout = QVBoxLayout(self)

        summary = QLabel(
            f"<b>{entry.values.get('entry_kind', '')}</b> — "
            f"{entry.values.get('person', '') or 'unknown'}<br>"
            f"{entry.values.get('start_utc', '')} → {entry.values.get('end_utc', '')}<br>"
            f"He: {entry.values.get('helium_start_pct', 0.0):g}% → "
            f"{entry.values.get('helium_end_pct', 0.0):g}%  |  "
            f"LN2: {entry.values.get('ln2_start_pct', 0.0):g}% → "
            f"{entry.values.get('ln2_end_pct', 0.0):g}%<br>"
            f"{entry.values.get('notes', '')}"
        )
        summary.setObjectName("servicing_detail_summary")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        recording_name = str(entry.values.get("recording", ""))
        self._export_btn = QPushButton("Export recording as CSV")
        self._export_btn.setObjectName("servicing_detail_export_recording_btn")
        self._export_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._export_btn.clicked.connect(self._on_export_recording)
        self._export_btn.setEnabled(False)

        if not recording_name:
            layout.addWidget(QLabel("No recording for this entry."))
        else:
            self._recording = load_recording_sidecar(store, recording_name)
            if self._recording is None:
                layout.addWidget(
                    QLabel(f"Recording {recording_name!r} could not be loaded.")
                )
            else:
                plot_widget = pg.PlotWidget()
                plot_widget.setObjectName("servicing_detail_plot")
                plot_widget.setLabel("bottom", "Time")
                plot_widget.setLabel("left", "Value")
                plot_widget.showGrid(x=True, y=True, alpha=0.3)
                plot_widget.addLegend()
                unix_time = self._recording.get("unix_time", [])
                channels = self._recording.get("channels", {})
                for i, (name, series) in enumerate(sorted(channels.items())):
                    color = PLOT_SERIES[i % len(PLOT_SERIES)]
                    plot_widget.plot(
                        unix_time[: len(series)],
                        series,
                        pen=pg.mkPen(color, width=2),
                        name=name,
                    )
                layout.addWidget(plot_widget)
                self._export_btn.setEnabled(True)

        layout.addWidget(self._export_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

    def _on_export_recording(self) -> None:
        if self._recording is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export recording as CSV", "recording.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            write_recording_csv(path, self._recording)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))


class _LogKindTable(QWidget):
    """One servicing-log kind's table + (for editable kinds) its action buttons.

    Args:
        kind_spec: The log kind's declaration.
        store: The store to read/write, or ``None`` (table stays empty; used
            only defensively — the page does not construct a table without a
            store).
        get_current_person: Callable returning the attribution prefill.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        kind_spec: LogKindSpec,
        store: ServicingLogStore | None,
        get_current_person: Callable[[], str] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind_spec = kind_spec
        self._store = store
        self._get_current_person = get_current_person or (lambda: "")
        self._field_names = list(kind_spec.fields)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.addWidget(QLabel(f"<b>{kind_spec.title}</b>"))

        self._table = QTableWidget(0, len(self._field_names) + 1)
        self._table.setObjectName(f"servicing_table_{kind_spec.key}")
        headers = [self._column_label(name) for name in self._field_names] + ["Edited"]
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setMinimumHeight(120)
        outer.addWidget(self._table)

        self._add_btn: QPushButton | None = None
        self._edit_btn: QPushButton | None = None
        self._delete_btn: QPushButton | None = None
        self._history_btn: QPushButton | None = None

        if kind_spec.editable:
            btn_row = QHBoxLayout()

            self._add_btn = QPushButton("Add entry")
            self._add_btn.setObjectName(f"servicing_add_btn_{kind_spec.key}")
            self._add_btn.setProperty("class", BTN_CLASS_PRIMARY)
            self._add_btn.clicked.connect(self._on_add_clicked)
            btn_row.addWidget(self._add_btn)

            self._edit_btn = QPushButton("Edit")
            self._edit_btn.setObjectName(f"servicing_edit_btn_{kind_spec.key}")
            self._edit_btn.setProperty("class", BTN_CLASS_SECONDARY)
            self._edit_btn.clicked.connect(self._on_edit_clicked)
            btn_row.addWidget(self._edit_btn)

            self._history_btn = QPushButton("History")
            self._history_btn.setObjectName(f"servicing_history_btn_{kind_spec.key}")
            self._history_btn.setProperty("class", BTN_CLASS_SECONDARY)
            self._history_btn.clicked.connect(self._on_history_clicked)
            btn_row.addWidget(self._history_btn)

            self._delete_btn = QPushButton("Delete")
            self._delete_btn.setObjectName(f"servicing_delete_btn_{kind_spec.key}")
            self._delete_btn.setProperty("class", BTN_CLASS_DANGER)
            self._delete_btn.clicked.connect(self._on_delete_clicked)
            btn_row.addWidget(self._delete_btn)

            btn_row.addStretch()
            outer.addLayout(btn_row)

            self._table.itemSelectionChanged.connect(self._update_button_states)

        self.refresh()

    def _column_label(self, name: str) -> str:
        spec = self._kind_spec.fields[name]
        return f"{name} ({spec.unit})" if spec.unit else name

    def _format_value(self, value: Any) -> str:
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def _selected_entry_id(self) -> str | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _update_button_states(self) -> None:
        has_selection = self._selected_entry_id() is not None
        if self._edit_btn is not None:
            self._edit_btn.setEnabled(has_selection)
        if self._delete_btn is not None:
            self._delete_btn.setEnabled(has_selection)
        if self._history_btn is not None:
            self._history_btn.setEnabled(has_selection)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Repopulate the table from the store, newest first."""
        if self._store is None:
            self._table.setRowCount(0)
            return
        entries = self._store.entries(self._kind_spec.key)  # already newest-first
        self._table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            for col, name in enumerate(self._field_names):
                item = QTableWidgetItem(self._format_value(entry.values.get(name, "")))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.entry_id)
                self._table.setItem(row, col, item)
            marker = QTableWidgetItem("edited" if entry.revision > 1 else "")
            marker.setFlags(marker.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, len(self._field_names), marker)
        self._table.resizeColumnsToContents()
        if self._kind_spec.editable:
            self._update_button_states()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add_clicked(self) -> None:
        if self._store is None:
            return
        dialog = ServicingLogEntryDialog(self._kind_spec, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.collected_values()
            self._store.add_entry(self._kind_spec.key, values, source="manual")
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Add entry failed", str(exc))
            return
        self.refresh()

    def _on_edit_clicked(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None or self._store is None:
            return
        history = self._store.revisions(self._kind_spec.key, entry_id)
        if not history:
            return
        latest = history[-1]
        dialog = ServicingLogEntryDialog(
            self._kind_spec,
            initial_values=latest.values,
            is_edit=True,
            revised_by_default=self._get_current_person(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.collected_values()
            self._store.revise_entry(
                self._kind_spec.key, entry_id, values, revised_by=dialog.revised_by()
            )
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Edit entry failed", str(exc))
            return
        self.refresh()

    def _on_delete_clicked(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None or self._store is None:
            return
        dialog = _DeleteConfirmDialog(
            self._kind_spec.title, self._get_current_person(), parent=self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._store.delete_entry(self._kind_spec.key, entry_id, revised_by=dialog.revised_by())
        self.refresh()

    def _on_history_clicked(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None or self._store is None:
            return
        history = self._store.revisions(self._kind_spec.key, entry_id)
        RevisionHistoryDialog(self._kind_spec, history, parent=self).exec()


class _ServicingLogTable(QWidget):
    """The unified "servicing" kind's table: timeline, filter chips, detail, CSV exports.

    Renders the flat ``servicing`` log kind (docs/plans/unified-servicing-
    log-and-run-recording.md §4) as ONE chronological table — helium fills,
    sample changes, other operation runs, and manual entries all in one
    timeline, sorted newest-first by ``sorted_servicing_entries`` (parsed
    ``start_utc``, falling back to ``created_utc``). A row of filter chips
    (derived from the data's ``entry_kind`` values, never hardcoded) narrows
    the table live. Selecting a row with a non-empty ``recording`` enables
    "View recording", which opens ``ServicingRecordingDialog`` (plot +
    per-recording CSV export). "Export table as CSV" exports the currently
    filtered rows via ``write_servicing_table_csv``. Add/Edit/Delete/History
    reuse the same ``ServicingLogEntryDialog``/``RevisionHistoryDialog``/
    ``_DeleteConfirmDialog`` machinery as ``_LogKindTable`` (the ``servicing``
    kind is always editable).

    Args:
        kind_spec: The ``servicing`` kind's declaration.
        store: The store to read/write, or ``None`` (table stays empty).
        get_current_person: Callable returning the attribution prefill.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        kind_spec: LogKindSpec,
        store: ServicingLogStore | None,
        get_current_person: Callable[[], str] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind_spec = kind_spec
        self._store = store
        self._get_current_person = get_current_person or (lambda: "")
        self._field_names = list(kind_spec.fields)
        self._active_filter: str = _ALL_KINDS_CHIP
        self._all_entries: list[ServiceLogEntry] = []
        self._filtered_entries: list[ServiceLogEntry] = []
        self._chip_buttons: dict[str, QPushButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.addWidget(QLabel(f"<b>{kind_spec.title}</b>"))

        self._chip_row = QHBoxLayout()
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)
        outer.addLayout(self._chip_row)

        self._table = QTableWidget(0, len(self._field_names) + 1)
        self._table.setObjectName(f"servicing_table_{kind_spec.key}")
        headers = [self._column_label(name) for name in self._field_names] + ["Edited"]
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setMinimumHeight(120)
        self._table.itemSelectionChanged.connect(self._update_button_states)
        outer.addWidget(self._table)

        btn_row = QHBoxLayout()

        self._add_btn = QPushButton("Add entry")
        self._add_btn.setObjectName(f"servicing_add_btn_{kind_spec.key}")
        self._add_btn.setProperty("class", BTN_CLASS_PRIMARY)
        self._add_btn.clicked.connect(self._on_add_clicked)
        btn_row.addWidget(self._add_btn)

        self._edit_btn = QPushButton("Edit")
        self._edit_btn.setObjectName(f"servicing_edit_btn_{kind_spec.key}")
        self._edit_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        btn_row.addWidget(self._edit_btn)

        self._history_btn = QPushButton("History")
        self._history_btn.setObjectName(f"servicing_history_btn_{kind_spec.key}")
        self._history_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._history_btn.clicked.connect(self._on_history_clicked)
        btn_row.addWidget(self._history_btn)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setObjectName(f"servicing_delete_btn_{kind_spec.key}")
        self._delete_btn.setProperty("class", BTN_CLASS_DANGER)
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        btn_row.addWidget(self._delete_btn)

        btn_row.addStretch()
        outer.addLayout(btn_row)

        export_row = QHBoxLayout()

        self._view_btn = QPushButton("View recording")
        self._view_btn.setObjectName("servicing_view_recording_btn")
        self._view_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._view_btn.clicked.connect(self._on_view_recording)
        export_row.addWidget(self._view_btn)

        self._export_table_btn = QPushButton("Export table as CSV")
        self._export_table_btn.setObjectName("servicing_export_table_btn")
        self._export_table_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._export_table_btn.clicked.connect(self._on_export_table)
        export_row.addWidget(self._export_table_btn)

        export_row.addStretch()
        outer.addLayout(export_row)

        self.refresh()

    def _column_label(self, name: str) -> str:
        spec = self._kind_spec.fields[name]
        return f"{name} ({spec.unit})" if spec.unit else name

    def _format_value(self, value: Any) -> str:
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def _selected_entry(self) -> ServiceLogEntry | None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._filtered_entries):
            return None
        return self._filtered_entries[row]

    def _update_button_states(self) -> None:
        entry = self._selected_entry()
        has_selection = entry is not None
        self._edit_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)
        self._history_btn.setEnabled(has_selection)
        self._view_btn.setEnabled(
            has_selection and bool(entry.values.get("recording")) if entry is not None else False
        )

    def _rebuild_chips(self, kinds: list[str]) -> None:
        """Rebuild the filter-chip row from the data's ``entry_kind`` values."""
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                self._chip_group.removeButton(widget)
                widget.setParent(None)
                widget.deleteLater()
        self._chip_buttons = {}

        for label in [_ALL_KINDS_CHIP, *kinds]:
            chip = QPushButton(label)
            chip.setObjectName(f"servicing_chip_{label}")
            chip.setCheckable(True)
            chip.setChecked(label == self._active_filter)
            chip.setProperty(
                "class", BTN_CLASS_PRIMARY if label == self._active_filter else BTN_CLASS_SECONDARY
            )
            chip.clicked.connect(lambda _checked=False, l=label: self._on_chip_clicked(l))
            self._chip_group.addButton(chip)
            self._chip_row.addWidget(chip)
            self._chip_buttons[label] = chip
        self._chip_row.addStretch()

    def _on_chip_clicked(self, label: str) -> None:
        self._active_filter = label
        for chip_label, chip in self._chip_buttons.items():
            cls = BTN_CLASS_PRIMARY if chip_label == label else BTN_CLASS_SECONDARY
            chip.setProperty("class", cls)
            chip.style().unpolish(chip)
            chip.style().polish(chip)
        self._render_rows()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the store, rebuild filter chips, and repopulate the table."""
        if self._store is None:
            self._all_entries = []
            self._rebuild_chips([])
            self._render_rows()
            return
        entries = self._store.entries(self._kind_spec.key)
        self._all_entries = sorted_servicing_entries(entries)
        kinds = distinct_entry_kinds(self._all_entries)
        if self._active_filter != _ALL_KINDS_CHIP and self._active_filter not in kinds:
            self._active_filter = _ALL_KINDS_CHIP
        self._rebuild_chips(kinds)
        self._render_rows()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _render_rows(self) -> None:
        self._filtered_entries = filter_entries_by_kind(self._all_entries, self._active_filter)
        self._table.setRowCount(len(self._filtered_entries))
        for row, entry in enumerate(self._filtered_entries):
            for col, name in enumerate(self._field_names):
                value = entry.values.get(name, "")
                text = "●" if name == "recording" and value else self._format_value(value)
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.entry_id)
                self._table.setItem(row, col, item)
            marker = QTableWidgetItem("edited" if entry.revision > 1 else "")
            marker.setFlags(marker.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, len(self._field_names), marker)
        self._table.resizeColumnsToContents()
        self._update_button_states()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add_clicked(self) -> None:
        if self._store is None:
            return
        dialog = ServicingLogEntryDialog(self._kind_spec, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.collected_values()
            self._store.add_entry(self._kind_spec.key, values, source="manual")
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Add entry failed", str(exc))
            return
        self.refresh()

    def _on_edit_clicked(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._store is None:
            return
        history = self._store.revisions(self._kind_spec.key, entry.entry_id)
        if not history:
            return
        latest = history[-1]
        dialog = ServicingLogEntryDialog(
            self._kind_spec,
            initial_values=latest.values,
            is_edit=True,
            revised_by_default=self._get_current_person(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.collected_values()
            self._store.revise_entry(
                self._kind_spec.key, entry.entry_id, values, revised_by=dialog.revised_by()
            )
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Edit entry failed", str(exc))
            return
        self.refresh()

    def _on_delete_clicked(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._store is None:
            return
        dialog = _DeleteConfirmDialog(
            self._kind_spec.title, self._get_current_person(), parent=self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._store.delete_entry(
            self._kind_spec.key, entry.entry_id, revised_by=dialog.revised_by()
        )
        self.refresh()

    def _on_history_clicked(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._store is None:
            return
        history = self._store.revisions(self._kind_spec.key, entry.entry_id)
        RevisionHistoryDialog(self._kind_spec, history, parent=self).exec()

    def _on_view_recording(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._store is None or not entry.values.get("recording"):
            return
        ServicingRecordingDialog(entry, self._store, parent=self).exec()

    def _on_export_table(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export table as CSV", "servicing_log.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            write_servicing_table_csv(path, self._filtered_entries, self._field_names)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))


class ServicingLogPage(QWidget):
    """MonitorWindow's page 2: servicing-log tables + the relocated LogPanel.

    Builds one table per kind in ``log_kinds`` (each must be a declared,
    editable ``LogKindSpec``; an undeclared or non-editable name is skipped
    with a WARNING log), plus the read-only "operations" audit trail
    whenever ``servicing_store`` is not ``None`` — the operations stream has
    no config toggle of its own (plan §9: "always on when any operation
    exists"). ``log_panel`` is composed here, not created — MonitorWindow
    still owns its attach()/detach() lifecycle.

    Args:
        servicing_store: The active setup's ``ServicingLogStore``, or
            ``None`` when the setup has no ``cryogenics:`` block (no tables
            are shown; the LogPanel still is).
        log_kinds: The declared, editable log-kind keys this setup keeps
            (``Station.read_servicing_logs_config()``'s result).
        log_panel: The already-constructed application ``LogPanel``.
        get_current_person: Callable returning the attribution prefill for
            edit/delete dialogs (typically the active experiment's user).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        servicing_store: ServicingLogStore | None,
        log_kinds: list[str],
        log_panel: LogPanel,
        get_current_person: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("servicing_log_page")
        self._store = servicing_store
        self._tables: list[_LogKindTable | _ServicingLogTable] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Logs</b>"))

        tables_container = QWidget()
        tables_layout = QVBoxLayout(tables_container)
        tables_layout.setContentsMargins(0, 0, 0, 0)

        kind_specs: list[LogKindSpec] = []
        for key in log_kinds:
            spec = DECLARED_LOG_KINDS.get(key)
            if spec is None or not spec.editable:
                logger.warning(
                    "ServicingLogPage: %r is not a declared editable log kind; skipping", key
                )
                continue
            kind_specs.append(spec)
        # The legacy machine-only "operations" stream table shows only when
        # the unified "servicing" kind is NOT declared (a not-yet-migrated
        # setup): post-migration the operations file is .bak-renamed, so the
        # table would sit permanently empty under the unified timeline —
        # against the one-timeline goal (plan unified-servicing-log-and-run-
        # recording.md §4).
        if servicing_store is not None and not any(
            spec.key == "servicing" for spec in kind_specs
        ):
            kind_specs.append(DECLARED_LOG_KINDS["operations"])

        for spec in kind_specs:
            # The unified "servicing" kind (docs/plans/unified-servicing-log-
            # and-run-recording.md §4) gets the dedicated timeline/filter/
            # detail/export table instead of the generic per-kind one.
            table_cls = _ServicingLogTable if spec.key == "servicing" else _LogKindTable
            table = table_cls(spec, servicing_store, get_current_person, parent=tables_container)
            self._tables.append(table)
            tables_layout.addWidget(table)
        tables_layout.addStretch()

        tables_scroll = QScrollArea()
        tables_scroll.setObjectName("servicing_log_tables_scroll")
        tables_scroll.setWidgetResizable(True)
        tables_scroll.setWidget(tables_container)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("servicing_log_splitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(tables_scroll)
        splitter.addWidget(log_panel)
        splitter.setSizes([500, 300])
        outer.addWidget(splitter)

    def refresh(self) -> None:
        """Refresh every log-kind table from its store.

        Called by the host on log-page-shown and on ``run_finished`` — never
        on a timer.
        """
        for table in self._tables:
            table.refresh()
