"""Tests for the sensor module."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# We need real-ish base classes to avoid metaclass conflicts
class FakeCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

class FakeSensorEntity:
    available = True

# Mock all homeassistant modules
for mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.components.sensor",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.dt",
    "aiohttp",
]:
    sys.modules.setdefault(mod, MagicMock())

# Patch the base classes to use our fakes
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = FakeCoordinatorEntity
sys.modules["homeassistant.components.sensor"].SensorEntity = FakeSensorEntity

from custom_components.socalgas.sensor import SoCalGasStatusSensor


class TestStatusSensor:
    """Status sensor reports coordinator state."""

    def test_native_value_ok_on_success(self):
        coordinator = MagicMock()
        coordinator.last_update_success = True
        coordinator.last_exception = None
        entry = MagicMock()

        sensor = SoCalGasStatusSensor(coordinator, entry, "home")
        assert sensor.native_value == "ok"

    def test_native_value_error_on_failure(self):
        coordinator = MagicMock()
        coordinator.last_update_success = False
        coordinator.last_exception = Exception("connection timeout")
        entry = MagicMock()

        sensor = SoCalGasStatusSensor(coordinator, entry, "home")
        assert sensor.native_value == "error"

    def test_native_value_unknown_when_no_update_yet(self):
        coordinator = MagicMock()
        coordinator.last_update_success = False
        coordinator.last_exception = None
        entry = MagicMock()

        sensor = SoCalGasStatusSensor(coordinator, entry, "home")
        assert sensor.native_value == "unknown"

    def test_extra_attributes_include_error(self):
        coordinator = MagicMock()
        coordinator.last_update_success = False
        coordinator.last_exception = Exception("auth timeout")
        coordinator.data = {"last_update": "2026-03-22T10:00:00", "readings_count": 42}
        coordinator.last_update_success_time = None
        entry = MagicMock()

        sensor = SoCalGasStatusSensor(coordinator, entry, "home")
        attrs = sensor.extra_state_attributes
        assert attrs["last_error"] == "auth timeout"
        assert attrs["last_update"] == "2026-03-22T10:00:00"
        assert attrs["readings_imported"] == 42
