"""The two **Chat client**s: the vendor's, and the one every test runs against.

``FakeChatClient`` is the ``sim_`` rule applied to the model — it answers from
a canned script, records every request it was given (the prompt, the messages,
the tools it was offered and the thread it ran on), and models the failure mode
that matters, an unreachable model, as an ``AssistantError``. No network, no
SDK, no key.

``AnthropicChatClient`` is the real one. Its SDK is an optional dependency
(``pip install cryosoft[assistant]``), imported lazily inside ``__init__`` so
that a checkout without it imports this module and runs every test unchanged,
and an installation that switches the assistant on without installing it gets
one clear ``AssistantError`` naming the command that fixes it — never a stack
trace from an import.

**The key is never logged and never leaves this object.** It comes from the
same place the **Draft client**'s does — the user-level settings file's
``assistant.api_key`` or its ``CRYOSOFT_ASSISTANT_APIKEY`` override, redacted
by ``AssistantSettings`` in ``repr()`` and ``to_dict()`` — and is passed to
the vendor SDK and nowhere else. An empty key deliberately means "let the SDK
resolve credentials from the environment", which is how an installation keeps
it out of every file.

**Which SDK, and why.** This is a plain Messages-API tool-use loop over the
``anthropic`` package: one request per step, the tools handed in verbatim, the
loop itself in ``runtime.py``. That is the shape the assistant needs, because
its tool surface must be EXACTLY ``Gateway.tool_schemas()`` and its tool
execution EXACTLY ``Gateway.call_tool()`` — a harness that brings a tool
surface of its own (a filesystem, a shell) would give the assistant a second
way to act, outside the permission matrix, the kill switch and the feed. Owning
the loop here is also what lets the thread rule hold: the model call is the
only thing that leaves this thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cryosoft.session.assistant.runtime import (
    AssistantError,
    ChatResult,
    ToolCall,
)
from cryosoft.session.eln.settings import AssistantSettings

logger = logging.getLogger(__name__)

__all__ = ["ChatRequest", "FakeChatClient", "AnthropicChatClient"]


@dataclass(frozen=True)
class ChatRequest:
    """One request a ``FakeChatClient`` was given, as a test reads it back.

    Attributes:
        system: The system prompt it was called with.
        messages: The conversation, copied.
        tools: The tool declarations it was offered — asserted against
            ``Gateway.tool_schemas()`` by the tests, which is how "the
            assistant's tools are the gateway's" stays true.
        max_tokens: The cap it was given.
        thread: The identifier of the thread the call actually ran on, so a
            test can assert the model call left the GUI thread.
    """

    system: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    max_tokens: int
    thread: int


@dataclass
class FakeChatClient:
    """An in-memory **Chat client** — the workhorse of every assistant test.

    Scripted: it answers with ``replies[i]`` for the *i*-th call and repeats
    the last one once the script runs out, so a test writes exactly the
    conversation it means to test and nothing else.

    Attributes:
        replies: The answers to give, in order.
        offline: When ``True``, every call raises ``AssistantError``.
        requests: One ``ChatRequest`` per call, in order.
    """

    replies: list[ChatResult] = field(default_factory=list)
    offline: bool = False
    requests: list[ChatRequest] = field(default_factory=list)

    def create_message(
        self,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> ChatResult:
        """Answer from the script, recording the request and its thread.

        Args:
            system: The system prompt.
            messages: The conversation so far.
            tools: The tool declarations.
            max_tokens: Cap on the generated tokens.

        Returns:
            The next scripted ``ChatResult``, or the last one again once the
            script is exhausted; an empty script answers with nothing to say.

        Raises:
            AssistantError: When ``offline`` is set.
        """
        index = len(self.requests)
        self.requests.append(
            ChatRequest(
                system=system,
                messages=tuple(dict(message) for message in messages),
                tools=tuple(dict(tool) for tool in tools),
                max_tokens=int(max_tokens),
                thread=threading.get_ident(),
            )
        )
        if self.offline:
            raise AssistantError("the fake chat client is offline")
        if not self.replies:
            return ChatResult(text_blocks=("",), model="fake-model")
        return self.replies[min(index, len(self.replies) - 1)]


def _as_int(value: object, default: int = 0) -> int:
    """Coerce a vendor value to ``int``, falling back to ``default`` on junk.

    Args:
        value: Anything the SDK reported.
        default: What to answer when it is not a number.

    Returns:
        The integer value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _block_dicts(content: object) -> tuple[dict[str, Any], ...]:
    """Return one answer's content blocks as JSON-safe dicts, or ``()``.

    The blocks are echoed back verbatim on the next request, so anything the
    model needs to see again travels unchanged. A vendor object that cannot be
    dumped yields ``()``, and the runtime rebuilds an equivalent message from
    the text and the tool calls instead — a degraded conversation is better
    than a failed one.

    Args:
        content: The answer's ``content`` list, as the SDK returned it.

    Returns:
        One dict per block, in order.
    """
    if not isinstance(content, Sequence):
        return ()
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, Mapping):
            blocks.append(dict(block))
            continue
        dump = getattr(block, "model_dump", None)
        if not callable(dump):
            return ()
        try:
            payload = dump(mode="json", exclude_none=True)
        except Exception:  # noqa: BLE001 — a shape we cannot dump is not fatal
            logger.warning("Assistant: could not serialise an answer block")
            return ()
        if not isinstance(payload, Mapping):
            return ()
        blocks.append(dict(payload))
    return tuple(blocks)


