#!/usr/bin/env python3
"""Check retention and reconcile.

- Purge drops clips past the age limit, then the oldest until the size fits.
- Reconcile drops index entries with no file and files with no entry, but
  never touches a clip that is still being saved.

    pip install homeassistant && python tests/test_retention.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import types
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from homeassistant.util import dt as dt_util

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


async def run_in_executor(func, *args):
    return func(*args)


def make(index: dict, files: list[str], cache_days: int = 7, max_bytes: int = 10**9):
    tmp = pathlib.Path(tempfile.mkdtemp())
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    coord._clips_dir, coord._thumbs_dir = tmp / "clips", tmp / "thumbs"
    for directory in (coord._clips_dir, coord._thumbs_dir):
        directory.mkdir()
    for name in files:
        (coord._clips_dir / f"{name}.mp4").write_bytes(b"x")
    coord._index = index
    coord._in_flight = set()
    coord._segments = {}
    coord._schedule_save = lambda: None
    mod.ReolinkClipCacheCoordinator.cache_days = property(lambda _s: cache_days)
    mod.ReolinkClipCacheCoordinator.max_cache_bytes = property(lambda _s: max_bytes)
    return coord


def record(days_ago: float, size: int = 100) -> dict:
    start = dt_util.utcnow() - timedelta(days=days_ago)
    return {"start": start.isoformat(), "size": size, "cached_at": "now"}


async def main() -> None:
    mod.async_dispatcher_send = lambda *_a: None

    # Age limit.
    coord = make({"old": record(10), "new": record(1)}, ["old", "new"])
    removed = await coord.async_purge()
    check("clips past the age limit go, newer ones stay",
          removed == 1 and set(coord._index) == {"new"} and not (coord._clips_dir / "old.mp4").exists()
          and (coord._clips_dir / "new.mp4").exists(), str(coord._index))

    # Size limit: oldest first, only as many as needed.
    coord = make({"a": record(3), "b": record(2), "c": record(1)}, ["a", "b", "c"], max_bytes=250)
    removed = await coord.async_purge()
    check("over the size limit the oldest clip goes first",
          removed == 1 and set(coord._index) == {"b", "c"}, str(coord._index))

    # Nothing to do.
    coord = make({"a": record(1)}, ["a"])
    check("a cache within both limits is left alone", await coord.async_purge() == 0 and "a" in coord._index)

    # Reconcile.
    coord = make({"gone": record(1), "here": record(1), "saving": record(1)}, ["here", "orphan", "saving"])
    del coord._index["saving"]
    coord._in_flight = {"saving"}
    await coord.async_reconcile()
    check("an entry with no file is dropped", "gone" not in coord._index)
    check("a file with no entry is deleted", not (coord._clips_dir / "orphan.mp4").exists())
    check("a clip still being saved is not deleted", (coord._clips_dir / "saving.mp4").exists())
    check("a healthy clip is kept", "here" in coord._index and (coord._clips_dir / "here.mp4").exists())


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
