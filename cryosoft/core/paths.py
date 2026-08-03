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


def _app_config_path() -> Path:
    """Resolve the machine-level settings file's path (not guaranteed to exist).

    Returns:
        ``%ProgramData%\\CryoSoft\\App-config.yaml`` on Windows
        (``os.name == "nt"``), or ``/etc/cryosoft/App-config.yaml`` on other
        platforms.
    """
    if os.name == "nt":
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(program_data) / "CryoSoft" / "App-config.yaml"
    return Path("/etc/cryosoft/App-config.yaml")


def _read_measurement_root_setting(config_path: Path) -> str | None:
    """Read the ``measurement_root`` key out of the machine settings file.

    ``App-config.yaml`` has exactly one key for now, so this is a tiny
    single-key line parser rather than a general YAML parser: it does not
    pull in a PyYAML dependency for a stdlib-only contract C1 module (see
    the module docstring). It reads the first ``measurement_root: <value>``
    line, splits on the first colon, and strips surrounding whitespace and
    matching quotes from the value. Comments (``#``) and any other key are
    ignored, so the format stays forward-compatible with the file growing a
    second key later.

    Args:
        config_path: Path to the settings file to read.

    Returns:
        The value of ``measurement_root``, or ``None`` if the file is
        missing, unreadable, or has no such key.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep or key.strip() != "measurement_root":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        return value

    return None


def measurement_root() -> Path:
    """Resolve the fixed CryoSoft measurement root without creating it.

    The measurement root is a machine-level, admin-set value — deliberately
    not GUI-editable or per-user — so it stays fixed across a station's
    lifetime rather than drifting via live settings.

    Precedence:

    1. ``CRYOSOFT_MEASUREMENT_ROOT`` environment variable, if set and
       non-empty.
    2. The ``measurement_root`` key in a machine-level settings file,
       ``%ProgramData%\\CryoSoft\\App-config.yaml`` on Windows
       (``os.name == "nt"``) or ``/etc/cryosoft/App-config.yaml`` on other
       platforms. A missing file, a missing key, or a blank value all fall
       through to step 3.
    3. No fallback: raises ``RuntimeError``.

    This is a pure function: it only resolves and returns a path, it never
    creates the directory, the settings file, or anything else.

    Returns:
        The resolved measurement root path (not guaranteed to exist).

    Raises:
        RuntimeError: Neither the environment variable nor the settings
            file resolves a measurement root, naming the exact settings
            file path that was checked.
    """
    env_dir = os.environ.get("CRYOSOFT_MEASUREMENT_ROOT")
    if env_dir:
        return Path(env_dir)

    config_path = _app_config_path()
    setting = _read_measurement_root_setting(config_path)
    if setting:
        return Path(setting)

    raise RuntimeError(
        "No measurement root configured. Set the CRYOSOFT_MEASUREMENT_ROOT "
        "environment variable, or create "
        f"{config_path} with a 'measurement_root: <path>' line."
    )
