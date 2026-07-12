# ---
# description: |
#   ConfigEditorWindow: an interactive editor for CryoSoft device/instrument
#   configs. Lists shipped (read-only) and user configs, edits devices.yaml /
#   monitor.yaml as text behind a hard validation gate (no invalid config can be
#   saved), keeps a named version history per user config (browse + restore), and
#   applies a config (which triggers a restart via an injected callback).
# entry_point: Opened from MonitorWindow's Config menu ("Open Config Editor…").
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.config_catalog (ConfigCatalog)
#   - cryosoft.core.station (validate_config_dir)
# input: |
#   A ConfigCatalog, the active config path, and an apply_callback(path) that the
#   host (MonitorWindow) uses to persist-and-restart.
# process: |
#   Editing a shipped config is disabled; "Duplicate to edit" forks it into a
#   user copy. Save validates the editor text in a temp directory before writing
#   a new version; Apply saves then calls apply_callback.
# output: |
#   New/updated user config files and version snapshots on disk; a restart
#   request via apply_callback.
# ---

"""ConfigEditorWindow — validated, versioned editing of instrument configs."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt

from cryosoft.core.config_catalog import ConfigCatalog
from cryosoft.core.station import validate_config_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.core.config_catalog import ConfigEntry

_DEVICES = "devices.yaml"
_MONITOR = "monitor.yaml"


class ConfigEditorWindow(QMainWindow):
    """A window to browse, edit, validate, version, and apply configs.

    Args:
        catalog: The config catalog.
        active_config_path: The currently-active config path (for the Apply
            confirmation and initial selection).
        apply_callback: Called with a config path when the user applies a
            config; the host persists it and restarts.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        catalog: ConfigCatalog,
        active_config_path: str | None = None,
        apply_callback: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._active_config_path = active_config_path
        self._apply_callback = apply_callback

        self.setWindowTitle("CryoSoft — Config Editor")
        self.resize(900, 600)
        self._build_ui()
        self._load_configs()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ── Config selector row ───────────────────────────────────────
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Config:"))
        self._config_selector = QComboBox()
        self._config_selector.setObjectName("config_selector")
        self._config_selector.currentIndexChanged.connect(self._on_config_selected)
        selector_row.addWidget(self._config_selector, stretch=1)
        self._readonly_label = QLabel("")
        self._readonly_label.setObjectName("readonly_label")
        selector_row.addWidget(self._readonly_label)
        self._duplicate_btn = QPushButton("Duplicate to edit")
        self._duplicate_btn.setObjectName("duplicate_btn")
        self._duplicate_btn.setToolTip("Copy this read-only config into an editable user copy")
        self._duplicate_btn.clicked.connect(self._on_duplicate)
        selector_row.addWidget(self._duplicate_btn)
        root.addLayout(selector_row)

        # ── Editors (left) | version history (right) ──────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        self._tabs = QTabWidget()
        self._devices_editor = QPlainTextEdit()
        self._devices_editor.setObjectName("devices_editor")
        self._monitor_editor = QPlainTextEdit()
        self._monitor_editor.setObjectName("monitor_editor")
        self._tabs.addTab(self._devices_editor, "devices.yaml")
        self._tabs.addTab(self._monitor_editor, "monitor.yaml")
        self._tabs.setMinimumWidth(420)
        splitter.addWidget(self._tabs)

        version_panel = QWidget()
        version_layout = QVBoxLayout(version_panel)
        version_layout.addWidget(QLabel("Versions"))
        self._version_list = QListWidget()
        self._version_list.setObjectName("version_list")
        version_layout.addWidget(self._version_list)
        self._restore_btn = QPushButton("Restore selected")
        self._restore_btn.setObjectName("restore_btn")
        self._restore_btn.setToolTip("Replace the current config with the selected version")
        self._restore_btn.clicked.connect(self._on_restore)
        version_layout.addWidget(self._restore_btn)
        version_panel.setMinimumWidth(220)
        splitter.addWidget(version_panel)
        splitter.setSizes([620, 260])
        root.addWidget(splitter, stretch=1)

        # ── Validation output ─────────────────────────────────────────
        self._validation_output = QPlainTextEdit()
        self._validation_output.setObjectName("validation_output")
        self._validation_output.setReadOnly(True)
        self._validation_output.setMaximumHeight(90)
        self._validation_output.setPlaceholderText("Validation results appear here.")
        root.addWidget(self._validation_output)

        # ── Action buttons ────────────────────────────────────────────
        button_row = QHBoxLayout()
        self._validate_btn = QPushButton("Validate")
        self._validate_btn.setObjectName("validate_btn")
        self._validate_btn.setToolTip("Check the config without saving")
        self._validate_btn.clicked.connect(self._on_validate)
        self._save_btn = QPushButton("Save Version")
        self._save_btn.setObjectName("save_btn")
        self._save_btn.setToolTip("Validate, then save as a new version (no restart)")
        self._save_btn.clicked.connect(self._on_save)
        self._apply_btn = QPushButton("Apply && Restart")
        self._apply_btn.setObjectName("apply_btn")
        self._apply_btn.setToolTip("Save, make this the active config, and restart CryoSoft")
        self._apply_btn.clicked.connect(self._on_apply)
        button_row.addWidget(self._validate_btn)
        button_row.addStretch()
        button_row.addWidget(self._save_btn)
        button_row.addWidget(self._apply_btn)
        root.addLayout(button_row)

    # ------------------------------------------------------------------
    # Config list + selection
    # ------------------------------------------------------------------

    def _load_configs(self, select_path: str | None = None) -> None:
        """Populate the selector from the catalog and select one entry.

        Args:
            select_path: Path to select after loading; defaults to the active
                config, else the first entry.
        """
        self._config_selector.blockSignals(True)
        self._config_selector.clear()
        entries = self._catalog.list_configs()
        for entry in entries:
            suffix = "  (read-only)" if entry.read_only else ""
            self._config_selector.addItem(f"{entry.name}{suffix}", str(entry.path))
        self._config_selector.blockSignals(False)

        target = select_path or self._active_config_path
        index = 0
        if target:
            for i in range(self._config_selector.count()):
                if Path(self._config_selector.itemData(i)).resolve() == Path(target).resolve():
                    index = i
                    break
        if self._config_selector.count():
            self._config_selector.setCurrentIndex(index)
            self._on_config_selected(index)

    def _current_entry(self) -> ConfigEntry | None:
        """Return the ConfigEntry currently selected, or None."""
        path = self._config_selector.currentData()
        return self._catalog.get_by_path(path) if path else None

    def _on_config_selected(self, _index: int) -> None:
        """Load the selected config's text and update editability + versions."""
        entry = self._current_entry()
        if entry is None:
            return
        devices = entry.path / _DEVICES
        monitor = entry.path / _MONITOR
        self._devices_editor.setPlainText(
            devices.read_text(encoding="utf-8") if devices.is_file() else ""
        )
        self._monitor_editor.setPlainText(
            monitor.read_text(encoding="utf-8") if monitor.is_file() else ""
        )
        self._set_editable(not entry.read_only)
        self._readonly_label.setText("read-only baseline" if entry.read_only else "")
        self._populate_versions(entry)
        self._validation_output.clear()

    def _set_editable(self, editable: bool) -> None:
        """Enable/disable editing and the save/apply/version controls."""
        self._devices_editor.setReadOnly(not editable)
        self._monitor_editor.setReadOnly(not editable)
        self._save_btn.setEnabled(editable)
        self._apply_btn.setEnabled(editable)
        self._restore_btn.setEnabled(editable)
        self._duplicate_btn.setEnabled(not editable)

    def _populate_versions(self, entry: ConfigEntry) -> None:
        """Fill the version list for a user config (empty for shipped)."""
        self._version_list.clear()
        if entry.read_only:
            return
        for version in self._catalog.list_versions(entry.name):
            text = version.label or version.version_id
            if version.label:
                text = f"{version.label}  ({version.version_id})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, version.version_id)
            self._version_list.addItem(item)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_duplicate(self) -> None:
        """Fork the selected shipped config into an editable user copy."""
        entry = self._current_entry()
        if entry is None or not entry.read_only:
            return
        name, ok = QInputDialog.getText(
            self, "Duplicate Config", "Name for the editable copy:", text=entry.name
        )
        if not ok or not name.strip():
            return
        try:
            new_entry = self._catalog.fork_shipped(entry.name, name.strip())
        except (FileExistsError, FileNotFoundError) as exc:
            QMessageBox.warning(self, "Duplicate Config", str(exc))
            return
        self._load_configs(select_path=str(new_entry.path))

    def _validate_text(self) -> list[str]:
        """Validate the current editor contents via a temp directory.

        Returns:
            A list of error strings; empty means valid.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / _DEVICES).write_text(
                self._devices_editor.toPlainText(), encoding="utf-8"
            )
            (tmp_path / _MONITOR).write_text(
                self._monitor_editor.toPlainText(), encoding="utf-8"
            )
            return validate_config_dir(str(tmp_path))

    def _on_validate(self) -> None:
        """Validate and show the result, without saving."""
        errors = self._validate_text()
        self._show_validation(errors)

    def _show_validation(self, errors: list[str]) -> None:
        """Render a validation result to the output box."""
        if errors:
            self._validation_output.setPlainText(
                "Invalid config:\n- " + "\n- ".join(errors)
            )
        else:
            self._validation_output.setPlainText("Valid.")

    def _on_save(self) -> bool:
        """Validate then save a new version. Returns True on success."""
        entry = self._current_entry()
        if entry is None or entry.read_only:
            return False
        errors = self._validate_text()
        self._show_validation(errors)
        if errors:
            QMessageBox.warning(
                self,
                "Cannot Save",
                "This config is invalid and was not saved. See the validation "
                "output for details.",
            )
            return False
        label, ok = QInputDialog.getText(
            self, "Save Version", "Version name (optional):"
        )
        if not ok:
            return False
        self._catalog.save_version(
            entry.name,
            self._devices_editor.toPlainText(),
            self._monitor_editor.toPlainText(),
            label=label.strip(),
        )
        self._populate_versions(entry)
        self._validation_output.setPlainText("Saved a new version.")
        return True

    def _on_apply(self) -> None:
        """Save, then (after confirmation) make active and restart."""
        entry = self._current_entry()
        if entry is None or entry.read_only:
            return
        if not self._on_save():
            return
        reply = QMessageBox.question(
            self,
            "Apply Config",
            f"Make '{entry.name}' the active config and restart CryoSoft now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._apply_callback is not None:
            self._apply_callback(str(entry.path))

    def _on_restore(self) -> None:
        """Restore the selected version over the active config text."""
        entry = self._current_entry()
        if entry is None or entry.read_only:
            return
        item = self._version_list.currentItem()
        if item is None:
            return
        version_id = item.data(Qt.ItemDataRole.UserRole)
        self._catalog.restore_version(entry.name, version_id)
        # Reload the editors from the now-restored active files.
        self._on_config_selected(self._config_selector.currentIndex())
        self._validation_output.setPlainText(f"Restored version {version_id}.")
