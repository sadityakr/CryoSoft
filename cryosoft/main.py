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
#   actually loaded, constructs the session layer (ExperimentStore rooted at
#   app_settings.sessions_root() + UserRoster + SessionManager wired to the
#   Orchestrator — sessions_root() is a dedicated, app-settings-backed
#   location, decoupled from the Data Directory form field), then — only when
#   the active config declares a cryogenics: block AND the station has the
#   level VI it names (docs/plans/cryogenics-logbook.md §9) — builds a
#   HeliumRecordStore/ServicingLogStore rooted at sessions_root()/"servicing"
#   (a sibling of the experiment folders, never inside one), constructs a
#   CryogenicsRecorder, and connects it to the Orchestrator's
#   states_updated/run_started/run_finished signals. Reads the operations:
#   config block (read_operations_config(), GUI-safe, {} when undeclared)
#   unconditionally. Opens the Monitor (passing the catalog, session manager,
#   a restart callback, any fallback warning, the operations: config, and —
#   when cryogenics is active — the same store instances, config, and
#   recorder, so the Monitor window's Operations panel and Logs page share
#   the recorder's data), and enters the Qt event loop.
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
from cryosoft.session.manager import SessionManager
from cryosoft.session.servicing_log import (
    CryogenicsRecorder,
    HeliumRecordStore,
    ServicingLogStore,
)
from cryosoft.session.store import ExperimentStore, UserRoster

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

    # Session layer (L6). Experiment records live under the dedicated,
    # app-settings-backed sessions_root() — a deliberate machine-level
    # setting, never derived from the Data Directory form field (which is
    # itself now *derived from* the open session — see
    # docs/plans/unified-session-record.md §4/§8). The user roster stays
    # setup-local, next to the app-settings files.
    session_manager = SessionManager(
        store=ExperimentStore(app_settings.sessions_root()),
        roster=UserRoster(app_settings.session_file_path().parent / "users.json"),
        orchestrator=orchestrator,
        station=station,
        config_name=used_entry.name if used_entry is not None else Path(used_path).name,
        config_path=used_path,
    )

    # Cryogenics management (Phase 3/5, docs/plans/cryogenics-logbook.md §9/§10):
    # config-gated like every optional feature — a setup without a
    # cryogenics: block (or without the level VI it names) carries zero
    # footprint and this whole block is a no-op. Stores are rooted at
    # sessions_root()/"servicing" — a Setup-tier location sibling to (never
    # inside) any one experiment folder, since these records describe the rig
    # across all sessions and must not keep depending on the Data Directory
    # form field now that it is derived from whichever session is open. The
    # same store instances feed both the automatic recorder and the Monitor
    # window's Cryogenics panel / Logs page, so both always see the same data.
    cryogenics_config = read_cryogenics_config(used_path)
    # Operations panel (plan §12): declared operations.<key>: config blocks,
    # GUI-safe to read unconditionally (empty {} when the setup declares
    # none) — the panel decides which discovered class each key maps to.
    operations_config = read_operations_config(used_path)
    cryogenics_recorder: CryogenicsRecorder | None = None
    helium_store: HeliumRecordStore | None = None
    servicing_store: ServicingLogStore | None = None
    servicing_log_kinds: list[str] = []
    if cryogenics_config and station.has_vi(cryogenics_config["level_vi"]):
        servicing_root = app_settings.sessions_root() / "servicing"
        config_identity = (
            used_entry.name if used_entry is not None else Path(used_path).name
        )
        helium_store = HeliumRecordStore(servicing_root, config_identity)
        servicing_store = ServicingLogStore(servicing_root, config_identity)
        # One-time migration of any pre-unification cryogenics.jsonl/
        # operations.jsonl into servicing.jsonl (docs/plans/unified-
        # servicing-log-and-run-recording.md §2). Idempotent no-op once
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
