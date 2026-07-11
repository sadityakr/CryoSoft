# ---
# description: |
#   Auto-discovering conformance tests for the CryoSoft layer interfaces.
#   These tests iterate over the drivers, virtual_instruments, procedures, and
#   configs packages themselves, so any NEW module an agent adds is checked
#   automatically — no test needs to be written for it to be covered.
# last_updated: 2026-07-11
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
from pathlib import Path

import pytest

import cryosoft.drivers
import cryosoft.procedures
import cryosoft.virtual_instruments
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import Station, _import_class
from cryosoft.virtual_instruments.base import BaseVirtualInstrument

CONFIGS_DIR = Path(cryosoft.__file__).parent / "configs"

# Modules in virtual_instruments/ that hold base classes, not concrete VIs.
VI_BASE_MODULES = {
    "cryosoft.virtual_instruments.base",
    "cryosoft.virtual_instruments.rampable",
}

# Registry types accepted by Station.register_vi via config (distinct from a VI
# class's own vi_type like "magnet" — see GLOSSARY.md).
CONFIG_VI_TYPES = {"system", "measurement", "level"}


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
    """Every procedure names itself and declares type + default for each parameter."""
    assert proc_cls.name, f"{proc_cls.__name__} must set the 'name' class attribute"
    assert proc_cls.parameters, f"{proc_cls.__name__} declares no parameters"
    for param_name, spec in proc_cls.parameters.items():
        assert "type" in spec, f"{proc_cls.__name__}.{param_name} spec lacks 'type'"
        assert "default" in spec, (
            f"{proc_cls.__name__}.{param_name} spec lacks 'default' — every "
            f"parameter needs a default so the procedure runs unattended"
        )
    valid_x_keys = (
        ["unix_time"] + list(proc_cls.sweep_data_keys) + list(proc_cls.measurement_data_keys)
    )
    assert proc_cls.default_x_key in valid_x_keys, (
        f"{proc_cls.__name__}.default_x_key={proc_cls.default_x_key!r} is not one "
        f"of its data keys {valid_x_keys}"
    )


@pytest.mark.parametrize("proc_cls", _all_procedure_classes(), ids=lambda c: c.__name__)
def test_procedure_constructs_from_defaults(proc_cls: type, tmp_path) -> None:
    """Every procedure must construct with zero explicit parameters.

    BaseProcedure merges declared defaults into the params dict, so a complete
    default set means agents and scripts can always instantiate a procedure
    without reproducing the GUI's parameter form.
    """
    proc = proc_cls(
        station=Station(),
        sample_info={"sample_name": "conformance", "sample_id": "T0", "comments": ""},
        data_directory=str(tmp_path),
    )
    assert proc.get_sweep_array(), (
        f"{proc_cls.__name__} built an empty sweep array from its own defaults"
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
