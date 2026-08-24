"""WebSocket API the dashboard card talks to.

One ``clips`` call returns a whole camera-day with signed local URLs already
attached, so the card never has to walk the media source itself and cached
clips need no extra round trip before playback starts.
"""

from __future__ import annotations

import logging
from datetime import date as dt_date
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    WS_CAMERAS,
    WS_CLIPS,
    WS_DATES,
    WS_RESOLVE,
    WS_STATUS,
)
from .coordinator import ReolinkClipCacheCoordinator

_LOGGER = logging.getLogger(__name__)


@callback
def _coordinator(hass: HomeAssistant) -> ReolinkClipCacheCoordinator | None:
    """Return the active coordinator, if the integration is set up."""
    return next(iter(hass.data.get(DOMAIN, {}).values()), None)


def _parse_date(value: str | None) -> dt_date:
    """Parse an ISO date, defaulting to today in HA's timezone."""
    if value:
        try:
            return dt_date.fromisoformat(value)
        except ValueError:
            _LOGGER.debug("Ignoring unparseable date %r", value)
    return dt_util.now().date()


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register every WebSocket command once."""
    websocket_api.async_register_command(hass, ws_cameras)
    websocket_api.async_register_command(hass, ws_dates)
    websocket_api.async_register_command(hass, ws_clips)
    websocket_api.async_register_command(hass, ws_resolve)
    websocket_api.async_register_command(hass, ws_status)


@websocket_api.websocket_command({vol.Required("type"): WS_CAMERAS})
@websocket_api.async_response
async def ws_cameras(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the discovered cameras and their detection sensors."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not set up")
        return
    connection.send_result(msg["id"], {"cameras": coordinator.camera_list()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_DATES,
        vol.Required("camera"): str,
    }
)
@websocket_api.async_response
async def ws_dates(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the days that have recordings for a camera."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not set up")
        return
    dates = await coordinator.async_dates(msg["camera"])
    connection.send_result(msg["id"], {"dates": dates})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_CLIPS,
        vol.Required("camera"): str,
        vol.Optional("date"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_clips(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return every clip for a camera-day, cached ones with local URLs."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not set up")
        return

    day = _parse_date(msg.get("date"))
    clips = await coordinator.async_clips(
        msg["camera"], day, connection.refresh_token_id
    )
    connection.send_result(
        msg["id"],
        {
            "clips": clips,
            "total": len(clips),
            "cached": sum(1 for clip in clips if clip["cached"]),
            "date": day.isoformat(),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_RESOLVE,
        vol.Optional("clip_id"): vol.Any(str, None),
        vol.Optional("media_content_id"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_resolve(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Resolve one clip for playback, preferring the local cache."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not set up")
        return

    result = await coordinator.async_resolve(
        clip_id=msg.get("clip_id"),
        media_content_id=msg.get("media_content_id"),
        refresh_token_id=connection.refresh_token_id,
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): WS_STATUS})
@websocket_api.async_response
async def ws_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return cache statistics."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not set up")
        return
    connection.send_result(msg["id"], coordinator.stats())
