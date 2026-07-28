# ---
# description: |
#   Unit tests for TensormeterRTM2MeasurementVI (L1 Virtual Instruments layer).
#   Covers both operating modes: the default internal mode (CryoSoft owns
#   excitation/analysis/routing) and the externally configured mode (the
#   externally configured standard on MeasurementInstrumentBase — a vendor
#   tool owns excitation/analysis/routing, CryoSoft only arms the data path,
#   triggers, reads, and saves). Exercises the detached-idle lifecycle,
#   n_valid under-delivery accounting, and tensor_component column
#   selection, against the sim driver.
# entry_point: pytest tests/test_tensormeter_measurement_vi.py -v
# last_updated: 2026-07-27
# ---

from __future__ import annotations

import math

import pytest

from cryosoft.core.exceptions import CryoSoftCommunicationError
from cryosoft.drivers.sim_tensormeter_rtm2 import SimTensormeterRTM2, _DATA_COLUMNS
from cryosoft.virtual_instruments.measurement.tensormeter_rtm2_measurement import (
    _RAW_CHANNEL_NAMES,
    TensormeterRTM2MeasurementVI,
)

_EXPECTED_KEYS = {
    "res_a_ohm_array", "res_a_ohm", "res_a_ohm_error",
    "res_b_ohm_array", "res_b_ohm", "res_b_ohm_error",
    "n_valid", "raw_channels_block",
}


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture()
def driver() -> SimTensormeterRTM2:
    return SimTensormeterRTM2("SIM")


@pytest.fixture()
def vi(driver: SimTensormeterRTM2) -> TensormeterRTM2MeasurementVI:
    """Default (internal) mode VI — configured_externally omitted."""
    return TensormeterRTM2MeasurementVI({"tensormeter": driver})


@pytest.fixture()
def external_vi_and_driver():
    """Externally configured VI + its (born-detached) driver."""
    d = SimTensormeterRTM2("SIM")
    inst = TensormeterRTM2MeasurementVI(
        {"tensormeter": d}, configured_externally=True
    )
    return inst, d


# ------------------------------------------------------------------
# take_reading before initiate must raise
# ------------------------------------------------------------------

def test_take_reading_before_initiate_raises(vi: TensormeterRTM2MeasurementVI) -> None:
    with pytest.raises(RuntimeError, match=r"initiate_measurement\(\)"):
        vi.take_reading()


# ------------------------------------------------------------------
# 8. Backward compat: default (internal) mode runs the full existing
#    setup sequence unchanged.
# ------------------------------------------------------------------

def test_internal_mode_writes_excitation_and_routing(
    vi: TensormeterRTM2MeasurementVI, driver: SimTensormeterRTM2
) -> None:
    vi.initiate_measurement(
        current_amplitude_A=2e-3, averaging_time_s=0.01,
        analysis_mode="kelvin", readings_per_point=3,
    )
    # A couple of the internal-mode driver writes this VI has always made.
    assert driver._control_mode == 1
    assert driver._waveform_mode == 0
    assert driver._analysis_mode == 1  # "kelvin"
    assert driver._current_amplitude_A == pytest.approx(2e-3)
    assert driver._averaging_time_s == pytest.approx(0.01)


def test_internal_mode_take_reading_shape(vi: TensormeterRTM2MeasurementVI) -> None:
    vi.initiate_measurement(readings_per_point=4)
    data = vi.take_reading()
    assert set(data.keys()) == _EXPECTED_KEYS
    assert len(data["res_a_ohm_array"]) == 4
    assert len(data["res_b_ohm_array"]) == 4
    assert data["n_valid"] == 4


# ------------------------------------------------------------------
# raw_channels_block (see MeasurementInstrumentBase's "Raw diagnostic
# blocks" standard) — all 44 raw driver columns preserved per reading,
# alongside the tensor_component-selected res_a_ohm/res_b_ohm pair.
# ------------------------------------------------------------------

def test_raw_channels_block_shape(vi: TensormeterRTM2MeasurementVI) -> None:
    vi.initiate_measurement(readings_per_point=4)
    data = vi.take_reading()
    block = data["raw_channels_block"]
    assert len(block) == 4
    for row in block:
        assert len(row) == 44


def test_raw_block_row_counts_declares_readings_per_point(
    vi: TensormeterRTM2MeasurementVI,
) -> None:
    counts = vi.raw_block_row_counts({"readings_per_point": 7})
    assert counts == {"raw_channels_block": 7}


def test_measurement_raw_blocks_declares_44_channel_names() -> None:
    labels = TensormeterRTM2MeasurementVI.measurement_raw_blocks["raw_channels_block"]
    assert len(labels) == 44
    assert labels == list(_RAW_CHANNEL_NAMES)


