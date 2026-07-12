# ---
# description: |
#   SweepAxisWidget: auto-generated sweep-shape editor for one Procedure's
#   declared SweepAxis. Renders a mode selector (Linear / Segments / CSV)
#   driving a QStackedWidget of the matching sub-form, plus a hysteresis
#   checkbox. This is the one piece of GUI code needed to give every
#   SweepAxis-declaring Procedure the full sweep_builder feature set (linear
#   range, piecewise segments for a fine subfield, custom CSV, hysteresis) —
#   a new Procedure never needs its own GUI code, only a sweep_axis
#   declaration in core/procedure.py.
# entry_point: Not run directly. Instantiated by ProcedureWindow.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.sweep_builder (SweepAxis)
# input: |
#   A SweepAxis instance at construction. No other coupling to a Procedure
#   class or Station.
# process: |
#   The mode QComboBox switches the QStackedWidget page. get_params() reads
#   whichever page is active (plus the always-visible hysteresis checkbox)
#   and returns a dict of {axis.key}_-prefixed values matching
#   sweep_builder.sweep_axis_param_specs(); ProcedureWindow merges this
#   directly into the collected parameter dict.
# output: |
#   get_params() -> dict[str, Any]; raises ValueError (with a user-facing
#   message) if the active mode's own inputs are missing or unparseable.
# last_updated: 2026-07-12
# ---

"""SweepAxisWidget — mode-selector sweep-shape editor for a SweepAxis."""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.sweep_builder import SweepAxis

# Row order matches the QComboBox item order; index <-> mode string.
_MODES = ["linear", "segments", "csv"]
_MODE_LABELS = ["Linear", "Segments", "CSV"]
_SEGMENT_COLUMNS = ["Start", "End", "Step"]


