"""GUI tests for ConfigEditorWindow (validated, versioned config editing).

Uses the real shipped configs as read-only baselines and a tmp_path user dir.
Modal dialogs (QInputDialog / QMessageBox) are monkeypatched per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from cryosoft.core.config_catalog import ConfigCatalog
from cryosoft.gui import app_settings
from cryosoft.gui.config_editor import ConfigEditorWindow


@pytest.fixture
def editor_catalog(tmp_path):
    """Catalog over the real shipped configs and a throwaway user dir."""
    return ConfigCatalog(app_settings.shipped_config_dir(), tmp_path / "user")


def _editor(catalog, qtbot, apply_cb=None, active=None):
    win = ConfigEditorWindow(catalog, active_config_path=active, apply_callback=apply_cb)
    qtbot.addWidget(win)
    return win


def _select(win, name):
    """Select the config named ``name`` in the editor's selector."""
    for i in range(win._config_selector.count()):
        if Path(win._config_selector.itemData(i)).name == name:
            win._config_selector.setCurrentIndex(i)
            return
    raise AssertionError(f"config '{name}' not in selector")


def test_shipped_config_is_read_only(editor_catalog, qtbot):
    """Selecting a shipped config disables editing and enables Duplicate."""
    win = _editor(editor_catalog, qtbot)
    _select(win, "sim_cryostat")
    assert win._devices_editor.isReadOnly()
    assert not win._save_btn.isEnabled()
    assert not win._apply_btn.isEnabled()
    assert win._duplicate_btn.isEnabled()


def test_duplicate_creates_editable_copy(editor_catalog, qtbot, monkeypatch):
    """Duplicate forks a shipped config into an editable user copy."""
    win = _editor(editor_catalog, qtbot)
    _select(win, "sim_cryostat")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("sim_copy", True))
    win._on_duplicate()
    assert win._current_entry().name == "sim_copy"
    assert not win._devices_editor.isReadOnly()
    assert win._save_btn.isEnabled()


def test_save_invalid_config_refused(editor_catalog, qtbot, monkeypatch):
    """Saving an invalid config is blocked; no new version is written."""
    editor_catalog.fork_shipped("sim_cryostat", "sim_copy")  # seeds 1 version
    win = _editor(editor_catalog, qtbot)
    _select(win, "sim_copy")
    win._devices_editor.setPlainText("real_drivers: {: broken")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(1))
    assert win._on_save() is False
    assert warned
    assert len(editor_catalog.list_versions("sim_copy")) == 1  # only the seed


def test_save_valid_config_creates_version(editor_catalog, qtbot, monkeypatch):
    """Saving a valid config adds a version."""
    editor_catalog.fork_shipped("sim_cryostat", "sim_copy")
    win = _editor(editor_catalog, qtbot)
    _select(win, "sim_copy")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("v1", True))
    assert win._on_save() is True
    assert len(editor_catalog.list_versions("sim_copy")) == 2  # seed + v1


def test_apply_calls_callback_with_path(editor_catalog, qtbot, monkeypatch):
    """Apply saves then invokes the apply callback with the config path."""
    editor_catalog.fork_shipped("sim_cryostat", "sim_copy")
    applied = []
    win = _editor(editor_catalog, qtbot, apply_cb=lambda p: applied.append(p))
    _select(win, "sim_copy")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("v", True))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    win._on_apply()
    assert applied and Path(applied[0]).name == "sim_copy"


def test_restore_reverts_editor_text(editor_catalog, qtbot, monkeypatch):
    """Restoring the seed version reverts an edit."""
    editor_catalog.fork_shipped("sim_cryostat", "sim_copy")
    win = _editor(editor_catalog, qtbot)
    _select(win, "sim_copy")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("mod", True))
    win._devices_editor.setPlainText(
        win._devices_editor.toPlainText() + "\n# an edit\n"
    )
    win._on_save()
    # Newest-first: the seed version is the last row.
    win._version_list.setCurrentRow(win._version_list.count() - 1)
    win._on_restore()
    assert "# an edit" not in win._devices_editor.toPlainText()
