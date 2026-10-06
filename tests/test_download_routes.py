#!/usr/bin/env python3
"""Check how clips are downloaded and how a refusing NVR is handled.

- The Reolink library route (download_vod) is tried first, and a route that
  worked for a camera is tried first next time.
- A clip that no route can fetch gives one warning naming every route.
- A camera whose clips keep failing pauses instead of being asked every sweep,
  and a manual sweep (force) ignores the pause.

    pip install homeassistant && python tests/test_download_routes.py
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import tempfile
import types
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


MCID = "media-source://reolink/FILE|entry|0|sub|Mp4Record/2026-10-06/RecM05_x.mp4|20261006211714|20261006211744"


async def run_in_executor(func, *args):
    return func(*args)


def make(tmp: pathlib.Path) -> mod.ReolinkClipCacheCoordinator:
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    coord._vod_types = {}
    coord._cameras = {}
    coord._backoff = {}
    coord._clip_failures = {}
    coord._last_error = ""
    coord._last_status = None
    coord._root = tmp
    coord._last_sweep = {}
    coord._shutdown = False
    return coord


class FakeStream:
    def __init__(self, data: bytes):
        self.data = data

    async def iter_chunked(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i : i + size]


class FakeApi:
    def __init__(self, data: bytes | None):
        self.data = data
        self.calls = []

    async def download_vod(self, filename, wanted_filename=None, start_time=None, end_time=None, channel=None, stream=None):
        self.calls.append((filename, start_time, end_time, channel, stream))
        if self.data is None:
            raise RuntimeError("Server disconnected")
        return types.SimpleNamespace(stream=FakeStream(self.data), close=lambda: None, length=len(self.data), filename="x.mp4", etag=None)


async def main() -> None:
    mod.DOWNLOAD_RETRY_BACKOFF = (0, 0)
    mod.DOWNLOAD_SPACING = 0
    tmp = pathlib.Path(tempfile.mkdtemp())
    descriptor = {
        "media_content_id": MCID, "camera": "back_door", "camera_name": "BACK DOOR",
        "event_type": "person", "start": "2026-10-06T21:17:14", "clip_id": "c1",
    }

    coord = make(tmp)
    order = coord._route_order(descriptor)
    check("the library route is tried first", order[0] == "LIBRARY", str(order))
    coord._vod_types["back_door"] = "NVR_DOWNLOAD"
    order = coord._route_order(descriptor)
    check("a route that worked goes first next time", order[0] == "NVR_DOWNLOAD" and "LIBRARY" in order and len(order) == len(set(order)), str(order))

    # The library route writes the clip and passes the NVR times and channel through.
    coord = make(tmp)
    api = FakeApi(b"x" * 200_000)
    coord._host_api = lambda _d: api
    written = await coord._async_library_download(descriptor, tmp / "a.part")
    check("the library route writes the whole clip", written == 200_000 and (tmp / "a.part").stat().st_size == 200_000, str(written))
    check("download_vod gets the NVR times, channel and stream", bool(api.calls) and api.calls[0][1:] == ("20261006211714", "20261006211744", 0, "sub"), str(api.calls))

    # Every route failing: one warning for the clip, naming each route.
    coord = make(tmp)
    coord._host_api = lambda _d: FakeApi(None)

    async def fail_route(route, d, dest):
        if route == "LIBRARY":
            return await coord._async_library_download(d, dest)
        coord._last_error = f"{route} refused"
        return 0

    coord._async_try_route = fail_route
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    mod._LOGGER.addHandler(handler)
    mod._LOGGER.setLevel(logging.DEBUG)
    written = await coord._async_download_with_retries(descriptor, tmp / "b.part")
    mod._LOGGER.removeHandler(handler)
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    check("a clip no route can fetch returns nothing", written == 0)
    check("it logs exactly one warning", len(warnings) == 1, str([r.getMessage() for r in warnings]))
    msg = warnings[0].getMessage() if warnings else ""
    check("the warning names every route", all(x in msg for x in ("Reolink library", "(DOWNLOAD)", "(NVR_DOWNLOAD)", "(PLAYBACK)", "proxy")), msg)
    check("the warning carries the library error", "Server disconnected" in msg, msg)

    # A camera whose clips keep failing pauses; a manual sweep ignores the pause.
    coord = make(tmp)
    camera = types.SimpleNamespace(key="back_door", name="BACK DOOR")
    listed: list[date] = []

    async def list_day(cam, day, **_kw):
        listed.append(day)
        return [{**descriptor, "clip_id": f"c{i}"} for i in range(5)]

    async def cache_fail(d):
        coord._clip_failures[d["clip_id"]] = coord._clip_failures.get(d["clip_id"], 0) + 1
        return False

    coord.async_list_day = list_day
    coord._async_cache_clip = cache_fail
    coord._is_cached = lambda _cid: False
    await coord._async_sweep_camera(camera, date(2026, 10, 6))
    check("failing clips pause the camera", "back_door" in coord._backoff, str(coord._backoff))
    coord._last_sweep.clear()
    before = len(listed)
    await coord._async_sweep_camera(camera, date(2026, 10, 6))
    check("a paused camera is not asked again", len(listed) == before)
    await coord._async_sweep_camera(camera, date(2026, 10, 6), force=True)
    check("a manual sweep ignores the pause", len(listed) == before + 1)

    # Clips that failed CLIP_FAILURE_LIMIT sweeps are skipped until forced.
    coord = make(tmp)
    coord.async_list_day = list_day
    coord._is_cached = lambda _cid: False
    tried: list[str] = []

    async def cache_track(d):
        tried.append(d["clip_id"])
        return True

    coord._async_cache_clip = cache_track
    coord._clip_failures = {"c0": mod.CLIP_FAILURE_LIMIT}
    await coord._async_sweep_camera(camera, date(2026, 10, 6))
    check("a clip that kept failing is skipped", "c0" not in tried, str(tried))
    coord._last_sweep.clear()
    tried.clear()
    await coord._async_sweep_camera(camera, date(2026, 10, 6), force=True)
    check("a manual sweep retries it", "c0" in tried, str(tried))


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
