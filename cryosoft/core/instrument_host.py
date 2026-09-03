"""InstrumentHost — who builds the instrument stack, and where it lives.

One object owns the Station and the Orchestrator and decides which thread they
are built on. Today there is one mode:

* ``inline`` — everything is constructed on the caller's thread, exactly as
  the application has always done it. Behaviour is unchanged; what changes is
  that the construction has a single home, and the client is handed a
  ``StatusMirror``-primed ``OrchestratorProxy`` instead of the engine itself.
* ``threaded`` — the instrument stack moves to its own ``QThread``, so a slow
  ``measure()`` can no longer freeze the window. Not implemented here; the
  seam it needs is: the Station is built by ``_station_factory`` (so every
  pyvisa ``ResourceManager`` and serial port is opened by the thread that will
  use it), and ``client_state()`` captures the mirror's priming values on the
  engine's own thread so the client never reads across the boundary.

That seam is the whole point of this module existing before the thread does:
``inline`` and ``threaded`` differ in where ``start()`` runs, and in nothing
the client can see.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QObject

from cryosoft.core import events as ev
from cryosoft.core.orchestrator import Orchestrator

if TYPE_CHECKING:
    from cryosoft.core.orchestrator_proxy import OrchestratorProxy
    from cryosoft.core.station import Station

logger = logging.getLogger(__name__)

#: The modes ``InstrumentHost`` knows. ``threaded`` is declared here, and
#: refused by ``start()``, so the flag that will select it has a name before
#: the implementation lands.
MODES: tuple[str, ...] = ("inline", "threaded")

#: The setting that will select the mode once ``threaded`` exists, named in
#: the refusal so an operator who set it is told exactly what to unset.
THREAD_FLAG: str = "monitor.yaml `instrument_thread` (CRYOSOFT_INSTRUMENT_THREAD)"


class InstrumentHost(QObject):
    """Owns the instrument stack and hands a client its adapter.

    Args:
        station_factory: Builds the Station. A callable rather than a built
            Station because in ``threaded`` mode it must run *inside* the
            thread that will own every instrument handle.
        mode: ``"inline"`` (default) or ``"threaded"``.
        orchestrator_options: Keyword arguments for the ``Orchestrator``
            constructor beyond the Station — tick interval, safety timings,
            the run catalog. May be a callable taking the built Station,
            for the common case where those values come from the config the
            Station build itself resolved.
        parent: Optional Qt parent.

    Raises:
        ValueError: If *mode* is not one of ``MODES``.
    """

    def __init__(
        self,
        station_factory: Callable[[], Station],
        *,
        mode: str = "inline",
        orchestrator_options: (
            Mapping[str, Any] | Callable[[Station], Mapping[str, Any]] | None
        ) = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if mode not in MODES:
            raise ValueError(
                f"unknown InstrumentHost mode {mode!r}; expected one of "
                f"{list(MODES)}"
            )
        self._mode = mode
        self._station_factory = station_factory
        self._orchestrator_options = orchestrator_options or {}
        self._station: Station | None = None
        self._orchestrator: Orchestrator | None = None
        self._client_state: tuple[ev.StationInfo, ev.StatusSnapshot, dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Build the Station and the Orchestrator, and capture the client state.

        Idempotent: a second call is a no-op, so a caller that is unsure
        whether the host is up may simply start it.

        Raises:
            NotImplementedError: In ``threaded`` mode, which is not
                implemented yet — the message names the flag that selects it.
        """
        if self._orchestrator is not None:
            return
        if self._mode == "threaded":
            raise NotImplementedError(
                "InstrumentHost(mode='threaded') is not implemented yet: the "
                "instrument stack still runs on the caller's thread. Leave "
                f"{THREAD_FLAG} unset (or false) to use inline mode."
            )
        self._station = self._station_factory()
        options = self._orchestrator_options
        if callable(options):
            options = options(self._station)
        self._orchestrator = Orchestrator(self._station, **dict(options))
        # The mirror's priming values, taken HERE — on whichever thread the
        # engine was built on — so no client ever reads across the boundary.
        self._client_state = (
            self._orchestrator.station_info(),
            self._orchestrator.status_snapshot(),
            self._orchestrator.get_operational_status(),
        )
        logger.info("Instrument host started in %s mode", self._mode)

    def shutdown(self) -> None:
        """Stop the engine's tick timer. Idempotent, and safe before start."""
        if self._orchestrator is not None:
            self._orchestrator.shutdown()

    # ------------------------------------------------------------------
    # What the host hands out
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        """The mode this host was constructed for."""
        return self._mode

    @property
    def station(self) -> Station:
        """The Station this host built.

        Returns:
            The Station.

        Raises:
            RuntimeError: If the host has not been started.
        """
        if self._station is None:
            raise RuntimeError("InstrumentHost.start() has not been called")
        return self._station

    @property
    def orchestrator(self) -> Orchestrator:
        """The Orchestrator this host built.

        Returns:
            The engine.

        Raises:
            RuntimeError: If the host has not been started.
        """
        if self._orchestrator is None:
            raise RuntimeError("InstrumentHost.start() has not been called")
        return self._orchestrator

    def client_state(
        self,
    ) -> tuple[ev.StationInfo, ev.StatusSnapshot, dict[str, Any]]:
        """Return what a client's status mirror is primed with.

        The station's first declaration, the status at start, and the latest
        operational-status record — all captured on the engine's own thread
        at ``start()``. Everything after them arrives on the event stream.

        Returns:
            ``(station_info, status_snapshot, operational_status)``.

        Raises:
            RuntimeError: If the host has not been started.
        """
        if self._client_state is None:
            raise RuntimeError("InstrumentHost.start() has not been called")
        return self._client_state

    def build_proxy(self, parent: QObject | None = None) -> OrchestratorProxy:
        """Build the client adapter for this host's engine.

        Args:
            parent: Optional Qt parent for the proxy.

        Returns:
            An :class:`~cryosoft.core.orchestrator_proxy.OrchestratorProxy`
            with a mirror already primed from ``client_state()``.

        Raises:
            RuntimeError: If the host has not been started.
        """
        # Local import: the proxy imports this module for typing, so a
        # module-level import here would be circular.
        from cryosoft.core.orchestrator_proxy import OrchestratorProxy

        return OrchestratorProxy.for_host(self, parent=parent)
