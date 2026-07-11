# ---
# description: |
#   Central theme module for CryoSoft. Defines the color palette and
#   produces the application-wide Qt Style Sheet (QSS) via build_stylesheet().
# entry_point: imported by cryosoft/main.py and gui modules
# dependencies:
#   - PyQt6 >= 6.5 (no runtime import — pure string generation)
# input: |
#   None. All values are module-level constants.
# process: |
#   build_stylesheet() concatenates QSS rule blocks for every widget type
#   used in the application and returns the full string for QApplication.setStyleSheet().
# output: |
#   A QSS string and module-level color/class constants used by widget code.
# last_updated: 2026-04-17
# ---

"""CryoSoft application theme — color palette and global QSS."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Background layers
# ---------------------------------------------------------------------------
BG_BASE = "#121212"      # window / overall background
BG_SURFACE = "#252526"   # cards, panels, group boxes
BG_ELEVATED = "#2D2D30"  # inputs, dropdowns, slightly raised surface

# ---------------------------------------------------------------------------
# Accent and status
# ---------------------------------------------------------------------------
ACCENT = "#007ACC"        # primary action (buttons, focus rings, progress)
STATUS_OK = "#28A745"     # green  — connected / running
STATUS_WARN = "#FFC107"   # amber  — stale data
STATUS_ERROR = "#DC3545"  # red    — disconnected / emergency

# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
TEXT_PRIMARY = "#D4D4D4"
TEXT_FADED = "#888888"
TEXT_ON_ACCENT = "#FFFFFF"

# ---------------------------------------------------------------------------
# Log level colors (referenced both here and in _QtLogHandler HTML spans)
# ---------------------------------------------------------------------------
LOG_DEBUG = "#606060"
LOG_INFO = "#D4D4D4"
LOG_WARNING = "#CE9178"
LOG_ERROR = "#F44747"
LOG_CRITICAL = "#FF4040"

# ---------------------------------------------------------------------------
# Dynamic property values for QSS class-based targeting
# Usage: widget.setProperty("class", BTN_CLASS_PRIMARY)
# QSS:   QPushButton[class="primary"] { ... }
# ---------------------------------------------------------------------------
BTN_CLASS_PRIMARY = "primary"
BTN_CLASS_SECONDARY = "secondary"
BTN_CLASS_DANGER = "danger"


def build_stylesheet() -> str:
    """Return the full application QSS string.

    Applied once on QApplication in main.py. All widgets inherit these rules.
    Widget-specific rules use objectName or dynamic 'class' properties for
    targeting — never objectName for styling when tests use it for findChild().
    """
    return f"""
/* ── Base ─────────────────────────────────────────────────────────────── */
QWidget {{
    background-color: {BG_BASE};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI";
    font-size: 10pt;
}}

/* ── Main window / dialog ─────────────────────────────────────────────── */
QMainWindow {{
    background-color: {BG_BASE};
}}

/* ── Group boxes ──────────────────────────────────────────────────────── */
QGroupBox {{
    background-color: {BG_SURFACE};
    border: 2px solid transparent;
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 6px;
    padding-left: 4px;
    padding-right: 4px;
    padding-bottom: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    color: {TEXT_FADED};
    font-size: 9pt;
}}

/* ── Group box status borders (property-driven; set by InstrumentPanel) ── */
/* The base rule reserves a 2px transparent border so switching a panel to a
   coloured status border never changes its geometry — no layout jump. All
   other geometry (radius/margins/padding) is inherited from the base rule. */
QGroupBox[status="stale"] {{
    border: 2px solid {STATUS_WARN};
}}
QGroupBox[status="disconnected"] {{
    border: 2px solid {STATUS_ERROR};
}}
QGroupBox[status="disconnected"]::title {{
    color: {STATUS_ERROR};
}}

/* ── Buttons — base (secondary) ──────────────────────────────────────── */
QPushButton {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    border: 1px solid #555555;
    border-radius: 4px;
    padding: 4px 12px;
    font-size: 10pt;
}}
QPushButton:hover {{
    border-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}
QPushButton:pressed {{
    background-color: #1a1a2e;
}}
QPushButton:disabled {{
    color: #555555;
    border-color: #333333;
}}

/* ── Buttons — primary ────────────────────────────────────────────────── */
QPushButton[class="primary"] {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
    border: none;
}}
QPushButton[class="primary"]:hover {{
    background-color: #005A9E;
}}
QPushButton[class="primary"]:pressed {{
    background-color: #004175;
}}
QPushButton[class="primary"]:disabled {{
    background-color: #2a3a4a;
    color: #555555;
}}

