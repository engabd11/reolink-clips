#!/usr/bin/env python3
"""Check the Sweep now and Clear cache buttons.

- Sweep now looks back over every day the cache keeps, ignores any pause after
  failed downloads, and runs in the background so the press returns at once.
- Clear cache deletes every clip, thumbnail and segment and empties the index,
  keeping the folders.

    pip install homeassistant && python tests/test_buttons.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import button as btn
from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


async def run_in_executor(func, *args):
    return func(*args)


async def main() -> None:
    check("there is a Sweep now and a Clear cache button",
          [d.key for d in btn.BUTTONS] == ["sweep_now", "clear_cache"])

    # Pressing the buttons.
    calls: list[tuple] = []

    class FakeCoordinator:
        cache_days = 3

        async def async_sweep(self, camera_key=None, days=None, force=False):
            calls.append(("sweep", len(days or []), force))
            return 0

        async def async_clear(self):
            calls.append(("clear",))
            return 0

    background: list[str] = []

    def create_task(_hass, coro, name):
        background.append(name)
        asyncio.get_running_loop().create_task(coro)

    entry = types.SimpleNamespace(entry_id="e1", async_create_background_task=create_task)
    fake = FakeCoordinator()
    buttons = {d.key: btn.ClipCacheButton(fake, entry, d) for d in btn.BUTTONS}
    for b in buttons.values():
        b.hass = object()
    await buttons["sweep_now"].async_press()
    await asyncio.sleep(0)
    check("Sweep now runs in the background", len(background) == 1, str(background))
    check("over every day the cache keeps, ignoring any pause", ("sweep", 3, True) in calls, str(calls))
    await buttons["clear_cache"].async_press()
    check("Clear cache clears", ("clear",) in calls, str(calls))
    check("the buttons share the integration's device",
          buttons["sweep_now"].device_info["identifiers"] == {("reolink_clip_cache", "e1")})

    # Clearing for real.
    tmp = pathlib.Path(tempfile.mkdtemp())
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    coord._clips_dir, coord._thumbs_dir, coord._segments_dir = tmp / "clips", tmp / "thumbs", tmp / "segments"
    for directory, name in ((coord._clips_dir, "a.mp4"), (coord._clips_dir, "b.part"), (coord._thumbs_dir, "a.jpg"), (coord._segments_dir, "s.mp4")):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"x")
    coord._index = {"a": {"cached_at": "now"}, "b": {"cached_at": "now"}}
    coord._clip_failures = {"z": 2}
    coord._segments = {"k": {}}
    saved: list[dict] = []

    async def save(data):
        saved.append(data)

    coord._store = types.SimpleNamespace(async_save=save)
    mod.async_dispatcher_send = lambda *_a: None
    removed = await coord.async_clear()
    left = [p.name for d in (coord._clips_dir, coord._thumbs_dir, coord._segments_dir) for p in d.iterdir()]
    check("Clear cache removes every file", removed == 2 and not left, f"{removed} {left}")
    check("and keeps the folders", coord._clips_dir.is_dir() and coord._thumbs_dir.is_dir())
    check("and empties the index, saved at once",
          coord._index == {} and coord._clip_failures == {} and saved and saved[-1] == {"clips": {}}, str(saved))


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
