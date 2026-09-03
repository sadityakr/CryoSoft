# cryosoft/mcp — the MCP adapter (a separate process)

## Purpose

Let an external agent session — an editor, an assistant, anything that
speaks the Model Context Protocol — drive a running CryoSoft application
without being able to reach an instrument.

The package is a **translator and nothing else**. It speaks MCP over stdio
to the session that launched it, and newline-delimited JSON-RPC over a local
socket to the running app's **Gateway server**, and every MCP method is
answered by making the gateway request that already answers it. It holds no
Station, no Orchestrator and no session layer; it renders no tool, describes
no instrument, and decides no authority.

Three properties are the whole design:

- **It is a separate process, mechanically.** Import contract C21 in
  `pyproject.toml` allows this package exactly one CryoSoft import,
  `cryosoft.core.events` — the pure contract. There is no code path from
  here to a driver, and adding one fails the build rather than a review.
  `tests/test_mcp_adapter.py` checks the same rule by reading the modules'
  own imports, so a module added to `cryosoft/core/` later cannot slip past
  the enumerated list in the contract.
- **The surface is the app's own.** `tools/list` is the **Gateway server**'s
  `tools/list`, re-keyed from the contract's `input_schema` to MCP's
  `inputSchema`; `tools/call` is forwarded verbatim in both directions; each
  of the three resources is a URI over one of the read tools the **Tool
  surface** already offers. Nothing here is written per tool, so a tool
  added to the app appears in an external session with no change to this
  package.
- **Authority lives in the app, not here.** This process declares a **Role**
  and an actor id at the handshake; whether it may have them is the app's
  decision, taken against the deployment's ceiling. Every call it forwards
  is judged by the same permission matrix, attendance and kill switch that
  judge an in-process client, and lands in the same **Agent feed**.

## Architecture layer

Beside the seven layers, not on top of them: a client process. It sits
outside the application entirely and reaches it only through the socket the
**Gateway server** publishes, which is why its import allowlist is one
module wide.

## Entry (what comes in)

- **`python -m cryosoft.mcp`**, launched by the session that wants to drive
  the app. `--descriptor`, `--role`, `--actor-id`, `--timeout`, `--framing`
  and `--log-level`, each defaulting from the matching `CRYOSOFT_MCP_*`
  environment variable so a launcher can configure it without arguments.
- **`gateway.json`**, the descriptor the running app publishes beside its
  socket: the socket name, the owning pid, the wire's schema version and the
  per-launch token. Found through `CRYOSOFT_GATEWAY_DESCRIPTOR`, else
  `CRYOSOFT_LOG_DIR`, else the platform's state directory.
- **MCP requests on stdin**, one JSON-RPC message per line.

## Exit (what goes out)

- **MCP responses and notifications on stdout**, one message per line, and
  nothing else — every diagnostic goes to stderr, which is where a client
  shows it.
- **Gateway requests on the socket**: `hello` once, `events/subscribe` once,
  then one `tools/list` or `tools/call` per MCP request.
- **Exit status**: 0 when the session ended normally, 1 when the app could
  not be found or refused this connection.

## Interface contract

### The methods

| MCP method | Answered with |
|---|---|
| `initialize` | The handshake-era result: the negotiated revision, capabilities, identity, instructions. |
| `server/discover` | The stateless-era result: every revision this adapter speaks, plus the same identity and instructions. |
| `ping` | `{}` |
| `tools/list` | The gateway's `tools/list`, each entry re-keyed to `inputSchema`. |
| `tools/call` | The gateway's `tools/call`, its answer rendered as one JSON text block. |
| `resources/list` | The three resources below. |
| `resources/read` | A `tools/call` on the read tool that URI names. |
| `resources/templates/list` | An empty list — there are no templated resources. |
| `logging/setLevel` | `{}`, accepted and ignored (see below). |
| anything else | JSON-RPC `-32601`. |

| Resource | Read through |
|---|---|
| `cryosoft://status` | `read_status` |
| `cryosoft://station` | `read_station_info` |
| `cryosoft://manifest` | `read_manifest` |

The app's `StateChange`, `StatusSnapshot` and `Verdict` messages travel the
other way as `notifications/message`, the spec-defined channel, with the
whole contract message in `data`. MCP has no channel for an arbitrary
server-pushed domain event, and inventing one would make this adapter a
protocol of its own.

### Two protocol eras, one server

MCP's current revision (`2026-07-28`) is stateless: it retired the
`initialize` / `notifications/initialized` exchange, made capability
discovery an ordinary `server/discover` request, and gave every cacheable
result three required fields. The revisions before it are handshake-based,
and the clients driving this adapter still speak them. Both are served, and
every result carries the current revision's `resultType` / `ttlMs` /
`cacheScope` whichever era asked for it: an older client ignores fields it
does not know, a newer one requires them.

`ttlMs` is 0 on every list — the tool surface grows when an instrument
connects and the status changes every tick, so nothing here is cacheable and
nothing here claims to be. `cacheScope` is `private` because a connection is
one actor with one role and its answers are scoped to that authority.

### A refusal is a result, not an error

The gateway answers a refused call with a dict naming the rule that refused
it, and that dict is rendered into the tool result — `isError: true`, with
the whole answer readable — where the model can act on it. A JSON-RPC error
is reserved for the cases where there is no answer at all: an unserved
method, unreadable parameters, a URI that is not a resource, and a gateway
connection that failed. This is the **Verdict standard**'s rule in another
protocol: an error the caller can act on belongs in the result, not in the
envelope.

### Nothing is hidden from the session

