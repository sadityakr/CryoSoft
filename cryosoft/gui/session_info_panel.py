# ---
# description: |
#   SessionInfoPanel: the Session Information quadrant of MonitorWindow — the
#   GUI surface for the Experiment tier (Setup-tier concerns — config
#   identity, instrument metadata, user login — live in the menu bar, not
#   here). Holds the experiment status/Start-Close control (when a
#   SessionManager is wired), sample name/ID/comments and the data-directory
#   field with its Browse button, and an eLab status line (publish controls
#   land with Track B; today it just reflects ElnLink on the open
#   experiment). The sample fields stay free-editable per run regardless of
#   whether an experiment is open; whatever they hold at "Start Experiment"
#   time is snapshotted onto the ExperimentRecord for record-keeping. It is
#   the single owner of session-level sample metadata in the GUI, read by
#   ProcedureWindow through MonitorWindow's get_sample_info/get_data_dir
#   callables. Data Dir is derived-but-editable: opening/switching a session
#   forces the field to that session's own data/ folder (remembering
#   whatever it held before, to restore on close), and a plain status note
#   (no stylesheet) appears whenever the field points outside the open
#   session's folder — see docs/plans/unified-session-record.md §5.
# entry_point: Not run directly. Hosted as MonitorWindow's bottom-left quadrant.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.gui.app_settings (sessions_root default fallback)
#   - cryosoft.gui.form_autosave (SessionState)
#   - cryosoft.gui.theme (button classes)
#   - cryosoft.gui.experiment_dialogs (Start/Close experiment dialogs)
#   - cryosoft.session.manager (SessionManager, optional)
# input: |
#   A loaded SessionState (via apply_session) to prefill the fields, and an
#   optional SessionManager whose experiment_changed signal drives the
#   experiment status row and the Data Dir field.
# process: |
#   Builds the form inside a QScrollArea (objectNames session_info_quadrant /
#   session_info_scroll and the *_input fields are preserved API for tests).
#   The experiment row is always built; without a SessionManager its button
#   stays disabled. On each experiment_changed, an open/switched experiment
#   (a changed experiment_id) forces Data Dir to current_data_dir(); closing
#   restores the field to whatever it held immediately before the session
#   opened.
# output: |
#   get_sample_info()/get_data_dir() read the live field values.
#   SessionManager.start_experiment()/close_experiment()/set_findings()/
#   set_attended() are called from the dialogs' results.
# ---

"""SessionInfoPanel — the Session Information quadrant (experiment + sample metadata)."""

from __future__ import annotations

from pathlib import Path

import qtawesome as qta
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.gui import app_settings
from cryosoft.gui.experiment_dialogs import CloseExperimentDialog, StartExperimentDialog
from cryosoft.gui.form_autosave import SessionState
from cryosoft.gui.theme import TEXT_PRIMARY
from cryosoft.session.manager import SessionManager

_ELN_NOT_CONFIGURED_TEXT = "eLab publishing is not configured yet"
_OUTSIDE_SESSION_NOTE_TEXT = "saving outside the current session folder"


