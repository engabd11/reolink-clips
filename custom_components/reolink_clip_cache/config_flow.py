"""Config and options flow for Reolink Clip Cache."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import (
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
    REOLINK_MEDIA_PREFIX,
    STREAMS,
)

EVENT_TYPE_CHOICES = ["person", "vehicle", "animal", "package", "visitor", "face"]


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the shared options schema, pre-filled from the current values."""
    return vol.Schema(
        {
            vol.Required(
                CONF_EVENT_TYPES,
                default=current.get(CONF_EVENT_TYPES, DEFAULT_EVENT_TYPES),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=EVENT_TYPE_CHOICES,
                    multiple=True,
                    translation_key="event_types",
                )
            ),
            vol.Required(
                CONF_STREAM, default=current.get(CONF_STREAM, DEFAULT_STREAM)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=STREAMS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="stream",
                )
            ),
            vol.Required(
                CONF_CACHE_DAYS,
                default=current.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=90, step=1, unit_of_measurement="days",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MAX_CACHE_MB,
                default=current.get(CONF_MAX_CACHE_MB, DEFAULT_MAX_CACHE_MB),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100, max=102400, step=100, unit_of_measurement="MB",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SWEEP_MINUTES,
                default=current.get(CONF_SWEEP_MINUTES, DEFAULT_SWEEP_MINUTES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=60, step=1, unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _coerce(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise the numeric selector output, which arrives as floats."""
    return {
        CONF_EVENT_TYPES: user_input[CONF_EVENT_TYPES],
        CONF_STREAM: user_input[CONF_STREAM],
        CONF_CACHE_DAYS: int(user_input[CONF_CACHE_DAYS]),
        CONF_MAX_CACHE_MB: int(user_input[CONF_MAX_CACHE_MB]),
        CONF_SWEEP_MINUTES: int(user_input[CONF_SWEEP_MINUTES]),
    }


async def _async_count_cameras(hass: HomeAssistant) -> int:
    """Return how many Reolink cameras the media source offers."""
    from homeassistant.components.media_source import async_browse_media

    result = await async_browse_media(hass, REOLINK_MEDIA_PREFIX)
    return len(result.children or [])


class ReolinkClipCacheConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            try:
                cameras = await _async_count_cameras(self.hass)
            except Exception as err:  # noqa: BLE001 - surface whatever went wrong
                errors["base"] = "cannot_connect"
                description_placeholders["error"] = str(err)
            else:
                if cameras:
                    return self.async_create_entry(
                        title="Reolink Clip Cache",
                        data={},
                        options=_coerce(user_input),
                    )
                errors["base"] = "no_cameras"

        return self.async_show_form(
            step_id="user",
            data_schema=_options_schema(user_input or {}),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> ReolinkClipCacheOptionsFlow:
        """Return the options flow."""
        return ReolinkClipCacheOptionsFlow()


class ReolinkClipCacheOptionsFlow(OptionsFlow):
    """Handle changes to an existing entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=_coerce(user_input))

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(self.config_entry.options)),
        )
