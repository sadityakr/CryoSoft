# ---
# description: |
#   Integration tests for Layer 4 (Procedures). Tests BaseProcedure subclassing,
#   FieldSweep method contracts, DataManager integration, and a full
#   Orchestrator end-to-end loop with FieldSweep using simulated instruments.
# last_updated: 2026-07-13
# ---

import json

import h5py
import numpy as np
import pytest

from cryosoft.core.plan import Command, ParamGroup, ParamSpec, PhasePlan, StepPlan, Target
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import build_station
from cryosoft.core.sweep_builder import SweepAxis, SweepSegment
from cryosoft.procedures.field_sweep import FieldSweep

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {
    "sample_name": "Test Sample",
    "sample_id": "T-001",
    "comments": "automated test",
}

# Minimal params that make the sweep fast in tests. The generic FieldSweep runs
# the delta-mode measurement VI here (current / n_readings are its parameters).
FAST_PARAMS = {
    "measurement_vi": "keithley_delta_mode",
    "field_start": -0.1,
    "field_end": 0.1,
    "field_steps": 3,
    "temperature": 300.0,  # Room temperature — sim driver starts at 300 K (instant settle)
    "current": 1e-6,
    "n_readings": 5,
    "init_wait": 0.0,
    "step_wait": 0.0,
}


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


@pytest.fixture
def procedure(station, tmp_path):
    return FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_PARAMS,
    )


# ── BaseProcedure contract ────────────────────────────────────────────────────

def test_base_procedure_not_instantiable_without_methods():
    """BaseProcedure.initiate() etc. raise NotImplementedError."""
    class NullProc(BaseProcedure):
        pass

    proc = NullProc(station=None, sample_info={}, data_directory="/tmp")
    with pytest.raises(NotImplementedError):
        proc.initiate()
    with pytest.raises(NotImplementedError):
        proc.change_sweep_step()
    with pytest.raises(NotImplementedError):
        proc.measure()
    with pytest.raises(NotImplementedError):
        proc.standby()


def test_base_procedure_empty_sweep():
    """Default _build_sweep_array returns [] and get_progress returns 1.0."""
    class NullProc(BaseProcedure):
        pass

    proc = NullProc(station=None, sample_info={}, data_directory="/tmp")
    assert proc.get_sweep_array() == []
    assert proc.get_progress() == 1.0
    assert proc.get_sweep_position() == (0, 0)


def test_base_procedure_sweep_position_is_one_based():
    """get_sweep_position() reports human 1-based point number and total."""
    class AxisProc(BaseProcedure):
        sweep_axis = SweepAxis(
            key="voltage", unit="V", data_key="voltage_V", description="Bias voltage",
            default_start=0.0, default_end=1.0, default_steps=3,
        )

    proc = AxisProc(station=None, sample_info={}, data_directory="/tmp")
    assert proc.get_sweep_position() == (1, 3)   # _index starts at 0 -> point 1 of 3
    proc._index = 2
    assert proc.get_sweep_position() == (3, 3)


def test_base_procedure_sweep_axis_default_build_sweep_array():
    """A subclass that only declares sweep_axis gets a working sweep for free."""
    class AxisProc(BaseProcedure):
        sweep_axis = SweepAxis(
            key="voltage", unit="V", data_key="voltage_V", description="Bias voltage",
            default_start=0.0, default_end=1.0, default_steps=3,
        )

    proc = AxisProc(station=None, sample_info={}, data_directory="/tmp")
    assert proc.get_sweep_array() == pytest.approx([0.0, 0.5, 1.0])


def test_base_procedure_sweep_axis_merges_hidden_params():
    """sweep_axis's hidden parameters are merged into cls.parameters."""
    class AxisProc(BaseProcedure):
        sweep_axis = SweepAxis(
            key="voltage", unit="V", data_key="voltage_V", description="Bias voltage",
        )

    assert "voltage_mode" in AxisProc.parameters
    assert "voltage_start" in AxisProc.parameters
    assert "voltage_csv_path" in AxisProc.parameters
    # voltage_segments is not a merged param: a list of segment dicts can't be a
    # scalar ParamSpec, so the SweepAxisWidget owns it directly (see
    # sweep_axis_param_specs). Everything merged is a typed ParamSpec.
    assert "voltage_segments" not in AxisProc.parameters
    assert all(isinstance(spec, ParamSpec) for spec in AxisProc.parameters.values())


