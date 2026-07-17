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
#   actually loaded, constructs the session layer (ExperimentStore rooted in the
#   data dir + UserRoster + SessionManager wired to the Orchestrator), opens the
#   Monitor (passing the catalog, session manager, a restart callback, and any
#   fallback warning), and enters the Qt event loop.
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
from cryosoft.core.station import build_station_with_fallback
from cryosoft.gui import app_settings, form_autosave
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.theme import PLOT_AXIS, PLOT_BG, build_stylesheet
from cryosoft.session.manager import SessionManager
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

    orchestrator = Orchestrator(station, tick_interval_ms=3000)

    # Session layer (L6). Experiment records live inside the data directory
    # (they archive with the data they describe); the user roster is
    # setup-local, next to the app-settings files. The data directory comes
    # from the form autosave — the same value the GUI restores into its field.
    autosave = form_autosave.load(app_settings.session_file_path())
    session_manager = SessionManager(
        store=ExperimentStore(Path(autosave.data_dir) / "experiments"),
        roster=UserRoster(app_settings.session_file_path().parent / "users.json"),
        orchestrator=orchestrator,
        station=station,
        config_name=used_entry.name if used_entry is not None else Path(used_path).name,
    )

    monitor = MonitorWindow(
        station,
        orchestrator,
        catalog=catalog,
        active_config_path=used_path,
        restart_callback=_restart_application,
        startup_warning="; ".join(warnings) if warnings else None,
        session_manager=session_manager,
    )
    monitor.show()

    # The Orchestrator timer starts automatically in __init__.
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
