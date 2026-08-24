"""HTTP views serving cached clips and thumbnails.

Cached footage is deliberately kept outside ``/config/www``: anything under
that folder is served at ``/local/...`` with no authentication at all. These
views require auth, which the card satisfies with the short-lived signed URLs
the WebSocket API hands it.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CLIP_URL, DOMAIN, THUMB_URL

_LOGGER = logging.getLogger(__name__)


class _CachedFileView(HomeAssistantView):
    """Serve a file from the clip cache by its opaque clip id."""

    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the view."""
        self.hass = hass

    def _resolve(self, clip_id: str) -> Path | None:
        """Map a clip id to a file, or None if it is not ours."""
        raise NotImplementedError

    async def get(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        """Return the requested file."""
        # clip_id is only ever used as a dictionary key, never as a path
        # segment, so a crafted value cannot escape the cache directory.
        path = self._resolve(clip_id)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)

        if not await self.hass.async_add_executor_job(path.is_file):
            return web.Response(status=HTTPStatus.NOT_FOUND)

        # FileResponse handles Range requests, which the video element needs
        # in order to seek.
        return web.FileResponse(path)


class ClipView(_CachedFileView):
    """Serve a cached MP4."""

    url = f"{CLIP_URL}/{{clip_id}}"
    name = f"api:{DOMAIN}:clip"

    def _resolve(self, clip_id: str) -> Path | None:
        """Return the clip path for an indexed clip id."""
        for coordinator in self.hass.data.get(DOMAIN, {}).values():
            if clip_id in coordinator.index:
                return coordinator.clip_path(clip_id)
        return None


class ThumbnailView(_CachedFileView):
    """Serve a cached poster frame."""

    url = f"{THUMB_URL}/{{clip_id}}"
    name = f"api:{DOMAIN}:thumb"

    def _resolve(self, clip_id: str) -> Path | None:
        """Return the thumbnail path for an indexed clip id."""
        for coordinator in self.hass.data.get(DOMAIN, {}).values():
            record = coordinator.index.get(clip_id)
            if record and record.get("has_thumbnail"):
                return coordinator.thumb_path(clip_id)
        return None


def async_register_views(hass: HomeAssistant) -> None:
    """Register the clip and thumbnail views."""
    hass.http.register_view(ClipView(hass))
    hass.http.register_view(ThumbnailView(hass))
