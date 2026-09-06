# CLAUDE.md — I2AS

I2AS (Instrument to Agentic Station) is a framework for building an
agent-operable measurement station out of a rack of laboratory instruments:
a PyQt6 desktop application plus an agent gateway, both clients of one
single-writer engine, built as a layered, standards-driven architecture. A
user writes drivers, virtual instruments, procedures, analysis recipes and
one YAML config; the framework supplies the orchestration, the safety and
the agent surface. This file is the guide for anyone — human or agent —
contributing to the framework or building a station on it.

## Environment

The project virtual environment lives at `.venv` (project root). Run all
code, tests, and tools through it, and install any required libraries into
it, never into the system Python.

## Architecture: seven layers, dependencies point strictly downward

```
L0  Drivers               raw instrument I/O; sim drivers mirror real drivers 1:1
L1  Virtual instruments   standardised capability interfaces over drivers
L2  Station + Config      builds VIs from YAML configs; owns state snapshots
L3  Orchestrator          single tick loop and state machine; sole writer to hardware
L4  Procedures            measurement recipes driven by the Orchestrator
L5  Data manager          HDF5 output (lives in core/)
L6  Session manager       experiments, runs, queue, analysis, notebook, agent gateway
GUI                       PyQt6 windows; hold an Orchestrator proxy, never the engine
```

Beside the stack, as separate processes that can import nothing that
touches an instrument: the analysis worker (`analysis/`), the MCP adapter
(`mcp/`), the reference CLI (`ctl/`) and the doctor CLI (`troubleshoot/`).

- **The single hardware thread standard.** Exactly one thread — the
  *instrument thread* — ever touches the Orchestrator, the Station, a VI, a
  driver or the DataManager. Inside it nothing has changed: one QTimer tick
  drives everything and ramps are generators that yield one step per tick.
  The GUI, the session layer, the agent gateway, analysis and every network
  call live on the main thread, and the two sides meet only through the
  control contract — a `Command` in, one `Verdict` and the `Event` stream
  back — over queued Qt connections carrying copies. Never a second thread on
  the bus and never a lock around it; never a blocking call on the GUI side
  waiting for the engine (a read is answered from the status mirror, a
  command is posted and answered by its verdict later); never a blocking call
  in the tick path. `inline` mode is the same design collapsed onto one
  thread and is temporary — one release, then it goes.
- **The Orchestrator is a state machine** with states IDLE, INITIATING,
  RAMPING, MEASURING, SWEEPING, STANDBY, PAUSED, ERROR, EMERGENCY. All
  hardware writes flow through it; the GUI, the agent gateway and procedures
  submit requests, they never touch drivers or VIs directly.
- **Config files are the single source of truth** for instrument addresses,
  safety limits, and variable mappings. Limits are setup properties, so they
  live in the config, never hardcoded.
- If a higher layer needs a new capability from a lower one, route it through
  every layer in between (driver method → VI method → Orchestrator action).
  Never shortcut across layers, and never create a circular import; if one
  seems necessary, the design is wrong and needs refactoring.

## Standards over one-off code

The core principle of this repository: every level defines a written,
machine-checked standard, so that adding a new driver, virtual instrument,
procedure, analysis recipe or config means implementing the standard with
minimal new code and zero changes to the core. When you build something new,
follow the existing standard for that level; when a task genuinely needs a
new convention, write it down as a standard (base-class docstring, folder
README, GLOSSARY entry) and add a conformance test so future work inherits
it automatically. Existing standards include:

- **Driver contract**: a plain Python class, `__init__` takes a single VISA
  resource string, importable from `i2as.drivers.*`, with `get_idn()`,
  `close()` and `safe_shutdown()`; every real driver has a sim twin with an
  identical public API that models the instrument's physics (including
  failure modes) so wrong command sequences fail in tests instead of on
  hardware. Every state-changing write is verified and says how.
- **Sim coupling**: two sims that share a physical quantity exchange it
  through a `SimEnvironment` named by the `@<name>` suffix of their resource
  strings, never by importing each other. See `drivers/README.md`.
- **VI contract**: `__init__(self, drivers, **init_params)`, silent on the
  bus; capabilities exposed via the `@monitored` and `@control` decorators;
  safety interlocks via `evaluate_safety()` and the `safety_flags` manifest
  (`advisory` / `hold` / `critical`; a critical flag is station-wide by
  construction).
- **Action-class declaration**: every `@control` declares
  `action_class=` (`read` / `recovery` / `run_control` / `envelope`), the
  authority an agent needs for it. It is a judgement about the instrument,
  so it lives on the VI; the gateway reads it off the station declaration.
  Conformance requires every shipped control to declare it.
- **Control contract**: `core/events.py`'s frozen, JSON-safe `Command` /
  `Verdict` / `Event` families, declared once and rendered for both clients —
  a client sends a `Command`, gets exactly one `Verdict`, and sees every
  consequence as an `Event`, so neither client can offer an action the other
  cannot see.
- **Single hardware thread**: one thread owns every instrument and the tick;
  everything else is a client of it over the control contract. Whoever
  decides which thread is `core/instrument_host.py`; the full text is the
  paragraph above and `GLOSSARY.md`'s **Instrument thread**.
