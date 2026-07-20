# troubleshoot/

## Purpose
Diagnostic toolbox for commissioning a new setup and debugging a misbehaving
one: scan the VISA bus, preflight a config against the real instruments, and
exercise individual driver methods or raw SCPI commands. Built primarily for
agents (the setup-supervisor skill family drives it via the CLI), usable by
humans directly. The main application never uses this package; it is run
while the app is closed, because serial instruments are exclusive-open.

## Architecture layer
None of L0–L5. Like `cryosoft/main.py`, this package sits *beside* the stack
as a leaf entry point: it may import drivers (L0) and the Station's config
helpers (L2), and nothing in cryosoft imports it. Contracts C9/C10 in
`pyproject.toml` enforce both directions.

## Entry (what comes in)
Config directories (`devices.yaml`), VISA addresses, driver aliases, and
driver method names with string arguments. A pyvisa `ResourceManager` is
injected everywhere, so tests substitute a fake bus.

## Exit (what goes out)
`ProbeResult` and `MethodInfo` dataclasses — JSON-ready via `as_dict()`, with
every failure classified by a stable `FaultCode` (the machine-readable fault
taxonomy the triage skill branches on). Log records via the standard logging
setup. The engine writes no files.

## Interface contract
Three invariants, load-bearing for agent use:

1. **Every operation terminates on its own** — bounded by VISA timeouts,
   never waiting on input.
2. **`FaultCode` values are API** — the triage skill maps them to physical
   causes; never rename or repurpose one, only add.
3. **Read/write separation** — `DriverBench.call()` refuses state-changing
   methods unless `allow_write=True`; the CLI exposes the two paths as
   separate subcommands (`read` vs `write`) so the permission harness can
   gate them differently.

The engine relies on the driver contract's `get_idn()` (enforced by
`tests/test_conformance.py::test_driver_has_get_idn`) as the universal
identify probe, and on the optional `expect_idn` key in a config's
`real_drivers` entry (substring, case-insensitive) for identity checks.

## How to add a new module
This package should stay small. New diagnostic primitives go into
`engine.py` (Qt-free, injectable dependencies, always-terminating); new user
surfaces (CLI subcommands) go into `cli.py`. Anything Qt belongs in
`cryosoft/gui/`, not here.

## CLI
`python -m cryosoft.troubleshoot <subcommand>` — one-shot commands, each
terminating on its own, with `--json` for machine-readable output and exit
code 0 (all OK) / 1 (any fault). Every invocation appends a JSONL line to
`cryosoft/logs/troubleshoot.jsonl` (the session transcript agents mine when
hardening the triage skill).

| Subcommand | What it does | Allowlist-safe? |
|---|---|---|
| `scan [--probe] [--probe-serial]` | list bus resources, optionally identify each | yes |
| `probe <address>` | raw identify query to one bare address | yes |
| `check [--config X] [--no-bus]` | preflight every driver in a config | yes |
| `bench-l0 [--config X]` | L0 bench: idn + one passive getter per driver (zero excitation) | yes |
| `methods <target>` | list a driver's public methods | yes |
| `idn <target>` | identify one instrument via its driver | yes |
| `read <target> <method> [args] [--repeat N] [--interval S]` | call a read-only driver method; repeats expose intermittent/timing faults | yes |
| `write <target> <method> [args]` | call a state-changing method | **no — keep prompted** |
| `query <target> "<cmd>"` | raw command with reply | **no — raw bytes can mutate state** |
| `send <target> "<cmd>"` | raw command, no reply | **no — keep prompted** |

`<target>` is a config alias, or a dotted driver class path together with
`--address` (driver development). `--config` takes a path or a bare name
resolved against shipped and user config folders; default is the machine's
saved active config, falling back to `sim_cryostat`.

## Files
- `engine.py` — bus scan, config preflight (`check_config`), the L0 bench
  (`bench_l0` — idn + one passive getter per driver), `DriverBench`
  (introspect / call / raw query-send), `FaultCode` taxonomy.
- `cli.py` — the one-shot argparse CLI over the engine (grammar above is API
  for skills and allowlists).
- `__main__.py` — `python -m cryosoft.troubleshoot` entry point.
