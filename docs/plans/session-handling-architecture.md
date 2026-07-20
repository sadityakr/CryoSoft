# Plan: long-term session handling — the Session Bundle standard

**Status:** design — the umbrella architecture for session handling.
Positions `unified-session-record.md` as the first implementation slice
and amends it (§7) with hardening items that are cheap now and expensive
later. The record model and code identifiers from
`session-management-layer.md` are unchanged; the roadmap slots into
`agent-native-architecture.md` §8 (this is the substrate F1/A1/B1 read).
**Scope:** how session state — experiment/run records, GUI state, and
measurement data — is stored, versioned, protected, relocated, and
extended over years and across very different cryostat setups. This is a
*persistence and contract* plan: no Orchestrator/procedure behavior
changes, no new hardware paths.
**Date:** 2026-07-20

---

## 1. Where we are today (survey of the implemented code, 2026-07-20)

The L6 layer from `session-management-layer.md` Phase 0/1 is built and
live: `session/models.py` (tolerant-parse records), `session/store.py`
(atomic writes, `active.json` resume pointer), `session/manager.py`
(single writer of experiment state, run recording from manifests,
envelope install, crash-resume marking stale runs failed). The GUI
form-autosave tier (`gui/form_autosave.py` + per-user files under
`%APPDATA%/CryoSoft/sessions/`) and the servicing-log framework are also
live. `unified-session-record.md` (2026-07-20) is the agreed next slice:
promote the experiment folder to the user-facing "session", nest GUI
state and HDF5 data inside it, promote the run queue into the record.

Against the *long-term* requirement — records that stay loadable,
relocatable, and trustworthy for years, on setups we have not met yet —
the survey found six concrete gaps. None is urgent in isolation; all are
cheap to fix while the on-disk layout is being reshaped anyway, and
expensive after thousands of records exist in the wild:

1. **`experiment.json` carries no schema version.**
   `form_autosave.py` stamps `"version": 1` (and never reads it);
   `ExperimentRecord.to_dict()` stamps nothing. Tolerant parse hides
   *missing* keys, but it cannot express "this file is from a newer
   CryoSoft" or drive a deliberate migration. Every long-lived format we
   ship needs an explicit version from day one.
2. **`RunRecord.data_file` is an absolute path** (from
   `DataManager.filepath` via the run manifest). Copy a session folder
   to another machine — the exact backup/archival story the unified plan
   promises — and every data link inside it dangles. Same for
   `active.json`-adjacent tooling and the future Agent Gateway's
   `read_run_data()`.
3. **The store root is derived from a form field.** `main.py` roots the
   `ExperimentStore` at `Path(autosave.data_dir) / "experiments"` — the
   *last value of a GUI text box* decides where the canonical scientific
   record lives, and editing that box mid-life silently forks the store.
   The unified plan already fixes this (`app_settings.sessions_root()`);
   this plan makes the rule explicit: **record location is Setup-tier
   state, never form content.**
4. **Persistent save failures are invisible.**
   `SessionManager._save_current()` logs and carries on — correct for
   one transient failure (a save must never kill a measurement), wrong
   as a steady state: on a full disk or dropped network share the
   operator measures for hours believing runs are being recorded while
   nothing reaches disk.
5. **No instance/writer locking.** Two app instances pointed at one
   sessions root (double launch; two machines sharing a network drive)
   would interleave atomic writes and clobber each other's records —
   last writer wins, silently. The single-writer principle is enforced
   in-process only.
6. **Record saves run inside Orchestrator signal handlers.**
   `_on_run_started`/`_on_run_finished` write the whole
   `experiment.json` synchronously in the tick's signal cascade. Each
   run appends a full station `settings_snapshot`, so the file — and the
   per-manifest write — grows without bound over a long session.
   Harmless at tens of runs; a design liability at thousands.

## 2. Design goals (what "long-term and robust" must mean, testably)

- **G1 — Durable:** a session written today loads correctly, or degrades
  loudly and safely, in any future CryoSoft version. Never the reverse
  assumption (new files need not load in old versions, but old versions
  must *detect* newer files rather than mis-parse them).
- **G2 — Self-contained and relocatable:** one session = one directory.
  Copy/move/archive the directory and *everything* — record, GUI state,
  queue, data files, future ELN outbox and agent feed — still resolves.
  A human with a file manager, or an agent with `Read`, gets the
  complete picture from the folder alone, no app required.
- **G3 — Setup-agnostic:** the session schema contains nothing
  instrument-specific. Any setup expressible in `devices.yaml` produces
  valid sessions with zero session-layer changes; a session from a
  dilution fridge and one from a 12 T VTI cryostat differ only in data,
  never in shape. (This is the repository's standards-over-one-off-code
  principle applied to persistence.)
