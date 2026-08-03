# Config-directory migration

**Status: proposal, not started.** Companion to the log-directory split
(`cryosoft/core/paths.py`, `docs/plans/archive/trend-history-persistence.md`):
same problem, one tier further along than logs already are.

## Problem

`cryosoft/configs/` currently holds four directories, all git-tracked:

- `sim_cryostat/`, `sim_real_cryostat/` — fully simulated, no site data. Safe
  to ship; the test suite and CI build against them.
- `a-sample-real-cryostat/` — the Kläui Lab reference station. Per
  `cryosoft/configs/README.md`, addresses are "partly placeholder pending
  bench check" — an illustrative example, not necessarily live secrets.
- `12t-cryo/` — a real commissioned installation (see
  `cryosoft/logs/incidents/2026-07-16-commissioning-12t-cryo.md`). Its
  `devices.yaml` carries real GPIB/VISA addresses and safety limits tuned to
  one physical rig.

That last one is the actual problem: a real setup's addresses and limits are
per-installation data, not source code, and don't belong in a repository with
a GitHub remote (`[[project_gitlab_migration]]` memory: GitLab is canonical,
`github` remote is kept). Every commit that touched `12t-cryo/` already
carries whatever was in it at that point — moving the file forward stops
future commits from adding to that, it does not retroactively remove it from
history. If a bench check confirms real, sensitive values are in there,
scrubbing prior history is a separate, more invasive decision for the user
to authorize explicitly; this plan only covers the forward-looking move.

## What already exists (don't rebuild this)

The shipped/user split this plan needs is **already built**, just not used
for real configs yet:

- `cryosoft/core/config_catalog.py`: `ConfigCatalog` discovers shipped
  (read-only, git-tracked) vs. user (writable, per-installation) config
  directories, forks a shipped config into an editable user copy on first
  edit, and keeps a named version history per user config.
- `cryosoft/gui/app_settings.py`: `user_config_dir()` resolves
  `%APPDATA%/CryoSoft/configs` via `QStandardPaths.AppDataLocation`;
  `shipped_config_dir()` resolves `cryosoft/configs` next to the package.
- `cryosoft/troubleshoot/cli.py`: `_user_config_dir()` / `_shipped_config_dir()`
  are a **second, independent implementation** of the same two paths — the
  module docstring says why: import-linter contract C10 keeps the
  troubleshoot toolbox out of `cryosoft.gui`, so it can't import
  `app_settings` and hand-rolls the APPDATA lookup with
  `os.environ.get("APPDATA", ...)` instead of Qt's `QStandardPaths`.

So a config's "shipped vs. user" location is already a first-class concept.
What's missing is (a) real configs actually living in the user tier instead
of the shipped one, and (b) one canonical, stdlib-only implementation of the
path logic instead of two hand-maintained copies.

## Proposed change

1. **Consolidate the duplicated resolver into `cryosoft/core/paths.py`.**
   Add `config_directory()` (the user tier — `%APPDATA%\CryoSoft\configs` on
   Windows, `$XDG_CONFIG_HOME/cryosoft` default `~/.config/cryosoft`
   elsewhere) and `shipped_config_dir()` (`cryosoft/configs`, next to the
   package), following the exact env-var → platform-dir → repo-fallback
   shape `log_directory()` already established, stdlib-only, C1 foundation.
   `app_settings.py` and `cli.py` both import from it instead of each
   maintaining their own copy — this is the same fix already applied to
   `log_directory()` in this session, one tier over.
   - `app_settings.user_config_dir()`'s use of `QStandardPaths` should stay
     as a thin Qt-flavoured wrapper if any GUI-only behavior depends on it
     (verify before removing), but the *path computation* itself moves to
     `paths.py` so there's one source of truth.
   - `data_directory()` for durable per-installation records (see below)
     joins the same module.

