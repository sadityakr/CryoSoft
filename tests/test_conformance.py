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
import pkgutil
import typing
from pathlib import Path

import pytest

import cryosoft.drivers
import cryosoft.procedures
import cryosoft.virtual_instruments
from cryosoft.core.decorators import (
    VALID_CONTROL_SCOPES,
    get_control_panel,
    get_control_scope,
    get_control_specs,
)
from cryosoft.core.exceptions import CryoSoftSafetyError
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

    Concurrency-scope hook (docs/plans/operation-concurrency-and-error-
    scoping.md §1): ``None`` (claim everything) is always valid; a non-``None``
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
# See docs/plans/cryogenics-logbook.md §9: an optional cryogenics: block plus
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
        "fill_zero_field_eps_T",
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
# See docs/plans/cryogenics-logbook.md §9: an optional operations: block, one
# named sub-block per OperationBase subclass (only sample_change ships so
# far). A config that declares none carries zero footprint; a declared
# operations.sample_change: must reference a real vi_type: system VI with
# sane, ordered timing/tolerance values, and needle_valve must be "manual"
# (a VI-capability reference is future work, plan §8.2).


@pytest.mark.parametrize("config_dir", _config_dirs(), ids=lambda p: p.name)
def test_operations_config_block(config_dir: Path) -> None:
    """A declared operations.sample_change: block is well-formed."""
    devices = _load_yaml(config_dir / "devices.yaml")
    operations = devices.get("operations")
    if operations is None:
        pytest.skip(f"{config_dir.name} declares no operations: block")
    assert isinstance(operations, dict), (
        f"{config_dir.name}: operations: must be a mapping"
    )

    sample_change = operations.get("sample_change")
    if sample_change is None:
        pytest.skip(f"{config_dir.name} declares no operations.sample_change: block")
    assert isinstance(sample_change, dict), (
        f"{config_dir.name}: operations.sample_change: must be a mapping"
    )

    vti_vi_name = sample_change.get("vti_vi", "temperature_vti")
    virtual_instruments = devices.get("virtual_instruments", {})
    vi_cfg = virtual_instruments.get(vti_vi_name)
    assert vi_cfg is not None, (
        f"{config_dir.name}: operations.sample_change.vti_vi={vti_vi_name!r} "
        f"does not name a registered VI"
    )
    assert vi_cfg.get("vi_type") == "system", (
        f"{config_dir.name}: operations.sample_change.vti_vi={vti_vi_name!r} "
        f"must be a vi_type: system VI, got {vi_cfg.get('vi_type')!r}"
    )

    positive_keys = (
        "temperature_tolerance_K",
        "temperature_window_s",
        "zero_field_eps_T",
        "zero_field_window_s",
        "postcondition_timeout_s",
    )
    for key in positive_keys:
        if key not in sample_change:
            continue
        assert float(sample_change[key]) > 0, (
            f"{config_dir.name}: operations.sample_change.{key} must be "
            f"positive, got {sample_change[key]!r}"
        )

    needle_valve = sample_change.get("needle_valve", "manual")
    assert needle_valve == "manual", (
        f"{config_dir.name}: operations.sample_change.needle_valve="
        f"{needle_valve!r} is not supported; only 'manual' is implemented "
        f"today (a VI-capability reference is future work, plan §8.2)"
    )


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
    controls are operation-scope (plan §5) — the switch-heater/persistent-mode
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

    The reading loop is a procedure-only mechanism (plan §5: "reading-loop
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
# See OperationBase's docstring, plan §4.1, and §12 (readiness/next-due). The
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
    """readiness_conditions() must return a tuple of ReadinessCondition (plan §12).

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
    (e.g. ``HeliumFillOperation``'s level meter, ``SampleChangeOperation``'s
    magnets/VTI/switch/measurement VIs) can never silently under-claim.
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
    entries to a class by ``config_key`` (plan §12) — a collision would make
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

    expected_keys = set(vi_cls.measurement_data_keys) | set(vi_cls.measurement_scalar_columns)
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

    for name in vi_cls.measurement_scalar_columns:
        value = data[name]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (
            f"{vi_cls.__name__}.take_reading()['{name}'] must be a real-number "
            f"scalar, got {value!r}"
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
# ParamSpecs — see LogKindSpec's docstring and docs/plans/cryogenics-logbook.md
# §6.1. A new log kind is covered the moment it's added to the registry, no
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
