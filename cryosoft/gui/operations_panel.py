# ---
# description: |
#   OperationsPanel: MonitorWindow page 1's bottom-right selector entry
#   (renamed from "Cryogenics" to "Operations", plan §12 — absorbs, does not
#   replace, the Phase 5 cryogenics status section). Built when cryogenics
#   is enabled OR the operations: config is non-empty. Structure: when
#   cryogenics is configured, a QSplitter divides the panel into a left pane
#   (cryogenics status: He/N2 levels, consumption readout + window combo,
#   level plot with fill markers — unchanged from Phase 5) and a right pane
#   (one generic OperationCard per available operation); with no cryogenics
#   config the panel is just the right pane's cards, unsplit. Cards: the
#   helium fill (iff cryogenics is configured) plus one per operations:
#   config block whose declared config_key matches a discovered
#   OperationBase subclass. A card renders its operation's
#   readiness_conditions() as a live checklist, its next_due() as a header
#   line, an operator-confirmations row while it is the active run, a ready
#   banner once a run finishes done with every condition holding, and a
#   start/finish button — all driven purely by the operation class's
#   declarations, so adding an operation to a setup never touches this file.
# entry_point: Not run directly. Built by MonitorWindow._build_bottom_right_quadrant
#   whenever cryogenics is enabled or an operations: config block exists.
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13
#   - qtawesome
#   - cryosoft.core.operation (ReadinessCondition, NextDue — for type context
#     only; OperationCard consumes instances via duck-typed calls)
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.procedure_discovery (discover_operations) — the GUI
#     importing operation classes is allowed (see gui/README.md and
#     pyproject.toml contract C8, which only forbids drivers/concrete VIs)
#   - cryosoft.procedures.operations.helium_fill (HeliumFillOperation) —
#     constructed directly (helium fill is wired by cryogenics_config, not
#     the generic operations: config_key lookup)
#   - cryosoft.session.servicing_log (HeliumRecordStore, ServicingLogStore,
#     consumption_rate_pct_per_h)
# input: |
#   The active cryogenics: config (Station.read_cryogenics_config) and
#   operations: config (Station.read_operations_config), a HeliumRecordStore
#   and ServicingLogStore (both optional — None when cryogenics is absent),
#   per-tick state snapshots via on_states_updated(), one operation_status
#   milestone line per on_operation_status() call, and the Orchestrator's
#   run_started/run_finished signals (connected directly by each
#   OperationCard, following the same precedent as InstrumentPanel's own
#   action_succeeded connection — states_updated and operation_status route
#   through the window instead, see monitor_window.py's teardown-race note:
#   both fire every tick, unlike the run-boundary-only run_started/finished).
# process: |
#   on_states_updated() updates the He/N2 readouts and (throttled, exactly
#   as Phase 5) recomputes the consumption rate and redraws the level plot,
#   caching the rate; it then assembles one context dict ({"state",
#   "now_unix", "consumption_rate_pct_per_h"}) per tick and forwards
#   (state, context) to every OperationCard, which re-evaluates its
#   readiness checklist, next-due label, and ready banner from it — no
#   per-operation code here. on_operation_status() forwards one milestone
#   line to every card; only the running one (if any) displays it (design
#   doc operation-concurrency-and-error-scoping.md §2's hard status
#   separation — this text never reaches the Procedure window). A card's
#   button opens a generic OperatorDialog, constructs a FRESH operation
#   instance via the panel-supplied factory closure, and calls
#   orchestrator.run_operation(); while that operation is the active run the
#   button becomes "Finish <name>" and calls orchestrator.finish_operation()
#   (immediately going to a disabled "Finishing <name>…" state, plan §2,
#   until run_finished arrives — which also surfaces any
#   postconditions_unmet as a warning badge on the card); a declared
#   operator_confirmations checkbox calls orchestrator.confirm_operation(key)
#   and disables itself.
# output: |
#   A QWidget hosted (scrolled) in MonitorWindow's bottom-right quadrant.
#   Side effect: submits an operation to the Orchestrator.
# last_updated: 2026-07-21
# ---