def test_raw_channels_block_pads_short_delivery_on_row_axis_only(
    vi: TensormeterRTM2MeasurementVI, driver: SimTensormeterRTM2
) -> None:
    """Row-axis NaN padding mirrors res_a_ohm_array's — channel axis never pads."""
    vi.initiate_measurement(readings_per_point=5)

    real_read_new_data = driver.read_new_data

    def truncated_read_new_data(timeout: float | None = None) -> list[dict[str, float]]:
        return real_read_new_data(timeout)[:2]

    driver.read_new_data = truncated_read_new_data  # type: ignore[method-assign]

    data = vi.take_reading()
    block = data["raw_channels_block"]

    assert len(block) == 5
    for row in block[:2]:
        assert len(row) == 44
        assert not any(math.isnan(v) for v in row)
    for row in block[2:]:
        assert len(row) == 44
        assert all(math.isnan(v) for v in row)


def test_raw_channels_block_agrees_with_res_a_res_b_arrays(
    vi: TensormeterRTM2MeasurementVI,
) -> None:
    """The block's res_a/res_b columns match the extracted mean/error/array pair."""
    vi.initiate_measurement(readings_per_point=4, tensor_component="1st_re")
    data = vi.take_reading()

    idx_a = _RAW_CHANNEL_NAMES.index("res_a_1st_re_ohm")
    idx_b = _RAW_CHANNEL_NAMES.index("res_b_1st_re_ohm")
    for i, row in enumerate(data["raw_channels_block"]):
        assert row[idx_a] == pytest.approx(data["res_a_ohm_array"][i])
        assert row[idx_b] == pytest.approx(data["res_b_ohm_array"][i])


def test_internal_mode_standby_zeros_current(
    vi: TensormeterRTM2MeasurementVI, driver: SimTensormeterRTM2
) -> None:
    vi.initiate_measurement(current_amplitude_A=3e-3)
    vi.standby()
    assert driver._current_amplitude_A == pytest.approx(0.0)
    assert vi._initiated is False
    # Internal mode never releases the connection.
    assert driver._closed is False


def test_internal_mode_born_attached(driver: SimTensormeterRTM2) -> None:
    TensormeterRTM2MeasurementVI({"tensormeter": driver})
    assert driver._closed is False


# ------------------------------------------------------------------
# 5b. Detached-idle lifecycle
# ------------------------------------------------------------------

def test_external_mode_born_detached(external_vi_and_driver) -> None:
    _, d = external_vi_and_driver
    assert d._closed is True


