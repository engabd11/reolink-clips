"""Reolink Clip Cache integration for Home Assistant.

Pre-caches event clips (Person, Vehicle, Animal, ...) from your Reolink NVR so
they play instantly in dashboard cards, instead of waiting 20+ seconds while
the NVR seeks and remuxes each clip on demand.

Clips are cached by sweeping the Reolink media source for recordings the index
has not seen yet, because the NVR only finalises a recording once the event
has ended.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CARD_FILENAME,
    CARD_URL,
    CONF_CACHE_DAYS,
    CONF_EVENT_TYPES,
    CONF_MAX_CACHE_MB,
    CONF_STREAM,
    CONF_SWEEP_MINUTES,
    DEFAULT_CACHE_DAYS,
    DEFAULT_EVENT_TYPES,
    DEFAULT_MAX_CACHE_MB,
    DEFAULT_STREAM,
    DEFAULT_SWEEP_MINUTES,
    DOMAIN,
    PLATFORMS,
    SERVICE_PURGE_CACHE,
    SERVICE_REFRESH_CACHE,
    SERVICE_SWEEP_NOW,
    VERSION,
)
from .coordinator import ReolinkClipCacheCoordinator
from .http import async_register_views
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DATA_REGISTERED = f"{DOMAIN}_registered"

SWEEP_NOW_SCHEMA = vol.Schema(
    {
        vol.Optional("camera"): cv.string,
        vol.Optional("days", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (YAML configuration is not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Reolink Clip Cache from a config entry."""
    coordinator = ReolinkClipCacheCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await coordinator.async_setup()

    # HTTP views, WebSocket commands and the card are process-wide, so they are
    # registered once rather than per entry.
    if not hass.data.get(DATA_REGISTERED):
        hass.data[DATA_REGISTERED] = True
        async_register_views(hass)
        async_register_commands(hass)
        await _async_register_card(hass)

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a 1.x entry to the 2.0 options."""
    if entry.version > 2:
        return False

    if entry.version == 1:
        old = dict(entry.options)
        # 1.x matched folder titles ("low"/"clear"); 2.0 addresses the media
        # source stream directly.
        stream = "main" if old.get("resolution") in ("clear", "main") else DEFAULT_STREAM
        hass.config_entries.async_update_entry(
            entry,
            version=2,
            options={
                CONF_EVENT_TYPES: DEFAULT_EVENT_TYPES,
                CONF_STREAM: stream,
                CONF_CACHE_DAYS: int(old.get("cache_days", DEFAULT_CACHE_DAYS)),
                CONF_MAX_CACHE_MB: DEFAULT_MAX_CACHE_MB,
                CONF_SWEEP_MINUTES: DEFAULT_SWEEP_MINUTES,
            },
        )
        _LOGGER.info(
            "Migrated Reolink Clip Cache to version 2. Clips now live under "
            "<config>/%s and are served over authenticated URLs; anything left "
            "in <config>/www/reolink_cache can be deleted",
            DOMAIN,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_unload()

        if not hass.data.get(DOMAIN):
            for service in (
                SERVICE_PURGE_CACHE,
                SERVICE_REFRESH_CACHE,
                SERVICE_SWEEP_NOW,
            ):
                hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the dashboard card and add it to the frontend automatically.

    Registering it here means the card ships and versions with the
    integration, so there is no separate dashboard resource to add.
    """
    card_path = Path(__file__).parent / "frontend" / CARD_FILENAME
    if not await hass.async_add_executor_job(card_path.is_file):
        _LOGGER.warning("Card file is missing at %s", card_path)
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(card_path), True)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    _LOGGER.debug("Registered dashboard card at %s", CARD_URL)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services."""

    def _coordinators() -> list[ReolinkClipCacheCoordinator]:
        return list(hass.data.get(DOMAIN, {}).values())

    async def purge_cache(_call: ServiceCall) -> None:
        """Apply the retention limits now."""
        for coordinator in _coordinators():
            await coordinator.async_purge()

    async def refresh_cache(_call: ServiceCall) -> None:
        """Re-discover cameras and reconcile the index with the disk."""
        for coordinator in _coordinators():
            await coordinator.async_discover()
            await coordinator.async_reconcile()

    async def sweep_now(call: ServiceCall) -> None:
        """Look for new recordings right away."""
        today = dt_util.now().date()
        days = [today - timedelta(days=offset) for offset in range(call.data["days"])]
        for coordinator in _coordinators():
            cached = await coordinator.async_sweep(
                camera_key=call.data.get("camera"), days=days
            )
            _LOGGER.info("Manual sweep cached %d new clip(s)", cached)

    hass.services.async_register(DOMAIN, SERVICE_PURGE_CACHE, purge_cache)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_CACHE, refresh_cache)
    hass.services.async_register(
        DOMAIN, SERVICE_SWEEP_NOW, sweep_now, schema=SWEEP_NOW_SCHEMA
    )
