"""CryoSoft application entry point."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import pyqtgraph as pg
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

from cryosoft.core.config_catalog import ConfigCatalog
from cryosoft.core.instrument_host import InstrumentHost, resolve_mode
from cryosoft.core.logging_config import setup_logging
from cryosoft.core.paths import measurement_root
from cryosoft.core.request_spool import RequestSpool
from cryosoft.core.station import (
    Station,
    build_station_with_fallback,
    read_cryogenics_config,
    read_assistant_config,
    read_gateway_config,
    read_instrument_thread,
    read_operations_config,
    read_panels_config,
    read_request_spool_config,
    read_safety_config,
    read_servicing_logs_config,
    read_trends_config,
)
from cryosoft.core.trend_check_runner import TrendCheckRunner
from cryosoft.core.trend_checks import declared_checks
from cryosoft.gui import app_settings
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.procedure_discovery import discover_operations, discover_procedures
from cryosoft.gui.theme import PLOT_AXIS, PLOT_BG, build_stylesheet
from cryosoft.session.agent_feed import AgentFeed
from cryosoft.session.assistant import (
    AnthropicChatClient,
    AssistantError,
    AssistantRuntime,
    AssistantTranscript,
)
from cryosoft.session.eln.publisher import ElnPublisher
from cryosoft.session.gateway import authorize_spooled
from cryosoft.session.eln.settings import load_eln_settings
from cryosoft.session.gateway import Gateway, GatewayServer, ToolContext
from cryosoft.session.manager import ExperimentManager
from cryosoft.session.models import GUEST_USER_ID, GUEST_USER_NAME, User
from cryosoft.session.servicing_log import (
    CryogenicsRecorder,
    HeliumRecordStore,
    ServicingLogStore,
)
from cryosoft.session.store import ExperimentStore, SessionStore, UserRoster

logger = logging.getLogger(__name__)


def _startup_candidates() -> list[str]:
    """Return the ordered config candidates for startup, safest last.

    The saved active config is tried first; if it is a user copy, its shipped
    namesake (the never-edited baseline) is tried next; the always-loadable
    ``sim_cryostat`` is the final guarantee. Order-preserving de-dup.

    Returns:
        A list of config directory paths, most-preferred first.
    """
    candidates: list[str] = []
    active = app_settings.config_active()
    if active is not None:
        name, source = active
        base_dir = (
            app_settings.user_config_dir()
            if source == "user"
            else app_settings.shipped_config_dir()
        )
        candidates.append(str(base_dir / name))
        if source == "user":
            shipped_baseline = app_settings.shipped_config_dir() / name
            if shipped_baseline.is_dir():
                candidates.append(str(shipped_baseline))
    candidates.append(str(app_settings.shipped_config_dir() / "sim_cryostat"))

    seen: set[str] = set()
    ordered: list[str] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _ensure_guest_user_registered(roster: UserRoster) -> None:
    """Ensure the fixed Guest roster identity exists (idempotent).

    Runs unconditionally, every launch — the same "must never fail to start
    for lack of an explicit choice" principle ``_resolve_active_session``
    already follows one level down: nobody having logged in must not make
    ``ExperimentManager.start_experiment()``'s roster-membership check
    reject ``user_id=GUEST_USER_ID`` the first time it's used. Never
    clobbers an existing Guest entry (e.g. an admin who gave it a different
    display name) — only adds it when entirely absent.

    Args:
        roster: The setup-local user roster.
    """
    if roster.get(GUEST_USER_ID) is None:
        roster.add(User(user_id=GUEST_USER_ID, name=GUEST_USER_NAME))


def _resolve_active_session(store: SessionStore) -> tuple[str, str]:
    """Return the active ``(user_id, session_id)``, auto-creating a bootstrap session if needed.

    The app must never fail to start for lack of an explicit session choice
    (the startup-wiring rule behind the Session tier — see ``GLOSSARY.md``'s
    **Session**): when the store's active pointer is unset, points at a
    session record that fails to load, or is in the pre-per-user-nesting
    shape (first-ever launch, a corrupt pointer, or an older install), a
    bootstrap session is created and activated on the spot, owned by
    whoever is logged in (or ``GUEST_USER_ID`` when nobody is).

    Args:
        store: The ``SessionStore`` rooted at ``measurement_root() / "sessions"``.

    Returns:
        The active session's ``(user_id, session_id)`` — either the pair
        already pointed to, or a freshly created bootstrap session's.
    """
    active = store.get_active()
    if active is not None and store.load(*active) is not None:
        return active
    user_id = app_settings.current_user_id() or GUEST_USER_ID
    session = store.create_session(name=user_id, user_id=user_id)
    store.set_active(user_id, session.session_id)
    return user_id, session.session_id


def _open_experiment_feed(manager: ExperimentManager) -> AgentFeed | None:
    """Return the **Agent feed** of whichever experiment is open right now.

    Resolved on demand rather than once at startup, so a client that
    connects after the physicist opened a new experiment leaves its trail in
    that experiment's folder — the feed belongs to the experiment, not to
    the process.

    Args:
        manager: The session layer's experiment façade.

    Returns:
        The feed for the open experiment, or ``None`` when none is open (in
        which case a connection records nothing rather than inventing a
        folder to record into).
    """
    experiment = manager.current_experiment()
    if experiment is None:
        return None
    return AgentFeed(
        manager.store.agent_feed_path(experiment.experiment_id),
        experiment.experiment_id,
    )


def _open_assistant_transcript(
    manager: ExperimentManager,
) -> AssistantTranscript | None:
    """Return the **Assistant transcript** of whichever experiment is open now.

    Resolved on demand, exactly like ``_open_experiment_feed()`` and for the
    same reason: the conversation belongs to the experiment, not to the
    process, so a question asked after the physicist opened a new experiment
    is written into that experiment's folder.

    Args:
        manager: The session layer's experiment façade.

    Returns:
        The transcript for the open experiment, or ``None`` when none is open
        (in which case the conversation still runs and simply keeps no
        evidence, rather than inventing a folder to record into).
    """
    experiment = manager.current_experiment()
    if experiment is None:
        return None
    return AssistantTranscript(
        manager.store.assistant_transcript_path(experiment.experiment_id),
        experiment.experiment_id,
    )


def _build_gateway_server(
    engine: Any,
    gateway_config: Mapping[str, Any],
    *,
    station_info: Callable[[], Any],
    tool_context: ToolContext,
    feed: Callable[[], AgentFeed | None],
    socket_name: str | None = None,
    descriptor: Path | str | None = None,
    token: str | None = None,
) -> GatewayServer | None:
    """Build and start the **Gateway server**, if this setup asks for one.

    Factored out of ``main()`` so the wiring itself is testable: the object
    handed in is the **Orchestrator proxy**, not the engine, because this
    server lives on the GUI thread and under the single hardware thread
    standard the engine does not. The proxy satisfies the gateway's
    ``EngineClient`` — ``submit()`` posts the command across, and the two
    contract streams arrive queued under their client-side names.

    Args:
        engine: The engine client every connection's ``Gateway`` attaches to.
        gateway_config: ``read_gateway_config()``'s answer for this setup —
            ``gateway_server`` gates the whole thing, ``gateway_max_role``
            caps what any connection may claim.
        station_info: The station's declaration snapshot, or a callable
            returning it.
        tool_context: The collaborators the session tools read through.
        feed: Resolves the open experiment's **Agent feed** at connection
            time, so a client that connects later records into whichever
            experiment is open then.
        socket_name: The local-socket name to listen on; defaults to the
            installation's.
        descriptor: Where to write the descriptor file; defaults to the
            installation's.
        token: The secret to require in ``hello``; a fresh random one when
            omitted, which is the production path.

    Returns:
        The listening server, or ``None`` when this setup does not ask for
        one or names a ``gateway_max_role`` that is not a **Role** — an
        unknown ceiling closes the door rather than guessing at it, and the
        window opens regardless.
    """
    if not gateway_config["gateway_server"]:
        return None
    try:
        server = GatewayServer(
            engine,
            max_role=gateway_config["gateway_max_role"],
            station_info=station_info,
            tool_context=tool_context,
            feed=feed,
            socket_name=socket_name,
            descriptor=descriptor,
            token=token,
        )
    except ValueError:
        logger.exception(
            "Gateway server not started: monitor.yaml declares the unknown "
            "gateway_max_role %r",
            gateway_config["gateway_max_role"],
        )
        return None
    server.start()
    return server


def _restart_application() -> None:
    """Relaunch the app in a fresh process and quit this one.

    Used after a config switch (which needs a clean rebuild of the whole
    instrument stack). ``startDetached`` launches an independent process before
    this one exits, so the window closes and reopens.
    """
    QProcess.startDetached(sys.executable, sys.argv)
    QApplication.quit()


def main(*, on_station_built: Callable[[Station], None] | None = None) -> None:
    """Start the CryoSoft application.

    Args:
        on_station_built: Optional hook run once, immediately after the
            Station is built and before the Monitor window is shown.
            Monitoring is off at that point (the production default), so a
            hook that sets sim-driver test-control attributes (e.g.
            ``scripts/run_scenario.py``, driving ``tests.scenarios``' apply
            functions) lands them before anything polls the hardware. Never
            used in normal `python -m cryosoft.main` startup.
    """
    setup_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("CryoSoft")
    app.setApplicationVersion("0.1.0")
    app.setStyleSheet(build_stylesheet())
    pg.setConfigOptions(background=PLOT_BG, foreground=PLOT_AXIS, antialias=True)

    catalog = ConfigCatalog(
        app_settings.shipped_config_dir(), app_settings.user_config_dir()
    )

    # The run catalog: {class __name__: class} for every discovered procedure
    # and operation. Discovery lives up here because neither the engine
    # (contract C5) nor the session layer (C11) may import
    # cryosoft.procedures — whoever owns discovery hands the catalog down, so
    # a client that speaks the control contract can name a run by class and
    # the run queue can resolve a stored spec back to its class.
    run_catalog: dict[str, type] = {
        cls.__name__: cls for cls in (*discover_procedures(), *discover_operations())
    }

    # The instrument stack is built by the InstrumentHost, not here: which
    # THREAD owns the Station and the Orchestrator is the host's decision, and
    # `inline` mode is exactly today's behaviour with that decision named. The
    # station factory therefore carries the whole build — including the config
    # fallback, whose resolved path the Orchestrator's own safety settings
    # come from, which is why the options are a callable too.
    build: dict[str, Any] = {}

    def _build_station() -> Station:
        """Build the Station from the first usable startup config."""
        station, used_path, warnings = build_station_with_fallback(
            _startup_candidates()
        )
        build["used_path"] = used_path
        build["warnings"] = warnings
        if on_station_built is not None:
            on_station_built(station)
        return station

    def _orchestrator_options(_station: Station) -> dict[str, Any]:
        """Read the engine's settings from the config the build resolved."""
        safety_config = read_safety_config(build["used_path"])
        # The Request spool (GLOSSARY.md's **Request spool**): off unless the
        # setup asks for it, and capped at the role the setup declares. The
        # permission hook is wired HERE because the role model is the session
        # layer's and the engine may not import it (contract C12) — the same
        # reason the run catalog is handed down rather than discovered below.
        spool_config = read_request_spool_config(build["used_path"])
        request_spool = (
            RequestSpool(
                max_role=spool_config["max_role"], authorizer=authorize_spooled
            )
            if spool_config["enabled"]
            else None
        )
        return {
            "tick_interval_ms": 3000,
            "manual_override_timeout_s": safety_config["manual_override_timeout_s"],
            "stall_seconds": safety_config["stall_seconds"],
            "hold_enforcement_interval_s": safety_config[
                "hold_enforcement_interval_s"
            ],
            "hold_enforcement_max_attempts": safety_config[
                "hold_enforcement_max_attempts"
            ],
            "run_catalog": run_catalog,
            "request_spool": request_spool,
        }

    # The instrument-thread flag. Read from the config the app is ABOUT to
    # load rather than the one it ends up with, because which thread owns the
    # stack has to be decided before anything is built; a fallback to another
    # config does not change the mode. `CRYOSOFT_INSTRUMENT_THREAD` overrides
    # it for one launch, which is how CI runs the GUI suite in both modes.
    mode = resolve_mode(read_instrument_thread(_startup_candidates()[0]))

    # Trend-check standard (core/trend_checks.py, GLOSSARY.md's **Trend
    # check**): a small, single-purpose scheduler independent of the
    # Orchestrator — it holds only a Station, never an Orchestrator — that
    # evaluates this setup's declared checks on its own slow timer and
    # publishes failing ones as advisory-severity conditions. It is a STATION
    # COMPANION: it holds the Station, so the host builds it wherever the
    # Station lives and stops it there too.
    def _build_trend_check_runner(station: Station) -> TrendCheckRunner:
        """Build the trend-check scheduler beside the Station it publishes into."""
        trends_config = read_trends_config(build["used_path"])
        return TrendCheckRunner(
            station,
            declared_checks(trends_config),
            refresh_interval_s=trends_config["refresh_interval_s"],
        )

    app.instrument_host = InstrumentHost(
        _build_station,
        mode=mode,
        orchestrator_options=_orchestrator_options,
        station_companions=(_build_trend_check_runner,),
    )
    app.instrument_host.start()
    # Process lifecycle, owned here: the host stops the tick timer on the
    # thread that owns it and, in threaded mode, joins the instrument thread
    # with a bounded wait, so a wedged instrument read can never leave the
    # application unexitable.
    app.aboutToQuit.connect(app.instrument_host.shutdown)
    station = app.instrument_host.station
    used_path = build["used_path"]
    warnings = build["warnings"]

    # Persist the config that actually loaded (by identity, not path) so the
    # next launch starts there even from a different clone/worktree.
    used_entry = catalog.get_by_path(used_path)
    if used_entry is not None:
        app_settings.set_config_active(used_entry.name, used_entry.source)
    if warnings:
        for warning in warnings:
            logger.warning("Startup config fallback: %s", warning)
    for offline_name in station.offline_vi_names():
        logger.warning(
            "Instrument offline at startup: %s (%s)",
            offline_name,
            station.get_offline_info(offline_name).reason,
        )

    # The control contract, rendered for this process's clients: one typed
    # method per command, the engine's signals re-exposed, and every read
    # answered from a status mirror the host primed on the engine's own
    # thread. Nothing above this line hands the engine itself to anyone.
    orchestrator = app.instrument_host.build_proxy()
    mirror = orchestrator.status

    # Session layer (L6 + the Session tier above it). measurement_root() is
    # the fixed, machine-level, admin-set root (never derived from the Data
    # Directory form field, which is itself now *derived from* the open
    # experiment — see cryosoft.core.paths.measurement_root()). SessionStore
    # owns the Session tier: sessions/<user_id>/<session_id>/ folders one
    # level above experiments, nested per owner so ownership is structural,
    # not just a field inside session.json. _ensure_guest_user_registered()
    # guarantees the fixed Guest roster identity exists before anything looks
    # it up; _resolve_active_session() auto-creates a bootstrap session on
    # first-ever launch (or a corrupt/legacy-shape pointer) so the app never
    # refuses to start for lack of an explicit session choice, owned by
    # whoever is logged in or Guest otherwise. ExperimentStore is then rooted
    # two levels deeper, inside that one active session's own folder —
    # switching sessions (User menu, Resume Session…) only updates
    # SessionStore's active pointer and takes effect on the next launch;
    # ExperimentManager keeps this ExperimentStore for the process lifetime:
    # it is always constructed with one real, already-resolved
    # ExperimentStore and grows no live rebind machinery (GLOSSARY.md's
    # Session). The user roster relocates to
    # measurement_root()/"users.json", alongside "sessions/" and
    # "servicing/".
    roster = UserRoster(measurement_root() / "users.json")
    _ensure_guest_user_registered(roster)
    session_store = SessionStore(measurement_root() / "sessions")
    active_user_id, active_session_id = _resolve_active_session(session_store)
    session_manager = ExperimentManager(
        store=ExperimentStore(measurement_root() / "sessions" / active_user_id / active_session_id),
        roster=roster,
        orchestrator=orchestrator,
        config_name=used_entry.name if used_entry is not None else Path(used_path).name,
        config_path=used_path,
        session_store=session_store,
        station=station,
        run_catalog=run_catalog,
    )

    # The pull seam (GLOSSARY.md's **Run queue**): the engine ASKS the session
    # layer's queue for the next run rather than holding one of its own, and
    # reads the waiting entries from it for every QueueChanged event. Wired
    # here because this is the one place that owns both objects — the queue
    # cannot exist before the engine it broadcasts through, and the engine
    # must not import the session layer (contract C12). It is installed
    # through the proxy, like every other call a client makes here.
    orchestrator.install_run_queue(
        next_run=session_manager.next_run,
        queue_entries=session_manager.queue_entries,
        take_next_spec=session_manager.take_next_spec,
        build_spec=session_manager.build_spec,
    )

    # The Gateway server (cryosoft/session/gateway/local_server.py,
    # GLOSSARY.md's **Gateway server**): a local socket carrying the same
    # Gateway an in-process client holds, one per connection, so an agent in
    # its own process is authorised, fed and seen exactly as an in-process
    # one. Off unless this setup's monitor.yaml turns it on, and never
    # handing out more than the role that file names — opening a station to
    # autonomous clients is a setup decision, so it lives in the config like
    # every limit. It is a QLocalServer on THIS event loop, and it is built
    # over the PROXY: the server runs on the GUI thread while the engine runs
    # on the instrument thread, so a frame is parsed in a slot that posts its
    # command across like every other client — no thread of its own, and no
    # second writer to the bus. Wired here because this is the one place that
    # owns both the proxy and the session layer; attached to `app` so its
    # ownership is explicit, like every other QObject built in main().
    gateway_config = read_gateway_config(used_path)
    app.gateway_server = _build_gateway_server(
        orchestrator,
        gateway_config,
        station_info=station.station_info,
        tool_context=ToolContext(experiments=session_manager, run_catalog=run_catalog),
        feed=lambda: _open_experiment_feed(session_manager),
    )
    if app.gateway_server is not None:
        # Stopping on quit is what keeps the descriptor honest: a gateway.json
        # left behind names a socket that is gone and a token that means
        # nothing, and an adapter reading it reports "cannot connect" instead
        # of "the app is not running". The socket itself is reclaimed either
        # way — start() removes a stale one — so this exists for the
        # descriptor's sake.
        app.aboutToQuit.connect(app.gateway_server.stop)

    # The Embedded assistant (cryosoft/session/assistant/, GLOSSARY.md's
    # **Embedded assistant**): the physicist's chat client for this
    # experiment, and deliberately NOT a third client of the engine — it holds
    # one Gateway, its tools ARE that gateway's tool surface and its tool
    # execution IS that gateway's call_tool(), so it is authorised, fed and
    # seen exactly as an agent in another process. Off unless this setup's
    # monitor.yaml says so, and never offering more authority than that file
    # names (falling back to the ceiling it already grants an out-of-process
    # client, since the assistant is one). Its Gateway is built over the
    # PROXY, like the gateway server's: the assistant runs on the GUI thread,
    # and the proxy is what a client on this side of the instrument thread
    # holds — it satisfies the control contract's client surface
    # (EngineClient: submit + the two streams) exactly as the engine does.
    #
    # Two things are resolved at call time rather than once: the experiment's
    # Agent feed and its Assistant transcript, so a conversation had after the
    # physicist opened a new experiment is recorded in that experiment's
    # folder.
    assistant_config = read_assistant_config(used_path)
    assistant_max_role = (
        assistant_config["assistant_max_role"] or gateway_config["gateway_max_role"]
    )
    # Read once and shared with the publisher below: the assistant's model,
    # key, token cap and price table live in the same user-level settings file
    # the notebook's do, and two reads could disagree.
    eln_settings = load_eln_settings()

    def _assistant_gateway(role: str) -> Gateway:
        """Build the assistant's connection under one role."""
        return Gateway(
            orchestrator,
            role,
            "assistant",
            station_info=station.station_info,
            tool_context=ToolContext(
                experiments=session_manager, run_catalog=run_catalog
            ),
            feed=_open_experiment_feed(session_manager),
        )

    app.assistant_runtime = None
    if assistant_config["assistant"]:
        try:
            # The client first: with no key (or without the optional extra
            # installed) there is nothing to connect a gateway for.
            chat_client = AnthropicChatClient(eln_settings.assistant)
            app.assistant_runtime = AssistantRuntime(
                _assistant_gateway(assistant_max_role),
                chat_client,
                transcript=_open_assistant_transcript(session_manager),
                parent=app,
            )
        except (AssistantError, ValueError):
            # No key, no optional extra, or an unknown role in the config: the
            # dock is still registered and says so in one line, because a
            # missing key is a configuration fact and not a fault that should
            # take the window with it.
            logger.exception("Embedded assistant not started")

    # ELN publishing (cryosoft/session/eln/): entirely opt-in and entirely
    # GUI-side. With no user-level settings file — the default — the
    # publisher is built, finds nothing configured, and does nothing: the
    # drain timer never starts and on_run_finished() returns immediately, so
    # a setup that has no notebook carries no footprint. The timer lives HERE,
    # in the application entry point, rather than in the Orchestrator, for the
    # same reason all network I/O does: it must never share the tick that
    # writes to hardware. Attached to `app` so its ownership is explicit — a QObject with no Python reference is eligible
    # for GC regardless of Qt-side parenting.
    app.eln_publisher = ElnPublisher(session_manager, eln_settings)
    orchestrator.run_finished.connect(app.eln_publisher.on_run_finished)
    # The other direction of the same seam: the manager holds the publisher so
    # that approving a **draft entry** parked on a run record can queue it.
    session_manager.attach_eln_publisher(app.eln_publisher)
    app.eln_publisher.start()

    # Cryogenics management:
    # config-gated like every optional feature — a setup without a
    # cryogenics: block (or without the level VI it names) carries zero
    # footprint and this whole block is a no-op. Stores are rooted at
    # measurement_root()/"servicing" — a Setup-tier location sibling to
    # "sessions/" (flat, never inside a session or experiment folder), since
    # these records describe the rig across all sessions and must not keep
    # depending on the Data Directory form field now that it is derived from
    # whichever experiment is open. The
    # same store instances feed both the automatic recorder and the Monitor
    # window's Cryogenics panel / Logs page, so both always see the same data.
    cryogenics_config = read_cryogenics_config(used_path)
    # Operations panel: declared operations.<key>: config blocks,
    # GUI-safe to read unconditionally (empty {} when the setup declares
    # none) — the panel decides which discovered class each key maps to.
    operations_config = read_operations_config(used_path)
    cryogenics_recorder: CryogenicsRecorder | None = None
    helium_store: HeliumRecordStore | None = None
    servicing_store: ServicingLogStore | None = None
    servicing_log_kinds: list[str] = []
    if cryogenics_config and station.has_vi(cryogenics_config["level_vi"]):
        servicing_root = measurement_root() / "servicing"
        config_identity = (
            used_entry.name if used_entry is not None else Path(used_path).name
        )
        helium_store = HeliumRecordStore(servicing_root, config_identity)
        servicing_store = ServicingLogStore(servicing_root, config_identity)
        # One-time migration of any pre-unification cryogenics.jsonl/
        # operations.jsonl into servicing.jsonl. Idempotent no-op once
        # servicing.jsonl exists or neither legacy file is present, so it is
        # always safe to call unconditionally on every startup.
        servicing_store.migrate_legacy(level_vi_name=cryogenics_config["level_vi"])
        servicing_log_kinds = read_servicing_logs_config(used_path)
        cryogenics_recorder = CryogenicsRecorder(
            helium_store,
            servicing_store,
            level_vi_name=cryogenics_config["level_vi"],
            history_sample_s=float(cryogenics_config["history_sample_s"]),
        )
        orchestrator.states_updated.connect(cryogenics_recorder.on_states_updated)
        orchestrator.run_started.connect(cryogenics_recorder.on_run_started)
        orchestrator.run_finished.connect(cryogenics_recorder.on_run_finished)
        logger.info(
            "Cryogenics recorder active (level_vi=%s, config=%s)",
            cryogenics_config["level_vi"],
            config_identity,
        )

    monitor = MonitorWindow(
        station,
        orchestrator,
        catalog=catalog,
        active_config_path=used_path,
        restart_callback=_restart_application,
        startup_warning="; ".join(warnings) if warnings else None,
        session_manager=session_manager,
        session_store=session_store,
        cryogenics_config=cryogenics_config or None,
        operations_config=operations_config or None,
        helium_store=helium_store,
        servicing_store=servicing_store,
        servicing_log_kinds=servicing_log_kinds,
        cryogenics_recorder=cryogenics_recorder,
        panels_config=read_panels_config(used_path),
        mirror=mirror,
        assistant_enabled=bool(assistant_config["assistant"]),
        assistant_runtime=app.assistant_runtime,
        assistant_max_role=assistant_max_role,
        assistant_role_factory=_assistant_gateway,
    )
    monitor.show()

    # The Orchestrator's tick timer starts in __init__, but monitoring starts
    # OFF: no instrument is polled (and no communication errors can fire)
    # until the user starts monitoring from the Monitor window's header
    # toggle, normally after "Initiate All" has brought the instruments up.
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
