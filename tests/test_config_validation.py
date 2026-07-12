"""Unit tests for validate_config_dir — the config editor's dry-run gate.

Confirms it reports importable-class and driver-reference problems without
instantiating any driver or VI (so validating a real-hardware config is safe).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.station import build_station_with_fallback, validate_config_dir

_SIM_CONFIG = "cryosoft/configs/sim_cryostat"

_GOOD_DEVICES = """\
real_drivers:
  ips_x:
    class: cryosoft.drivers.sim_oxford_ips120.SimOxfordIPS120
    address: "SIM::IPS_X"
virtual_instruments:
  magnet_x:
    class: cryosoft.virtual_instruments.magnet.superconducting_magnet.SuperconductingMagnetVI
    drivers: {main: ips_x}
    vi_type: system
"""

_MONITOR = "monitor:\n  tick_interval_ms: 3000\n"


def _write(base: Path, devices: str, monitor: str = _MONITOR) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "devices.yaml").write_text(devices, encoding="utf-8")
    (base / "monitor.yaml").write_text(monitor, encoding="utf-8")
    return base


def test_valid_config_has_no_errors(tmp_path):
    """A well-formed config validates clean."""
    d = _write(tmp_path / "cfg", _GOOD_DEVICES)
    assert validate_config_dir(str(d)) == []


def test_missing_files_reported(tmp_path):
    """A directory with no YAML files reports both missing."""
    (tmp_path / "empty").mkdir()
    errors = validate_config_dir(str(tmp_path / "empty"))
    assert any("devices.yaml" in e for e in errors)
    assert any("monitor.yaml" in e for e in errors)


def test_unknown_driver_reference_reported(tmp_path):
    """A VI referencing an undefined driver is flagged."""
    devices = _GOOD_DEVICES.replace("main: ips_x", "main: does_not_exist")
    d = _write(tmp_path / "cfg", devices)
    errors = validate_config_dir(str(d))
    assert any("does_not_exist" in e for e in errors)


def test_unimportable_class_reported(tmp_path):
    """A non-importable driver class is flagged."""
    devices = _GOOD_DEVICES.replace(
        "cryosoft.drivers.sim_oxford_ips120.SimOxfordIPS120",
        "cryosoft.drivers.nope.DoesNotExist",
    )
    d = _write(tmp_path / "cfg", devices)
    errors = validate_config_dir(str(d))
    assert any("ips_x" in e for e in errors)


def test_malformed_yaml_reported(tmp_path):
    """Unparseable devices.yaml is reported, not raised."""
    d = _write(tmp_path / "cfg", "real_drivers: {: broken\n")
    errors = validate_config_dir(str(d))
    assert any("devices.yaml" in e for e in errors)


# ── build_station_with_fallback ───────────────────────────────────────────────

def test_fallback_first_valid_wins():
    """The first valid config is used and there are no warnings."""
    station, used, warnings = build_station_with_fallback([_SIM_CONFIG])
    assert used == _SIM_CONFIG
    assert warnings == []
    assert station.get_vi_names()


def test_fallback_skips_invalid_uses_next(tmp_path):
    """An invalid first candidate is skipped (with a warning) for the next."""
    bad = _write(tmp_path / "bad", "real_drivers: {: broken\n")
    station, used, warnings = build_station_with_fallback([str(bad), _SIM_CONFIG])
    assert used == _SIM_CONFIG
    assert warnings and "bad" in warnings[0]
    assert station.get_vi_names()


def test_fallback_all_invalid_raises(tmp_path):
    """If nothing is usable, it raises rather than returning a broken station."""
    bad = _write(tmp_path / "bad", "real_drivers: {: broken\n")
    with pytest.raises(CryoSoftConfigError):
        build_station_with_fallback([str(bad)])