`logging/setLevel` is accepted and ignored. Every notification this adapter
sends is one the application itself emitted, and dropping a state change
because a client asked for less noise would hide the cryostat from the
session driving it.

### Two backends, one translation

`adapter.py` produces every payload; the backends differ in framing alone.

- **`shim.py` is the reference backend.** Stdlib only, so the tests exercise
  it and it always exists. It waits on stdin and the gateway socket
  together, so an event reaches the session the moment the app emits it.
- **`sdk.py` is optional.** When a deployment installs the `mcp` extra, the
  framing comes from the package that owns the protocol. It checks, before a
  byte of stdin is read, that the installed package exposes every name it
  uses, and returns `False` on any mismatch so the shim takes the session
  intact — a package it was not written against costs a log line, never a
  broken session. Its one visible difference, and the reason the shim stays
  the default: the SDK owns the loop, so events are delivered at request
  boundaries rather than the instant they arrive.

**Never make the test suite need the extra.** A transport the tests cannot
run is a transport nobody checks; `test_the_optional_package_is_absent_and_the_shim_serves`
fails if it creeps into the test environment.

## How to add a new module

1. **Check the one import rule first.** If the module needs anything from
   CryoSoft but `cryosoft.core.events`, it does not belong in this package —
   the thing it wants belongs in the **Tool surface**, where every client
   gets it, and reaches here as a tool.
2. **Put payloads in `translate.py` and routing in `adapter.py`.** A payload
   function is pure — no socket, no process, no state — so both backends
   produce identical bytes and the tests can check the shapes without
   either.
3. **Never write a tool.** If an external session needs an action the app
   can take, add it to the app: a `CommandName`, a `@control`, or a session
   tool in `session/gateway/tools.py`. It reaches MCP by itself.
4. **Add the test beside the others.** A pure translation gets a shape test;
   a new method gets a dispatch test against the JSON-RPC fake; anything
   touching the framing gets a subprocess test, because the claim being made
   is about a real process on real pipes.
5. **Update this README and the `Files` table in the same commit.**

## Wiring an external session

`.mcp.json` at the repository root already declares this server for a Claude
Code session; `.claude/skills/measure-session/SKILL.md` describes how such a
session drives a measurement. The application must be running with
`gateway_server: true` in its `monitor.yaml`, and it will hand out at most
the role that file's `gateway_max_role` names.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `__main__.py` | The process: parse the command line (every option defaulting from a `CRYOSOFT_MCP_*` variable), read the descriptor, open one gateway connection under the declared role and identity, choose a framing backend, and serve until stdin closes. Exits 1, with the reason on stderr, when the app cannot be found or refuses the connection — there is no offline mode, because an adapter without an app behind it would publish a surface it cannot serve. | `build_parser()`, `main(argv)`, `FRAMINGS` | `tests/test_mcp_adapter.py` |
| `adapter.py` | The translation: one MCP request in, one gateway request out. Holds the single dispatch table both backends use, forwards `tools/list` and `tools/call` unchanged, answers a resource read through the tool that serves it, and turns a failed connection into a JSON-RPC error rather than a crash. Drains and translates the gateway's notifications. | `McpAdapter` (`open()`, `close()`, `handle(request)`, `drain_notifications()`), `METHOD_NOT_FOUND`, `INVALID_PARAMS`, `INTERNAL_ERROR`, `RESOURCE_NOT_FOUND` | `tests/test_mcp_adapter.py` |
| `client.py` | Finding the running app and speaking its wire: the descriptor's location and contents (schema checked, never guessed at), and a newline-delimited JSON-RPC client that says `hello` at connect, answers one request at a time, and queues the server's notifications for the caller to collect. Stdlib only, and deliberately re-derives the descriptor's location rather than importing the module that owns that rule for the app. | `GatewayClient` (`connect()`, `close()`, `fileno()`, `call()`, `receive_available()`, `take_notifications()`), `GatewayError`, `read_descriptor()`, `default_descriptor_path()`, `DESCRIPTOR_FILENAME`, `DEFAULT_TIMEOUT_S`, `SUPPORTED_SCHEMA` | `tests/test_mcp_adapter.py` |
| `sdk.py` | The optional framing: the same session served through the `mcp` package when a deployment installed the extra. Checks every name it uses before reading stdin and declines rather than serving partially. | `serve_with_sdk(adapter)`, `sdk_unavailable_reason()` | `tests/test_mcp_adapter.py` |
| `shim.py` | The reference framing: newline-delimited JSON-RPC over stdio with the stdlib, waiting on stdin and the gateway socket together so an event is written out the moment it arrives. Answers a malformed frame with a parse error and keeps serving; stdout carries protocol and nothing else. | `serve(adapter, stdin=None, stdout=None)`, `MAX_FRAME_BYTES` | `tests/test_mcp_adapter.py` |
| `translate.py` | Every MCP payload, as pure functions: the two handshakes, the re-keyed tool list, the tool result, the three resources and their contents, and the gateway notification rendered as `notifications/message`. Describes no tool, no instrument and no command in words of its own. | `PROTOCOL_VERSIONS`, `LATEST_PROTOCOL_VERSION`, `HANDSHAKE_PROTOCOL_VERSION`, `RESOURCES`, `RESOURCE_TOOLS`, `STATUS_URI`, `STATION_URI`, `MANIFEST_URI`, `initialize_result()`, `discover_result()`, `mcp_tool()`, `tools_list_result()`, `resources_list_result()`, `tool_result()`, `resource_contents()`, `log_notification()`, `log_notifications()` | `tests/test_mcp_adapter.py` |
