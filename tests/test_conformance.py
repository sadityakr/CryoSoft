# ---
# description: |
#   Auto-discovering conformance tests for the CryoSoft layer interfaces.
#   These tests iterate over the drivers, virtual_instruments, procedures, and
#   configs packages themselves, so any NEW module an agent adds is checked
#   automatically — no test needs to be written for it to be covered.
# last_updated: 2026-07-13
# ---

"""Interface-conformance tests: every driver, VI, procedure, and config obeys its contract.

These tests are the safety net for agentic development. They discover modules at
runtime (via pkgutil) instead of naming them, so adding a new driver, VI,
procedure, or config directory automatically brings it under test. If one of
these tests fails on your new module, fix the module to match the contract —
do not weaken the test.

Contracts enforced here (import boundaries are enforced separately by
import-linter, see pyproject.toml [tool.importlinter]):

* Drivers: one public class per module; ``__init__`` takes exactly one required
  argument (the VISA resource string); sim drivers construct without hardware;
  a real driver and its ``sim_``-twin expose identical public APIs.
* Virtual instruments: subclass BaseVirtualInstrument, set ``vi_type``, and use
  the mandated ``__init__(self, drivers, **init_params)`` signature.
* Measurement methods (MeasurementInstrumentBase subclasses): declare the
  self-describing class attributes (``measurement_parameters`` of ParamSpec,
  ``measurement_data_keys``, valid ``measurement_scalar_columns`` dtypes),
  implement the ``data_arrays`` / ``initiate_measurement`` / ``take_reading`` / ``standby``
  lifecycle, and round-trip against their sim drivers so the returned keys and
  array lengths match what they declare.
* Procedures: subclass BaseProcedure, have a name, declare a default for every
  parameter, and are constructible from defaults alone.
* Configs: every ``cryosoft/configs/<name>/`` directory has a loadable
  devices.yaml + monitor.yaml whose classes import and whose driver references
  resolve.
"""

from __future__ import annotations

import importlib
import inspect
import math
import pkgutil
import typing
from pathlib import Path

import pytest

import cryosoft.drivers
import cryosoft.procedures
import cryosoft.virtual_instruments
from cryosoft.core.availability import (
    AVAILABILITY_STATES,
    AVAILABILITY_TAGS,
    TAG_POLICY,
    TAG_PRECEDENCE,
)
from cryosoft.core.conditions import SEVERITIES
from cryosoft.core.decorators import (
    VALID_CONTROL_SCOPES,
    get_control_panel,
    get_control_scope,
    get_control_specs,
)
from cryosoft.core.exceptions import CryoSoftCommunicationError, CryoSoftSafetyError
from cryosoft.core.operation import OperationBase, ReadinessCondition
from cryosoft.core.plan import ParamSpec
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import Station, _import_class, build_station
from cryosoft.session.servicing_log import DECLARED_LOG_KINDS
from cryosoft.virtual_instruments.base import (
    BaseVirtualInstrument,
    MeasurementInstrumentBase,
)

CONFIGS_DIR = Path(cryosoft.__file__).parent / "configs"

# Modules in virtual_instruments/ that hold base classes, not concrete VIs.
VI_BASE_MODULES = {
    "cryosoft.virtual_instruments.base",
    "cryosoft.virtual_instruments.rampable",
}

# Registry types accepted by Station.register_vi via config (distinct from a VI
# class's own vi_type like "magnet" — see GLOSSARY.md).
CONFIG_VI_TYPES = {"system", "measurement", "level", "switch"}


# ── Discovery helpers ─────────────────────────────────────────────────────────
# pkgutil.iter_modules / walk_packages list the modules inside a package at
# runtime — this is what makes these tests pick up new files automatically.


def _driver_module_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(cryosoft.drivers.__path__))


def _public_classes(module) -> list[type]:
    """Classes defined in *module* itself (not imported into it)."""
    return [
        cls
        for name, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__ and not name.startswith("_")
    ]


def _public_api(cls: type) -> dict[str, inspect.Signature]:
    """Public method name -> signature (self excluded)."""
    api: dict[str, inspect.Signature] = {}
    for name, func in inspect.getmembers(cls, inspect.isfunction):
        if name.startswith("_"):
            continue
        sig = inspect.signature(func)
        params = [p for p in sig.parameters.values() if p.name != "self"]
        api[name] = sig.replace(parameters=params)
    return api


def _all_vi_classes() -> list[type]:
    """Every concrete VI class in cryosoft.virtual_instruments."""
    classes: list[type] = []
    for mod_info in pkgutil.walk_packages(
        cryosoft.virtual_instruments.__path__, prefix="cryosoft.virtual_instruments."
    ):
        if mod_info.name in VI_BASE_MODULES:
            continue
        module = importlib.import_module(mod_info.name)
        for cls in _public_classes(module):
            if issubclass(cls, BaseVirtualInstrument):
                classes.append(cls)
    return classes


def _all_procedure_classes() -> list[type]:
    classes: list[type] = []
    for mod_info in pkgutil.iter_modules(cryosoft.procedures.__path__):
        module = importlib.import_module(f"cryosoft.procedures.{mod_info.name}")
        for cls in _public_classes(module):
            if issubclass(cls, BaseProcedure) and cls is not BaseProcedure:
                classes.append(cls)
    return classes


def _all_operation_classes() -> list[type]:
    """Every concrete OperationBase subclass anywhere under cryosoft.procedures.

    Walks the package tree (not just its top level), so
    ``cryosoft.procedures.operations`` and any future operations subpackage
    are picked up automatically — the discovery scaffold (and every test
    parametrized on it) tolerates an empty result too, for a hypothetical
    setup with no operations module at all.
    """
    classes: list[type] = []
    for mod_info in pkgutil.walk_packages(
        cryosoft.procedures.__path__, prefix="cryosoft.procedures."
    ):
        module = importlib.import_module(mod_info.name)
        for cls in _public_classes(module):
            if issubclass(cls, OperationBase) and cls is not OperationBase:
                classes.append(cls)
    return classes


def _load_yaml(path: Path) -> dict:
    from ruamel.yaml import YAML

    with path.open("r", encoding="utf-8") as f:
        return dict(YAML().load(f))