def test_external_mode_ping_verifies_and_releases(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    assert inst.ping() is True
    assert d._closed is True  # released again after the round trip


def test_external_mode_ping_false_when_hung(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d._hung = True
    assert inst.ping() is False
    assert d._closed is True  # still released, even on failure


def test_external_mode_full_reacquire_cycle(external_vi_and_driver) -> None:
    """initiate -> read -> standby -> initiate again works (reacquire)."""
    inst, d = external_vi_and_driver
    d._averaging_time_s = 0.0  # keep the test fast

    inst.initiate_measurement(readings_per_point=2)
    assert d._closed is False
    data = inst.take_reading()
    assert set(data.keys()) == _EXPECTED_KEYS
    inst.standby()
    assert d._closed is True

    # Reacquire.
    inst.initiate_measurement(readings_per_point=2)
    assert d._closed is False
    data2 = inst.take_reading()
    assert set(data2.keys()) == _EXPECTED_KEYS
    inst.standby()
    assert d._closed is True


def test_driver_call_while_detached_raises(external_vi_and_driver) -> None:
    _, d = external_vi_and_driver
    with pytest.raises(CryoSoftCommunicationError):
        d.get_idn()


# ------------------------------------------------------------------
# 1. External mode preserves external state
# ------------------------------------------------------------------

def test_external_mode_preserves_excitation_state(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    # "External tool" (e.g. TMCS) state, set directly on the sim.
    d.ensure_connected()
    d._analysis_mode = 4  # ratiometric
    d._current_amplitude_A = 7e-3
    d._averaging_time_s = 0.02
    d._control_mode = 1
    d.close()  # back to detached, as __init__ left it

    inst.initiate_measurement(
        # Within the configured safety limit (control_limits is enforced
        # structurally by @control regardless of mode) but distinct from
        # the externally-set 7e-3 the sim already holds — must be ignored.
        current_amplitude_A=5e-3,
        analysis_mode="differential",  # must be ignored
        averaging_time_s=0.0,  # must be ignored
        readings_per_point=3,
    )

    assert d._analysis_mode == 4
    assert d._current_amplitude_A == pytest.approx(7e-3)
    assert d._averaging_time_s == pytest.approx(0.02)
    assert d._control_mode == 1
    # Buffer cleared at arming.
    assert d._data_buffer == []

    data = inst.take_reading()
    assert set(data.keys()) == _EXPECTED_KEYS
    assert len(data["res_a_ohm_array"]) == 3


# ------------------------------------------------------------------
# 2. Timing readback
# ------------------------------------------------------------------

def test_external_mode_timing_readback(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d.ensure_connected()
    d._averaging_time_s = 0.321
    d.close()

    inst.initiate_measurement(averaging_time_s=0.05)  # ignored value

    assert inst._averaging_time_s == pytest.approx(0.321)


# ------------------------------------------------------------------
# 3. Channel-selection re-assert
# ------------------------------------------------------------------

def test_external_mode_reasserts_full_channel_selection(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d.ensure_connected()
    d.select_data_channels(12, 22, 33)  # leftover non-default selection
    d.close()

    inst.initiate_measurement(readings_per_point=2)

    assert d._selected_channels is None  # default restored
    data = inst.take_reading()  # would KeyError if mis-keyed
    assert set(data.keys()) == _EXPECTED_KEYS


# ------------------------------------------------------------------
# 4. Snapshot
# ------------------------------------------------------------------

def test_external_mode_snapshot_captured(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d.ensure_connected()
    d._current_amplitude_A = 9e-3
    d.close()

    inst.initiate_measurement()

    assert isinstance(inst.last_settings_snapshot, dict)
    assert inst.last_settings_snapshot["camp"] == pytest.approx(9e-3)


def test_internal_mode_has_no_snapshot(vi: TensormeterRTM2MeasurementVI) -> None:
    assert vi.last_settings_snapshot is None
    vi.initiate_measurement()
    # Internal mode never captures a snapshot — nothing external to record.
    assert vi.last_settings_snapshot is None


# ------------------------------------------------------------------
# 5. Standby in external mode
# ------------------------------------------------------------------

def test_external_mode_standby_does_not_zero_current(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d.ensure_connected()
    d._current_amplitude_A = 5e-3
    d.close()

    inst.initiate_measurement()
    inst.standby()

    assert d._current_amplitude_A == pytest.approx(5e-3)
    assert inst._initiated is False
    assert d._closed is True


# ------------------------------------------------------------------
# 6. Liveness
# ------------------------------------------------------------------

def test_external_mode_initiate_raises_when_hung(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d._hung = True
    with pytest.raises(CryoSoftCommunicationError):
        inst.initiate_measurement()


# ------------------------------------------------------------------
# 7. n_valid: under-delivery and full delivery
# ------------------------------------------------------------------

def test_full_delivery_n_valid_equals_readings_per_point(
    vi: TensormeterRTM2MeasurementVI,
) -> None:
    vi.initiate_measurement(readings_per_point=6)
    data = vi.take_reading()
    assert data["n_valid"] == 6
    assert not any(math.isnan(v) for v in data["res_a_ohm_array"])
    assert not any(math.isnan(v) for v in data["res_b_ohm_array"])


def test_under_delivery_pads_and_reports_n_valid(
    vi: TensormeterRTM2MeasurementVI, driver: SimTensormeterRTM2
) -> None:
    vi.initiate_measurement(readings_per_point=5)

    # Simulate the free-running instrument delivering fewer rows than
    # requested within the settle window: truncate what read_new_data()
    # returns, without touching the sim driver module itself.
    real_read_new_data = driver.read_new_data

    def truncated_read_new_data(timeout: float | None = None) -> list[dict[str, float]]:
        return real_read_new_data(timeout)[:2]

    driver.read_new_data = truncated_read_new_data  # type: ignore[method-assign]

    data = vi.take_reading()

    assert data["n_valid"] == 2
    assert len(data["res_a_ohm_array"]) == 5
    assert len(data["res_b_ohm_array"]) == 5
    assert not math.isnan(data["res_a_ohm_array"][0])
    assert not math.isnan(data["res_a_ohm_array"][1])
    assert math.isnan(data["res_a_ohm_array"][2])
    assert math.isnan(data["res_a_ohm_array"][3])
    assert math.isnan(data["res_a_ohm_array"][4])
    assert not math.isnan(data["res_a_ohm"])  # mean computed over the 2 valid


# ------------------------------------------------------------------
# 7b. tensor_component column selection
# ------------------------------------------------------------------

def _fake_row_with_distinct_values() -> dict[str, float]:
    row = dict.fromkeys(_DATA_COLUMNS, 0.0)
    row["res_a_dc_ohm"], row["res_b_dc_ohm"] = 111.0, 211.0
    row["res_a_1st_im_ohm"], row["res_b_1st_im_ohm"] = 333.0, 433.0
    row["res_a_2nd_re_ohm"], row["res_b_2nd_re_ohm"] = 555.0, 655.0
    return row


@pytest.mark.parametrize(
    "component,expected_a,expected_b",
    [("dc", 111.0, 211.0), ("1st_im", 333.0, 433.0), ("2nd_re", 555.0, 655.0)],
)
def test_tensor_component_extracts_matching_columns(
    vi: TensormeterRTM2MeasurementVI,
    driver: SimTensormeterRTM2,
    component: str,
    expected_a: float,
    expected_b: float,
) -> None:
    fake_row = _fake_row_with_distinct_values()
    driver.read_new_data = lambda timeout=None: [fake_row]  # type: ignore[method-assign]

    vi.initiate_measurement(
        readings_per_point=1, averaging_time_s=0.0, tensor_component=component
    )
    data = vi.take_reading()

    assert data["res_a_ohm"] == pytest.approx(expected_a)
    assert data["res_b_ohm"] == pytest.approx(expected_b)
    assert set(data.keys()) == _EXPECTED_KEYS


def test_tensor_component_works_in_external_mode(external_vi_and_driver) -> None:
    inst, d = external_vi_and_driver
    d.ensure_connected()
    d._averaging_time_s = 0.0
    d.close()

    fake_row = _fake_row_with_distinct_values()
    d.read_new_data = lambda timeout=None: [fake_row]  # type: ignore[method-assign]

    inst.initiate_measurement(readings_per_point=1, tensor_component="2nd_re")
    data = inst.take_reading()

    assert data["res_a_ohm"] == pytest.approx(555.0)
    assert data["res_b_ohm"] == pytest.approx(655.0)


def test_unknown_tensor_component_raises(vi: TensormeterRTM2MeasurementVI) -> None:
    with pytest.raises(ValueError, match="tensor_component"):
        vi.initiate_measurement(tensor_component="not_a_component")


# ------------------------------------------------------------------
# @control decoration (GUI discoverability)
# ------------------------------------------------------------------

def test_initiate_is_control(vi: TensormeterRTM2MeasurementVI) -> None:
    assert getattr(vi.initiate_measurement, "_is_control", False) is True


def test_initiate_control_params_include_tensor_component(
    vi: TensormeterRTM2MeasurementVI,
) -> None:
    params = getattr(vi.initiate_measurement, "_control_params", {})
    assert "tensor_component" in params


# ------------------------------------------------------------------
# externally_owned_parameters / active_measurement_parameters /
# reading_parameters — the externally-configured standard's self-description
# (see MeasurementInstrumentBase's "Externally configured instruments"
# section). The procedure form and the reading-loop registry both derive
# from this ClassVar automatically.
# ------------------------------------------------------------------

_EXTERNALLY_OWNED = {
    "current_amplitude_A", "averaging_time_s", "analysis_mode", "switch_sequence",
}


def test_active_measurement_parameters_internal_mode_is_full_set(
    vi: TensormeterRTM2MeasurementVI,
) -> None:
    """Internal mode: active_measurement_parameters is measurement_parameters, unchanged."""
    assert vi.configured_externally is False
    assert (
        vi.active_measurement_parameters == TensormeterRTM2MeasurementVI.measurement_parameters
    )


def test_active_measurement_parameters_external_mode_hides_owned_four(
    external_vi_and_driver,
) -> None:
    """External mode omits exactly the four excitation/analysis/routing params."""
    inst, _d = external_vi_and_driver
    assert inst.configured_externally is True
    active = inst.active_measurement_parameters
    assert set(active) == set(TensormeterRTM2MeasurementVI.measurement_parameters) - _EXTERNALLY_OWNED
    # Data-path parameters stay active — they write nothing to the instrument.
    assert "tensor_component" in active
    assert "readings_per_point" in active
    for name in _EXTERNALLY_OWNED:
        assert name not in active


def test_reading_parameters_excludes_externally_owned_in_external_mode(
    driver: SimTensormeterRTM2,
) -> None:
    """A loopable-but-externally-owned parameter disappears from reading_parameters.

    Uses a small in-test subclass declaring a reading_setters entry for a
    parameter that is ALSO externally owned — RTM2 itself declares no
    reading_setters — so the loop-protection standard (see
    MeasurementInstrumentBase.reading_parameters) can be exercised: without
    it, a reading loop over this parameter in external mode would dispatch
    setter WRITES to the instrument mid-run, clobbering the external tool's
    configuration.
    """

    class _LoopableExternalVI(TensormeterRTM2MeasurementVI):
        reading_setters = {"current_amplitude_A": "set_current_amplitude"}

        def set_current_amplitude(self, current_amplitude_A: float) -> None:
            pass

    internal_vi = _LoopableExternalVI({"tensormeter": driver})
    assert "current_amplitude_A" in internal_vi.reading_parameters

    external_driver = SimTensormeterRTM2("SIM")
    external_vi = _LoopableExternalVI(
        {"tensormeter": external_driver}, configured_externally=True
    )
    assert "current_amplitude_A" not in external_vi.reading_parameters