- **G4 — Crash-safe at the context level:** the on-disk record is never
  more than one mutation behind reality, no crash leaves a torn file,
  and restart resumes the open session exactly as
  `_resume_active_experiment` does today (in-flight runs marked failed,
  never resumed mid-step — confirmed scope).
- **G5 — Honest:** the system never silently loses or silently invents
  record content. Persistent write failure is surfaced to the operator;
  unreadable records degrade to explicit warnings, not defaults that
  masquerade as truth (the existing "unknown status ⇒ failed/closed"
  discipline, generalized).
- **G6 — Extensible by addition:** every planned track (ELN outbox,
  agent action feed, probe runs, history browsing) lands as *new files
  or new keys inside the bundle*, never as a new parallel store. New
  session artifacts have exactly one legal home.

## 3. The core concept: the Session Bundle

One directory per session, the **Session Bundle** — the unified plan's
layout, adopted as a *named, documented, conformance-checked contract*
rather than an implementation detail:

```
<sessions_root>/
    active.json                      # resume pointer (exists today)
    .lock                            # writer lock (§8) — root-level, one writer per store
    <experiment_id>/                 # ══ one Session Bundle ══
        experiment.json              # the record: identity, runs, queue, findings, envelope
        gui_state.json               # the session's GUI state (unified plan §7)
        data/                        # this session's HDF5 files
        runs/                        # reserved: per-run sidecars if records outgrow one file (§7 D3)
        outbox.jsonl                 # reserved: ELN publisher journal (session-management plan §5.3)
        agent_actions.jsonl          # reserved: agent action feed (agent-native plan §3.4)
```

Rules of the bundle (these become the **Session-record standard**, §5):

- `experiment.json` is the bundle's **manifest**: everything else in the
  bundle is reachable from it by *bundle-relative* reference.
- Reserved names are declared now so future tracks don't improvise:
  a new artifact kind must claim a name here (plan + README update)
  before any code writes it.
- A bundle is **live** while `status == "open"` and this store's writer
  holds the lock; it is **sealed** once closed. Sealed bundles are never
  written again by the app (history browsing is read-only; a re-opened
  question is a *new* session referencing the old one in findings).
  Sealing is what makes "archive = copy the folder" trustworthy.
- What stays *outside* the bundle, deliberately: the user roster and
  servicing logs (Setup-tier — they describe the rig and its people
  across all sessions), machine chrome (QSettings), and the per-user
  autosave fallback (User-tier, §6).

## 4. Persistence tiers, formalized

Today's implicit split becomes a documented decision table — every new
persisted item must name its tier (GLOSSARY gets this table):

| Tier | Lives | Examples | Lifecycle |
|---|---|---|---|
| **Machine** | QSettings (registry) | window geometry, splitter state, active config identity, logged-in user id | per install, survives everything, never portable |
| **Setup** | app-data dir + `<sessions_root>` siblings | `users.json` roster, servicing logs, helium record, `sessions_root` setting itself | per rig; changes rarely; independent of any session |
| **User** | `%APPDATA%/CryoSoft/sessions/<user_id>.json` | form autosave when **no session is open** (fallback) | per person per install; convenience cache, losable |
| **Session** | the Session Bundle | record, runs, queue, session GUI state, data, (future) outbox + agent feed | per named session; the scientific record; durable |

Precedence rule for GUI state (makes the unified plan's §7 behavior a
stated invariant): **an open session's `gui_state.json` always wins over
the per-user file; the per-user file is written only when no session is
open.** Switching sessions saves outgoing state to the outgoing bundle,
then loads incoming state from the incoming bundle — the per-user file
is not touched by a switch. This keeps the User tier a pure fallback: it
can be deleted at any time without touching any scientific record.

## 5. The Session-record standard (written + machine-checked)

