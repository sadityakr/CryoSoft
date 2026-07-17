# CLAUDE.md — CryoSoft

Instrument-agnostic cryostat operating system: a PyQt6 desktop application
that controls cryostat measurement systems (superconducting magnets,
temperature controllers, cryogen level meters, source/measure electronics)
through a layered, standards-driven architecture.

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
L6  Session manager       experiments, runs, users; envelope + run records
GUI                       PyQt6 windows; talks only to the Orchestrator's public API
```

- **Single-threaded cooperative scheduling.** One QTimer tick drives
  everything; ramps are generators that yield one step per tick. There is no
  second thread and no concurrent bus access, which is the design's answer
  to GPIB race conditions. Never add a thread or a blocking call in the tick
  path.
- **The Orchestrator is a state machine** with states IDLE, INITIATING,
  RAMPING, MEASURING, SWEEPING, STANDBY, PAUSED, ERROR, EMERGENCY. All
  hardware writes flow through it; the GUI and procedures submit requests,
  they never touch drivers or VIs directly.
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
procedure, or config means implementing the standard with minimal new code
and zero changes to the core. When you build something new, follow the
existing standard for that level; when a task genuinely needs a new
convention, write it down as a standard (base-class docstring, folder
README, GLOSSARY entry) and add a conformance test so future work inherits
it automatically. Existing standards include:

- **Driver contract**: a plain Python class, `__init__` takes a single VISA
  resource string, importable from `cryosoft.drivers.*`; every real driver
  has a sim twin with an identical public API that models the instrument's
  physics (including failure modes) so wrong command sequences fail in tests
  instead of on hardware.
- **VI contract**: `__init__(self, drivers, **init_params)`; capabilities
  exposed via the `@monitored` and `@control` decorators; safety interlocks
  via `evaluate_safety()`.
- **Control-validation standard**: every `@control` method declares its
  limited parameters in the `control_limits` class attribute; limit values
  come from config `init_params`; the base class enforces them before any
  hardware call and every GUI action gets an explicit success or failure
  verdict. See `virtual_instruments/base.py`.
- **Folder README standard**: every functional folder has a `README.md` with
  Purpose, Architecture layer, Entry, Exit, Interface contract, How to add a
  new module, and Files sections. Update it in the same commit as the code
  change it describes.

## Build bottom-up, test at every layer

Each layer must have passing tests before anything is built on top of it.
Deliver tests alongside code, never as an afterthought. For a new class:
interface first, then tests against sims/mocks, then implementation, then an
integration test with the layer below. Every feature must be testable
without hardware.

## The harness: `make check` before you are done

No task is complete until `make check` passes (equivalently `ruff check .`,
`lint-imports`, `pytest -m "not hardware"`, all from the activated `.venv`).
CI runs exactly these targets on every push. Three parts:

- **Layer contracts (C1–C12)**: import-linter rules in `pyproject.toml` that
  enforce the layer boundaries mechanically. Never edit or weaken a contract
  to make it pass; propose the change instead.
- **Conformance tests** (`tests/test_conformance.py`): auto-discover every
  driver, VI, procedure, and config and check it against its standard. A new
  module is covered the moment the file exists. If a conformance test fails
  on your module, fix the module, never the test.
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
  procedures.
- **Logging, never print**: `logging.getLogger(__name__)` per module. DEBUG
  for bus traffic, INFO for state changes, WARNING for recoverable issues,
  ERROR for failures, CRITICAL for safety events.
- **SI units in all APIs**: Tesla, Kelvin, Ampere, Volt, second. Display
  formatting (mK, µA) is a GUI concern only.
- **Constants and limits in config, not in code.**

## Session workflow

- **First action of every session: read `LOGBOOK.md`** (a local working log
  at the project root, not git-tracked). It records, newest first, what
  changed, what tests pass, what is blocked, and what is next.
- **Last action of every session: prepend a dated `LOGBOOK.md` entry** with
  what changed, the actual test results, and the next step. Mark work "Done"
  only when tests pass. Never rewrite past entries.
- Local working documents (`LOGBOOK.md`, `directives/`) are intentionally
  untracked; do not commit them.

## Scope discipline

Do not modify, refactor, or extend anything beyond the explicit scope of the
current task. If something elsewhere should be improved, mention it as a
suggestion and leave it unchanged. Do not add features beyond the
architecture silently; propose them first. "Implement the magnet VI" means
exactly the magnet VI, its tests, and its sim, nothing else.
