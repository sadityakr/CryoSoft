# ---
# description: |
#   app_settings: a one-function factory for the application's QSettings object,
#   plus machine-level identity persisted through it — which config is active
#   (config_active/set_config_active), who is currently logged in
#   (current_user_id/set_current_user_id), and where L6 session (experiment)
#   folders live (sessions_root/set_sessions_root). It exists purely as a test
#   seam. Windows persist their geometry via QSettings("CryoSoft", "CryoSoft"),
#   which on Windows is backed by the real registry
#   (HKCU\Software\CryoSoft\CryoSoft). Routing every construction through this
#   factory lets GUI tests monkeypatch it to point at a throwaway .ini file, so
#   a pytest run never overwrites the user's real saved geometry.
#   session_file_path(user_id) is the per-user form autosave (used only when no
#   L6 session is open): with a logged-in user_id it resolves to that person's
#   own file under sessions/, so switching users switches what's remembered.
#   sessions_root() resolves to <Documents>/CryoData at runtime whenever the
#   QSettings key is unset, so the default is never persisted by merely
#   reading it — only set_sessions_root() writes the key.
# entry_point: Not run directly. Called by MonitorWindow and ProcedureWindow.
# dependencies:
#   - PyQt6 >= 6.5
# input: |
#   None. get_settings() takes no arguments.
# process: |
#   get_settings() constructs and returns a QSettings scoped to the CryoSoft
#   organisation/application.
# output: |
#   A QSettings instance. In production this is the native (registry) store;
#   under test it is monkeypatched to an INI-format file.
# ---

"""app_settings — QSettings factory used as a test seam.

Dependency seam: a single indirection point (this factory) that tests
monkeypatch so GUI tests never touch the real registry. Windows import the
*module* and call ``app_settings.get_settings()`` rather than importing the
function directly, so that ``monkeypatch.setattr(app_settings, "get_settings",
...)`` is seen at every call site.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSettings, QStandardPaths

_ORGANISATION = "CryoSoft"
_APPLICATION = "CryoSoft"

_SESSION_FILENAME = "last_session.json"
_SESSIONS_SUBDIR = "sessions"
_ACTIVE_CONFIG_NAME_KEY = "ActiveConfig/name"
_ACTIVE_CONFIG_SOURCE_KEY = "ActiveConfig/source"
_CURRENT_USER_KEY = "CurrentUser/user_id"
_SESSIONS_ROOT_KEY = "Sessions/root"
_SESSIONS_ROOT_DEFAULT_DIRNAME = "CryoData"


def get_settings() -> QSettings:
    """Return the application's QSettings store.

    Returns:
        A ``QSettings`` scoped to the CryoSoft organisation and application. In
        production this is the platform-native store (the Windows registry);
        GUI tests monkeypatch this function to return an INI-file store instead.
    """
    return QSettings(_ORGANISATION, _APPLICATION)


def session_file_path(user_id: str | None = None) -> Path:
    """Return the path to a persistent form-autosave JSON file.

    The file lives in the platform per-installation application-data
    directory (``%APPDATA%/CryoSoft/`` on Windows), separate from both the
    registry and the user's measurement data directory. This is the second
    persistence tier: ``get_settings()`` holds machine-specific window/dock
    *chrome*, while this file holds portable session *content* (sample
    metadata, procedure params, run queue). Like ``get_settings``, GUI tests
    monkeypatch this function to redirect it into a throwaway directory.

    Args:
        user_id: When given (someone is logged in — see ``current_user_id``),
            returns that person's own autosave file
            (``%APPDATA%/CryoSoft/sessions/<user_id>.json``), so switching
            users switches what's remembered instead of one person's fields
            overwriting another's. ``None`` (nobody logged in yet, or a
            caller that predates the login feature) returns the original
            shared ``last_session.json``.

    Returns:
        The absolute ``Path`` of the session JSON file. The parent directory is
        not guaranteed to exist yet; ``form_autosave.save`` creates it on first write.
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if user_id:
        return Path(base) / _SESSIONS_SUBDIR / f"{user_id}.json"
    return Path(base) / _SESSION_FILENAME


