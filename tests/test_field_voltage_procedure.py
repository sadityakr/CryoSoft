import h5py
import pytest

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import build_station
from cryosoft.core.sweep_builder import SweepSegment
from cryosoft.procedures.field_sweep import FieldSweep

CONFIG_PATH = "cryosoft/configs/sim_real_cryostat"

SAMPLE_INFO = {
    "sample_name": "Test Sample",
    "sample_id": "T-001",
    "comments": "field-voltage measurement end-to-end test",
}


@pytest.fixture
def station():
    s = build_station(CONFIG_PATH)
    # Fast ramp + short heater dwell so the test completes quickly. This only
    # changes timing, not the switch-heater-aware sequence itself.
    s.magnet_z._default_ramp_rate = 6000.0
    s.magnet_z._ramp_segments = []
    s.magnet_z._heater.warmup_s = 0.0
    s.magnet_z._heater.cooldown_s = 0.0
    return s


def test_field_voltage_sweep_full_orchestrator_loop(station, tmp_path, qtbot):
    """FieldSweep runs a piecewise + hysteresis field sweep against the
    simulated Mercury iPS-M / Keithley delta-mode setup, keeping the switch
    heater energised across all sweep points and correctly reporting field
    values throughout (regression coverage for both fixed bugs), then parks
    fully (zero field, heater off) via magnet_z's own standby() at finish."""
    segments = [
        SweepSegment(start=-0.1, end=-0.02, step=0.04),
        SweepSegment(start=-0.02, end=0.02, step=0.02),
        SweepSegment(start=0.02, end=0.1, step=0.04),
    ]

    procedure = FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        measurement_vi="keithley_delta_mode",
        field_mode="segments",
        field_segments=segments,
        field_hysteresis=True,
        temperature=300.0,  # sim VTI driver starts at 300 K -> instant settle
        current=1e-6,
        n_readings=3,
        init_wait=0.0,
        step_wait=0.0,
    )
    expected_sweep = procedure.get_sweep_array()
    expected_n_points = len(expected_sweep)
    assert expected_n_points > len(segments)  # hysteresis actually doubled it back

    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(procedure)

    with qtbot.waitSignal(orch.procedure_finished, timeout=20000):
        pass

    assert orch._state == OrchestratorState.IDLE
    assert procedure._index == expected_n_points

    # The procedure ran under automatic heater management the whole way
    # (never the manual persistent-mode toggle), so the switch heater stayed
    # energised at every sweep point. At finish, standby() commands
    # magnet_z's own standby() — a full park (zero field, heater off) —
    # rather than setting a field target itself.
    assert station.magnet_z.persistent_mode_enabled() is False
    assert station.magnet_z.switch_heater_state() == "OFF"
    assert station.magnet_z.magnet_field_T() == pytest.approx(0.0, abs=1e-3)

    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert f["metadata"].attrs["procedure_name"] == "Field Sweep"
        field_T = f["data"]["field_T"][:]
        voltage_V = f["data"]["voltage_V"][:]

        assert field_T.shape[0] == expected_n_points
        # Regression: get_field() must track each point's real ramped value,
        # not collapse to ~0 for every point (the bug this fix addresses had
        # every reading read back near 0 once the magnet's persistent-mode
        # PSU-zero step fired after each ramp).
        assert field_T.max() == pytest.approx(max(expected_sweep), abs=0.01)
        assert field_T.min() == pytest.approx(min(expected_sweep), abs=0.01)
        assert len(set(round(v, 4) for v in field_T)) > 1
        assert not (voltage_V == 0.0).all()
