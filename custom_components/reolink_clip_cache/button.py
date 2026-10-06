"""Buttons for Reolink Clip Cache: sweep for new clips now, or clear the cache."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_DIR_NAME
from .coordinator import ReolinkClipCacheCoordinator


async def _sweep(coordinator: ReolinkClipCacheCoordinator) -> None:
    """Look for clips across every day the cache keeps, ignoring any pause."""
    today = dt_util.now().date()
    days = [today - timedelta(days=offset) for offset in range(max(1, coordinator.cache_days))]
    await coordinator.async_sweep(days=days, force=True)


async def _clear(coordinator: ReolinkClipCacheCoordinator) -> None:
    """Delete every cached clip."""
    await coordinator.async_clear()


@dataclass(frozen=True, kw_only=True)
class ClipCacheButtonDescription(ButtonEntityDescription):
    """Describes a clip cache button."""

    press_fn: Callable[[ReolinkClipCacheCoordinator], Awaitable[None]]
    # Long jobs run in the background so the press returns at once.
    background: bool = False


BUTTONS: tuple[ClipCacheButtonDescription, ...] = (
    ClipCacheButtonDescription(
        key="sweep_now",
        translation_key="sweep_now",
        icon="mdi:cloud-download-outline",
        press_fn=_sweep,
        background=True,
    ),
    ClipCacheButtonDescription(
        key="clear_cache",
        translation_key="clear_cache",
        icon="mdi:delete-sweep-outline",
        entity_category=EntityCategory.CONFIG,
        press_fn=_clear,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator: ReolinkClipCacheCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ClipCacheButton(coordinator, entry, description) for description in BUTTONS
    )


class ClipCacheButton(ButtonEntity):
    """A one-tap action on the clip cache."""

    _attr_has_entity_name = True

    entity_description: ClipCacheButtonDescription

    def __init__(
        self,
        coordinator: ReolinkClipCacheCoordinator,
        entry: ConfigEntry,
        description: ClipCacheButtonDescription,
    ) -> None:
        """Initialise the button."""
        self.entity_description = description
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Reolink Clip Cache",
            manufacturer="Reolink",
            model="Clip Cache",
        )

    async def async_press(self) -> None:
        """Run the button's action."""
        job = self.entity_description.press_fn(self._coordinator)
        if self.entity_description.background:
            self._entry.async_create_background_task(
                self.hass, job, f"{STORAGE_DIR_NAME}_{self.entity_description.key}"
            )
            return
        await job
