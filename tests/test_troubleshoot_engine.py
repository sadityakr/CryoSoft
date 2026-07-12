# ---
# description: |
#   Behavior tests for the troubleshoot engine: bus scan, raw address probes,
#   config preflight fault classification (every FaultCode reachable), and the
#   DriverBench (introspection, read/write gating, arg coercion, raw I/O).
#   All hardware is faked: a FakeResourceManager plays the VISA bus and the
#   sim drivers play the instruments.
# last_updated: 2026-07-12
# ---

"""Troubleshoot engine tests — every FaultCode class is exercised without hardware."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cryosoft.core.exceptions import CryoSoftCommunicationError
from cryosoft.troubleshoot.engine import (
    DriverBench,
    FaultCode,
    ProbeResult,
    check_config,
    is_read_only,
    probe_address,
    scan_bus,
)

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeInstrument:
    """Stand-in for a pyvisa resource: canned reply, or raise on query."""

    def __init__(self, reply: str = "FAKE,INSTR,0,1.0", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.timeout = None
        self.writes: list[str] = []
        self.closed = False

    def query(self, command: str) -> str:
        if self.fail:
            raise OSError("simulated VISA timeout")
        self.writes.append(command)
        return self.reply

    def write(self, command: str) -> None:
        self.writes.append(command)

    def close(self) -> None:
        self.closed = True


class FakeResourceManager:
    """Stand-in for pyvisa.ResourceManager with a configurable bus."""

    def __init__(self, resources: dict[str, FakeInstrument] | None = None) -> None:
        self.resources = resources or {}

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self.resources.keys())

    def open_resource(self, address: str) -> FakeInstrument:
        if address not in self.resources:
            raise OSError(f"VI_ERROR_RSRC_NFOUND: {address}")
        return self.resources[address]


# Driver classes referenced from generated configs by dotted path.
# This module is not named sim_*, so the engine treats them as real drivers.


class AlwaysUpDriver:
    """Fake 'real' driver that constructs and identifies fine."""

    def __init__(self, resource_string: str) -> None:
        self.resource = resource_string

    def get_idn(self) -> str:
        return "ACME,MODEL1,SN42,9.9"


class OpenFailsDriver:
    """Fake driver whose constructor cannot open its resource."""

    def __init__(self, resource_string: str) -> None:
        raise CryoSoftCommunicationError(
            f"cannot open {resource_string}", vi_name="OpenFailsDriver"
        )


class SilentDriver:
    """Fake driver that opens but never answers the identify query."""

    def __init__(self, resource_string: str) -> None:
        pass

    def get_idn(self) -> str:
        raise CryoSoftCommunicationError("timeout", vi_name="SilentDriver")


class BuggyDriver:
    """Fake driver whose get_idn has a plain Python bug."""

    def __init__(self, resource_string: str) -> None:
        pass

    def get_idn(self) -> str:
        raise RuntimeError("unparseable response: index out of range")


class EmptyIdnDriver:
    """Fake driver that answers the identify query with nothing."""

    def __init__(self, resource_string: str) -> None:
        pass

    def get_idn(self) -> str:
        return "   "


# ── Config builder ────────────────────────────────────────────────────────────

_THIS = "tests.test_troubleshoot_engine"


def make_config(tmp_path: Path, drivers: dict[str, dict]) -> str:
    """Write a minimal valid config directory and return its path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lines = ["real_drivers:"]
    for alias, cfg in drivers.items():
        lines.append(f"  {alias}:")
        for key, value in cfg.items():
            lines.append(f"    {key}: \"{value}\"")
    (tmp_path / "devices.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n", encoding="utf-8"
    )
    return str(tmp_path)


# ── FaultCode taxonomy is API ─────────────────────────────────────────────────


def test_fault_codes_are_stable() -> None:
    """The taxonomy values are consumed by the triage skill — renames break it."""
    assert {c.value for c in FaultCode} == {
        "OK",
        "CONFIG_INVALID",
        "ADDRESS_NOT_ON_BUS",
        "OPEN_FAILED",
        "NO_RESPONSE",
        "WRONG_IDN",
        "GARBLED_RESPONSE",
        "DRIVER_ERROR",
    }


def test_probe_result_as_dict_is_json_ready() -> None:
    result = ProbeResult(
        alias="a", address="GPIB0::1::INSTR", driver_class="x.Y",
        code=FaultCode.OK, idn="ACME",
    )
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["code"] == "OK"
    assert result.ok


