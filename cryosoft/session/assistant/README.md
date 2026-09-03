# cryosoft/session/assistant — the Embedded assistant (L6)

## Purpose

Let the physicist ask the running experiment a question in plain language, and
let the answer come from the instrument itself rather than from a guess. The
assistant reads the station and acts on it through **exactly the tools the
Agent gateway publishes**, executed by **exactly `Gateway.call_tool()`**.

That sentence is the whole design. The assistant is not a third client of the
engine beside the human and the agent — it *is* the agent client, with a chat
window on the front of it. It acts under the same **Role**, is refused by the
same permission matrix, is stopped by the same **Kill switch**, is bounded by
the same **Attendance** rule and the same session envelope, and leaves the same
**Agent feed** trail as an agent in another process. Nothing in this folder
defines a capability, reaches an instrument, or holds a Station, a VI or the
Orchestrator. A permission the assistant had and an agent did not would be a
second control path, and a second control path is what the gateway exists to
prevent.

The folder's second job is **evidence**. The feed records what an autonomous
client *did*; it cannot record what the assistant was *asked* and what it
*said*, because those never reach the engine. The **Assistant transcript**
writes that half into the experiment's own folder, so copying the folder copies
the whole story.

## Architecture layer

**L6, inside the Session Manager**, one level above the gateway. Imports
`cryosoft.core.*` and `cryosoft.session.*` downward and nothing else — bound by
the same import-linter contracts as the rest of `cryosoft/session/`: **C11**
(session never imports gui/main/drivers/VIs/procedures) and **C12** (nothing
below the GUI imports session). No new contract: this package is inside
`cryosoft.session` and is covered by both already.

It imports PyQt6 for its threading and its signals, exactly as
`session/gateway/local_server.py` does. Qt is not the GUI layer; a widget is.

## Entry (what comes in)

- **A `Gateway`**, already built with a `Role`, an actor id and — when an
  experiment is open — the experiment's **Agent feed**. Whoever owns the engine
  builds it; this package never does, which is what keeps the assistant's
  authority a property of the connection rather than something it can set.
- **A `ChatClient`**: `create_message(system, messages, tools, max_tokens)
  -> ChatResult`. `AnthropicChatClient` is the real one, `FakeChatClient` is
  the one every test uses.
- **`AssistantSettings`** (`session/eln/settings.py`): the model, the token cap,
  the API key and the price table, from the user-level settings file. The same
  record the **Draft client** reads, with the same
  `CRYOSOFT_ASSISTANT_APIKEY` override and the same redaction — an LLM account
  is a property of the person and the installation, never of a config folder.
- **An `AssistantTranscript`** (optional): the evidence file to append to.
  Without one the conversation still runs and still publishes its records to a
  window; nothing is kept.
- **Questions**, through `AssistantRuntime.ask(text)`.

## Exit (what goes out)

- **Tool calls on the gateway**, and nothing else. The tools sent to the model
  are `gateway.tool_schemas()`, read afresh every step, so an instrument that
  connects mid-conversation brings its capability tools with it.
- **Transcript records**, one per message and one per tool call, appended to
  `assistant_transcript.jsonl` in the experiment folder.
- **Qt signals**: `status_changed(status, detail)`, `message_added(record)`,
  `cost_changed(turn, session)`, `turn_finished(text)`, `failed(message)`.
  `message_added` carries the transcript record itself, so a window renders the
  evidence rather than a second rendering of it.
- **Two cost lines**, this turn and this session, in the four fields
  (`model`, `input_tokens`, `output_tokens`, `cost_usd`) that
  `DraftEntry.cost_line()` and the **Agent feed** already use.

## Interface contract

### The system-prompt standard

`ASSISTANT_SYSTEM_PROMPT` is a constant, in the same sense the draft prompt is:
plain text, read from nothing in the environment, changed deliberately and
visibly. Four rules in it are load-bearing:

1. **Say what it is.** A client of one experiment's gateway; it holds no
   instrument and has no other way in. The role, the envelope, attendance and
   the kill switch bound it and are enforced outside it.
2. **Probe before running.** `validate_run` answers "may I run this, and how
   long will it take?"; `probe_run` runs the short version. A full run is
   proposed only after those have answered.
3. **Name a refusal verbatim.** Every answer carries a `code`, a `reason` and a
   `detail` naming the rule. Quote the reason; do not paraphrase it into "I
   couldn't do that".
4. **Never claim an action without an `OK` verdict.** If the code is not `OK`,
   the action did not happen — the single most damaging thing an assistant at
   a cryostat can say otherwise.

### The Chat client contract

One method, `create_message(system, messages, tools, max_tokens) -> ChatResult`.
The loop, the tool routing and the accounting are the runtime's, so an
implementation has one job and a test double is a dozen lines.
`ChatResult` carries `text_blocks`, `tool_calls`, `model`, `input_tokens`,
`output_tokens`, `stop_reason` and — optionally — the raw `content` blocks, so
anything the model needs to see again travels back unchanged. Every failure is
one `AssistantError`.

**A refusal is data, not an error.** A refused tool call comes back as an
ordinary `tool_result` whose content is the gateway's answer dict verbatim,
never flagged as an error: the assistant is supposed to read the reason and
quote it, not treat it as a fault and retry it in a different shape.