# ── Driver contract ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("module_name", _driver_module_names())
def test_driver_module_contract(module_name: str) -> None:
    """Every driver module holds exactly one public class taking one required arg."""
    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    classes = _public_classes(module)
    assert len(classes) == 1, (
        f"cryosoft.drivers.{module_name} must define exactly one public class, "
        f"found {[c.__name__ for c in classes]}"
    )
    init_params = [
        p
        for p in inspect.signature(classes[0].__init__).parameters.values()
        if p.name != "self"
    ]
    required = [
        p
        for p in init_params
        if p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert len(required) == 1, (
        f"{classes[0].__name__}.__init__ must take exactly one required argument "
        f"(the VISA resource string), got {[p.name for p in required]}"
    )


@pytest.mark.parametrize("module_name", _driver_module_names())
def test_driver_has_get_idn(module_name: str) -> None:
    """Every driver exposes get_idn() taking no arguments.

    get_idn() is the universal "is the right instrument at this address?"
    probe used by the troubleshoot engine's config preflight. Pre-SCPI
    instruments that do not answer ``*IDN?`` (the Oxford ISOBUS family)
    implement it with their native identify command (``V``) instead.
    """
    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    (cls,) = _public_classes(module)
    method = getattr(cls, "get_idn", None)
    assert callable(method), (
        f"{cls.__name__} lacks get_idn() — every driver must expose an "
        f"identification query under this uniform name"
    )
    required = [
        p
        for p in inspect.signature(method).parameters.values()
        if p.name != "self"
        and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert not required, (
        f"{cls.__name__}.get_idn() must take no required arguments, "
        f"got {[p.name for p in required]}"
    )


@pytest.mark.parametrize("module_name", _driver_module_names())
def test_driver_has_close(module_name: str) -> None:
    """Every driver exposes close() taking no arguments.

    The driver half of the connection-lifecycle standard (see
    ``BaseVirtualInstrument`` and ``drivers/README.md``): ``close()`` releases
    the bus session so the operator can drive the instrument from its own
    front panel or vendor software. ``Station.disconnect_instrument()`` calls
    it on every driver the disconnecting VI exclusively owns, so a driver
    without one would silently leak a session that keeps the instrument
    locked to CryoSoft.
    """
    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    (cls,) = _public_classes(module)
    method = getattr(cls, "close", None)
    assert callable(method), (
        f"{cls.__name__} lacks close() — every driver must be able to "
        f"release its session (the connection-lifecycle standard)"
    )
    required = [
        p
        for p in inspect.signature(method).parameters.values()
        if p.name != "self"
        and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert not required, (
        f"{cls.__name__}.close() must take no required arguments, "
        f"got {[p.name for p in required]}"
    )


@pytest.mark.parametrize(
    "module_name",
    [m for m in _driver_module_names() if m.startswith("sim_")],
)
def test_sim_driver_constructs_without_hardware(module_name: str) -> None:
    """Sim drivers must be constructible with a dummy resource string."""
    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    (cls,) = _public_classes(module)
    cls("SIM::CONFORMANCE")


@pytest.mark.parametrize(
    "module_name",
    [m for m in _driver_module_names() if m.startswith("sim_")],
)
def test_sim_driver_is_dead_after_close(module_name: str) -> None:
    """A closed sim driver fails every command, starting with get_idn().

    The sim half of the connection-lifecycle standard's ``close()`` contract:
    a released session is really gone. Sims model it (rather than no-opping)
    for the same reason they model every other failure mode — so a
    use-after-disconnect bug fails here instead of on hardware. ``close()``
    itself must stay idempotent and silent: a disconnect always succeeds.
    """
    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    (cls,) = _public_classes(module)
    driver = cls("SIM::CONFORMANCE")
    driver.get_idn()  # reachable before the close
    driver.close()
    driver.close()  # idempotent — must not raise
    with pytest.raises(CryoSoftCommunicationError):
        driver.get_idn()


@pytest.mark.parametrize(
    "real_name",
    [
        m
        for m in _driver_module_names()
        if not m.startswith("sim_") and f"sim_{m}" in _driver_module_names()
    ],
)
def test_sim_real_driver_api_parity(real_name: str) -> None:
    """A real driver and its sim_ twin must expose identical public APIs.

    This is the contract that lets a config swap sim for real hardware without
    touching any VI or procedure: code written against the sim works on the
    real instrument. Method names AND signatures must match exactly.
    """
    real_mod = importlib.import_module(f"cryosoft.drivers.{real_name}")
    sim_mod = importlib.import_module(f"cryosoft.drivers.sim_{real_name}")
    (real_cls,) = _public_classes(real_mod)
    (sim_cls,) = _public_classes(sim_mod)

    real_api = _public_api(real_cls)
    sim_api = _public_api(sim_cls)

    assert real_api.keys() == sim_api.keys(), (
        f"Public API mismatch between {real_cls.__name__} and {sim_cls.__name__}: "
        f"real-only={sorted(real_api.keys() - sim_api.keys())}, "
        f"sim-only={sorted(sim_api.keys() - real_api.keys())}"
    )
    for method, real_sig in real_api.items():
        sim_params = list(sim_api[method].parameters)
        real_params = list(real_sig.parameters)
        assert real_params == sim_params, (
            f"Signature mismatch on {method}(): "
            f"{real_cls.__name__}{real_sig} vs {sim_cls.__name__}{sim_api[method]}"
        )


# ── Virtual-instrument contract ───────────────────────────────────────────────


@pytest.mark.parametrize("vi_cls", _all_vi_classes(), ids=lambda c: c.__name__)
def test_vi_contract(vi_cls: type) -> None:
    """Every concrete VI sets vi_type and uses __init__(self, drivers, **init_params)."""
    assert vi_cls.vi_type != "unknown", (
        f"{vi_cls.__name__} must set vi_type (inherit from a typed base such as "
        f"MagnetBase / TemperatureControllerBase, or set the class attribute)"
    )

    params = list(inspect.signature(vi_cls.__init__).parameters.values())
    assert params[0].name == "self"
    assert len(params) >= 2 and params[1].name == "drivers", (
        f"{vi_cls.__name__}.__init__ first argument must be 'drivers' "
        f"(the Station injects driver instances there)"
    )
    kinds = [p.kind for p in params]
    assert inspect.Parameter.VAR_KEYWORD in kinds, (
        f"{vi_cls.__name__}.__init__ must accept **init_params "
        f"(config init_params are passed as keyword arguments)"
    )
    extra_required = [
        p
        for p in params[2:]
        if p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert not extra_required, (
        f"{vi_cls.__name__}.__init__ has required arguments beyond 'drivers': "
        f"{[p.name for p in extra_required]} — give them defaults and read them "
        f"from **init_params instead"
    )


# ── Connection-lifecycle standard ─────────────────────────────────────────────
# See BaseVirtualInstrument's "Connection-lifecycle standard" docstring:
# connect/disconnect own the bus session, initiate/standby own the instrument's
# state, and building the Station sends nothing but an identity query. These
# tests make rule 1 ("construction is silent") binding for every present and
# future VI, which is the rule a new driver or VI is most likely to break by
# accident — a single convenience command in __init__ is easy to write and
# invisible until it clobbers a running experiment at app startup.

# Commands a VI's __init__ MAY still issue on its drivers. `close` is a
# RELEASE, not a state change: an externally configured VI is born detached so
# that starting CryoSoft while the vendor tool holds the instrument works at
# all (see MeasurementInstrumentBase's "Externally configured instruments").
_SILENT_CONSTRUCTION_ALLOWED = frozenset({"close"})


def _vi_specs_from_configs() -> list[tuple[str, str, type, dict]]:
    """Return ``(config, vi_name, vi_class, init_params)`` for every configured VI.

    Drawn from the shipped configs rather than a hand-written list, so a VI
    added to any setup is covered the moment its config entry exists — and
    with the REAL init_params it will be built with, which is what decides
    whether a constructor takes a command path or not.
    """
    specs: list[tuple[str, str, type, dict]] = []
    # CONFIGS_DIR directly rather than _config_dirs(), which is defined
    # further down with the config-contract tests; this parametrize runs at
    # import time.
    for config_dir in sorted(p for p in CONFIGS_DIR.iterdir() if p.is_dir()):
        devices = _load_yaml(config_dir / "devices.yaml")
        for vi_name, vi_cfg in (devices.get("virtual_instruments") or {}).items():
            specs.append(
                (
                    config_dir.name,
                    vi_name,
                    _import_class(vi_cfg["class"]),
                    dict(vi_cfg.get("init_params") or {}),
                )
            )
    return specs


@pytest.mark.parametrize(
    "config_name, vi_name, vi_cls, init_params",
    _vi_specs_from_configs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_vi_construction_sends_no_commands(
    config_name: str, vi_name: str, vi_cls: type, init_params: dict
) -> None:
    """Building a VI must not command its instruments (connection-lifecycle rule 1).

    Constructs the VI with recording stand-ins for its drivers and asserts
    nothing was called on them. Building the Station is a *connection* act:
    the only command it sends is the identity query, so an operator who
    starts CryoSoft mid-experiment — or while the instrument is on its own
    front panel — finds every instrument exactly as they left it. A setup
    command that wants to run at bring-up belongs in ``initiate()``.

    ``MagicMock`` stands in for each driver: it accepts any call and any
    attribute, so this exercises the constructor's real path (config
    validation included) while recording every command it would have sent.
    """
    from unittest.mock import MagicMock

    drivers: dict[str, MagicMock] = {}

    class _RecordingDrivers(dict):
        """A driver dict that mints a recording driver for any role asked for."""

        def __missing__(self, role: str) -> MagicMock:
            driver = MagicMock(name=f"driver:{role}")
            drivers[role] = driver
            self[role] = driver
            return driver

    vi_cls(_RecordingDrivers(), **init_params)

    offenders: list[str] = []
    for role, driver in drivers.items():
        for call in driver.mock_calls:
            # call[0] is the dotted name: "" for the driver itself being
            # called, "set_rate" for a method, "adapter.write" for nesting.
            name = call[0]
            if not name or name.split(".")[0] in _SILENT_CONSTRUCTION_ALLOWED:
                continue
            offenders.append(f"{role}.{name}")

    assert not offenders, (
        f"{config_name}/{vi_name} ({vi_cls.__name__}).__init__ commanded its "
        f"instrument(s): {sorted(set(offenders))}. Building the Station must "
        f"send nothing but the identity query (the connection-lifecycle "
        f"standard, see BaseVirtualInstrument) — move these to initiate()."
    )


@pytest.mark.parametrize("vi_cls", _all_vi_classes(), ids=lambda c: c.__name__)
def test_vi_has_connection_lifecycle_hooks(vi_cls: type) -> None:
    """Every VI answers ping() and disconnect() with no required arguments.

    Both are inherited from ``BaseVirtualInstrument``, so this only ever
    fails on a VI that overrode one with a different shape — which would
    break ``build_station()``'s identity check or
    ``Station.disconnect_instrument()`` for that instrument alone, silently,
    on whichever setup happens to configure it. ``is_attached()`` — the
    detach-when-idle declaration's observable state (see the class
    docstring) — is part of the same hook set.
    """
    for name in ("ping", "disconnect", "initiate", "standby", "is_attached"):
        method = getattr(vi_cls, name, None)
        assert callable(method), f"{vi_cls.__name__} lacks {name}()"
        required = [
            p
            for p in inspect.signature(method).parameters.values()
            if p.name != "self"
            and p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        assert not required, (
            f"{vi_cls.__name__}.{name}() must take no required arguments, "
            f"got {[p.name for p in required]}"
        )


# ── Availability standard (cryosoft.core.availability) ───────────────────────
# See availability.py's module docstring: a closed tag vocabulary, a derived
# state vocabulary, and a declared tag -> policy table (TAG_POLICY) are the
# one place the "why can't I use this instrument?" policies are stated. These
# tests make the table's own internal consistency binding, the moment a tag
# or a policy row is added or edited — a fixed module, not one pkgutil
# auto-discovers, but the checked-in harness (`make check`) is where a future
# edit meets them regardless of whether the author also knows about
# tests/test_availability.py's pure-policy coverage of the same invariants.


def test_tag_policy_covers_exactly_the_availability_tags() -> None:
    """TAG_POLICY has one row per tag, no orphan row, no undeclared tag."""
    assert set(TAG_POLICY) == set(AVAILABILITY_TAGS), (
        f"TAG_POLICY keys {sorted(TAG_POLICY)} must exactly match "
        f"AVAILABILITY_TAGS {sorted(AVAILABILITY_TAGS)} — no orphan row, no "
        f"undeclared tag"
    )
    for tag, policy in TAG_POLICY.items():
        assert policy.tag == tag, (
            f"TAG_POLICY[{tag!r}].tag == {policy.tag!r} — a TagPolicy's own "
            f"'tag' field must match the key it is filed under"
        )


def test_tag_policy_states_and_precedence_stay_in_vocabulary() -> None:
    """Every TagPolicy.state is declared; TAG_PRECEDENCE is a permutation of the tags."""
    for tag, policy in TAG_POLICY.items():
        assert policy.state in AVAILABILITY_STATES, (
            f"TAG_POLICY[{tag!r}].state {policy.state!r} is not one of "
            f"AVAILABILITY_STATES {AVAILABILITY_STATES}"
        )
    assert sorted(TAG_PRECEDENCE) == sorted(AVAILABILITY_TAGS), (
        f"TAG_PRECEDENCE {TAG_PRECEDENCE} must be a permutation of "
        f"AVAILABILITY_TAGS {AVAILABILITY_TAGS}"
    )
    assert len(TAG_PRECEDENCE) == len(set(TAG_PRECEDENCE)), (
        f"TAG_PRECEDENCE {TAG_PRECEDENCE} names a tag more than once"
    )


# ── Detach-when-idle standard (BaseVirtualInstrument.detach_when_idle) ───────
# See BaseVirtualInstrument's "Detach-when-idle declaration": a VI opts in by
# overriding the detach_when_idle property; __init_subclass__'s wrap (or the
# base's own standby()) then releases the driver session automatically. These
# tests make the opt-in binding for every present and future VI that declares
# it — currently only TensormeterRTM2MeasurementVI (12t-cryo's
# tensormeter_measurement, configured_externally: true).


def _sim_driver_class_for(real_dotted: str) -> type:
    """Return the sim twin class for a driver's dotted class path.

    A config may already reference a ``sim_*`` module directly (e.g.
    ``sim_cryostat``) — imported as-is. Otherwise uses the same
    ``sim_<module>`` naming convention as ``test_sim_real_driver_api_parity``
    — the one existing derivation from a real driver's dotted path to its
    sim twin.
    """
    module_path, _, _ = real_dotted.rpartition(".")
    package, _, module_name = module_path.rpartition(".")
    sim_module_name = module_name if module_name.startswith("sim_") else f"sim_{module_name}"
    sim_module = importlib.import_module(f"{package}.{sim_module_name}")
    (sim_cls,) = _public_classes(sim_module)
    return sim_cls


def _detach_when_idle_vi_specs() -> list[tuple[str, type, dict, dict[str, type]]]:
    """(spec id, vi_cls, init_params, {role: sim driver class}) for every
    configured VI whose REAL config build declares ``detach_when_idle``.

    Drawn from the shipped configs' real ``drivers`` role mapping and real
    ``init_params`` — e.g. 12t-cryo's ``tensormeter_measurement``,
    ``configured_externally: true`` — with each real driver class swapped
    for its sim twin so the checks run with no hardware. Filtering is done
    by actually constructing the VI and reading ``detach_when_idle``, so a
    future config declaring it is covered automatically, without this file
    needing to know which VI or config that will be.
    """
    specs: list[tuple[str, type, dict, dict[str, type]]] = []
    for config_dir in sorted(p for p in CONFIGS_DIR.iterdir() if p.is_dir()):
        devices = _load_yaml(config_dir / "devices.yaml")
        real_drivers_cfg = devices.get("real_drivers") or {}
        for vi_name, vi_cfg in (devices.get("virtual_instruments") or {}).items():
            vi_cls = _import_class(vi_cfg["class"])
            if not issubclass(vi_cls, MeasurementInstrumentBase):
                continue
            init_params = dict(vi_cfg.get("init_params") or {})
            role_map = vi_cfg.get("drivers") or {}
            sim_driver_classes = {
                role: _sim_driver_class_for(real_drivers_cfg[driver_key]["class"])
                for role, driver_key in role_map.items()
            }
            probe_drivers = {
                role: cls("SIM::CONFORMANCE") for role, cls in sim_driver_classes.items()
            }
            vi = vi_cls(probe_drivers, **init_params)
            if vi.detach_when_idle:
                specs.append(
                    (f"{config_dir.name}/{vi_name}", vi_cls, init_params, sim_driver_classes)
                )
    return specs


@pytest.mark.parametrize(
    "spec_id, vi_cls, init_params, sim_driver_classes",
    _detach_when_idle_vi_specs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_detach_when_idle_vi_drivers_support_ensure_connected(
    spec_id: str, vi_cls: type, init_params: dict, sim_driver_classes: dict[str, type]
) -> None:
    """A VI declaring detach_when_idle must have reconnect-capable drivers.

    ``BaseVirtualInstrument._attach()`` duck-types ``ensure_connected()``
    (deliberately not part of the driver contract — see
    ``drivers/README.md``): a VI that declares ``detach_when_idle`` without
    a driver implementing it would silently never reacquire its session.
    """
    for role, cls in sim_driver_classes.items():
        driver = cls("SIM::CONFORMANCE")
        assert callable(getattr(driver, "ensure_connected", None)), (
            f"{spec_id} ({vi_cls.__name__}) declares detach_when_idle but "
            f"its {role!r} driver ({cls.__name__}) has no ensure_connected() "
            f"— the detach-when-idle standard's opt-in reconnect capability"
        )


@pytest.mark.parametrize(
    "spec_id, vi_cls, init_params, sim_driver_classes",
    _detach_when_idle_vi_specs(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_detach_when_idle_vi_really_releases_and_reacquires(
    spec_id: str, vi_cls: type, init_params: dict, sim_driver_classes: dict[str, type]
) -> None:
    """standby() really releases the session; arming really reacquires it.

    Built against the sim driver — proves the declaration is framework
    behaviour that actually moves ``is_attached()``, not just a flag that
    reads true without anything happening underneath.
    """
    drivers = {role: cls("SIM::CONFORMANCE") for role, cls in sim_driver_classes.items()}
    vi = vi_cls(drivers, **init_params)
    defaults = {name: spec.default for name, spec in vi_cls.measurement_parameters.items()}

    vi.initiate_measurement(**defaults)
    assert vi.is_attached() is True, (
        f"{spec_id} ({vi_cls.__name__}).initiate_measurement() must "
        f"reacquire the session for a detach_when_idle VI"
    )

    vi.standby()
    assert vi.is_attached() is False, (
        f"{spec_id} ({vi_cls.__name__}).standby() must release the session "
        f"for a detach_when_idle VI"
    )


def test_detach_when_idle_vi_owns_its_driver_aliases_exclusively() -> None:
    """A detach_when_idle VI must not share a config driver alias with any other VI.

    ``BaseVirtualInstrument._detach()`` releases every driver in
    ``self._drivers`` unconditionally (see the "Detach-when-idle
    declaration" docstring) — it has no notion of another VI still needing
    that same session, because a VI may never consult the Station's alias
    map (Layer 1 cannot import Layer 2). ``Station.disconnect_instrument()``
    avoids exactly this hazard for its own release path by routing through
    ``_exclusive_aliases()`` before closing anything; this test applies the
    SAME predicate to every shipped config, for every VI this file's
    ``_detach_when_idle_vi_specs()`` found to declare ``detach_when_idle``,
    so a future config that lets two VIs share an alias with one of them
    detach_when_idle fails CI instead of silently breaking the other VI's
    session in the field.
    """
    from cryosoft.core.station import _exclusive_aliases

    detach_when_idle_spec_ids = {spec_id for spec_id, *_ in _detach_when_idle_vi_specs()}

    for config_dir in sorted(p for p in CONFIGS_DIR.iterdir() if p.is_dir()):
        devices = _load_yaml(config_dir / "devices.yaml")
        vi_cfgs = devices.get("virtual_instruments") or {}
        for vi_name, vi_cfg in vi_cfgs.items():
            spec_id = f"{config_dir.name}/{vi_name}"
            if spec_id not in detach_when_idle_spec_ids:
                continue
            role_aliases = vi_cfg.get("drivers") or {}
            mine = set(role_aliases.values())
            exclusive = set(_exclusive_aliases(role_aliases, vi_cfgs, vi_name))
            shared = mine - exclusive
            assert not shared, (
                f"{spec_id}: declares detach_when_idle but driver alias/es "
                f"{sorted(shared)} are also named by another VI in "
                f"{config_dir.name}/devices.yaml — _detach() would close "
                f"them unconditionally on standby(), breaking whichever "
                f"other VI still needs the shared session; a "
                f"detach_when_idle VI must own its driver aliases "
                f"exclusively (see BaseVirtualInstrument's "
                f"'Detach-when-idle declaration')"
            )


# ── Safety-flag manifest standard ─────────────────────────────────────────────
# See BaseVirtualInstrument's "Safety-flag manifest standard" docstring: every
# flag a VI's evaluate_safety() can report is declared, once, in the class
# attribute `safety_flags` (flag name -> severity), merged over the MRO by
# `merged_safety_flags()` exactly like `control_limits`. `safety_concerns()`
# is the consumer side and may only name hold-severity flags. These tests make
# the standard binding for every present and future VI.

# Valid severities for a safety_flags manifest entry — the System-Condition
# standard's severity ladder (scope follows from severity: "hold" is scoped
# to concerned VIs, "critical" is station-wide by definition, "advisory" is
# reserved with no enforcement yet), canonically declared in
# cryosoft.core.conditions.
SAFETY_FLAG_SEVERITIES = SEVERITIES


def _all_manifest_severities() -> dict[str, str]:
    """{flag: severity} unioned across every discovered VI class's merged manifest.

    The global producer-side picture: a consumer VI's ``safety_concerns()``
    may name a flag reported by ANY VI, not just its own, so validating a
    concern requires looking here rather than at one class's own manifest.
    """
    severities: dict[str, str] = {}
    for cls in _all_vi_classes():
        severities.update(cls.merged_safety_flags())
    return severities


@pytest.mark.parametrize("vi_cls", _all_vi_classes(), ids=lambda c: c.__name__)
def test_safety_flags_manifest_is_valid(vi_cls: type) -> None:
    """Every safety_flags entry has a valid severity; no MRO contradictions.

    Every key in the merged-MRO manifest is a non-empty str and every value
    is one of SAFETY_FLAG_SEVERITIES. Separately, walking the MRO directly
    (rather than through the already-merged dict) catches a subclass that
    redeclares an inherited flag with a DIFFERENT severity than a base
    already gave it — the one thing merged_safety_flags() silently allows
    (last-declared-wins) because the "no contradiction" invariant is a
    conformance concern, not a runtime one (see the class docstring).
    """
    merged = vi_cls.merged_safety_flags()
    for flag, severity in merged.items():
        assert isinstance(flag, str) and flag, (
            f"{vi_cls.__name__}.safety_flags key {flag!r} must be a "
            f"non-empty str"
        )
        assert severity in SAFETY_FLAG_SEVERITIES, (
            f"{vi_cls.__name__}.safety_flags[{flag!r}] = {severity!r} is not "
            f"one of {SAFETY_FLAG_SEVERITIES}"
        )

    declared: dict[str, str] = {}
    for klass in reversed(vi_cls.__mro__):
        own = vars(klass).get("safety_flags") or {}
        for flag, severity in own.items():
            assert flag not in declared or declared[flag] == severity, (
                f"{vi_cls.__name__}: {klass.__name__}.safety_flags "
                f"redeclares {flag!r} as {severity!r}, contradicting the "
                f"inherited severity {declared.get(flag)!r}"
            )
            declared[flag] = severity


@pytest.mark.parametrize("vi_cls", _all_vi_classes(), ids=lambda c: c.__name__)
def test_safety_concerns_are_consumer_side_and_hold_only(vi_cls: type) -> None:
    """Every safety_concerns() flag is hold-severity and actually producible.

    The consumer-side half of the safety-flag manifest standard: a VI's
    safety_concerns() may only name flags that (a) appear in SOME
    discovered VI's merged safety_flags manifest, and (b) have severity
    "hold" there. Naming a critical flag here would be meaningless — a
    tripped critical flag already forces station-wide EMERGENCY (the
    System-Condition standard, ``core/conditions.py``: critical scope is
    station-wide by construction), which stops this VI (and every other)
    regardless of any per-VI concern declaration. MagnetBase is the one
    concrete example today: it names "helium_low" (hold) but not "quench"
    (critical), even though it is the VI that reports "quench".

    A bare (``__init__``-less) instance is enough: every existing
    ``safety_concerns()`` override is a pure function of the class, never
    of instance state populated by ``__init__`` (see
    ``tests/test_l0_keithley_6221_error_queue.py`` for the same
    ``object.__new__`` idiom used to probe a class without constructing it).
    """
    severities = _all_manifest_severities()
    concerns = object.__new__(vi_cls).safety_concerns()
    for flag in concerns:
        assert flag in severities, (
            f"{vi_cls.__name__}.safety_concerns() names {flag!r}, which no "
            f"discovered VI's safety_flags manifest produces"
        )
        assert severities[flag] == "hold", (
            f"{vi_cls.__name__}.safety_concerns() names {flag!r}, whose "
            f"manifest severity is {severities[flag]!r}, not 'hold'"
        )


def test_no_dead_hold_flags() -> None:
    """Every hold-severity flag some VI can produce has at least one consumer.

    A hold-severity flag with no VI declaring it in safety_concerns() would
    trip a per-VI hold that holds nothing — the producer side of the
    safety-flag manifest standard would be dead weight. Checked once,
    globally, rather than per-VI, since "has a consumer" is a property of
    the whole station's declarations, not of any one class.
    """
    severities = _all_manifest_severities()
    hold_flags = {flag for flag, severity in severities.items() if severity == "hold"}
    consumed: set[str] = set()
    for cls in _all_vi_classes():
        consumed |= object.__new__(cls).safety_concerns()
    dead = hold_flags - consumed
    assert not dead, f"hold-severity flag(s) with no consumer: {sorted(dead)}"


# ── Procedure contract ────────────────────────────────────────────────────────


@pytest.mark.parametrize("proc_cls", _all_procedure_classes(), ids=lambda c: c.__name__)
def test_procedure_declaration(proc_cls: type) -> None:
    """Every procedure names itself and declares each parameter as a ParamSpec."""
    assert proc_cls.name, f"{proc_cls.__name__} must set the 'name' class attribute"
    assert proc_cls.parameters, f"{proc_cls.__name__} declares no parameters"
    for param_name, spec in proc_cls.parameters.items():
        # Parameters are ParamSpec now (Wave 4). The old "spec has 'type' and
        # 'default'" checks moved INTO the type: ParamSpec.__post_init__ requires
        # both (and validates choices / bounds) at construction, so a value being
        # a ParamSpec instance is exactly that guarantee — and every parameter
        # still carries a default, so the procedure runs unattended.
        assert isinstance(spec, ParamSpec), (
            f"{proc_cls.__name__}.{param_name} must be a ParamSpec, got {spec!r}"
        )
    valid_x_keys = (
        ["unix_time"] + list(proc_cls.sweep_data_keys) + list(proc_cls.measurement_data_keys)
    )
    assert proc_cls.default_x_key in valid_x_keys, (
        f"{proc_cls.__name__}.default_x_key={proc_cls.default_x_key!r} is not one "
        f"of its data keys {valid_x_keys}"
    )


@pytest.mark.parametrize("proc_cls", _all_procedure_classes(), ids=lambda c: c.__name__)
def test_procedure_parameter_has_description(proc_cls: type) -> None:
    """Every procedure parameter declares a non-empty 'description'.

    The GUI parameter form (ProcedureWindow._build_param_form) now labels
    each field with the bare parameter name and moves the human-readable
    explanation into a hover tooltip. A parameter without a description would
    render a tooltip missing its most important sentence, so every parameter
    in sweep_parameters / system_parameters / measurement_parameters must
    carry one. 'unit' stays optional — dimensionless counts legitimately have
    none.
    """
    for group_name in ("sweep_parameters", "system_parameters", "measurement_parameters"):
        group_params = getattr(proc_cls, group_name)
        for param_name, spec in group_params.items():
            # ParamSpec allows an empty description; a *non-empty* one is a
            # procedure-level rule ParamSpec does NOT enforce, so it stays tested.
            description = spec.description
            assert isinstance(description, str) and description.strip(), (
                f"{proc_cls.__name__}.{group_name}['{param_name}'] lacks a "
                f"non-empty 'description' — add a one-sentence physics-appropriate "
                f"description, it is shown as the GUI tooltip"
            )


@pytest.mark.parametrize("proc_cls", _all_procedure_classes(), ids=lambda c: c.__name__)
def test_procedure_choices_spec(proc_cls: type) -> None:
    """Enumerated ('choices') parameters follow the label->value dict standard.

    A parameter that declares 'choices' renders as a GUI drop-down and its
    collected value is the *mapped* value (see BaseProcedure docstring and
    cryosoft.gui.param_form.build_param_widget). The three invariants this test
    used to assert one by one — choices is a non-empty label->value dict, every
    value is an instance of the declared 'type', and 'default' is one of the
    mapped values — have moved INTO the type: ParamSpec.__post_init__ enforces
    all of them at construction. So the class simply importing (which
    _all_procedure_classes already did) proves them. Here we only re-affirm that
    a choices-declaring parameter is a ParamSpec carrying a non-empty choices
    dict; the deeper enforcement is exercised by the ParamSpec unit tests.
    """
    for param_name, spec in proc_cls.parameters.items():
        if not (isinstance(spec, ParamSpec) and spec.choices):
            continue
        ctx = f"{proc_cls.__name__}.{param_name}"
        assert isinstance(spec.choices, dict) and spec.choices, (
            f"{ctx} 'choices' must be a non-empty label->value dict"
        )


@pytest.mark.parametrize("proc_cls", _all_procedure_classes(), ids=lambda c: c.__name__)
def test_procedure_constructs_from_defaults(proc_cls: type, tmp_path) -> None:
    """Every procedure must construct with zero explicit parameters.

    BaseProcedure merges declared defaults into the params dict, so a complete
    default set means agents and scripts can always instantiate a procedure
    without reproducing the GUI's parameter form.

    A generic sweep procedure (``requires_measurement_vi``) resolves its
    measurement VI and that VI's parameters from the station, so it cannot be
    built from an empty ``Station``; it is handed a populated sim station
    instead. This does not weaken the check for static procedures — they still
    build from an empty station — it only supplies the one thing a
    station-dependent procedure legitimately needs.
    """
    if getattr(proc_cls, "requires_measurement_vi", False):
        station = build_station("cryosoft/configs/sim_cryostat")
    else:
        station = Station()
    proc = proc_cls(
        station=station,
        sample_info={"sample_name": "conformance", "sample_id": "T0", "comments": ""},
        data_directory=str(tmp_path),
    )
    assert proc.get_sweep_array(), (
        f"{proc_cls.__name__} built an empty sweep array from its own defaults"
    )


@pytest.mark.parametrize("proc_cls", _all_procedure_classes(), ids=lambda c: c.__name__)
def test_procedure_claimed_vi_names_contract(proc_cls: type, tmp_path) -> None:
    """claimed_vi_names() returns None or a set of VI names known to the station.

    Concurrency-scope hook: ``None`` (claim everything) is always valid; a
    non-``None``
    return must be a ``set[str]`` naming VIs the station actually has, so a
    typo in a narrowed claim can never silently under-claim.
    """
    if getattr(proc_cls, "requires_measurement_vi", False):
        station = build_station("cryosoft/configs/sim_cryostat")
    else:
        station = Station()
    proc = proc_cls(
        station=station,
        sample_info={"sample_name": "conformance", "sample_id": "T0", "comments": ""},
        data_directory=str(tmp_path),
    )
    claimed = proc.claimed_vi_names()
    if claimed is None:
        return
    assert isinstance(claimed, set) and all(isinstance(name, str) for name in claimed), (
        f"{proc_cls.__name__}.claimed_vi_names() must return None or a set[str], "
        f"got {claimed!r}"
    )
    known = set(station.get_vi_names())
    unknown = claimed - known
    assert not unknown, (
        f"{proc_cls.__name__}.claimed_vi_names() names VI(s) not on the station: "
        f"{sorted(unknown)}"
    )


# ── Config contract ───────────────────────────────────────────────────────────


def _config_dirs() -> list[Path]:
    return sorted(p for p in CONFIGS_DIR.iterdir() if p.is_dir())


@pytest.mark.parametrize("config_dir", _config_dirs(), ids=lambda p: p.name)
def test_config_schema(config_dir: Path) -> None:
    """devices.yaml and monitor.yaml exist, load, and reference real classes."""
    devices_file = config_dir / "devices.yaml"
    monitor_file = config_dir / "monitor.yaml"
    assert devices_file.exists(), f"{config_dir.name} lacks devices.yaml"
    assert monitor_file.exists(), f"{config_dir.name} lacks monitor.yaml"

    devices = _load_yaml(devices_file)
    monitor = _load_yaml(monitor_file)

    assert "monitor" in monitor and "tick_interval_ms" in monitor["monitor"], (
        f"{config_dir.name}/monitor.yaml must define monitor.tick_interval_ms"
    )

    driver_names = set(devices.get("real_drivers", {}).keys())
    assert driver_names, f"{config_dir.name}/devices.yaml declares no real_drivers"

    for drv_name, drv_cfg in devices["real_drivers"].items():
        assert "class" in drv_cfg, f"driver '{drv_name}' lacks a 'class' entry"
        assert "address" in drv_cfg, f"driver '{drv_name}' lacks an 'address' entry"
        _import_class(drv_cfg["class"])  # raises CryoSoftConfigError if broken

    for vi_name, vi_cfg in devices.get("virtual_instruments", {}).items():
        assert "class" in vi_cfg, f"VI '{vi_name}' lacks a 'class' entry"
        vi_cls = _import_class(vi_cfg["class"])
        assert issubclass(vi_cls, BaseVirtualInstrument), (
            f"VI '{vi_name}' class {vi_cfg['class']} is not a BaseVirtualInstrument"
        )
        assert vi_cfg.get("vi_type") in CONFIG_VI_TYPES, (
            f"VI '{vi_name}' vi_type={vi_cfg.get('vi_type')!r} must be one of "
            f"{sorted(CONFIG_VI_TYPES)}"
        )
        for role, drv_ref in vi_cfg.get("drivers", {}).items():
            assert drv_ref in driver_names, (
                f"VI '{vi_name}' role '{role}' references unknown driver '{drv_ref}'"
            )


@pytest.mark.parametrize("config_dir", _config_dirs(), ids=lambda p: p.name)
def test_panels_config_names_real_vis_and_controls(config_dir: Path) -> None:
    """Every monitor.yaml panels: entry names a declared VI and its @control methods.

    A typo'd VI or control name would otherwise fail silently — the card
    would just render without the control the operator expected.
    """
    from cryosoft.core.station import read_panels_config

    panels = read_panels_config(str(config_dir))
    if not panels:
        return
    devices = _load_yaml(config_dir / "devices.yaml")
    declared_vis = devices.get("virtual_instruments", {})
    for vi_name, controls in panels.items():
        assert vi_name in declared_vis, (
            f"{config_dir.name}/monitor.yaml panels: names VI '{vi_name}', "
            f"which devices.yaml does not declare"
        )
        vi_cls = _import_class(declared_vis[vi_name]["class"])
        control_names = set(_control_methods(vi_cls))
        for control_name in controls:
            assert control_name in control_names, (
                f"{config_dir.name}/monitor.yaml panels: {vi_name} lists "
                f"'{control_name}', which is not a @control method on "
                f"{vi_cls.__name__}"
            )


# ── Cryogenics config block ───────────────────────────────────────────────────
# An optional cryogenics: block plus
# a servicing_logs: list. A config that declares neither carries zero
# footprint (the feature stays off); a config that declares cryogenics: must
# reference a real vi_type: level VI with sane, ordered bounds.


@pytest.mark.parametrize("config_dir", _config_dirs(), ids=lambda p: p.name)
def test_cryogenics_config_block(config_dir: Path) -> None:
    """A declared cryogenics: block names a real level VI with sane bounds."""
    devices = _load_yaml(config_dir / "devices.yaml")
    cryo = devices.get("cryogenics")
    if cryo is None:
        pytest.skip(f"{config_dir.name} declares no cryogenics: block")
    assert isinstance(cryo, dict), f"{config_dir.name}: cryogenics: must be a mapping"

    level_vi_name = cryo.get("level_vi")
    virtual_instruments = devices.get("virtual_instruments", {})
    vi_cfg = virtual_instruments.get(level_vi_name)
    assert vi_cfg is not None, (
        f"{config_dir.name}: cryogenics.level_vi={level_vi_name!r} does not "
        f"name a registered VI"
    )
    assert vi_cfg.get("vi_type") == "level", (
        f"{config_dir.name}: cryogenics.level_vi={level_vi_name!r} must be a "
        f"vi_type: level VI, got {vi_cfg.get('vi_type')!r}"
    )

    helium_low_threshold = float(
        (vi_cfg.get("init_params") or {}).get("helium_low_threshold", 0.0)
    )
    warning_pct = float(cryo.get("helium_warning_pct", 0.0))
    assert warning_pct > helium_low_threshold, (
        f"{config_dir.name}: cryogenics.helium_warning_pct ({warning_pct}) "
        f"must exceed the level VI's helium_low_threshold "
        f"({helium_low_threshold})"
    )

    positive_keys = (
        "helium_warning_pct",
        "fill_target_pct",
        "fill_zero_field_window_s",
        "fill_complete_window_s",
        "max_fill_duration_s",
        "sample_period_s",
        "history_sample_s",
    )
    for key in positive_keys:
        if key not in cryo:
            continue
        assert float(cryo[key]) > 0, (
            f"{config_dir.name}: cryogenics.{key} must be positive, "
            f"got {cryo[key]!r}"
        )

    servicing_logs = devices.get("servicing_logs") or []
    assert isinstance(servicing_logs, list), (
        f"{config_dir.name}: servicing_logs must be a list"
    )
    for kind in servicing_logs:
        assert kind in DECLARED_LOG_KINDS, (
            f"{config_dir.name}: servicing_logs entry {kind!r} is not a "
            f"declared log kind ({sorted(DECLARED_LOG_KINDS)})"
        )


# ── Operations config block ───────────────────────────────────────────────────
# An optional operations: block, one named sub-block per OperationBase
# subclass (sample_load and sample_unload ship so far, sharing the same
# block shape via _SampleAccessOperationBase). A config that declares none
# carries zero footprint; a declared operations.sample_load:/
# operations.sample_unload: must reference a real vi_type: system VI with
# sane, ordered timing/tolerance values, and needle_valve must be "manual"
# (a VI-capability reference is future work).

_SAMPLE_ACCESS_CONFIG_KEYS = ("sample_load", "sample_unload")


def _check_sample_access_block(config_dir: Path, devices: dict, operations: dict, key: str) -> None:
    """Validate one operations.<key>: block against the sample-access shape."""
    block = operations.get(key)
    if block is None:
        pytest.skip(f"{config_dir.name} declares no operations.{key}: block")
    assert isinstance(block, dict), (
        f"{config_dir.name}: operations.{key}: must be a mapping"
    )

    vti_vi_name = block.get("vti_vi", "temperature_vti")
    virtual_instruments = devices.get("virtual_instruments", {})
    vi_cfg = virtual_instruments.get(vti_vi_name)
    assert vi_cfg is not None, (
        f"{config_dir.name}: operations.{key}.vti_vi={vti_vi_name!r} "
        f"does not name a registered VI"
    )
    assert vi_cfg.get("vi_type") == "system", (
        f"{config_dir.name}: operations.{key}.vti_vi={vti_vi_name!r} "
        f"must be a vi_type: system VI, got {vi_cfg.get('vi_type')!r}"
    )

    positive_keys = (
        "temperature_tolerance_K",
        "temperature_window_s",
    )
    for pos_key in positive_keys:
        if pos_key not in block:
            continue
        assert float(block[pos_key]) > 0, (
            f"{config_dir.name}: operations.{key}.{pos_key} must be "
            f"positive, got {block[pos_key]!r}"
        )

    needle_valve = block.get("needle_valve", "manual")
    assert needle_valve == "manual", (
        f"{config_dir.name}: operations.{key}.needle_valve="
        f"{needle_valve!r} is not supported; only 'manual' is implemented "
        f"today (a VI-capability reference is future work)"
    )


@pytest.mark.parametrize("config_dir", _config_dirs(), ids=lambda p: p.name)
@pytest.mark.parametrize("config_key", _SAMPLE_ACCESS_CONFIG_KEYS)
def test_operations_config_block(config_dir: Path, config_key: str) -> None:
    """A declared operations.sample_load:/operations.sample_unload: block is well-formed."""
    devices = _load_yaml(config_dir / "devices.yaml")
    operations = devices.get("operations")
    if operations is None:
        pytest.skip(f"{config_dir.name} declares no operations: block")
    assert isinstance(operations, dict), (
        f"{config_dir.name}: operations: must be a mapping"
    )

    _check_sample_access_block(config_dir, devices, operations, config_key)


# ── Control-validation standard ───────────────────────────────────────────────
# See BaseVirtualInstrument's "Control-validation standard" docstring: VIs
# declare control_limits (method -> {param: limit_name}); __init__ populates
# self._limits from the config's init_params; the base class enforces at call
# time. These tests make the standard binding for every future VI and config.


def test_control_limits_reference_real_control_methods_and_params() -> None:
    """Every control_limits entry names an existing @control method and real params."""
    for cls in _all_vi_classes():
        for method_name, param_map in cls.control_limits.items():
            method = getattr(cls, method_name, None)
            assert callable(method), (
                f"{cls.__name__}.control_limits names '{method_name}', "
                f"which is not a method on the class"
            )
            assert getattr(method, "_is_control", False), (
                f"{cls.__name__}.control_limits names '{method_name}', "
                f"which is not tagged @control — limits only guard @control methods"
            )
            sig_params = set(inspect.signature(method).parameters)
            for param_name in param_map:
                assert param_name in sig_params, (
                    f"{cls.__name__}.control_limits['{method_name}'] names "
                    f"parameter '{param_name}', which is not in the method signature"
                )


def _sim_config_dirs() -> list[Path]:
    """Config dirs whose drivers are all simulated (buildable without hardware)."""
    result = []
    for config_dir in _config_dirs():
        devices = _load_yaml(config_dir / "devices.yaml")
        driver_classes = [
            cfg["class"] for cfg in devices.get("real_drivers", {}).values()
        ]
        if driver_classes and all(".sim_" in c for c in driver_classes):
            result.append(config_dir)
    return result


@pytest.mark.parametrize("config_dir", _sim_config_dirs(), ids=lambda p: p.name)
def test_config_populates_all_declared_control_limits(config_dir: Path) -> None:
    """Every limit a VI declares must be populated when built from this config.

    A declared-but-unpopulated limit would otherwise only explode when a user
    presses the button (CryoSoftConfigError at call time); this test moves
    that failure to CI.
    """
    station = build_station(str(config_dir))
    for vi_name in station.get_vi_names():
        vi = getattr(station, vi_name)
        for method_name, param_map in type(vi).control_limits.items():
            for param_name, limit_name in param_map.items():
                assert limit_name in vi._limits, (
                    f"{config_dir.name}: VI '{vi_name}' declares limit "
                    f"'{limit_name}' for {method_name}({param_name}) but its "
                    f"__init__ never populated self._limits with it — add the "
                    f"config key or the derivation"
                )


@pytest.mark.parametrize("config_dir", _sim_config_dirs(), ids=lambda p: p.name)
def test_declared_finite_limits_reject_out_of_range(config_dir: Path) -> None:
    """Every finite upper limit actually refuses an out-of-range @control call.

    Calls each limited @control method with a value beyond its declared
    maximum and requires CryoSoftSafetyError — i.e. the standard is not just
    declared, it is enforced, for every VI in every buildable config.
    """
    station = build_station(str(config_dir))
    enforced = 0
    for vi_name in station.get_vi_names():
        vi = getattr(station, vi_name)
        for method_name, param_map in type(vi).control_limits.items():
            method = getattr(vi, method_name)
            control_params = getattr(method, "_control_params", {})
            for param_name, limit_name in param_map.items():
                _lo, hi = vi._limits[limit_name]
                if hi is None:
                    continue  # unbounded above — nothing to violate
                # Fill the method's other params from their defaults; skip if
                # any lacks one (cannot build a safe call generically).
                kwargs: dict = {}
                buildable = True
                for other_name, other_info in control_params.items():
                    if other_name == param_name:
                        continue
                    if "default" in other_info:
                        kwargs[other_name] = other_info["default"]
                    else:
                        buildable = False
                if not buildable:
                    continue
                kwargs[param_name] = hi + abs(hi) + 1.0
                with pytest.raises(CryoSoftSafetyError):
                    method(**kwargs)
                enforced += 1
    assert enforced > 0, (
        f"{config_dir.name}: no finite limits were exercised — expected at "
        f"least one limited @control method"
    )


@pytest.mark.parametrize("config_dir", _sim_config_dirs(), ids=lambda p: p.name)
def test_evaluate_safety_flags_are_declared_in_manifest(config_dir: Path) -> None:
    """Every flag a VI's evaluate_safety() reports is in its safety_flags manifest.

    Builds the sim station, takes one state snapshot, and calls each VI's
    ``evaluate_safety()`` with its own slice exactly as
    ``Station.check_safety()`` does (station.py's ``check_safety()``:
    ``vi.evaluate_safety(state.get(vi_name, {}))``), asserting the returned
    flag keys are a subset of that VI's merged ``safety_flags`` manifest —
    the sim round-trip proof that the declarative manifest actually matches
    what the VI can report, not just what it is documented to report.
    """
    station = build_station(str(config_dir))
    state = station.get_state()
    checked = 0
    for vi_name in station.get_vi_names():
        vi = station.get_vi(vi_name)
        flags = vi.evaluate_safety(state.get(vi_name, {}))
        manifest = type(vi).merged_safety_flags()
        undeclared = set(flags) - set(manifest)
        assert not undeclared, (
            f"{config_dir.name}: VI '{vi_name}' evaluate_safety() reported "
            f"undeclared flag(s) {sorted(undeclared)} — not in its "
            f"safety_flags manifest {sorted(manifest)}"
        )
        checked += 1
    assert checked > 0, f"{config_dir.name}: no VIs to check"


# ── Control-declaration standard (GUI metadata) ──────────────────────────────
# See cryosoft.core.decorators: @control optionally declares params=
# {name: ParamSpec} (widget shape, unit, bounds, choices) and panel=
# (default monitor-card placement). The decorator enforces name matching and
# the VI base class enforces the ParamSpec type at import; these tests bind
# the semantic parts for every VI, present and future.


@pytest.mark.parametrize("vi_cls", _all_vi_classes(), ids=lambda c: c.__name__)
def test_control_declarations_are_consistent(vi_cls: type) -> None:
    """Declared control ParamSpecs agree with the signature; panel is a bool."""
    for method_name, method in _control_methods(vi_cls).items():
        assert isinstance(get_control_panel(method), bool), (
            f"{vi_cls.__name__}.{method_name}: _control_panel must be a bool"
        )
        specs = get_control_specs(method)
        if not specs:
            continue
        try:
            hints = typing.get_type_hints(inspect.unwrap(method))
        except Exception:
            hints = {}
        for param_name, spec in specs.items():
            assert isinstance(spec, ParamSpec), (
                f"{vi_cls.__name__}.{method_name}: params[{param_name!r}] "
                f"must be a ParamSpec, got {type(spec).__name__}"
            )
            annotated = hints.get(param_name)
            if annotated in (float, int, str, bool):
                assert spec.type is annotated, (
                    f"{vi_cls.__name__}.{method_name}: params[{param_name!r}] "
                    f"declares type {spec.type.__name__} but the signature "
                    f"annotates {annotated.__name__} — they must agree"
                )


# ── Capability-scope standard ─────────────────────────────────────────────────
# See cryosoft.core.decorators ("@control gains a scope") and GLOSSARY.md's
# "Capability scope" entry: every @control method carries "measurement"
# (default, usable by any plan) or "operation" (usable only by an operation's
# plan; still an ordinary GUI control). These tests make the standard binding
# for every VI, present and future.


def _control_methods(cls: type) -> dict[str, object]:
    """{method_name: method} for every @control method defined on *cls*."""
    methods: dict[str, object] = {}
    for name in dir(cls):
        try:
            attr = getattr(cls, name)
        except AttributeError:
            continue
        if callable(attr) and getattr(attr, "_is_control", False):
            methods[name] = attr
    return methods


@pytest.mark.parametrize("vi_cls", _all_vi_classes(), ids=lambda c: c.__name__)
def test_control_scope_is_a_valid_value(vi_cls: type) -> None:
    """Every @control method's capability scope is "measurement" or "operation"."""
    for method_name, method in _control_methods(vi_cls).items():
        scope = get_control_scope(method)
        assert scope in VALID_CONTROL_SCOPES, (
            f"{vi_cls.__name__}.{method_name} has invalid control scope "
            f"{scope!r}, must be one of {sorted(VALID_CONTROL_SCOPES)}"
        )


def test_known_operation_scope_controls() -> None:
    """The persistent-magnet heater/persistent-mode and level-meter refresh
    controls are operation-scope — the switch-heater/persistent-mode
    entry-exit methods on the persistent magnet VI, and
    CryogenLevelMeterVI.set_refresh_rate.
    """
    from cryosoft.virtual_instruments.level.cryogen_level_meter import CryogenLevelMeterVI
    from cryosoft.virtual_instruments.magnet.superconducting_magnet_persistent import (
        SuperconductingMagnetPersistentVI,
    )

    assert get_control_scope(CryogenLevelMeterVI.set_refresh_rate) == "operation"
    for method_name in (
        "enable_persistent_mode",
        "disable_persistent_mode",
        "switch_heater_on",
        "switch_heater_off",
    ):
        method = getattr(SuperconductingMagnetPersistentVI, method_name)
        assert get_control_scope(method) == "operation", (
            f"SuperconductingMagnetPersistentVI.{method_name} must be "
            f"operation-scope"
        )


def test_reading_setters_are_measurement_scope() -> None:
    """Every reading_setters target method is measurement-scope.

    The reading loop is a procedure-only mechanism ("reading-loop
    setters and the measurement lifecycle are measurement-scope by
    definition, so no existing procedure changes behavior").
    """
    checked = 0
    for vi_cls in _all_vi_classes():
        for param_name, setter_name in vi_cls.reading_setters.items():
            method = getattr(vi_cls, setter_name, None)
            if method is None:
                continue
            scope = get_control_scope(method)
            assert scope == "measurement", (
                f"{vi_cls.__name__}.reading_setters[{param_name!r}] setter "
                f"{setter_name!r} must be measurement-scope, got {scope!r}"
            )
            checked += 1
    assert checked > 0, "expected at least one declared reading_setters entry"


@pytest.mark.parametrize(
    "vi_cls",
    [cls for cls in _all_vi_classes() if issubclass(cls, MeasurementInstrumentBase)],
    ids=lambda c: c.__name__,
)
def test_measurement_lifecycle_is_measurement_scope(vi_cls: type) -> None:
    """A measurement VI's initiate_measurement()/standby() lifecycle is measurement-scope.

    Some concrete VIs keep @control on initiate_measurement() so the GUI can arm it
    manually (MeasurementInstrumentBase docstring); that @control must never
    carry operation scope. standby() is typically undecorated, which
    defaults to measurement-scope — checked here too for completeness.
    """
    for method_name in ("initiate_measurement", "standby"):
        method = getattr(vi_cls, method_name)
        scope = get_control_scope(method)
        assert scope == "measurement", (
            f"{vi_cls.__name__}.{method_name} must be measurement-scope, "
            f"got {scope!r}"
        )


# ── Operation contract (L4, cryosoft.core.operation.OperationBase) ───────────
# See OperationBase's docstring, including readiness/next-due. The
# discovery scaffold above tolerates an empty parametrize set too, which
# pytest handles by simply collecting zero test cases.


@pytest.mark.parametrize("op_cls", _all_operation_classes(), ids=lambda c: c.__name__)
def test_operation_declaration(op_cls: type) -> None:
    """Every OperationBase subclass names itself and declares valid tolerated flags."""
    assert op_cls.name, f"{op_cls.__name__} must set the 'name' class attribute"
    tolerated = op_cls.tolerated_safety_flags
    assert isinstance(tolerated, frozenset), (
        f"{op_cls.__name__}.tolerated_safety_flags must be a frozenset, "
        f"got {tolerated!r}"
    )
    assert all(isinstance(flag, str) for flag in tolerated), (
        f"{op_cls.__name__}.tolerated_safety_flags must contain only str "
        f"flags, got {tolerated!r}"
    )


@pytest.mark.parametrize("op_cls", _all_operation_classes(), ids=lambda c: c.__name__)
def test_operation_constructs_from_defaults(op_cls: type) -> None:
    """Every OperationBase subclass must construct from a sim station alone.

    Unlike a plain procedure (some of which build from an empty ``Station``),
    every operation resolves VIs from the station at construction (e.g. the
    helium fill's ``Station.magnet_vi_names()`` and its configured level VI),
    so it needs a populated one — mirrors
    ``test_procedure_constructs_from_defaults``'s station-dependent branch.
    Every other constructor argument (``person``, the plan-§9 ``**config``
    keys) must have a working default.
    """
    station = build_station("cryosoft/configs/sim_cryostat")
    op_cls(station)


@pytest.mark.parametrize("op_cls", _all_operation_classes(), ids=lambda c: c.__name__)
def test_operation_readiness_conditions_returns_tuple_of_readiness_condition(op_cls: type) -> None:
    """readiness_conditions() must return a tuple of ReadinessCondition.

    The Operations panel (``gui/operations_panel.py``) builds one checklist
    row per element with zero per-operation code — a wrong return type would
    silently break every card, not just this one, so it is checked here for
    every discovered operation automatically.
    """
    station = build_station("cryosoft/configs/sim_cryostat")
    op = op_cls(station)
    conditions = op.readiness_conditions()
    assert isinstance(conditions, tuple), (
        f"{op_cls.__name__}.readiness_conditions() must return a tuple, got {type(conditions)!r}"
    )
    for condition in conditions:
        assert isinstance(condition, ReadinessCondition), (
            f"{op_cls.__name__}.readiness_conditions() must contain only "
            f"ReadinessCondition instances, got {condition!r}"
        )


@pytest.mark.parametrize("op_cls", _all_operation_classes(), ids=lambda c: c.__name__)
def test_operation_claimed_vi_names_contract(op_cls: type) -> None:
    """claimed_vi_names() returns None or a set of VI names known to the station.

    Mirrors ``test_procedure_claimed_vi_names_contract``: ``None`` (claim
    everything) is always valid; a non-``None`` return must be a
    ``set[str]`` naming real station VIs, so a typo in a narrowed claim
    (e.g. ``HeliumFillOperation``'s level meter, ``SampleLoadOperation``'s/
    ``SampleUnloadOperation``'s magnets/VTI/measurement VIs) can never
    silently under-claim.
    """
    station = build_station("cryosoft/configs/sim_cryostat")
    op = op_cls(station)
    claimed = op.claimed_vi_names()
    if claimed is None:
        return
    assert isinstance(claimed, set) and all(isinstance(name, str) for name in claimed), (
        f"{op_cls.__name__}.claimed_vi_names() must return None or a set[str], "
        f"got {claimed!r}"
    )
    known = set(station.get_vi_names())
    unknown = claimed - known
    assert not unknown, (
        f"{op_cls.__name__}.claimed_vi_names() names VI(s) not on the station: "
        f"{sorted(unknown)}"
    )


def test_operation_config_key_unique_across_operations() -> None:
    """A non-empty config_key must be unique across every discovered operation.

    The Operations panel maps ``operations: {config_key: block}`` config
    entries to a class by ``config_key`` — a collision would make
    that mapping ambiguous.
    """
    keys = [op_cls.config_key for op_cls in _all_operation_classes() if op_cls.config_key]
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"config_key collision(s) across operations: {duplicates}"


# ── Measurement-method standard ───────────────────────────────────────────────
# See MeasurementInstrumentBase: every concrete measurement VI is self-describing
# (measurement_parameters / measurement_data_keys / measurement_scalar_columns)
# and implements one uniform lifecycle (data_arrays / initiate_measurement / take_reading /
# standby). These tests make that standard binding for every future measurement
# VI the moment its file exists.

# Superset of sim drivers covering every role any measurement VI asks for. Each
# VI picks the roles it needs (e.g. "source"+"meter" or "main") and ignores the
# rest, so one dict builds every measurement VI without per-class knowledge. Add
# a role here when a new measurement VI introduces a new instrument.
_SIM_MEASUREMENT_DRIVER_CLASSES = {
    "source": "cryosoft.drivers.sim_keithley_6221.SimKeithley6221",
    "meter": "cryosoft.drivers.sim_keithley_2182a.SimKeithley2182A",
    "main": "cryosoft.drivers.sim_keithley_2400.SimKeithley2400",
    "lockin": "cryosoft.drivers.sim_lockin.SimLockIn",
    "tensormeter": "cryosoft.drivers.sim_tensormeter_rtm2.SimTensormeterRTM2",
}


def _all_measurement_vi_classes() -> list[type]:
    """Every concrete measurement-method VI class."""
    return [
        cls
        for cls in _all_vi_classes()
        if issubclass(cls, MeasurementInstrumentBase)
    ]


def _build_sim_measurement_drivers() -> dict[str, object]:
    """Fresh sim-driver instances for every role a measurement VI may use."""
    return {
        role: _import_class(dotted)("SIM::CONFORMANCE")
        for role, dotted in _SIM_MEASUREMENT_DRIVER_CLASSES.items()
    }


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_self_description(vi_cls: type) -> None:
    """Every measurement VI declares valid self-describing class attributes."""
    params = vi_cls.measurement_parameters
    assert params, (
        f"{vi_cls.__name__} declares no measurement_parameters — a measurement "
        f"method must own its GUI-facing knobs as ParamSpecs"
    )
    for name, spec in params.items():
        assert isinstance(spec, ParamSpec), (
            f"{vi_cls.__name__}.measurement_parameters['{name}'] must be a "
            f"ParamSpec, got {spec!r}"
        )

    assert vi_cls.measurement_data_keys, (
        f"{vi_cls.__name__} declares no measurement_data_keys — it must name the "
        f"arrays take_reading() returns"
    )

    # selector_label is optional (falls back to display_label in the GUI) but,
    # when declared, must be a plain string — it labels the method drop-down.
    assert isinstance(vi_cls.selector_label, str), (
        f"{vi_cls.__name__}.selector_label must be a str (the short "
        f"method-selection drop-down label), got {vi_cls.selector_label!r}"
    )

    for name, dtype in vi_cls.measurement_scalar_columns.items():
        assert dtype in ("float", "int"), (
            f"{vi_cls.__name__}.measurement_scalar_columns['{name}'] dtype "
            f"{dtype!r} must be 'float' or 'int'"
        )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_externally_owned_parameters_contract(vi_cls: type) -> None:
    """externally_owned_parameters obeys the externally-configured standard.

    A ``frozenset`` (or at least a ``set`` — the type the standard names on
    ``MeasurementInstrumentBase``), and every name in it must be an existing
    ``measurement_parameters`` key. The empty default (no external-
    configuration support) always passes trivially.
    """
    assert isinstance(vi_cls.externally_owned_parameters, (frozenset, set)), (
        f"{vi_cls.__name__}.externally_owned_parameters must be a frozenset "
        f"(or set), got {vi_cls.externally_owned_parameters!r}"
    )
    unknown = sorted(
        set(vi_cls.externally_owned_parameters) - set(vi_cls.measurement_parameters)
    )
    assert not unknown, (
        f"{vi_cls.__name__}.externally_owned_parameters names {unknown}, "
        f"which {'is' if len(unknown) == 1 else 'are'} not (an) existing "
        f"measurement_parameters key(s)"
    )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_mean_error_array_convention(vi_cls: type) -> None:
    """Every array-valued quantity gets a companion mean + SEM scalar column.

    The mean/error/array convention (see ``MeasurementInstrumentBase`` and
    ``quantity_columns()``): every ``measurement_data_keys`` entry names a
    raw-sample array and MUST end in ``"_array"``; its base quantity name
    (the key with that suffix stripped) MUST have both a bare-name mean and
    a ``"_error"`` (SEM) scalar column in ``measurement_scalar_columns``,
    both dtype ``"float"``. Binding for every future measurement VI, not
    just the ones that adopted the convention explicitly.
    """
    for array_key in vi_cls.measurement_data_keys:
        assert array_key.endswith("_array"), (
            f"{vi_cls.__name__}.measurement_data_keys entry {array_key!r} "
            f"must end in '_array' per the mean/error/array convention"
        )
        base_name = array_key[: -len("_array")]
        assert vi_cls.measurement_scalar_columns.get(base_name) == "float", (
            f"{vi_cls.__name__}.measurement_scalar_columns must declare "
            f"{base_name!r} (the mean, dtype 'float') for array quantity "
            f"{array_key!r}"
        )
        error_name = f"{base_name}_error"
        assert vi_cls.measurement_scalar_columns.get(error_name) == "float", (
            f"{vi_cls.__name__}.measurement_scalar_columns must declare "
            f"{error_name!r} (the SEM, dtype 'float') for array quantity "
            f"{array_key!r}"
        )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_lifecycle_methods(vi_cls: type) -> None:
    """Every measurement VI implements the lifecycle; take_reading takes no args."""
    for method_name in ("data_arrays", "initiate_measurement", "take_reading", "standby"):
        assert callable(getattr(vi_cls, method_name, None)), (
            f"{vi_cls.__name__} lacks the '{method_name}' lifecycle method"
        )

    required = [
        p
        for p in inspect.signature(vi_cls.take_reading).parameters.values()
        if p.name != "self"
        and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert not required, (
        f"{vi_cls.__name__}.take_reading() must take no required arguments "
        f"(everything is fixed at initiate_measurement()), got {[p.name for p in required]}"
    )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_round_trip(vi_cls: type) -> None:
    """Built from sim drivers, a measurement VI returns exactly what it declares.

    initiate_measurement(**defaults) then take_reading() must yield exactly
    measurement_data_keys (each with the length data_arrays declared) plus every
    measurement_scalar_columns key — the machine check that a measurement method
    is plug-compatible the day its file exists.
    """
    vi = vi_cls(_build_sim_measurement_drivers())
    defaults = {name: spec.default for name, spec in vi_cls.measurement_parameters.items()}

    vi.initiate_measurement(**defaults)
    data = vi.take_reading()

    expected_keys = (
        set(vi_cls.measurement_data_keys)
        | set(vi_cls.measurement_scalar_columns)
        | set(vi_cls.measurement_raw_blocks)
    )
    assert set(data) == expected_keys, (
        f"{vi_cls.__name__}.take_reading() returned keys {sorted(data)}, "
        f"expected {sorted(expected_keys)}"
    )

    arrays = vi.data_arrays(defaults)
    assert set(arrays) == set(vi_cls.measurement_data_keys), (
        f"{vi_cls.__name__}.data_arrays() keys {sorted(arrays)} != "
        f"measurement_data_keys {sorted(vi_cls.measurement_data_keys)}"
    )
    for name, length in arrays.items():
        assert len(data[name]) == length, (
            f"{vi_cls.__name__}.take_reading()['{name}'] has length "
            f"{len(data[name])}, but data_arrays declared {length}"
        )

    block_rows = vi.raw_block_row_counts(defaults)
    assert set(block_rows) == set(vi_cls.measurement_raw_blocks), (
        f"{vi_cls.__name__}.raw_block_row_counts() keys {sorted(block_rows)} != "
        f"measurement_raw_blocks {sorted(vi_cls.measurement_raw_blocks)}"
    )
    for name, labels in vi_cls.measurement_raw_blocks.items():
        rows = block_rows[name]
        block = data[name]
        assert len(block) == rows, (
            f"{vi_cls.__name__}.take_reading()['{name}'] has {len(block)} rows, "
            f"but raw_block_row_counts declared {rows}"
        )
        for row in block:
            assert len(row) == len(labels), (
                f"{vi_cls.__name__}.take_reading()['{name}'] row has "
                f"{len(row)} channels, but measurement_raw_blocks declared "
                f"{len(labels)}"
            )

    for name in vi_cls.measurement_scalar_columns:
        value = data[name]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (
            f"{vi_cls.__name__}.take_reading()['{name}'] must be a real-number "
            f"scalar, got {value!r}"
        )

    for array_key in vi_cls.measurement_data_keys:
        base_name = array_key[: -len("_array")]
        error = data[f"{base_name}_error"]
        assert error >= 0.0 or math.isnan(error), (
            f"{vi_cls.__name__}.take_reading()['{base_name}_error'] must be "
            f">= 0 (or NaN, when zero samples are valid), got {error!r}"
        )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_raw_block_names_dont_collide(vi_cls: type) -> None:
    """A raw diagnostic block never shadows a data-key/scalar-column name.

    Per MeasurementInstrumentBase's "Raw diagnostic blocks" standard: blocks
    are deliberately excluded from measurement_data_keys/
    measurement_scalar_columns (so they never appear in a GUI plot-axis
    dropdown); a colliding name would make the two self-descriptions
    ambiguous. Every declared block's channel-label list must also be
    non-empty — an empty block declares a zero-width channel axis, which
    HDF5 cannot allocate meaningfully.
    """
    other_names = set(vi_cls.measurement_data_keys) | set(vi_cls.measurement_scalar_columns)
    for block_name, labels in vi_cls.measurement_raw_blocks.items():
        assert block_name not in other_names, (
            f"{vi_cls.__name__}.measurement_raw_blocks name {block_name!r} "
            f"collides with a measurement_data_keys/measurement_scalar_columns "
            f"name"
        )
        assert labels, (
            f"{vi_cls.__name__}.measurement_raw_blocks[{block_name!r}] must "
            f"be a non-empty channel-label list"
        )


# ── Session-model standard (L6) ───────────────────────────────────────────────
# Every dataclass in cryosoft.session.models follows the tolerant-parse
# contract (see the module docstring and cryosoft/session/README.md):
# constructs from defaults alone, to_dict() is JSON-safe, from_dict() accepts
# junk without raising and round-trips to_dict() output. A new model in
# models.py is covered the moment the class exists.


def _session_model_classes() -> list[type]:
    import dataclasses

    from cryosoft.session import models

    return [
        obj
        for name, obj in vars(models).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and dataclasses.is_dataclass(obj)
        # Only models defined here — not the eager-validating core.plan types
        # (SessionEnvelope/EnvelopeBound) the module re-imports.
        and obj.__module__ == models.__name__
    ]


@pytest.mark.parametrize("model_cls", _session_model_classes(), ids=lambda c: c.__name__)
def test_session_model_constructs_from_defaults(model_cls: type) -> None:
    """Every session model constructs with no arguments."""
    model_cls()


@pytest.mark.parametrize("model_cls", _session_model_classes(), ids=lambda c: c.__name__)
def test_session_model_dict_contract(model_cls: type) -> None:
    """to_dict()/from_dict() exist, are JSON-safe, and round-trip defaults."""
    import json as json_module

    instance = model_cls()
    assert hasattr(instance, "to_dict") and hasattr(model_cls, "from_dict"), (
        f"{model_cls.__name__} must implement to_dict()/from_dict()"
    )
    payload = instance.to_dict()
    json_module.dumps(payload)  # JSON-safe or this raises
    assert model_cls.from_dict(payload) == instance


@pytest.mark.parametrize("model_cls", _session_model_classes(), ids=lambda c: c.__name__)
@pytest.mark.parametrize(
    "junk", [None, 42, "text", [], {"bogus_key": object}], ids=type
)
def test_session_model_from_dict_tolerates_junk(model_cls: type, junk) -> None:
    """from_dict() never raises on junk input — it degrades to defaults."""
    result = model_cls.from_dict(junk)
    assert isinstance(result, model_cls)


# ── Servicing-log kind standard (L6) ──────────────────────────────────────────
# Every declared LogKindSpec (cryosoft.session.servicing_log.DECLARED_LOG_KINDS)
# must have a valid key, a title, and a non-empty ordered field schema of
# ParamSpecs — see LogKindSpec's docstring. A new log kind is covered the
# moment it's added to the registry, no
# test needs to be written for it. ParamSpec.__post_init__ already enforces at
# construction that every field's default matches its declared type, so a
# LogKindSpec that imports at all already has a usable default per field.


@pytest.mark.parametrize("kind_key", sorted(DECLARED_LOG_KINDS), ids=lambda k: k)
def test_log_kind_spec_is_valid(kind_key: str) -> None:
    """Every declared log kind has a valid key and a ParamSpec field schema."""
    spec = DECLARED_LOG_KINDS[kind_key]
    assert spec.key == kind_key, (
        f"DECLARED_LOG_KINDS[{kind_key!r}] must be registered under its own key, "
        f"got LogKindSpec.key={spec.key!r}"
    )
    assert spec.key and spec.key.isidentifier() and spec.key == spec.key.lower(), (
        f"LogKindSpec.key {spec.key!r} must be a non-empty lowercase identifier"
    )
    assert spec.title, f"LogKindSpec({spec.key!r}) must declare a non-empty title"
    assert spec.fields, f"LogKindSpec({spec.key!r}) declares no fields"
    for name, field_spec in spec.fields.items():
        assert isinstance(field_spec, ParamSpec), (
            f"LogKindSpec({spec.key!r}).fields[{name!r}] must be a ParamSpec, "
            f"got {field_spec!r}"
        )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_reading_setters_contract(vi_cls: type) -> None:
    """reading_setters obeys the reading-loop standard.

    Every key names an existing, non-bool measurement parameter; every value
    names a real method of the VI whose signature accepts the parameter under
    its own name. See MeasurementInstrumentBase's "reading loop" section.
    """
    for param_name, setter_name in vi_cls.reading_setters.items():
        spec = vi_cls.measurement_parameters.get(param_name)
        assert spec is not None, (
            f"{vi_cls.__name__}.reading_setters key {param_name!r} is not a "
            f"measurement parameter"
        )
        assert spec.type is not bool, (
            f"{vi_cls.__name__}.reading_setters key {param_name!r} is a bool — "
            f"a bool cannot be looped over a value list"
        )
        setter = getattr(vi_cls, setter_name, None)
        assert callable(setter), (
            f"{vi_cls.__name__}.reading_setters[{param_name!r}] names method "
            f"{setter_name!r}, which the VI does not have"
        )
        sig_params = inspect.signature(setter).parameters
        accepts = param_name in sig_params or any(
            p.kind is p.VAR_KEYWORD for p in sig_params.values()
        )
        assert accepts, (
            f"{vi_cls.__name__}.{setter_name}() must accept the looped "
            f"parameter as a keyword named {param_name!r}"
        )


@pytest.mark.parametrize(
    "vi_cls", _all_measurement_vi_classes(), ids=lambda c: c.__name__
)
def test_measurement_vi_reading_setter_round_trip(vi_cls: type) -> None:
    """Every reading setter reconfigures the reading, never its shape.

    Built from sim drivers and armed with defaults, calling each declared
    setter (with the parameter's default value, as one reading-loop iteration
    would) must leave take_reading() returning exactly the declared keys and
    lengths.
    """
    if not vi_cls.reading_setters:
        pytest.skip(f"{vi_cls.__name__} declares no reading_setters")
    vi = vi_cls(_build_sim_measurement_drivers())
    defaults = {
        name: spec.default for name, spec in vi_cls.measurement_parameters.items()
    }
    vi.initiate_measurement(**defaults)
    arrays = vi.data_arrays(defaults)
    expected_keys = (
        set(vi_cls.measurement_data_keys) | set(vi_cls.measurement_scalar_columns)
    )
    for param_name, setter_name in vi_cls.reading_setters.items():
        getattr(vi, setter_name)(**{param_name: defaults[param_name]})
        data = vi.take_reading()
        assert set(data) == expected_keys, (
            f"{vi_cls.__name__}: after {setter_name}(), take_reading() returned "
            f"{sorted(data)}, expected {sorted(expected_keys)}"
        )
        for name, length in arrays.items():
            assert len(data[name]) == length, (
                f"{vi_cls.__name__}: after {setter_name}(), '{name}' has length "
                f"{len(data[name])}, declared {length}"
            )


# ── Reading-loop standard (BaseVirtualInstrument level, all VI roles) ────────
# reading_setters is a VI-level standard: the switch VI's route and a
# measurement VI's current are the same loopable-parameter concept. Check
# every VI the sim station builds, whatever its role.

def test_reading_loop_standard_on_sim_station() -> None:
    """Every sim-station VI with reading_setters honours the loop standard.

    For each declared entry: reading_parameters supplies a ParamSpec for the
    key; the setter is a real method accepting the parameter under its own
    name; and a non-measurement participant's reading_safe_off (if declared)
    names a real method.
    """
    station = build_station("cryosoft/configs/sim_cryostat")
    checked = 0
    for vi_name in station.get_vi_names():
        vi = station.get_vi(vi_name)
        specs = vi.reading_parameters
        for param_name, setter_name in vi.reading_setters.items():
            checked += 1
            spec = specs.get(param_name)
            assert isinstance(spec, ParamSpec), (
                f"{vi_name}.reading_parameters must supply a ParamSpec for "
                f"loopable parameter {param_name!r}, got {spec!r}"
            )
            setter = getattr(vi, setter_name, None)
            assert callable(setter), (
                f"{vi_name}.reading_setters[{param_name!r}] names method "
                f"{setter_name!r}, which the VI does not have"
            )
            sig_params = inspect.signature(setter).parameters
            accepts = param_name in sig_params or any(
                p.kind is p.VAR_KEYWORD for p in sig_params.values()
            )
            assert accepts, (
                f"{vi_name}.{setter_name}() must accept the looped parameter "
                f"as a keyword named {param_name!r}"
            )
        if vi.reading_safe_off:
            assert callable(getattr(vi, vi.reading_safe_off, None)), (
                f"{vi_name}.reading_safe_off names method "
                f"{vi.reading_safe_off!r}, which the VI does not have"
            )
    # The sim station must exercise the standard: the switch's route and the
    # DC VI's current at minimum.
    assert checked >= 2, "sim station should declare at least two loopable parameters"
