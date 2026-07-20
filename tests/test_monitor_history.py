# ---
# description: |
#   Unit tests for MonitorHistory, the Qt-free ring-buffer used by the
#   Monitor window's trend plots. Covers flattening (matches
#   Station.last_state_flat()'s convention), ring behavior (maxlen
#   eviction), window filtering, and unknown/late-arriving keys.
# last_updated: 2026-07-11
# ---

from cryosoft.gui.monitor_history import MonitorHistory


def test_flattening_matches_last_state_flat_convention():
    """Nested two-VI state flattens to {vi}_{field} keys; bools/strings/_-fields excluded."""
    history = MonitorHistory()
    state = {
        "magnet_z": {
            "get_field": 0.5,
            "magnet_current": 12.3,
            "ramp_status": "ramping",  # string -> excluded
            "_stale": True,  # leading underscore -> excluded
        },
        "temperature_vti": {
            "temperature_K": 4.2,
            "heater_on": True,  # bool -> excluded even though bool is an int subclass
            "_disconnected": False,  # leading underscore -> excluded
        },
    }
    history.record(state, timestamp=100.0)

    assert history.keys() == sorted(
        ["magnet_z_get_field", "magnet_z_magnet_current", "temperature_vti_temperature_K"]
    )

    # Hand-computed expectation matching Station.last_state_flat()'s output shape
    # for this same state (excluding the measurement-VI filter, which is a
    # Station-level concern, not a MonitorHistory concern).
    expected_flat = {
        "magnet_z_get_field": 0.5,
        "magnet_z_magnet_current": 12.3,
        "temperature_vti_temperature_K": 4.2,
    }
    for key, value in expected_flat.items():
        times, values = history.series(key)
        assert times == [100.0]
        assert values == [value]


def test_ring_buffer_caps_length_and_keeps_newest():
    """Recording more points than maxlen keeps only the newest; length never exceeds maxlen."""
    retention_s = 10.0
    tick_interval_s = 1.0
    history = MonitorHistory(retention_s=retention_s, tick_interval_s=tick_interval_s)
    maxlen = int(retention_s / tick_interval_s) + 8  # matches MonitorHistory's internal slack

    n_points = maxlen + 20
    for i in range(n_points):
        history.record({"vi": {"field": float(i)}}, timestamp=float(i))

    times, values = history.series("vi_field")
    assert len(times) == maxlen
    assert len(values) == maxlen
    # The oldest points were evicted; only the newest maxlen remain.
    expected_first_value = float(n_points - maxlen)
    assert values[0] == expected_first_value
    assert values[-1] == float(n_points - 1)


def test_window_filtering_returns_chronological_subset():
    """series(key, window_s, now) returns exactly the points within the window, in order."""
    history = MonitorHistory()
    for t in [0.0, 5.0, 10.0, 15.0, 20.0]:
        history.record({"vi": {"field": t}}, timestamp=t)

    times, values = history.series("vi_field", window_s=10.0, now=20.0)
    # Points with t >= now - window_s = 10.0
    assert times == [10.0, 15.0, 20.0]
    assert values == [10.0, 15.0, 20.0]


def test_window_none_returns_everything_and_unknown_key_returns_empty():
    """window_s=None returns full history; an unknown key returns empty lists."""
    history = MonitorHistory()
    for t in [0.0, 1.0, 2.0]:
        history.record({"vi": {"field": t}}, timestamp=t)

    times, values = history.series("vi_field", window_s=None)
    assert times == [0.0, 1.0, 2.0]
    assert values == [0.0, 1.0, 2.0]

    times, values = history.series("does_not_exist")
    assert times == []
    assert values == []


def test_key_appearing_only_in_later_records_still_works():
    """A flat key first seen mid-run starts its own history from that point on."""
    history = MonitorHistory()
    history.record({"vi_a": {"field": 1.0}}, timestamp=0.0)
    assert "vi_b_field" not in history.keys()

    history.record({"vi_a": {"field": 2.0}, "vi_b": {"field": 99.0}}, timestamp=1.0)

    assert "vi_b_field" in history.keys()
    times, values = history.series("vi_b_field")
    assert times == [1.0]
    assert values == [99.0]

    times, values = history.series("vi_a_field")
    assert times == [0.0, 1.0]
    assert values == [1.0, 2.0]
