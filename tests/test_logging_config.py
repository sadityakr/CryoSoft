"""Unit tests for cryosoft.core.logging_config's default log location."""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from cryosoft.core.logging_config import _default_log_dir, setup_logging


def test_default_log_dir_is_under_appdata_not_repo():
    """The default log directory resolves under the OS app-data dir, not the repo.

    _default_log_dir() only sets QCoreApplication's applicationName when it is
    still empty (so it never clobbers the name main.py's QApplication sets) —
    but pytest-qt's own session QApplication has already set one
    ("pytest-qt-qapp") by the time any test runs, so this pins the name main.py
    would set to make the assertion deterministic regardless of test order,
    then restores whatever pytest-qt had.
    """
    original_name = QCoreApplication.applicationName()
    QCoreApplication.setApplicationName("CryoSoft")
    try:
        log_dir = _default_log_dir()
    finally:
        QCoreApplication.setApplicationName(original_name)
    assert log_dir.name == "logs"
    assert log_dir.parent.name == "CryoSoft"
    repo_logs_dir = Path(__file__).resolve().parents[1] / "cryosoft" / "logs"
    assert log_dir != repo_logs_dir


def test_setup_logging_writes_to_explicit_log_dir(tmp_path):
    """An explicit log_dir (e.g. for a portable install) is still honored.

    setup_logging() early-returns once the root "cryosoft" logger already has
    handlers (idempotency guard against repeated calls), so any handlers left
    by a prior call are cleared first and restored afterwards to keep this
    test independent of call order.
    """
    root = logging.getLogger("cryosoft")
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)
    try:
        setup_logging(log_dir=tmp_path)
        assert (tmp_path / "cryosoft.log").exists()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