A new section in `cryosoft/session/README.md` (the standard's home) plus
conformance tests that auto-cover every current and future bundle
artifact. The clauses:

- **S1 — Versioned:** every JSON artifact carries `schema_version: int`
  at the top level. Loaders: same version ⇒ parse; *older* ⇒ migrate
  (S8); *newer* ⇒ refuse to write, open read-only, tell the operator
  ("this session was written by a newer CryoSoft") — never tolerant-parse
  a future format into silent data loss.
- **S2 — Tolerant within a version:** the existing contract, unchanged —
  missing keys default, unknown keys are ignored, `from_dict` never
  raises. (S1 and S2 compose: version gates *interpretation*, tolerance
  handles *variation*.)
- **S3 — Atomic:** every write is `.tmp` + `os.replace` (already
  universal); append-only streams (`.jsonl`) append whole lines only.
- **S4 — Bundle-relative:** no absolute path is ever persisted inside a
  bundle. `RunRecord.data_file` becomes relative to the bundle root
  (`data/<name>.h5`); loaders resolve against the bundle directory, and
  — for compatibility — a stored absolute path is accepted read-only and
  re-resolved by basename against `data/` when it dangles.
- **S5 — Single writer, locked:** one process writes a sessions root at
  a time, enforced by the store lock (§8). All record mutations still
  flow through `SessionManager` in that process (unchanged).
- **S6 — Setup-agnostic schema:** session models contain no
  instrument-type-specific fields; anything per-instrument is a dict
  keyed by VI name (`settings_snapshot`, `envelope`) or free-form
  metadata. Conformance already half-checks this via C11 (no VI
  imports); add an explicit test that model field names never reference
  VI types/roles.
- **S7 — Opaque foreign content:** content authored by another layer
  (the GUI's queue items, `gui_state.json`) is stored verbatim and never
  interpreted by the session layer — the unified plan's rule, promoted
  to a clause.
- **S8 — Migration by copy, never in place:** on loading an older
  version, migrate *in memory*; write the migrated form only on the
  first real save, and write a one-time `experiment.v<N>.json.bak`
  sibling before the first overwrite. Migrations live in one module
  (`session/migrations.py`), are pure functions `dict -> dict` chained
  version-by-version, and each ships with a fixture file of the old
  format in `tests/` — the migration harness replays every historical
  fixture through to current on every CI run, forever. That harness is
  what makes G1 a test, not a hope.

Conformance additions (`tests/test_conformance.py`): every dataclass in
`session/models.py` round-trips with `schema_version` present; every
persisted-artifact writer in the session layer uses the atomic helper;
no persisted string field of any model matches an absolute-path pattern
after `to_dict()` of a store-produced record; every migration fixture
loads to the current version.

## 6. Record scaling and the tick path (§1 gap 6)

Decision now, structure later:

- **Keep one `experiment.json` per bundle for v1** — human
  inspectability and one-file manifests are worth more than premature
  sharding; measured sizes (a full station snapshot is a few KB; a heavy
  session of ~200 runs lands around 1 MB) are fine for atomic rewrite.
- **Bound the cost where it runs.** The saves triggered from run
  manifests execute in the Orchestrator's signal cascade. Two cheap
  guards land in slice 1: (a) the settings snapshot is captured at run
  start only (already true) and *capped* — a snapshot exceeding a size
  limit (config-free constant, ~256 KB serialized) is stored truncated
  with an explicit `"_truncated": true` marker; (b) a WARNING logs the
  first time a record save exceeds 100 ms, so growth is observed before
  it is felt.
- **The escape hatch is pre-declared** (that's what `runs/` in §3 is
  reserved for): if a real setup crosses the tripwire, run rows move to
  one sidecar JSON per run under `runs/`, `experiment.json` keeps a
  light index, and `schema_version` bumps through the S8 harness. The
  decision costs nothing today and prevents an unplanned format fork
  under pressure later.

## 7. Amendments to the `unified-session-record.md` slice

That plan is right and stays the implementation vehicle. Fold these in
(same slice, mostly the same files it already touches):

1. **`schema_version` on `ExperimentRecord.to_dict()` and the new
   `gui_state.json`** (S1) — one field now versus a forensic "which era
   is this file from" migration later. `active.json` gets it too.
2. **`RunRecord.data_file` stored bundle-relative** (S4). The manager
   receives the absolute path in the manifest and relativizes against
   `store.data_dir(experiment_id)` before recording; the compatibility
   read path covers all existing flat-file records, so the unified
   plan's "no migration of old HDF5 files" stance is preserved.
3. **Save-failure surfacing** (G5): `SessionManager` gains a
   `store_health_changed(dict)` signal — emitted on the first failed
   save and on recovery. The GUI shows it in the session status area
   (the notification-banner pattern); after N consecutive failures the
   message escalates to "runs are NOT being recorded". No behavior
   change to the measurement itself.
4. **The store lock** (§8) taken at `ExperimentStore` construction in
   `main.py`, released on exit.
5. **Sealed-bundle rule**: `switch_experiment` already refuses closed
   targets; additionally, `SessionManager` must refuse any mutation of a
   record whose `status != open` (defense in depth for future callers —
   today's call sites already can't reach this).
6. **Snapshot cap + slow-save WARNING** (§6).

Everything else in that plan — layout, `sessions_root` setting, queue
promotion, dialogs, menu items, test list — stands as written.

## 8. Concurrency: the store lock

Minimal, honest, and boring — this is a lab desktop app, not a database:

- `<sessions_root>/.lock` containing `{host, pid, started_utc, app_version}`,
  written with the atomic helper at store construction.
- Stale detection: same host + dead PID ⇒ take over (log INFO). Live PID
  or *different host* ⇒ start **read-only**: the app opens, monitoring
  and measurement work, but the session UI shows "sessions locked by
  <host>" and `SessionManager` refuses `start_experiment`/record writes
  the same way it will refuse newer-version files (S1). A `--force`
  override does not exist; the human resolves it by closing the other
  instance. (Cross-host PID liveness is unknowable — refusing is the
  only honest behavior on a shared drive.)
- The lock protects the *store*; external readers (agents, humans,
  backup scripts) never need it — S3 atomic writes guarantee they always
  see a complete file. The future Agent Gateway runs *inside* the
  locked process, so it inherits writership; external Claude Code
  sessions go through the Gateway, never through direct file writes.
  This sentence goes in the gateway README when A1 lands.

## 9. Failure-mode matrix (the behaviors tests must pin)

| Failure | Behavior (all exist or land in slice 1) |
|---|---|
| Crash mid-run | Resume marks `running` runs failed with reason; envelope re-installed; queue restored from record (unified plan). |
| Crash mid-write | `.tmp` orphan ignored; previous file intact (S3). |
| Disk full / share dropped | Measurement continues; `store_health_changed` banner; recovery announced (amendment 3). |
| Corrupt `experiment.json` | Load returns `None`; active pointer cleared with WARNING (exists); bundle left untouched for post-mortem. |
| Record from newer CryoSoft | Read-only + operator message; never overwritten (S1). |
| Record from older CryoSoft | Migrated in memory; `.bak` written once on first save (S8). |
| Second instance / other machine | Read-only mode with named holder (§8). |
| Bundle moved/copied elsewhere | Everything resolves — relative paths (S4); relocation test in CI: build a session with runs via sim, `shutil.move` the bundle + root, reload, assert every run's data file opens. |
| Old flat-layout data (pre-unification) | Absolute-path read compatibility + basename re-resolution (S4). |

## 10. Extensibility to very different setups

Why this design needs zero changes for a new rig — and the checks that
keep it true:

- **Schema:** nothing in the bundle names an instrument type (S6).
  VI-keyed dicts and free-form metadata absorb any station
  `devices.yaml` can describe; a setup with no magnet, three
  thermometers, and a custom VI produces byte-identical *structure*.
- **Features:** optional subsystems (cryogenics, operations, future
  ones) live in Setup-tier stores gated by config blocks — they never
  add required fields to the bundle. A session from a setup without
  feature X is indistinguishable from one where X was unused.
- **Location:** `sessions_root` is one Setup-tier setting; a lab can
  point it at a local disk, a mirrored share, or per-project roots, and
  the bundle contract is identical everywhere.
- **Consumers:** the eLab publisher, the Agent Gateway's read tools, and
  any lab's ad-hoc analysis scripts all read the same documented bundle
  — the bundle *is* the integration surface, which is exactly what
  makes the framework extendable without coordination.

## 11. Roadmap

- **S-slice 1 (now):** `unified-session-record.md` + §7 amendments.
  One PR series, `make check` green, GLOSSARY rows (**Session bundle**,
  **Sealed bundle**, **Persistence tier**, **Store lock**,
  **Session-record standard**), session README gains the standard.
- **S-slice 2:** the migration harness (`session/migrations.py`, first
  fixture = a pre-`schema_version` v0 record), relocation CI test,
  conformance clauses from §5. No user-visible change; purely the
  guarantees.
- **S-slice 3:** read-only history browsing (open sealed bundles in the
  Load Session dialog, view-only), export/archive affordance ("Reveal
  session folder" first; zip export only if a lab asks).
- **Alignment:** F1 of the agent-native roadmap is complete once slice 1
  lands; A1 (gateway read tools) and B1 (ELN adapter) both consume the
  bundle and its reserved files with no further session-layer changes.

## 12. Open questions

1. **Sealing strictness:** should *findings* remain editable on a closed
   session (a physicist writes conclusions days later)? Proposed: yes —
   `set_findings` becomes the one sanctioned post-close mutation,
   recorded with a `findings_revised_utc`; everything else stays sealed.
   Decide before slice 1's `switch_experiment` tests are written.
2. **Data filename linkage:** should HDF5 files be named by `run_id`
   (perfect record↔file correlation) instead of today's
   `<prefix>_<timestamp>.h5`? Cheap in the DataManager, but it changes a
   user-visible convention — ask the physicist before slice 1.
3. **Read-only app mode UX** (§8): full read-only GUI vs. blocking only
   session features while measurement stays live. Proposed: block
   session features only — a locked store must not prevent an emergency
   field ramp-down. Confirm.
4. **Backup guidance:** the bundle makes external sync (robocopy,
   Syncthing, institutional backup) sufficient. Do we want an in-app
   "mirror sessions root" later, or explicitly declare backup
   out-of-app forever? Leaning: out-of-app, documented in the README.
