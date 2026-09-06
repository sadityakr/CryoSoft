# cryosoft/session/eln — ELN publishing (L6)

## Purpose

Publish what CryoSoft measured into an **electronic lab notebook**, without
ever letting the notebook's availability affect a measurement. Six
separable pieces: a backend-neutral **ELN adapter** standard (`adapter.py`)
with an eLabFTW backend and an in-memory sim twin, deterministic **body
renderers** that turn a run manifest into entry text (`templates.py`), an
offline-first **outbox** that queues what to publish and retries it forever
(`outbox.py`), a **publisher** deciding what is queued when and draining the
queue off the tick (`publisher.py`), user-level settings holding the
backend URL, the API keys, the assistant's price table and the analysis
stage's switches (`settings.py`), and LLM **drafting** that turns one
finished run's facts into a **draft entry** a human approves (`drafting.py`).

With the analysis stage switched on the publisher does not queue a finished
run at all: it asks for an analysis (`analysis_requested`), and what comes
back is parked as a **Pending entry** on the run record — an **analysed
entry** when the recipe ran, the facts-only entry when it did not. Approval
is then the only door to the notebook.

## Architecture layer

**L6, inside the Session Manager.** Imports `cryosoft.session.*` and
`cryosoft.core.*` downward and nothing else — the same C11/C12 import
contracts that bind the rest of `cryosoft/session/` apply here unchanged. It
holds the only network I/O in the whole application, and that I/O is never
reached from the Orchestrator tick: adapters are called exclusively from a
GUI-side drain, never from `core/`.

## Entry (what comes in)

- **Settings**: the user-level JSON file resolved by `eln_settings_path()`
  (`$CRYOSOFT_ELN_SETTINGS`, else `eln-settings.json` under
  `core.paths.user_config_dir()` — `%APPDATA%\CryoSoft` on Windows,
  `~/.config/cryosoft` elsewhere), plus the
  `CRYOSOFT_ELAB_APIKEY` environment variable which overrides its `api_key`.
  Never a shipped config, never git-tracked.
- **Run manifests** (the Orchestrator's `run_finished` payload: `run_id`,
  `procedure`, `kind`, `params`, `data_file`, timestamps, terminal `status`,
  `reason`) plus the session layer's `experiment_context()`, rendered by
  `templates.py` and queued by `outbox.py`.
- **The experiment's own `outbox.jsonl`** (`ExperimentStore.outbox_path()`) —
  read back on every drain, including after a restart or on a machine that
  the experiment folder was copied to.
- **A drain tick**: `ElnPublisher.drain_once()`, called by the publisher's own
  QTimer, started from `cryosoft.main` once the application has an event loop.
- **An analysis report** (`cryosoft.analysis.report.AnalysisReport`, or its
  `report.json` dict): what one **Analysis recipe** made of one finished run
  — prose, derived values, figures, tables, warnings, and the two flags
  saying whether the entry should also carry the run's fact tables and its
  raw data file. Handed in by `ElnPublisher.export_report()` together with
  the directory its figures were written to. This package reads a report; it
  never runs one.
- **A draft request** (`DraftRequest`): one run's manifest-shaped facts, its
  per-column `summary_stats`, the **Station info** snapshot and the latest
  `StatusSnapshot` at run end — assembled by the gateway's `draft_eln_entry`
  tool and rendered into the draft prompt. Never a file path and never a
  credential.

## Exit (what goes out)

- **One model request per draft**, through the injectable **Draft client**
  (`DraftClient.complete()`), and only when drafting is switched on. Every
  test runs against `FakeDraftClient`, so no test ever reaches a network.
- **HTTP requests to one ELN backend**, and nothing else. Every request is
  issued inside an `ElnAdapter` method, and for `elabftw.py` through a
  single injectable `ElnHttpTransport.request()` call — the same split the
  driver layer uses: the adapter is the instrument, the transport is the
  bus, so the whole backend is testable against canned responses.
- **`ElnEntryRef`** — backend, entry id, URL, template id — the value a
  successful publish returns, sharing its field names with
  `cryosoft.session.models.ElnLink` so a persisted link is
  `ElnLink.from_dict(ref.to_dict())` away, with no translation layer.
- **Appended `outbox.jsonl` lines** inside the experiment folder: one per
  enqueue and one per state change, so the folder stays the complete,
  portable record — copy it and its unpublished runs travel with it.
- **`ExperimentManager.set_run_eln_link()`** — the confirmed entry reference,
  handed to the session layer's single writer. This package never edits a
  record or writes an experiment file itself.
