"""Config and options flow for Reolink Clip Cache."""

from __future__ import annotations

import logging
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
from homeassistant.util import slugify

from .const import (
    CONF_CACHE_DAYS,
    CONF_CAMERAS,
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
)

_LOGGER = logging.getLogger(__name__)

# Labels are spelled out here rather than looked up through a selector
# translation key: a missing lookup renders as an undefined label, which the
# frontend then trips over while filtering the dropdown.
EVENT_TYPE_OPTIONS = [
    {"value": "person", "label": "Person"},
    {"value": "vehicle", "label": "Vehicle"},
    {"value": "animal", "label": "Animal"},
    {"value": "package", "label": "Package"},
    {"value": "visitor", "label": "Visitor"},
    {"value": "face", "label": "Face"},
]

STREAM_OPTIONS = [
    {"value": "sub", "label": "Low resolution (smaller, caches faster)"},
    {"value": "main", "label": "High resolution (best picture, much larger)"},
]


async def async_list_cameras(hass: HomeAssistant) -> list[dict[str, str]]:
    """Return the Reolink cameras the media source offers, as selector options.

    Values match the slugs discovery assigns, so a saved selection lines up
    with what the coordinator finds.
    """
    from homeassistant.components.media_source import async_browse_media

    root = await async_browse_media(hass, REOLINK_MEDIA_PREFIX)
    return [
        {"value": slugify(child.title), "label": child.title}
        for child in root.children or []
        if child.title
    ]


def _schema(current: dict[str, Any], cameras: list[dict[str, str]]) -> vol.Schema:
    """Build the shared options schema, pre-filled from the current values."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_CAMERAS, default=current.get(CONF_CAMERAS, [])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=cameras,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                    custom_value=True,
                )
            ),
            vol.Required(
                CONF_EVENT_TYPES,
                default=current.get(CONF_EVENT_TYPES, DEFAULT_EVENT_TYPES),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=EVENT_TYPE_OPTIONS,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_STREAM, default=current.get(CONF_STREAM, DEFAULT_STREAM)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=STREAM_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_CACHE_DAYS,
                default=current.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=90,
                    step=1,
                    unit_of_measurement="days",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MAX_CACHE_MB,
                default=current.get(CONF_MAX_CACHE_MB, DEFAULT_MAX_CACHE_MB),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100,
                    max=102400,
                    step=100,
                    unit_of_measurement="MB",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SWEEP_MINUTES,
                default=current.get(CONF_SWEEP_MINUTES, DEFAULT_SWEEP_MINUTES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=60,
                    step=1,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _coerce(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise the selector output, which returns numbers as floats."""
    return {
        CONF_CAMERAS: list(user_input.get(CONF_CAMERAS) or []),
        CONF_EVENT_TYPES: list(user_input[CONF_EVENT_TYPES]),
        CONF_STREAM: user_input[CONF_STREAM],
        CONF_CACHE_DAYS: int(user_input[CONF_CACHE_DAYS]),
        CONF_MAX_CACHE_MB: int(user_input[CONF_MAX_CACHE_MB]),
        CONF_SWEEP_MINUTES: int(user_input[CONF_SWEEP_MINUTES]),
    }


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
        placeholders: dict[str, str] = {}

        try:
            cameras = await async_list_cameras(self.hass)
        except Exception as err:  # noqa: BLE001 - surface whatever went wrong
            _LOGGER.debug("Could not list Reolink cameras: %s", err)
            cameras = []
            errors["base"] = "cannot_connect"
            placeholders["error"] = str(err)

        if user_input is not None and not errors:
            if cameras:
                return self.async_create_entry(
                    title="Reolink Clip Cache",
                    data={},
                    options=_coerce(user_input),
                )
            errors["base"] = "no_cameras"

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}, cameras),
            errors=errors,
            description_placeholders=placeholders,
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

        try:
            cameras = await async_list_cameras(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not list Reolink cameras: %s", err)
            cameras = []

        current = dict(self.config_entry.options)
        # Keep a saved selection selectable even when the media source is
        # unreachable, so opening options cannot silently clear it.
        known = {option["value"] for option in cameras}
        cameras += [
            {"value": camera, "label": camera}
            for camera in current.get(CONF_CAMERAS, [])
            if camera not in known
        ]

        return self.async_show_form(
            step_id="init", data_schema=_schema(current, cameras)
        )
