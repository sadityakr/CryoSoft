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

The drafting assistant's own model, key, token cap and price table live in the
same file under ``assistant`` (``AssistantSettings``), for exactly the same
reasons: an LLM account is a property of the person and the installation, the
key must never travel with a config directory, and it is redacted from
``repr()`` and ``to_dict()`` under the identical rule. Its own environment
override is ``CRYOSOFT_ASSISTANT_APIKEY``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Environment variable holding the elabFTW API key, overriding the file.
API_KEY_ENV_VAR = "CRYOSOFT_ELAB_APIKEY"

#: Environment variable holding the drafting assistant's API key, overriding
#: the file's ``assistant.api_key``.
ASSISTANT_API_KEY_ENV_VAR = "CRYOSOFT_ASSISTANT_APIKEY"

#: Environment variable pointing at an explicit settings file (tests, and
#: installations that keep user state somewhere unusual).
SETTINGS_PATH_ENV_VAR = "CRYOSOFT_ELN_SETTINGS"

_SETTINGS_FILENAME = "eln-settings.json"

_REDACTED = "***"

#: The model a draft is written by when the settings file names none. Chosen
#: as the vendor's current general-purpose default; a setup that wants a
#: cheaper or a newer one sets ``assistant.model`` and, if it is not in the
#: price table below, ``assistant.prices``.
DEFAULT_ASSISTANT_MODEL = "claude-opus-5"

#: Largest number of tokens a single draft may generate. A drafted summary is
#: a handful of paragraphs; the cap is what stops a runaway completion from
#: costing an unbounded amount. Deliberately several times what the prose
#: itself needs: on the current default model the vendor's reasoning is on by
#: default and is generated — and billed — against this same cap, so a cap
#: sized for the prose alone would truncate the draft rather than bound it.
DEFAULT_ASSISTANT_MAX_TOKENS = 8192

#: List price per one million tokens, per model, in US dollars — the source
#: the reported ``cost_usd`` of a draft is computed from.
#:
#: Source: Anthropic's published API pricing (https://www.anthropic.com/pricing),
#: as of 2026-06-24. These are LIST prices: an account on partner or negotiated
#: rates overrides the whole table from the settings file's ``assistant.prices``,
#: which is why the numbers live in settings rather than in the drafting code.
#: A model with no row here reports ``cost_usd`` of 0.0 and logs a WARNING —
#: never a guessed price.
DEFAULT_MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}


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


def _as_prices(value: object) -> dict[str, dict[str, float]]:
    """Coerce a JSON value to a per-model price table, dropping malformed rows.

    Args:
        value: Any parsed JSON value. A non-mapping, or a row that names
            neither an input nor an output price, degrades to the default
            table and a skipped row respectively — a mangled price must never
            stop a draft, it must only stop the draft claiming a cost.

    Returns:
        ``{model: {"input": usd_per_mtok, "output": usd_per_mtok}}``.
    """
    if not isinstance(value, dict):
        return {model: dict(row) for model, row in DEFAULT_MODEL_PRICES.items()}
    table: dict[str, dict[str, float]] = {}
    for model, row in value.items():
        if not isinstance(row, dict):
            logger.warning("Ignoring malformed price row for model %r", model)
            continue
        table[str(model)] = {
            "input": _as_float(row.get("input"), 0.0),
            "output": _as_float(row.get("output"), 0.0),
        }
    return table


@dataclass(frozen=True)
class AssistantSettings:
    """The drafting assistant's half of the user-level settings file.

    Lives under ``assistant`` in the same JSON file as the notebook's own
    settings and follows the same rules: every field has a working default,
    the whole record parses tolerantly, and the key is redacted from
    ``repr()`` and from ``to_dict()`` unless a caller explicitly asks for the
    secret.

    Attributes:
        enabled: Master switch for drafting. ``False`` (the default) means no
            **Draft client** is built and no model is ever called.
        model: The model id a draft is written by.
        api_key: The API key, redacted from ``repr``/``to_dict``. Empty means
            "let the vendor SDK resolve credentials from the environment",
            which is how an installation keeps the key out of every file.
        max_tokens: Cap on one draft's generated tokens.
        prices: ``{model: {"input": usd_per_mtok, "output": usd_per_mtok}}``,
            the table a draft's ``cost_usd`` is computed from. Defaults to
            ``DEFAULT_MODEL_PRICES``; an account on other rates replaces it.
    """

    enabled: bool = False
    model: str = DEFAULT_ASSISTANT_MODEL
    api_key: str = ""
    max_tokens: int = DEFAULT_ASSISTANT_MAX_TOKENS
    prices: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            model: dict(row) for model, row in DEFAULT_MODEL_PRICES.items()
        }
    )

    def __repr__(self) -> str:
        """Return a repr with the API key redacted (never log the key)."""
        return (
            f"AssistantSettings(enabled={self.enabled!r}, model={self.model!r}, "
            f"api_key={_REDACTED if self.api_key else ''!r}, "
            f"max_tokens={self.max_tokens!r}, prices={sorted(self.prices)!r})"
        )

    def to_dict(self, include_secret: bool = False) -> dict[str, Any]:
        """Return a JSON-safe dict representation.

        Args:
            include_secret: When ``True``, the real ``api_key`` is included —
                for writing the settings file back, never for logging.

        Returns:
            A JSON-serialisable dict of every setting.
        """
        return {
            "enabled": self.enabled,
            "model": self.model,
            "api_key": (
                self.api_key if include_secret else (_REDACTED if self.api_key else "")
            ),
            "max_tokens": self.max_tokens,
            "prices": {model: dict(row) for model, row in self.prices.items()},
        }

    @classmethod
    def from_dict(cls, data: object) -> AssistantSettings:
        """Build ``AssistantSettings`` from a parsed dict, tolerating bad input.

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
            model=_as_str(data.get("model"), defaults.model) or defaults.model,
            api_key=api_key,
            max_tokens=_as_int(data.get("max_tokens"), defaults.max_tokens),
            prices=_as_prices(data.get("prices")),
        )


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
        assistant: The drafting assistant's own settings (model, key, token
            cap, price table). Off by default, so an installation that never
            drafts carries no LLM footprint at all.
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
    assistant: AssistantSettings = field(default_factory=AssistantSettings)

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
            f"tags={self.tags!r}, assistant={self.assistant!r})"
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
            "assistant": self.assistant.to_dict(include_secret=include_secret),
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
            assistant=AssistantSettings.from_dict(data.get("assistant")),
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
        file's ``api_key`` and ``CRYOSOFT_ASSISTANT_APIKEY`` overriding the
        file's ``assistant.api_key`` when those variables are set.
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
    assistant_key = os.environ.get(ASSISTANT_API_KEY_ENV_VAR)
    if assistant_key:
        settings = replace(
            settings, assistant=replace(settings.assistant, api_key=assistant_key)
        )
    return settings