class AnthropicChatClient:
    """The real **Chat client**: one Messages request per step of the loop.

    Holds no conversation of its own — the runtime owns the messages, the
    tools and the accounting, and this object turns one request into one
    ``ChatResult``.
    """

    def __init__(self, settings: AssistantSettings | None = None) -> None:
        """Build the vendor client from the assistant settings.

        Args:
            settings: The assistant settings — the model, the token cap and
                the API key. ``None`` uses the defaults, whose empty key means
                the SDK resolves credentials from the environment itself.

        Raises:
            AssistantError: The vendor SDK is not installed, or the client
                could not be constructed (a malformed base URL, an unusable
                key).
        """
        try:
            import anthropic
        except ImportError as error:  # the optional extra is not installed
            raise AssistantError(
                "The embedded assistant needs the 'anthropic' package, which "
                "is an optional dependency: install it with "
                "`pip install cryosoft[assistant]`."
            ) from error

        self._settings = settings or AssistantSettings()
        try:
            self._client = (
                anthropic.Anthropic(api_key=self._settings.api_key)
                if self._settings.api_key
                else anthropic.Anthropic()
            )
        except Exception as error:  # the SDK raises its own types
            raise AssistantError(
                f"could not build the assistant client: {error}"
            ) from error
        logger.info("Assistant chat client ready (model=%s)", self._settings.model)

    @property
    def settings(self) -> AssistantSettings:
        """The assistant settings this client was built with."""
        return self._settings

    def create_message(
        self,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> ChatResult:
        """Ask the model once and return its answer with the token counts.

        Args:
            system: The system prompt.
            messages: The conversation so far, in the vendor's message shape.
            tools: The tool declarations — exactly ``tool_schemas()``.
            max_tokens: Cap on the generated tokens.

        Returns:
            The answer, the tools it asked for, the model that actually
            answered, and the tokens each half consumed.

        Raises:
            AssistantError: Any vendor failure — unreachable, refused,
                rate-limited or malformed — mapped to this package's one
                exception type so a caller has exactly one thing to catch.
        """
        try:
            message = self._client.messages.create(
                model=self._settings.model,
                max_tokens=max(int(max_tokens), 1),
                system=system,
                messages=[dict(entry) for entry in messages],
                tools=[dict(tool) for tool in tools],
            )
        except Exception as error:  # the SDK raises its own types
            raise AssistantError(
                f"the assistant model could not be reached: {error}"
            ) from error

        content = list(getattr(message, "content", []) or [])
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            kind = str(getattr(block, "type", "") or "")
            if kind == "text":
                texts.append(str(getattr(block, "text", "")))
            elif kind == "tool_use":
                raw = getattr(block, "input", {})
                calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        args=dict(raw) if isinstance(raw, Mapping) else {},
                    )
                )
        usage = getattr(message, "usage", None)
        return ChatResult(
            text_blocks=tuple(texts),
            tool_calls=tuple(calls),
            model=str(getattr(message, "model", "") or self._settings.model),
            input_tokens=_as_int(getattr(usage, "input_tokens", 0)),
            output_tokens=_as_int(getattr(usage, "output_tokens", 0)),
            stop_reason=str(getattr(message, "stop_reason", "") or ""),
            content=_block_dicts(content),
        )
