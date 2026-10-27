"""session_dialogs — dialog for picking/creating the active Session (L6 tier)."""

from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cryosoft.session.store import SessionStore

_SESSION_ID_ROLE = Qt.ItemDataRole.UserRole
_OWNER_ROLE = Qt.ItemDataRole.UserRole + 1


class ResumeSessionDialog(QDialog):
    """Pick an existing Session to resume, or create a new one.

    Every session owned by ``user_id`` is listed via
    ``SessionStore.list_sessions()`` — sessions live under
    ``sessions/<user_id>/``, so "nobody logged in yet" must already have
    been resolved to the fixed Guest identity by the caller before this
    dialog is built (see ``cryosoft.session.models.GUEST_USER_ID``).
    ``selected_session_id()`` is only meaningful after ``exec()`` returns
    ``Accepted``.

    Creating a session asks for its **folder** as well as its display name.
    A session is a folder holding every experiment run inside it, so which
    folder that is belongs to the operator rather than to a slug derived
    from the name behind their back. The folder is still one segment
    directly under ``<measurement_root>/sessions/<user_id>/`` — the Session
    tier's layout is structural (ownership IS a directory) — so the field
    names that one folder and the resolved path is shown in full beneath
    it. ``SessionStore.validate_session_dirname()`` is the single rule the
    name is judged by; this dialog only renders its refusal.
    """

    def __init__(
        self,
        store: SessionStore,
        user_id: str,
        parent: QWidget | None = None,
        focus_new: bool = False,
        all_users: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resume Session")
        self._store = store
        self._user_id = user_id
        # Load Session mode: every user's sessions, plus Browse... for a
        # session.json, and no new-session form (loading is not creating).
        self._all_users = all_users
        # True once the operator edits the folder field themselves, after
        # which the name no longer drives it — typing a name must not
        # silently overwrite a folder they chose deliberately.
        self._dirname_edited = False

        self._list = QListWidget()
        self._list.setObjectName("resume_session_list")
        self._populate()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._list.itemSelectionChanged.connect(self._update_ok_enabled)
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)
        if all_users:
            # Loading is not creating: no new-session form is built at all,
            # so nothing is left holding a widget Qt has already deleted.
            self.setWindowTitle("Load Session")
            layout.addLayout(self._build_browse_row())
        else:
            layout.addWidget(self._build_new_session_box())
        layout.addWidget(buttons)
        self.setMinimumWidth(520)
        if focus_new and not all_users:
            self.setWindowTitle("New Session")
            self._new_name_input.setFocus()

    def _build_browse_row(self) -> QVBoxLayout:
        """Build the Browse controls that find a ``session.json`` on disk.

        The refusal message sits on its own full-width row above the button:
        beside it, the message was squeezed into the leftover column and
        wrapped every few characters.
        """
        column = QVBoxLayout()
        self._browse_error_label = QLabel()
        self._browse_error_label.setObjectName("load_session_browse_error")
        self._browse_error_label.setWordWrap(True)
        self._browse_error_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._browse_error_label.hide()
        column.addWidget(self._browse_error_label)
        row = QHBoxLayout()
        row.addStretch(1)
        browse_btn = QPushButton("Browse for session.json…")
        browse_btn.setObjectName("browse_session_btn")
        browse_btn.setToolTip(
            "Find a session.json inside the sessions folder and load that session"
        )
        browse_btn.clicked.connect(self._on_browse_clicked)
        row.addWidget(browse_btn)
        column.addLayout(row)
        return column

    def _on_browse_clicked(self) -> None:
        """Let the operator pick a ``session.json`` and select what it names."""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Find session.json",
            str(self._store.root),
            "Session record (session.json);;All files (*)",
        )
        if path:
            self.select_session_file(path)

    def select_session_file(self, path: str) -> bool:
        """Select the session a ``session.json`` path names, or say why not.

        Args:
            path: A ``session.json`` file or its session folder.

        Returns:
            True when it named a session of this store and that session is now
            selected; False when it did not (the reason is shown inline).
        """
        located = self._store.locate(path)
        if located is None:
            self._browse_error_label.setText(
                f"{path} is not a session in {self._store.root} - a session "
                "must be a folder with a readable session.json directly "
                "inside a user's folder there."
            )
            self._browse_error_label.show()
            return False
        self._browse_error_label.hide()
        for index in range(self._list.count()):
            item = self._list.item(index)
            if (
                item.data(_OWNER_ROLE) == located[0]
                and item.data(_SESSION_ID_ROLE) == located[1]
            ):
                self._list.setCurrentItem(item)
                return True
        # On disk but not in the list built at open (e.g. created since): add
        # it so the choice is visible before OK.
        item = self._session_item(*located)
        if item is None:
            return False
        self._list.addItem(item)
        self._list.setCurrentItem(item)
        return True

    def _build_new_session_box(self) -> QWidget:
        """Build the "new session" form: display name, folder, resolved path."""
        box = QGroupBox("New session")
        box.setObjectName("new_session_box")
        form = QFormLayout(box)

        self._new_name_input = QLineEdit()
        self._new_name_input.setObjectName("new_session_name_input")
        self._new_name_input.setPlaceholderText("e.g. R vs T, spring batch")
        self._new_name_input.textChanged.connect(self._on_name_changed)
        form.addRow("Name:", self._new_name_input)

        self._new_dirname_input = QLineEdit()
        self._new_dirname_input.setObjectName("new_session_dirname_input")
        self._new_dirname_input.setPlaceholderText("folder name")
        self._new_dirname_input.setToolTip(
            "The folder this session's experiments are saved in, directly "
            "under this user's sessions folder"
        )
        self._new_dirname_input.textEdited.connect(self._on_dirname_edited)
        self._new_dirname_input.textChanged.connect(self._update_create_enabled)
        form.addRow("Folder:", self._new_dirname_input)

        self._path_label = QLabel()
        self._path_label.setObjectName("new_session_path_label")
        self._path_label.setProperty("class", "secondary_label")
        self._path_label.setWordWrap(True)
        # A filesystem path is one unbroken token, so the label's own size
        # hint is as wide as the whole path and a deep measurement root would
        # stretch the dialog off the screen. Ignored horizontal policy makes
        # the label take the width the layout gives it and wrap inside that,
        # instead of the layout taking its width from the label.
        self._path_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("Saved in:", self._path_label)

        self._error_label = QLabel()
        self._error_label.setObjectName("new_session_error_label")
        self._error_label.setWordWrap(True)
        self._error_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._error_label.hide()
        form.addRow("", self._error_label)

        create_row = QHBoxLayout()
        create_row.addStretch(1)
        self._create_btn = QPushButton("Create")
        self._create_btn.setObjectName("create_session_btn")
        self._create_btn.setEnabled(False)
        self._create_btn.clicked.connect(self._on_create_clicked)
        create_row.addWidget(self._create_btn)
        form.addRow("", create_row)

        self._update_path_label()
        return box

    def _session_item(self, owner: str, session_id: str) -> QListWidgetItem | None:
        """Build the list row for one session, or ``None`` if it will not load."""
        session = self._store.load(owner, session_id)
        if session is None:
            return None
        label = f"{session.name} ({session.created_utc[:10]})"
        if self._all_users:
            label = f"{session.name} - {owner} ({session.created_utc[:10]})"
        item = QListWidgetItem(label)
        item.setData(_SESSION_ID_ROLE, session_id)
        item.setData(_OWNER_ROLE, owner)
        return item

    def _populate(self) -> None:
        """List ``user_id``'s sessions, or every user's in Load Session mode."""
        if self._all_users:
            pairs = self._store.list_all_sessions()
        else:
            pairs = [
                (self._user_id, sid) for sid in self._store.list_sessions(self._user_id)
            ]
        for owner, session_id in pairs:
            item = self._session_item(owner, session_id)
            if item is not None:
                self._list.addItem(item)

    def _update_ok_enabled(self) -> None:
        self._ok_button.setEnabled(bool(self._list.selectedItems()))

    def _on_name_changed(self, text: str) -> None:
        """Mirror the name into the folder field until the operator edits it."""
        if not self._dirname_edited:
            self._new_dirname_input.setText(self._suggested_dirname(text))
        self._update_create_enabled()

    def _on_dirname_edited(self, _text: str) -> None:
        """Remember that the folder is the operator's choice from now on."""
        self._dirname_edited = True

    def _suggested_dirname(self, name: str) -> str:
        """Return the folder name a display name suggests.

        Uses ``SessionStore.make_session_id()`` so the offered default is
        exactly what the store would have derived on its own — the chosen
        folder replaces that derivation rather than running beside it.

        Args:
            name: The display name typed so far.

        Returns:
            The suggested folder name, or ``""`` for an empty name.
        """
        if not name.strip():
            return ""
        created = datetime.now(timezone.utc).isoformat()
        return self._store.make_session_id(name, created, self._user_id)

    def _update_create_enabled(self) -> None:
        """Enable Create only for a folder name the store would accept."""
        self._update_path_label()
        has_name = bool(self._new_name_input.text().strip())
        dirname = self._new_dirname_input.text().strip()
        if not has_name or not dirname:
            self._error_label.hide()
            self._create_btn.setEnabled(False)
            return
        try:
            self._store.validate_session_dirname(dirname, self._user_id)
        except ValueError as exc:
            self._error_label.setText(str(exc))
            self._error_label.show()
            self._create_btn.setEnabled(False)
            return
        self._error_label.hide()
        self._create_btn.setEnabled(True)

    def _update_path_label(self) -> None:
        """Show the full folder the session would occupy."""
        dirname = self._new_dirname_input.text().strip()
        base = self._store.root / self._user_id
        self._path_label.setText(str(base / dirname) if dirname else str(base))

    def _on_create_clicked(self) -> None:
        """Create the session in the chosen folder, select it, and accept."""
        name = self._new_name_input.text().strip()
        dirname = self._new_dirname_input.text().strip()
        if not name or not dirname:
            return
        try:
            session = self._store.create_session(
                name=name, user_id=self._user_id, session_dirname=dirname
            )
        except (OSError, ValueError) as exc:
            # Rendered inline, not as a second modal on top of this one.
            self._error_label.setText(str(exc))
            self._error_label.show()
            self._create_btn.setEnabled(False)
            return
        item = QListWidgetItem(f"{session.name} ({session.created_utc[:10]})")
        item.setData(_SESSION_ID_ROLE, session.session_id)
        item.setData(_OWNER_ROLE, self._user_id)
        self._list.addItem(item)
        self._list.setCurrentItem(item)
        self.accept()

    def selected_session(self) -> tuple[str, str] | None:
        """Return the chosen ``(user_id, session_id)``; meaningful after ``accept()``.

        Unlike ``selected_session_id()`` this carries the owner, which Load
        Session needs because the chosen session may belong to anyone.

        Returns:
            The pair, or ``None`` if nothing is selected.
        """
        items = self._list.selectedItems()
        if not items:
            return None
        session_id = str(items[0].data(_SESSION_ID_ROLE) or "")
        owner = str(items[0].data(_OWNER_ROLE) or self._user_id)
        return (owner, session_id) if session_id else None

    def selected_session_id(self) -> str | None:
        """Return the chosen session id. Only meaningful after ``accept()``.

        Returns:
            The selected session's store id, or ``None`` if nothing is
            selected.
        """
        items = self._list.selectedItems()
        if not items:
            return None
        return str(items[0].data(_SESSION_ID_ROLE) or "") or None
