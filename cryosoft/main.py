# ---
# description: |
#   CryoSoft application entry point. Builds the Station from the sim_cryostat
#   config, starts the Orchestrator, and opens the Monitor and Procedure windows.
# entry_point: python -m cryosoft.main  OR  python cryosoft/main.py
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (build_station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.core.logging_config (setup_logging)
#   - cryosoft.gui.monitor_window (MonitorWindow)
# input: |
#   No CLI arguments. Config path is hardcoded to cryosoft/configs/sim_cryostat.
# process: |
#   Initialises logging, creates QApplication, builds the Station and Orchestrator,
#   opens both windows, starts the Orchestrator timer, and enters the Qt event loop.
# output: |
#   The running CryoSoft desktop application. Exits when all windows are closed.
# last_updated: 2026-04-16
# ---

"""CryoSoft application entry point."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from cryosoft.core.logging_config import setup_logging
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station
from cryosoft.gui.monitor_window import MonitorWindow


def main() -> None:
    """Start the CryoSoft application."""
    setup_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("CryoSoft")
    app.setApplicationVersion("0.1.0")

    station = build_station("cryosoft/configs/sim_cryostat")
    orchestrator = Orchestrator(station, tick_interval_ms=3000)

    monitor = MonitorWindow(station, orchestrator)
    monitor.show()

    # The Orchestrator timer starts automatically in __init__.
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
