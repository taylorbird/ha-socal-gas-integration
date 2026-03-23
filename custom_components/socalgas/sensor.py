"""Sensors for SoCal Gas integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SoCalGasCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SoCal Gas sensors."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return

    slug = (
        entry.data.get("account_name", "home").lower().replace(" ", "_")
    )

    async_add_entities([
        SoCalGasStatusSensor(coordinator, entry, slug),
        SoCalGasUsageTodaySensor(coordinator, entry, slug),
        SoCalGasCostTodaySensor(coordinator, entry, slug),
        SoCalGasUsageThisMonthSensor(coordinator, entry, slug),
        SoCalGasCostThisMonthSensor(coordinator, entry, slug),
    ])


class SoCalGasStatusSensor(SensorEntity):
    """Sensor showing the integration's current status.

    Does not inherit CoordinatorEntity so it stays available even when
    the coordinator fails — allowing it to report error details.
    """

    def __init__(
        self, coordinator: SoCalGasCoordinator, entry: ConfigEntry, slug: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__()
        self.coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{slug}_status"
        self._attr_name = "SoCal Gas Status"
        self._attr_icon = "mdi:fire"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state when coordinator updates."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        if self.coordinator.last_update_success:
            return "ok"
        if self.coordinator.last_exception:
            return "error"
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {}
        data = self.coordinator.data
        if isinstance(data, dict):
            if "last_update" in data:
                attrs["last_update"] = data["last_update"]
            if "readings_count" in data:
                attrs["readings_imported"] = data["readings_count"]
        if self.coordinator.last_exception:
            attrs["last_error"] = str(self.coordinator.last_exception)
        if self.coordinator.last_update_success_time:
            attrs["last_success"] = (
                self.coordinator.last_update_success_time.isoformat()
            )
        return attrs


class _SoCalGasStatisticSensor(CoordinatorEntity, SensorEntity):
    """Base class for sensors that read from long-term statistics."""

    def __init__(
        self,
        coordinator: SoCalGasCoordinator,
        entry: ConfigEntry,
        slug: str,
        key: str,
        name: str,
        icon: str,
        unit: str,
        device_class: SensorDeviceClass | None,
        statistic_id: str,
        period: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._slug = slug
        self._statistic_id = statistic_id
        self._period = period  # "day" or "month"
        self._attr_unique_id = f"{DOMAIN}_{slug}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2 if unit == "USD" else 1
        if device_class:
            self._attr_device_class = device_class
        self._cached_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._update_from_statistics()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._update_from_statistics())

    async def _update_from_statistics(self) -> None:
        """Query the recorder for the current period's sum."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        now_local = dt_util.now()  # HA-configured local time
        if self._period == "day":
            start_local = now_local.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:  # month
            start_local = now_local.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
        # Convert to UTC for the recorder query
        start = start_local.astimezone(timezone.utc)
        now = datetime.now(tz=timezone.utc)

        try:
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                now,
                {self._statistic_id},
                "hour",
                None,
                {"state"},
            )
        except Exception:
            _LOGGER.debug(
                "Could not query statistics for %s", self._statistic_id,
                exc_info=True,
            )
            return

        rows = result.get(self._statistic_id, [])
        if rows:
            total = sum(
                (row.get("state") or 0.0) for row in rows
            )
            self._cached_value = round(total, 2)
        else:
            self._cached_value = None

        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._cached_value


class SoCalGasUsageTodaySensor(_SoCalGasStatisticSensor):
    """Gas usage for today in ft³."""

    def __init__(self, coordinator, entry, slug):
        super().__init__(
            coordinator, entry, slug,
            key="usage_today",
            name="SoCal Gas Usage Today",
            icon="mdi:fire",
            unit="ft³",
            device_class=None,
            statistic_id=f"{DOMAIN}:gas_consumption_{slug}",
            period="day",
        )


class SoCalGasCostTodaySensor(_SoCalGasStatisticSensor):
    """Gas cost for today in USD."""

    def __init__(self, coordinator, entry, slug):
        super().__init__(
            coordinator, entry, slug,
            key="cost_today",
            name="SoCal Gas Cost Today",
            icon="mdi:currency-usd",
            unit="USD",
            device_class=SensorDeviceClass.MONETARY,
            statistic_id=f"{DOMAIN}:gas_cost_{slug}",
            period="day",
        )


class SoCalGasUsageThisMonthSensor(_SoCalGasStatisticSensor):
    """Gas usage for current month in ft³."""

    def __init__(self, coordinator, entry, slug):
        super().__init__(
            coordinator, entry, slug,
            key="usage_this_month",
            name="SoCal Gas Usage This Month",
            icon="mdi:fire",
            unit="ft³",
            device_class=None,
            statistic_id=f"{DOMAIN}:gas_consumption_{slug}",
            period="month",
        )


class SoCalGasCostThisMonthSensor(_SoCalGasStatisticSensor):
    """Gas cost for current month in USD."""

    def __init__(self, coordinator, entry, slug):
        super().__init__(
            coordinator, entry, slug,
            key="cost_this_month",
            name="SoCal Gas Cost This Month",
            icon="mdi:currency-usd",
            unit="USD",
            device_class=SensorDeviceClass.MONETARY,
            statistic_id=f"{DOMAIN}:gas_cost_{slug}",
            period="month",
        )
