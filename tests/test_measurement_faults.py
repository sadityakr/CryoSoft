"""Behaviour tests for the measurement-fault standard, layer by layer.

GLOSSARY.md's **Measurement fault**: an instrument that says a reading is
not a measurement (compliance, overflow, no data) makes ``take_reading()``
raise ``CryoSoftMeasurementError``; the Orchestrator fails the run on the
first such datapoint (source parked, reason shown, queue held) and records
an advisory ``measurement:<vi>`` condition that the next
``initiate_measurement`` clears. Nothing is locked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cryosoft.core.exceptions import (
    MEASUREMENT_FAULT_FLAGS,
    CryoSoftCommunicationError,
    CryoSoftMeasurementError,
)
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import Command
from cryosoft.core.station import build_station
from cryosoft.drivers.keithley_2182a import Keithley2182A
from cryosoft.drivers.keithley_6221 import Keithley6221
from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221
from cryosoft.procedures.time_series import TimeSeries
from cryosoft.virtual_instruments.measurement.measurement_dc_mode import DCModeMeasurementVI
from cryosoft.virtual_instruments.measurement.measurement_delta_mode import (
    DeltaModeMeasurementVI,
)

CONFIG_PATH = "cryosoft/configs/sim_cryostat"


# ── The typed error ──────────────────────────────────────────────────────────


def test_error_rejects_a_flag_outside_the_vocabulary():
    with pytest.raises(ValueError, match="unknown measurement-fault flag"):
        CryoSoftMeasurementError("melted", "no")


def test_error_is_not_a_communication_error():
    """Bus-tolerant code catches CryoSoftCommunicationError; it must not swallow this."""
    assert not issubclass(CryoSoftMeasurementError, CryoSoftCommunicationError)
    exc = CryoSoftMeasurementError("compliance", "msg", context="ctx")
    assert (exc.flag, exc.context, exc.vi_name) == ("compliance", "ctx", "")


# ── L0 sims ──────────────────────────────────────────────────────────────────


def test_sim_2182a_overflows_above_120_percent_of_range():
    meter = SimKeithley2182A("SIM")
    meter._noise_std = 0.0
    meter.set_range(0.01)
    meter._base_voltage = 0.011          # within the 120 % headroom
    assert meter.get_voltage() == pytest.approx(0.011)
    meter._base_voltage = 0.02           # over range
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        meter.get_voltage()
    assert excinfo.value.flag == "overflow"


def test_sim_2182a_autorange_does_not_overflow_a_signal_a_larger_range_holds():
    meter = SimKeithley2182A("SIM")
    meter.set_range(0.01)
    meter.set_auto_range(True)
    meter._base_voltage = 5.0
    assert meter.get_voltage() == pytest.approx(5.0, abs=1e-6)


def _paired_source(meter: SimKeithley2182A | None = None) -> SimKeithley6221:
    source = SimKeithley6221("SIM")
    source._paired_meter = meter or SimKeithley2182A("SIM")
    return source


def test_sim_6221_delta_compliance_abort_stops_the_engine():
    """1 mA into the 1500 ohm sample needs 1.5 V: over a 1 V compliance."""
    source = _paired_source()
    source.configure_and_start_delta(high_current=1e-3, n_readings=5, delay=0.0, compliance=1.0)
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        source.acquire_delta_readings(5)
    assert excinfo.value.flag == "compliance"
    assert source._delta_armed is False


def test_sim_6221_delta_without_compliance_abort_keeps_running():
    source = _paired_source()
    source.configure_and_start_delta(
        high_current=1e-3, n_readings=3, delay=0.0, compliance=1.0, compliance_abort=False
    )
    assert len(source.acquire_delta_readings(3)) == 3


def test_sim_6221_delta_with_no_reading_is_no_data():
    source = _paired_source()
    source.configure_and_start_delta(high_current=1e-6, n_readings=5, delay=0.0, compliance=1.0)
    source._delta_return_count = 0
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        source.acquire_delta_readings(5)
    assert excinfo.value.flag == "no_data"


# ── L0 real drivers (VISA bypassed) ──────────────────────────────────────────


def _real(cls, query_replies):
    driver = object.__new__(cls)
    instr = MagicMock()
    instr.query.side_effect = query_replies
    instr.timeout = 2000
    driver._instr = instr
    return driver


def test_real_2182a_overflow_marker_is_never_returned_as_a_voltage():
    driver = _real(Keithley2182A, ["+9.9E37"])
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        driver.get_voltage()
    assert excinfo.value.flag == "overflow"


def test_real_2182a_normal_reading_passes_through():
    driver = _real(Keithley2182A, ["+1.234E-06"])
    assert driver.get_voltage() == pytest.approx(1.234e-6)


def test_real_6221_relay_overflow(monkeypatch):
    monkeypatch.setattr("cryosoft.drivers.keithley_6221.time.sleep", lambda s: None)
    driver = _real(Keithley6221, ["+9.9E37"])
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        driver.get_voltage()
    assert excinfo.value.flag == "overflow"


def test_real_6221_delta_compliance_is_reported_after_one_failed_poll(monkeypatch):
    """The fix for the ~30 s freeze: one timed-out poll, then the latched reason.

    Replies in order: EVENt? cleared at start ("0"), the first FRES? poll
    times out, EVENt? reports bit 1 (compliance) -> raise immediately,
    instead of two more 10 s timeouts.
    """
    monkeypatch.setattr("cryosoft.drivers.keithley_6221.time.sleep", lambda s: None)
    driver = _real(Keithley6221, ["0", TimeoutError("VI_ERROR_TMO"), "2"])
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        driver.acquire_delta_readings(5, 0.0)
    assert excinfo.value.flag == "compliance"
    assert driver._instr.query.call_count == 3


def test_real_6221_delta_no_reading_is_no_data(monkeypatch):
    monkeypatch.setattr("cryosoft.drivers.keithley_6221.time.sleep", lambda s: None)
    # start EVENt?, then 3 x (empty poll, EVENt? clean)
    driver = _real(Keithley6221, ["0", "", "0", "", "0", "", "0"])
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        driver.acquire_delta_readings(5, 0.0)
    assert excinfo.value.flag == "no_data"


def test_real_6221_delta_overflow_in_the_buffer(monkeypatch):
    monkeypatch.setattr("cryosoft.drivers.keithley_6221.time.sleep", lambda s: None)
    driver = _real(Keithley6221, ["0", "+9.9E37"])
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        driver.acquire_delta_readings(5, 0.0)
    assert excinfo.value.flag == "overflow"


def test_real_6221_delta_normal_readings_unaffected(monkeypatch):
    monkeypatch.setattr("cryosoft.drivers.keithley_6221.time.sleep", lambda s: None)
    driver = _real(Keithley6221, ["0", "1.0E-06", "2.0E-06"])
    assert driver.acquire_delta_readings(2, 0.0) == [1e-6, 2e-6]


# ── L1 measurement VIs ───────────────────────────────────────────────────────


def _vi(cls, *, paired: bool = True):
    meter = SimKeithley2182A("SIM")
    source = SimKeithley6221("SIM")
    if paired:
        source._paired_meter = meter
    return cls({"source": source, "meter": meter}), source, meter


def test_delta_vi_compliance_message_carries_the_settings():
    vi, *_ = _vi(DeltaModeMeasurementVI)
    vi.initiate_measurement(current=1e-3, n_readings=5, compliance_V=1.0, delay_s=0.0)
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        vi.take_reading()
    assert excinfo.value.flag == "compliance"
    assert "delta current 0.001 A" in str(excinfo.value)
    assert "compliance 1 V" in str(excinfo.value)


def test_dc_vi_overflow_from_the_meter():
    vi, _source, meter = _vi(DCModeMeasurementVI, paired=False)
    vi.initiate_measurement(current=1e-6, n_readings=3, voltmeter_range_V=0.01, delay_s=0.0)
    meter._base_voltage = 1.0
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        vi.take_reading()
    assert excinfo.value.flag == "overflow"


def test_dc_vi_no_sample_at_all_is_no_data():
    vi, _source, meter = _vi(DCModeMeasurementVI, paired=False)
    vi.initiate_measurement(current=1e-6, n_readings=3, delay_s=0.0)
    meter._simulate_error = True
    with pytest.raises(CryoSoftMeasurementError) as excinfo:
        vi.take_reading()
    assert excinfo.value.flag == "no_data"


def test_dc_vi_compliance_abort_off_keeps_measuring():
    """compliance_abort=False is the operator's choice: no fault is raised."""
    vi, *_ = _vi(DCModeMeasurementVI)
    vi.initiate_measurement(
        current=1e-3, n_readings=2, compliance_V=1.0, voltmeter_range_V=0.0,
        compliance_abort=False, delay_s=0.0,
    )
    assert vi.take_reading()["n_valid"] == 2


