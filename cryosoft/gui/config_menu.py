# ---
# description: |
#   ConfigMenuController: owns MonitorWindow's Config menu — the checkable
#   list of shipped/user configs, the switch-and-restart flow, and the lazy
#   config-editor window. Extracted from monitor_window.py so config
#   management (a startup/restart concern) is separate from live monitoring.
# entry_point: Not run directly. Created by MonitorWindow when a ConfigCatalog
#   is provided.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.config_catalog (ConfigCatalog)
#   - cryosoft.gui.app_settings (active-config persistence)
#   - cryosoft.gui.config_editor (ConfigEditorWindow, lazily imported)
# input: |
#   The hosting window (dialog/menu parent), the QMenu to populate, the
#   catalog, the active config path, a restart callback, and a save-session
#   callable invoked before any restart.
# process: |
#   populate() rebuilds the menu as an exclusive QActionGroup; selecting a
#   different config asks for confirmation, persists it as active, saves the
#   session, and restarts via the injected callback.
# output: |
#   The active config identity in QSettings; an application restart request.
# ---

"""ConfigMenuController — the Config menu, selection flow, and editor launcher."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QMainWindow, QMenu, QMessageBox

from cryosoft.gui import app_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.core.config_catalog import ConfigCatalog


class ConfigMenuController:
    """Builds and drives the Config menu of the hosting window.

    Args:
        window: The hosting QMainWindow (parent for dialogs, actions, and the
            lazily-created config editor).
        menu: The already-added Config QMenu to populate.
        catalog: The ConfigCatalog listing shipped and user configs.
        active_config_path: Path string of the currently-active config, or None.
        restart_callback: Called after a config switch is persisted, or None.
        save_session: Callable persisting the current GUI session; invoked
            before any restart so no session content is lost.
    """

    def __init__(
        self,
        window: QMainWindow,
        menu: QMenu,
        catalog: ConfigCatalog,
        active_config_path: str | None,
        restart_callback: Callable[[], None] | None,
        save_session: Callable[[], None],
    ) -> None:
        self._window = window
        self._menu = menu
        self._catalog = catalog
        self._active_config_path = active_config_path
        self._restart_callback = restart_callback
        self._save_session = save_session
        self._config_editor = None  # lazily created
        self.populate()

    def populate(self) -> None:
        """(Re)build the Config menu: a checkable list plus the editor entry."""
        self._menu.clear()
        group = QActionGroup(self._window)
        group.setExclusive(True)
        active = self._active_config_path
        for entry in self._catalog.list_configs():
            label = entry.name + ("  (read-only)" if entry.read_only else "")
            action = QAction(label, self._window, checkable=True)
            path_str = str(entry.path)
            if active and Path(path_str).resolve() == Path(active).resolve():
                action.setChecked(True)
            action.triggered.connect(
                lambda _checked, p=path_str: self._on_select_config(p)
            )
            group.addAction(action)
            self._menu.addAction(action)

        self._menu.addSeparator()
        editor_action = QAction("Open Config Editor…", self._window)
        editor_action.setToolTip("Edit device/instrument configs with validation")
        editor_action.triggered.connect(self._on_open_config_editor)
        self._menu.addAction(editor_action)

    def _on_select_config(self, path: str) -> None:
        """Switch the active config to ``path`` after a warning, then restart."""
        if self._active_config_path and (
            Path(path).resolve() == Path(self._active_config_path).resolve()
        ):
            return
        reply = QMessageBox.question(
            self._window,
            "Switch Config",
            f"Switch to config '{Path(path).name}'?\n\n"
            "CryoSoft will save the current session and restart to load it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.populate()  # revert the radio selection
            return
        self.apply_active_config(path)

    def apply_active_config(self, path: str) -> None:
        """Persist the config at ``path`` as active, save the session, restart."""
        self._save_session()
        entry = self._catalog.get_by_path(path)
        if entry is not None:
            app_settings.set_config_active(entry.name, entry.source)
        if self._restart_callback is not None:
            self._restart_callback()

    def _on_open_config_editor(self) -> None:
        """Open the config editor window (lazily created)."""
        from cryosoft.gui.config_editor import ConfigEditorWindow

        if self._config_editor is None:
            self._config_editor = ConfigEditorWindow(
                self._catalog,
                active_config_path=self._active_config_path,
                apply_callback=self.apply_active_config,
                parent=self._window,
            )
        self._config_editor.show()
        self._config_editor.raise_()
        self._config_editor.activateWindow()
