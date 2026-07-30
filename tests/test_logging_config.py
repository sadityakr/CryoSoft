# ---
# description: |
#   Behavior tests for cryosoft.core.logging_config: log_directory()'s
#   resolution precedence (CRYOSOFT_LOG_DIR env var, platform user-data
#   location, packaged fallback), its purity (resolves but never creates a
#   directory), that setup_logging() still honours an explicit log_dir
#   argument, and that the four JSONL streams (status, trend_raw,
#   trend_3min, trend_hourly) are configured as TimedRotatingFileHandlers
#   with the right when/backupCount/utc and are idempotent across repeated
#   setup_logging() calls.
# last_updated: 2026-07-25
# ---

"""Tests for the CryoSoft log-directory resolver and logging setup."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cryosoft.core import logging_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a clean slate for the resolver's env inputs."""
    monkeypatch.delenv("CRYOSOFT_LOG_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)


def _pretend_platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``log_directory()`` take the ``name`` branch on any host OS.

    Setting the real ``os.name`` attribute is a *global* change, and pathlib
    reads it to pick a concrete flavour when a ``Path`` is constructed. A test
    that patches it to ``"nt"`` therefore makes pathlib attempt a
    ``WindowsPath`` on Linux (``NotImplementedError``), and one that patches it
    to ``"posix"`` makes pathlib attempt a ``PosixPath`` on Windows — in both
    cases the resolver blows up before the branch under test is reached.

    Rebinding only this module's ``os`` reference leaves the real ``os.name``
    intact for pathlib. The stub carries the two attributes ``log_directory()``
    actually uses, and ``environ`` is the live mapping so ``monkeypatch.setenv``
    keeps working through it.

    Args:
        monkeypatch: The active pytest monkeypatch fixture.
        name: The ``os.name`` value to present, ``"nt"`` or ``"posix"``.
    """
    monkeypatch.setattr(
        logging_config, "os", SimpleNamespace(name=name, environ=os.environ)
    )


def test_log_directory_honours_explicit_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(tmp_path / "custom_logs"))
    assert logging_config.log_directory() == tmp_path / "custom_logs"


def test_log_directory_empty_env_var_is_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", "")
    _pretend_platform(monkeypatch, "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = logging_config.log_directory()
    assert result == tmp_path / "AppData" / "Local" / "CryoSoft" / "logs"


def test_log_directory_windows_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pretend_platform(monkeypatch, "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = logging_config.log_directory()
    assert result == tmp_path / "AppData" / "Local" / "CryoSoft" / "logs"


def test_log_directory_windows_without_localappdata_falls_back_to_packaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_platform(monkeypatch, "nt")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    result = logging_config.log_directory()
    assert result == Path(logging_config.__file__).parent.parent / "logs"


def test_log_directory_posix_uses_xdg_style_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path.home() is stubbed so the expected value is deterministic rather
    # than whatever the host's real home directory happens to be.
    fake_home = tmp_path / "home" / "fakeuser"
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(lambda: fake_home))
    _pretend_platform(monkeypatch, "posix")
    result = logging_config.log_directory()
    assert result == fake_home / ".local" / "state" / "cryosoft" / "logs"


def test_log_directory_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "does_not_exist_yet" / "logs"
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(fresh))
    result = logging_config.log_directory()
    assert result == fresh
    assert not result.exists()


_JSONL_LOGGER_NAMES = (
    "cryosoft.status",
    "cryosoft.trend_raw",
    "cryosoft.trend_3min",
    "cryosoft.trend_hourly",
)


def test_setup_logging_honours_explicit_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stray CRYOSOFT_LOG_DIR must never override an explicit argument.
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(tmp_path / "should_not_be_used"))

    # Fresh loggers so repeated test-suite calls to setup_logging() elsewhere
    # don't short-circuit this one via the idempotency guards.
    for name in ("cryosoft", "cryosoft.status"):
        logger = logging.getLogger(name)
        logger.handlers.clear()

    explicit_dir = tmp_path / "explicit_logs"
    logging_config.setup_logging(log_dir=explicit_dir)

    assert explicit_dir.exists()
    assert (explicit_dir / "cryosoft.log").exists()
    assert not (tmp_path / "should_not_be_used").exists()

    # Clean up handlers so later tests in the suite get a fresh setup_logging().
    for name in ("cryosoft", "cryosoft.status"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()


def _clear_jsonl_loggers() -> None:
    """Close and drop handlers on the root + four JSONL loggers.

    Shared setup/teardown helper so each test below gets a truly fresh
    ``setup_logging()`` call, unaffected by idempotency guards tripped by
    other tests or by import-time side effects elsewhere in the suite.
    """
    for name in ("cryosoft", *_JSONL_LOGGER_NAMES):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()


@pytest.fixture
def _fresh_jsonl_loggers():
    _clear_jsonl_loggers()
    yield
    _clear_jsonl_loggers()


def _jsonl_handlers(name: str) -> list[logging.handlers.TimedRotatingFileHandler]:
    """Return only the stream handlers ``setup_logging()`` installed on ``name``.

    The full ``logger.handlers`` list is not CryoSoft's alone: pytest attaches
    its own capture handlers to exactly these four loggers, precisely because
    they set ``propagate=False`` and would otherwise be invisible to ``caplog``.
    Asserting on that list pins the test runner's internals rather than the
    behaviour under test, and breaks whenever the runner changes them.

    Args:
        name: Logger name, e.g. ``"cryosoft.status"``.

    Returns:
        The logger's timed-rotating file handlers, in attachment order.
    """
    return [
        handler
        for handler in logging.getLogger(name).handlers
        if isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    ]


def test_jsonl_handlers_are_timed_rotating_with_expected_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_jsonl_loggers: None
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(tmp_path / "unused"))
    logging_config.setup_logging(log_dir=tmp_path / "logs")

    expected = {
        "cryosoft.status": ("midnight", 7),
        "cryosoft.trend_raw": ("midnight", 2),
        "cryosoft.trend_3min": ("midnight", 8),
        "cryosoft.trend_hourly": ("W0", 53),
    }

    for name, (when, backup_count) in expected.items():
        logger = logging.getLogger(name)
        handlers = _jsonl_handlers(name)
        assert len(handlers) == 1
        handler = handlers[0]
        assert handler.utc is True
        assert handler.backupCount == backup_count
        assert handler.when.lower() == when.lower() or (
            when == "midnight" and handler.when.lower() == "midnight"
        )
        assert logger.propagate is False
        assert logger.level == logging.INFO


def test_jsonl_handlers_write_expected_filenames(
    tmp_path: Path, _fresh_jsonl_loggers: None
) -> None:
    log_dir = tmp_path / "logs"
    logging_config.setup_logging(log_dir=log_dir)

    expected_files = {
        "cryosoft.status": "status.jsonl",
        "cryosoft.trend_raw": "trend_history_raw.jsonl",
        "cryosoft.trend_3min": "trend_history_3min.jsonl",
        "cryosoft.trend_hourly": "trend_history_hourly.jsonl",
    }
    for name, filename in expected_files.items():
        handlers = _jsonl_handlers(name)
        assert len(handlers) == 1
        assert Path(handlers[0].baseFilename).name == filename


def test_setup_logging_twice_does_not_duplicate_jsonl_handlers(
    tmp_path: Path, _fresh_jsonl_loggers: None
) -> None:
    log_dir = tmp_path / "logs"
    logging_config.setup_logging(log_dir=log_dir)
    logging_config.setup_logging(log_dir=log_dir)

    for name in _JSONL_LOGGER_NAMES:
        assert len(_jsonl_handlers(name)) == 1


def test_foreign_handler_does_not_suppress_the_jsonl_writer(
    tmp_path: Path, _fresh_jsonl_loggers: None
) -> None:
    """A handler attached by something else must not disable CryoSoft's stream.

    The idempotency guard asks whether this stream's own writer is installed,
    so an unrelated handler no longer makes setup_logging() conclude it has
    already run and skip the writer — which would leave status.jsonl empty
    while the application looked healthy.
    """
    foreign = logging.NullHandler()
    logging.getLogger("cryosoft.status").addHandler(foreign)

    logging_config.setup_logging(log_dir=tmp_path / "logs")

    assert len(_jsonl_handlers("cryosoft.status")) == 1
    # The foreign handler is left alone, not evicted.
    assert foreign in logging.getLogger("cryosoft.status").handlers
