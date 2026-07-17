# ---
# description: |
#   LogPanel: the read-only real-time log view (objectName ``log_panel``) plus
#   QtLogHandler, the logging.Handler that appends coloured HTML lines to it.
#   Extracted from monitor_window.py so the log concern (widget + handler +
#   attach/detach lifecycle) lives in one module.
# entry_point: Not run directly. Hosted by MonitorWindow's bottom-right quadrant.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.gui.theme (LOG_* colours)
# input: |
#   Python logging records from the shared "cryosoft" logger.
# process: |
#   attach() adds the handler to the "cryosoft" logger (guarding against a
#   duplicate); detach() removes it so nothing writes to a destroyed widget.
#   The handler drops per-method VI polling noise below WARNING and trims the
#   document to a line cap so a long run cannot grow memory without bound.
# output: |
#   Coloured, timestamped log lines in the panel.
# ---

"""LogPanel — real-time log widget and its Qt logging handler."""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import QSizePolicy, QTextEdit

from cryosoft.gui.theme import (
    LOG_CRITICAL,
    LOG_DEBUG,
    LOG_ERROR,
    LOG_INFO,
    LOG_WARNING,
    TEXT_PRIMARY,
)

_LOG_MAX_LINES = 500


class QtLogHandler(logging.Handler):
    """Logging handler that appends coloured HTML lines to a QTextEdit.

    Args:
        widget: The read-only QTextEdit to write into.
    """

    _LEVEL_COLOURS: dict[int, str] = {
        logging.DEBUG: LOG_DEBUG,
        logging.INFO: LOG_INFO,
        logging.WARNING: LOG_WARNING,
        logging.ERROR: LOG_ERROR,
        logging.CRITICAL: LOG_CRITICAL,
    }

    def __init__(self, widget: QTextEdit) -> None:
        super().__init__()
        self._widget = widget
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        # Per-method VI polling noise is written to the file log only.
        if record.name.startswith("cryosoft.vi.") and record.levelno < logging.WARNING:
            return
        try:
            widget = self._widget
            if not widget or not widget.isVisible():
                return
            text = self.format(record)
            colour = self._LEVEL_COLOURS.get(record.levelno, TEXT_PRIMARY)
            bold_open = "<b>" if record.levelno >= logging.CRITICAL else ""
            bold_close = "</b>" if record.levelno >= logging.CRITICAL else ""
            html = f'<span style="color:{colour};">{bold_open}{text}{bold_close}</span>'
            widget.append(html)

            # Trim to _LOG_MAX_LINES to avoid unbounded growth
            doc = widget.document()
            while doc.blockCount() > _LOG_MAX_LINES:
                cursor = widget.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                cursor.select(cursor.SelectionType.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

        except Exception:  # noqa: BLE001
            self.handleError(record)


class LogPanel(QTextEdit):
    """Read-only real-time log display (objectName ``log_panel``).

    Owns its ``QtLogHandler``. The hosting window calls :meth:`attach` after
    construction and :meth:`detach` from its ``closeEvent`` so the handler
    never writes to a destroyed widget (RuntimeError on a dead widget).
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("log_panel")
        self.setReadOnly(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.handler = QtLogHandler(self)
        self.handler.setLevel(logging.DEBUG)

    def attach(self) -> None:
        """Add the handler to the shared "cryosoft" logger.

        Guards against a duplicate in case the hosting window is ever
        reconstructed within the same process (handlers live on the shared
        logger, so a leak would accumulate).
        """
        cryosoft_logger = logging.getLogger("cryosoft")
        if self.handler not in cryosoft_logger.handlers:
            cryosoft_logger.addHandler(self.handler)

    def detach(self) -> None:
        """Remove the handler from the shared "cryosoft" logger."""
        logging.getLogger("cryosoft").removeHandler(self.handler)
