# ---
# description: |
#   Behavior tests for cryosoft.core.logging_config: log_directory()'s
#   resolution precedence (CRYOSOFT_LOG_DIR env var, platform user-data
#   location, packaged fallback), its purity (resolves but never creates a
#   directory), and that setup_logging() still honours an explicit log_dir
#   argument.
# last_updated: 2026-07-25
# ---

"""Tests for the CryoSoft log-directory resolver and logging setup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cryosoft.core import logging_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a clean slate for the resolver's env inputs."""
    monkeypatch.delenv("CRYOSOFT_LOG_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)


def test_log_directory_honours_explicit_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(tmp_path / "custom_logs"))
    assert logging_config.log_directory() == tmp_path / "custom_logs"


def test_log_directory_empty_env_var_is_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", "")
    monkeypatch.setattr(logging_config.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = logging_config.log_directory()
    assert result == tmp_path / "AppData" / "Local" / "CryoSoft" / "logs"


def test_log_directory_windows_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_config.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = logging_config.log_directory()
    assert result == tmp_path / "AppData" / "Local" / "CryoSoft" / "logs"


def test_log_directory_windows_without_localappdata_falls_back_to_packaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_config.os, "name", "nt")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    result = logging_config.log_directory()
    assert result == Path(logging_config.__file__).parent.parent / "logs"


def test_log_directory_posix_uses_xdg_style_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path.home() is stubbed to a plain, already-constructed Path object
    # (built under the real, unpatched os.name) rather than letting
    # log_directory() call the real Path.home(): on this Windows test
    # machine, pathlib's home() dispatches on os.name at construction time
    # and raises UnsupportedOperation if os.name has been monkeypatched to
    # "posix". Dividing an existing Path with "/" does not re-dispatch on
    # os.name, so this isolates the branch under test without that crash.
    fake_home = tmp_path / "home" / "fakeuser"
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(logging_config.os, "name", "posix")
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
