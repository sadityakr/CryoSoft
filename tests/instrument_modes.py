"""Running a GUI suite against either instrument mode.

The **instrument-thread flag** decides whether the Station and the
Orchestrator live on their own thread — the default — or on the caller's
(``cryosoft.core.instrument_host``). Everything above the boundary is
supposed not to care — same proxy, same primed mirror, same signals — and the
only way to keep that true is to run the same GUI suite both ways. This module
is what makes that a matter of an environment variable:

    pytest tests/test_gui.py                                # threaded
    CRYOSOFT_INSTRUMENT_THREAD=0 pytest tests/test_gui.py   # inline

Not a test file. It gives a suite three things:

* ``instrument_mode()`` — what the environment asks for, once per session;
* ``build_host()`` / ``shutdown_host()`` — a host in that mode, torn down so
  that no tick and no queued delivery can land in a half-destroyed widget
  tree;
* the **tick helper** family — ``on_engine()``, ``set_on_engine()`` and
  ``tick_engine()``. A GUI test that reaches past the client boundary (forcing
  a state, calling ``_tick()``, setting a private the engine only writes from
  inside a tick) is calling into the engine, and in threaded mode that is a
  call onto another thread. These run it where the engine lives, wait for it,
  and then drain the client's own event loop so the queued consequences —
  the mirror update, the widget repaint — have landed by the time the helper
  returns. Inline they are a direct call plus a drain, so a test reads the
  same either way.

A test that builds its OWN station and drives it synchronously
(``station.get_state()`` in a loop) is single-threaded by construction and
stays that way; the mode governs the shared fixtures, which is where the
windows are built.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from PyQt6.QtCore import QCoreApplication

from cryosoft.core.instrument_host import InstrumentHost, resolve_mode
from cryosoft.core.station import build_station

#: How long a marshalled call may take before the suite calls it a failure.
CALL_TIMEOUT_S: float = 10.0

#: How long ``shutdown_host()`` gives the instrument thread to stop.
JOIN_TIMEOUT_MS: int = 5000


def instrument_mode() -> str:
    """Return the mode this test session runs in.

    Reads ``CRYOSOFT_INSTRUMENT_THREAD`` alone — a test session has no
    ``monitor.yaml`` to consult, and CI selects the mode by environment.
    Unset means ``threaded``, exactly as it does for the application.

    Returns:
        ``"inline"`` or ``"threaded"``.
    """
    return resolve_mode(None)


def build_host(
    config_path: str, *, tick_interval_ms: int = 50, **kwargs: Any
) -> InstrumentHost:
    """Build and start a host over the sim station, in this session's mode.

    Args:
        config_path: The config directory to build the Station from.
        tick_interval_ms: The engine's tick interval.
        **kwargs: Passed through to ``InstrumentHost``.

    Returns:
        The started host.
    """
    host = InstrumentHost(
        lambda: build_station(config_path),
        mode=instrument_mode(),
        orchestrator_options={"tick_interval_ms": tick_interval_ms},
        join_timeout_ms=JOIN_TIMEOUT_MS,
        **kwargs,
    )
    host.start()
    return host


def shutdown_host(host: InstrumentHost) -> None:
    """Stop a host and make sure nothing it emitted is still in flight.

    Three steps, and each one has cost a segfault at some point: stop the
    engine (so no further tick fires into a widget tree qtbot is about to
    destroy), cut the engine's connections (so a delivery already posted to
    this thread cannot re-emit through an object that is gone), and drain.

    Args:
        host: The host to stop. Safe on a host that was never started.
    """
    host.shutdown()
    try:
        host.orchestrator.disconnect()
    except (RuntimeError, TypeError):
        pass
    drain()


def drain(rounds: int = 3) -> None:
    """Let every queued delivery already posted to this thread land.

    Args:
        rounds: How many times to run the client's event loop dry. More than
            one because a delivery can post another.
    """
    for _ in range(rounds):
        QCoreApplication.processEvents()


def engine_of(client: Any) -> Any:
    """Return the Orchestrator behind *client*.

    Args:
        client: An ``OrchestratorProxy`` or an ``Orchestrator``.

    Returns:
        The engine itself.
    """
    return getattr(client, "_engine", client)


def on_engine(client: Any, call: Callable[[], Any], *, settle: bool = True) -> Any:
    """Run *call* where the engine lives, wait for it, then drain this thread.

    The **tick helper**'s general form. Inline this is ``call()`` followed by
    a drain; threaded it posts the call across, waits for it, and then drains
    — so that by the time it returns, the consequences the engine emitted have
    been delivered to this thread's widgets and mirror.

    Args:
        client: The proxy (or engine) the test holds.
        call: A zero-argument callable to run on the engine's thread.
        settle: Whether to drain this thread's event loop afterwards. Pass
            ``False`` for a call that emits nothing — a bare attribute set —
            where draining would only give the engine's tick timer a chance
            to fire and undo what was just forced.

    Returns:
        Whatever *call* returned.

    Raises:
        AssertionError: If the engine does not run the call in time.
        BaseException: Whatever *call* raised, re-raised here.
    """
    bridge = getattr(client, "bridge", None)
    if bridge is None or bridge.on_engine_thread():
        result = call()
        if settle:
            drain()
        return result

    answer: list[Any] = []
    failure: list[BaseException] = []
    done = threading.Event()

    def _run() -> None:
        try:
            answer.append(call())
        except BaseException as exc:  # noqa: BLE001 — carried back to the test
            failure.append(exc)
        finally:
            done.set()

    bridge.post(_run)
    assert done.wait(CALL_TIMEOUT_S), (
        "the instrument thread did not run the call within "
        f"{CALL_TIMEOUT_S:.0f} s"
    )
    if settle:
        drain()
    if failure:
        raise failure[0]
    return answer[0] if answer else None


def set_on_engine(client: Any, name: str, value: Any) -> None:
    """Set one engine attribute from a test, on the engine's own thread.

    The forcing pattern GUI tests use to stand a scenario up without driving
    the whole state machine to it. Written through here rather than as a bare
    assignment because ``client`` is the proxy: assigning to it would set an
    attribute on the proxy and quietly change nothing.

    Args:
        client: The proxy (or engine) the test holds.
        name: The engine attribute to set.
        value: What to set it to.
    """
    engine = engine_of(client)
    on_engine(client, lambda: setattr(engine, name, value), settle=False)


def tick_engine(client: Any, times: int = 1) -> None:
    """Fire *times* engine ticks where the engine lives, and wait for them.

    What replaces a bare ``orchestrator._tick()`` in a test that has moved
    behind the client boundary.

    Args:
        client: The proxy (or engine) the test holds.
        times: How many ticks to run.
    """
    engine = engine_of(client)
    for _ in range(times):
        on_engine(client, engine._tick)


@contextmanager
def ticks_paused(client: Any) -> Iterator[None]:
    """Hold the engine's tick timer for the body of the ``with``.

    A GUI test that FORCES engine state — ``_state``, an active run — and then
    clicks is describing a moment, not a trajectory: the next tick would
    advance the state machine straight back out of it. Inline that never
    happened, because nothing ran the event loop between the two lines;
    threaded, the engine ticks on its own clock and would. Stopping the timer
    makes the moment hold in both modes.

    Args:
        client: The proxy (or engine) the test holds.

    Yields:
        Nothing; the timer is stopped for the duration.
    """
    engine = engine_of(client)
    on_engine(client, engine._timer.stop, settle=False)
    try:
        yield
    finally:
        on_engine(client, engine._timer.start, settle=False)


def settled(client: Any, rounds: int = 1) -> None:
    """Wait until everything already sent to the engine has come back.

    A GUI action crosses the boundary twice — the command out, the event that
    proves it happened back — and inline both hops are the same call stack, so
    a test may assert immediately. Threaded they are two event-loop turns, and
    a test that asserts immediately is asserting before the answer exists.
    This closes exactly that gap: it posts a no-op behind everything already
    queued for the engine, waits for it (so every earlier command has run),
    then drains this thread (so every event they emitted has been delivered).

    A no-op inline, deliberately: there is nothing in flight, and running the
    event loop there would only let the tick timer fire in the middle of a
    test that never expected one.

    Args:
        client: The proxy the test holds.
        rounds: How many round trips to wait out, for an effect that takes
            more than one.
    """
    if getattr(client, "bridge", None) is None:
        return
    for _ in range(rounds):
        on_engine(client, lambda: None)
