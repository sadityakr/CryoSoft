# ---
# description: |
#   app_settings: a one-function factory for the application's QSettings object.
#   It exists purely as a test seam. Windows persist their geometry via
#   QSettings("CryoSoft", "CryoSoft"), which on Windows is backed by the real
#   registry (HKCU\Software\CryoSoft\CryoSoft). Routing every construction
#   through this factory lets GUI tests monkeypatch it to point at a throwaway
#   .ini file, so a pytest run never overwrites the user's real saved geometry.
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


def get_settings() -> QSettings:
    """Return the application's QSettings store.

    Returns:
        A ``QSettings`` scoped to the CryoSoft organisation and application. In
        production this is the platform-native store (the Windows registry);
        GUI tests monkeypatch this function to return an INI-file store instead.
    """
    return QSettings(_ORGANISATION, _APPLICATION)


def session_file_path() -> Path:
    """Return the path to the persistent ``last_session.json`` file.

    The file lives in the platform per-user application-data directory
    (``%APPDATA%/CryoSoft/`` on Windows), separate from both the registry and
    the user's measurement data directory. This is the second persistence tier:
    ``get_settings()`` holds machine-specific window/dock *chrome*, while this
    file holds portable session *content* (sample metadata, procedure params,
    run queue). Like ``get_settings``, GUI tests monkeypatch this function to
    redirect it into a throwaway directory.

    Returns:
        The absolute ``Path`` of the session JSON file. The parent directory is
        not guaranteed to exist yet; ``session.save`` creates it on first write.
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    return Path(base) / _SESSION_FILENAME
