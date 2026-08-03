# ---
# description: |
#   Behavior tests for cryosoft.core.paths: log_directory()'s resolution
#   precedence (CRYOSOFT_LOG_DIR env var, platform user-data location,
#   packaged fallback) and its purity (resolves but never creates a
#   directory).
# last_updated: 2026-08-03
# ---

"""Tests for the CryoSoft installation-path resolver."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cryosoft.core import paths


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
    monkeypatch.setattr(paths, "os", SimpleNamespace(name=name, environ=os.environ))


def test_log_directory_honours_explicit_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(tmp_path / "custom_logs"))
    assert paths.log_directory() == tmp_path / "custom_logs"


def test_log_directory_empty_env_var_is_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", "")
    _pretend_platform(monkeypatch, "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = paths.log_directory()
    assert result == tmp_path / "AppData" / "Local" / "CryoSoft" / "logs"


def test_log_directory_windows_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pretend_platform(monkeypatch, "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = paths.log_directory()
    assert result == tmp_path / "AppData" / "Local" / "CryoSoft" / "logs"


def test_log_directory_windows_without_localappdata_falls_back_to_packaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_platform(monkeypatch, "nt")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    result = paths.log_directory()
    assert result == Path(paths.__file__).parent.parent / "logs"


def test_log_directory_posix_uses_xdg_style_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path.home() is stubbed so the expected value is deterministic rather
    # than whatever the host's real home directory happens to be.
    fake_home = tmp_path / "home" / "fakeuser"
    monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: fake_home))
    _pretend_platform(monkeypatch, "posix")
    result = paths.log_directory()
    assert result == fake_home / ".local" / "state" / "cryosoft" / "logs"


def test_log_directory_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "does_not_exist_yet" / "logs"
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(fresh))
    result = paths.log_directory()
    assert result == fresh
    assert not result.exists()
