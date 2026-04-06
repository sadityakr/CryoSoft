# ---
# description: |
#   Tests for the CryoSoft foundation layer: exceptions and decorators.
#   All tests must pass before any other layer can be built.
# entry_point: pytest tests/test_foundation.py -v
# last_updated: 2026-04-06
# ---

"""Tests for exceptions.py and decorators.py."""

import pytest
from cryosoft.core.exceptions import (
    CryoSoftError,
    CryoSoftCommunicationError,
    CryoSoftSafetyError,
    CryoSoftConfigError,
)
from cryosoft.core.decorators import (
    monitored,
    control,
    get_monitored_methods,
    get_control_methods,
)


# ── Exception tests ──────────────────────────────────────────────────

class TestExceptionHierarchy:
    """Verify the exception inheritance tree."""

    def test_base_exception_is_exception(self):
        assert issubclass(CryoSoftError, Exception)

    def test_communication_error_is_cryosoft_error(self):
        assert issubclass(CryoSoftCommunicationError, CryoSoftError)

    def test_safety_error_is_cryosoft_error(self):
        assert issubclass(CryoSoftSafetyError, CryoSoftError)

    def test_config_error_is_cryosoft_error(self):
        assert issubclass(CryoSoftConfigError, CryoSoftError)

    def test_communication_error_attributes(self):
        original = ValueError("VISA timeout")
        err = CryoSoftCommunicationError(
            "Lost connection to magnet_x",
            vi_name="magnet_x",
            original_error=original,
        )
        assert err.vi_name == "magnet_x"
        assert err.original_error is original
        assert "Lost connection" in str(err)

    def test_communication_error_defaults(self):
        err = CryoSoftCommunicationError("timeout")
        assert err.vi_name == ""
        assert err.original_error is None

    def test_catch_all_cryosoft_errors(self):
        """Verify that catching CryoSoftError catches all subtypes."""
        with pytest.raises(CryoSoftError):
            raise CryoSoftCommunicationError("test")
        with pytest.raises(CryoSoftError):
            raise CryoSoftSafetyError("test")
        with pytest.raises(CryoSoftError):
            raise CryoSoftConfigError("test")


# ── Decorator tests ──────────────────────────────────────────────────

class TestMonitoredDecorator:
    """Verify @monitored marks methods correctly."""

    def test_marks_method(self):
        @monitored
        def temperature(self) -> float:
            return 4.2
        assert temperature._is_monitored is True

    def test_preserves_function_name(self):
        @monitored
        def temperature(self) -> float:
            return 4.2
        assert temperature.__name__ == "temperature"

    def test_callable(self):
        """Decorated method should still work."""
        class FakeVI:
            @monitored
            def temperature(self) -> float:
                return 4.2

        vi = FakeVI()
        assert vi.temperature() == 4.2


class TestControlDecorator:
    """Verify @control marks methods and extracts parameters."""

    def test_marks_method(self):
        @control
        def set_field(self, target_T: float):
            pass
        assert set_field._is_control is True

    def test_extracts_params(self):
        @control
        def set_field(self, target_T: float, rate: float = 0.5):
            pass
        params = set_field._control_params
        assert "target_T" in params
        assert params["target_T"]["type"] == float
        assert "rate" in params
        assert params["rate"]["default"] == 0.5

    def test_no_self_in_params(self):
        @control
        def set_field(self, target_T: float):
            pass
        assert "self" not in set_field._control_params

    def test_callable(self):
        class FakeVI:
            @control
            def set_field(self, target_T: float):
                self.field = target_T

        vi = FakeVI()
        vi.set_field(5.0)
        assert vi.field == 5.0


class TestDiscoveryFunctions:
    """Verify get_monitored_methods and get_control_methods."""

    def _make_vi_class(self):
        class TestVI:
            @monitored
            def temperature(self) -> float:
                return 4.2

            @monitored
            def heater_output(self) -> float:
                return 30.0

            @control
            def set_temperature(self, target_K: float):
                pass

            def _private_method(self):
                pass

            def plain_method(self):
                pass

        return TestVI

    def test_get_monitored_methods(self):
        cls = self._make_vi_class()
        methods = get_monitored_methods(cls)
        assert "temperature" in methods
        assert "heater_output" in methods
        assert "set_temperature" not in methods
        assert "_private_method" not in methods
        assert "plain_method" not in methods

    def test_get_control_methods(self):
        cls = self._make_vi_class()
        methods = get_control_methods(cls)
        assert "set_temperature" in methods
        assert "target_K" in methods["set_temperature"]
        assert "temperature" not in methods

    def test_works_on_instances(self):
        cls = self._make_vi_class()
        instance = cls()
        assert "temperature" in get_monitored_methods(instance)
        assert "set_temperature" in get_control_methods(instance)
