# ---
# description: |
#   RampableVI mixin for Virtual Instruments that support controlled ramping.
#   Defines the abstract Ramp API (start_ramp, advance_ramp, ramp_status,
#   stop_ramp) that the Orchestrator calls every tick while a ramp is active
#   (stop_ramp on abort/ERROR/EMERGENCY: kills the generator AND holds the
#   hardware).
# entry_point: Not run directly; mixed into magnet and temperature VIs.
# dependencies:
#   - abc
# input: |
#   Subclasses must implement all four abstract methods.
# process: |
#   start_ramp() initialises the ramp generator. advance_ramp() calls next()
#   on the generator. ramp_status() returns one of RAMPING / TARGET_REACHED / IDLE.
# output: |
#   Status strings consumed by Station.check_ramps() and Orchestrator.
# last_updated: 2026-04-06
# ---

"""RampableVI mixin — abstract ramp API for system VIs."""

from __future__ import annotations

from abc import abstractmethod


class RampableVI:
    """Mixin for any VI that requires controlled ramping.

    The Orchestrator calls ``start_ramp(target)`` once, then calls
    ``advance_ramp()`` every tick until ``ramp_status()`` returns
    ``"TARGET_REACHED"``.

    Subclasses *must* implement all four abstract methods.
    """

    @abstractmethod
    def start_ramp(self, target: float) -> None:
        """Begin ramping to *target* value.

        Called by ``Station.process_system_targets()``.
        Target is in user units (tesla for magnets, kelvin for temperature).
        Ramp rate is determined internally from YAML config stored in
        ``self._init_params``.

        Args:
            target: Desired end value in user-facing units.
        """
        ...

    @abstractmethod
    def advance_ramp(self) -> None:
        """Advance the ramp by one step.

        Called by ``Station.check_ramps()`` every Orchestrator tick while
        this VI is ramping.  Internally calls ``next()`` on the generator
        returned by ``_ramp_generator()``.
        """
        ...

    @abstractmethod
    def ramp_status(self) -> str:
        """Return the current ramp state string.

        Returns:
            ``"RAMPING"``        — VI has not yet reached its target.
            ``"TARGET_REACHED"`` — target reached and confirmed.
            ``"IDLE"``           — no ramp active.
        """
        ...

    @abstractmethod
    def stop_ramp(self) -> None:
        """Stop any active ramp and freeze the hardware where it is.

        Called by the Orchestrator on abort and on ERROR/EMERGENCY entry.
        Implementations MUST both clear the internal ramp generator and
        command the hardware to hold: for autonomous hardware (a magnet PSU
        keeps ramping to its last setpoint on its own), clearing the
        generator alone does not stop the physical ramp. After this call,
        ``ramp_status()`` must report ``"IDLE"``.
        """
        ...