# ── Bus scan and raw probes ───────────────────────────────────────────────────


def test_scan_bus_lists_sorted_addresses() -> None:
    rm = FakeResourceManager(
        {"GPIB0::19::INSTR": FakeInstrument(), "ASRL10::INSTR": FakeInstrument()}
    )
    assert scan_bus(rm) == ["ASRL10::INSTR", "GPIB0::19::INSTR"]


def test_probe_address_ok() -> None:
    instr = FakeInstrument(reply="KEITHLEY,2182A,123,C01")
    rm = FakeResourceManager({"GPIB0::7::INSTR": instr})
    result = probe_address(rm, "GPIB0::7::INSTR")
    assert result.code is FaultCode.OK
    assert result.idn == "KEITHLEY,2182A,123,C01"
    assert instr.closed  # probe must release the resource


def test_probe_address_open_failed() -> None:
    result = probe_address(FakeResourceManager(), "GPIB0::9::INSTR")
    assert result.code is FaultCode.OPEN_FAILED


def test_probe_address_no_response() -> None:
    rm = FakeResourceManager({"ASRL5::INSTR": FakeInstrument(fail=True)})
    result = probe_address(rm, "ASRL5::INSTR")
    assert result.code is FaultCode.NO_RESPONSE


def test_probe_address_garbled_empty_reply() -> None:
    rm = FakeResourceManager({"ASRL5::INSTR": FakeInstrument(reply="  ")})
    result = probe_address(rm, "ASRL5::INSTR", idn_command="V")
    assert result.code is FaultCode.GARBLED_RESPONSE


def test_probe_address_custom_idn_command() -> None:
    instr = FakeInstrument(reply="ILM200 Version 1.08")
    rm = FakeResourceManager({"ASRL11::INSTR": instr})
    probe_address(rm, "ASRL11::INSTR", idn_command="V")
    assert instr.writes == ["V"]


# ── Config preflight classification ───────────────────────────────────────────


def test_check_config_invalid_dir(tmp_path: Path) -> None:
    results = check_config(str(tmp_path / "nope"))
    assert len(results) == 1
    assert results[0].code is FaultCode.CONFIG_INVALID


def test_check_config_unimportable_class(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, {"ghost": {"class": "cryosoft.drivers.nope.Ghost", "address": "X"}}
    )
    results = check_config(config)
    assert results[0].code is FaultCode.CONFIG_INVALID


def test_check_config_all_ok_without_bus(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"acme": {"class": f"{_THIS}.AlwaysUpDriver", "address": "GPIB0::1::INSTR"}},
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.OK
    assert result.idn == "ACME,MODEL1,SN42,9.9"
    assert result.alias == "acme"


def test_check_config_open_failed(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"dead": {"class": f"{_THIS}.OpenFailsDriver", "address": "GPIB0::2::INSTR"}},
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.OPEN_FAILED


def test_check_config_no_response(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"mute": {"class": f"{_THIS}.SilentDriver", "address": "GPIB0::3::INSTR"}},
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.NO_RESPONSE


def test_check_config_driver_error(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"buggy": {"class": f"{_THIS}.BuggyDriver", "address": "GPIB0::4::INSTR"}},
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.DRIVER_ERROR
    assert "RuntimeError" in result.detail


def test_check_config_garbled(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"blank": {"class": f"{_THIS}.EmptyIdnDriver", "address": "GPIB0::5::INSTR"}},
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.GARBLED_RESPONSE


def test_check_config_wrong_idn(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {
            "acme": {
                "class": f"{_THIS}.AlwaysUpDriver",
                "address": "GPIB0::1::INSTR",
                "expect_idn": "LAKESHORE",
            }
        },
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.WRONG_IDN
    assert result.idn  # the actual identity is still reported for diagnosis


def test_check_config_expect_idn_match_is_case_insensitive(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {
            "acme": {
                "class": f"{_THIS}.AlwaysUpDriver",
                "address": "GPIB0::1::INSTR",
                "expect_idn": "model1",
            }
        },
    )
    (result,) = check_config(config)
    assert result.code is FaultCode.OK


def test_check_config_address_not_on_bus(tmp_path: Path) -> None:
    """With a bus scan available, an unlisted+unreachable address is physical."""
    config = make_config(
        tmp_path,
        {"gone": {"class": f"{_THIS}.OpenFailsDriver", "address": "GPIB0::9::INSTR"}},
    )
    rm = FakeResourceManager({"GPIB0::1::INSTR": FakeInstrument()})
    (result,) = check_config(config, rm=rm)
    assert result.code is FaultCode.ADDRESS_NOT_ON_BUS


def test_check_config_ok_but_unlisted_is_reported_in_detail(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"acme": {"class": f"{_THIS}.AlwaysUpDriver", "address": "GPIB0::1::INSTR"}},
    )
    (result,) = check_config(config, rm=FakeResourceManager())
    assert result.code is FaultCode.OK
    assert "not listed" in result.detail