2. **Move real per-site configs out of the shipped tier.**
   - `12t-cryo/` moves to the user config directory (`config_directory()`)
     on whichever machine actually runs that installation, and drops out of
     `cryosoft/configs/` and git entirely.
   - `a-sample-real-cryostat/` needs an explicit decision, not an assumption:
     is it a genuine site config that should also move, or a deliberately
     public, placeholder-address *example* that stays shipped precisely so
     new users have a real-driver-shaped config to read? Confirm which with
     whoever owns the Kläui Lab station before moving it — don't fold it into
     step 2 automatically alongside `12t-cryo/`.
   - `.gitignore` gets an explicit rule so a real config dropped into
     `cryosoft/configs/` by habit doesn't get committed by accident — e.g.
     ignore everything under `cryosoft/configs/*/` except an explicit
     allowlist of the shipped names (mirrors the existing whitelist
     structure at the top of `.gitignore`).

3. **This is what guarantees the repo always has a default config.** Once
   step 2 lands, `cryosoft/configs/` contains only `sim_cryostat/` and
   `sim_real_cryostat/` (plus `a-sample-real-cryostat/` if step 2 keeps it).
   That is already exactly what `resolve_config()` (`cli.py`) and
   `build_station_with_fallback()` (`station.py`) depend on today —
   `resolve_config()` falls back to `shipped_config_dir() / "sim_cryostat"`
   when nothing else is selected, and `build_station_with_fallback()`'s
   docstring calls `sim_cryostat` "the always-loadable" last candidate. Right
   now that guarantee is *accidentally* true — the shipped tier also happens
   to contain real site data. After this migration it becomes *actually*
   true by construction: the shipped tier can only ever contain configs with
   no site-specific secrets, because nothing else is allowed to live there.
   No new mechanism needed — this plan makes the existing fallback promise
   match what's actually in git.

4. **`setup-commission` writes new configs to the user tier, never the repo.**
   The skill currently has no documented target directory for a brand-new
   commissioned config; whatever it picks by default should be
   `config_directory()` / a name, not `cryosoft/configs/<name>` — so a newly
   commissioned setup never touches git in the first place, matching the
   same intent as the incident-report / `data_directory()` move (see below).

5. **Incidents move alongside, via the same module.** Per the earlier
   discussion in this session: incident reports
   (`cryosoft/logs/incidents/YYYY-MM-DD-<slug>.md`) are durable
   per-installation records, not disposable telemetry, so they don't belong
   under `log_directory()`'s "no migration, starts empty" contract either.
   Add `data_directory()` to `paths.py` (same precedence shape; Windows
   `%LOCALAPPDATA%\CryoSoft\data`, POSIX `$XDG_DATA_HOME/cryosoft` default
   `~/.local/share/cryosoft`) and move incidents to
   `data_directory() / "incidents"`. `connection_status.json`
   (`.claude/skills/diagnose-connections/connection_status.py`) moves the
   same way — it's a live per-installation snapshot, not source code.
   Update `GLOSSARY.md`'s **Incident report** entry and the two skills that
   currently hardcode `cryosoft/logs/...` (`diagnose-connections`,
   `setup-commission`) to call the resolver instead of a literal path —
   same fix already applied to `log_directory()`'s callers this session.

## Verify before touching any file

`cryosoft/configs/` directories are auto-discovered by more than one test —
`tests/test_conformance.py` (schema), `tests/test_config_catalog.py`
(discovery), `tests/test_config_validation.py` (limit/reference validation).
Before moving `12t-cryo/` (or `a-sample-real-cryostat/`) out of the repo,
check whether any of these hardcode an expected config count or name rather
than iterating whatever is present — a test asserting "exactly 4 configs
exist" would need updating in the same change, not discovered by a red CI
run after the fact.

## Out of scope here

- Rewriting git history to remove `12t-cryo/`'s past commits — a separate,
  explicit decision (see Problem above).
- Any change to `log_directory()` or the logs migration — already done
  (`cryosoft/core/paths.py`, this session).
