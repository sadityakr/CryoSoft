"""The **Embedded assistant** (L6): a tool-use loop over the **Agent gateway**.

The package's public surface, re-exported so a caller writes
``from cryosoft.session.assistant import AssistantRuntime`` and never has to
know which module a name lives in. See ``README.md`` for the standards this
folder keeps: the system-prompt standard, the **Chat client** contract, the
**Assistant transcript** record standard, and the assistant's thread rule.
"""

from cryosoft.session.assistant.clients import (
    AnthropicChatClient,
    ChatRequest,
    FakeChatClient,
)
from cryosoft.session.assistant.runtime import (
    ASSISTANT_SYSTEM_PROMPT,
    DEFAULT_MAX_STEPS,
    STATUS_CALLING,
    STATUS_IDLE,
    STATUS_REFUSED,
    STATUS_THINKING,
    AssistantError,
    AssistantRuntime,
    ChatClient,
    ChatResult,
    ToolCall,
    empty_cost_line,
)
from cryosoft.session.assistant.transcript import (
    RECORD_ASSISTANT,
    RECORD_TOOL,
    RECORD_USER,
    AssistantTranscript,
    read_transcript,
)

__all__ = [
    "ASSISTANT_SYSTEM_PROMPT",
    "DEFAULT_MAX_STEPS",
    "RECORD_ASSISTANT",
    "RECORD_TOOL",
    "RECORD_USER",
    "STATUS_CALLING",
    "STATUS_IDLE",
    "STATUS_REFUSED",
    "STATUS_THINKING",
    "AnthropicChatClient",
    "AssistantError",
    "AssistantRuntime",
    "AssistantTranscript",
    "ChatClient",
    "ChatRequest",
    "ChatResult",
    "FakeChatClient",
    "ToolCall",
    "empty_cost_line",
    "read_transcript",
]