def test_declared_flags_are_the_ones_raised():
    for cls in (DCModeMeasurementVI, DeltaModeMeasurementVI):
        assert cls.measurement_fault_flags == {"compliance", "overflow", "no_data"}
        assert cls.measurement_fault_flags <= MEASUREMENT_FAULT_FLAGS


# ── L2 Station: the advisory record and its clearing ─────────────────────────


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


def test_record_is_advisory_and_scoped_to_the_vi(station):
    station.record_measurement_fault("keithley_dc_mode", "compliance", "in compliance")
    cond = station.conditions()["measurement:keithley_dc_mode"]
    assert (cond.origin, cond.severity, cond.kind) == ("measurement", "advisory", "compliance")
    assert cond.source_vis == ("keithley_dc_mode",)


def test_manual_arm_clears_the_record(station):
    station.record_measurement_fault("keithley_dc_mode", "compliance", "in compliance")
    station.execute_vi_action(
        "keithley_dc_mode", "initiate_measurement", allowed_scope="operation",
        current=1e-6, n_readings=1,
    )
    assert "measurement:keithley_dc_mode" not in station.conditions()


def test_run_arm_clears_the_record_and_other_methods_do_not(station):
    station.record_measurement_fault("keithley_dc_mode", "overflow", "over range")
    station.send_measurement_commands(
        (Command("keithley_dc_mode", "initiate", {}),)
    )
    assert "measurement:keithley_dc_mode" in station.conditions()
    station.send_measurement_commands(
        (Command("keithley_dc_mode", "initiate_measurement", {"current": 1e-6, "n_readings": 1}),)
    )
    assert "measurement:keithley_dc_mode" not in station.conditions()


