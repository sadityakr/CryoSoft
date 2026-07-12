# ---
# description: |
#   Central theme module for CryoSoft. Defines the light "lab" colour palette
#   and produces the application-wide Qt Style Sheet (QSS) via build_stylesheet().
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
# ---

"""CryoSoft application theme — light "lab" colour palette and global QSS."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Background layers
# ---------------------------------------------------------------------------
BG_BASE = "#f9f9f7"      # window / overall background
BG_SURFACE = "#fcfcfb"   # cards, panels, group boxes
BG_ELEVATED = "#ffffff"  # inputs, dropdowns, log panel, lists

# ---------------------------------------------------------------------------
# Borders
# ---------------------------------------------------------------------------
BORDER_HAIRLINE = "#e1e0d9"  # subtle dividers, input borders
BORDER_STRONG = "#c3c2b7"    # stronger control border (secondary button, scroll handle)

# ---------------------------------------------------------------------------
# Accent and status
# ---------------------------------------------------------------------------
ACCENT = "#2a78d6"        # focus rings, selection, splitter hover, combo highlight
STATUS_OK = "#0ca30c"     # green  — connected / running
STATUS_WARN = "#fab219"   # amber  — stale data
STATUS_ERROR = "#d03b3b"  # red    — disconnected / emergency

# ---------------------------------------------------------------------------
# Buttons — primary (filled blue)
# ---------------------------------------------------------------------------
BTN_PRIMARY_FILL = "#256abf"      # white text 5.39:1
BTN_PRIMARY_HOVER = "#1c5cab"
BTN_PRIMARY_PRESSED = "#184f95"
BTN_PRIMARY_DISABLED = "#9ec5f4"  # with white text

# ---------------------------------------------------------------------------
# Buttons — danger (filled red)
# ---------------------------------------------------------------------------
BTN_DANGER_FILL = "#d03b3b"     # white text 4.80:1
BTN_DANGER_HOVER = "#b53232"    # white text 6.06:1
BTN_DANGER_PRESSED = "#9c2a2a"  # derived (darker); white text 7.56:1

# ---------------------------------------------------------------------------
# Buttons — secondary (white, outlined)
# ---------------------------------------------------------------------------
BTN_SECONDARY_PRESSED = "#e8f0fb"  # tinted press feedback

# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#898781"
TEXT_FADED = TEXT_MUTED    # backward-compat alias (was the dark-theme faded token)
TEXT_ON_ACCENT = "#ffffff"

# ---------------------------------------------------------------------------
# Scrollbar
# ---------------------------------------------------------------------------
SCROLL_TRACK = "#f0efec"

# ---------------------------------------------------------------------------
# Status-bar level backgrounds (state-driven, see MonitorWindow)
# ---------------------------------------------------------------------------
STATUS_BAR_DEFAULT_BG = BORDER_HAIRLINE  # neutral / idle
STATUS_BAR_ACTIVE_BG = BTN_PRIMARY_FILL  # running / paused
STATUS_BAR_ERROR_BG = STATUS_ERROR       # error / emergency

# ---------------------------------------------------------------------------
# Log level colors (referenced both here and in _QtLogHandler HTML spans).
# All verified >=4.5:1 on the white (#ffffff) log background.
# ---------------------------------------------------------------------------
LOG_DEBUG = "#6b6963"
LOG_INFO = "#0b0b0b"
LOG_WARNING = "#8a5a00"
LOG_ERROR = "#b3261e"
LOG_CRITICAL = "#8f1d1d"

# ---------------------------------------------------------------------------
# Plot tokens (pyqtgraph). Series pair is a validated colorblind-safe pair,
# both >=3:1 on the plot surface.
# ---------------------------------------------------------------------------
PLOT_BG = "#fcfcfb"
PLOT_AXIS = "#52514e"
PLOT_GRID = "#e1e0d9"
PLOT_SERIES = ["#2a78d6", "#008300"]

# ---------------------------------------------------------------------------
# Notification banner (NotificationBanner) — severity-driven strip colours.
# ---------------------------------------------------------------------------
BANNER_ERROR_BG = "#fbeaea"
BANNER_ERROR_BORDER = "#d03b3b"
BANNER_ERROR_TEXT = "#8f1d1d"
BANNER_WARNING_BG = "#fdf3d7"
BANNER_WARNING_BORDER = "#fab219"
BANNER_WARNING_TEXT = "#8a5a00"

BANNER_SEVERITY_ERROR = "error"
BANNER_SEVERITY_WARNING = "warning"

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
    color: {TEXT_SECONDARY};
    font-size: 9pt;
    font-weight: bold;
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

/* ── Panel name label (InstrumentPanel's custom header row) ───────────── */
/* Replaces QGroupBox::title now that the toggle button sits next to it —
   QGroupBox can't embed a widget next to its native title. */
QLabel[class="panel_name_label"][status="disconnected"] {{
    color: {STATUS_ERROR};
}}

/* ── Buttons — base (secondary) ──────────────────────────────────────── */
QPushButton,
QPushButton[class="secondary"] {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    padding: 4px 12px;
    font-size: 10pt;
}}
QPushButton:hover,
QPushButton[class="secondary"]:hover {{
    border-color: {ACCENT};
}}
QPushButton:pressed,
QPushButton[class="secondary"]:pressed {{
    background-color: {BTN_SECONDARY_PRESSED};
}}
QPushButton:disabled,
QPushButton[class="secondary"]:disabled {{
    color: {TEXT_MUTED};
    border-color: {BORDER_HAIRLINE};
}}

/* ── Buttons — primary ────────────────────────────────────────────────── */
QPushButton[class="primary"] {{
    background-color: {BTN_PRIMARY_FILL};
    color: {TEXT_ON_ACCENT};
    border: none;
}}
QPushButton[class="primary"]:hover {{
    background-color: {BTN_PRIMARY_HOVER};
}}
QPushButton[class="primary"]:pressed {{
    background-color: {BTN_PRIMARY_PRESSED};
}}
QPushButton[class="primary"]:disabled {{
    background-color: {BTN_PRIMARY_DISABLED};
    color: {TEXT_ON_ACCENT};
}}

/* ── Buttons — danger ─────────────────────────────────────────────────── */
QPushButton[class="danger"] {{
    background-color: {BTN_DANGER_FILL};
    color: {TEXT_ON_ACCENT};
    border: none;
    font-weight: bold;
}}
QPushButton[class="danger"]:hover {{
    background-color: {BTN_DANGER_HOVER};
}}
QPushButton[class="danger"]:pressed {{
    background-color: {BTN_DANGER_PRESSED};
}}

/* ── Emergency acknowledge (targeted by objectName) ──────────────────── */
QPushButton#ack_emergency_btn {{
    background-color: {BTN_DANGER_FILL};
    color: {TEXT_ON_ACCENT};
    border: none;
    font-weight: bold;
    font-size: 11pt;
}}
QPushButton#ack_emergency_btn:hover {{
    background-color: {BTN_DANGER_HOVER};
}}

/* ── Line edits ───────────────────────────────────────────────────────── */
QLineEdit {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 10pt;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled {{
    color: {TEXT_MUTED};
    background-color: {BG_BASE};
}}

/* ── Text edit ────────────────────────────────────────────────────────── */
QTextEdit {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
    border-radius: 4px;
    font-size: 10pt;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
}}

/* Log panels — targeted by objectName ────────────────────────────────── */
QTextEdit#log_panel,
QTextEdit#status_log {{
    background-color: {BG_ELEVATED};
    font-family: "Consolas", "Courier New", monospace;
    font-size: 10pt;
    border: 1px solid {BORDER_HAIRLINE};
    color: {TEXT_PRIMARY};
}}

/* ── Combo boxes ──────────────────────────────────────────────────────── */
QComboBox {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
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
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
    outline: none;
}}

/* ── Progress bar ─────────────────────────────────────────────────────── */
QProgressBar {{
    background-color: {BORDER_HAIRLINE};
    border: none;
    border-radius: 3px;
    max-height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {BTN_PRIMARY_FILL};
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
    background-color: {SCROLL_TRACK};
    width: 8px;
    border-radius: 4px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {BORDER_STRONG};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {TEXT_MUTED};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background-color: {SCROLL_TRACK};
    height: 8px;
    border-radius: 4px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: {BORDER_STRONG};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {TEXT_MUTED};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── List widgets ─────────────────────────────────────────────────────── */
QListWidget {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
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
    background-color: {BTN_SECONDARY_PRESSED};
}}

/* ── Splitters ────────────────────────────────────────────────────────── */
QSplitter::handle {{
    background-color: {BORDER_HAIRLINE};
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
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border-bottom: 1px solid {BORDER_HAIRLINE};
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
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}

/* ── Status bar (state-driven via dynamic 'level' property) ──────────── */
QStatusBar {{
    background-color: {STATUS_BAR_DEFAULT_BG};
    color: {TEXT_PRIMARY};
    font-size: 9pt;
}}
QStatusBar QLabel {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
}}
QStatusBar[level="active"] {{
    background-color: {STATUS_BAR_ACTIVE_BG};
    color: {TEXT_ON_ACCENT};
}}
QStatusBar[level="active"] QLabel {{
    color: {TEXT_ON_ACCENT};
}}
QStatusBar[level="error"] {{
    background-color: {STATUS_BAR_ERROR_BG};
    color: {TEXT_ON_ACCENT};
}}
QStatusBar[level="error"] QLabel {{
    color: {TEXT_ON_ACCENT};
}}

/* ── Notification banner (severity-driven via dynamic 'severity' prop) ── */
/* WA_StyledBackground is set on the widget so QSS paints its background. */
QWidget#notification_banner {{
    border-radius: 4px;
}}
QWidget#notification_banner[severity="error"] {{
    background-color: {BANNER_ERROR_BG};
    border-left: 3px solid {BANNER_ERROR_BORDER};
}}
QWidget#notification_banner[severity="error"] QLabel {{
    color: {BANNER_ERROR_TEXT};
    background-color: transparent;
}}
QWidget#notification_banner[severity="warning"] {{
    background-color: {BANNER_WARNING_BG};
    border-left: 3px solid {BANNER_WARNING_BORDER};
}}
QWidget#notification_banner[severity="warning"] QLabel {{
    color: {BANNER_WARNING_TEXT};
    background-color: transparent;
}}
QPushButton#banner_dismiss_btn {{
    background-color: transparent;
    border: none;
    font-size: 12pt;
    font-weight: bold;
    padding: 0px 8px;
}}
QWidget#notification_banner[severity="error"] QPushButton#banner_dismiss_btn {{
    color: {BANNER_ERROR_TEXT};
}}
QWidget#notification_banner[severity="warning"] QPushButton#banner_dismiss_btn {{
    color: {BANNER_WARNING_TEXT};
}}

/* ── Labels — value readout (instrument values) ──────────────────────── */
/* Kept just above the 10pt field label and bold so values still read as the
   emphasised element, while staying compact enough to show full numbers. */
QLabel[class="value_readout"] {{
    font-size: 11pt;
    font-weight: bold;
    color: {TEXT_PRIMARY};
    background-color: transparent;
}}

/* ── Labels — secondary / unit / type ────────────────────────────────── */
QLabel[class="secondary_label"] {{
    color: {TEXT_MUTED};
    font-size: 9pt;
    background-color: transparent;
}}

/* ── Form layout labels ───────────────────────────────────────────────── */
QLabel {{
    background-color: transparent;
}}

/* ── Tool tips ────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_HAIRLINE};
    padding: 4px;
}}

/* ── Dock widgets (Monitor window dock-based layout) ──────────────────── */
QDockWidget {{
    color: {TEXT_PRIMARY};
    font-size: 9pt;
    font-weight: bold;
}}
QDockWidget::title {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER_HAIRLINE};
    border-bottom: none;
    padding: 4px 6px;
}}
QDockWidget::close-button,
QDockWidget::float-button {{
    background-color: transparent;
    border: none;
    padding: 2px;
}}
QDockWidget::close-button:hover,
QDockWidget::float-button:hover {{
    background-color: {BTN_SECONDARY_PRESSED};
    border-radius: 2px;
}}

/* ── Connection status dot (compact Other Devices rows) ───────────────── */
QLabel[class="conn_dot"] {{
    font-size: 16px;
    color: {TEXT_MUTED};
    background-color: transparent;
}}
QLabel[class="conn_dot"][status="connected"] {{
    color: {STATUS_OK};
}}
QLabel[class="conn_dot"][status="disconnected"] {{
    color: {STATUS_ERROR};
}}

/* ── Lifecycle glow dot (LifecycleToggleButton) ───────────────────────── */
QLabel[class="lifecycle_dot"] {{
    font-size: 16px;
    background-color: transparent;
}}
QLabel[class="lifecycle_dot"][status="standby"] {{
    color: {STATUS_ERROR};
}}
QLabel[class="lifecycle_dot"][status="initiated"] {{
    color: {STATUS_OK};
}}
"""