/* ── Buttons — danger ─────────────────────────────────────────────────── */
QPushButton[class="danger"] {{
    background-color: {STATUS_ERROR};
    color: {TEXT_ON_ACCENT};
    border: none;
    font-weight: bold;
}}
QPushButton[class="danger"]:hover {{
    background-color: #a71d2a;
}}
QPushButton[class="danger"]:pressed {{
    background-color: #7a141f;
}}

/* ── Emergency acknowledge (targeted by objectName) ──────────────────── */
QPushButton#ack_emergency_btn {{
    background-color: {STATUS_ERROR};
    color: {TEXT_ON_ACCENT};
    border: none;
    font-weight: bold;
    font-size: 11pt;
}}
QPushButton#ack_emergency_btn:hover {{
    background-color: #a71d2a;
}}

/* ── Line edits ───────────────────────────────────────────────────────── */
QLineEdit {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 10pt;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled {{
    color: #555555;
    background-color: #1a1a1a;
}}

/* ── Text edit ────────────────────────────────────────────────────────── */
QTextEdit {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    font-size: 10pt;
}}

/* Log panel — targeted by objectName ─────────────────────────────────── */
QTextEdit#log_panel {{
    background-color: #1E1E1E;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 10pt;
    border: none;
    color: {TEXT_PRIMARY};
}}

/* ── Combo boxes ──────────────────────────────────────────────────────── */
QComboBox {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 10pt;
}}
QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    width: 10px;
    height: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
    outline: none;
}}

/* ── Progress bar ─────────────────────────────────────────────────────── */
QProgressBar {{
    background-color: {BG_ELEVATED};
    border: none;
    border-radius: 3px;
    max-height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}

/* ── Scroll areas ─────────────────────────────────────────────────────── */
QScrollArea {{
    border: none;
    background-color: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}

/* ── Scroll bars ──────────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background-color: #1E1E1E;
    width: 8px;
    border-radius: 4px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: #3C3C3C;
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: #555555;
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background-color: #1E1E1E;
    height: 8px;
    border-radius: 4px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: #3C3C3C;
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: #555555;
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── List widgets ─────────────────────────────────────────────────────── */
QListWidget {{
    background-color: #1E1E1E;
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 4px 8px;
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}
QListWidget::item:hover:!selected {{
    background-color: {BG_ELEVATED};
}}

/* ── Splitters ────────────────────────────────────────────────────────── */
QSplitter::handle {{
    background-color: #3C3C3C;
}}
QSplitter::handle:horizontal {{
    width: 4px;
}}
QSplitter::handle:vertical {{
    height: 4px;
}}
QSplitter::handle:hover {{
    background-color: {ACCENT};
}}

/* ── Menu bar ─────────────────────────────────────────────────────────── */
QMenuBar {{
    background-color: #1E1E1E;
    color: {TEXT_PRIMARY};
    border-bottom: 1px solid #3C3C3C;
}}
QMenuBar::item {{
    padding: 4px 10px;
    background-color: transparent;
}}
QMenuBar::item:selected {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}

/* ── Menus ────────────────────────────────────────────────────────────── */
QMenu {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}

/* ── Status bar ───────────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
    font-size: 9pt;
}}
QStatusBar QLabel {{
    background-color: transparent;
    color: {TEXT_ON_ACCENT};
}}

/* ── Labels — value readout (large instrument values) ────────────────── */
QLabel[class="value_readout"] {{
    font-size: 17pt;
    font-weight: bold;
    color: {TEXT_PRIMARY};
    background-color: transparent;
}}

/* ── Labels — secondary / unit / type ────────────────────────────────── */
QLabel[class="secondary_label"] {{
    color: {TEXT_FADED};
    font-size: 9pt;
    background-color: transparent;
}}

/* ── Form layout labels ───────────────────────────────────────────────── */
QLabel {{
    background-color: transparent;
}}

/* ── Tool tips ────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid #3C3C3C;
    padding: 4px;
}}
"""
