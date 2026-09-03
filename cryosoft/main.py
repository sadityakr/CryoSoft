"""CryoSoft application entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

import pyqtgraph as pg
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

from cryosoft.core.config_catalog import ConfigCatalog
from cryosoft.core.logging_config import setup_logging
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.paths import measurement_root
from cryosoft.core.station import (
    Station,
    build_station_with_fallback,
    read_cryogenics_config,
    read_operations_config,
    read_panels_config,
    read_safety_config,
    read_servicing_logs_config,
    read_trends_config,
)
from cryosoft.core.trend_check_runner import TrendCheckRunner
from cryosoft.core.trend_checks import declared_checks
from cryosoft.gui import app_settings
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.status_mirror import StatusMirror
from cryosoft.gui.theme import PLOT_AXIS, PLOT_BG, build_stylesheet
from cryosoft.session.eln.publisher import ElnPublisher
from cryosoft.session.eln.settings import load_eln_settings
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
    station, used_path, warnings = build_station_with_fallback(_startup_candidates())
    if on_station_built is not None:
        on_station_built(station)
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

    safety_config = read_safety_config(used_path)
    orchestrator = Orchestrator(
        station,
        tick_interval_ms=3000,
        manual_override_timeout_s=safety_config["manual_override_timeout_s"],
        stall_seconds=safety_config["stall_seconds"],
        hold_enforcement_interval_s=safety_config["hold_enforcement_interval_s"],
        hold_enforcement_max_attempts=safety_config["hold_enforcement_max_attempts"],
    )

    # The status-mirror standard (GLOSSARY.md's **Status mirror**): the GUI
    # answers every read from this mirror and never calls into the engine.
    # Built and primed HERE, next to the engine, because the event stream is
    # a broadcast — the declaration emitted at construction is already gone
    # by the time a widget exists — and because the priming reads must be
    # taken on the engine's own thread.
    mirror = StatusMirror.for_engine(orchestrator)

    # Trend-check standard (core/trend_checks.py, GLOSSARY.md's **Trend
    # check**): a small, single-purpose scheduler independent of the
    # Orchestrator — it holds only a Station, never an Orchestrator — that
    # evaluates this setup's declared checks on its own slow timer and
    # publishes failing ones as advisory-severity conditions. Attached to
    # `app` (rather than left as a bare local) so its ownership is explicit:
    # a QObject with no Python reference is eligible for GC regardless of
    # Qt-side parenting, and `app` outlives everything else built in main().
    trends_config = read_trends_config(used_path)
    app.trend_check_runner = TrendCheckRunner(
        station,
        declared_checks(trends_config),
        refresh_interval_s=trends_config["refresh_interval_s"],
    )

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
    )

    # ELN publishing (cryosoft/session/eln/): entirely opt-in and entirely
    # GUI-side. With no user-level settings file — the default — the
    # publisher is built, finds nothing configured, and does nothing: the
    # drain timer never starts and on_run_finished() returns immediately, so
    # a setup that has no notebook carries no footprint. The timer lives HERE,
    # in the application entry point, rather than in the Orchestrator, for the
    # same reason all network I/O does: it must never share the tick that
    # writes to hardware. Attached to `app` (like trend_check_runner) so its
    # ownership is explicit — a QObject with no Python reference is eligible
    # for GC regardless of Qt-side parenting.
    app.eln_publisher = ElnPublisher(session_manager, load_eln_settings())
    orchestrator.run_finished.connect(app.eln_publisher.on_run_finished)
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
    )
    monitor.show()

    # The Orchestrator's tick timer starts in __init__, but monitoring starts
    # OFF: no instrument is polled (and no communication errors can fire)
    # until the user starts monitoring from the Monitor window's header
    # toggle, normally after "Initiate All" has brought the instruments up.
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
