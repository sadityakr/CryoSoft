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
#   gets a table here automatically, with zero new GUI code.
# entry_point: Not run directly. Hosted as MonitorWindow's page 2 (Logs),
#   built once in MonitorWindow._build_ui.
# dependencies:
#   - PyQt6 >= 6.5
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
#   Builds one _LogKindTable per declared editable kind, plus the
#   non-editable "operations" table whenever a store is present (the
#   operations stream has no config toggle — it is on whenever any operation
#   exists, i.e. whenever the store exists). Add/Edit/Delete act on the
#   store's revision-model API directly and call refresh() afterwards;
#   refresh() is also called by the host on log-page-shown and run_finished
#   — never on a timer.
# output: |
#   A QWidget (page 2) with one QGroupBox+QTableWidget per log kind and the
#   hosted LogPanel beneath, in a resizable QSplitter.
# last_updated: 2026-07-19
# ---

"""ServicingLogPage — MonitorWindow's page 2: servicing-log tables + the app log."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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
from cryosoft.gui.theme import BTN_CLASS_DANGER, BTN_CLASS_PRIMARY, BTN_CLASS_SECONDARY
from cryosoft.session.servicing_log import DECLARED_LOG_KINDS, LogKindSpec, ServicingLogStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.gui.log_panel import LogPanel
    from cryosoft.session.models import ServiceLogEntry

logger = logging.getLogger(__name__)

__all__ = ["ServicingLogPage", "ServicingLogEntryDialog", "RevisionHistoryDialog"]


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
        self._tables: list[_LogKindTable] = []

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
        if servicing_store is not None:
            kind_specs.append(DECLARED_LOG_KINDS["operations"])

        for spec in kind_specs:
            table = _LogKindTable(spec, servicing_store, get_current_person, parent=tables_container)
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