### The assistant's thread rule

**Model calls run off the GUI thread; tool calls run on it.**

- `create_message()` is handed to a `QThreadPool` worker and its answer comes
  back through a queued signal. The worker is given a client, a prompt and a
  turn token — no gateway, no transcript, no engine — so the rule is enforced
  by what it holds, not by a convention about what it does.
- Everything else happens in the slot that receives that answer: the loop,
  every `Gateway.call_tool()`, every transcript write, every signal. In the
  application that slot runs on the GUI thread, which is the thread that drives
  the tick.
- **The engine's single-hardware-thread invariant is untouched.** An assistant
  that called into the gateway from a pool thread would be a second writer to
  the instrument bus, which is precisely the GPIB race the tick design exists
  to prevent. `tests/test_assistant.py` asserts both halves: the client's
  thread differs from the caller's, and the gateway's does not.
- The chat window never blocks. `ask()` returns immediately; the answer arrives
  as a signal.

### Cancellation is between steps

`stop()` invalidates the turn token, so the in-flight answer is discarded on
arrival, no tool is called for it and no further model call is made. The
network call itself is not torn down — nothing is gained by it — and the
conversation keeps every message up to the last completed step, so the next
question continues from a coherent state.

### The turn is bounded

`DEFAULT_MAX_STEPS` model calls per question, then the turn ends saying so. A
cap on steps rather than a timeout: how many times the assistant may go round
is the quantity a physicist can reason about, and the one a runaway
conversation actually exhausts.

### The Assistant transcript record standard

One JSON object per line, appended, never rewritten — the journal-of-facts
discipline the **Agent feed** follows. Every record carries every key
(`schema`, `ts`, `seq`, `experiment_id`, `turn`, `record`, `role`, `text`,
`tool`, `args`, `verdict`, `detail`, `cost`); a value that does not apply is
`null`, never a missing key. One record per message and per tool call, in the
order they happened, so a turn reads as a `user` record followed by alternating
`assistant` and `tool` records. Recording never raises into its caller: a full
disk must not swallow the answer the physicist is waiting for.

### Where the key comes from

`session/eln/settings.py`, unchanged and unduplicated: `assistant.api_key` in
the user-level settings file, overridden by `CRYOSOFT_ASSISTANT_APIKEY`,
redacted from `repr()` and `to_dict()`, passed to the vendor SDK and nowhere
else. An empty key means "let the SDK resolve credentials from the
environment". With no key at all, nothing here is constructed — the chat dock
says so in one line instead of failing.

## How to add a new module

1. Keep the dependency direction: this package may import `core.*` and
   `session.*`, never `gui`/`main`/drivers/VIs/procedures (C11 fails the build
   otherwise), and nothing below the GUI may import back (C12).
2. **A new capability is a new gateway tool, never a module here.** If the
   assistant needs to do something it cannot do today, the answer is a
   declaration in `session/gateway/tools.py` (or a `@control` further down that
   renders into one) plus its action-class row — never a call this package
   makes on its own. The moment this folder can do something an agent in
   another process cannot, the permission matrix has stopped being the whole
   truth.
3. **A new client implements the contract and nothing else.** No loop, no
   retry policy of its own, no conversation state; map every vendor failure to
   `AssistantError`, and import the SDK lazily so a checkout without the extra
   still runs the whole suite.
4. **Nothing new may run on the pool thread.** A worker gets a client, a prompt
   and a token. If something else seems to need to go there, it needs to come
   back through the signal instead.
5. New behavior needs its own tests in `tests/test_assistant.py`; conformance
   coverage is necessary but not sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `runtime.py` | The system-prompt standard, the **Chat client** contract, and `AssistantRuntime` — the tool-use loop whose tools are `Gateway.tool_schemas()` and whose execution is `Gateway.call_tool()`. Owns the turn, the step cap, cancellation between steps, the two cost lines, and the assistant's thread rule (model calls on a `QThreadPool` worker, everything else on the receiving thread). | `ASSISTANT_SYSTEM_PROMPT`, `AssistantRuntime` (`ask()`, `stop()`, `set_gateway()`, `turn_cost()`, `session_cost()`, `is_busy()`, `role`), `ChatClient`, `ChatResult`, `ToolCall`, `AssistantError`, `STATUS_*`, `empty_cost_line()` | `tests/test_assistant.py` |
| `clients.py` | The two **Chat client**s: `FakeChatClient` (scripted, records every request and the thread it ran on, models the unreachable model) and `AnthropicChatClient` (one Messages request per step, SDK imported lazily, key from `AssistantSettings`, every vendor failure mapped to `AssistantError`). | `FakeChatClient`, `ChatRequest`, `AnthropicChatClient` | `tests/test_assistant.py` |
| `transcript.py` | The **Assistant transcript**: the append-only JSONL conversation record for one experiment, one line per message and per tool call, every key always present, recording that never raises. | `AssistantTranscript` (`record_user()`, `record_assistant()`, `record_tool()`, `path`), `read_transcript()`, `SCHEMA_VERSION`, `RECORD_*` | `tests/test_assistant.py` |
