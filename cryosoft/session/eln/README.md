# cryosoft/session/eln — ELN publishing (L6)

## Purpose

Publish what CryoSoft measured into an **electronic lab notebook**, without
ever letting the notebook's availability affect a measurement. Three
separable pieces: a backend-neutral **ELN adapter** standard (`adapter.py`)
with an eLabFTW backend and an in-memory sim twin, deterministic **body
renderers** that turn a run manifest into entry text (`templates.py`), an
offline-first **outbox** that queues what to publish and retries it forever
(`outbox.py`), and user-level settings holding the backend URL and API key
(`settings.py`).

## Architecture layer

**L6, inside the Session Manager.** Imports `cryosoft.session.*` and
`cryosoft.core.*` downward and nothing else — the same C11/C12 import
contracts that bind the rest of `cryosoft/session/` apply here unchanged. It
holds the only network I/O in the whole application, and that I/O is never
reached from the Orchestrator tick: adapters are called exclusively from a
GUI-side drain, never from `core/`.

## Entry (what comes in)

- **Settings**: the user-level JSON file resolved by `eln_settings_path()`
  (`$CRYOSOFT_ELN_SETTINGS`, else `%APPDATA%\CryoSoft\eln-settings.json` on
  Windows, else `~/.config/cryosoft/eln-settings.json`), plus the
  `CRYOSOFT_ELAB_APIKEY` environment variable which overrides its `api_key`.
  Never a shipped config, never git-tracked.
- **Run manifests** (the Orchestrator's `run_finished` payload: `run_id`,
  `procedure`, `kind`, `params`, `data_file`, timestamps, terminal `status`,
  `reason`) plus the session layer's `experiment_context()`, rendered by
  `templates.py` and queued by `outbox.py`.
- **The experiment's own `outbox.jsonl`** (`ExperimentStore.outbox_path()`) —
  read back on every drain, including after a restart or on a machine that
  the experiment folder was copied to.

## Exit (what goes out)

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
- **The API key is never logged.** `ElnSettings` redacts it in `repr()` and
  in `to_dict()`; only `to_dict(include_secret=True)` (writing the file back,
  building an auth header) yields the real value.
- **Rendered bodies are self-contained.** No `<script>`, `<link>`, `<img>`,
  or external URL of any kind, every value HTML-escaped and length-capped,
  and identical output for identical input — conformance-checked, so an entry
  renders the same in the notebook, in an export, and in a test snapshot.

## How to add a new module

1. **A new backend** is one leaf module here holding one `ElnAdapter`
   subclass, plus an injectable transport so its HTTP dialect is testable
   against canned responses. Implement the contract's six methods and
   nothing more; declare `backend` and `capabilities`. Conformance covers it
   the moment the file exists, and no core code changes.
2. **A new rendered section** is a function in `templates.py` — plain Python,
   no template-language dependency, deterministic, escaped.
3. **A new job kind** is a `kind` constant plus a branch in
   `Outbox._publish()`; the journal, the dedup rule, and the backoff are
   already generic.
4. **Never** put threading, or a network call outside an adapter method, into
   this package, and never call an adapter from `core/`.
5. New behaviour needs its own tests in `tests/test_eln.py`; conformance
   coverage is necessary but not sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `adapter.py` | The ELN adapter standard: the abstract contract, its value types, and its one exception. | `ElnAdapter`, `ElnEntryRef`, `ElnTemplate`, `ElnCapabilities`, `ElnError` | `tests/test_conformance.py` |
| `sim_eln.py` | The in-memory twin of the contract — the workhorse of every ELN test; models offline, transient failure, and refused uploads. | `SimElnAdapter` (`entries`, `uploads`, `links`, `calls`, `offline`) | `tests/test_eln.py` |
| `settings.py` | User-level backend URL/key/policy: tolerant load, environment override, redaction. | `ElnSettings`, `load_eln_settings`, `eln_settings_path`, `API_KEY_ENV_VAR`, `SETTINGS_PATH_ENV_VAR` | `tests/test_eln.py` |
| `outbox.py` | The offline-first publish journal: append-only JSONL, idempotent by `job_id`, persisted capped backoff, one job per drain, never raises. | `Outbox` (`enqueue`, `jobs`, `get`, `pending`, `drain`), `OutboxJob`, `DrainResult`, `JOB_*`/`DRAIN_*` constants | `tests/test_eln.py` |
| `elabftw.py` | The eLabFTW backend: REST API v2 over `/users/me`, `/experiments_templates`, `/experiments`, `/experiments/{id}`, `/experiments/{id}/uploads`; token auth, verified TLS, hand-rolled multipart, every non-2xx mapped to `ElnError` without the key. | `ElabFtwAdapter`, `ElnHttpTransport`, `UrllibTransport`, `HttpResponse` | `tests/test_eln.py` |
| `templates.py` | Run manifest → entry title, self-contained HTML body, and flat metadata. | `render_run_title`, `render_run_body`, `render_run_metadata` | `tests/test_eln.py` |