# ── L3/L4 integration: a real run into compliance ────────────────────────────


def _delta_time_series(station, tmp_path, current: float) -> TimeSeries:
    return TimeSeries(
        station=station,
        sample_info={"sample_name": "S", "sample_id": "MF-1", "comments": ""},
        data_directory=str(tmp_path),
        step_time_s=0.001,
        max_duration_s=0.003,
        measurement_vi="keithley_delta_mode",
        current=current,
        n_readings=3,
        voltmeter_range_V=0.1,
        compliance_V=1.0,
        delay_s=0.0,
        compliance_abort=True,
        cold_switch=False,
    )


def _tick_until(orch, predicate, max_ticks: int = 2000):
    for _ in range(max_ticks):
        orch._tick()
        if predicate():
            return
    raise AssertionError("predicate never became true")


def test_run_into_compliance_fails_with_the_reason_and_nothing_stays_locked(
    station, tmp_path, qtbot
):
    vi = station.get_vi("keithley_delta_mode")
    vi._source._paired_meter = vi._meter   # model the sample: 1500 ohm
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.start_monitoring()
    finished: list[dict] = []
    errors: list = []
    orch.run_finished.connect(finished.append)
    orch.error_event.connect(errors.append)
    try:
        orch.run_procedure(_delta_time_series(station, tmp_path, current=1e-3))
        _tick_until(orch, lambda: bool(finished))

        # Fail and stop: the run failed with the reason, back to IDLE (not
        # ERROR), the engine no longer sourcing. (The queue hold is the shared
        # run-failure path's, covered with a queue in test_l3_orchestrator.)
        manifest = finished[-1]
        assert manifest["status"] == "failed"
        assert "compliance" in manifest["reason"]
        assert "keithley_delta_mode" in manifest["reason"]
        assert "datapoint 1" in manifest["reason"]
        assert orch._state == OrchestratorState.IDLE
        assert vi._source._delta_armed is False
        failure = [e for e in errors if e.kind == "run_failure"][-1]
        assert failure.vi_name == "keithley_delta_mode"

        # The advisory record answers "why did it stop".
        cond = station.conditions()["measurement:keithley_delta_mode"]
        assert (cond.severity, cond.kind) == ("advisory", "compliance")

        # Not locked: a manual action is admitted at once.
        admitted, _ = orch._manual_action_admissible("keithley_delta_mode")
        assert admitted is True

        # The next run at a current the sample can carry arms (clearing the
        # record) and completes.
        finished.clear()
        orch.run_procedure(_delta_time_series(station, tmp_path, current=1e-6))
        _tick_until(orch, lambda: bool(finished))
        assert finished[-1]["status"] == "done"
        assert "measurement:keithley_delta_mode" not in station.conditions()
    finally:
        orch.shutdown()