- **Control-validation standard**: every `@control` method declares its
  limited parameters in the `control_limits` class attribute; limit values
  come from config `init_params`; the base class enforces them before any
  hardware call and every GUI action gets an explicit success or failure
  verdict. See `virtual_instruments/base.py`.
- **Role discovery**: a procedure never names a configured instrument. It
  declares the roles it needs in `role_parameters`; the candidates come from
  the Station's role accessors (`magnet_vi_names()`, `temperature_vi_names()`,
  `stage_vi_names()`, the fronts of `vi_names_by_base()`), the choice renders
  as an ordinary parameter, and the only candidate is the default. See
  `procedures/README.md`.
- **Image blocks**: a measurement VI whose reading is a frame declares it in
  `measurement_image_blocks` with its pixel shape and unit; the data manager
  writes it as a self-describing 2-D block and `data_reader.read_image()`
  serves it back. The raw diagnostic block is its channel-labelled sibling.
  See `virtual_instruments/measurement/README.md`.
- **Folder README standard**: every functional folder has a `README.md` with
  Purpose, Architecture layer, Entry, Exit, Interface contract, How to add a
  new module, and Files sections. Update it in the same commit as the code
  change it describes.
- **Code-reference standard**: comments and docstrings in `i2as/` cite
  *standards*, never design documents. Name the concept ("the hard status
  separation", "the claim standard") and, if a pointer is needed, point at
  `GLOSSARY.md`, the folder `README.md`, or the owning base class. Never
  write `plan §4.2` or a path to a planning document in code: a plan is a
  dated proposal that gets implemented and superseded, and a comment citing
  one is a pointer that rots silently and says nothing to a reader who does
  not have the document. This repository ships no design documents at all;
  the code plus its READMEs, base-class docstrings and `GLOSSARY.md` present
  the complete picture on their own, and the conformance suite fails any
  plan-document citation under `i2as/`. Vendor documentation is the
  deliberate exception: an instrument manual's section number
  (`vendor doc §3.11`) is a stable external reference and belongs in the
  driver that implements it.

## Build bottom-up, test at every layer

Each layer must have passing tests before anything is built on top of it.
Deliver tests alongside code, never as an afterthought. For a new class:
interface first, then tests against sims/mocks, then implementation, then an
integration test with the layer below. Every feature must be testable
without hardware — the two shipped configs, `sim_cryostat` and
`sim_imaging`, are the stations the suite builds.

## The harness: `make check` before you are done

No task is complete until `make check` passes (equivalently `ruff check .`,
`lint-imports`, `pytest -m "not hardware"` in both thread modes, all from
the activated `.venv`). CI runs exactly these targets on every push. Three
parts:

- **Layer contracts**: the numbered import-linter rules in `pyproject.toml`
  (which is their only source of truth — do not restate the count here, it
  grows) that enforce the layer boundaries mechanically. Never edit or weaken
  a contract to make it pass; propose the change instead.
- **Conformance tests** (`tests/test_conformance.py`): auto-discover every
  driver, VI, procedure, config and analysis recipe and check it against its
  standard. A new module is covered the moment the file exists. If a
  conformance test fails on your module, fix the module, never the test.
- **Behavior tests**: the layer suites in `tests/`. Conformance coverage is
  necessary but not sufficient; new behavior needs its own tests.

**Terminology**: `GLOSSARY.md` at the project root is the canonical
vocabulary. Use its terms exactly; if a change introduces a new recurring
term, add it to `GLOSSARY.md` in the same commit.

## Coding principles

- **Type hints everywhere**; use `from __future__ import annotations` for
  forward references.
- **Google-style docstrings on every public method** (Args, Returns,
  Raises); they are the documentation for people writing new drivers and
  procedures, and the text the agent's tool descriptions are rendered from.
- **Logging, never print**: `logging.getLogger(__name__)` per module. DEBUG
  for bus traffic, INFO for state changes, WARNING for recoverable issues,
  ERROR for failures, CRITICAL for safety events.
- **SI units in all APIs**: Tesla, Kelvin, Ampere, Volt, metre, second.
  Display formatting (mK, µA, mm) is a GUI concern only.
- **Constants and limits in config, not in code.**
- **Naming during the extraction**: the package is still importable as
  `i2as` and a later, mechanical rename converts it. In prose the
  framework is I2AS; code paths, module names, environment variables
  (`I2AS_*`) and commands (`python -m i2as.…`) keep their current
  spelling until that sweep.

## Session workflow (optional local practice)

A contributor may keep `LOGBOOK.md` at the project root — an untracked
working log, newest entry first, recording what changed, which tests
actually passed, what is blocked and what is next. If you keep one, read it
first in every session and prepend a dated entry last; mark work "Done" only
when tests pass, and never rewrite past entries. Local working documents
(`LOGBOOK.md`, `directives/`) are intentionally untracked; do not commit
them.

## Scope discipline

Do not modify, refactor, or extend anything beyond the explicit scope of the
current task. If something elsewhere should be improved, mention it as a
suggestion and leave it unchanged. Do not add features beyond the
architecture silently; propose them first. "Implement the stage VI" means
exactly the stage VI, its tests, and its sim, nothing else.
