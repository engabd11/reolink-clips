"""Reolink Clip Cache integration for Home Assistant.

Pre-caches event clips from your Reolink NVR so they load instantly
in dashboard cards, instead of waiting 20+ seconds for the NVR API
to seek and remux each clip on demand.

Event types cached: Person, Vehicle, Animal (configurable per camera).
Motion events are NOT cached (they're too frequent and less useful).
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import ReolinkClipCacheCoordinator

__all__ = ["DOMAIN", "PLATFORMS"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Reolink Clip Cache integration (YAML config not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Reolink Clip Cache from a config entry."""
    coordinator = ReolinkClipCacheCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await coordinator.async_setup()

    # Register services
    async def purge_cache(service: ServiceCall) -> None:
        """Manually purge old clips."""
        await coordinator.purge_cache()

    async def refresh_cache(service: ServiceCall) -> None:
        """Force refresh cache for all cameras."""
        await coordinator.refresh_all()

    hass.services.async_register(DOMAIN, "purge_cache", purge_cache)
    hass.services.async_register(DOMAIN, "refresh_cache", refresh_cache)

    # Forward platforms (sensor for cache stats)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register options flow listener
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: ReolinkClipCacheCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_unload()

    # Forward unload to platforms
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.services.async_remove(DOMAIN, "purge_cache")
        hass.services.async_remove(DOMAIN, "refresh_cache")

    return unload_ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)