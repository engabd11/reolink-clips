#!/usr/bin/env python3
"""Check the FLV route for NVRs that refuse every MP4 download.

- FLV is on the route ladder and the NVR login in its URL never reaches a log.
- A playback stream that does not end is read for the clip's length plus a
  grace period and kept; one the NVR cuts off early keeps what arrived.
- An answer that is not FLV is refused.
- An FLV recording is remuxed to MP4, and is never stored as it came.
- diagnose names FLV data when it sees it.

    pip install homeassistant && python tests/test_flv_route.py
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import types

from aiohttp import ClientSession, web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import const
from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


MCID = "media-source://reolink/FILE|entry|0|sub|Mp4Record/2026-10-06/RecM05_x.mp4|20261006211714|20261006211744"
FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"


async def run_in_executor(func, *args):
    return func(*args)


def make(tmp: pathlib.Path) -> mod.ReolinkClipCacheCoordinator:
    coord = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coord.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    coord._vod_types = {}
    coord._last_error = ""
    coord._last_status = None
    coord._root = tmp
    return coord


async def endless(request: web.Request) -> web.StreamResponse:
    """An FLV playback stream that never ends."""
    response = web.StreamResponse(headers={"Content-Type": "video/x-flv"})
    await response.prepare(request)
    await response.write(FLV_HEADER)
    try:
        while True:
            await response.write(b"\x00" * 4096)
            await asyncio.sleep(0.05)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def cut_off(request: web.Request) -> web.StreamResponse:
    """A stream the NVR drops part way through."""
    response = web.StreamResponse(headers={"Content-Type": "video/x-flv"})
    response.enable_chunked_encoding()
    await response.prepare(request)
    await response.write(FLV_HEADER + b"\x00" * 8192)
    request.transport.close()
    return response


async def refused(_request: web.Request) -> web.Response:
    return web.Response(status=401, text="bad login for password=hunter2", content_type="text/html")


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    descriptor = {"media_content_id": MCID, "camera": "carport", "camera_name": "Carport", "clip_id": "c1"}

    # Ladder and redaction.
    check("FLV is on the route ladder, before PLAYBACK",
          "FLV" in const.VOD_TYPE_LADDER and const.VOD_TYPE_LADDER.index("FLV") < const.VOD_TYPE_LADDER.index("PLAYBACK"))
    check("FLV is in a camera's route order", "FLV" in make(tmp)._route_order(descriptor))
    secret = "https://nvr/flv?port=1935&channel=0&user=admin&password=hunter2&token=abc123 'x'"
    cleaned = mod.redact(f"ServerDisconnectedError: {secret}")
    check("redact hides the user, password and token",
          all(x not in cleaned for x in ("admin", "hunter2", "abc123")) and "password=***" in cleaned, cleaned)
    check("a clip's length comes from its id", mod.ReolinkClipCacheCoordinator._clip_seconds(descriptor) == 30)

    # The FLV route asks for the clip's length plus the grace period.
    coord = make(tmp)
    asked: dict = {}

    async def fake_download(url, dest, headers=None, label="", max_seconds=None):
        asked["max_seconds"] = max_seconds
        dest.write_bytes(asked["body"])
        return len(asked["body"])

    coord._async_download = fake_download
    asked["body"] = FLV_HEADER + b"\x00" * 100
    written = await coord._async_flv_download("http://nvr/flv", descriptor, tmp / "f.part")
    check("the FLV route keeps an FLV stream", written == len(asked["body"]), coord._last_error)
    check("it listens for the clip's length plus the grace period",
          asked["max_seconds"] == 30 + const.FLV_GRACE_SECONDS, str(asked))
    asked["body"] = b"<html>password=hunter2 login failed</html>"
    written = await coord._async_flv_download("http://nvr/flv", descriptor, tmp / "g.part")
    check("an answer that is not FLV is refused", written == 0 and "other than FLV" in coord._last_error, coord._last_error)
    check("that refusal does not leak the password", "hunter2" not in coord._last_error, coord._last_error)

    # Real HTTP: an endless stream stops on time, a cut-off one keeps its data.
    app = web.Application()
    app.router.add_get("/endless", endless)
    app.router.add_get("/cut", cut_off)
    app.router.add_get("/refused", refused)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    async with ClientSession() as session:
        mod.async_get_clientsession = lambda _hass, verify_ssl=True: session
        coord = make(tmp)
        started = time.monotonic()
        written = await coord._async_download(f"{base}/endless", tmp / "e.part", max_seconds=1)
        took = time.monotonic() - started
        check("an endless stream is read for max_seconds and kept", written > 0 and 0.9 <= took < 5, f"{written} bytes in {took:.1f}s")

        coord = make(tmp)
        written = await coord._async_download(f"{base}/cut", tmp / "c.part", max_seconds=30)
        check("a stream the NVR drops keeps what arrived", written >= len(FLV_HEADER), f"{written} {coord._last_error}")

        coord = make(tmp)
        written = await coord._async_download(f"{base}/refused?user=admin&password=hunter2", tmp / "r.part")
        check("a refusal is reported without the password",
              written == 0 and coord._last_status == 401 and "hunter2" not in coord._last_error, coord._last_error)

        coord = make(tmp)
        report = await coord._async_probe(f"{base}/endless", {}, ranged=False)
        check("diagnose names FLV data", report.startswith("OK") and "FLV data" in report, report)
        report = await coord._async_probe(f"{base}/refused", {}, ranged=False)
        check("diagnose reports a refusal without the password", "401" in report and "hunter2" not in report, report)

    await runner.cleanup()

    # On-demand caching waits so it does not compete with the viewer's stream,
    # except for a camera that only works over FLV, where nothing streams.
    slept: list[float] = []
    real_sleep = mod.asyncio.sleep

    async def fake_sleep(seconds, *args):
        slept.append(seconds)

    mod.asyncio.sleep = fake_sleep
    try:
        for route, waits in (("DOWNLOAD", True), ("FLV", False)):
            coord = make(tmp)
            coord._cameras = {"carport": types.SimpleNamespace(key="carport", entry_id="entry", channel="0")}
            coord._index = {}
            coord._vod_types = {"carport": route}
            cached: list[str] = []

            async def list_day(_camera, _day, **_kw):
                return [descriptor]

            async def cache(d):
                cached.append(d["clip_id"])
                return False

            coord.async_list_day = list_day
            coord._async_cache_clip = cache
            slept.clear()
            await coord._async_cache_on_demand(MCID)
            waited = mod.ON_DEMAND_CACHE_DELAY in slept
            check(f"on-demand caching {'waits' if waits else 'starts at once'} when the camera uses {route}",
                  waited is waits and cached == ["c1"], f"slept {slept}, cached {cached}")
    finally:
        mod.asyncio.sleep = real_sleep

    # Without ffmpeg an FLV recording is refused, never stored as .mp4.
    coord = make(tmp)
    coord._ffmpeg_binary = lambda: None
    src, dest = tmp / "n.part", tmp / "n.mp4"
    src.write_bytes(FLV_HEADER + b"\x00" * 100)
    ok = await coord._async_faststart(src, dest)
    check("without ffmpeg an FLV recording is not stored", ok is False and not dest.exists(), coord._last_error)

    # With ffmpeg a real FLV recording becomes a playable MP4.
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("  SKIP  remuxing a real FLV recording (no ffmpeg)")
    else:
        src, dest = tmp / "real.part", tmp / "real.mp4"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "flv", str(src)],
            check=True,
        )

        async def run_ffmpeg(binary, *args):
            proc = await asyncio.create_subprocess_exec(
                binary, "-hide_banner", "-loglevel", "error", *args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0

        coord = make(tmp)
        coord._ffmpeg_binary = lambda: ffmpeg
        coord._run_ffmpeg = run_ffmpeg
        ok = await coord._async_faststart(src, dest)
        head = dest.read_bytes()[:64] if dest.exists() else b""
        check("an FLV recording is remuxed to MP4", ok and b"ftyp" in head and not src.exists(), coord._last_error)


asyncio.run(main())
print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
