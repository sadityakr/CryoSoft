# ---
# description: |
#   Unit tests for cryosoft.gui.lifecycle_toggle.LifecycleToggleButton: initial
#   state, click callback (opposite of current state, no optimistic flip), and
#   set_initiated() driving both the button text/class and the glow dot.
# entry_point: pytest tests/test_lifecycle_toggle.py -v
# ---

from cryosoft.gui.lifecycle_toggle import LifecycleToggleButton


def test_starts_in_standby_state(qtbot):
    w = LifecycleToggleButton("magnet_x", lambda action: None)
    qtbot.addWidget(w)

    assert w.is_initiated() is False
    assert w._btn.text() == "Initiate"
    assert w._dot.property("status") == "standby"


def test_click_calls_back_with_initiate_and_does_not_flip_state(qtbot):
    calls = []
    w = LifecycleToggleButton("magnet_x", calls.append)
    qtbot.addWidget(w)

    w._btn.click()

    assert calls == ["initiate"]
    # No optimistic flip: state only changes via set_initiated().
    assert w.is_initiated() is False
    assert w._btn.text() == "Initiate"


def test_set_initiated_true_updates_button_and_dot(qtbot):
    w = LifecycleToggleButton("magnet_x", lambda action: None)
    qtbot.addWidget(w)

    w.set_initiated(True)

    assert w.is_initiated() is True
    assert w._btn.text() == "Standby"
    assert w._dot.property("status") == "initiated"


def test_click_after_initiated_calls_back_with_standby(qtbot):
    calls = []
    w = LifecycleToggleButton("magnet_x", calls.append)
    qtbot.addWidget(w)
    w.set_initiated(True)

    w._btn.click()

    assert calls == ["standby"]
    assert w.is_initiated() is True  # still waiting for confirmation


def test_set_initiated_same_state_is_a_noop(qtbot):
    w = LifecycleToggleButton("magnet_x", lambda action: None)
    qtbot.addWidget(w)

    w.set_initiated(False)  # already False

    assert w.is_initiated() is False
    assert w._btn.text() == "Initiate"
