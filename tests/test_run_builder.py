# ---
# description: |
#   Tests for cryosoft.core.run_builder — the single headless construction path
#   for a procedure. Covers the kwargs contract, experiment_info normalisation,
#   the PROCEDURE_BUILD_ERRORS tuple that every caller catches, and the module's
#   headlessness (no Qt import, buildable with no QApplication).
# last_updated: 2026-08-08
# ---

import sys

import pytest

from cryosoft.core.exceptions import CryoSoftConfigError, CryoSoftSafetyError
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.run_builder import PROCEDURE_BUILD_ERRORS, build_procedure
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {"sample_name": "S", "sample_id": "S-1", "comments": ""}

FAST_PARAMS = {
    "measurement_vi": "dc_measurement",
    "field_start": -0.1,
    "field_end": 0.1,
    "field_steps": 3,
    "temperature": 300.0,
    "current_A": 1e-6,
    "readings_per_point": 5,
    "init_wait": 0.0,
    "step_wait": 0.0,
}


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


# ── Construction contract ────────────────────────────────────────────────────

def test_builds_a_real_procedure_from_plain_values(station, tmp_path):
    """The happy path: a class plus plain values yields a ready procedure."""
    proc = build_procedure(
        FieldSweep,
        station=station,
        params=FAST_PARAMS,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        file_prefix="run1",
    )

    assert isinstance(proc, FieldSweep)
    assert proc._sample_info == SAMPLE_INFO
    assert proc._file_prefix == "run1"
    assert proc._station is station


def test_no_qapplication_is_needed(station, tmp_path):
    """The point of the module: construction never touches Qt.

    A GUI-free client (a test, a script, a future headless caller) must be able
    to build a run. Guarded explicitly because the two call sites this module
    replaced both lived on widgets. This test deliberately requests no ``qtbot``
    /``qapp`` fixture, so it runs with whatever Qt state the session happens to
    have — including none.
    """
    proc = build_procedure(
        FieldSweep,
        station=station,
        params=FAST_PARAMS,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
    )
    assert proc is not None


def test_module_imports_no_qt():
    """run_builder must stay importable in a process with no Qt loaded."""
    import cryosoft.core.run_builder as rb

    qt_names = [n for n in dir(rb) if "Qt" in n or "QMessage" in n]
    assert qt_names == []
    assert "PyQt6" not in {m.split(".")[0] for m in rb.__dict__.get("__annotations__", {})}
    # The module's own source must not import Qt at all.
    src = sys.modules["cryosoft.core.run_builder"].__file__
    assert src is not None
    with open(src, encoding="utf-8") as fh:
        assert "PyQt6" not in fh.read()


def test_experiment_info_none_becomes_empty_dict(station, tmp_path):
    """A caller with no open experiment passes None and needs no special case."""
    proc = build_procedure(
        FieldSweep,
        station=station,
        params=FAST_PARAMS,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        experiment_info=None,
    )
    assert proc._experiment_info == {}


def test_experiment_info_is_passed_through(station, tmp_path):
    proc = build_procedure(
        FieldSweep,
        station=station,
        params=FAST_PARAMS,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        experiment_info={"experiment_id": "exp-7"},
    )
    assert proc._experiment_info["experiment_id"] == "exp-7"


# ── The error tuple every caller catches ─────────────────────────────────────

def test_build_errors_tuple_covers_cryosoft_errors():
    """CryoSoftConfigError/SafetyError derive from CryoSoftError, NOT ValueError.

    The regression this guards: queue_panel used to catch only
    (TypeError, ValueError), so a procedure refusing a restored run with
    CryoSoftConfigError escaped into session restore.
    """
    assert issubclass(CryoSoftConfigError, PROCEDURE_BUILD_ERRORS)
    assert issubclass(CryoSoftSafetyError, PROCEDURE_BUILD_ERRORS)
    assert not issubclass(CryoSoftConfigError, ValueError)
    assert issubclass(TypeError, PROCEDURE_BUILD_ERRORS)
    assert issubclass(ValueError, PROCEDURE_BUILD_ERRORS)


def test_refusal_raises_and_is_caught_by_the_tuple(station, tmp_path):
    """A procedure that refuses construction raises, and the tuple catches it."""
    class RefusingProc(BaseProcedure):
        name = "Refusing"

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            raise CryoSoftConfigError("this station has no magnet")

    with pytest.raises(PROCEDURE_BUILD_ERRORS):
        build_procedure(
            RefusingProc,
            station=station,
            params={},
            sample_info=SAMPLE_INFO,
            data_directory=str(tmp_path),
        )


def test_unknown_params_are_absorbed_not_rejected(station, tmp_path):
    """Documents existing BaseProcedure behaviour, so the change is visible.

    ``BaseProcedure.__init__`` takes ``**param_values``, so an unknown key is
    absorbed into ``_params`` rather than raising. A queue entry restored after
    a procedure renamed a parameter therefore builds successfully and silently
    ignores the stale value — it does NOT surface as a build error. Asserted
    here so that if the signature is ever tightened, this test fails and the
    callers' error handling gets revisited deliberately.
    """
    proc = build_procedure(
        FieldSweep,
        station=station,
        params={**FAST_PARAMS, "no_such_parameter": 1},
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
    )
    assert proc._params["no_such_parameter"] == 1
