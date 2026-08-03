# ---
# description: |
#   Resolves the machine-local, per-installation directories CryoSoft reads
#   and writes outside of source control. Today that is the log directory
#   alone (log_directory()); this module is the place any further such
#   directory belongs, so path resolution stays in one stdlib-only spot
#   rather than being duplicated per caller.
# entry_point: Not run directly. Imported by logging_config (which re-exports
#   log_directory for its existing callers), the troubleshoot CLI and status
#   reader, and the GUI trend modules.
# dependencies: stdlib only (contract C1 — see the module docstring).
# input: |
#   Environment only: CRYOSOFT_LOG_DIR overrides the log directory outright;
#   LOCALAPPDATA selects the per-user location on Windows.
# process: |
#   Pure resolution — each function picks a path by documented precedence and
#   returns it. Nothing here creates a directory or touches the filesystem;
#   the caller that writes owns the mkdir.
# output: |
#   pathlib.Path values, not guaranteed to exist.
# last_updated: 2026-08-03
# ---

"""CryoSoft installation-path resolution.

Machine-local directories — where logs go on *this* installation — are a
deployment property, not a source-tree one, and resolving them is neither
logging's job nor any single caller's. Keeping every such rule here means a
caller asks for a directory instead of reimplementing the precedence, and
the rules stay readable side by side.

This module is import-linter contract C1 foundation: it must import nothing
else from the ``cryosoft`` package, stdlib only.
"""

from __future__ import annotations

import os
from pathlib import Path


def log_directory() -> Path:
    """Resolve the CryoSoft log directory without creating it.

    Precedence:

    1. ``CRYOSOFT_LOG_DIR`` environment variable, if set and non-empty.
    2. ``%LOCALAPPDATA%\\CryoSoft\\logs`` on Windows (``os.name == "nt"``),
       or ``~/.local/state/cryosoft/logs`` on other platforms — provided the
       relevant platform variable (``LOCALAPPDATA`` on Windows) is set.
    3. ``cryosoft/logs/`` (next to this package) as the final fallback, used
       when the platform-specific location above is unavailable.

    This is a pure function: it only resolves and returns a path, it never
    creates the directory or any file in it. ``setup_logging()`` is
    responsible for the ``mkdir(parents=True, exist_ok=True)``.

    No migration of existing log files is performed when the resolved
    location changes (e.g. moving off ``CRYOSOFT_LOG_DIR`` or between
    machines). Logs are disposable operational telemetry, not data of
    record: the new location simply starts empty. Do not write a migrator
    for this.

    Returns:
        The resolved log directory path (not guaranteed to exist).
    """
    env_dir = os.environ.get("CRYOSOFT_LOG_DIR")
    if env_dir:
        return Path(env_dir)

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "CryoSoft" / "logs"
    else:
        return Path.home() / ".local" / "state" / "cryosoft" / "logs"

    return Path(__file__).parent.parent / "logs"
