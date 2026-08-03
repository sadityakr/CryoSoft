# ---
# description: |
#   Behavior tests for cryosoft.main's session-tier startup wiring —
#   specifically _resolve_active_session(), the small testable seam that
#   picks (or bootstraps) the active SessionStore session before
#   ExperimentStore/ExperimentManager are constructed. The rest of main()
#   builds a full QApplication and real hardware-adjacent objects, so it is
#   not unit-tested directly here (see docs/plans/session-tier-and-terminology.md,
#   "Startup wiring (decided)").
# last_updated: 2026-08-03
# ---

"""Tests for cryosoft.main's session-tier startup wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from cryosoft.main import _resolve_active_session
from cryosoft.session.store import SessionStore


def test_resolve_active_session_creates_bootstrap_when_none_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-ever launch (no active pointer): a bootstrap session is created and activated."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")

    session_id = _resolve_active_session(store)

    assert store.get_active() == session_id
    session = store.load(session_id)
    assert session is not None
    assert session.user_id == "jdoe"
    assert session.name == "jdoe"


def test_resolve_active_session_falls_back_to_default_name_when_logged_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No logged-in user yet: the bootstrap session is named/owned "default"/""."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: None)
    store = SessionStore(tmp_path / "sessions")

    session_id = _resolve_active_session(store)

    session = store.load(session_id)
    assert session is not None
    assert session.name == "default"
    assert session.user_id == ""


def test_resolve_active_session_returns_existing_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-active, loadable session is returned unchanged — no new session created."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")
    existing = store.create_session(name="Cooldown 3", user_id="jdoe")
    store.set_active(existing.session_id)

    session_id = _resolve_active_session(store)

    assert session_id == existing.session_id
    assert store.list_sessions() == [existing.session_id]


def test_resolve_active_session_recovers_from_corrupt_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active pointer naming a session that fails to load is replaced, not fatal."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")
    store.set_active("does_not_exist")

    session_id = _resolve_active_session(store)

    assert session_id != "does_not_exist"
    assert store.get_active() == session_id
    assert store.load(session_id) is not None
