# ---
# description: |
#   session_dialogs: modal dialogs for switching between existing L6 session
#   records (experiments) — currently just LoadSessionDialog, the picker
#   MonitorWindow's User menu "Load Session…" action opens. Mirrors
#   UserPickerWidget's list-plus-accept pattern from experiment_dialogs.py:
#   every stored experiment is listed via SessionManager.store, open ones
#   selectable, closed ones shown grayed out with a "(closed)" suffix and
#   disabled (item flags, never a stylesheet). The caller reads back the
#   chosen experiment_id after accept() and drives the actual switch itself
#   (MonitorWindow._switch_session) — this module never calls SessionManager
#   mutators, same separation experiment_dialogs.py uses.
# entry_point: Not run directly. Opened by cryosoft.gui.monitor_window.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.session.manager (SessionManager)
#   - cryosoft.session.models (EXPERIMENT_STATUS_OPEN)
# input: |
#   A SessionManager, read once at construction via its store's
#   list_experiments()/load() for each experiment's title/user/status/
#   created date.
# process: |
#   A QListWidget populated once at construction; closed experiments have
#   Qt.ItemFlag.ItemIsEnabled cleared (renders grayed out through the normal
#   disabled-item palette — no setStyleSheet) and cannot be selected/accepted.
# output: |
#   selected_experiment_id() returns the chosen id after exec() accepts.
# ---

"""session_dialogs — dialogs for picking an existing session (L6 experiment)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryosoft.session.manager import SessionManager
from cryosoft.session.models import EXPERIMENT_STATUS_OPEN

_EXPERIMENT_ID_ROLE = Qt.ItemDataRole.UserRole


class LoadSessionDialog(QDialog):
    """Pick an existing session (experiment) to switch to.

    Every experiment in ``session_manager.store`` is listed, newest id last
    (``ExperimentStore.list_experiments()`` is sorted). Open ones are
    selectable; closed ones are shown grayed out with a ``"(closed)"``
    suffix and disabled via item flags — never a stylesheet, per the GUI
    styling standard. ``selected_experiment_id()`` is only meaningful after
    ``exec()`` returns ``Accepted``.
    """

    def __init__(
        self, session_manager: SessionManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Load Session")
        self._session_manager = session_manager

        self._list = QListWidget()
        self._list.setObjectName("load_session_list")
        self._populate()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._list.itemSelectionChanged.connect(self._update_ok_enabled)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)
        layout.addWidget(buttons)

    def _populate(self) -> None:
        """List every stored experiment; only open ones are selectable."""
        store = self._session_manager.store
        for experiment_id in store.list_experiments():
            record = store.load(experiment_id)
            if record is None:
                continue
            is_open = record.status == EXPERIMENT_STATUS_OPEN
            label = f"{record.title} — {record.user_id} ({record.created_utc[:10]})"
            if not is_open:
                label += " (closed)"
            item = QListWidgetItem(label)
            item.setData(_EXPERIMENT_ID_ROLE, experiment_id)
            if not is_open:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._list.addItem(item)

    def _update_ok_enabled(self) -> None:
        items = self._list.selectedItems()
        self._ok_button.setEnabled(
            bool(items) and bool(items[0].flags() & Qt.ItemFlag.ItemIsEnabled)
        )

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        if item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.accept()

    def selected_experiment_id(self) -> str:
        """Return the chosen experiment id. Only meaningful after ``accept()``.

        Returns:
            The selected experiment's store id, or ``""`` if nothing is
            selected.
        """
        items = self._list.selectedItems()
        if not items:
            return ""
        return str(items[0].data(_EXPERIMENT_ID_ROLE) or "")
