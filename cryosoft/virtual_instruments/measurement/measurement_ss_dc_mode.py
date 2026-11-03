"""SSDCModeMeasurementVI — spin Seebeck DC mode (Keithley 6221 + 2182A).

The source current drives a heater whose thermal gradient takes time to
settle, so this method never switches the excitation off on its own: route
changes reconfigure the armed instrument in place (``route_setter``), a
normal run end only disarms (``run_end_method``), and only ``standby()`` —
which abort, run failure and EMERGENCY always dispatch — zeroes the source.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from cryosoft.core.decorators import control
from cryosoft.virtual_instruments.base import EXCITATION_CURRENT_LIMIT
from cryosoft.virtual_instruments.measurement.measurement_dc_mode import (
    _NOT_INITIATED,
    AUTO_RANGE_V,
    DCModeMeasurementVI,
)

logger = logging.getLogger(__name__)


class SSDCModeMeasurementVI(DCModeMeasurementVI):
    """Spin Seebeck DC mode: DC mode whose heater current stays on.

    Same drivers, parameters, data columns and reading loop as
    ``DCModeMeasurementVI``; three things differ, all about keeping the
    excitation (and so the sample's thermal gradient) steady:

    * **Arming never zeroes the source.** The arming path's engine-aborting
      ``set_current()`` writes the target current instead of 0 A, so a
      re-arm — or a new run started while the heater is still on — never
      collapses the gradient.
    * **Route changes keep the current on.** ``route_setter`` is
      ``apply_route_settings``: the procedure switches the route (hot
      switching keeps the source loaded) and then applies only the values
      that differ — compliance, voltmeter range, and the current, which goes
      straight to its new value.
    * **A normal run end leaves the current on.** ``run_end_method`` is
      ``disarm``. Abort, run failure and EMERGENCY still dispatch
      ``standby()``, which zeroes the source.
    """

    display_label: str = "Spin Seebeck DC mode"
    selector_label: ClassVar[str] = "SS DC mode (6221 + 2182A)"

    route_setter: ClassVar[str] = "apply_route_settings"
    run_end_method: ClassVar[str] = "disarm"

    # Control-validation standard: the route setter commands the excitation
    # current too, so it carries the same ceiling as the arming path.
    control_limits: ClassVar[dict[str, dict[str, str]]] = {
        **DCModeMeasurementVI.control_limits,
        "apply_route_settings": {"current": EXCITATION_CURRENT_LIMIT},
    }

    def _arming_baseline_current(self) -> float:
        """Return the target current, so arming never switches the heater off.

        Returns:
            The current being armed, in Amperes.
        """
        return self._current

    @control(
        panel=False,
        params={
            name: DCModeMeasurementVI.measurement_parameters[name]
            for name in DCModeMeasurementVI.route_parameters
        },
    )
    def apply_route_settings(
        self,
        current: float | None = None,
        voltmeter_range_V: float | None = None,
        compliance_V: float | None = None,
    ) -> None:
        """Apply one route's values to the armed instruments, current left on.

        The ``route_setter`` behind the route settings. Only a value that
        differs from the armed one is written; an omitted value (``None``,
        e.g. a current the reading loop is varying) is left alone.

        Args:
            current: The route's source current in Amperes.
            voltmeter_range_V: The route's voltmeter range in Volts, or
                ``AUTO_RANGE_V`` for autorange.
            compliance_V: The route's source voltage compliance in Volts.

        Raises:
            RuntimeError: If ``initiate_measurement()`` has not been called first.
        """
        if self._armed is _NOT_INITIATED:
            raise RuntimeError(
                "initiate_measurement() must be called before apply_route_settings()."
            )
        source = self._source  # type: ignore[attr-defined]
        meter = self._meter    # type: ignore[attr-defined]
        if compliance_V is not None and float(compliance_V) != self._compliance_V:
            self._compliance_V = float(compliance_V)
            source.set_compliance(self._compliance_V)
        if voltmeter_range_V is not None and float(voltmeter_range_V) != self._voltmeter_range_V:
            self._voltmeter_range_V = float(voltmeter_range_V)
            if self._voltmeter_range_V == AUTO_RANGE_V:
                meter.set_auto_range(True)
            else:
                meter.set_range(self._voltmeter_range_V)
        if current is not None and float(current) != self._current:
            self._current = float(current)
            source.set_current(self._current)

    def disarm(self) -> None:
        """End the measurement and leave the source current flowing.

        The ``run_end_method``: a later ``take_reading()`` needs a fresh
        ``initiate_measurement()``, but the heater keeps its gradient until
        ``standby()`` (abort, failure, EMERGENCY, or the operator) turns it off.
        """
        logger.info(
            "%s: measurement disarmed; source current left at %g A",
            type(self).__name__,
            self._current,
        )
        self._armed = _NOT_INITIATED
