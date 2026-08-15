"""Sensor platform for Reolink Clip Cache.

Provides a sensor showing cache statistics: total clips, disk usage, etc.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ReolinkClipCacheCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: ReolinkClipCacheCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        ReolinkCacheStatsSensor(coordinator, entry),
    ])


class ReolinkCacheStatsSensor(CoordinatorEntity, SensorEntity):
    """Sensor that shows Reolink Clip Cache statistics."""

    _attr_icon = "mdi:filmstrip-box"
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ReolinkClipCacheCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cache_stats"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Reolink Clip Cache",
            "manufacturer": "Reolink",
            "model": "Clip Cache",
        }
        self._coordinator = coordinator

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Clip Cache Size"

    @property
    def native_value(self) -> float:
        """Return the total cache size in MB."""
        total_size = 0
        if self._coordinator._cache_dir.exists():
            for f in self._coordinator._cache_dir.glob("*.mp4"):
                try:
                    total_size += f.stat().st_size
                except OSError:
                    pass
        return round(total_size / (1024 * 1024), 2)

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        total_clips = 0
        camera_stats = {}
        for cam, dates in self._coordinator._index.items():
            cam_clips = sum(len(v) for v in dates.values())
            camera_stats[cam] = cam_clips
            total_clips += cam_clips

        return {
            "total_clips": total_clips,
            "cameras": camera_stats,
            "cache_days": self._coordinator.options.get("cache_days", 7),
            "resolution": self._coordinator.options.get("resolution", "low"),
        }