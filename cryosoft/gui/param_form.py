# ---
# description: |
#   param_form: the single place that maps procedure ParamSpec declarations to
#   Qt input widgets for the ProcedureWindow parameter form. Given a ParamGroup
#   (or a raw name->ParamSpec mapping) it builds a QGroupBox / QFormLayout of
#   labelled, tooltipped input rows, and provides the inverse read helpers
#   (collect the typed value, and the raw display-string round-trip used by the
#   session cache). ProcedureWindow owns layout and the SweepAxisWidget; this
#   module owns only the ParamSpec -> widget semantics.
# entry_point: Not run directly. Imported by cryosoft.gui.procedure_window.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.plan (ParamSpec, ParamGroup)
# input: |
#   ParamSpec / ParamGroup value objects and, for the read helpers, the widgets
#   this module previously built. No coupling to a Procedure class or Station.
# process: |
#   build_param_widget picks the widget from the ParamSpec: a non-empty
#   ``choices`` dict -> QComboBox (labels shown, mapped value collected), a
#   ``bool`` type -> QCheckBox, anything else -> a QLineEdit seeded with the
#   default. build_group_box / build_form_layout lay those out as QFormLayout
#   rows keyed by the canonical parameter name (+ unit) with the description /
#   default / range in a hover tooltip. collect_value reverses the mapping;
#   get_widget_raw / set_widget_raw give a uniform string form for caching.
# output: |
#   Qt widgets (QGroupBox / QFormLayout / individual inputs) plus a
#   ``{param_name: QWidget}`` registry the caller reads back from. collect_value
#   raises ValueError / TypeError if a text field cannot be parsed as its type.
# last_updated: 2026-07-13
# ---

