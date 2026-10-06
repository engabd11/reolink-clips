#!/usr/bin/env python3
"""Check the native file name routes (home-assistant/core#179099).

An RLN8-410 on firmware 3.6.5 hangs up on every request for a recording made
by time, but serves its hour-long segments by the name its Search reports.

- The segment holding a clip is found from the Search reply, with the clip's
  offset into it, and looked up once per clip.
- NATIVE_FLV streams from that offset (FLV seek); NATIVE_DOWNLOAD fetches the
  segment and asks for the clip to be cut out of it.
- The cut is done by ffmpeg, and without ffmpeg a segment is never stored.
- The heavy native download comes late in the route order.

    pip install homeassistant && python tests/test_native_route.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# A clip at 17:37:04 for 45 s, listed under a time-based name.
MCID = "media-source://reolink/FILE|entry|2|sub|20261006165959|20261006173704|20261006173749"


def t(y, mo, d, h, mi, s):
    return {"year": y, "mon": mo, "day": d, "hour": h, "min": mi, "sec": s}


SEARCH = [
    {"name": "1-2-0-01260906055959-00000", "StartTime": t(2026, 10, 6, 16, 59, 59), "EndTime": t(2026, 10, 6, 17, 59, 59), "size": "197656576", "type": "sub"},
    {"name": "1-2-0-01260906065959-00000", "StartTime": t(2026, 10, 6, 17, 59, 59), "EndTime": t(2026, 10, 6, 18, 59, 59), "size": "1000", "type": "sub"},
    {"StartTime": t(2026, 10, 6, 17, 30, 0), "EndTime": t(2026, 10, 6, 17, 40, 0), "size": "5", "type": "sub"},
]


async def run_in_executor(func, *args):
    return func(*args)


class FakeApi:
    def __init__(self):
        self.searches = 0

    async def request_vod_files(self, channel, start, end, status_only=False, stream=None, **_kw):
        self.searches += 1
        return [], [types.SimpleNamespace(data=d) for d in SEARCH]


def make(tmp: pathlib.Path, api: FakeApi) -> mod.ReolinkClipCacheCoordinator:
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    coord._vod_types = {}
    coord._native_files = {}
    coord._last_error = ""
    coord._last_status = None
    coord._root = tmp
    coord._host_api = lambda _d: api
    return coord


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    descriptor = {"media_content_id": MCID, "camera": "carport", "camera_name": "Carport", "clip_id": "c1"}

    # Picking the segment.
    native = mod.pick_native_file(SEARCH, "20261006173704")
    check("the named segment holding the clip is picked",
          native is not None and native["name"] == "1-2-0-01260906055959-00000", str(native))
    check("the clip's offset into it is right (37 min 5 s)", native and native["offset"] == 37 * 60 + 5, str(native))
    check("a clip in the next hour gets the next segment",
          (mod.pick_native_file(SEARCH, "20261006180500") or {}).get("name") == "1-2-0-01260906065959-00000")
    check("entries without a name are ignored, and no cover gives None",
          mod.pick_native_file(SEARCH[2:], "20261006173704") is None and mod.pick_native_file(SEARCH, "20261006200000") is None)

    # Seek.
    flv = "https://nvr:443/flv?port=1935&app=bcs&stream=playback.bcs&channel=2&type=1&start=1-2-0-x&seek=0&user=u&password=p"
    sought = mod.with_seek(flv, 2225)
    check("with_seek sets the seek and keeps the rest", sought == flv.replace("seek=0", "seek=2225"), sought)
    check("with_seek adds a seek when there is none", mod.with_seek("http://nvr/flv?a=1", 5) == "http://nvr/flv?a=1&seek=5")

    # Route order.
    api = FakeApi()
    order = make(tmp, api)._route_order(descriptor)
    check("native FLV is tried right after the library", order[:2] == ["LIBRARY", "NATIVE_FLV"], str(order))
    check("the whole-segment download comes just before the proxy", order[-2:] == ["NATIVE_DOWNLOAD", "PROXY"], str(order))

    # NATIVE_FLV: native name, seek to the offset, streamed for the clip.
    coord = make(tmp, api)
    asked: list[tuple] = []

    async def direct_source(d, request_type, plain_http=False, file_name=None):
        asked.append((request_type, plain_http, file_name))
        if request_type == "FLV":
            return f"https://nvr/flv?start={file_name}&seek=0&password=p"
        return f"https://nvr/cgi-bin/api.cgi?cmd=Download&source={file_name}&token=t"

    flv_urls: list[str] = []

    async def flv_download(url, d, dest):
        flv_urls.append(url)
        return 1234

    coord._async_direct_source = direct_source
    coord._async_flv_download = flv_download
    written = await coord._async_try_route("NATIVE_FLV", descriptor, tmp / "a.part")
    check("NATIVE_FLV asks for FLV by the native name",
          written == 1234 and asked and asked[-1] == ("FLV", False, "1-2-0-01260906055959-00000"), str(asked))
    check("and seeks to the clip", flv_urls and "seek=2225" in flv_urls[-1], str(flv_urls))
    check("NATIVE_FLV leaves nothing to trim", "_trim" not in descriptor)

    # NATIVE_DOWNLOAD: native name, then the clip is to be cut out.
    downloads: list[dict] = []

    async def download(url, dest, headers=None, label="", max_seconds=None, timeout=None):
        downloads.append({"url": url, "timeout": timeout})
        return 99

    coord._async_download = download
    written = await coord._async_try_route("NATIVE_DOWNLOAD", descriptor, tmp / "b.part")
    check("NATIVE_DOWNLOAD fetches the segment by its native name",
          written == 99 and asked[-1] == ("DOWNLOAD", False, "1-2-0-01260906055959-00000"), str(asked))
    check("with the longer segment timeout", downloads and downloads[-1]["timeout"] == mod.NATIVE_DOWNLOAD_TIMEOUT, str(downloads))
    check("and marks the clip to cut out (offset, length)", descriptor.get("_trim") == (2225, 45), str(descriptor.get("_trim")))
    check("the NVR is searched once per clip", api.searches == 1, str(api.searches))

    # No named segment: a clear reason, no request.
    class EmptyApi(FakeApi):
        async def request_vod_files(self, *a, **k):
            return [], []

    coord = make(tmp, EmptyApi())
    coord._async_direct_source = direct_source
    n = len(asked)
    written = await coord._async_try_route("NATIVE_FLV", dict(descriptor), tmp / "c.part")
    check("no named segment: nothing asked of the NVR", written == 0 and len(asked) == n)
    check("and the reason says so", "none with a name" in coord._last_error, coord._last_error)

    # Cutting.
    coord = make(tmp, api)
    coord._ffmpeg_binary = lambda: None
    src = tmp / "seg.part"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    ok = await coord._async_faststart(src, tmp / "seg.mp4", (10, 5))
    check("without ffmpeg a whole segment is not stored as the clip", ok is False and not (tmp / "seg.mp4").exists(), coord._last_error)

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not (ffmpeg and ffprobe):
        print("  SKIP  cutting a real clip (no ffmpeg/ffprobe)")
    else:
        seg = tmp / "real.part"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=12",
             "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-f", "mp4", str(seg)],
            check=True,
        )

        async def run_ffmpeg(binary, *args):
            proc = await asyncio.create_subprocess_exec(
                binary, "-hide_banner", "-loglevel", "error", *args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0

        coord._ffmpeg_binary = lambda: ffmpeg
        coord._run_ffmpeg = run_ffmpeg
        out = tmp / "cut.mp4"
        ok = await coord._async_faststart(seg, out, (5, 4))
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(out)],
                               capture_output=True, text=True)
        duration = float(json.loads(probe.stdout or "{}").get("format", {}).get("duration", 0))
        check("ffmpeg cuts the clip out of the segment (about 4 s of 12)", ok and 3.5 <= duration <= 5.5, f"{ok} {duration}")


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