class SweepAxisWidget(QWidget):
    """Sweep-shape editor for one declared ``SweepAxis``.

    Args:
        axis: The Procedure's declared sweep axis.
        parent: Optional Qt parent widget.
    """

    def __init__(self, axis: SweepAxis, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._axis = axis
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        k = self._axis.key
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        mode_row = QHBoxLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.setObjectName(f"sweep_{k}_mode_combo")
        self._mode_combo.addItems(_MODE_LABELS)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        root.addLayout(mode_row)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_linear_page())
        self._stack.addWidget(self._build_segments_page())
        self._stack.addWidget(self._build_csv_page())
        root.addWidget(self._stack)

        self._hysteresis_checkbox = QCheckBox(f"Hysteresis (forward + backward {self._axis.description.lower()} loop)")
        self._hysteresis_checkbox.setObjectName(f"sweep_{k}_hysteresis_checkbox")
        root.addWidget(self._hysteresis_checkbox)

    def _build_linear_page(self) -> QWidget:
        k = self._axis.key
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(4)

        unit = self._axis.unit
        self._start_input = QLineEdit(str(self._axis.default_start))
        self._start_input.setObjectName(f"sweep_{k}_start_input")
        self._end_input = QLineEdit(str(self._axis.default_end))
        self._end_input.setObjectName(f"sweep_{k}_end_input")
        self._steps_input = QLineEdit(str(self._axis.default_steps))
        self._steps_input.setObjectName(f"sweep_{k}_steps_input")

        form.addRow(f"Start ({unit}):", self._start_input)
        form.addRow(f"End ({unit}):", self._end_input)
        form.addRow("Steps:", self._steps_input)
        return page

    def _build_segments_page(self) -> QWidget:
        k = self._axis.key
        page = QWidget()
        col = QVBoxLayout(page)
        col.setSpacing(4)

        self._segments_table = QTableWidget(0, len(_SEGMENT_COLUMNS))
        self._segments_table.setObjectName(f"sweep_{k}_segments_table")
        self._segments_table.setHorizontalHeaderLabels(
            [f"{name} ({self._axis.unit})" for name in _SEGMENT_COLUMNS]
        )
        col.addWidget(self._segments_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add segment")
        add_btn.setObjectName(f"sweep_{k}_add_segment_btn")
        add_btn.clicked.connect(self._add_segment_row)
        remove_btn = QPushButton("Remove segment")
        remove_btn.setObjectName(f"sweep_{k}_remove_segment_btn")
        remove_btn.clicked.connect(self._remove_segment_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        col.addLayout(btn_row)
        return page

    def _build_csv_page(self) -> QWidget:
        k = self._axis.key
        page = QWidget()
        row = QHBoxLayout(page)
        self._csv_input = QLineEdit()
        self._csv_input.setObjectName(f"sweep_{k}_csv_input")
        self._csv_input.setPlaceholderText("Path to single-column CSV file")
        browse_btn = QPushButton("Browse...")
        browse_btn.setObjectName(f"sweep_{k}_csv_browse_btn")
        browse_btn.clicked.connect(self._on_browse_csv)
        row.addWidget(self._csv_input)
        row.addWidget(browse_btn)
        return page

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_mode_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)

    def _add_segment_row(self) -> None:
        """Append a blank segment row, seeding Start from the previous row's End.

        Segments must be contiguous (each start == the previous end), so
        carrying the value forward saves retyping it for the common case of
        building up a piecewise sweep one sub-range at a time.
        """
        row = self._segments_table.rowCount()
        self._segments_table.insertRow(row)
        prev_end = self._segments_table.item(row - 1, 1) if row > 0 else None
        start_text = prev_end.text() if prev_end is not None else ""
        self._segments_table.setItem(row, 0, QTableWidgetItem(start_text))
        self._segments_table.setItem(row, 1, QTableWidgetItem(""))
        self._segments_table.setItem(row, 2, QTableWidgetItem(""))

    def _remove_segment_row(self) -> None:
        row = self._segments_table.currentRow()
        if row >= 0:
            self._segments_table.removeRow(row)

    def _on_browse_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select sweep CSV file", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._csv_input.setText(path)

    # ------------------------------------------------------------------
    # Public API consumed by ProcedureWindow
    # ------------------------------------------------------------------

    def param_keys(self) -> set[str]:
        """Return the set of hidden parameter names this widget owns.

        Used by ProcedureWindow to skip these in the generic flat-field
        collection loop (they're read via ``get_params()`` instead).
        """
        k = self._axis.key
        return {
            f"{k}_mode",
            f"{k}_start",
            f"{k}_end",
            f"{k}_steps",
            f"{k}_segments",
            f"{k}_csv_path",
            f"{k}_hysteresis",
        }

    def get_params(self) -> dict[str, Any]:
        """Read the current widget state into a sweep_axis parameter dict.

        Only the active mode's own inputs are validated strictly — fields on
        an inactive page are read on a best-effort basis (falling back to the
        axis defaults) so an unrelated half-filled tab never blocks a run.

        Returns:
            Dict of ``{axis.key}_``-prefixed values matching
            ``sweep_builder.sweep_axis_param_specs()``.

        Raises:
            ValueError: If the active mode's required input is missing or
                cannot be parsed.
        """
        k = self._axis.key
        mode = _MODES[self._mode_combo.currentIndex()]
        result: dict[str, Any] = {f"{k}_mode": mode}

        result[f"{k}_start"] = self._parse_float(
            self._start_input.text(), self._axis.default_start, required=mode == "linear",
            field_label=f"{self._axis.description} start",
        )
        result[f"{k}_end"] = self._parse_float(
            self._end_input.text(), self._axis.default_end, required=mode == "linear",
            field_label=f"{self._axis.description} end",
        )
        result[f"{k}_steps"] = self._parse_int(
            self._steps_input.text(), self._axis.default_steps, required=mode == "linear",
            field_label=f"{self._axis.description} steps",
        )

        result[f"{k}_segments"] = self._read_segments_table() if mode == "segments" else []

        if mode == "csv":
            path = self._csv_input.text().strip()
            if not path:
                raise ValueError("CSV sweep mode selected but no file chosen.")
            result[f"{k}_csv_path"] = path
        else:
            result[f"{k}_csv_path"] = ""

        result[f"{k}_hysteresis"] = self._hysteresis_checkbox.isChecked()
        return result

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_float(text: str, default: float, *, required: bool, field_label: str) -> float:
        try:
            return float(text)
        except ValueError:
            if required:
                raise ValueError(f"Cannot parse '{text}' as a number for '{field_label}'.") from None
            return default

    @staticmethod
    def _parse_int(text: str, default: int, *, required: bool, field_label: str) -> int:
        try:
            return int(text)
        except ValueError:
            if required:
                raise ValueError(f"Cannot parse '{text}' as an integer for '{field_label}'.") from None
            return default

    def _read_segments_table(self) -> list[dict[str, float]]:
        segments: list[dict[str, float]] = []
        for row in range(self._segments_table.rowCount()):
            cells = [self._segments_table.item(row, col) for col in range(3)]
            texts = [cell.text().strip() if cell is not None else "" for cell in cells]
            if all(text == "" for text in texts):
                continue
            try:
                start, end, step = (float(t) for t in texts)
            except ValueError:
                raise ValueError(
                    f"Segment row {row + 1}: all of Start, End, Step must be numbers."
                ) from None
            segments.append({"start": start, "end": end, "step": step})

        if not segments:
            raise ValueError("Segments sweep mode selected but no segments were added.")
        return segments
