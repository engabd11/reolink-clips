#!/usr/bin/env python3
"""Check that a new event is cached ahead of a running sweep.

- A detection fetches the camera's newest clips without waiting for the sweep
  lock, which a long Sweep now backfill holds for its whole run.
- While that fetch (or a Play press) waits for the download slot, the sweep
  holds off before its next clip, so the new clip goes next.
- Only the newest few uncached clips are fetched, and not for a paused camera.

    pip install homeassistant && python tests/test_event_priority.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def make() -> mod.ReolinkClipCacheCoordinator:
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace()
    coord._cameras = {"carport": types.SimpleNamespace(key="carport", name="Carport", entry_id="e", channel="2")}
    coord._backoff = {}
    coord._clip_failures = {}
    coord._last_sweep = {}
    coord._shutdown = False
    coord._sweep_lock = asyncio.Lock()
    coord._priority_jobs = 0
    coord._priority_idle = asyncio.Event()
    coord._priority_idle.set()
    coord._is_cached = lambda _cid: False
    coord._schedule_save = lambda: None
    return coord


def clips(prefix: str, n: int) -> list[dict]:
    return [{"clip_id": f"{prefix}{i}", "camera": "carport"} for i in range(n)]


async def main() -> None:
    mod.DOWNLOAD_SPACING = 0
    mod.async_dispatcher_send = lambda *_a: None

    # The sweep lock is held by a long backfill: the event fetch does not wait for it.
    coord = make()
    done: list[str] = []

    async def list_day(_camera, _day, **_kw):
        return clips("new", 5)

    async def cache(descriptor):
        done.append(descriptor["clip_id"])
        return True

    coord.async_list_day = list_day
    coord._async_cache_clip = cache
    await coord._sweep_lock.acquire()
    cached = await asyncio.wait_for(coord.async_sweep_event("carport"), 2)
    coord._sweep_lock.release()
    check("a new event is cached while a sweep holds the lock", cached == mod.EVENT_FETCH_LIMIT, str(done))
    check("only the newest few uncached clips are fetched", done == ["new0", "new1", "new2"], str(done))

    # The sweep yields: sweep clip 1, then the new event, then the rest of the sweep.
    coord = make()
    order: list[str] = []
    slot = asyncio.Semaphore(1)
    first_started = asyncio.Event()

    async def slow_cache(descriptor):
        async with slot:
            order.append(descriptor["clip_id"])
            if descriptor["clip_id"] == "old0":
                first_started.set()
                await asyncio.sleep(0.2)
            return True

    async def list_old(_camera, _day, **_kw):
        return clips("old", 3)

    coord._async_cache_clip = slow_cache
    coord.async_list_day = list_old
    camera = coord._cameras["carport"]
    sweep = asyncio.create_task(coord._async_sweep_camera(camera, date.today() - timedelta(days=2), force=True))
    await first_started.wait()
    coord.async_list_day = list_day
    event = asyncio.create_task(coord.async_sweep_event("carport"))
    await asyncio.gather(sweep, event)
    check("the new event goes right after the download in progress",
          order[:4] == ["old0", "new0", "new1", "new2"] and order[4:] == ["old1", "old2"], str(order))

    # A paused camera is left alone.
    coord = make()
    coord._backoff["carport"] = (mod.dt_util.utcnow() + timedelta(minutes=10), 1)
    called: list[str] = []

    async def no_list(*_a, **_k):
        called.append("list")
        return []

    coord.async_list_day = no_list
    check("a camera paused after failed downloads is not asked",
          await coord.async_sweep_event("carport") == 0 and not called)

    # A detection schedules the event fetch, not a full sweep.
    coord = make()
    coord._pending_sweeps = {}
    scheduled = {}

    def call_later(_hass, delay, action):
        scheduled["delay"], scheduled["action"] = delay, action
        return lambda: None

    mod.async_call_later = call_later
    ran: list[str] = []

    async def fake_event(key):
        ran.append(key)
        return 0

    coord.async_sweep_event = fake_event
    coord._schedule_event_sweep("carport")
    await scheduled["action"](None)
    check("a detection runs the event fetch after the settle delay",
          ran == ["carport"] and scheduled["delay"] == mod.EVENT_SETTLE_DELAY, str(scheduled))


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
