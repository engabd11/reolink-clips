"""Sensors exposing Reolink Clip Cache statistics.

Values come from the in-memory index and are pushed on the index-updated
signal — nothing here touches the filesystem, because entity properties are
read from the event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_INDEX_UPDATED
from .coordinator import ReolinkClipCacheCoordinator


@dataclass(frozen=True, kw_only=True)
class ClipCacheSensorDescription(SensorEntityDescription):
    """Describes a clip cache sensor."""

    value_fn: Callable[[dict[str, Any]], float | int]


SENSORS: tuple[ClipCacheSensorDescription, ...] = (
    ClipCacheSensorDescription(
        key="cache_size",
        translation_key="cache_size",
        icon="mdi:harddisk",
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda stats: stats["total_size_mb"],
    ),
    ClipCacheSensorDescription(
        key="cached_clips",
        translation_key="cached_clips",
        icon="mdi:filmstrip-box-multiple",
        native_unit_of_measurement="clips",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda stats: stats["total_clips"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: ReolinkClipCacheCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ClipCacheSensor(coordinator, entry, description) for description in SENSORS
    )


class ClipCacheSensor(SensorEntity):
    """A statistic about the local clip cache."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    entity_description: ClipCacheSensorDescription

    def __init__(
        self,
        coordinator: ReolinkClipCacheCoordinator,
        entry: ConfigEntry,
        description: ClipCacheSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        self.entity_description = description
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Reolink Clip Cache",
            manufacturer="Reolink",
            model="Clip Cache",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to index changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_INDEX_UPDATED, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Write the new state when the index changes."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int:
        """Return the sensor value."""
        return self.entity_description.value_fn(self._coordinator.stats())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the cache breakdown."""
        stats = self._coordinator.stats()
        return {
            "cameras": stats["cameras"],
            "cache_days": stats["cache_days"],
            "max_cache_size_mb": stats["max_cache_size_mb"],
            "stream": stats["stream"],
            "event_types": stats["event_types"],
            "storage_path": stats["storage_path"],
            "last_clip": stats["last_clip"],
        }