def user_config_dir() -> Path:
    """Return the directory holding the user's editable config copies.

    ``%APPDATA%/CryoSoft/configs`` on Windows. Separate from the shipped,
    read-only configs in the repo. Monkeypatchable test seam.

    Returns:
        The ``Path`` of the user config directory (may not exist yet).
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    return Path(base) / "configs"


def shipped_config_dir() -> Path:
    """Return the repo's read-only shipped-config directory (``cryosoft/configs``).

    Resolved relative to the package so it is independent of the current working
    directory.

    Returns:
        The ``Path`` of the shipped config directory.
    """
    return Path(__file__).resolve().parents[1] / "configs"


def config_active() -> tuple[str, str] | None:
    """Return the saved active config's ``(name, source)`` identity, or None.

    The active config is machine-level (which cryostat this install controls),
    so it lives in QSettings rather than the per-session JSON file. Identity
    (name + source) is stored rather than a resolved absolute path, so the
    saved selection stays valid across clones/worktrees: the caller re-derives
    the actual directory via ``shipped_config_dir()``/``user_config_dir()`` at
    load time instead of trusting a path that may no longer exist.

    Returns:
        A ``(name, source)`` tuple (``source`` is ``"shipped"`` or ``"user"``),
        or None when no config has been selected yet.
    """
    settings = get_settings()
    name = settings.value(_ACTIVE_CONFIG_NAME_KEY)
    source = settings.value(_ACTIVE_CONFIG_SOURCE_KEY)
    if not name or not source:
        return None
    return (str(name), str(source))


def set_config_active(name: str, source: str) -> None:
    """Persist a config's ``(name, source)`` identity as active for next launch.

    Args:
        name: The config's directory name (``ConfigEntry.name``).
        source: ``"shipped"`` or ``"user"`` (``ConfigEntry.source``).
    """
    settings = get_settings()
    settings.setValue(_ACTIVE_CONFIG_NAME_KEY, name)
    settings.setValue(_ACTIVE_CONFIG_SOURCE_KEY, source)


def current_user_id() -> str | None:
    """Return the roster id of whoever is currently "logged in", or None.

    Machine-level like the active config: persists across restarts until the
    User menu's "Log in as…" switches it. Identity only — no password, no
    session token; the roster (``cryosoft.session.store.UserRoster``) is the
    source of truth for whether the id still exists.

    Returns:
        The roster ``user_id``, or ``None`` when nobody has logged in yet.
    """
    value = get_settings().value(_CURRENT_USER_KEY)
    return str(value) if value else None


def sessions_root() -> Path:
    """Return the configured root directory for L6 session (experiment) folders.

    Machine-level like ``config_active``/``current_user_id`` — this changes
    rarely, so it lives in ``QSettings``, not the per-user JSON tier. When the
    key has never been set, resolves to ``<Documents>/CryoData`` via
    ``QStandardPaths`` **at call time** rather than persisting that default —
    only ``set_sessions_root`` ever writes the key, so a plain read never has
    a side effect and a later OS/user-profile change is picked up
    automatically. Nothing here creates the directory; the store creates it
    lazily on the first actual save.

    Returns:
        The sessions root ``Path`` (may not exist yet).
    """
    value = get_settings().value(_SESSIONS_ROOT_KEY)
    if value:
        return Path(str(value))
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DocumentsLocation
    )
    return Path(base) / _SESSIONS_ROOT_DEFAULT_DIRNAME


def set_sessions_root(path: str | Path) -> None:
    """Persist the sessions root for this and future launches.

    Args:
        path: The directory under which session (experiment) folders should
            live going forward. Not validated or created here — an
            already-running ``ExperimentStore`` keeps its old root until the
            app is restarted (see ``gui/README.md`` / the User menu's
            "Sessions Folder…" action).
    """
    get_settings().setValue(_SESSIONS_ROOT_KEY, str(path))


def set_current_user_id(user_id: str | None) -> None:
    """Persist (or clear) who is currently logged in.

    Args:
        user_id: The roster id to remember, or ``None`` to log out (falls
            back to the shared ``last_session.json`` on next read).
    """
    settings = get_settings()
    if user_id:
        settings.setValue(_CURRENT_USER_KEY, user_id)
    else:
        settings.remove(_CURRENT_USER_KEY)
