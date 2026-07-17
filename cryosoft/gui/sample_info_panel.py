# ---
# description: |
#   SampleInfoPanel: the Sample Info quadrant of MonitorWindow — sample name,
#   ID, comments, and the data-directory field with its Browse button.
#   Extracted from monitor_window.py; it is the single owner of session-level
#   sample metadata in the GUI, read by ProcedureWindow through
#   MonitorWindow's get_sample_info/get_data_dir callables.
# entry_point: Not run directly. Hosted as MonitorWindow's bottom-left quadrant.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.gui.session (SessionState)
#   - cryosoft.gui.theme (button classes)
# input: |
#   A loaded SessionState (via apply_session) to prefill the fields.
# process: |
#   Builds the form inside a QScrollArea (objectNames sample_info_quadrant /
#   sample_info_scroll and the *_input fields are preserved API for tests).
# output: |
#   get_sample_info()/get_data_dir() read the live field values.
# ---

"""SampleInfoPanel — the Sample Info quadrant (session-level metadata)."""

from __future__ import annotations

import qtawesome as qta
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.gui.session import SessionState
from cryosoft.gui.theme import TEXT_PRIMARY

_DEFAULT_DATA_DIR = "C:/CryoData"


class SampleInfoPanel(QWidget):
    """The Sample Info quadrant: name, ID, comments, and data-directory fields.

    ObjectNames (``sample_info_quadrant``, ``sample_info_scroll``,
    ``sample_name_input``, ``sample_id_input``, ``comments_input``,
    ``data_dir_input``, ``browse_btn``) are preserved API — tests and muscle
    memory rely on them.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sample_info_quadrant")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Sample Info</b>"))

        scroll = QScrollArea()
        scroll.setObjectName("sample_info_scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._build_form())
        outer.addWidget(scroll)

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
        self._data_dir_input = QLineEdit(_DEFAULT_DATA_DIR)
        self._data_dir_input.setObjectName("data_dir_input")
        browse_btn = QPushButton("Browse…")
        browse_btn.setObjectName("browse_btn")
        browse_btn.setIcon(qta.icon("fa5s.folder-open", color=TEXT_PRIMARY))
        browse_btn.setToolTip("Choose the directory where run data is saved")
        browse_btn.clicked.connect(self._on_browse_dir)
        dir_row.addWidget(self._data_dir_input)
        dir_row.addWidget(browse_btn)
        form.addRow("Data Dir:", dir_row)

        return box

    def _on_browse_dir(self) -> None:
        """Open a directory browser and fill the data-dir field."""
        selected = QFileDialog.getExistingDirectory(
            self, "Select Data Directory", self._data_dir_input.text()
        )
        if selected:
            self._data_dir_input.setText(selected)

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
            Absolute path string; falls back to ``"C:/CryoData"`` if empty.
        """
        return self._data_dir_input.text().strip() or _DEFAULT_DATA_DIR

    def apply_session(self, state: SessionState) -> None:
        """Populate the fields from a loaded session.

        Args:
            state: The session whose sample metadata is applied.
        """
        self._sample_name_input.setText(state.sample_name)
        self._sample_id_input.setText(state.sample_id)
        self._comments_input.setPlainText(state.comments)
        self._data_dir_input.setText(state.data_dir or _DEFAULT_DATA_DIR)
