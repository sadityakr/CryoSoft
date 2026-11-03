import json
import logging
import time

import pytest

# Trend keys seeded into the test log directory: real sim-config monitored
# keys, so the Trends quadrant has something to draw for them.
_SEED_TREND_KEYS = {"magnet_z_magnet_field_T": 0.0, "temperature_vti_heater_output": 5.0}


def _seed_trend_history(log_dir) -> None:
    """Write a few lines into each trend-history tier, ending just before now.

    Five raw samples (3 s apart), three 3-min buckets and two hourly buckets —
    enough that every tier reader has real data to parse, small enough that a
    window redraw on every tick costs nothing.
    """
    now = time.time()
    raw = [
        {"t": now - 3.0 * i, "s": "IDLE", "v": {k: v + 0.01 * i for k, v in _SEED_TREND_KEYS.items()}}
        for i in range(5, 0, -1)
    ]

    def bucket(t: float, n: int) -> dict:
        stats = {"min": 0.0, "max": 0.0, "mean": 0.0, "std": 0.0, "count": n}
        return {"t": t, "n": n, "v": {k: {**stats, "min": v, "max": v, "mean": v} for k, v in _SEED_TREND_KEYS.items()}}

    tiers = {
        "trend_history_raw.jsonl": raw,
        "trend_history_3min.jsonl": [bucket(now - 180.0 * i, 60) for i in range(3, 0, -1)],
        "trend_history_hourly.jsonl": [bucket(now - 3600.0 * i, 1200) for i in range(2, 0, -1)],
    }
    for filename, records in tiers.items():
        (log_dir / filename).write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )


@pytest.fixture(autouse=True)
def isolated_log_directory(tmp_path_factory, monkeypatch):
    """Point cryosoft.core.paths.log_directory() at a small seeded test directory.

    Without this every test that builds a MonitorWindow (or a trend reader,
    logger, spool, ...) resolves the machine's real log directory — which a
    CryoSoft instance running on the same machine keeps filling (hundreds of
    MB of trend history), so a window redrawing its trends every tick could
    spend the whole test re-parsing it. The directory carries a few entries
    per trend tier (see _seed_trend_history) rather than none, so the read
    path is exercised. A test that needs its own log directory sets
    CRYOSOFT_LOG_DIR itself; monkeypatch keeps the last value.
    """
    # Its own temporary directory, never inside the test's tmp_path, which
    # some tests require to stay exactly as they left it.
    log_dir = tmp_path_factory.mktemp("logs")
    _seed_trend_history(log_dir)
    monkeypatch.setenv("CRYOSOFT_LOG_DIR", str(log_dir))
    return log_dir


@pytest.fixture(autouse=True)
def configure_logging():
    """Ensure logging is configured for all tests."""
    logging.basicConfig(level=logging.DEBUG)


@pytest.fixture(autouse=True)
def isolated_measurement_root(tmp_path, monkeypatch):
    """Point cryosoft.core.paths.measurement_root() at a throwaway directory.

    ExperimentInfoPanel falls back to measurement_root() whenever no
    experiment is open and the Data Dir field is empty — including at
    MonitorWindow construction (apply_session()) — so this must be isolated
    globally, the same way the per-file isolated_settings fixtures isolate
    QSettings, or a pytest run would either raise (no
    CRYOSOFT_MEASUREMENT_ROOT/App-config.yaml configured on the test
    machine) or read the real machine-level settings file.
    """
    monkeypatch.setenv("CRYOSOFT_MEASUREMENT_ROOT", str(tmp_path / "measurement_root"))
