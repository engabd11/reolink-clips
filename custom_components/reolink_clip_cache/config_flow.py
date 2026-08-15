"""Config flow for Reolink Clip Cache integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CACHEABLE_EVENT_TYPES,
    DEFAULT_CACHE_DAYS,
    DEFAULT_RESOLUTION,
    DOMAIN,
)


class ReolinkClipCacheConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Reolink Clip Cache."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate that Reolink integration is available
            if await self._validate_reolink():
                return self.async_create_entry(
                    title="Reolink Clip Cache",
                    data={},
                    options={
                        "cache_days": user_input.get("cache_days", DEFAULT_CACHE_DAYS),
                        "resolution": user_input.get("resolution", DEFAULT_RESOLUTION),
                    },
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("cache_days", default=DEFAULT_CACHE_DAYS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=90)
                ),
                vol.Required("resolution", default=DEFAULT_RESOLUTION): vol.In(
                    ["low", "sub", "clear"]
                ),
            }),
            errors=errors,
        )

    async def _validate_reolink(self) -> bool:
        """Check that Reolink integration is configured and media source works."""
        # Verify Reolink media source is accessible
        try:
            result = await self.hass.components.media_source.async_browse_media(
                self.hass, "media-source://reolink"
            )
            return result is not None
        except Exception:
            return False

    @staticmethod
    @config_entries.HANDLERS.register(DOMAIN)
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ReolinkClipCacheOptionsFlow:
        """Get the options flow."""
        return ReolinkClipCacheOptionsFlow(config_entry)


class ReolinkClipCacheOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Reolink Clip Cache."""

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "cache_days",
                    default=current.get("cache_days", DEFAULT_CACHE_DAYS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=90)),
                vol.Required(
                    "resolution",
                    default=current.get("resolution", DEFAULT_RESOLUTION),
                ): vol.In(["low", "sub", "clear"]),
            }),
        )