def test_get_param_groups_default_returns_declared_groups_in_order():
    """The default get_param_groups yields Sweep/System/Measurement, skipping empty ones."""
    class ThreeGroupProc(BaseProcedure):
        sweep_parameters = {
            "sweep_a": ParamSpec(type=float, default=1.0, description="a"),
        }
        system_parameters = {
            "sys_a": ParamSpec(type=float, default=2.0, description="b"),
        }
        measurement_parameters = {
            "meas_a": ParamSpec(type=int, default=3, description="c"),
        }

    groups = ThreeGroupProc.get_param_groups(station=None)
    assert [g.key for g in groups] == ["sweep", "system", "measurement"]
    assert [g.title for g in groups] == ["Sweep", "System", "Measurement"]
    assert all(isinstance(g, ParamGroup) for g in groups)
    assert set(groups[0].params) == {"sweep_a"}
    assert set(groups[2].params) == {"meas_a"}


def test_get_param_groups_skips_empty_groups():
    """A group with no declared parameters is omitted (here: no sweep_parameters)."""
    class TwoGroupProc(BaseProcedure):
        system_parameters = {
            "sys_a": ParamSpec(type=float, default=2.0, description="b"),
        }
        measurement_parameters = {
            "meas_a": ParamSpec(type=int, default=3, description="c"),
        }

    groups = TwoGroupProc.get_param_groups(station=None)
    # sweep_parameters is empty (this proc uses no flat sweep params), so the
    # Sweep group is skipped entirely — System and Measurement remain, in order.
    assert [g.key for g in groups] == ["system", "measurement"]


# ── FieldSweep instantiation ────────────────────────────────────────────────

def test_instantiation(procedure):
    """FieldSweep instantiates without error."""
    assert procedure is not None


def test_sweep_array_length(procedure):
    """Sweep array has the correct number of points."""
    sweep = procedure.get_sweep_array()
    assert len(sweep) == 3


def test_sweep_array_values(procedure):
    """Sweep array spans the correct field range."""
    sweep = procedure.get_sweep_array()
    assert sweep[0] == pytest.approx(-0.1)
    assert sweep[-1] == pytest.approx(0.1)
    assert sweep[1] == pytest.approx(0.0)


# ── Sweep-shape overrides (sweep_builder integration) ───────────────────────────


def test_sweep_segments_override_take_precedence(station, tmp_path):
    """Passing field_segments (+field_mode) builds a piecewise sweep instead of linear."""
    params = dict(FAST_PARAMS)
    params["field_mode"] = "segments"
    params["field_segments"] = [
        {"start": -0.1, "end": -0.02, "step": 0.04},
        {"start": -0.02, "end": 0.02, "step": 0.02},
        {"start": 0.02, "end": 0.1, "step": 0.04},
    ]
    procedure = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path), **params
    )
    sweep = procedure.get_sweep_array()
    assert sweep[0] == pytest.approx(-0.1)
    assert sweep[-1] == pytest.approx(0.1)
    assert any(abs(v) < 1e-9 for v in sweep)  # the fine sub-segment crosses zero


def test_sweep_segments_accepts_sweepsegment_instances(station, tmp_path):
    """field_segments may also be a list of SweepSegment dataclass instances."""
    params = dict(FAST_PARAMS)
    params["field_mode"] = "segments"
    params["field_segments"] = [
        SweepSegment(start=0.0, end=0.1, step=0.05),
    ]
    procedure = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path), **params
    )
    assert procedure.get_sweep_array() == pytest.approx([0.0, 0.05, 0.1])


def test_sweep_csv_mode_ignores_segments(station, tmp_path):
    """field_mode='csv' reads field_csv_path and ignores field_segments entirely."""
    csv_file = tmp_path / "fields.csv"
    csv_file.write_text("0.1\n0.0\n-0.1\n")

    params = dict(FAST_PARAMS)
    params["field_mode"] = "csv"
    params["field_csv_path"] = str(csv_file)
    params["field_segments"] = [{"start": 0.0, "end": 1.0, "step": 0.5}]  # ignored: mode is csv

    procedure = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path), **params
    )
    assert procedure.get_sweep_array() == pytest.approx([0.1, 0.0, -0.1])


