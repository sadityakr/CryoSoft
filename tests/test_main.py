"""Tests for cryosoft.main's session-tier startup wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from cryosoft.main import _ensure_guest_user_registered, _resolve_active_session
from cryosoft.session.models import GUEST_USER_ID, GUEST_USER_NAME, User
from cryosoft.session.store import SessionStore, UserRoster, _write_json_atomic


def test_resolve_active_session_creates_bootstrap_when_none_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-ever launch (no active pointer): a bootstrap session is created and activated."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")

    user_id, session_id = _resolve_active_session(store)

    assert user_id == "jdoe"
    assert store.get_active() == (user_id, session_id)
    session = store.load(user_id, session_id)
    assert session is not None
    assert session.user_id == "jdoe"
    assert session.name == "jdoe"


def test_resolve_active_session_falls_back_to_guest_when_logged_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No logged-in user yet: the bootstrap session is owned/named by the Guest identity."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: None)
    store = SessionStore(tmp_path / "sessions")

    user_id, session_id = _resolve_active_session(store)

    assert user_id == GUEST_USER_ID
    session = store.load(user_id, session_id)
    assert session is not None
    assert session.name == GUEST_USER_ID
    assert session.user_id == GUEST_USER_ID


def test_resolve_active_session_returns_existing_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-active, loadable session is returned unchanged — no new session created."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")
    existing = store.create_session(name="Cooldown 3", user_id="jdoe")
    store.set_active("jdoe", existing.session_id)

    user_id, session_id = _resolve_active_session(store)

    assert (user_id, session_id) == ("jdoe", existing.session_id)
    assert store.list_sessions("jdoe") == [existing.session_id]


def test_resolve_active_session_recovers_from_corrupt_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active pointer naming a session that fails to load is replaced, not fatal."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")
    store.set_active("jdoe", "does_not_exist")

    user_id, session_id = _resolve_active_session(store)

    assert session_id != "does_not_exist"
    assert store.get_active() == (user_id, session_id)
    assert store.load(user_id, session_id) is not None


def test_resolve_active_session_recovers_from_legacy_flat_active_pointer_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-per-user-nesting pointer ({"active": "..."}) is treated as unset, not fatal."""
    from cryosoft.gui import app_settings

    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")
    store = SessionStore(tmp_path / "sessions")
    _write_json_atomic(store.root / "active.json", {"active": "some_old_session_id"})

    user_id, session_id = _resolve_active_session(store)

    assert user_id == "jdoe"
    assert store.get_active() == (user_id, session_id)
    assert store.load(user_id, session_id) is not None


def test_ensure_guest_user_registered_adds_guest_once(tmp_path: Path) -> None:
    roster = UserRoster(tmp_path / "users.json")
    assert roster.get(GUEST_USER_ID) is None

    _ensure_guest_user_registered(roster)

    guest = roster.get(GUEST_USER_ID)
    assert guest is not None
    assert guest.name == GUEST_USER_NAME


def test_ensure_guest_user_registered_does_not_clobber_existing_guest_customization(
    tmp_path: Path,
) -> None:
    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id=GUEST_USER_ID, name="Visiting Scientist"))

    _ensure_guest_user_registered(roster)

    assert roster.get(GUEST_USER_ID).name == "Visiting Scientist"


def test_resolve_active_session_does_not_hand_a_user_someone_elses_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug: everyone kept landing in the first user's session.

    The active pointer is machine-wide while sessions are per-user folders,
    so a pointer left on the previous user's session must not be handed to
    whoever logs in next — their experiments would be written into that
    user's folder while carrying their own name.
    """
    from cryosoft.gui import app_settings

    store = SessionStore(tmp_path / "sessions")
    theirs = store.create_session(name="lennart", user_id="lennart")
    store.set_active("lennart", theirs.session_id)
    mine = store.create_session(name="akashdeep", user_id="akashdeep")
    monkeypatch.setattr(app_settings, "current_user_id", lambda: "akashdeep")

    user_id, session_id = _resolve_active_session(store)

    assert (user_id, session_id) == ("akashdeep", mine.session_id)
    assert store.get_active() == ("akashdeep", mine.session_id)


def test_resolve_active_session_creates_one_for_a_user_who_owns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new user inherits nobody's session — they get their own."""
    from cryosoft.gui import app_settings

    store = SessionStore(tmp_path / "sessions")
    theirs = store.create_session(name="lennart", user_id="lennart")
    store.set_active("lennart", theirs.session_id)
    monkeypatch.setattr(app_settings, "current_user_id", lambda: "aditya")

    user_id, session_id = _resolve_active_session(store)

    assert user_id == "aditya"
    assert store.get_active() == ("aditya", session_id)
    # lennart's own session is untouched by any of this.
    assert store.list_sessions("lennart") == [theirs.session_id]


def test_resolve_active_session_keeps_the_pointer_when_it_is_already_yours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owning several sessions must not silently move you off the active one."""
    from cryosoft.gui import app_settings

    store = SessionStore(tmp_path / "sessions")
    first = store.create_session(name="One", user_id="jdoe", session_dirname="one")
    store.create_session(name="Two", user_id="jdoe", session_dirname="two")
    store.set_active("jdoe", first.session_id)
    monkeypatch.setattr(app_settings, "current_user_id", lambda: "jdoe")

    assert _resolve_active_session(store) == ("jdoe", first.session_id)