class SessionInfoPanel(QWidget):
    """The Session Information quadrant: experiment control, plus sample fields.

    ObjectNames (``session_info_quadrant``, ``session_info_scroll``,
    ``sample_name_input``, ``sample_id_input``, ``comments_input``,
    ``data_dir_input``, ``browse_btn``, ``experiment_status_label``,
    ``start_close_experiment_btn``, ``attended_checkbox``) are preserved API
    — tests and muscle memory rely on them.

    Args:
        parent: Optional Qt parent widget.
        session_manager: The L6 SessionManager. When ``None`` (unit tests
            that build the panel standalone), the experiment row is shown
            but its button stays disabled.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_manager = session_manager
        # Data Dir transition tracking (rule 3, plan §5): _last_experiment_id
        # detects an actual open/switch transition (vs. a same-experiment
        # experiment_changed re-emit from e.g. an attendance/findings edit);
        # _pre_session_data_dir remembers the field's manual value from just
        # before the first such transition, so closing can restore it.
        self._last_experiment_id: str | None = None
        self._pre_session_data_dir: str | None = None
        self.setObjectName("session_info_quadrant")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Experiment</b>"))
        outer.addLayout(self._build_experiment_row())
        outer.addWidget(QLabel("<b>Sample Info</b>"))

        scroll = QScrollArea()
        scroll.setObjectName("session_info_scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._build_form())
        outer.addWidget(scroll)

        outer.addWidget(QLabel("<b>eLab</b>"))
        self._eln_status_label = QLabel(_ELN_NOT_CONFIGURED_TEXT)
        self._eln_status_label.setObjectName("eln_status_label")
        self._eln_status_label.setWordWrap(True)
        outer.addWidget(self._eln_status_label)

        if self._session_manager is not None:
            self._session_manager.experiment_changed.connect(self._on_experiment_changed)
            current = self._session_manager.current_experiment()
            self._on_experiment_changed(current.to_dict() if current is not None else {})

    def _build_experiment_row(self) -> QVBoxLayout:
        """Build the experiment status label, Start/Close button, and attendance box.

        Returns:
            A layout with the status/button row plus the (initially hidden)
            attendance checkbox.
        """
        section = QVBoxLayout()

        status_row = QHBoxLayout()
        self._experiment_status_label = QLabel("No experiment open")
        self._experiment_status_label.setObjectName("experiment_status_label")
        self._experiment_status_label.setWordWrap(True)
        status_row.addWidget(self._experiment_status_label, 1)

        self._start_close_btn = QPushButton("Start Experiment…")
        self._start_close_btn.setObjectName("start_close_experiment_btn")
        if self._session_manager is None:
            self._start_close_btn.setEnabled(False)
            self._start_close_btn.setToolTip("Session management is not available")
        else:
            self._start_close_btn.clicked.connect(self._on_start_close_clicked)
        status_row.addWidget(self._start_close_btn)
        section.addLayout(status_row)

        self._attended_checkbox = QCheckBox("Attended")
        self._attended_checkbox.setObjectName("attended_checkbox")
        self._attended_checkbox.setChecked(True)
        self._attended_checkbox.setVisible(False)
        self._attended_checkbox.toggled.connect(self._on_attended_toggled)
        section.addWidget(self._attended_checkbox)

        return section

    def _on_start_close_clicked(self) -> None:
        """Open the Start or Close Experiment dialog depending on current state."""
        if self._session_manager is None:
            return
        if self._session_manager.current_experiment() is None:
            self._run_start_dialog()
        else:
            self._run_close_dialog()

    def _run_start_dialog(self) -> None:
        assert self._session_manager is not None
        dialog = StartExperimentDialog(self._session_manager.roster, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title, user_id, attended = dialog.result_values()
        try:
            self._session_manager.start_experiment(
                title=title,
                user_id=user_id,
                sample_info=self.get_sample_info(),
                attended=attended,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Could not start experiment", str(exc))

    def _run_close_dialog(self) -> None:
        assert self._session_manager is not None
        record = self._session_manager.current_experiment()
        current_findings = record.findings if record is not None else ""
        dialog = CloseExperimentDialog(current_findings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._session_manager.set_findings(dialog.findings())
        self._session_manager.close_experiment()

    def _on_attended_toggled(self, checked: bool) -> None:
        if self._session_manager is not None:
            self._session_manager.set_attended(checked)

    def _on_experiment_changed(self, record: dict) -> None:
        """Reflect a SessionManager ``experiment_changed`` payload in the row.

        Args:
            record: ``ExperimentRecord.to_dict()``, or ``{}`` when none open.
        """
        if not record:
            self._experiment_status_label.setText("No experiment open")
            self._start_close_btn.setText("Start Experiment…")
            self._attended_checkbox.setVisible(False)
            self._eln_status_label.setText(_ELN_NOT_CONFIGURED_TEXT)
            self._restore_data_dir_on_close()
            return

        user_id = record.get("user_id", "")
        user_name = user_id
        if self._session_manager is not None:
            user = self._session_manager.roster.get(user_id)
            if user is not None and user.name:
                user_name = user.name
        attended = bool(record.get("attended", True))

        self._experiment_status_label.setText(
            f"{record.get('title', '')} — {user_name} "
            f"({'attended' if attended else 'unattended'})"
        )
        self._start_close_btn.setText("Close Experiment…")
        self._attended_checkbox.setVisible(True)
        self._attended_checkbox.blockSignals(True)
        self._attended_checkbox.setChecked(attended)
        self._attended_checkbox.blockSignals(False)

        eln_link = record.get("eln_link") or {}
        if eln_link.get("url"):
            self._eln_status_label.setText(f"Published: {eln_link['url']}")
        else:
            self._eln_status_label.setText(f"Not published yet — {_ELN_NOT_CONFIGURED_TEXT}")

        self._force_data_dir_on_open(record.get("experiment_id", ""))

    def _force_data_dir_on_open(self, experiment_id: str) -> None:
        """Force Data Dir to the (newly) active session's own folder.

        Only acts on an actual transition — a different ``experiment_id``
        than last seen (covers both a brand-new/switched-in open experiment
        and, from ``None``, the very first one in this sequence). A
        same-experiment re-emit (attendance/findings edits) leaves whatever
        the physicist has since typed alone. The field's text just before
        the first such transition is captured so closing can restore it.

        Args:
            experiment_id: The now-open experiment's id (never empty here).
        """
        if experiment_id == self._last_experiment_id:
            return
        if self._last_experiment_id is None:
            self._pre_session_data_dir = self._data_dir_input.text()
        self._last_experiment_id = experiment_id
        if self._session_manager is not None:
            data_dir = self._session_manager.current_data_dir()
            if data_dir is not None:
                self._data_dir_input.setText(str(data_dir))
        self._update_data_dir_note()

    def _restore_data_dir_on_close(self) -> None:
        """Restore whatever Data Dir held immediately before the session opened."""
        if self._last_experiment_id is not None and self._pre_session_data_dir is not None:
            self._data_dir_input.setText(self._pre_session_data_dir)
        self._last_experiment_id = None
        self._pre_session_data_dir = None
        self._update_data_dir_note()

    def _build_form(self) -> QWidget:
        """Build the sample-info form (session-level metadata).

        Returns:
            A QWidget with name, ID, comments, and data-dir form fields.
        """
        box = QWidget()
        form = QFormLayout(box)

        self._sample_name_input = QLineEdit()
        self._sample_name_input.setObjectName("sample_name_input")
        self._sample_name_input.setPlaceholderText("e.g. Si_001")
        form.addRow("Name:", self._sample_name_input)

        self._sample_id_input = QLineEdit()
        self._sample_id_input.setObjectName("sample_id_input")
        self._sample_id_input.setPlaceholderText("e.g. S2024-01")
        form.addRow("ID:", self._sample_id_input)

        self._comments_input = QTextEdit()
        self._comments_input.setObjectName("comments_input")
        self._comments_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        form.addRow("Comments:", self._comments_input)

        dir_row = QHBoxLayout()
        # Starts empty ("no explicit choice yet"); apply_session() (called
        # right after construction by MonitorWindow) and/or the SessionManager
        # experiment_changed handler above fill in the right value — the
        # session's own folder when one is open, else the sessions_root()
        # default (see _default_data_dir_text()).
        self._data_dir_input = QLineEdit()
        self._data_dir_input.setObjectName("data_dir_input")
        self._data_dir_input.textChanged.connect(self._update_data_dir_note)
        browse_btn = QPushButton("Browse…")
        browse_btn.setObjectName("browse_btn")
        browse_btn.setIcon(qta.icon("fa5s.folder-open", color=TEXT_PRIMARY))
        browse_btn.setToolTip("Choose the directory where run data is saved")
        browse_btn.clicked.connect(self._on_browse_dir)
        dir_row.addWidget(self._data_dir_input)
        dir_row.addWidget(browse_btn)
        form.addRow("Data Dir:", dir_row)

        self._data_dir_note = QLabel(_OUTSIDE_SESSION_NOTE_TEXT)
        self._data_dir_note.setObjectName("data_dir_note")
        self._data_dir_note.setWordWrap(True)
        self._data_dir_note.hide()
        form.addRow("", self._data_dir_note)

        return box

    def _on_browse_dir(self) -> None:
        """Open a directory browser and fill the data-dir field.

        Opens at the open session's own data folder (rule 3) when one is
        active, else at whatever the field currently holds.
        """
        start_dir = self._data_dir_input.text()
        if self._session_manager is not None:
            current_dir = self._session_manager.current_data_dir()
            if current_dir is not None:
                start_dir = str(current_dir)
        selected = QFileDialog.getExistingDirectory(self, "Select Data Directory", start_dir)
        if selected:
            self._data_dir_input.setText(selected)

    def _update_data_dir_note(self) -> None:
        """Show/hide the "saving outside the current session folder" note.

        Only meaningful while a session is open: compares the field's
        current path against the session folder
        (``current_data_dir().parent``, since ``current_data_dir()`` is that
        folder's ``data/`` sub-directory). No session open, or an empty
        field, both hide the note.
        """
        session_folder = self._current_session_folder()
        text = self._data_dir_input.text().strip()
        if session_folder is None or not text:
            self._data_dir_note.hide()
            return
        try:
            outside = not Path(text).resolve().is_relative_to(session_folder.resolve())
        except (OSError, ValueError):
            outside = True
        self._data_dir_note.setVisible(outside)

    def _current_session_folder(self) -> Path | None:
        """Return the open experiment's session folder, or ``None`` when none is open."""
        if self._session_manager is None:
            return None
        data_dir = self._session_manager.current_data_dir()
        return data_dir.parent if data_dir is not None else None

    # ------------------------------------------------------------------
    # Public accessors (surfaced by MonitorWindow to ProcedureWindow)
    # ------------------------------------------------------------------

    def get_sample_info(self) -> dict[str, str]:
        """Return the current sample info as a dict.

        Returns:
            Dict with keys ``sample_name``, ``sample_id``, ``comments``.
        """
        return {
            "sample_name": self._sample_name_input.text().strip(),
            "sample_id": self._sample_id_input.text().strip(),
            "comments": self._comments_input.toPlainText().strip(),
        }

    def get_data_dir(self) -> str:
        """Return the configured data directory path.

        Returns:
            Absolute path string; falls back to the open session's own data
            folder, or (no session open) ``app_settings.sessions_root()``, if
            the field is empty.
        """
        return self._data_dir_input.text().strip() or self._default_data_dir_text()

    def _default_data_dir_text(self) -> str:
        """Return the fallback Data Dir text for an empty field/session state.

        The open session's own data folder when one is active (mirrors the
        experiment_changed-driven forcing above), else
        ``app_settings.sessions_root()`` — the same substitution
        ``form_autosave``'s now-empty ``_DEFAULT_DATA_DIR`` relies on the GUI
        to make (form_autosave itself stays Qt-free and cannot resolve a
        platform Documents directory).
        """
        session_folder_data_dir = (
            self._session_manager.current_data_dir()
            if self._session_manager is not None
            else None
        )
        if session_folder_data_dir is not None:
            return str(session_folder_data_dir)
        return str(app_settings.sessions_root())

    def apply_session(self, state: SessionState) -> None:
        """Populate the fields from a loaded session.

        Args:
            state: The session whose sample metadata is applied.
        """
        self._sample_name_input.setText(state.sample_name)
        self._sample_id_input.setText(state.sample_id)
        self._comments_input.setPlainText(state.comments)
        self._data_dir_input.setText(state.data_dir or self._default_data_dir_text())
        self._update_data_dir_note()