def test_sweep_hysteresis_extends_linear_sweep(station, tmp_path):
    """field_hysteresis=True appends the reverse sweep (minus the duplicated peak)."""
    params = dict(FAST_PARAMS)
    params["field_start"] = -0.1
    params["field_end"] = 0.1
    params["field_steps"] = 3
    params["field_hysteresis"] = True

    procedure = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path), **params
    )
    assert procedure.get_sweep_array() == pytest.approx([-0.1, 0.0, 0.1, 0.0, -0.1])


def test_initial_progress(procedure):
    """Progress is 0.0 at construction."""
    assert procedure.get_progress() == pytest.approx(0.0)


# ── initiate() ────────────────────────────────────────────────────────────────

def test_initiate_returns_correct_structure(procedure, tmp_path):
    """initiate() returns a PhasePlan with typed targets and commands."""
    plan = procedure.initiate()
    procedure.standby()  # Close file

    assert isinstance(plan, PhasePlan)

    assert "magnet_z" in plan.targets
    assert isinstance(plan.targets["magnet_z"], Target)
    assert plan.targets["magnet_z"].target == pytest.approx(-0.1)

    assert "temperature_vti" in plan.targets
    assert plan.targets["temperature_vti"].target == pytest.approx(300.0)

    arm = next(c for c in plan.commands if c.vi_name == "keithley_delta_mode")
    assert arm.method == "initiate_measurement"

    assert plan.wait_s == pytest.approx(0.0)


def test_initiate_full_phaseplan_content_and_command_order(procedure, tmp_path):
    """initiate() returns the complete, exact PhasePlan (incl. command order)."""
    plan = procedure.initiate()
    procedure.standby()  # Close file

    # Exactly the two system targets, both plain field/temperature targets.
    assert set(plan.targets) == {"magnet_z", "temperature_vti"}
    assert plan.targets["magnet_z"] == Target(-0.1)
    assert plan.targets["temperature_vti"] == Target(300.0)

    # Exactly one command: arm the delta-mode measurement, first in order.
    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, Command)
    assert cmd.vi_name == "keithley_delta_mode"
    assert cmd.method == "initiate_measurement"
    assert cmd.kwargs["current"] == pytest.approx(1e-6)
    assert cmd.kwargs["n_readings"] == 5
    assert cmd.kwargs["voltmeter_range_V"] == pytest.approx(0.01)

    assert plan.wait_s == pytest.approx(0.0)


def test_initiate_creates_hdf5_file(procedure, tmp_path):
    """initiate() creates an HDF5 file in data_directory."""
    procedure.initiate()
    procedure.standby()

    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    assert h5_files[0].stat().st_size > 0


def test_initiate_hdf5_metadata(procedure, tmp_path):
    """HDF5 file created by initiate() contains correct metadata."""
    procedure.initiate()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    with h5py.File(filepath, "r") as f:
        meta = f["metadata"].attrs
        assert meta["procedure_name"] == "Field Sweep"
        params = json.loads(meta["procedure_params"])
        assert params["field_steps"] == 3
        si = json.loads(meta["sample_info"])
        assert si["sample_name"] == "Test Sample"


def test_initiate_uses_custom_file_prefix(station, tmp_path):
    """A procedure constructed with file_prefix names its HDF5 file accordingly."""
    proc = FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        file_prefix="my_custom_run",
        **FAST_PARAMS,
    )
    proc.initiate()
    filepath = proc._data_manager.filepath
    proc.standby()

    assert filepath.name.startswith("my_custom_run_")
    with h5py.File(filepath, "r") as f:
        # Metadata still records the real procedure name, independent of the filename.
        assert f["metadata"].attrs["procedure_name"] == "Field Sweep"


# ── change_sweep_step() ───────────────────────────────────────────────────────