- **A `DraftEntry`** — title, escaped self-contained `body_html`, tags, and
  the cost line (`model`, `input_tokens`, `output_tokens`, `cost_usd`,
  `prompt_digest`). Data, returned to whoever asked: shown to a human for
  approval, parked on the run record as `pending_eln_draft`, or handed to
  `ElnPublisher.export_draft()`. Drafting itself publishes nothing.
- **A parked Pending entry**: `export_report()` and `park_facts_entry()` both
  end at `ExperimentManager.set_pending_eln_draft()` — the single writer of
  experiment state — and publish nothing. A parked entry carries its
  `attachments` (an analysed entry's figures, by absolute path and caption),
  its `attach_data_file` flag and its `source` (`model` / `analysis` /
  `facts`), all of which travel into the outbox job when a human approves it.
- **The written settings file**: `save_eln_settings()` is the one place a key
  reaches a disk file — written atomically, `0o600` on POSIX, and never
  logged.
- **Signals for the GUI**: `publish_state_changed(dict)`
  (`synced` / `pending` / `offline` / `disabled`, plus a pending count),
  `run_published(dict)` (run id, experiment id, the `ElnLink`), and
  `analysis_requested(run_id, manifest, data_path)` — emitted INSTEAD of
  queuing when the analysis stage is on, for whoever owns the analysis
  runner.

## Interface contract

- **The ELN adapter standard** is written in `adapter.py`'s module docstring
  and machine-checked by the ELN-adapter conformance tests: one concrete
  `ElnAdapter` per backend module; `__init__(self, settings, ...)` takes a
  single plain settings mapping (the analogue of the driver contract's
  one-resource-string rule) and nothing else required; a lowercase `backend`
  id and a declared `ElnCapabilities`; and **exactly** the contract's public
  methods — `verify`, `list_templates`, `create_entry`, `update_entry`,
  `attach_file`, `attach_link` — so any adapter substitutes for any other.
- **Adapters are stateless and synchronous** and raise exactly one exception
  type, `ElnError`. Queuing, retry, and backoff belong to the outbox.
- **An approved draft is not a second write path.** `export_draft()` queues
  one ordinary outbox job whose title, body and tags come from an approved
  **draft entry** instead of from the renderers, merging the notebook's
  standing tags with the draft's own and stamping `draft_model` and
  `draft_prompt_digest` into the entry's metadata, so the notebook itself
  says which model wrote the prose and from which prompt. Same journal, same
  `job_id`, same idempotency, same drain — the draft is data, and only the
  text differs.
- **The analysis stage takes precedence over auto-publish.** When
  `settings.enabled and settings.analysis.enabled`, an experiment is open and
  the run has a data file, `on_run_finished()` emits `analysis_requested` and
  queues NOTHING, whatever `auto_publish` says — because on that path nothing
  publishes until a human approves the entry. With the analysis stage off,
  the run takes exactly today's path. Whatever the analysis produced is
  parked, attended or not: an unattended experiment's analysed entry waits
  for the human who reads it later rather than publishing an unreviewed
  result.
- **An analysed entry names its figures; it never embeds one.**
  `render_analysed_body()` writes the figure's file name into a captioned
  list and the file travels as an **Outbox** attachment, so the body stays
  self-contained under the same rule every other body obeys. Attachments are
  uploaded after the data file, in order, under the same size caps, with the
  same link fallback when a file is missing or too large — and the data file
  itself is attached only when the job says so.
- **Nothing publishes directly.** Work is rendered in full at enqueue time
  and queued; the drain never re-renders against state that has since moved
  on. `Outbox.drain()` attempts at most one job per call and never raises
  into its caller — the caller is a GUI timer, and a notebook outage must not
  take the event loop down with it. Jobs are idempotent by `job_id`, retried
  forever under a persisted, capped exponential backoff, and never deleted.
- **One sim twin for all backends** (`sim_eln.SimElnAdapter`). Because the
  contract fixes the public API *exactly*, every adapter's surface is
  identical, so one in-memory twin stands in for all of them — a backend's
  own HTTP dialect is faked one level lower, at its injectable transport.
  This is the `sim_` driver rule applied to notebooks, including the rule
  that a twin models failure modes (offline, transient failure, refused
  upload), not just the happy path.
- **Publishing is opt-in and never silent.** No settings file (the default)
  means nothing is configured, the drain timer never starts, and nothing
  leaves the machine. `auto_publish: false` leaves the manual export as the
  only trigger. And a run belonging to no experiment is never published —
  an ad-hoc run has no record for an entry to attach to.
- **Settings are swapped, never rebuilt around.** `reload_settings()` takes
  the record the **eLab setup** dialog just saved, resolves the backend
  adapter afresh (the URL, the key or the backend itself may have changed)
  and re-arms the drain timer according to `is_configured` — so switching the
  track off stops the network the moment Save is pressed.
- **The API keys are never logged.** `ElnSettings` and `AssistantSettings`
  redact theirs in `repr()` and in `to_dict()`; only
  `to_dict(include_secret=True)` (writing the file back, building an auth
  header, constructing the draft client) yields the real value. The
  assistant's key comes from the same user-level file under
  `assistant.api_key`, or from `CRYOSOFT_ASSISTANT_APIKEY`; an empty key
  deliberately means "let the vendor SDK resolve credentials from the
  environment". No key ever reaches a prompt or an entry body.
- **The draft prompt is a standard**, written in `drafting.py`'s module
  docstring: two plain-text halves, deterministic (sorted keys, `repr` for
  floats, no clock and no environment), facts under six fixed headings, and a
  two-marker answer shape that parses tolerantly. `prompt_digest` is the
  SHA-256 of both halves, so two drafts of one run are provably the same
  question and a changed prompt is visible in the entry it produced.
- **A draft never replaces the facts.** `render_draft_body()` puts the model's
  prose ABOVE the same escaped fact tables a published run gets, so a reviewer
  checks every drafted sentence against the numbers beneath it and a useless
  completion still yields a complete, correct entry.
- **The model is one injectable collaborator.** `DraftClient` is one method,
  `complete(system, user, max_tokens)`, raising the same `ElnError` an adapter
  does. `AnthropicDraftClient`'s SDK is the OPTIONAL `assistant` extra,
  imported lazily: a checkout without it imports this package, renders
  prompts, and runs every test unchanged, and gets one clear `ElnError` naming
  the install command at the moment a real client is constructed.
- **Cost is reported, never estimated.** `cost_usd` multiplies the vendor's
  reported token counts by the per-model price table in `settings.py`
  (`DEFAULT_MODEL_PRICES`, overridable per installation from the settings
  file). A model with no price row reports `0.0` and a WARNING — never a
  guessed price.
- **Backends are discovered, not listed.** `publisher.discover_backends()`
  walks this package for `ElnAdapter` subclasses and keys them by their
  declared `backend`, so a new backend is selectable from the settings file
  the moment its file exists — the same auto-discovery idiom as drivers, VIs,
  and procedures.
- **Rendered bodies are self-contained.** No `<script>`, `<link>`, `<img>`,
  or external URL of any kind, every value HTML-escaped and length-capped,
  and identical output for identical input — conformance-checked, so an entry
  renders the same in the notebook, in an export, and in a test snapshot.

## Decisions this package makes

The owning design left four questions open. Each is answered here with the
simplest defensible option; changing one is a design change, not a routine
edit.

- **One ELN entry per run**, not per experiment. The **run manifest** is the
  unit the Orchestrator hands over, a run's HDF5 file is exactly one
  attachment, and idempotency falls straight out of the run id
  (`job_id = "publish_run:<run_id>"`). So `RunRecord.eln_link` is what the
  publisher writes. `ExperimentRecord.eln_link` stays in the record model,
  unwritten by this package, for a future coarser parent entry — the record
  model supports either granularity, and nothing here forecloses it.
- **A QTimer drain, one job per firing** — no thread, no async client. This
  is the tick loop's cooperative single-threaded philosophy applied to the
  network: a slow upload delays the next upload, never the event loop's next
  turn, and never a hardware write. The measured alternatives, in preference
  order if multi-MB uploads over slow lab links prove to stutter the GUI:
  chunked uploads per firing, then an async single-threaded HTTP client, then
  one dedicated upload thread that touches only HTTP and files. Start here
  and measure.
- **No terminal failure state.** A job is retried forever under a capped
  backoff rather than being marked dead after *n* attempts, because the case
  this exists for is an offline week, and a dead job is a silently lost run.
  A wrong API key therefore shows up as a permanently `offline` status chip
  with the backend's own message in `detail` — deliberately not a separate
  "auth error" state, which would be a second thing to get wrong for no
  behavioural difference.
- **The publisher never asks the notebook who anybody is.** Identity comes
  from the local **user** roster; the ELN account is a property of the
  installation, in the user-level settings file. This works offline and adds
  no auth complexity.


## How to add a new module

1. **A new backend** is one leaf module here holding one `ElnAdapter`
   subclass, plus an injectable transport so its HTTP dialect is testable
   against canned responses. Implement the contract's six methods and
   nothing more; declare `backend` and `capabilities`. Conformance covers it
   the moment the file exists, and no core code changes.
2. **A new rendered section** is a function in `templates.py` — plain Python,
   no template-language dependency, deterministic, escaped. Prose that did not
   come from the machine goes through `render_prose_section()`.
3. **A new job kind** is a `kind` constant plus a branch in
   `Outbox._publish()`; the journal, the dedup rule, and the backoff are
   already generic.
4. **A new draft client** is a class with one `complete()` method in
   `drafting.py`, whose vendor SDK is an optional extra imported lazily and
   whose every failure becomes an `ElnError`. Never add a required dependency
   for it, and never let a key reach a log line, a prompt, or an entry.
5. **A new pending-entry source** is a renderer in `templates.py` plus one
   `ElnPublisher` method that builds a `DraftEntry` with its own `source` and
   parks it through `manager.set_pending_eln_draft()`. Never a second write
   path: approval and `export_draft()` stay the only way into the outbox.
6. **Never** put threading, or a network call outside an adapter method or a
   draft client, into this package, and never call either from `core/`.
7. New behaviour needs its own tests in `tests/test_eln.py`; conformance
   coverage is necessary but not sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `adapter.py` | The ELN adapter standard: the abstract contract, its value types, and its one exception. | `ElnAdapter`, `ElnEntryRef`, `ElnTemplate`, `ElnCapabilities`, `ElnError` | `tests/test_conformance.py` |
| `sim_eln.py` | The in-memory twin of the contract — the workhorse of every ELN test; models offline, transient failure, and refused uploads. | `SimElnAdapter` (`entries`, `uploads`, `links`, `calls`, `offline`) | `tests/test_eln.py` |
| `settings.py` | User-level backend URL/key/policy, the assistant's model, key, token cap and price table, and the analysis stage's switches: tolerant load, environment overrides, redaction, and the atomic 0o600 write-back. | `ElnSettings`, `AssistantSettings`, `AnalysisSettings`, `load_eln_settings`, `save_eln_settings`, `eln_settings_path`, `API_KEY_ENV_VAR`, `ASSISTANT_API_KEY_ENV_VAR`, `SETTINGS_PATH_ENV_VAR`, `DEFAULT_ASSISTANT_MODEL`, `DEFAULT_MODEL_PRICES` | `tests/test_eln.py` |
| `outbox.py` | The offline-first publish journal: append-only JSONL, idempotent by `job_id`, persisted capped backoff, one job per drain, never raises. Attaches the data file (when the job asks) and then every attachment, under the same caps and link fallback. | `Outbox` (`enqueue`, `jobs`, `get`, `pending`, `drain`), `OutboxJob`, `DrainResult`, `JOB_*`/`DRAIN_*` constants | `tests/test_eln.py` |
| `elabftw.py` | The eLabFTW backend: REST API v2 over `/users/me`, `/experiments_templates`, `/experiments`, `/experiments/{id}`, `/experiments/{id}/uploads`; token auth, verified TLS, hand-rolled multipart, every non-2xx mapped to `ElnError` without the key. | `ElabFtwAdapter`, `ElnHttpTransport`, `UrllibTransport`, `HttpResponse` | `tests/test_eln.py` |
| `publisher.py` | What is queued when (a finished run, a manual export, or an approved **Pending entry**), what is sent to the analysis stage instead, what is PARKED for approval (an **analysed entry** or the facts fallback), the GUI-side drain timer, backend discovery, and the hand-off of a confirmed link to the manager. | `ElnPublisher` (`on_run_finished`, `export_run`, `export_draft`, `export_report`, `park_facts_entry`, `reload_settings`, `drain_once`, `start`, `stop`, `pending_count`, `status`; signals `publish_state_changed`, `run_published`, `analysis_requested`), `discover_backends`, `PUBLISH_*` constants | `tests/test_eln.py` |
| `templates.py` | Run manifest (and, for an analysed entry, an **Analysis report**) → entry title, self-contained HTML body (published, drafted or analysed), and flat metadata; shared row builders so a run reads identically in every body. | `render_run_title`, `render_run_body`, `render_draft_body`, `render_analysed_title`, `render_analysed_body`, `render_prose_section`, `render_stats_section`, `render_run_metadata` | `tests/test_eln.py` |
| `drafting.py` | The draft prompt standard and the **Draft client** contract: render one run's facts into a deterministic prompt, ask one model, parse tolerantly, and return a **draft entry** carrying its prompt digest and cost line. Also owns `DraftEntry`, the shape of EVERY **Pending entry** (its `source`, `attachments`, `attach_data_file` and `metadata`). Publishes nothing. | `DraftRequest`, `DraftEntry`, `SOURCE_MODEL`, `SOURCE_ANALYSIS`, `SOURCE_FACTS`, `DraftClient`, `CompletionResult`, `draft_entry`, `render_draft_prompt`, `prompt_digest`, `parse_completion`, `manifest_from_run`, `cost_usd`, `cost_line`, `COST_FIELDS`, `FakeDraftClient`, `AnthropicDraftClient`, `DRAFT_SYSTEM_PROMPT` | `tests/test_eln.py` |
