"""User-level ELN settings — where the backend URL and the API key come from.

**Never a shipped config, never git-tracked.** An ELN account is a property of
the person and the installation, not of the cryostat, so these settings live
in one JSON file in the platform's per-user application-data directory, beside
the other user-level state the app writes at runtime — the same split
``cryosoft.core.paths`` applies to logs and the measurement root, and
``cryosoft.gui.app_settings`` applies to form autosave and user config copies.
Nothing here is ever written into ``cryosoft/configs/``: a config directory
describes a cryostat and is shared/committed, and an API key must never
travel with it.

The API key additionally honours the ``CRYOSOFT_ELAB_APIKEY`` environment
variable, which overrides the file — the way to run against a real instance
without the key ever reaching a disk file at all.

**The key is never logged.** ``ElnSettings`` redacts it in ``repr()`` and
omits it from ``to_dict()`` unless a caller explicitly asks for the secret
(only the adapter does, to build its auth header), so an accidental
``logger.info("settings=%s", settings)`` cannot leak it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Environment variable holding the elabFTW API key, overriding the file.
API_KEY_ENV_VAR = "CRYOSOFT_ELAB_APIKEY"

#: Environment variable pointing at an explicit settings file (tests, and
#: installations that keep user state somewhere unusual).
SETTINGS_PATH_ENV_VAR = "CRYOSOFT_ELN_SETTINGS"

_SETTINGS_FILENAME = "eln-settings.json"

_REDACTED = "***"


def eln_settings_path() -> Path:
    """Resolve the user-level ELN settings file without creating it.

    Precedence, mirroring ``cryosoft.core.paths``:

    1. ``CRYOSOFT_ELN_SETTINGS``, if set and non-empty.
    2. ``%APPDATA%\\CryoSoft\\eln-settings.json`` on Windows
       (``os.name == "nt"``), when ``APPDATA`` is set.
    3. ``~/.config/cryosoft/eln-settings.json`` on other platforms, and as
       the Windows fallback when ``APPDATA`` is unset.

    Returns:
        The resolved path (not guaranteed to exist). Pure — this never
        creates the file or its directory.
    """
    env_path = os.environ.get(SETTINGS_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "CryoSoft" / _SETTINGS_FILENAME
    return Path.home() / ".config" / "cryosoft" / _SETTINGS_FILENAME


def _as_bool(value: object, default: bool) -> bool:
    """Return ``value`` if it is a bool, else ``default`` (defensive parse)."""
    return value if isinstance(value, bool) else default


def _as_float(value: object, default: float) -> float:
    """Coerce a JSON value to ``float``, falling back to ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int) -> int:
    """Coerce a JSON value to ``int``, falling back to ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: object, default: str = "") -> str:
    """Coerce a JSON value to ``str``, falling back to ``default`` on ``None``."""
    return default if value is None else str(value)


def _as_tags(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    """Coerce a JSON value to a tuple of tag strings, falling back to ``default``."""
    if not isinstance(value, list):
        return default
    return tuple(str(item) for item in value if item is not None)


@dataclass(frozen=True)
class ElnSettings:
    """Everything the ELN track reads out of the user-level settings file.

    Every field has a working default and the whole record parses tolerantly,
    so a missing, truncated, or hand-mangled file degrades to "ELN publishing
    is off" instead of breaking startup.

    Attributes:
        enabled: Master switch. ``False`` (the default) means no publisher is
            constructed at all and nothing ever leaves the machine.
        backend: Which adapter to build (``"elabftw"`` today).
        base_url: Backend root URL, e.g. ``https://elab.example.org``.
        api_key: The backend API key. Redacted from ``repr``/``to_dict``.
        team_id: Optional backend team id, when the backend scopes by team.
        template_id: Backend template new entries are created from, or ``""``.
        verify_tls: Verify the server certificate. ``True`` by default and
            only ever turned off deliberately, for a lab instance with a
            self-signed certificate.
        timeout_s: Per-request HTTP timeout. Short by design: the drain runs on
            the GUI timer and must not stall the event loop.
        auto_publish: Publish a run automatically when it finishes. ``False``
            leaves the manual export action as the only trigger.
        max_attachment_bytes: Attachment cap. A data file larger than this is
            recorded as a link instead of uploaded.
        drain_interval_s: How often the GUI timer calls
            ``ElnPublisher.drain_once()``.
        retry_base_s: First retry delay after a failed publish; doubles per
            attempt.
        retry_max_s: Ceiling for that doubling.
        tags: Tags stamped on every entry CryoSoft creates.
    """

    enabled: bool = False
    backend: str = "elabftw"
    base_url: str = ""
    api_key: str = ""
    team_id: str = ""
    template_id: str = ""
    verify_tls: bool = True
    timeout_s: float = 15.0
    auto_publish: bool = True
    max_attachment_bytes: int = 50 * 1024 * 1024
    drain_interval_s: float = 5.0
    retry_base_s: float = 30.0
    retry_max_s: float = 3600.0
    tags: tuple[str, ...] = ("cryosoft",)

    def __repr__(self) -> str:
        """Return a repr with the API key redacted (never log the key)."""
        return (
            f"ElnSettings(enabled={self.enabled!r}, backend={self.backend!r}, "
            f"base_url={self.base_url!r}, api_key={_REDACTED if self.api_key else ''!r}, "
            f"team_id={self.team_id!r}, template_id={self.template_id!r}, "
            f"verify_tls={self.verify_tls!r}, timeout_s={self.timeout_s!r}, "
            f"auto_publish={self.auto_publish!r}, "
            f"max_attachment_bytes={self.max_attachment_bytes!r}, "
            f"drain_interval_s={self.drain_interval_s!r}, "
            f"retry_base_s={self.retry_base_s!r}, retry_max_s={self.retry_max_s!r}, "
            f"tags={self.tags!r})"
        )

    @property
    def is_configured(self) -> bool:
        """Whether publishing can actually run (enabled, with a URL and a key)."""
        return bool(self.enabled and self.base_url and self.api_key)

    def to_dict(self, include_secret: bool = False) -> dict[str, Any]:
        """Return a JSON-safe dict representation.

        Args:
            include_secret: When ``True``, the real ``api_key`` is included —
                for writing the settings file back, never for logging. The
                default redacts it.

        Returns:
            A JSON-serialisable dict of every setting.
        """
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "base_url": self.base_url,
            "api_key": self.api_key if include_secret else (_REDACTED if self.api_key else ""),
            "team_id": self.team_id,
            "template_id": self.template_id,
            "verify_tls": self.verify_tls,
            "timeout_s": self.timeout_s,
            "auto_publish": self.auto_publish,
            "max_attachment_bytes": self.max_attachment_bytes,
            "drain_interval_s": self.drain_interval_s,
            "retry_base_s": self.retry_base_s,
            "retry_max_s": self.retry_max_s,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: object) -> ElnSettings:
        """Build ``ElnSettings`` from a parsed dict, tolerating bad input.

        Args:
            data: Any parsed JSON value; junk degrades to defaults.

        Returns:
            The settings record. A redacted ``api_key`` read back from a
            ``to_dict()`` dump is treated as "no key".
        """
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        api_key = _as_str(data.get("api_key"))
        if api_key == _REDACTED:
            api_key = ""
        return cls(
            enabled=_as_bool(data.get("enabled"), defaults.enabled),
            backend=_as_str(data.get("backend"), defaults.backend) or defaults.backend,
            base_url=_as_str(data.get("base_url")).rstrip("/"),
            api_key=api_key,
            team_id=_as_str(data.get("team_id")),
            template_id=_as_str(data.get("template_id")),
            verify_tls=_as_bool(data.get("verify_tls"), defaults.verify_tls),
            timeout_s=_as_float(data.get("timeout_s"), defaults.timeout_s),
            auto_publish=_as_bool(data.get("auto_publish"), defaults.auto_publish),
            max_attachment_bytes=_as_int(
                data.get("max_attachment_bytes"), defaults.max_attachment_bytes
            ),
            drain_interval_s=_as_float(
                data.get("drain_interval_s"), defaults.drain_interval_s
            ),
            retry_base_s=_as_float(data.get("retry_base_s"), defaults.retry_base_s),
            retry_max_s=_as_float(data.get("retry_max_s"), defaults.retry_max_s),
            tags=_as_tags(data.get("tags"), defaults.tags),
        )


def load_eln_settings(path: Path | None = None) -> ElnSettings:
    """Load the user-level ELN settings, never raising.

    A missing file is the normal case (nobody has configured an ELN yet) and
    yields the disabled defaults silently. An unreadable or malformed file
    logs a WARNING and also yields the defaults — publishing switches itself
    off rather than taking the app down with it.

    Args:
        path: Settings file to read. ``None`` uses ``eln_settings_path()``.

    Returns:
        The parsed settings, with ``CRYOSOFT_ELAB_APIKEY`` overriding the
        file's ``api_key`` when that variable is set.
    """
    settings_path = eln_settings_path() if path is None else path
    settings = ElnSettings()
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("No ELN settings file at %s — publishing stays off", settings_path)
        raw = ""
    except OSError as exc:
        logger.warning("Could not read ELN settings %s: %s", settings_path, exc)
        raw = ""

    if raw:
        try:
            settings = ElnSettings.from_dict(json.loads(raw))
        except (ValueError, TypeError) as exc:
            logger.warning("Malformed ELN settings %s: %s", settings_path, exc)

    env_key = os.environ.get(API_KEY_ENV_VAR)
    if env_key:
        settings = replace(settings, api_key=env_key)
    return settings
