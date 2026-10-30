#!/usr/bin/env python3
"""Check the retention limits, the disk reconcile and the camera guard.

- Purge drops clips past the age limit, then the oldest until the cache fits
  its size limit, and deletes their files.
- A clip evicted for size leaves a marker so the sweep does not fetch it
  again; the age limit drops the marker later.
- Reconcile drops index entries whose file is gone and deletes files the
  index does not know.
- A sweep for a camera that does not exist sweeps nothing.

    pip install homeassistant && python tests/test_purge.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import types
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []
MB = 1024 * 1024


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


async def run_in_executor(func, *args):
    return func(*args)


def make(tmp: pathlib.Path, cache_days: int, max_mb: int) -> mod.ReolinkClipCacheCoordinator:
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    coord.entry = types.SimpleNamespace(options={"cache_days": cache_days, "max_cache_size_mb": max_mb})
    coord._root, coord._clips_dir, coord._thumbs_dir = tmp, tmp / "clips", tmp / "thumbs"
    coord._clips_dir.mkdir(parents=True, exist_ok=True)
    coord._thumbs_dir.mkdir(parents=True, exist_ok=True)
    coord._index = {}
    coord._clip_failures = {}
    coord._schedule_save = lambda: None
    return coord


def add(coord, clip_id: str, days_ago: float, size_mb: int) -> None:
    start = mod.dt_util.now() - timedelta(days=days_ago)
    coord._index[clip_id] = {"start": start.isoformat(), "size": size_mb * MB, "cached_at": "now"}
    (coord._clips_dir / f"{clip_id}.mp4").write_bytes(b"x")
    (coord._thumbs_dir / f"{clip_id}.jpg").write_bytes(b"x")


def files(coord) -> set[str]:
    return {p.stem for p in coord._clips_dir.iterdir()}


async def main() -> None:
    mod.async_dispatcher_send = lambda *_a: None

    # Age first, then oldest first until the cache fits.
    coord = make(pathlib.Path(tempfile.mkdtemp()), cache_days=7, max_mb=100)
    add(coord, "old", 10, 1)
    add(coord, "a", 2, 60)
    add(coord, "b", 1, 60)
    add(coord, "c", 0, 10)
    removed = await coord.async_purge()
    check("purge removes the expired clip and the oldest over the size limit", removed == 2, str(removed))
    check("their files are deleted, the newest kept", files(coord) == {"b", "c"}, str(files(coord)))
    check("the expired clip leaves nothing in the index", "old" not in coord._index)
    check("the clip evicted for size leaves a marker", coord._index.get("a", {}).get("evicted") is True, str(coord._index.get("a")))
    check("a marker is not a cached clip", not coord._is_cached("a") and coord.stats()["total_clips"] == 2, str(coord.stats()))

    # The sweep leaves an evicted clip alone, even when forced.
    check("the sweep does not fetch an evicted clip again", not coord._needs_fetch("a") and not coord._needs_fetch("a", force=True))
    check("nor a cached one", not coord._needs_fetch("b"))
    check("but does fetch a new one", coord._needs_fetch("new"))
    coord._clip_failures["bad"] = mod.CLIP_FAILURE_LIMIT
    check("a clip that kept failing waits for a forced sweep", not coord._needs_fetch("bad") and coord._needs_fetch("bad", force=True))

    check("a second purge has nothing to do", await coord.async_purge() == 0 and files(coord) == {"b", "c"})

    # The age limit drops the marker in time.
    coord.entry.options["cache_days"] = 1
    await coord.async_purge()
    check("the age limit drops an old marker", "a" not in coord._index, str(coord._index.get("a")))

    # Reconcile with the disk.
    coord = make(pathlib.Path(tempfile.mkdtemp()), cache_days=7, max_mb=100)
    add(coord, "kept", 0, 1)
    add(coord, "gone", 0, 1)
    (coord._clips_dir / "gone.mp4").unlink()
    (coord._clips_dir / "orphan.mp4").write_bytes(b"x")
    coord._index["marker"] = {"start": mod.dt_util.now().isoformat(), "evicted": True}
    await coord.async_reconcile()
    check("reconcile drops an entry whose file is gone", "gone" not in coord._index and "kept" in coord._index)
    check("and deletes a file the index does not know", files(coord) == {"kept"}, str(files(coord)))
    check("and keeps eviction markers", "marker" in coord._index)

    # A camera that does not exist sweeps nothing.
    coord = make(pathlib.Path(tempfile.mkdtemp()), cache_days=7, max_mb=100)
    coord._shutdown = False
    coord._cameras = {"carport": types.SimpleNamespace(key="carport", name="Carport")}
    coord._sweep_lock = asyncio.Lock()
    swept: list[str] = []

    async def sweep_camera(camera, _day, _force):
        swept.append(camera.key)
        return 0

    coord._async_sweep_camera = sweep_camera
    check("a sweep for an unknown camera sweeps nothing",
          await coord.async_sweep(camera_key="carprot") == 0 and not swept, str(swept))
    await coord.async_sweep(camera_key="carport")
    check("a sweep for a known camera sweeps just that one", swept == ["carport"], str(swept))
    report = await coord.async_diagnose("carprot")
    check("diagnose names the cameras there are", "carport" in report.get("error", ""), str(report))


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