"""param_form — the one ParamSpec -> Qt-widget mapping for the procedure form.

This is the ONLY place that names Qt widget classes for procedure parameters.
Layer L4 (procedures) declares parameters as ``ParamSpec`` value objects and
never mentions a widget; the GUI's ProcedureWindow owns the surrounding layout
and the ``SweepAxisWidget``; and this module bridges the two. Adding a new input
kind (a new ParamSpec shape) means adding a branch here and nowhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QWidget,
)

from cryosoft.core.plan import ParamGroup, ParamSpec

__all__ = [
    "build_param_widget",
    "build_param_tooltip",
    "build_form_layout",
    "build_group_box",
    "collect_value",
    "get_widget_raw",
    "set_widget_raw",
]


def build_param_widget(param_name: str, spec: ParamSpec) -> QWidget:
    """Create the input widget for one parameter, chosen by its ``ParamSpec``.

    * ``spec.choices`` (label -> value dict) -> ``QComboBox`` of the labels,
      preselected to the label whose value equals ``spec.default``.
    * ``spec.type is bool`` -> ``QCheckBox``, checked to ``spec.default``.
    * otherwise -> ``QLineEdit`` seeded with ``str(spec.default)``.

    Args:
        param_name: The parameter name (used only for error/debug context).
        spec: The parameter's ``ParamSpec`` declaration.

    Returns:
        The constructed widget (not yet registered or laid out).
    """
    if spec.choices:
        combo = QComboBox()
        for label in spec.choices:
            combo.addItem(str(label))
        for label, value in spec.choices.items():
            if value == spec.default:
                combo.setCurrentText(str(label))
                break
        return combo
    if spec.type is bool:
        check = QCheckBox()
        check.setChecked(bool(spec.default))
        return check
    return QLineEdit(str(spec.default))


def build_param_tooltip(spec: ParamSpec) -> str:
    """Build the hover-tooltip text for one ``ParamSpec``.

    Assembles, in order and skipping any part whose source data is absent:
    the ``description`` sentence (a period is appended if missing), then
    ``Default: {default} {unit}.``, then a ``Range: ...`` line built from
    ``min``/``max`` (one-sided phrasing — ``Min: ...`` / ``Max: ...`` — when
    only one bound is declared).

    Args:
        spec: A single parameter's ``ParamSpec`` (``type``, ``default``, and
            optionally ``unit``, ``min``, ``max``, ``description``).

    Returns:
        A single plain-text string, parts joined with a space.
    """
    unit = spec.unit
    parts: list[str] = []

    description = spec.description
    if description:
        parts.append(description if description.endswith(".") else f"{description}.")

    unit_suffix = f" {unit}" if unit else ""
    parts.append(f"Default: {spec.default}{unit_suffix}.")

    has_min = spec.min is not None
    has_max = spec.max is not None
    if has_min and has_max:
        parts.append(f"Range: {spec.min} to {spec.max}{unit_suffix}.")
    elif has_min:
        parts.append(f"Min: {spec.min}{unit_suffix}.")
    elif has_max:
        parts.append(f"Max: {spec.max}{unit_suffix}.")

    return " ".join(parts)


def build_form_layout(
    params: Mapping[str, ParamSpec],
    label_overrides: Mapping[str, str] | None = None,
    wrap: bool = False,
) -> tuple[QFormLayout, dict[str, QWidget]]:
    """Build a ``QFormLayout`` of input rows for a name -> ``ParamSpec`` mapping.

    Each row's widget is chosen per parameter by ``build_param_widget``: a
    drop-down for ``choices``, a checkbox for ``bool``, else a text field.

    The label IS the canonical parameter name — the same key used in the
    procedure code and stored under ``/metadata/procedure_params`` in the HDF5
    output — plus its unit: ``f"{param_name} ({unit}):"`` when the spec declares
    a non-empty ``unit``, else ``f"{param_name}:"``. The prose ``description``
    (plus default and min/max range) moves into a tooltip set on both the input
    field and its form label. Each field's ``objectName`` is
    ``f"param_{param_name}_input"``.

    ``label_overrides`` lets the caller show a *prettier* visible label than the
    canonical parameter name WITHOUT changing the name the value is collected
    under (or its HDF5 metadata key). The ProcedureWindow uses this for the
    scanner/mux column, where the parameter names are ``mux_<route>`` (kept
    prefixed so route names cannot collide with measurement/system params) but
    the visible row label is the bare ``<route>``. Only the label changes; the
    ``objectName``, the collected key, and the metadata key stay ``mux_<route>``.

    Args:
        params: A parameter-group mapping (name -> ``ParamSpec``).
        label_overrides: Optional ``{param_name: visible_label}`` map; a param
            present here uses ``visible_label`` (plus its unit) instead of its
            canonical name for the row label. The unit suffix is still appended.
        wrap: When True, sets ``WrapLongRows`` so a row too wide for its column
            drops its field beneath the label (lowering the column's minimum
            width). Default False keeps every row inline.

    Returns:
        ``(form, widgets)``: the populated ``QFormLayout`` (not yet attached to a
        parent widget) and a ``{param_name: QWidget}`` registry of its inputs.
    """
    overrides = label_overrides or {}
    form = QFormLayout()
    form.setSpacing(4)
    if wrap:
        # WrapLongRows drops a row's field beneath its label only when the row
        # is too narrow to fit both side by side. It leaves short rows inline
        # but lowers the layout's minimum width to ~max(label, field) instead of
        # label+field — letting a capped column compress without a horizontal
        # scrollbar, and letting a wrapped field use the column's full width
        # (so long values are no longer clipped). Used for the Measurement /
        # scanner columns, which the ProcedureWindow must fit four-across.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    widgets: dict[str, QWidget] = {}
    for param_name, spec in params.items():
        unit = spec.unit
        display_name = overrides.get(param_name, param_name)
        label_text = f"{display_name} ({unit}):" if unit else f"{display_name}:"
        field = build_param_widget(param_name, spec)
        field.setObjectName(f"param_{param_name}_input")
        tooltip = build_param_tooltip(spec)
        field.setToolTip(tooltip)
        widgets[param_name] = field
        form.addRow(label_text, field)
        row_label = form.labelForField(field)
        if row_label is not None:
            row_label.setToolTip(tooltip)
    return form, widgets


def build_group_box(
    group: ParamGroup, wrap: bool = False
) -> tuple[QGroupBox, dict[str, QWidget]]:
    """Build one titled ``QGroupBox`` panel for a ``ParamGroup``.

    The box title is ``group.title``; its layout is the ``QFormLayout`` produced
    by ``build_form_layout(group.params)`` — the same rows the flat form renders.

    Args:
        group: The ``ParamGroup`` to render.
        wrap: Forwarded to ``build_form_layout`` (WrapLongRows when True).

    Returns:
        ``(box, widgets)``: the ``QGroupBox`` and its ``{param_name: QWidget}``
        input registry (so the caller can read the fields back on collect).
    """
    box = QGroupBox(group.title)
    form, widgets = build_form_layout(group.params, wrap=wrap)
    box.setLayout(form)
    return box, widgets


def collect_value(widget: QWidget, spec: ParamSpec) -> Any:
    """Read one input widget's current value, typed per its ``ParamSpec``.

    Inverse of ``build_param_widget``: a combobox returns the *mapped* value for
    its selected label, a checkbox returns a ``bool``, and a text field returns
    its stripped text coerced by ``spec.type``.

    Args:
        widget: A widget created by ``build_param_widget``.
        spec: The parameter's ``ParamSpec``.

    Returns:
        The collected value (mapped choice value, bool, or ``spec.type(text)``).

    Raises:
        ValueError: If a text field's contents cannot be parsed as ``spec.type``.
        TypeError: If ``spec.type`` rejects the text (e.g. wrong argument type).
    """
    if spec.choices:
        # A combobox can only hold labels this module added, so the lookup is safe.
        return spec.choices[widget.currentText()]
    if spec.type is bool:
        return widget.isChecked()
    raw = widget.text().strip()
    return spec.type(raw)


def get_widget_raw(widget: QWidget) -> str:
    """Read a parameter widget's display value as a string (for caching).

    Uniform string form so session persistence never has to branch on the
    concrete widget type: combobox -> current label, checkbox -> ``"True"`` /
    ``"False"``, line-edit -> its text.

    Args:
        widget: A widget created by ``build_param_widget``.

    Returns:
        The widget's current value as a string.
    """
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QCheckBox):
        return str(widget.isChecked())
    return widget.text() if isinstance(widget, QLineEdit) else ""


def set_widget_raw(widget: QWidget, raw: str) -> None:
    """Restore a parameter widget from a cached display string.

    Inverse of ``get_widget_raw``; a value that no longer matches any combobox
    item (or an unparseable checkbox string) is ignored so a stale cache can
    never crash form restoration.

    Args:
        widget: A widget created by ``build_param_widget``.
        raw: The cached string previously returned by ``get_widget_raw``.
    """
    if isinstance(widget, QComboBox):
        if raw in (widget.itemText(i) for i in range(widget.count())):
            widget.setCurrentText(raw)
    elif isinstance(widget, QCheckBox):
        widget.setChecked(raw == "True")
    elif isinstance(widget, QLineEdit):
        widget.setText(raw)
