#!/usr/bin/env python3
"""Check that an uncached clip resolves to a URL the browser can actually play.

The Reolink proxy view (/api/reolink/video/...) requires authentication, and a
<video> element cannot send a bearer token, so the URL handed to the card must
be signed for the viewer. Resolving through Python does not sign it the way the
media_source/resolve_media websocket command does, which is why uncached clips
used to fail with 401.

    pip install homeassistant && python tests/test_resolve_signing.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def make_coordinator(signed: list) -> mod.ReolinkClipCacheCoordinator:
    """A coordinator with just enough state for async_resolve."""
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace()
    coord._index = {}
    coord._vod_types = {}
    coord._cameras = {}
    started = []

    def create_task(_hass, coro, _name):
        started.append(_name)
        coro.close()  # the on-demand cache is not under test here

    coord.entry = types.SimpleNamespace(async_create_background_task=create_task)
    coord._started = started
    return coord


async def main() -> None:
    signed: list[tuple] = []

    def fake_sign(_hass, path, expiration, refresh_token_id=None, **_kw):
        signed.append((path, expiration, refresh_token_id))
        return f"{path}?authSig=signed"

    async def fake_resolve(_hass, media_content_id, _target):
        if "remote" in media_content_id:
            return types.SimpleNamespace(url="https://nvr.example/clip.mp4", mime_type="video/mp4")
        return types.SimpleNamespace(url="/api/reolink/video/entry/0/sub/Download/abc", mime_type="video/mp4")

    mod.async_sign_path = fake_sign
    mod.async_resolve_media = fake_resolve

    coord = make_coordinator(signed)
    mcid = "media-source://reolink/FILE|entry|0|sub|abc|20261006120000|20261006120030"
    result = await coord.async_resolve(media_content_id=mcid, refresh_token_id="token-123")

    check("uncached clip is reported as not cached", result.get("cached") is False, str(result))
    check("uncached URL is signed", str(result.get("url", "")).endswith("?authSig=signed"), str(result.get("url")))
    check("signed for the viewer's refresh token", signed and signed[-1][2] == "token-123", str(signed))
    check("signature lives as long as the clip list URLs", signed and signed[-1][1].total_seconds() == mod.SIGNED_URL_TTL, str(signed))
    check("on-demand caching is started in the background", len(coord._started) == 1, str(coord._started))

    signed.clear()
    remote = await coord.async_resolve(media_content_id="media-source://reolink/FILE|remote|0|sub|x|1|2", refresh_token_id="t")
    check("absolute URLs are passed through unsigned", remote.get("url") == "https://nvr.example/clip.mp4" and not signed, str(remote))

    check("on-demand cache waits before downloading", mod.ON_DEMAND_CACHE_DELAY >= 30, str(mod.ON_DEMAND_CACHE_DELAY))


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