"""OperationsPanel — cryogenics status (optional) + generic operation cards."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pyqtgraph as pg
import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import Station
from cryosoft.gui.procedure_discovery import discover_operations
from cryosoft.gui.theme import (
    BTN_CLASS_DANGER,
    BTN_CLASS_PRIMARY,
    PLOT_SERIES,
    STATUS_ERROR,
    STATUS_OK,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
)
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.session.servicing_log import consumption_rate_pct_per_h

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.core.operation import OperationBase
    from cryosoft.session.servicing_log import HeliumRecordStore, ServicingLogStore

logger = logging.getLogger(__name__)

__all__ = ["OperationCard", "OperationsPanel", "OperatorDialog"]

# Consumption / plot time-window options (plan §10: 1 h / 6 h / 24 h).
_CONSUMPTION_WINDOWS: list[tuple[str, float]] = [
    ("1 h", 3600.0),
    ("6 h", 21600.0),
    ("24 h", 86400.0),
]
# "1 h" would be the natural default but the helium record is sampled
# hourly (history_sample_s, config-driven): two consecutive samples land
# almost exactly 1 h apart, so a 1 h trailing window essentially never
# contains 2 usable points and the consumption readout would sit at "--"
# indefinitely. "6 h" reliably picks up multiple hourly samples.
_DEFAULT_WINDOW_LABEL = "6 h"

# Minimum real seconds between consumption/plot recomputes, so a fast
# Orchestrator tick does not re-read the helium-record file every 3 s in
# production. States_updated still drives every call; this only throttles
# the (comparatively expensive) file read + refit inside it — not a QTimer.
# The cached rate this produces is also what every OperationCard's next_due()
# context uses — no card reads the store more often than this throttle.
_RECOMPUTE_MIN_INTERVAL_S = 5.0


def _slug(name: str) -> str:
    """Return a lowercase, underscore-joined objectName fragment for *name*.

    Args:
        name: An operation's ``name`` (e.g. ``"Helium Fill"``).

    Returns:
        e.g. ``"helium_fill"`` — used to scope every widget objectName a
        card creates so two cards never collide.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class OperatorDialog(QDialog):
    """Small dialog asking for the operator name before starting an operation.

    Generic replacement for the Phase 5 ``FillOperatorDialog``: any
    operation's card opens this with its own title/message.

    Args:
        title: Window title, e.g. ``"Helium Fill"`` / ``"Sample Change"``.
        message: One-line description of what starting the operation does
            (an operation's ``description`` class attribute).
        prefill: Initial text for the operator-name field (typically the
            active experiment's user).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        title: str,
        message: str,
        prefill: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(message))
        form = QFormLayout()
        self._name_edit = QLineEdit(prefill)
        self._name_edit.setObjectName("operator_name_input")
        form.addRow("Operator name:", self._name_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def operator_name(self) -> str:
        """Return the entered operator name, stripped."""
        return self._name_edit.text().strip()


class OperationCard(QGroupBox):
    """One operation's live readiness checklist, next-due line, and start/finish control.

    Built generically from an operation *instance* — this class contains no
    per-operation logic (plan §12's hybrid-declaration standard: the
    operation class declares WHAT to check and predict; this card only
    renders it).

    Args:
        orchestrator: The active Orchestrator (``run_operation``,
            ``finish_operation``, ``confirm_operation``, and the
            ``run_started``/``run_finished`` signals this card connects
            directly, per the established direct-connection precedent for
            run-boundary signals).
        display_instance: An operation instance constructed once by the
            panel, used only to read ``name``/``description``/
            ``ready_message``/``operator_confirmations`` and to call
            ``readiness_conditions()``/``next_due()`` — never run.
        factory: Builds a FRESH operation instance for an actual run, given
            the operator name entered in the ``OperatorDialog``. Keeps this
            class generic: the panel supplies the operation-specific
            construction (station, config, data_directory, …) as a closure.
        get_current_person: Returns the attribution prefill for the dialog.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        display_instance: OperationBase,
        factory: Callable[[str], OperationBase],
        get_current_person: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(display_instance.name, parent)
        self._orchestrator = orchestrator
        self._display_instance = display_instance
        self._factory = factory
        self._get_current_person = get_current_person or (lambda: "")
        self._slug = _slug(display_instance.name)
        self.setObjectName(f"operation_card_{self._slug}")

        self._running = False
        #: Set the instant Finish is clicked (before run_finished arrives —
        #: design doc §2's "immediate finish" card contract): the button goes
        #: to a disabled "Finishing…" state right away rather than waiting
        #: for the terminal signal, since dispatching standby()/evaluating
        #: postconditions/ending the run all happen on the very next tick
        #: but are not instantaneous from the GUI's perspective.
        self._finishing = False
        self._last_run_done = False
        self._last_all_holding = True
        #: The instance built by the factory for a run this card started,
        #: held until its run_started arrives (see _on_run_started). The
        #: checklist must be re-bound to it because state the orchestrator
        #: mutates on the RUNNING instance — an operator confirmation via
        #: confirm_operation() — is invisible to the display instance's
        #: condition closures; without the re-bind, a confirmed needle valve
        #: would stay red forever and the ready banner could never show.
        self._pending_instance: OperationBase | None = None
        self._conditions = display_instance.readiness_conditions()
        self._condition_rows: dict[str, tuple[QLabel, QLabel]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        self._next_due_label = QLabel("")
        self._next_due_label.setObjectName(f"{self._slug}_next_due_label")
        self._next_due_label.setProperty("class", "value_readout")
        self._next_due_label.setWordWrap(True)
        self._next_due_label.hide()
        outer.addWidget(self._next_due_label)

        # Live status line (design doc operation-concurrency-and-error-
        # scoping.md §2's hard status separation): shows this operation's own
        # operation_status milestones while it is the active run — never the
        # Procedure window. Elided (single line) rather than word-wrapped —
        # a status line grows unboundedly less than a checklist detail.
        self._status_label = QLabel("")
        self._status_label.setObjectName(f"{self._slug}_status_label")
        self._status_label.setProperty("class", "value_readout")
        self._status_label.hide()
        outer.addWidget(self._status_label)

        for condition in self._conditions:
            # Label and detail stack on separate lines (rather than one wide
            # row) with the detail word-wrapped: the card now also renders in
            # the ~half-width right pane of the split OperationsPanel layout,
            # where a single-line "icon + label + detail" row clips text
            # instead of wrapping (QLabel paints, but does not elide, text
            # past its allotted width).
            row = QHBoxLayout()
            icon_label = QLabel()
            icon_label.setObjectName(f"{self._slug}_condition_{condition.key}_icon")
            icon_label.setFixedWidth(18)
            row.addWidget(icon_label)
            text_label = QLabel(condition.label)
            text_label.setObjectName(f"{self._slug}_condition_{condition.key}_label")
            text_label.setWordWrap(True)
            row.addWidget(text_label, stretch=1)
            outer.addLayout(row)

            detail_label = QLabel("")
            detail_label.setObjectName(f"{self._slug}_condition_{condition.key}_detail")
            detail_label.setProperty("class", "value_readout")
            detail_label.setWordWrap(True)
            detail_row = QHBoxLayout()
            detail_row.setContentsMargins(18, 0, 0, 0)  # indent under the icon column
            detail_row.addWidget(detail_label)
            outer.addLayout(detail_row)
            self._condition_rows[condition.key] = (icon_label, detail_label)

        self._confirmations: dict[str, str] = dict(
            getattr(display_instance, "operator_confirmations", {}) or {}
        )
        self._confirm_checkboxes: dict[str, QCheckBox] = {}
        self._confirmations_row: QWidget | None = None
        if self._confirmations:
            self._confirmations_row = QWidget()
            confirm_layout = QHBoxLayout(self._confirmations_row)
            confirm_layout.setContentsMargins(0, 0, 0, 0)
            for key, label in self._confirmations.items():
                checkbox = QCheckBox(label)
                checkbox.setObjectName(f"{self._slug}_confirm_{key}_checkbox")
                checkbox.toggled.connect(
                    lambda checked, k=key, box=checkbox: self._on_confirm_toggled(k, checked, box)
                )
                confirm_layout.addWidget(checkbox)
                self._confirm_checkboxes[key] = checkbox
            confirm_layout.addStretch()
            self._confirmations_row.hide()
            outer.addWidget(self._confirmations_row)

        # Reuses DiagnosticsWindow's validated "verdict_badge" QSS class
        # (severity="ok" -> STATUS_OK text, no fill, bold) rather than
        # inventing a new colour/rule (gui-edit standard).
        self._ready_banner = QLabel("")
        self._ready_banner.setObjectName(f"{self._slug}_ready_banner")
        self._ready_banner.setProperty("class", "verdict_badge")
        self._ready_banner.setProperty("severity", "ok")
        self._ready_banner.setWordWrap(True)
        self._ready_banner.hide()
        self._ready_banner.style().unpolish(self._ready_banner)
        self._ready_banner.style().polish(self._ready_banner)
        outer.addWidget(self._ready_banner)

        # Unmet-postcondition warning (design doc operation-concurrency-and-
        # error-scoping.md §2): finish is immediate and never blocks on a
        # postcondition, so an unmet one is surfaced here instead — the
        # same validated "verdict_badge" QSS class, severity="warning".
        self._postcondition_warning = QLabel("")
        self._postcondition_warning.setObjectName(f"{self._slug}_postcondition_warning")
        self._postcondition_warning.setProperty("class", "verdict_badge")
        self._postcondition_warning.setProperty("severity", "warning")
        self._postcondition_warning.setWordWrap(True)
        self._postcondition_warning.hide()
        self._postcondition_warning.style().unpolish(self._postcondition_warning)
        self._postcondition_warning.style().polish(self._postcondition_warning)
        outer.addWidget(self._postcondition_warning)

        button_row = QHBoxLayout()
        self._action_btn = QPushButton()
        self._action_btn.setObjectName(f"{self._slug}_action_btn")
        self._action_btn.clicked.connect(self._on_action_clicked)
        button_row.addWidget(self._action_btn)
        button_row.addStretch()
        outer.addLayout(button_row)
        self._sync_button()

        # Direct connection (not routed through the window's states_updated
        # forwarding): run_started/run_finished fire only at run boundaries,
        # not on every tick, so there is no teardown-race concern — the same
        # precedent InstrumentPanel's action_succeeded connection follows.
        self._orchestrator.run_started.connect(self._on_run_started)
        self._orchestrator.run_finished.connect(self._on_run_finished)

    # ------------------------------------------------------------------
    # Live updates
    # ------------------------------------------------------------------

    def on_states_updated(self, state: dict[str, Any], context: dict[str, Any]) -> None:
        """Re-evaluate the checklist, next-due line, and ready banner.

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator,
                passed straight through to every ``ReadinessCondition``'s
                ``check``/``detail``.
            context: The panel-assembled ``next_due()`` context (``"state"``,
                ``"now_unix"``, ``"consumption_rate_pct_per_h"``).
        """
        all_holding = True
        for condition in self._conditions:
            holds = bool(condition.check(state))
            all_holding = all_holding and holds
            row = self._condition_rows.get(condition.key)
            if row is None:
                continue
            icon_label, detail_label = row
            icon_label.setPixmap(
                qta.icon(
                    "fa5s.check-circle" if holds else "fa5s.times-circle",
                    color=STATUS_OK if holds else STATUS_ERROR,
                ).pixmap(16, 16)
            )
            if condition.detail is not None:
                detail_label.setText(condition.detail(state))
        self._last_all_holding = all_holding

        next_due = self._display_instance.next_due(context)
        if next_due is None:
            self._next_due_label.hide()
        else:
            self._next_due_label.setText(next_due.text)
            self._next_due_label.show()

        self._sync_ready_banner()

    def on_operation_status(self, text: str) -> None:
        """Show one operation_status milestone line, only while this card is running.

        Forwarded from ``MonitorWindow`` (never connected directly — see the
        gui-edit skill's destruction-order rule: ``operation_status`` fires
        every tick, so it must route through the window like
        ``states_updated`` does). A non-running card ignores every call —
        only one operation runs at a time, so at most one card's label is
        ever non-empty.

        Args:
            text: The milestone line (``Orchestrator.operation_status``).
        """
        if not self._running:
            return
        metrics = self._status_label.fontMetrics()
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, 400)
        self._status_label.setText(elided)
        self._status_label.setToolTip(text)
        self._status_label.show()

    # ------------------------------------------------------------------
    # Start / finish / confirm
    # ------------------------------------------------------------------

    def _on_action_clicked(self) -> None:
        if self._running:
            self._orchestrator.finish_operation()
            # Immediate visual feedback (design doc §2): the card goes to a
            # disabled "Finishing…" state right away rather than waiting for
            # run_finished — finish is fast (the next tick or two) but not
            # instantaneous from here.
            self._finishing = True
            self._sync_button()
            return
        dialog = OperatorDialog(
            self._display_instance.name,
            self._display_instance.description,
            self._get_current_person(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        person = dialog.operator_name()
        operation = self._factory(person)
        self._pending_instance = operation
        self._orchestrator.run_operation(operation)

    def _on_run_started(self, manifest: dict[str, Any]) -> None:
        if str(manifest.get("procedure", "")) != self._display_instance.name:
            return
        self._running = True
        self._finishing = False
        self._last_run_done = False
        self._status_label.setText("")
        self._status_label.hide()
        self._postcondition_warning.hide()
        if self._pending_instance is not None:
            # Re-bind the checklist to the instance the orchestrator is
            # actually running (and mutating — confirm_operation() lands
            # there, not on the display instance). Kept bound after the run
            # finishes so a given confirmation stays green under the ready
            # banner. Condition keys are class-declared, so the row lookup
            # in on_states_updated stays valid across the swap.
            self._conditions = self._pending_instance.readiness_conditions()
            self._pending_instance = None
        self._reset_confirmations()
        self._sync_button()
        self._sync_confirmations_row()
        self._sync_ready_banner()

    def _on_run_finished(self, manifest: dict[str, Any]) -> None:
        if str(manifest.get("procedure", "")) != self._display_instance.name:
            return
        self._running = False
        self._finishing = False
        self._last_run_done = str(manifest.get("status", "")) == "done"
        self._status_label.setText("")
        self._status_label.hide()
        unmet = manifest.get("postconditions_unmet") or []
        if unmet:
            self._postcondition_warning.setText(
                f"⚠ Finished with unmet postcondition(s): {', '.join(unmet)}"
            )
            self._postcondition_warning.show()
        else:
            self._postcondition_warning.hide()
        self._sync_button()
        self._sync_confirmations_row()
        self._sync_ready_banner()

    def _on_confirm_toggled(self, key: str, checked: bool, checkbox: QCheckBox) -> None:
        if not checked:
            return
        self._orchestrator.confirm_operation(key)
        checkbox.setEnabled(False)  # confirmations are one-way

    def _reset_confirmations(self) -> None:
        """Clear every confirmation checkbox for a fresh run (plan §12)."""
        for checkbox in self._confirm_checkboxes.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.setEnabled(True)
            checkbox.blockSignals(False)

    def _sync_button(self) -> None:
        if self._finishing:
            # Immediate visual feedback (design doc §2): disabled the instant
            # Finish is clicked, until run_finished flips it back — the run
            # itself ends within a tick or two, but the button must not
            # invite a second click (or look idle) in the meantime.
            self._action_btn.setEnabled(False)
            self._action_btn.setText(f"Finishing {self._display_instance.name}…")
            self._action_btn.setProperty("class", BTN_CLASS_DANGER)
            self._action_btn.setIcon(qta.icon("fa5s.hourglass-half", color=TEXT_PRIMARY))
            self._action_btn.setToolTip(
                f"Finishing {self._display_instance.name} — parking hardware"
            )
        elif self._running:
            self._action_btn.setEnabled(True)
            self._action_btn.setText(f"Finish {self._display_instance.name}")
            self._action_btn.setProperty("class", BTN_CLASS_DANGER)
            self._action_btn.setIcon(qta.icon("fa5s.stop", color=TEXT_PRIMARY))
            self._action_btn.setToolTip(
                f"Request a graceful stop of the running {self._display_instance.name}"
            )
        else:
            self._action_btn.setEnabled(True)
            self._action_btn.setText(f"{self._display_instance.name}…")
            self._action_btn.setProperty("class", BTN_CLASS_PRIMARY)
            self._action_btn.setIcon(qta.icon("fa5s.play", color=TEXT_ON_ACCENT))
            self._action_btn.setToolTip(self._display_instance.description)
        self._action_btn.style().unpolish(self._action_btn)
        self._action_btn.style().polish(self._action_btn)

    def _sync_confirmations_row(self) -> None:
        if self._confirmations_row is None:
            return
        self._confirmations_row.setVisible(self._running and bool(self._confirmations))

    def _sync_ready_banner(self) -> None:
        """Show the ready banner iff done + all-green + not running (plan §12)."""
        show = (
            bool(self._display_instance.ready_message)
            and self._last_run_done
            and self._last_all_holding
            and not self._running
        )
        if show:
            self._ready_banner.setText(f"✓ {self._display_instance.ready_message}")
        self._ready_banner.setVisible(show)


class OperationsPanel(QWidget):
    """Live cryogenics status (optional) + one OperationCard per available operation.

    Args:
        station: The active Station (passed to every operation constructor).
        orchestrator: The active Orchestrator (forwarded to every
            ``OperationCard``).
        cryogenics_config: The resolved ``cryogenics:`` block
            (``Station.read_cryogenics_config()``'s result), or ``None``/``{}``
            when cryogenics is not configured — the status section and the
            helium-fill card are both omitted in that case.
        operations_config: The resolved ``operations:`` block
            (``Station.read_operations_config()``'s result: ``{config_key:
            {key: value}}``), or ``None``/``{}`` for none declared.
        helium_store: Where the hourly helium/nitrogen samples live, or
            ``None`` when cryogenics is absent.
        servicing_store: Where cryogenics-log fill entries live, or ``None``.
        get_data_dir: Callable returning the app's configured data directory,
            passed to the helium-fill card's factory as ``data_directory``.
        get_current_person: Callable returning the attribution prefill for
            every card's operator dialog.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        cryogenics_config: dict[str, Any] | None,
        operations_config: dict[str, dict[str, Any]] | None,
        helium_store: HeliumRecordStore | None,
        servicing_store: ServicingLogStore | None,
        get_data_dir: Callable[[], str],
        get_current_person: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("operations_panel")
        self._station = station
        self._orchestrator = orchestrator
        self._cryogenics_config = dict(cryogenics_config) if cryogenics_config else {}
        self._operations_config = dict(operations_config or {})
        self._helium_store = helium_store
        self._servicing_store = servicing_store
        self._get_data_dir = get_data_dir
        self._get_current_person = get_current_person or (lambda: "")

        self._level_vi_name: str = str(self._cryogenics_config.get("level_vi", "level_meter"))
        volume = self._cryogenics_config.get("helium_volume_l")
        self._helium_volume_l: float | None = float(volume) if volume else None
        self._history_sample_s: float = float(
            self._cryogenics_config.get("history_sample_s", 3600.0)
        )
        self._gap_threshold_s: float = 2.0 * self._history_sample_s

        self._last_recompute_mono: float | None = None
        self._last_consumption_rate: float | None = None
        self._cards: list[OperationCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(8)

        self._helium_label: QLabel | None = None
        self._nitrogen_label: QLabel | None = None
        self._window_combo: QComboBox | None = None
        self._consumption_label: QLabel | None = None
        self._plot_widget: pg.PlotWidget | None = None
        self._curve = None
        self._fill_markers = None

        if self._cryogenics_config:
            # Two sub-panels side by side: cryogenic status (left) vs.
            # operation options (right) — a splitter (not a fixed ratio) so
            # the operator can favour the level plot or the cards on a given
            # screen; minimum widths + setChildrenCollapsible(False) keep
            # either side from vanishing (gui-edit layout rules).
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setObjectName("operations_panel_splitter")
            splitter.setChildrenCollapsible(False)

            left_pane = QWidget()
            left_pane.setObjectName("cryogenics_status_pane")
            left_pane.setMinimumWidth(220)
            left_layout = QVBoxLayout(left_pane)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.setSpacing(8)
            self._build_cryogenics_status_section(left_layout)
            left_layout.addStretch()

            right_pane = QWidget()
            right_pane.setObjectName("operation_cards_pane")
            right_pane.setMinimumWidth(220)
            right_layout = QVBoxLayout(right_pane)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(8)
            right_layout.addWidget(QLabel("<b>Operations</b>"))
            self._build_operation_cards(right_layout)
            right_layout.addStretch()

            splitter.addWidget(left_pane)
            splitter.addWidget(right_pane)
            splitter.setSizes([260, 260])
            outer.addWidget(splitter)

            self._recompute()
        else:
            self._build_operation_cards(outer)
            outer.addStretch()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_cryogenics_status_section(self, outer: QVBoxLayout) -> None:
        """Build the He/N2 readouts, consumption row, and level plot (Phase 5).

        Rows stack vertically rather than packing multiple labels per line:
        this section now also renders in the ~half-width left pane of the
        split OperationsPanel layout, where a single wide row (levels side
        by side, window combo + consumption label inline) no longer fits and
        would clip instead of wrap.
        """
        outer.addWidget(QLabel("<b>Cryogenics</b>"))

        self._helium_label = QLabel("He: — %")
        self._helium_label.setObjectName("cryo_helium_level_label")
        self._helium_label.setProperty("class", "value_readout")
        outer.addWidget(self._helium_label)

        self._nitrogen_label = QLabel("N₂: — %")
        self._nitrogen_label.setObjectName("cryo_nitrogen_level_label")
        self._nitrogen_label.setProperty("class", "value_readout")
        outer.addWidget(self._nitrogen_label)

        window_row = QHBoxLayout()
        window_row.addWidget(QLabel("Window:"))
        self._window_combo = QComboBox()
        self._window_combo.setObjectName("cryo_window_combo")
        for label, _seconds in _CONSUMPTION_WINDOWS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentText(_DEFAULT_WINDOW_LABEL)
        self._window_combo.currentTextChanged.connect(self._recompute)
        window_row.addWidget(self._window_combo)
        window_row.addStretch()
        outer.addLayout(window_row)

        self._consumption_label = QLabel("Consumption: —")
        self._consumption_label.setObjectName("cryo_consumption_label")
        self._consumption_label.setWordWrap(True)
        outer.addWidget(self._consumption_label)

        self._plot_widget = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._plot_widget.setObjectName("cryo_plot")
        self._plot_widget.setMinimumHeight(140)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLabel("left", "Helium (%)")
        level_pen = pg.mkPen(PLOT_SERIES[0], width=2)
        # connect='finite' breaks the line at any inserted NaN point (see
        # _build_gapped_series) — the gap is never bridged by a straight
        # line, matching "gaps rendered as gaps, no interpolation" (plan §10).
        self._curve = self._plot_widget.plot([], [], pen=level_pen, connect="finite")
        self._fill_markers = pg.ScatterPlotItem(
            symbol="t1", size=12, brush=pg.mkBrush(PLOT_SERIES[1]), pen=None
        )
        self._plot_widget.addItem(self._fill_markers)
        outer.addWidget(self._plot_widget)

    def _build_operation_cards(self, outer: QVBoxLayout) -> None:
        """Build one OperationCard per available operation (plan §12)."""
        if self._cryogenics_config and self._station.has_vi(self._level_vi_name):
            try:
                fill_display = HeliumFillOperation(self._station, **self._cryogenics_config)
            except Exception:
                logger.exception(
                    "OperationsPanel: failed to construct HeliumFillOperation display instance"
                )
            else:
                cryogenics_config = dict(self._cryogenics_config)

                def _fill_factory(
                    person: str, cfg: dict[str, Any] = cryogenics_config
                ) -> OperationBase:
                    return HeliumFillOperation(
                        self._station,
                        person=person,
                        data_directory=self._get_data_dir(),
                        **cfg,
                    )

                card = OperationCard(
                    self._orchestrator,
                    fill_display,
                    _fill_factory,
                    get_current_person=self._get_current_person,
                    parent=self,
                )
                outer.addWidget(card)
                self._cards.append(card)

        discovered = {cls.config_key: cls for cls in discover_operations() if cls.config_key}
        for key, block in self._operations_config.items():
            cls = discovered.get(key)
            if cls is None:
                logger.warning(
                    "OperationsPanel: no discovered operation class declares "
                    "config_key=%r for the operations.%s: config block — skipping",
                    key,
                    key,
                )
                continue
            try:
                display_instance = cls(self._station, **block)
            except Exception:
                logger.exception(
                    "OperationsPanel: failed to construct %s display instance for "
                    "operations.%s:",
                    cls.__name__,
                    key,
                )
                continue

            block_copy = dict(block)

            def _factory(
                person: str, cls: type[OperationBase] = cls, cfg: dict[str, Any] = block_copy
            ) -> OperationBase:
                return cls(self._station, person=person, **cfg)

            card = OperationCard(
                self._orchestrator,
                display_instance,
                _factory,
                get_current_person=self._get_current_person,
                parent=self,
            )
            outer.addWidget(card)
            self._cards.append(card)

    # ------------------------------------------------------------------
    # Live updates
    # ------------------------------------------------------------------

    def on_states_updated(self, state: dict[str, Any]) -> None:
        """Refresh the He/N2 readouts (throttled consumption/plot) and every card.

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator.
        """
        if self._cryogenics_config:
            self._update_cryo_readouts(state)
            now_mono = time.monotonic()
            if (
                self._last_recompute_mono is None
                or (now_mono - self._last_recompute_mono) >= _RECOMPUTE_MIN_INTERVAL_S
            ):
                self._recompute()
                self._last_recompute_mono = now_mono

        context: dict[str, Any] = {
            "state": state,
            "now_unix": time.time(),
            "consumption_rate_pct_per_h": self._last_consumption_rate,
        }
        for card in self._cards:
            card.on_states_updated(state, context)

    def on_operation_status(self, text: str) -> None:
        """Forward one operation_status milestone line to every card.

        Forwarded from ``MonitorWindow`` (never connected directly — see the
        gui-edit skill's destruction-order rule: ``operation_status`` fires
        every tick while an operation runs). Only one operation runs at a
        time, so each ``OperationCard`` ignores the call unless it is the
        running one (see ``OperationCard.on_operation_status``).

        Args:
            text: The milestone line (``Orchestrator.operation_status``).
        """
        for card in self._cards:
            card.on_operation_status(text)

    def _update_cryo_readouts(self, state: dict[str, Any]) -> None:
        vi_state = state.get(self._level_vi_name)
        if not isinstance(vi_state, dict):
            return
        helium = vi_state.get("helium_level")
        nitrogen = vi_state.get("nitrogen_level")
        if isinstance(helium, (int, float)) and not isinstance(helium, bool):
            self._helium_label.setText(f"He: {helium:.1f} %")
        if isinstance(nitrogen, (int, float)) and not isinstance(nitrogen, bool):
            self._nitrogen_label.setText(f"N₂: {nitrogen:.1f} %")

    def _recompute(self) -> None:
        """Recompute the consumption rate (cached for every card's next_due) and redraw the plot."""
        samples = self._helium_store.samples() if self._helium_store is not None else []
        window_s = dict(_CONSUMPTION_WINDOWS).get(
            self._window_combo.currentText(), dict(_CONSUMPTION_WINDOWS)[_DEFAULT_WINDOW_LABEL]
        )
        now = time.time()
        fill_intervals = self._fill_intervals()
        rate = consumption_rate_pct_per_h(samples, window_s, now, fill_intervals)
        self._last_consumption_rate = rate
        if rate is None:
            self._consumption_label.setText("Consumption: —")
        else:
            text = f"Consumption: {rate:.2f} %/h"
            if self._helium_volume_l:
                text += f" ({rate * self._helium_volume_l / 100.0:.2f} L/h)"
            self._consumption_label.setText(text)

        xs, ys = _build_gapped_series(samples, self._gap_threshold_s)
        self._curve.setData(xs, ys)

        marker_x, marker_y = self._fill_marker_points()
        self._fill_markers.setData(marker_x, marker_y)

    def _fill_intervals(self) -> tuple[tuple[float, float], ...]:
        """Return ``(start_unix, end_unix)`` for every fill entry in the servicing log.

        Reads the unified ``"servicing"`` kind (docs/plans/unified-servicing-
        log-and-run-recording.md §2 — the recorder no longer writes the
        legacy ``"cryogenics"`` kind), filtering to ``entry_kind ==
        "helium_fill"``.
        """
        if self._servicing_store is None:
            return ()
        intervals: list[tuple[float, float]] = []
        for entry in self._servicing_store.entries("servicing"):
            if entry.values.get("entry_kind") != "helium_fill":
                continue
            try:
                start = datetime.fromisoformat(str(entry.values.get("start_utc"))).timestamp()
                end = datetime.fromisoformat(str(entry.values.get("end_utc"))).timestamp()
            except (TypeError, ValueError):
                continue
            intervals.append((start, end))
        return tuple(intervals)

    def _fill_marker_points(self) -> tuple[list[float], list[float]]:
        """Return marker (x, y) points at each fill's start time/level.

        See ``_fill_intervals()`` for the unified ``"servicing"`` kind /
        ``entry_kind`` filtering this mirrors.
        """
        if self._servicing_store is None:
            return [], []
        xs: list[float] = []
        ys: list[float] = []
        for entry in self._servicing_store.entries("servicing"):
            if entry.values.get("entry_kind") != "helium_fill":
                continue
            try:
                start = datetime.fromisoformat(str(entry.values.get("start_utc"))).timestamp()
                level = float(entry.values.get("helium_start_pct", 0.0))
            except (TypeError, ValueError):
                continue
            xs.append(start)
            ys.append(level)
        return xs, ys


def _build_gapped_series(
    samples: list[tuple[float, float, float]], gap_threshold_s: float
) -> tuple[list[float], list[float]]:
    """Build (x, y) arrays for the level curve, inserting NaN across gaps.

    A NaN point is inserted whenever consecutive samples are separated by
    more than ``gap_threshold_s`` — with the curve's ``connect='finite'``,
    this breaks the line rather than interpolating a false straight line
    across a period the app was closed or monitoring was off.

    Args:
        samples: ``(unix_time, helium_pct, nitrogen_pct)`` tuples, any order.
        gap_threshold_s: Gap size (seconds) above which a break is inserted.

    Returns:
        Parallel ``(x, y)`` lists, chronological order.
    """
    ordered = sorted(samples, key=lambda sample: sample[0])
    xs: list[float] = []
    ys: list[float] = []
    prev_t: float | None = None
    for t, helium, _nitrogen in ordered:
        if prev_t is not None and (t - prev_t) > gap_threshold_s:
            xs.append(prev_t + 1e-3)
            ys.append(float("nan"))
        xs.append(t)
        ys.append(helium)
        prev_t = t
    return xs, ys
