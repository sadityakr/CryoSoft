"""AssistantDock — the chat dock for the **Embedded assistant**.

A dock rather than a quadrant, because the assistant is optional: a setup that
does not switch it on carries no widget at all, and one that does can move it,
float it or close it without disturbing the fixed monitor grid that the
physicist's own instruments live in.

What the widget does and does not do
------------------------------------

It renders one ``AssistantRuntime`` and holds no state of its own. Every line
it shows is a transcript record the runtime published — the same record written
to the **Assistant transcript** — so what is on screen and what is in the
evidence file cannot drift apart. It never touches the engine, never builds a
``Gateway`` and never calls a tool: the role selector asks a factory the
application handed it for a new connection, and everything else goes through
the runtime's four methods (``ask``, ``stop``, ``set_gateway``, ``role``).

Four things are always visible while a turn runs, because they are the four
questions a physicist watching an assistant act on their cryostat has:

* **What is it doing** — the status chip: idle, thinking, calling *<tool>*,
  or refused, with the refusing rule named.
* **What authority is it acting under** — the role selector, which never
  offers more than the deployment's ceiling.
* **What has it cost** — this turn and this session, from the runtime's own
  cost lines, never recomputed here.
* **How do I stop it** — the stop button, which cancels between steps.

With no runtime (no API key configured, or the assistant switched on in a
deployment that never installed the optional extra) the dock builds anyway and
says so in one line. A dock that failed to construct would take the window with
it, and a missing key is a configuration fact, not a fault.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.gui.theme import (
    ASSISTANT_REPLY_TEXT,
    ASSISTANT_TOOL_OK_TEXT,
    ASSISTANT_TOOL_REFUSED_TEXT,
    ASSISTANT_USER_TEXT,
    BTN_CLASS_DANGER,
    BTN_CLASS_PRIMARY,
)
from cryosoft.session.assistant import (
    STATUS_CALLING,
    STATUS_IDLE,
    STATUS_REFUSED,
    STATUS_THINKING,
    AssistantRuntime,
)
from cryosoft.session.gateway import ROLE_LADDER, Role, role_within_ceiling

logger = logging.getLogger(__name__)

#: What the dock says when it has no runtime to render. One line, and it names
#: both places a key may come from, because "no API key configured" without
#: saying where to put one is not an answer.
NO_CLIENT_MESSAGE = (
    "No API key configured — set assistant.api_key in the user settings file, "
    "or the CRYOSOFT_ASSISTANT_APIKEY environment variable, to use the "
    "assistant."
)

#: Chip text for each status the runtime publishes. ``STATUS_CALLING`` and
#: ``STATUS_REFUSED`` append the detail the runtime sent with them.
_CHIP_TEXT: dict[str, str] = {
    STATUS_IDLE: "idle",
    STATUS_THINKING: "thinking",
    STATUS_CALLING: "calling",
    STATUS_REFUSED: "refused",
}


def _roles_within(ceiling: str) -> list[str]:
    """Return every role a deployment with this ceiling may hand out.

    Read off ``PERMISSION_MATRIX`` cell by cell through
    ``role_within_ceiling()``, so the selector follows the one table that
    already orders the roles and no second ordering is maintained beside it.

    Args:
        ceiling: The most authority the deployment permits, as a role value. An
            unknown value is treated as the safest role — a typo in a config
            must narrow authority, never widen it.

    Returns:
        The permitted role values, weakest first.
    """
    try:
        cap = Role(ceiling)
    except ValueError:
        logger.warning(
            "Unknown assistant role ceiling %r; offering %r only.",
            ceiling,
            Role.OBSERVER.value,
        )
        cap = Role.OBSERVER
    return [role.value for role in ROLE_LADDER if role_within_ceiling(role, cap)]


class AssistantDock(QDockWidget):
    """The **Embedded assistant**'s chat dock (objectName ``assistant_dock``).

    Args:
        runtime: The ``AssistantRuntime`` to render, or ``None`` for the
            no-client state.
        max_role: The most authority the role selector may offer — the
            deployment's ``assistant_max_role``, falling back to its
            ``gateway_max_role``. Never widened here.
        role_factory: Called with a role value to build the ``Gateway`` the
            runtime should reconnect under. ``None`` (the default) leaves the
            selector showing the current role, disabled: a widget that cannot
            build a connection must not pretend it can change one.
        unavailable_reason: The one line to show when there is no runtime.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        runtime: AssistantRuntime | None = None,
        *,
        max_role: str = Role.OBSERVER.value,
        role_factory: Callable[[str], Any] | None = None,
        unavailable_reason: str = NO_CLIENT_MESSAGE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Assistant", parent)
        self.setObjectName("assistant_dock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self._runtime = runtime
        self._role_factory = role_factory
        self._max_role = max_role
        self._unavailable_reason = unavailable_reason

        body = QWidget()
        body.setObjectName("assistant_body")
        body.setMinimumWidth(320)
        root = QVBoxLayout(body)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        root.addLayout(self._build_header())
        self._transcript = QTextEdit()
        self._transcript.setObjectName("assistant_transcript")
        self._transcript.setReadOnly(True)
        self._transcript.setMinimumHeight(200)
        self._transcript.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._transcript, stretch=1)
        root.addWidget(self._build_cost_label())
        root.addLayout(self._build_input_row())

        self._unavailable = QLabel(self._unavailable_reason)
        self._unavailable.setObjectName("assistant_unavailable_label")
        self._unavailable.setWordWrap(True)
        self._unavailable.setProperty("class", "secondary_label")
        self._unavailable.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(self._unavailable)
        # Absorbs the leftover height in the no-client state, so the one line
        # sits at the top of the dock instead of floating in its middle.
        root.addStretch()

        self.setWidget(body)
        self._connect_runtime()
        self._apply_availability()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        """Build the role selector and the status chip.

        Returns:
            The header row's layout.
        """
        row = QHBoxLayout()
        row.setSpacing(6)

        self._role_label = QLabel("Role")
        row.addWidget(self._role_label)
        self._role_combo = QComboBox()
        self._role_combo.setObjectName("assistant_role_combo")
        self._role_combo.setToolTip(
            "The authority the assistant acts under. Never more than this "
            "setup's ceiling; the gateway refuses anything the role does not "
            "grant."
        )
        for role in _roles_within(self._max_role):
            self._role_combo.addItem(role)
        current = self._runtime.role if self._runtime is not None else ""
        if current and self._role_combo.findText(current) < 0:
            self._role_combo.addItem(current)
        if current:
            self._role_combo.setCurrentText(current)
        self._role_combo.setEnabled(
            self._runtime is not None and self._role_factory is not None
        )
        self._role_combo.currentTextChanged.connect(self._on_role_selected)
        row.addWidget(self._role_combo)

        row.addStretch()
        self._chip = QLabel(_CHIP_TEXT[STATUS_IDLE])
        self._chip.setObjectName("assistant_status_chip")
        self._chip.setProperty("class", "assistant_chip")
        self._chip.setProperty("status", STATUS_IDLE)
        self._chip.setToolTip("What the assistant is doing right now.")
        row.addWidget(self._chip)
        return row

    def _build_cost_label(self) -> QLabel:
        """Build the visible cost line.

        Returns:
            The label, already showing the zeroed line.
        """
        self._cost_label = QLabel("")
        self._cost_label.setObjectName("assistant_cost_label")
        self._cost_label.setProperty("class", "secondary_label")
        self._cost_label.setToolTip(
            "What the assistant has spent in model tokens, at this "
            "installation's own price table."
        )
        self._render_cost(
            self._runtime.turn_cost() if self._runtime is not None else {},
            self._runtime.session_cost() if self._runtime is not None else {},
        )
        return self._cost_label

    def _build_input_row(self) -> QHBoxLayout:
        """Build the question field, the send button and the stop button.

        Returns:
            The input row's layout.
        """
        row = QHBoxLayout()
        row.setSpacing(6)

        self._input = QLineEdit()
        self._input.setObjectName("assistant_input")
        self._input.setPlaceholderText("Ask about this experiment…")
        self._input.returnPressed.connect(self._on_send)
        row.addWidget(self._input, stretch=1)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("assistant_send_btn")
        self._send_btn.setProperty("class", BTN_CLASS_PRIMARY)
        self._send_btn.setToolTip("Put the question to the assistant.")
        self._send_btn.clicked.connect(self._on_send)
        row.addWidget(self._send_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("assistant_stop_btn")
        self._stop_btn.setProperty("class", BTN_CLASS_DANGER)
        self._stop_btn.setToolTip(
            "Cancel the turn between steps: no further tool is called and no "
            "further question is put to the model."
        )
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        row.addWidget(self._stop_btn)
        return row

    def _connect_runtime(self) -> None:
        """Subscribe to the runtime's four signals, when there is a runtime."""
        if self._runtime is None:
            return
        self._runtime.message_added.connect(self._on_message)
        self._runtime.status_changed.connect(self._on_status)
        self._runtime.cost_changed.connect(self._render_cost)
        self._runtime.turn_finished.connect(self._on_turn_finished)
        self._runtime.failed.connect(self._on_failed)

    def _apply_availability(self) -> None:
        """Show either the chat controls or the one-line unavailable state."""
        available = self._runtime is not None
        self._unavailable.setVisible(not available)
        for widget in (
            self._transcript,
            self._input,
            self._send_btn,
            self._cost_label,
            self._chip,
            self._role_label,
            self._role_combo,
        ):
            widget.setVisible(available)
        if not available:
            logger.info("Assistant dock built with no client: %s", NO_CLIENT_MESSAGE)
        self._stop_btn.setVisible(available)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def runtime(self) -> AssistantRuntime | None:
        """The runtime this dock renders, or ``None``."""
        return self._runtime

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------

    def _on_send(self) -> None:
        """Put whatever is in the field to the assistant, and clear it."""
        if self._runtime is None:
            return
        question = self._input.text().strip()
        if not question:
            return
        if self._runtime.ask(question):
            self._input.clear()
            self._set_running(True)

    def _on_stop(self) -> None:
        """Cancel the turn between steps."""
        if self._runtime is not None:
            self._runtime.stop()

    def _on_role_selected(self, role: str) -> None:
        """Reconnect the runtime under the selected role.

        A ``Gateway``'s role is fixed at construction, so changing it means
        connecting again — which only whoever owns the engine can do, hence
        the factory. A refused change (a turn is in flight) puts the selector
        back to the role actually in force rather than lying about it.

        Args:
            role: The role value the operator picked.
        """
        if self._runtime is None or self._role_factory is None:
            return
        if role == self._runtime.role:
            return
        try:
            gateway = self._role_factory(role)
        except Exception:  # noqa: BLE001 — a widget never raises into Qt
            logger.exception("Assistant: could not connect under role %r", role)
            gateway = None
        if gateway is None or not self._runtime.set_gateway(gateway):
            self._role_combo.blockSignals(True)
            self._role_combo.setCurrentText(self._runtime.role)
            self._role_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Runtime signals
    # ------------------------------------------------------------------

    def _on_message(self, record: dict) -> None:
        """Append one transcript record to the view.

        Args:
            record: The record the runtime published, in the **Assistant
                transcript**'s own shape.
        """
        self._transcript.append(_render_record(record))

    def _on_status(self, status: str, detail: str) -> None:
        """Update the status chip.

        Args:
            status: One of the runtime's four ``STATUS_*`` values.
            detail: The tool's name while calling, the refusing rule while
                refused.
        """
        text = _CHIP_TEXT.get(status, status)
        if detail:
            text = f"{text} {detail}"
        self._chip.setText(text)
        if self._chip.property("status") != status:
            self._chip.setProperty("status", status)
            # Dynamic-property QSS is only re-evaluated after an
            # unpolish/polish cycle; the chip has no children a descendant
            # selector targets, so this one widget is the whole cycle.
            self._chip.style().unpolish(self._chip)
            self._chip.style().polish(self._chip)

    def _render_cost(self, turn: dict, session: dict) -> None:
        """Show what this turn and this session have cost.

        Args:
            turn: The current turn's cost line.
            session: The whole session's cost line.
        """
        self._cost_label.setText(
            f"Cost — this turn ${float(turn.get('cost_usd', 0.0)):.4f} "
            f"({int(turn.get('input_tokens', 0)):,} in / "
            f"{int(turn.get('output_tokens', 0)):,} out) · "
            f"this session ${float(session.get('cost_usd', 0.0)):.4f}"
        )

    def _on_turn_finished(self, _text: str) -> None:
        """Re-enable the input once the turn is over.

        Args:
            _text: The final text; already rendered from its transcript record.
        """
        self._set_running(False)

    def _on_failed(self, message: str) -> None:
        """Report a model that could not be reached, in the transcript itself.

        Args:
            message: What went wrong.
        """
        self._transcript.append(
            f'<span style="color:{ASSISTANT_TOOL_REFUSED_TEXT};">'
            f"<b>error</b> {html.escape(message)}</span>"
        )
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        """Enable exactly the controls that make sense while a turn runs.

        Args:
            running: Whether a turn is in flight.
        """
        self._input.setEnabled(not running)
        self._send_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        if self._role_factory is not None:
            self._role_combo.setEnabled(not running)


def _render_record(record: dict) -> str:
    """Render one transcript record as the HTML line the view shows.

    Every colour is a theme token, and the text is escaped: a tool answer is
    written by the gateway, but a question is typed by a human and a reply
    comes from a model, and neither may inject markup into the view.

    Args:
        record: One **Assistant transcript** record.

    Returns:
        One HTML line.
    """
    kind = str(record.get("record", ""))
    if kind == "user":
        return (
            f'<span style="color:{ASSISTANT_USER_TEXT};"><b>You</b></span> '
            f"{html.escape(str(record.get('text') or ''))}"
        )
    if kind == "assistant":
        cost = record.get("cost") or {}
        suffix = ""
        if cost:
            suffix = (
                f' <span style="color:{ASSISTANT_REPLY_TEXT};">'
                f"(${float(cost.get('cost_usd', 0.0)):.4f})</span>"
            )
        return (
            f'<span style="color:{ASSISTANT_REPLY_TEXT};"><b>Assistant</b></span> '
            f"{html.escape(str(record.get('text') or ''))}{suffix}"
        )
    verdict = record.get("verdict") or {}
    code = str(verdict.get("code", ""))
    colour = ASSISTANT_TOOL_OK_TEXT if code == "OK" else ASSISTANT_TOOL_REFUSED_TEXT
    reason = str(verdict.get("reason") or "")
    tail = f" — {html.escape(reason)}" if reason else ""
    return (
        f'<span style="color:{colour};"><b>{html.escape(str(record.get("tool") or ""))}'
        f"</b> → {html.escape(code)}{tail}</span>"
    )