def test_change_sweep_step_returns_targets(procedure, tmp_path):
    """change_sweep_step() returns a StepPlan for each step."""
    procedure.initiate()

    step = procedure.change_sweep_step()
    assert step is not None
    assert isinstance(step, StepPlan)
    assert "magnet_z" in step.targets
    assert step.targets["magnet_z"].target == pytest.approx(0.0)
    assert step.wait_s == pytest.approx(0.0)

    procedure.standby()


def test_change_sweep_step_returns_none_at_end(procedure, tmp_path):
    """change_sweep_step() returns None after all steps are exhausted.

    With field_steps=3, sweep=[A, B, C]:
      initiate() → index=0, target=A
      change_sweep_step() → index=1, returns B
      change_sweep_step() → index=2, returns C
      change_sweep_step() → index=3 ≥ 3 → returns None
    """
    procedure.initiate()
    procedure.change_sweep_step()  # index 0→1
    procedure.change_sweep_step()  # index 1→2
    result = procedure.change_sweep_step()  # index 2→3 → None
    assert result is None

    procedure.standby()


def test_progress_updates(procedure, tmp_path):
    """get_progress() returns the correct fraction after each step."""
    procedure.initiate()
    assert procedure.get_progress() == pytest.approx(0.0)

    procedure.change_sweep_step()
    assert procedure.get_progress() == pytest.approx(1 / 3)

    procedure.change_sweep_step()
    assert procedure.get_progress() == pytest.approx(2 / 3)

    procedure.standby()


# ── measure() ─────────────────────────────────────────────────────────────────

def test_measure_without_initiate_raises(procedure):
    """measure() before initiate() raises RuntimeError."""
    with pytest.raises(RuntimeError):
        procedure.measure()


def test_measure_saves_datapoint(procedure, tmp_path):
    """measure() writes data to the HDF5 file at the correct sweep index."""
    procedure.initiate()
    # Arm the measurement VI (normally done via station.send_measurement_commands)
    procedure._station.keithley_delta_mode.initiate_measurement(
        current=1e-6, n_readings=5
    )

    procedure.measure()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    with h5py.File(filepath, "r") as f:
        # Only 1 point was saved → standby() trims datasets to shape (1, 5)
        assert not np.isnan(f["data"]["field_T"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))
        assert f["data"]["voltage_V"].shape == (1, 5)


def test_measure_stores_snapshot(procedure, tmp_path):
    """measure() stores a JSON station snapshot."""
    procedure.initiate()
    procedure._station.keithley_delta_mode.initiate_measurement(
        current=1e-6, n_readings=5
    )
    procedure.measure()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    with h5py.File(filepath, "r") as f:
        snap = json.loads(f["snapshots"]["0"][()])
        assert isinstance(snap, dict)
        assert len(snap) > 0


# ── standby() ─────────────────────────────────────────────────────────────────

def test_standby_returns_correct_structure(procedure, tmp_path):
    """standby() returns a PhasePlan parking the magnet and disarming the meas VI."""
    procedure.initiate()
    plan = procedure.standby()

    assert isinstance(plan, PhasePlan)
    assert "magnet_z" in plan.targets
    assert plan.targets["magnet_z"].target == pytest.approx(0.0)
    assert any(c.vi_name == "keithley_delta_mode" for c in plan.commands)
    assert plan.wait_s == pytest.approx(0.0)


def test_standby_closes_data_file(procedure, tmp_path):
    """standby() closes the HDF5 file (DataManager is set to None)."""
    procedure.initiate()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    assert procedure._data_manager is None
    # File should be valid and readable
    with h5py.File(filepath, "r") as f:
        assert f["metadata"].attrs["end_time"] != ""


def test_standby_double_call_safe(procedure, tmp_path):
    """standby() called twice does not raise."""
    procedure.initiate()
    procedure.standby()
    procedure.standby()  # Should not raise


# ── Full Orchestrator loop ────────────────────────────────────────────────────

