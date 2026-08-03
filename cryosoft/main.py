# ---
# description: |
#   CryoSoft application entry point. Resolves the active config (with a safe
#   fallback chain), builds the Station and Orchestrator, and opens the Monitor
#   window with the config catalog wired in.
# entry_point: python -m cryosoft.main  OR  python cryosoft/main.py
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (build_station_with_fallback)
#   - cryosoft.core.config_catalog (ConfigCatalog)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.core.logging_config (setup_logging)
#   - cryosoft.gui.monitor_window (MonitorWindow)
# input: |
#   No CLI arguments. The active config's (name, source) identity is read from
#   QSettings (ActiveConfig/name, ActiveConfig/source) and re-resolved to a
#   directory at startup; if unset, invalid, or unloadable, the startup
#   fallback chain lands on the always-safe sim_cryostat config.
# process: |
#   Initialises logging, creates QApplication, builds the ConfigCatalog, resolves
#   the Station via build_station_with_fallback(), persists the config that
#   actually loaded, then wires the session layer in three steps (see
#   "Startup wiring (decided)" in docs/plans/session-tier-and-terminology.md):
#   (1) resolve measurement_root() and construct one SessionStore(root); (2)
#   read SessionStore.get_active(), auto-creating and activating a bootstrap
#   session when unset or unloadable, so the app never fails to start for
#   lack of an explicit session choice; (3) construct ExperimentStore rooted
#   at that session's own folder (measurement_root()/"sessions"/session_id)
#   and pass it, plus UserRoster (rooted at measurement_root()/"users.json")
#   and the Orchestrator, to ExperimentManager exactly as before. Switching
#   sessions (the User menu's Resume Session… action) only updates
#   SessionStore's active pointer — it takes effect on the next launch;
#   ExperimentManager keeps the ExperimentStore it was constructed with for
#   the lifetime of this process. Then — only when
#   the active config declares a cryogenics: block AND the station has the
#   level VI it names — builds a
#   HeliumRecordStore/ServicingLogStore rooted at measurement_root()/"servicing"
#   (a sibling of "sessions/", flat at the measurement root, never inside a
#   session or experiment folder), constructs a
#   CryogenicsRecorder, and connects it to the Orchestrator's
#   states_updated/run_started/run_finished signals. Reads the operations:
#   config block (read_operations_config(), GUI-safe, {} when undeclared)
#   unconditionally. Opens the Monitor (passing the catalog, session manager,
#   the SessionStore, a restart callback, any fallback warning, the
#   operations: config, and — when cryogenics is active — the same store
#   instances, config, and recorder, so the Monitor window's Operations panel
#   and Logs page share the recorder's data), and enters the Qt event loop.
# output: |
#   The running CryoSoft desktop application. Exits when all windows are closed.
# ---

"""CryoSoft application entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

from cryosoft.core.config_catalog import ConfigCatalog
from cryosoft.core.logging_config import setup_logging
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.paths import measurement_root
from cryosoft.core.station import (
    build_station_with_fallback,
    read_cryogenics_config,
    read_operations_config,
    read_panels_config,
    read_servicing_logs_config,
)
from cryosoft.gui import app_settings
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.theme import PLOT_AXIS, PLOT_BG, build_stylesheet
from cryosoft.session.manager import ExperimentManager
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


def _resolve_active_session(store: SessionStore) -> str:
    """Return the active session id, auto-creating a bootstrap session if needed.

    The app must never fail to start for lack of an explicit session choice
    (see ``docs/plans/session-tier-and-terminology.md``, "Startup wiring
    (decided)"): when the store's active pointer is unset, or points at a
    session record that fails to load (first-ever launch, or a corrupt
    pointer), a bootstrap session is created and activated on the spot.

    Args:
        store: The ``SessionStore`` rooted at ``measurement_root() / "sessions"``.

    Returns:
        The active session's id — either the one already pointed to, or a
        freshly created bootstrap session's.
    """
    active_id = store.get_active()
    if active_id is not None and store.load(active_id) is not None:
        return active_id
    user_id = app_settings.current_user_id() or ""
    session = store.create_session(name=user_id or "default", user_id=user_id)
    store.set_active(session.session_id)
    return session.session_id


def _restart_application() -> None:
    """Relaunch the app in a fresh process and quit this one.

    Used after a config switch (which needs a clean rebuild of the whole
    instrument stack). ``startDetached`` launches an independent process before
    this one exits, so the window closes and reopens.
    """
    QProcess.startDetached(sys.executable, sys.argv)
    QApplication.quit()


def main() -> None:
    """Start the CryoSoft application."""
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

    orchestrator = Orchestrator(station, tick_interval_ms=3000)

    # Session layer (L6 + the Session tier above it). measurement_root() is
    # the fixed, machine-level, admin-set root (never derived from the Data
    # Directory form field, which is itself now *derived from* the open
    # experiment — see cryosoft.core.paths.measurement_root()). SessionStore
    # owns the Session tier: sessions/<session_id>/ folders one level above
    # experiments. _resolve_active_session() auto-creates a bootstrap session
    # on first-ever launch (or a corrupt pointer) so the app never refuses to
    # start for lack of an explicit session choice. ExperimentStore is then
    # rooted one level deeper, inside that one active session's own folder —
    # switching sessions (User menu, Resume Session…) only updates
    # SessionStore's active pointer and takes effect on the next launch;
    # ExperimentManager keeps this ExperimentStore for the process lifetime
    # (see "Startup wiring (decided)" in
    # docs/plans/session-tier-and-terminology.md). The user roster relocates
    # to measurement_root()/"users.json", alongside "sessions/" and
    # "servicing/".
    session_store = SessionStore(measurement_root() / "sessions")
    active_session_id = _resolve_active_session(session_store)
    session_manager = ExperimentManager(
        store=ExperimentStore(measurement_root() / "sessions" / active_session_id),
        roster=UserRoster(measurement_root() / "users.json"),
        orchestrator=orchestrator,
        station=station,
        config_name=used_entry.name if used_entry is not None else Path(used_path).name,
        config_path=used_path,
    )

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
            warning_pct=float(cryogenics_config["helium_warning_pct"]),
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
    )
    monitor.show()

    # The Orchestrator's tick timer starts in __init__, but monitoring starts
    # OFF: no instrument is polled (and no communication errors can fire)
    # until the user starts monitoring from the Monitor window's header
    # toggle, normally after "Initiate All" has brought the instruments up.
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
