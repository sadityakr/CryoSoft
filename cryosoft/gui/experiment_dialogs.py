# ---
# description: |
#   Modal dialogs for the experiment lifecycle: StartExperimentDialog (title,
#   user, attendance — with an inline "New user" flow via AddUserDialog) and
#   CloseExperimentDialog (closing findings text). Opened only by
#   SampleInfoPanel's Start/Close Experiment button; every SessionManager
#   mutation happens in the panel after a dialog accepts, never inside the
#   dialogs themselves (the one exception is AddUserDialog's caller adding
#   the new User to the roster, since the roster has no other writer).
# entry_point: Not run directly. Opened by cryosoft.gui.sample_info_panel.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.session.models (User)
#   - cryosoft.session.store (UserRoster)
# input: |
#   A UserRoster (read for the user combo) and, for CloseExperimentDialog,
#   the experiment's current findings text.
# process: |
#   Plain QDialog forms with a QDialogButtonBox(Ok|Cancel); Ok is disabled
#   until the required fields are valid.
# output: |
#   result_values()/findings()/user() accessors, read by the caller after
#   exec() returns Accepted.
# ---

"""Modal dialogs for starting/closing an experiment and adding a roster user."""

from __future__ import annotations

import re

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.session.models import User
from cryosoft.session.store import UserRoster


def _slugify(text: str) -> str:
    """Derive a roster-key slug from free text (lowercase, ``_``-joined)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class AddUserDialog(QDialog):
    """Add one person to the user roster: name, email, ORCID, and an id."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add User")
        self._id_edited_by_hand = False

        form = QFormLayout()

        self._name_input = QLineEdit()
        self._name_input.setObjectName("user_name_input")
        self._name_input.textChanged.connect(self._on_name_changed)
        self._name_input.textChanged.connect(self._update_ok_enabled)
        form.addRow("Name:", self._name_input)

        self._id_input = QLineEdit()
        self._id_input.setObjectName("user_id_input")
        self._id_input.textEdited.connect(self._on_id_edited)
        self._id_input.textChanged.connect(self._update_ok_enabled)
        form.addRow("User ID:", self._id_input)

        self._email_input = QLineEdit()
        self._email_input.setObjectName("user_email_input")
        form.addRow("Email:", self._email_input)

        self._orcid_input = QLineEdit()
        self._orcid_input.setObjectName("user_orcid_input")
        form.addRow("ORCID:", self._orcid_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_name_changed(self, text: str) -> None:
        """Auto-fill the id from the name until the user edits it by hand."""
        if not self._id_edited_by_hand:
            self._id_input.setText(_slugify(text))

    def _on_id_edited(self, _text: str) -> None:
        self._id_edited_by_hand = True

    def _update_ok_enabled(self) -> None:
        self._ok_button.setEnabled(
            bool(self._name_input.text().strip()) and bool(self._id_input.text().strip())
        )

    def user(self) -> User:
        """Return the entered user. Only meaningful after ``exec()`` accepts.

        Returns:
            A ``User`` built from the form fields (``eln_user_id`` empty).
        """
        return User(
            user_id=self._id_input.text().strip(),
            name=self._name_input.text().strip(),
            email=self._email_input.text().strip(),
            orcid=self._orcid_input.text().strip(),
        )


class StartExperimentDialog(QDialog):
    """Collect a title, user, and attendance flag to open a new experiment."""

    def __init__(self, roster: UserRoster, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Start Experiment")
        self._roster = roster

        form = QFormLayout()

        self._title_input = QLineEdit()
        self._title_input.setObjectName("experiment_title_input")
        self._title_input.setPlaceholderText("e.g. Hall bar A3 — SOT switching vs T")
        self._title_input.textChanged.connect(self._update_ok_enabled)
        form.addRow("Title:", self._title_input)

        user_row = QHBoxLayout()
        self._user_combo = QComboBox()
        self._user_combo.setObjectName("experiment_user_combo")
        user_row.addWidget(self._user_combo, 1)
        new_user_btn = QPushButton("New user…")
        new_user_btn.setObjectName("new_user_btn")
        new_user_btn.clicked.connect(self._on_new_user)
        user_row.addWidget(new_user_btn)
        form.addRow("User:", user_row)

        self._attended_checkbox = QCheckBox("Human attending this experiment")
        self._attended_checkbox.setObjectName("start_attended_checkbox")
        self._attended_checkbox.setChecked(True)
        form.addRow("", self._attended_checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._reload_users()

    def _reload_users(self, select_user_id: str = "") -> None:
        """Repopulate the user combo from the roster, optionally selecting one."""
        self._user_combo.clear()
        for user in self._roster.list_users():
            self._user_combo.addItem(user.name or user.user_id, userData=user.user_id)
        if select_user_id:
            index = self._user_combo.findData(select_user_id)
            if index >= 0:
                self._user_combo.setCurrentIndex(index)
        self._update_ok_enabled()

    def _on_new_user(self) -> None:
        dialog = AddUserDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        user = dialog.user()
        self._roster.add(user)
        self._reload_users(select_user_id=user.user_id)

    def _update_ok_enabled(self) -> None:
        self._ok_button.setEnabled(
            bool(self._title_input.text().strip()) and self._user_combo.count() > 0
        )

    def result_values(self) -> tuple[str, str, bool]:
        """Return ``(title, user_id, attended)``. Only meaningful after accept.

        Returns:
            The entered title, the selected user's roster id, and the
            attendance checkbox state.
        """
        return (
            self._title_input.text().strip(),
            self._user_combo.currentData() or "",
            self._attended_checkbox.isChecked(),
        )


class CloseExperimentDialog(QDialog):
    """Collect the experiment's closing findings text."""

    def __init__(self, current_findings: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Close Experiment")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Findings (optional):"))

        self._findings_input = QTextEdit()
        self._findings_input.setObjectName("close_findings_input")
        self._findings_input.setPlainText(current_findings)
        layout.addWidget(self._findings_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def findings(self) -> str:
        """Return the entered findings text."""
        return self._findings_input.toPlainText()