def test_check_config_shipped_sim_cryostat_preflights_green() -> None:
    """Integration: the always-safe shipped config passes its own preflight."""
    import cryosoft

    config = Path(cryosoft.__file__).parent / "configs" / "sim_cryostat"
    results = check_config(str(config))
    assert results, "sim_cryostat declares no drivers?"
    failed = [r for r in results if not r.ok]
    assert not failed, [(r.alias, r.code, r.detail) for r in failed]


# ── DriverBench ───────────────────────────────────────────────────────────────


@pytest.fixture()
def sim_bench(tmp_path: Path) -> DriverBench:
    config = make_config(
        tmp_path,
        {
            "meter": {
                "class": "cryosoft.drivers.sim_keithley_2182a.SimKeithley2182A",
                "address": "SIM::BENCH",
            }
        },
    )
    return DriverBench.from_config(config, "meter")


def test_bench_unknown_alias_lists_available(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        {"meter": {"class": f"{_THIS}.AlwaysUpDriver", "address": "X"}},
    )
    with pytest.raises(Exception, match="meter"):
        DriverBench.from_config(config, "typo")


def test_bench_list_methods_classifies_read_only(sim_bench: DriverBench) -> None:
    methods = {m.name: m for m in sim_bench.list_methods()}
    assert methods["get_voltage"].read_only
    assert not methods["set_range"].read_only
    assert methods["set_range"].params == {"range_v": "float"}
    assert "(" in methods["set_range"].signature


def test_bench_read_call(sim_bench: DriverBench) -> None:
    voltage = sim_bench.call("get_voltage")
    assert isinstance(voltage, float)


def test_bench_write_requires_allow_write(sim_bench: DriverBench) -> None:
    with pytest.raises(ValueError, match="changes instrument state"):
        sim_bench.call("set_range", ["0.1"])


def test_bench_write_with_allow_write_coerces_args(sim_bench: DriverBench) -> None:
    sim_bench.call("set_range", ["0.1"], allow_write=True)
    assert sim_bench.call("get_range") == pytest.approx(0.1)


def test_bench_unknown_method_reports_available(sim_bench: DriverBench) -> None:
    with pytest.raises(ValueError, match="get_voltage"):
        sim_bench.call("get_voltag")


def test_bench_private_method_refused(sim_bench: DriverBench) -> None:
    with pytest.raises(ValueError, match="[Pp]rivate"):
        sim_bench.call("_check_error")


def test_bench_bad_argument_names_the_parameter(sim_bench: DriverBench) -> None:
    with pytest.raises(ValueError, match="range_v"):
        sim_bench.call("set_range", ["not-a-number"], allow_write=True)


def test_bench_raw_io_refused_without_visa_handle(sim_bench: DriverBench) -> None:
    with pytest.raises(ValueError, match="raw VISA handle"):
        sim_bench.query("*IDN?")


def test_bench_raw_io_uses_underlying_handle() -> None:
    class HandleDriver:
        def __init__(self) -> None:
            self._instr = FakeInstrument(reply="RAW,OK")

    driver = HandleDriver()
    bench = DriverBench(driver, alias="raw", address="X", class_path="t.HandleDriver")
    assert bench.query("*IDN?") == "RAW,OK"
    bench.send(":OUTP OFF")
    assert driver._instr.writes == ["*IDN?", ":OUTP OFF"]
    bench.close()
    assert driver._instr.closed


def test_bench_from_class_ad_hoc() -> None:
    bench = DriverBench.from_class(f"{_THIS}.AlwaysUpDriver", "GPIB0::30::INSTR")
    assert bench.call("get_idn") == "ACME,MODEL1,SN42,9.9"
    assert bench.alias == "AlwaysUpDriver"


def test_is_read_only_prefixes() -> None:
    assert is_read_only("get_voltage")
    assert is_read_only("ping")
    assert not is_read_only("set_range")
    assert not is_read_only("initiate")
