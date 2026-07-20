# ---
# description: |
#   One-shot CLI over the troubleshoot engine: scan / probe / check / bench-l0 /
#   methods / idn / read / write / query / send, plus `status` (reads the
#   running app's operational-status log). Built for agents first: every
#   command terminates on its own, supports --json, returns exit code 0 (all
#   OK) or 1 (any fault), and appends a JSONL transcript line to cryosoft/logs/.
# entry_point: python -m cryosoft.troubleshoot <subcommand> ...
# dependencies:
#   - pyvisa (only for commands that touch a real bus)
#   - PyQt6 (optional: only to read the saved active config from QSettings)
# input: |
#   A subcommand plus arguments (see --help). --config accepts a config
#   directory path or a bare name resolved against the shipped and user config
#   folders; when omitted, the machine's saved active config is used, falling
#   back to the shipped sim_cryostat.
# process: |
#   Parses arguments, resolves the config, runs one engine operation, renders
#   the outcome (table or --json), appends the transcript line, and exits.
# output: |
#   Structured results on stdout, log records on stderr and cryosoft.log, one
#   JSONL line per invocation in cryosoft/logs/troubleshoot.jsonl.
# ---

"""Troubleshoot CLI — one-shot diagnostic commands for agents and humans.

Command grammar is API: the setup-supervisor skills and permission allowlists
hard-code it, so subcommand names and their meanings must stay stable.

Read/write split for permission gating: ``scan``, ``probe``, ``check``,
``bench-l0``, ``methods``, ``idn``, ``read``, and ``status`` never change
instrument state and are safe to allowlist (``status`` only reads a log
file). ``write`` calls state-changing driver methods and ``query`` /
``send`` transmit arbitrary raw bytes (a raw query can mutate state too), so
those three should stay behind a permission prompt.

There are deliberately no interactive prompts: authorization is the
harness's job, and a hung prompt is the worst failure mode for an agent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cryosoft
from cryosoft.core.logging_config import setup_logging
from cryosoft.troubleshoot import engine, status_reader
from cryosoft.troubleshoot.engine import (
    DriverBench,
    L0BenchResult,
    ProbeResult,
    bench_l0,
    check_config,
    probe_address,
    scan_bus,
)

logger = logging.getLogger(__name__)

# Mirrors cryosoft/gui/app_settings.py (_ORGANISATION/_APPLICATION/
# _ACTIVE_CONFIG_NAME_KEY/_ACTIVE_CONFIG_SOURCE_KEY). Duplicated because
# contract C10 keeps this package out of cryosoft.gui — if app_settings
# changes these, change them here too.
_QSETTINGS_ORG = "CryoSoft"
_QSETTINGS_APP = "CryoSoft"
_ACTIVE_CONFIG_NAME_KEY = "ActiveConfig/name"
_ACTIVE_CONFIG_SOURCE_KEY = "ActiveConfig/source"

# Test seam: tests monkeypatch these two module attributes.
_rm_factory = engine.open_resource_manager


def _transcript_dir() -> Path:
    """Directory for the JSONL invocation transcript (cryosoft/logs)."""
    return Path(cryosoft.__file__).parent / "logs"


# ── Config resolution ─────────────────────────────────────────────────────────


def _shipped_config_dir() -> Path:
    return Path(cryosoft.__file__).parent / "configs"


def _user_config_dir() -> Path:
    import os

    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CryoSoft" / "configs"


def _read_active_config() -> str | None:
    """Return the app's saved active-config directory, or None if unavailable.

    Resolved from the saved ``(name, source)`` identity rather than a stored
    path, so it stays correct across clones/worktrees (see app_settings.py
    ``config_active``/``set_config_active`` for the rationale).
    """
    try:
        from PyQt6.QtCore import QSettings
    except ImportError:
        return None
    settings = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    name = settings.value(_ACTIVE_CONFIG_NAME_KEY)
    source = settings.value(_ACTIVE_CONFIG_SOURCE_KEY)
    if not name or not source:
        return None
    base_dir = _user_config_dir() if str(source) == "user" else _shipped_config_dir()
    return str(base_dir / str(name))


def resolve_config(value: str | None) -> str:
    """Resolve a --config argument to a config directory path.

    Resolution order:

    1. ``value`` as a directory path (absolute or relative).
    2. ``value`` as a bare name under the shipped configs, then user configs.
    3. With no value: the machine's saved active config (QSettings), falling
       back to the shipped ``sim_cryostat``.

    Args:
        value: The --config argument, or None.

    Returns:
        Path string of an existing config directory.

    Raises:
        SystemExit: Via argparse-style error if nothing resolves.
    """
    if value:
        candidates = [
            Path(value),
            _shipped_config_dir() / value,
            _user_config_dir() / value,
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return str(candidate)
        raise SystemExit(
            f"error: config '{value}' not found (tried "
            f"{[str(c) for c in candidates]})"
        )

    active = _read_active_config()
    if active and Path(active).is_dir():
        return active
    return str(_shipped_config_dir() / "sim_cryostat")


# ── Bench construction ────────────────────────────────────────────────────────


def _make_bench(args: argparse.Namespace) -> DriverBench:
    """Build the bench from TARGET: config alias, or dotted class + --address."""
    if getattr(args, "address", None):
        return DriverBench.from_class(args.target, args.address)
    return DriverBench.from_config(resolve_config(args.config), args.target)


# ── Rendering ─────────────────────────────────────────────────────────────────


def _print_json(payload: dict[str, Any]) -> None:
    # default=repr: driver methods may return values JSON cannot encode.
    print(json.dumps(payload, indent=2, default=repr))


def _print_probe_table(results: list[ProbeResult]) -> None:
    for r in results:
        name = r.alias or r.address
        extra = r.idn or r.detail
        print(f"{name:<20} {r.address:<22} {r.code.value:<20} {extra}")
        if r.idn and r.detail:
            print(f"{'':<20} {'':<22} {'':<20} {r.detail}")


def _summarize(results: list[ProbeResult]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.code.value] = counts.get(r.code.value, 0) + 1
    return ", ".join(f"{n} {code}" for code, n in sorted(counts.items()))


# ── Subcommand implementations ────────────────────────────────────────────────
# Each returns (ok, payload): ok drives the exit code, payload goes to stdout
# (--json) and to the transcript either way.


def _cmd_scan(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    rm = _rm_factory()
    resources = scan_bus(rm)
    payload: dict[str, Any] = {"resources": resources}

    probes: list[ProbeResult] = []
    if args.probe or args.probe_serial:
        for address in resources:
            if address.upper().startswith("ASRL") and not args.probe_serial:
                continue  # unknown-baud serial probing is opt-in
            probes.append(probe_address(rm, address, idn_command=args.idn_command))
        payload["probes"] = [p.as_dict() for p in probes]

    if args.json:
        _print_json(payload)
    else:
        for address in resources:
            print(address)
        if probes:
            print()
            _print_probe_table(probes)
    return True, payload


def _cmd_probe(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    result = probe_address(_rm_factory(), args.address, idn_command=args.idn_command)
    if args.json:
        _print_json(result.as_dict())
    else:
        _print_probe_table([result])
    return result.ok, result.as_dict()


def _cmd_check(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    config = resolve_config(args.config)
    rm = None
    if not args.no_bus:
        try:
            rm = _rm_factory()
        except Exception as exc:  # noqa: BLE001 — degrade to no bus scan
            logger.warning("Bus scan unavailable, checking without it: %s", exc)
    results = check_config(config, rm=rm)
    ok = all(r.ok for r in results)
    payload = {
        "config": config,
        "ok": ok,
        "results": [r.as_dict() for r in results],
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Config: {config}")
        _print_probe_table(results)
        print(f"=> {_summarize(results)}")
    return ok, payload


def _print_l0_bench_table(results: list[L0BenchResult]) -> None:
    for r in results:
        idn_mark = "OK  " if r.idn_ok else "FAIL"
        print(f"{r.alias:<20} idn={idn_mark}  {r.idn or r.detail}")
        if r.getter:
            getter_mark = "OK  " if r.getter_ok else "FAIL"
            print(f"{'':<20} {r.getter}()={getter_mark}  {r.getter_value}")
        elif r.idn_ok:
            print(f"{'':<20} (no extra zero-arg getter found besides get_idn)")


def _cmd_bench_l0(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    """L0 bench: idn + one passive getter for every driver in a config.

    The automated half of the commissioning skill's L0 rung — zero
    excitation, no approval needed. Run after `check` is green; a human
    still has to eyeball whether the returned values are physically
    plausible (this only proves communication and parsing).
    """
    config = resolve_config(args.config)
    results = bench_l0(config)
    ok = all(r.ok for r in results)
    payload = {
        "config": config,
        "ok": ok,
        "results": [r.as_dict() for r in results],
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Config: {config}")
        _print_l0_bench_table(results)
        n_ok = sum(1 for r in results if r.ok)
        print(f"=> {n_ok}/{len(results)} passed L0")
    return ok, payload


def _cmd_methods(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    bench = _make_bench(args)
    try:
        methods = bench.list_methods()
    finally:
        bench.close()
    payload = {"target": args.target, "methods": [m.as_dict() for m in methods]}
    if args.json:
        _print_json(payload)
    else:
        for m in methods:
            marker = "read " if m.read_only else "WRITE"
            print(f"[{marker}] {m.name}{m.signature}  — {m.doc}")
    return True, payload


def _call_and_report(
    args: argparse.Namespace, method: str, call_args: list[str], allow_write: bool
) -> tuple[bool, dict[str, Any]]:
    bench = _make_bench(args)
    try:
        result = bench.call(method, call_args, allow_write=allow_write)
    finally:
        bench.close()
    payload = {
        "target": args.target,
        "method": method,
        "args": call_args,
        "result": result,
    }
    if args.json:
        _print_json(payload)
    else:
        print(repr(result))
    return True, payload


def _cmd_idn(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    return _call_and_report(args, "get_idn", [], allow_write=False)


def _cmd_read(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    if args.repeat <= 1:
        return _call_and_report(args, args.method, args.args, allow_write=False)
    return _cmd_read_repeated(args)


def _cmd_read_repeated(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    """Repeat a read to expose intermittent faults (timing/settling class).

    A read that passes some repeats and fails others — especially when the
    failure rate drops at longer --interval values — is the signature of a
    too-short waiting time rather than a dead instrument. Failures are
    collected, not aborted on, because the failure *pattern* is the datum.
    """
    import time

    bench = _make_bench(args)
    outcomes: list[dict[str, Any]] = []
    failures = 0
    try:
        for i in range(args.repeat):
            if i > 0 and args.interval > 0:
                time.sleep(args.interval)
            try:
                value = bench.call(args.method, args.args, allow_write=False)
                outcomes.append({"i": i, "ok": True, "value": value})
            except Exception as exc:  # noqa: BLE001 — per-iteration capture is the point
                failures += 1
                outcomes.append(
                    {"i": i, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        bench.close()

    ok = failures == 0
    payload = {
        "target": args.target,
        "method": args.method,
        "args": args.args,
        "repeat": args.repeat,
        "interval_s": args.interval,
        "failures": failures,
        "outcomes": outcomes,
    }
    if args.json:
        _print_json(payload)
    else:
        for o in outcomes:
            print(f"[{o['i']:>4}] {'ok   ' if o['ok'] else 'FAIL '} "
                  f"{o.get('value', o.get('error'))!r}")
        print(f"=> {failures}/{args.repeat} failed at interval {args.interval}s")
    return ok, payload


def _cmd_write(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    return _call_and_report(args, args.method, args.args, allow_write=True)


def _cmd_query(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    bench = _make_bench(args)
    try:
        response = bench.query(args.command)
    finally:
        bench.close()
    payload = {"target": args.target, "command": args.command, "response": response}
    if args.json:
        _print_json(payload)
    else:
        print(response)
    return True, payload


def _cmd_send(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    bench = _make_bench(args)
    try:
        bench.send(args.command)
    finally:
        bench.close()
    payload = {"target": args.target, "command": args.command}
    if args.json:
        _print_json(payload)
    else:
        print("sent")
    return True, payload


# ── Parser ────────────────────────────────────────────────────────────────────


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help="config directory path or name (default: the saved active "
        "config, falling back to the shipped sim_cryostat)",
    )


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        help="driver alias from the config's real_drivers — or, together "
        "with --address, a dotted driver class path (driver development)",
    )
    parser.add_argument(
        "--address",
        help="VISA resource string; makes TARGET a dotted class path instead "
        "of a config alias",
    )
    _add_config_arg(parser)


def _cmd_status(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    """Summarize the running app's operational-status log (works while it runs).

    Reads cryosoft/logs/status.jsonl (written by the Orchestrator each tick) and
    reports the current state, per-instrument ramp progress and trend, and any
    watchdog alerts. Exit 0 only when a log exists and its verdict is OK, so an
    agent can gate on the exit code. This is the one troubleshoot command that
    reads the LIVE app rather than opening instruments with the app closed.
    """
    log_path = args.log or (_transcript_dir() / "status.jsonl")
    records = status_reader.read_records(log_path, last=args.last)
    digest = status_reader.summarize(records)
    if args.json:
        _print_json(digest)
    else:
        print(status_reader.render_text(digest))
    ok = bool(digest.get("available")) and digest.get("verdict") == "OK"
    return ok, digest


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse command tree (kept separate for --help testing)."""
    parser = argparse.ArgumentParser(
        prog="python -m cryosoft.troubleshoot",
        description="CryoSoft troubleshoot toolbox: diagnose instruments and "
        "configs while the main application is closed.",
    )
    # A parent parser lets every subcommand accept --json in its natural
    # trailing position (e.g. "check --json"); add_help=False stops it from
    # stealing the subparsers' -h.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p = sub.add_parser("scan", help="list VISA resources on the bus",
                       parents=[common])
    p.add_argument("--probe", action="store_true",
                   help="also identify every non-serial resource")
    p.add_argument("--probe-serial", action="store_true",
                   help="probe serial (ASRL) resources too — opt-in, since "
                        "bytes at a wrong baud rate can confuse instruments")
    p.add_argument("--idn-command", default="*IDN?",
                   help="identify query for probes (default '*IDN?'; use 'V' "
                        "for pre-SCPI Oxford instruments)")
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("probe", parents=[common], help="raw identify query to one bare address")
    p.add_argument("address", help="VISA resource string")
    p.add_argument("--idn-command", default="*IDN?")
    p.set_defaults(func=_cmd_probe)

    p = sub.add_parser("check", parents=[common], help="preflight every driver in a config")
    _add_config_arg(p)
    p.add_argument("--no-bus", action="store_true",
                   help="skip the bus-presence scan (probe by opening only)")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("bench-l0", parents=[common],
                       help="L0 bench for every driver in a config: idn + one "
                            "passive getter (zero excitation, no approval needed)")
    _add_config_arg(p)
    p.set_defaults(func=_cmd_bench_l0)

    p = sub.add_parser("status", parents=[common],
                       help="summarize the RUNNING app's operational-status log")
    p.add_argument("--log", help="path to status.jsonl (default: cryosoft/logs/status.jsonl)")
    p.add_argument("--last", type=int, default=5,
                   help="recent records to fold in for the gap trend (default 5)")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("methods", parents=[common], help="list a driver's public methods")
    _add_target_args(p)
    p.set_defaults(func=_cmd_methods)

    p = sub.add_parser("idn", parents=[common], help="identify one configured instrument")
    _add_target_args(p)
    p.set_defaults(func=_cmd_idn)

    p = sub.add_parser("read", parents=[common], help="call a read-only driver method (get_*)")
    _add_target_args(p)
    p.add_argument("method")
    p.add_argument("args", nargs="*", help="method arguments (coerced by type hints)")
    p.add_argument("--repeat", type=int, default=1,
                   help="repeat the read N times to expose intermittent faults")
    p.add_argument("--interval", type=float, default=0.2,
                   help="seconds between repeats (default 0.2); a failure rate "
                        "that drops at longer intervals points to timing")
    p.set_defaults(func=_cmd_read)

    p = sub.add_parser("write", parents=[common], help="call a state-changing driver method")
    _add_target_args(p)
    p.add_argument("method")
    p.add_argument("args", nargs="*")
    p.set_defaults(func=_cmd_write)

    p = sub.add_parser("query", parents=[common], help="raw command with reply (state may change!)")
    _add_target_args(p)
    p.add_argument("command", help="raw command string, e.g. '*IDN?'")
    p.set_defaults(func=_cmd_query)

    p = sub.add_parser("send", parents=[common], help="raw command, no reply (state may change!)")
    _add_target_args(p)
    p.add_argument("command")
    p.set_defaults(func=_cmd_send)

    return parser


# ── Transcript ────────────────────────────────────────────────────────────────


def _append_transcript(argv: list[str], ok: bool, payload: dict[str, Any]) -> None:
    """Append one JSONL line describing this invocation (best-effort)."""
    try:
        directory = _transcript_dir()
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "argv": argv,
            "ok": ok,
            "payload": payload,
        }
        with (directory / "troubleshoot.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=repr) + "\n")
    except Exception as exc:  # noqa: BLE001 — a broken transcript must not fail the command
        logger.warning("Could not append troubleshoot transcript: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Run one troubleshoot command.

    Args:
        argv: Argument list (defaults to sys.argv[1:]). Exposed so tests call
            the CLI in-process.

    Returns:
        0 if the command fully succeeded, 1 on any fault or error.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    args = build_parser().parse_args(argv)

    try:
        ok, payload = args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — one place turns any failure into exit 1
        message = f"{type(exc).__name__}: {exc}"
        if args.json:
            _print_json({"error": message})
        else:
            print(f"error: {message}", file=sys.stderr)
        logger.error("troubleshoot %s failed: %s", args.subcommand, message)
        _append_transcript(argv, False, {"error": message})
        return 1

    _append_transcript(argv, ok, payload)
    return 0 if ok else 1