def test_full_orchestrator_loop(station, tmp_path, qtbot):
    """FieldSweep runs through a full Orchestrator cycle without errors."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    # Fast ramp rates so the test completes quickly
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []
    # temperature_vti starts at 300 K; target is also 300 K → instant settle

    procedure = FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_PARAMS,
    )

    orch = Orchestrator(station, tick_interval_ms=10)

    # Pre-arm keithley_delta_mode (normally done by orchestrator via station)
    # The Orchestrator calls station.send_measurement_commands from run_procedure,
    # which dispatches initiate(). That happens inside run_procedure → initiate().
    orch.run_procedure(procedure)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    # All 3 sweep points should have been measured.
    # _index is 3 after the final change_sweep_step() call returns None (0→1→2→3).
    assert procedure._index == 3
    assert orch._state == OrchestratorState.IDLE

    # HDF5 file must exist and be valid
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert f["metadata"].attrs["procedure_name"] == "Field Sweep"


# ── Public run surface: data_filepath / get_params / experiment_info ─────────

def test_data_filepath_none_before_initiate(procedure):
    """data_filepath is None until initiate() opens the data file."""
    assert procedure.data_filepath is None


def test_data_filepath_set_while_file_open_then_cleared(procedure, tmp_path):
    """data_filepath points at the HDF5 file during the run, None after close."""
    procedure.initiate()
    path = procedure.data_filepath
    assert path is not None
    assert path.endswith(".h5")
    assert str(tmp_path) in path
    procedure.standby()
    assert procedure.data_filepath is None


def test_last_datapoint_empty_before_measure(procedure, tmp_path):
    """last_datapoint is {} before anything is saved, populated after measure()."""
    assert procedure.last_datapoint == {}
    procedure.initiate()
    assert procedure.last_datapoint == {}
    procedure._station.keithley_delta_mode.initiate_measurement(current=1e-6, n_readings=5)
    procedure.measure()
    point = procedure.last_datapoint
    assert point, "expected a datapoint after measure()"
    # It is a copy: mutating it must not touch the stored datapoint.
    point.clear()
    assert procedure.last_datapoint
    procedure.standby()


def test_get_params_returns_merged_copy(procedure):
    """get_params() has form values + resolved measurement selection, as a copy."""
    params = procedure.get_params()
    assert params["field_steps"] == 3
    assert params["measurement_vi"] == "keithley_delta_mode"
    params["field_steps"] = 999
    assert procedure.get_params()["field_steps"] == 3


def test_run_kind_defaults_to_run():
    """The run-manifest kind of a normal procedure is 'run'."""
    assert FieldSweep.run_kind == "run"


def test_experiment_info_defaults_to_empty_in_hdf5(procedure, tmp_path):
    """Without a session layer the HDF5 experiment_info attribute records {}."""
    procedure.initiate()
    filepath = procedure._data_manager.filepath
    procedure.standby()
    with h5py.File(filepath, "r") as f:
        assert json.loads(f["metadata"].attrs["experiment_info"]) == {}


def test_experiment_info_forwarded_to_hdf5(station, tmp_path):
    """experiment_info passed at construction is stamped into the HDF5 file."""
    info = {"experiment_id": "exp-1", "user_id": "jdoe"}
    proc = FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        experiment_info=info,
        **FAST_PARAMS,
    )
    proc.initiate()
    filepath = proc._data_manager.filepath
    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert json.loads(f["metadata"].attrs["experiment_info"]) == info


def test_full_orchestrator_loop_emits_run_manifests(station, tmp_path, qtbot):
    """A real run emits run_started/run_finished manifests with the file path."""
    from cryosoft.core.orchestrator import Orchestrator

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    procedure = FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_PARAMS,
    )
    orch = Orchestrator(station, tick_interval_ms=10)
    started: list[dict] = []
    finished: list[dict] = []
    orch.run_started.connect(started.append)
    orch.run_finished.connect(finished.append)

    orch.run_procedure(procedure)
    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert len(started) == 1 and len(finished) == 1
    manifest = started[0]
    assert manifest["procedure"] == "Field Sweep"
    assert manifest["kind"] == "run"
    assert manifest["params"]["field_steps"] == 3
    assert manifest["data_file"].endswith(".h5")
    assert list(tmp_path.glob("*.h5"))[0].samefile(manifest["data_file"])
    end = finished[0]
    assert end["run_id"] == manifest["run_id"]
    assert end["status"] == "done"
    assert end["reason"] == ""
    # The file path survives into the finished manifest even though the
    # procedure closed (and forgot) its DataManager in standby().
    assert end["data_file"] == manifest["data_file"]
