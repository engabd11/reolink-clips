"""Coordinator for Reolink Clip Cache.

Discovers Reolink cameras, sweeps the Reolink media source for new event
recordings, downloads and faststarts them into local storage, and maintains
the index that the WebSocket API, the HTTP views and the sensors read from.

The NVR only finalises a recording *after* the event has ended, so caching is
driven by a periodic sweep that diffs the media source against the index.
Detection sensors merely bring the next sweep forward; they are a latency
optimisation, never the source of truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date as dt_date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientError

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import (
    async_browse_media,
    async_resolve_media,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .const import (
    CACHEABLE_TRIGGERS,
    CLIPS_DIR_NAME,
    CLIP_URL,
    CONF_CACHE_DAYS,
    CONF_CAMERAS,
    CONF_EVENT_TYPES,
    CONF_MAX_CACHE_MB,
    CONF_STREAM,
    CONF_SWEEP_MINUTES,
    CAMERA_BACKOFF_MINUTES,
    CLIP_FAILURE_LIMIT,
    CONSECUTIVE_FAILURE_LIMIT,
    DEFAULT_CACHE_DAYS,
    DEFAULT_EVENT_TYPES,
    DEFAULT_MAX_CACHE_MB,
    DEFAULT_STREAM,
    DEFAULT_SWEEP_MINUTES,
    DIAGNOSE_DAYS,
    DIRECT_ROUTES,
    DIRECT_HEADERS,
    DOWNLOAD_ATTEMPTS,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_HEADERS,
    DOWNLOAD_RETRY_BACKOFF,
    DOWNLOAD_SPACING,
    DOWNLOAD_TIMEOUT,
    EVENT_CLIP_CACHED,
    EVENT_SETTLE_DELAY,
    FFMPEG_TIMEOUT,
    FLV_FALLBACK_SECONDS,
    FLV_GRACE_SECONDS,
    NATIVE_DOWNLOAD,
    NATIVE_DOWNLOAD_TIMEOUT,
    NATIVE_FLV,
    NATIVE_ROUTES,
    SEGMENT_KEEP_MINUTES,
    SEGMENT_SETTLE_SECONDS,
    SEGMENTS_DIR_NAME,
    SEGMENTS_KEPT,
    MAX_CLIPS_PER_SWEEP,
    MAX_CONCURRENT_DOWNLOADS,
    OK_STATUSES,
    PLAIN_HTTP_SUFFIX,
    REOLINK_DOMAIN,
    REOLINK_MEDIA_PREFIX,
    SIGNAL_INDEX_UPDATED,
    ON_DEMAND_CACHE_DELAY,
    SIGNED_URL_TTL,
    STORAGE_DIR_NAME,
    STORAGE_KEY,
    STORAGE_VERSION,
    STREAMS,
    SWEEP_COOLDOWN,
    THUMBS_DIR_NAME,
    THUMB_URL,
    TRIGGER_ALIASES,
)

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 10

# The FLV route puts the NVR login in the URL, and a token rides on the others.
_SECRET_PARAMS = re.compile(r"((?:password|token|user)=)[^&\s'\"]*", re.IGNORECASE)

FLV_MAGIC = b"FLV"


def split_route(route: str) -> tuple[str, bool]:
    """Split a direct route into its VOD request type and whether it is plain HTTP."""
    if route.endswith(PLAIN_HTTP_SUFFIX):
        return route[: -len(PLAIN_HTTP_SUFFIX)], True
    return route, False


def plain_http_url(url: str, netport: dict[str, Any] | None) -> str | None:
    """Return an HTTPS NVR URL rewritten for the NVR's plain HTTP port.

    None when the URL is not HTTPS (nothing to change) or the NVR has its HTTP
    port turned off.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    ports = (netport or {}).get("NetPort") or {}
    if ports.get("httpEnable", 1) != 1:
        return None
    try:
        port = int(ports.get("httpPort") or 80)
    except (TypeError, ValueError):
        port = 80
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = host if port == 80 else f"{host}:{port}"
    return urlunsplit(("http", netloc, parts.path, parts.query, parts.fragment))


def with_seek(url: str, seconds: int) -> str:
    """Set the seek (seconds into the recording) of an NVR FLV playback URL."""
    if re.search(r"[?&]seek=", url):
        return re.sub(r"([?&]seek=)[^&]*", lambda m: f"{m.group(1)}{int(seconds)}", url, count=1)
    return f"{url}{'&' if '?' in url else '?'}seek={int(seconds)}"


def pick_native_file(files: list[dict[str, Any]], start_id: str) -> dict[str, Any] | None:
    """Pick the named recording segment that holds a clip starting at start_id.

    ``files`` are the raw entries of the NVR's Search reply. Returns the
    segment's name, its start id and the clip's offset into it in seconds.
    """
    clip_start = _parse_reolink_time(start_id)
    if clip_start is None:
        return None
    best: dict[str, Any] | None = None
    for data in files:
        name = data.get("name")
        try:
            seg_start = _parse_reolink_time(_time_id(data["StartTime"]))
            seg_end = _parse_reolink_time(_time_id(data["EndTime"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not name or seg_start is None or seg_end is None:
            continue
        if seg_start <= clip_start < seg_end:
            offset = int((clip_start - seg_start).total_seconds())
            # Prefer the segment that starts closest before the clip.
            if best is None or offset < best["offset"]:
                best = {"name": name, "start": _time_id(data["StartTime"]), "offset": offset}
    return best


def _time_id(value: dict[str, Any]) -> str:
    """Turn a Reolink time dict into a YYYYMMDDHHMMSS id."""
    return (
        f"{int(value['year']):04d}{int(value['mon']):02d}{int(value['day']):02d}"
        f"{int(value['hour']):02d}{int(value['min']):02d}{int(value['sec']):02d}"
    )


def redact(text: str) -> str:
    """Hide credentials that an error message may echo back from a URL."""
    return _SECRET_PARAMS.sub(r"\1***", text)


@dataclass(slots=True)
class CameraInfo:
    """A Reolink camera as seen through the media source."""

    key: str
    """Stable slug the card uses to address this camera."""

    name: str
    """Display name, identical to the media browser and the device name."""

    entry_id: str
    channel: str
    media_content_id: str
    sensors: dict[str, str] = field(default_factory=dict)
    """Detection trigger -> binary_sensor entity_id."""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation for the card."""
        return {
            "key": self.key,
            "name": self.name,
            "channel": self.channel,
            "event_types": sorted(self.sensors),
            "sensors": dict(self.sensors),
        }


@dataclass(slots=True)
class _StubChild:
    """Minimal stand-in for a browse result when only the id is known."""

    media_content_id: str
    title: str


# Browse results carry a full media source URI
# ("media-source://reolink/CAM|entry|0"), while the Reolink source's own
# identifiers are the bare part after the slash. Anything we hand to
# async_browse_media needs the URI form; anything we parse needs the bare form.
MEDIA_SOURCE_PREFIX = f"{REOLINK_MEDIA_PREFIX}/"


def bare_identifier(media_content_id: str) -> str:
    """Return the Reolink identifier without the media source URI scheme."""
    value = media_content_id or ""
    if value.startswith(MEDIA_SOURCE_PREFIX):
        return value[len(MEDIA_SOURCE_PREFIX):]
    return value


def media_uri(identifier: str) -> str:
    """Return the browsable media source URI for a Reolink identifier."""
    if identifier.startswith(MEDIA_SOURCE_PREFIX):
        return identifier
    return f"{MEDIA_SOURCE_PREFIX}{identifier}"


def clip_id_for(media_content_id: str) -> str:
    """Return the stable local id for a media source item.

    Derived from the media content id rather than from anything a caller
    supplies, so ids can never be steered at the filesystem. The URI scheme is
    stripped first so the id is the same whichever form the caller passes.
    """
    identifier = bare_identifier(media_content_id).encode("utf-8")
    return hashlib.sha1(identifier).hexdigest()[:16]


def _local_tz():
    """Return HA's configured timezone across HA versions."""
    getter = getattr(dt_util, "get_default_time_zone", None)
    return getter() if getter else dt_util.DEFAULT_TIME_ZONE


def _parse_reolink_time(value: str) -> datetime | None:
    """Parse a Reolink ``YYYYMMDDHHMMSS`` time id into an aware datetime.

    Reolink reports recording times in the camera's local time, which HA is
    configured to match.
    """
    try:
        naive = datetime.strptime(value, "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=_local_tz())


def normalise_trigger(trigger: str) -> str:
    """Fold Reolink's trigger spellings into the ones the card shows."""
    lowered = trigger.lower()
    return TRIGGER_ALIASES.get(lowered, lowered)


class ReolinkClipCacheCoordinator:
    """Owns discovery, the sweep loop, local storage and the clip index."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        self.hass = hass
        self.entry = entry

        self._root = Path(hass.config.path(STORAGE_DIR_NAME))
        self._clips_dir = self._root / CLIPS_DIR_NAME
        self._thumbs_dir = self._root / THUMBS_DIR_NAME
        self._segments_dir = self._root / SEGMENTS_DIR_NAME
        # Recording segments fetched for NATIVE_DOWNLOAD, by segment key.
        self._segments: dict[str, dict[str, Any]] = {}

        # clip_id -> clip record (a descriptor plus cache state)
        self._index: dict[str, dict[str, Any]] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        self._cameras: dict[str, CameraInfo] = {}

        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._in_flight: set[str] = set()
        self._sweep_lock = asyncio.Lock()
        self._last_sweep: dict[str, datetime] = {}

        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_detection: CALLBACK_TYPE | None = None
        self._pending_sweeps: dict[str, CALLBACK_TYPE] = {}
        self._base_url: str | None = None
        # camera key -> the download route that actually works for it
        self._vod_types: dict[str, str] = {}
        # camera key -> (paused until, how many sweeps in a row gave up)
        self._backoff: dict[str, tuple[datetime, int]] = {}
        # clip id -> sweeps that failed to download it
        self._clip_failures: dict[str, int] = {}
        # (entry, channel, stream, start id) -> the clip's native segment.
        self._native_files: dict[tuple[str, ...], dict[str, Any]] = {}
        # why the last download attempt failed, for one summary warning per clip
        self._last_error = ""
        self._last_status: int | None = None
        self._shutdown = False

    # ── Options ────────────────────────────────────────────────────────

    @property
    def options(self) -> dict[str, Any]:
        """Return the current entry options."""
        return dict(self.entry.options)

    @property
    def stream(self) -> str:
        """Return the media source stream to cache."""
        stream = self.options.get(CONF_STREAM, DEFAULT_STREAM)
        return stream if stream in STREAMS else DEFAULT_STREAM

    @property
    def event_types(self) -> list[str]:
        """Return the triggers the user wants cached."""
        configured = self.options.get(CONF_EVENT_TYPES) or DEFAULT_EVENT_TYPES
        return [normalise_trigger(item) for item in configured]

    @property
    def selected_cameras(self) -> list[str]:
        """Return the camera keys to cache, empty meaning every camera."""
        return list(self.options.get(CONF_CAMERAS) or [])

    @property
    def cache_days(self) -> int:
        """Return the retention window in days."""
        return int(self.options.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS))

    @property
    def max_cache_bytes(self) -> int:
        """Return the cache size ceiling in bytes."""
        megabytes = int(self.options.get(CONF_MAX_CACHE_MB, DEFAULT_MAX_CACHE_MB))
        return megabytes * 1024 * 1024

    @property
    def cameras(self) -> dict[str, CameraInfo]:
        """Return the discovered cameras keyed by slug."""
        return self._cameras

    @property
    def index(self) -> dict[str, dict[str, Any]]:
        """Return the clip index keyed by clip id."""
        return self._index

    # ── Setup / teardown ───────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Prepare storage and start work once HA has finished starting."""
        await self.hass.async_add_executor_job(self._make_dirs)
        await self._async_load_index()

        # Reolink and its media source may not be loaded yet during setup.
        self._unsubs.append(async_at_started(self.hass, self._async_started))

    def _make_dirs(self) -> None:
        """Create the cache directories and drop stale segments (executor)."""
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)
        if self._segments_dir.exists():
            shutil.rmtree(self._segments_dir, ignore_errors=True)

    async def _async_started(self, _hass: HomeAssistant) -> None:
        """Discover cameras and start the sweep loop."""
        await self.async_discover()
        await self.async_reconcile()

        minutes = max(
            1, int(self.options.get(CONF_SWEEP_MINUTES, DEFAULT_SWEEP_MINUTES))
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_interval_sweep, timedelta(minutes=minutes)
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_interval_purge, timedelta(hours=6)
            )
        )

        # Backfill today and yesterday so the card has something immediately.
        today = dt_util.now().date()
        self.entry.async_create_background_task(
            self.hass,
            self.async_sweep(days=[today, today - timedelta(days=1)]),
            f"{STORAGE_DIR_NAME}_initial_sweep",
        )

    async def async_unload(self) -> None:
        """Cancel everything this coordinator started."""
        self._shutdown = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_detection:
            self._unsub_detection()
            self._unsub_detection = None
        for cancel in self._pending_sweeps.values():
            cancel()
        self._pending_sweeps.clear()
        await self._store.async_save({"clips": self._index})
        await self.hass.async_add_executor_job(
            shutil.rmtree, self._segments_dir, True
        )

    # ── Discovery ──────────────────────────────────────────────────────

    async def async_discover(self) -> None:
        """Map Reolink detection sensors onto media source cameras.

        The media source exposes each camera as ``CAM|{entry_id}|{channel}``
        titled with the device name, and Reolink entity unique ids are
        ``{mac}_{channel}_{key}``. Joining on (entry, channel) gives an exact
        mapping with no hardcoded names; device names are the fallback for
        devices addressed by UID rather than by channel number.
        """
        try:
            root = await async_browse_media(self.hass, REOLINK_MEDIA_PREFIX)
        except Exception as err:  # noqa: BLE001 - media source raises many types
            _LOGGER.error("Could not browse the Reolink media source: %s", err)
            return

        cameras: dict[str, CameraInfo] = {}
        by_channel: dict[tuple[str, str], CameraInfo] = {}
        by_name: dict[str, CameraInfo] = {}

        for child in root.children or []:
            parts = bare_identifier(child.media_content_id).split("|")
            if len(parts) != 3 or parts[0] != "CAM":
                continue
            _, entry_id, channel = parts
            info = CameraInfo(
                key=slugify(child.title),
                name=child.title,
                entry_id=entry_id,
                channel=channel,
                media_content_id=child.media_content_id,
            )
            cameras[info.key] = info
            by_channel[(entry_id, channel)] = info
            by_name[child.title.lower()] = info

        if not cameras:
            _LOGGER.warning(
                "No Reolink cameras with playback support found in the media source"
            )

        cacheable = {normalise_trigger(item) for item in CACHEABLE_TRIGGERS}
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        for entry in self.hass.config_entries.async_entries(REOLINK_DOMAIN):
            for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
                if entity.disabled or entity.domain != "binary_sensor":
                    continue

                parts = entity.unique_id.split("_")
                if len(parts) < 3:
                    continue
                channel = parts[1]
                trigger = normalise_trigger("_".join(parts[2:]))
                if trigger not in cacheable:
                    continue

                camera = by_channel.get((entry.entry_id, channel))
                if camera is None and entity.device_id:
                    device = dev_reg.async_get(entity.device_id)
                    if device:
                        name = (device.name_by_user or device.name or "").lower()
                        camera = by_name.get(name)
                if camera is None:
                    continue

                camera.sensors[trigger] = entity.entity_id

        if selected := self.selected_cameras:
            missing = [key for key in selected if key not in cameras]
            if missing:
                _LOGGER.warning(
                    "Configured camera(s) %s were not found in the media source; "
                    "available cameras are %s",
                    ", ".join(missing),
                    ", ".join(sorted(cameras)) or "none",
                )
            cameras = {key: cam for key, cam in cameras.items() if key in selected}

        self._cameras = cameras
        self._restart_listeners()

        _LOGGER.info(
            "Discovered %d Reolink camera(s): %s",
            len(cameras),
            "; ".join(
                f"{camera.name} [{', '.join(sorted(camera.sensors)) or 'no detection sensors'}]"
                for camera in cameras.values()
            )
            or "none",
        )

    # ── Detection listeners ────────────────────────────────────────────

    def _restart_listeners(self) -> None:
        """Subscribe to every discovered detection sensor.

        Discovery can run again at any time, so the previous subscription is
        always dropped first rather than stacked on top of.
        """
        if self._unsub_detection:
            self._unsub_detection()
            self._unsub_detection = None

        entities: dict[str, str] = {}
        wanted = set(self.event_types)
        for camera in self._cameras.values():
            for trigger, entity_id in camera.sensors.items():
                if trigger in wanted:
                    entities[entity_id] = camera.key

        if not entities:
            return

        @callback
        def _on_detection(event: Event) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if new_state is None or old_state is None:
                return
            # The recording is only written once the event ends, so react to
            # the sensor clearing rather than to it triggering.
            if not (old_state.state == "on" and new_state.state == "off"):
                return
            if camera_key := entities.get(event.data["entity_id"]):
                self._schedule_event_sweep(camera_key)

        self._unsub_detection = async_track_state_change_event(
            self.hass, list(entities), _on_detection
        )
        _LOGGER.debug("Watching %d detection sensor(s)", len(entities))

    @callback
    def _schedule_event_sweep(self, camera_key: str) -> None:
        """Sweep a camera shortly after one of its events finished."""
        if cancel := self._pending_sweeps.pop(camera_key, None):
            cancel()

        async def _run(_now: datetime) -> None:
            self._pending_sweeps.pop(camera_key, None)
            await self.async_sweep(camera_key=camera_key)

        self._pending_sweeps[camera_key] = async_call_later(
            self.hass, EVENT_SETTLE_DELAY, _run
        )

    # ── Sweeping ───────────────────────────────────────────────────────

    async def _async_interval_sweep(self, _now: datetime) -> None:
        """Periodic sweep of every camera for today."""
        await self.async_sweep()

    async def _async_interval_purge(self, _now: datetime) -> None:
        """Periodic retention enforcement."""
        await self.async_purge()

    async def async_sweep(
        self,
        camera_key: str | None = None,
        days: list[dt_date] | None = None,
        force: bool = False,
    ) -> int:
        """Cache every event clip not already held locally.

        ``force`` (the sweep_now service) ignores a camera's back off and
        retries clips that failed before. Returns the number of clips newly
        cached.
        """
        if self._shutdown:
            return 0
        if not self._cameras:
            await self.async_discover()

        targets = (
            [self._cameras[camera_key]]
            if camera_key and camera_key in self._cameras
            else list(self._cameras.values())
        )
        if days is None:
            days = [dt_util.now().date()]

        cached = 0
        async with self._sweep_lock:
            for camera in targets:
                for day in days:
                    cached += await self._async_sweep_camera(camera, day, force)

        if cached:
            self._schedule_save()
            async_dispatcher_send(self.hass, SIGNAL_INDEX_UPDATED)
        return cached

    async def _async_sweep_camera(
        self, camera: CameraInfo, day: dt_date, force: bool = False
    ) -> int:
        """Diff one camera-day against the index and cache what is missing."""
        now = dt_util.utcnow()
        marker = f"{camera.key}|{day.isoformat()}"
        if not force:
            if (last := self._last_sweep.get(marker)) and now - last < SWEEP_COOLDOWN:
                return 0
            paused = self._backoff.get(camera.key)
            if paused and now < paused[0]:
                _LOGGER.debug(
                    "Sweep: %s is paused after failed downloads until %s",
                    camera.name,
                    paused[0],
                )
                return 0
        self._last_sweep[marker] = now

        try:
            descriptors = await self.async_list_day(camera, day)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Sweep of %s for %s failed: %s", camera.name, day, err)
            return 0

        pending = [
            descriptor
            for descriptor in descriptors
            if not self._is_cached(descriptor["clip_id"])
            and (
                force
                or self._clip_failures.get(descriptor["clip_id"], 0) < CLIP_FAILURE_LIMIT
            )
        ]
        if not pending:
            return 0

        batch = pending[:MAX_CLIPS_PER_SWEEP]
        _LOGGER.debug(
            "Sweep: %s %s - caching %d of %d missing clip(s)",
            camera.name,
            day,
            len(batch),
            len(pending),
        )
        cached = 0
        consecutive_failures = 0
        for index, descriptor in enumerate(batch):
            if self._shutdown:
                break
            if index and DOWNLOAD_SPACING:
                await asyncio.sleep(DOWNLOAD_SPACING)
            try:
                succeeded = await self._async_cache_clip(descriptor)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error caching a clip")
                succeeded = False

            if succeeded:
                cached += 1
                consecutive_failures = 0
                self._backoff.pop(camera.key, None)
                continue

            consecutive_failures += 1
            # With retries and backoff, grinding through a whole batch against
            # an NVR that is refusing everything would tie up the sweep for
            # many minutes. Give up early and try again next time.
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                level = self._backoff.get(camera.key, (now, 0))[1]
                minutes = CAMERA_BACKOFF_MINUTES[min(level, len(CAMERA_BACKOFF_MINUTES) - 1)]
                self._backoff[camera.key] = (
                    dt_util.utcnow() + timedelta(minutes=minutes),
                    level + 1,
                )
                _LOGGER.warning(
                    "%s: %d clips failed in a row, so downloads from this camera "
                    "pause for %d minutes. Run the reolink_clip_cache.diagnose "
                    "action to see what the NVR is doing",
                    camera.name,
                    consecutive_failures,
                    minutes,
                )
                break
        return cached

    async def async_list_day(
        self,
        camera: CameraInfo,
        day: dt_date,
        event_types: set[str] | None = None,
        folders_seen: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return descriptors for every wanted clip on one camera-day.

        Browses the media source only - that is the fast Reolink call. It is
        *resolving* a clip for playback that is slow, which is exactly what
        this cache exists to avoid.

        Pass ``event_types`` to override the configured filter, or an empty set
        to accept everything; ``folders_seen`` collects the trigger folders the
        day actually has, which diagnostics use to explain an empty result.
        """
        day_id = media_uri(
            f"DAY|{camera.entry_id}|{camera.channel}|{self.stream}"
            f"|{day.year}|{day.month}|{day.day}"
        )

        try:
            day_result = await async_browse_media(self.hass, day_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("No recordings for %s on %s (%s)", camera.name, day, err)
            return []

        wanted = set(self.event_types) if event_types is None else event_types
        descriptors: dict[str, dict[str, Any]] = {}

        # NVRs expose per-trigger subfolders; hubs and standalone cameras list
        # the day's files directly and carry the trigger in the title.
        event_folders = [
            child
            for child in day_result.children or []
            if bare_identifier(child.media_content_id).startswith("EVE|")
        ]

        if event_folders:
            for folder in event_folders:
                trigger = normalise_trigger(
                    bare_identifier(folder.media_content_id).split("|")[-1]
                )
                if folders_seen is not None:
                    folders_seen.append(trigger)
                if wanted and trigger not in wanted:
                    continue
                try:
                    folder_result = await async_browse_media(
                        self.hass, folder.media_content_id
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Could not browse %s: %s", folder.title, err)
                    continue
                for child in folder_result.children or []:
                    self._collect(descriptors, camera, child, day, trigger)
        else:
            for child in day_result.children or []:
                self._collect(descriptors, camera, child, day, None, wanted or None)

        return sorted(
            descriptors.values(), key=lambda item: item["start"], reverse=True
        )

    def _collect(
        self,
        descriptors: dict[str, dict[str, Any]],
        camera: CameraInfo,
        child: Any,
        day: dt_date,
        trigger: str | None,
        wanted: set[str] | None = None,
    ) -> None:
        """Turn one browse child into a clip descriptor."""
        descriptor = self._descriptor_for(camera, child, day, trigger)
        if descriptor is None:
            return
        if wanted is not None and not set(descriptor["event_types"]) & wanted:
            return

        if existing := descriptors.get(descriptor["clip_id"]):
            # The same recording can appear under several trigger folders.
            merged = sorted(
                set(existing["event_types"]) | set(descriptor["event_types"])
            )
            existing["event_types"] = merged
        else:
            descriptors[descriptor["clip_id"]] = descriptor

    def _descriptor_for(
        self,
        camera: CameraInfo,
        child: Any,
        day: dt_date,
        trigger: str | None,
    ) -> dict[str, Any] | None:
        """Parse a ``FILE|`` browse child into a clip descriptor.

        The identifier carries the recording's real start and end time, which
        is what lets a cached clip be matched back to an NVR event exactly
        instead of guessing from when the download happened to run.
        """
        media_content_id = child.media_content_id or ""
        parts = bare_identifier(media_content_id).split("|", 6)
        if len(parts) != 7 or parts[0] != "FILE":
            return None

        filename, start_id, end_id = parts[4], parts[5], parts[6]
        start = _parse_reolink_time(start_id)
        if start is None:
            return None
        end = _parse_reolink_time(end_id)

        title = child.title or ""
        triggers = self._triggers_from_title(title)
        if trigger:
            triggers.add(trigger)
        if not triggers:
            triggers = {"other"}

        ordered = sorted(triggers)
        return {
            "clip_id": clip_id_for(media_content_id),
            "media_content_id": media_content_id,
            "camera": camera.key,
            "camera_name": camera.name,
            "channel": camera.channel,
            "event_type": trigger or ordered[0],
            "event_types": ordered,
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "duration": int((end - start).total_seconds()) if end else None,
            "date": day.isoformat(),
            "filename": filename,
            "title": title,
        }

    @staticmethod
    def _triggers_from_title(title: str) -> set[str]:
        """Extract trigger names the media source appends to a file title."""
        lowered = title.lower()
        return {
            normalise_trigger(candidate)
            for candidate in [*CACHEABLE_TRIGGERS, "motion", "other"]
            if candidate in lowered
        }

    # ── Caching a clip ─────────────────────────────────────────────────

    def _is_cached(self, clip_id: str) -> bool:
        """Return True if the clip is already held locally."""
        record = self._index.get(clip_id)
        return bool(record and record.get("cached_at"))

    async def _async_cache_clip(self, descriptor: dict[str, Any]) -> bool:
        """Download, faststart and thumbnail one clip.

        Returns True when the clip was newly cached.
        """
        clip_id = descriptor["clip_id"]
        if clip_id in self._in_flight:
            return False
        self._in_flight.add(clip_id)

        try:
            async with self._semaphore:
                if self._shutdown or self._is_cached(clip_id):
                    return False

                clip_path = self.clip_path(clip_id)
                part_path = clip_path.with_suffix(".part")

                if not await self._async_download_with_retries(descriptor, part_path):
                    await self.hass.async_add_executor_job(_unlink, part_path)
                    self._clip_failures[clip_id] = self._clip_failures.get(clip_id, 0) + 1
                    return False
                self._clip_failures.pop(clip_id, None)

                if not await self._async_faststart(part_path, clip_path):
                    _LOGGER.warning(
                        "Could not store the %s clip from %s: %s",
                        descriptor.get("camera_name") or descriptor["camera"],
                        descriptor.get("start") or "",
                        self._last_error,
                    )
                    await self.hass.async_add_executor_job(_unlink, part_path)
                    self._clip_failures[clip_id] = self._clip_failures.get(clip_id, 0) + 1
                    return False
                thumb_ok = await self._async_thumbnail(
                    clip_path, self.thumb_path(clip_id)
                )
                size = await self.hass.async_add_executor_job(_size_of, clip_path)

                self._index[clip_id] = {
                    **descriptor,
                    "size": size,
                    "has_thumbnail": thumb_ok,
                    "cached_at": dt_util.utcnow().isoformat(),
                }

                self.hass.bus.async_fire(
                    EVENT_CLIP_CACHED,
                    {
                        "clip_id": clip_id,
                        "camera": descriptor["camera"],
                        "camera_name": descriptor["camera_name"],
                        "event_type": descriptor["event_type"],
                        "start": descriptor["start"],
                    },
                )
                _LOGGER.debug(
                    "Cached %s %s at %s (%d KiB)",
                    descriptor["camera_name"],
                    descriptor["event_type"],
                    descriptor["start"],
                    size // 1024,
                )
                return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to cache clip %s", descriptor.get("title"))
            return False
        finally:
            self._in_flight.discard(clip_id)

    async def _async_source_url(self, media_content_id: str) -> str | None:
        """Resolve a media source item to a URL this process can fetch."""
        try:
            media = await async_resolve_media(self.hass, media_content_id, None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not resolve %s: %s", media_content_id, err)
            return None

        url = media.url
        if url.startswith(("http://", "https://")):
            return url
        # Reolink hands back a relative, auth-protected proxy path. Sign it so
        # this background task can fetch it over HA's own HTTP server.
        signed = async_sign_path(
            self.hass, url, timedelta(seconds=DOWNLOAD_TIMEOUT + 60)
        )
        full = f"{self._internal_base_url()}{signed}"
        _LOGGER.debug("Fetching clip via the Reolink proxy: %s", full)
        return full

    def _internal_base_url(self) -> str:
        """Return a loopback base URL for HA's own HTTP server."""
        if self._base_url is None:
            scheme = "https" if self.hass.http.ssl_certificate else "http"
            self._base_url = f"{scheme}://127.0.0.1:{self.hass.http.server_port}"
        return self._base_url

    def _route_order(self, descriptor: dict[str, Any]) -> list[str]:
        """Return the download routes to try for a camera, best first.

        LIBRARY asks reolink-aio to download the clip the way the Reolink
        integration's own code does: it prepares the recording on an NVR with
        NvrDownload, sends the request through the shared, logged in session
        (renewing an expired login rather than failing with 401) and queues
        behind Home Assistant's other NVR requests. Then the NVR's own URLs,
        then Home Assistant's playback proxy. The route that last worked for a
        camera goes first.
        """
        nvr_download = [r for r in DIRECT_ROUTES if split_route(r)[0] == "NVR_DOWNLOAD"]
        others = [r for r in DIRECT_ROUTES if r not in nvr_download]
        routes = ["LIBRARY", NATIVE_FLV, *nvr_download, NATIVE_DOWNLOAD, *others, "PROXY"]
        if (known := self._vod_types.get(self._camera_marker(descriptor))) in routes:
            routes.remove(known)
            routes.insert(0, known)
        return routes

    @staticmethod
    def _route_label(route: str) -> str:
        """Describe a route in log messages."""
        if route == "LIBRARY":
            return "the Reolink library"
        if route == "PROXY":
            return "the Home Assistant proxy"
        if route == NATIVE_FLV:
            return "the NVR directly (FLV by native file name)"
        if route == NATIVE_DOWNLOAD:
            return "the NVR directly (Download by native file name)"
        request_type, plain_http = split_route(route)
        return f"the NVR directly ({request_type}{' over HTTP' if plain_http else ''})"

    def _host_api(self, descriptor: dict[str, Any]) -> Any | None:
        """Return the Reolink integration's API object for a clip, if loaded."""
        parts = bare_identifier(descriptor["media_content_id"]).split("|", 6)
        if len(parts) != 7 or parts[0] != "FILE":
            return None
        try:
            from homeassistant.components.reolink.util import get_host

            return get_host(self.hass, parts[1]).api
        except Exception:  # noqa: BLE001
            return None

    async def _async_library_download(
        self, descriptor: dict[str, Any], dest: Path, max_bytes: int | None = None
    ) -> int:
        """Download a clip through reolink-aio's own download_vod.

        ``max_bytes`` stops early (used by diagnose). Returns bytes written.
        """
        parts = bare_identifier(descriptor["media_content_id"]).split("|", 6)
        if len(parts) != 7 or parts[0] != "FILE":
            self._last_error = "not a Reolink recording id"
            return 0
        _, _entry_id, channel, stream, filename, start_id, end_id = parts
        api = self._host_api(descriptor)
        if api is None:
            self._last_error = "the Reolink integration is not loaded"
            return 0

        vod = None
        written = 0
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                vod = await api.download_vod(
                    filename,
                    wanted_filename=f"clip_{start_id}.mp4",
                    start_time=start_id,
                    end_time=end_id,
                    channel=int(channel),
                    stream=stream,
                )
                handle = await self.hass.async_add_executor_job(_open_write, dest)
                try:
                    async for chunk in vod.stream.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                        await self.hass.async_add_executor_job(handle.write, chunk)
                        written += len(chunk)
                        if max_bytes and written >= max_bytes:
                            break
                finally:
                    await self.hass.async_add_executor_job(handle.close)
        except TimeoutError:
            self._last_error = f"timed out after {DOWNLOAD_TIMEOUT}s"
            return 0
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any library error means try the next route
            self._last_error = f"{type(err).__name__}: {err}"
            return 0
        finally:
            if vod is not None:
                try:
                    vod.close()
                except Exception:  # noqa: BLE001
                    pass
        if not written:
            self._last_error = "the NVR sent an empty file"
        return written

    async def _async_try_route(
        self, route: str, descriptor: dict[str, Any], dest: Path
    ) -> int:
        """Try one route once. Returns bytes written; sets _last_error on failure."""
        self._last_error = ""
        self._last_status = None
        if route == "LIBRARY":
            return await self._async_library_download(descriptor, dest)
        if route == "PROXY":
            source = await self._async_source_url(descriptor["media_content_id"])
            if source is None:
                self._last_error = "the clip could not be resolved"
                return 0
            return await self._async_download(source, dest, label=self._route_label(route))

        native: dict[str, Any] | None = None
        if route in NATIVE_ROUTES:
            native = await self._async_native_file(descriptor)
            if native is None:
                return 0
            if route == NATIVE_DOWNLOAD:
                return await self._async_segment_clip(descriptor, native, dest)
            request_type, plain_http = "FLV", False
        else:
            request_type, plain_http = split_route(route)
        name = native["name"] if native else None
        url = await self._async_direct_source(descriptor, request_type, plain_http, name)
        if url is None:
            self._last_error = "not offered for this recording"
            return 0
        if request_type == "FLV":
            if native:
                url = with_seek(url, native["offset"])
            return await self._async_flv_download(url, descriptor, dest)
        written = await self._async_download(
            url, dest, headers=DIRECT_HEADERS, label=self._route_label(route)
        )
        if not written and self._last_status == 401:
            # The login token in the URL went stale (Home Assistant renews its
            # session now and then). Renew it and ask once more.
            if (api := self._host_api(descriptor)) is not None:
                try:
                    await api.expire_session(unsubscribe=False)
                except Exception:  # noqa: BLE001
                    pass
                if url := await self._async_direct_source(
                    descriptor, request_type, plain_http, name
                ):
                    await self.hass.async_add_executor_job(_unlink, dest)
                    written = await self._async_download(
                        url, dest, headers=DIRECT_HEADERS, label=self._route_label(route)
                    )
        return written

    async def _async_segment_clip(
        self, descriptor: dict[str, Any], native: dict[str, Any], dest: Path
    ) -> int:
        """Cut a clip out of its native recording segment, fetched once.

        Returns the clip's size in bytes; sets _last_error on failure.
        """
        if not (binary := self._ffmpeg_binary()):
            self._last_error = "ffmpeg is needed to cut a clip out of the NVR's recording"
            return 0
        segment = await self._async_segment(descriptor, native)
        if segment is None:
            return 0
        seconds = self._clip_seconds(descriptor)
        ok = await self._run_ffmpeg(
            binary,
            "-y",
            "-ss",
            str(native["offset"]),
            "-i",
            str(segment),
            *(["-t", str(seconds)] if seconds else []),
            "-c",
            "copy",
            "-f",
            "mp4",
            str(dest),
        )
        size = await self.hass.async_add_executor_job(_size_of, dest) if ok else 0
        if not size:
            await self.hass.async_add_executor_job(_unlink, dest)
            self._last_error = "ffmpeg could not cut the clip out of the recording"
        return size

    async def _async_segment(
        self, descriptor: dict[str, Any], native: dict[str, Any]
    ) -> Path | None:
        """Return the local copy of a clip's native segment, fetching it if needed.

        A copy is reused for the segment's other clips, unless it was fetched
        before this clip ended (the hour may still have been recording).
        """
        parts = bare_identifier(descriptor["media_content_id"]).split("|", 6)
        entry_id, channel, stream, end_id = parts[1], parts[2], parts[3], parts[6]
        key = f"{entry_id}|{channel}|{stream}|{native['name']}"
        clip_end = _parse_reolink_time(end_id)
        held = self._segments.get(key)
        if (
            held
            and clip_end is not None
            and held["fetched"] >= clip_end + timedelta(seconds=SEGMENT_SETTLE_SECONDS)
            and await self.hass.async_add_executor_job(_size_of, held["path"])
        ):
            return held["path"]

        await self._async_prune_segments()
        path = self._segments_dir / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.mp4"
        part = path.with_suffix(".part")
        label = self._route_label(NATIVE_DOWNLOAD)
        fetched = dt_util.now()
        written = 0
        for attempt in range(2):
            url = await self._async_direct_source(descriptor, "DOWNLOAD", False, native["name"])
            if url is None:
                self._last_error = "not offered for this recording"
                return None
            self._last_status = None
            written = await self._async_download(
                url, part, headers=DIRECT_HEADERS, label=label, timeout=NATIVE_DOWNLOAD_TIMEOUT
            )
            if written or self._last_status != 401 or attempt:
                break
            # A stale login token: renew it and ask once more.
            if (api := self._host_api(descriptor)) is not None:
                try:
                    await api.expire_session(unsubscribe=False)
                except Exception:  # noqa: BLE001
                    pass
        if not written:
            await self.hass.async_add_executor_job(_unlink, part)
            return None
        await self.hass.async_add_executor_job(part.replace, path)
        self._segments[key] = {"path": path, "fetched": fetched}
        _LOGGER.debug("Fetched recording segment %s (%d MiB)", native["name"], written >> 20)
        return path

    async def _async_prune_segments(self) -> None:
        """Delete old segments, and all but the newest few."""
        cutoff = dt_util.now() - timedelta(minutes=SEGMENT_KEEP_MINUTES)
        newest = sorted(self._segments, key=lambda k: self._segments[k]["fetched"], reverse=True)
        for index, key in enumerate(newest):
            held = self._segments[key]
            if index >= SEGMENTS_KEPT - 1 or held["fetched"] < cutoff:
                await self.hass.async_add_executor_job(_unlink, held["path"])
                del self._segments[key]

    async def _async_native_file(
        self, descriptor: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Find the NVR's native recording segment that holds a clip.

        Returns the segment's name and the clip's offset into it in seconds,
        or None (with _last_error set) when the NVR's Search does not name one.
        """
        parts = bare_identifier(descriptor["media_content_id"]).split("|", 6)
        if len(parts) != 7 or parts[0] != "FILE":
            self._last_error = "not a Reolink recording id"
            return None
        _, entry_id, channel, stream, _filename, start_id, end_id = parts
        key = (entry_id, channel, stream, start_id)
        if key in self._native_files:
            return self._native_files[key]

        api = self._host_api(descriptor)
        start, end = _parse_reolink_time(start_id), _parse_reolink_time(end_id)
        if api is None or start is None or end is None:
            self._last_error = "the Reolink integration is not loaded"
            return None
        try:
            _statuses, files = await api.request_vod_files(
                int(channel), start, end, stream=stream
            )
        except Exception as err:  # noqa: BLE001 - any library error means no native name
            self._last_error = redact(f"Search failed: {type(err).__name__}: {err}")
            return None

        native = pick_native_file(
            [getattr(file, "data", {}) for file in files], start_id
        )
        if native is None:
            self._last_error = (
                f"the NVR's Search listed {len(files)} file(s) and none with a "
                f"name covering {start_id}"
            )
            return None
        if len(self._native_files) > 256:
            self._native_files.clear()
        self._native_files[key] = native
        return native

    async def _async_flv_download(
        self, url: str, descriptor: dict[str, Any], dest: Path
    ) -> int:
        """Record a clip from the NVR's FLV playback stream.

        The stream is read for the clip's length plus a grace period and kept
        only if it really is FLV; _async_faststart then remuxes it to MP4.
        """
        seconds = self._clip_seconds(descriptor)
        listen = (seconds or FLV_FALLBACK_SECONDS) + FLV_GRACE_SECONDS
        written = await self._async_download(
            url,
            dest,
            headers=DIRECT_HEADERS,
            label=self._route_label("FLV"),
            max_seconds=listen,
        )
        if not written:
            return 0
        head = await self.hass.async_add_executor_job(_read_head, dest, 64)
        if not head.startswith(FLV_MAGIC):
            self._last_error = (
                "the NVR answered with something other than FLV: "
                f"{redact(head.decode('utf-8', 'replace')).strip()[:80]!r}"
            )
            return 0
        return written

    @staticmethod
    def _clip_seconds(descriptor: dict[str, Any]) -> int | None:
        """Return a clip's length from the start and end in its id."""
        parts = bare_identifier(descriptor["media_content_id"]).split("|", 6)
        if len(parts) != 7:
            return None
        start = _parse_reolink_time(parts[5])
        end = _parse_reolink_time(parts[6])
        if start is None or end is None or end <= start:
            return None
        return int((end - start).total_seconds())

    @staticmethod
    def _camera_marker(descriptor: dict[str, Any]) -> str:
        """Return a per-camera key for remembering what works."""
        return f"{descriptor.get('camera')}"

    async def _async_direct_source(
        self,
        descriptor: dict[str, Any],
        request_type_name: str = "DOWNLOAD",
        plain_http: bool = False,
        file_name: str | None = None,
    ) -> str | None:
        """Ask the Reolink integration for the NVR's own URL for a clip.

        This mirrors what Home Assistant's playback proxy does internally.
        Returns None whenever the Reolink integration is not importable, the
        request type does not apply, or anything else does not line up - the
        proxy remains as the fallback.
        """
        parts = bare_identifier(descriptor["media_content_id"]).split("|", 6)
        if len(parts) != 7 or parts[0] != "FILE":
            return None
        _, entry_id, channel, stream, filename, start_id, end_id = parts
        if file_name:
            filename = file_name

        try:
            from homeassistant.components.reolink.util import get_host
            from reolink_aio.enums import VodRequestType

            api = get_host(self.hass, entry_id).api
            request_type = VodRequestType[request_type_name]

            if request_type is VodRequestType.NVR_DOWNLOAD:
                if not api.is_nvr:
                    return None
                # This form asks the NVR to prepare the recording first.
                filename = f"{start_id}_{end_id}"

            _mime, url = await api.get_vod_source(
                int(channel), filename, stream, request_type
            )
        except Exception as err:  # noqa: BLE001 - must never break caching
            _LOGGER.debug(
                "No direct NVR URL via %s (%s): %s",
                request_type_name,
                type(err).__name__,
                err,
            )
            return None

        if not url.startswith(("http://", "https://")):
            return None
        if plain_http:
            return plain_http_url(url, getattr(api, "_netport_settings", None))
        return url

    async def _async_download_with_retries(
        self, descriptor: dict[str, Any], dest: Path
    ) -> int:
        """Fetch a clip, trying each route in turn, and remember what worked.

        Each failed route is logged at debug level; a clip that cannot be
        fetched at all gets one warning listing what every route said.
        """
        marker = self._camera_marker(descriptor)
        reasons: dict[str, str] = {}
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            for route in self._route_order(descriptor):
                written = await self._async_try_route(route, descriptor, dest)
                if written:
                    if self._vod_types.get(marker) != route:
                        _LOGGER.info(
                            "Downloading clips for %s through %s",
                            descriptor.get("camera_name") or marker,
                            self._route_label(route),
                        )
                        self._vod_types[marker] = route
                    return written
                reasons[route] = self._last_error or "failed"
                _LOGGER.debug(
                    "Clip download through %s failed: %s",
                    self._route_label(route),
                    reasons[route],
                )
                await self.hass.async_add_executor_job(_unlink, dest)

            if attempt < DOWNLOAD_ATTEMPTS:
                delay = DOWNLOAD_RETRY_BACKOFF[
                    min(attempt - 1, len(DOWNLOAD_RETRY_BACKOFF) - 1)
                ]
                await asyncio.sleep(delay)

        _LOGGER.warning(
            "Could not download the %s %s clip from %s. %s",
            descriptor.get("camera_name") or marker,
            descriptor.get("event_type") or "event",
            descriptor.get("start") or "",
            "; ".join(f"{self._route_label(r)}: {msg}" for r, msg in reasons.items()),
        )
        return 0

    async def _async_download(
        self,
        url: str,
        dest: Path,
        headers: dict[str, str] | None = None,
        label: str = "the Home Assistant proxy",
        max_seconds: float | None = None,
        timeout: float | None = None,
    ) -> int:
        """Stream a URL to disk. Returns the number of bytes written.

        ``max_seconds`` is for live streams such as FLV playback, which need
        not end when the recording does: reading stops at that point, and a
        stream the NVR cuts off still keeps what arrived.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        written = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds if max_seconds else None
        limit = max(timeout or DOWNLOAD_TIMEOUT, (max_seconds or 0) + 30)
        try:
            async with asyncio.timeout(limit):
                async with session.get(
                    url, headers=headers or DOWNLOAD_HEADERS
                ) as response:
                    if response.status not in OK_STATUSES:
                        # The Reolink proxy explains itself in the body; without
                        # this the failure is just a bare status code.
                        try:
                            detail = (await response.text())[:400].strip()
                        except Exception:  # noqa: BLE001
                            detail = "<unreadable body>"
                        self._last_status = response.status
                        self._last_error = redact(
                            f"HTTP {response.status} ({response.content_type})"
                            f"{f' {detail}' if detail else ''}"
                        )
                        return 0
                    handle = await self.hass.async_add_executor_job(_open_write, dest)
                    try:
                        async for chunk in response.content.iter_chunked(
                            DOWNLOAD_CHUNK_SIZE
                        ):
                            await self.hass.async_add_executor_job(handle.write, chunk)
                            written += len(chunk)
                            if deadline is not None and loop.time() >= deadline:
                                break
                    finally:
                        await self.hass.async_add_executor_job(handle.close)
        except TimeoutError:
            self._last_error = f"timed out after {limit:.0f}s"
            return 0
        except ClientError as err:
            if deadline is not None and written:
                # A playback stream that the NVR closes early is still a clip.
                return written
            # Naming the exception type matters: a dropped connection, a
            # refused one and a bad response all read alike without it.
            self._last_error = redact(f"{type(err).__name__}: {err}")
            return 0
        if not written:
            self._last_error = "empty response"
        return written

    async def _async_faststart(self, src: Path, dest: Path) -> bool:
        """Move the MP4 index to the front so playback can start immediately.

        Without this the browser has to fetch the whole file before the first
        frame, which is a large part of the delay this integration removes.
        An FLV recording is remuxed into MP4 the same way; it is useless to a
        browser as it is, so it fails (returns False) if that cannot be done.
        """
        head = await self.hass.async_add_executor_job(_read_head, src, 3)
        is_flv = head.startswith(FLV_MAGIC)

        if binary := self._ffmpeg_binary():
            base = ["-y", *(["-f", "flv"] if is_flv else []), "-i", str(src)]
            tail = ["-movflags", "+faststart", "-f", "mp4", str(dest)]
            ok = await self._run_ffmpeg(binary, *base, "-c", "copy", *tail)
            if not ok and is_flv:
                # Some cameras put audio in their FLV that MP4 will not take
                # as is. The picture is what matters here.
                ok = await self._run_ffmpeg(binary, *base, "-c:v", "copy", "-an", *tail)
            if ok:
                await self.hass.async_add_executor_job(_unlink, src)
                return True
            _LOGGER.debug("Faststart failed for %s, storing as downloaded", src.name)

        if is_flv:
            await self.hass.async_add_executor_job(_unlink, dest)
            self._last_error = "ffmpeg could not turn the FLV recording into MP4"
            return False
        await self.hass.async_add_executor_job(shutil.move, str(src), str(dest))
        return True

    async def _async_thumbnail(self, src: Path, dest: Path) -> bool:
        """Grab a poster frame for the filmstrip."""
        if not (binary := self._ffmpeg_binary()):
            return False
        return await self._run_ffmpeg(
            binary,
            "-y",
            "-ss",
            "1",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-2",
            "-q:v",
            "4",
            str(dest),
        )

    def _ffmpeg_binary(self) -> str | None:
        """Return the ffmpeg binary HA is configured to use."""
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager

            return get_ffmpeg_manager(self.hass).binary
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ffmpeg is unavailable; clips will not be optimised")
            return None

    async def _run_ffmpeg(self, binary: str, *args: str) -> bool:
        """Run ffmpeg and report whether it succeeded."""
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as err:
            _LOGGER.debug("Could not run ffmpeg: %s", err)
            return False

        try:
            async with asyncio.timeout(FFMPEG_TIMEOUT):
                _, stderr = await proc.communicate()
        except TimeoutError:
            proc.kill()
            _LOGGER.warning("ffmpeg timed out")
            return False

        if proc.returncode != 0:
            _LOGGER.debug("ffmpeg failed: %s", stderr.decode()[-300:])
            return False
        return True

    # ── Paths ──────────────────────────────────────────────────────────

    def clip_path(self, clip_id: str) -> Path:
        """Return the on-disk path for a cached clip."""
        return self._clips_dir / f"{clip_id}.mp4"

    def thumb_path(self, clip_id: str) -> Path:
        """Return the on-disk path for a cached thumbnail."""
        return self._thumbs_dir / f"{clip_id}.jpg"

    # ── Index persistence ──────────────────────────────────────────────

    async def _async_load_index(self) -> None:
        """Load the clip index, starting empty if the schema changed."""
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not read the clip index, starting empty")
            data = None
        self._index = data.get("clips", {}) if isinstance(data, dict) else {}
        _LOGGER.debug("Loaded %d cached clip(s)", len(self._index))

    @callback
    def _schedule_save(self) -> None:
        """Persist the index, coalescing a sweep's writes into one."""
        self._store.async_delay_save(lambda: {"clips": self._index}, SAVE_DELAY)

    async def async_reconcile(self) -> None:
        """Reconcile the index against what is actually on disk."""
        clip_ids = set(self._index)
        on_disk = await self.hass.async_add_executor_job(
            _list_ids, self._clips_dir, ".mp4"
        )

        missing = {
            clip_id
            for clip_id in clip_ids
            if self._index[clip_id].get("cached_at") and clip_id not in on_disk
        }
        for clip_id in missing:
            self._index.pop(clip_id, None)

        orphans = on_disk - clip_ids
        if orphans:
            await self.hass.async_add_executor_job(
                _remove_ids, self._clips_dir, self._thumbs_dir, orphans
            )

        if missing or orphans:
            _LOGGER.info(
                "Reconciled cache: dropped %d stale entr(ies), removed %d orphan file(s)",
                len(missing),
                len(orphans),
            )
            self._schedule_save()
            async_dispatcher_send(self.hass, SIGNAL_INDEX_UPDATED)

    # ── Retention ──────────────────────────────────────────────────────

    async def async_purge(self) -> int:
        """Enforce the age and size limits. Returns the clips removed."""
        cutoff = dt_util.utcnow() - timedelta(days=self.cache_days)
        records = sorted(
            self._index.items(), key=lambda item: item[1].get("start") or ""
        )

        doomed: list[str] = []
        total = 0
        for clip_id, record in records:
            start = dt_util.parse_datetime(record.get("start") or "")
            if start is not None and start < cutoff:
                doomed.append(clip_id)
            else:
                total += int(record.get("size") or 0)

        # Then oldest-first until the cache fits inside its ceiling.
        ceiling = self.max_cache_bytes
        for clip_id, record in records:
            if total <= ceiling:
                break
            if clip_id in doomed:
                continue
            doomed.append(clip_id)
            total -= int(record.get("size") or 0)

        if not doomed:
            return 0

        for clip_id in doomed:
            self._index.pop(clip_id, None)
        await self.hass.async_add_executor_job(
            _remove_ids, self._clips_dir, self._thumbs_dir, set(doomed)
        )

        _LOGGER.info("Purged %d cached clip(s)", len(doomed))
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_INDEX_UPDATED)
        return len(doomed)

    # ── Read API (WebSocket / sensors) ─────────────────────────────────

    def camera_list(self) -> list[dict[str, Any]]:
        """Return the discovered cameras for the card."""
        return [camera.as_dict() for camera in self._cameras.values()]

    async def async_dates(self, camera_key: str) -> list[dict[str, Any]]:
        """Return the days that have recordings for a camera."""
        camera = self._cameras.get(camera_key)
        if camera is None:
            return []

        stream_id = media_uri(f"RES|{camera.entry_id}|{camera.channel}|{self.stream}")
        try:
            result = await async_browse_media(self.hass, stream_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not list days for %s: %s", camera.name, err)
            return []

        days: list[dict[str, Any]] = []
        for child in result.children or []:
            parts = bare_identifier(child.media_content_id).split("|")
            if len(parts) != 7 or parts[0] != "DAY":
                continue
            try:
                day = dt_date(int(parts[4]), int(parts[5]), int(parts[6]))
            except ValueError:
                continue
            days.append({"date": day.isoformat(), "title": child.title})

        days.sort(key=lambda item: item["date"], reverse=True)
        return days

    async def async_clips(
        self,
        camera_key: str,
        day: dt_date,
        refresh_token_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every clip for a camera-day, cached ones carrying local URLs."""
        camera = self._cameras.get(camera_key)
        if camera is None:
            return []

        descriptors = await self.async_list_day(camera, day)
        return [self._decorate(item, refresh_token_id) for item in descriptors]

    def _decorate(
        self, descriptor: dict[str, Any], refresh_token_id: str | None
    ) -> dict[str, Any]:
        """Attach cache state and signed local URLs to a descriptor."""
        clip_id = descriptor["clip_id"]
        record = self._index.get(clip_id)
        cached = bool(record and record.get("cached_at"))

        return {
            **descriptor,
            "cached": cached,
            "url": self.signed_clip_url(clip_id, refresh_token_id) if cached else None,
            "thumbnail": (
                self.signed_thumb_url(clip_id, refresh_token_id)
                if cached and record and record.get("has_thumbnail")
                else None
            ),
            "size": (record or {}).get("size"),
        }

    def signed_clip_url(self, clip_id: str, refresh_token_id: str | None) -> str:
        """Return a short-lived authenticated URL for a cached clip."""
        return async_sign_path(
            self.hass,
            f"{CLIP_URL}/{clip_id}",
            timedelta(seconds=SIGNED_URL_TTL),
            refresh_token_id=refresh_token_id,
        )

    def signed_thumb_url(self, clip_id: str, refresh_token_id: str | None) -> str:
        """Return a short-lived authenticated URL for a cached thumbnail."""
        return async_sign_path(
            self.hass,
            f"{THUMB_URL}/{clip_id}",
            timedelta(seconds=SIGNED_URL_TTL),
            refresh_token_id=refresh_token_id,
        )

    async def async_resolve(
        self,
        clip_id: str | None = None,
        media_content_id: str | None = None,
        refresh_token_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a clip for playback, preferring the local cache.

        An uncached clip still plays: the caller gets the NVR proxy URL right
        away while the clip is pulled into the cache in the background, so the
        next play of it is instant.
        """
        if clip_id is None and media_content_id:
            clip_id = clip_id_for(media_content_id)
        if clip_id is None:
            return {"cached": False, "url": None}

        record = self._index.get(clip_id)
        if record and record.get("cached_at"):
            return {
                "cached": True,
                "clip_id": clip_id,
                "url": self.signed_clip_url(clip_id, refresh_token_id),
                "thumbnail": (
                    self.signed_thumb_url(clip_id, refresh_token_id)
                    if record.get("has_thumbnail")
                    else None
                ),
            }

        if not media_content_id:
            return {"cached": False, "clip_id": clip_id, "url": None}

        # Warm the cache for next time without making the caller wait.
        self.entry.async_create_background_task(
            self.hass,
            self._async_cache_on_demand(media_content_id),
            f"{STORAGE_DIR_NAME}_on_demand_{clip_id}",
        )

        try:
            media = await async_resolve_media(self.hass, media_content_id, None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Fallback resolve failed for %s: %s", media_content_id, err)
            return {"cached": False, "clip_id": clip_id, "url": None}

        return {
            "cached": False,
            "clip_id": clip_id,
            "url": self._signed_media_url(media.url, refresh_token_id),
        }

    def _signed_media_url(self, url: str, refresh_token_id: str | None) -> str:
        """Sign a relative media source URL for the browser.

        The Reolink proxy view requires authentication, and a <video> element
        cannot send the bearer token, so a bare /api/reolink/video/... path is
        refused with 401. The media_source/resolve_media websocket command signs
        its URLs; resolving in Python does not, so sign it here the same way.
        """
        if url.startswith(("http://", "https://")):
            return url
        return async_sign_path(
            self.hass,
            url,
            timedelta(seconds=SIGNED_URL_TTL),
            refresh_token_id=refresh_token_id,
        )

    async def _async_cache_on_demand(self, media_content_id: str) -> None:
        """Cache a single clip the card asked for.

        Waits first: the browser is streaming this very clip from the NVR, and
        NVRs serve only a few playback sessions at once, so a parallel download
        would slow the play the user is waiting on. Not for a camera that only
        works over FLV: Home Assistant's player asks that NVR for Download,
        which it refuses, so nothing is streaming and the cache is the only way
        the clip will play.
        """
        parts = bare_identifier(media_content_id).split("|", 6)
        if len(parts) != 7 or parts[0] != "FILE":
            return

        camera = next(
            (
                item
                for item in self._cameras.values()
                if item.entry_id == parts[1] and item.channel == parts[2]
            ),
            None,
        )
        if camera is None:
            return

        if self._vod_types.get(camera.key) != "FLV":
            await asyncio.sleep(ON_DEMAND_CACHE_DELAY)
        record = self._index.get(clip_id_for(media_content_id))
        if record and record.get("cached_at"):
            return

        start = _parse_reolink_time(parts[5])
        day = start.date() if start else dt_util.now().date()

        # Prefer the real browse entry so the record carries the right event
        # type; fall back to the bare identifier if the day cannot be listed.
        descriptor = next(
            (
                item
                for item in await self.async_list_day(camera, day)
                if item["media_content_id"] == media_content_id
            ),
            None,
        ) or self._descriptor_for(camera, _StubChild(media_content_id, ""), day, None)

        if descriptor and await self._async_cache_clip(descriptor):
            self._schedule_save()
            async_dispatcher_send(self.hass, SIGNAL_INDEX_UPDATED)

    # ── Diagnostics ────────────────────────────────────────────────────

    async def async_diagnose(self, camera_key: str | None = None) -> dict[str, Any]:
        """Try every route for one clip and report what each one did.

        Reolink NVRs vary in which VOD request types they will serve, and a
        refusal arrives as a dropped connection rather than a useful error.
        This tries them all against a single real recording so the answer is
        measured rather than guessed at.
        """
        if not self._cameras:
            await self.async_discover()

        cameras = (
            [self._cameras[camera_key]]
            if camera_key and camera_key in self._cameras
            else list(self._cameras.values())
        )

        report: dict[str, Any] = {
            "stream": self.stream,
            "event_types": self.event_types,
            "routes_in_use": dict(self._vod_types),
            "paused_cameras": {
                key: until.isoformat()
                for key, (until, _level) in self._backoff.items()
                if until > dt_util.utcnow()
            },
            "cameras": {},
        }

        today = dt_util.now().date()

        for camera in cameras:
            entry: dict[str, Any] = {}
            report["cameras"][camera.name] = entry

            # Which days the NVR admits to having, which also proves whether
            # browsing works at all.
            try:
                dates = await self.async_dates(camera.key)
                entry["days_with_recordings"] = [item["date"] for item in dates[:7]]
            except Exception as err:  # noqa: BLE001
                entry["days_with_recordings"] = f"listing days failed: {err}"
                dates = []

            # Run early enough in the morning and today has nothing yet, so
            # walk back until a real recording turns up.
            candidates = [today - timedelta(days=offset) for offset in range(DIAGNOSE_DAYS)]
            candidates += [
                day
                for item in dates
                if (day := dt_date.fromisoformat(item["date"])) not in candidates
            ]

            descriptor = None
            folders: list[str] = []
            unfiltered = 0
            for day in candidates:
                try:
                    # Ignore the configured event types here: if the day has
                    # recordings the filter is excluding, that is the answer.
                    clips = await self.async_list_day(
                        camera, day, event_types=set(), folders_seen=folders
                    )
                except Exception as err:  # noqa: BLE001
                    entry["error"] = f"listing {day} failed: {err}"
                    break
                if not clips:
                    continue

                unfiltered = len(clips)
                wanted = set(self.event_types)
                matching = [
                    clip for clip in clips if set(clip["event_types"]) & wanted
                ]
                entry["tested_day"] = day.isoformat()
                entry["clips_on_day"] = unfiltered
                entry["clips_matching_event_types"] = len(matching)
                descriptor = (matching or clips)[0]
                break

            if folders:
                entry["trigger_folders_present"] = sorted(set(folders))

            if descriptor is None:
                entry.setdefault(
                    "error",
                    f"no recordings at all in the last {DIAGNOSE_DAYS} days or on "
                    "any day the NVR lists - the day listing came back empty",
                )
                continue

            if (api := self._host_api(descriptor)) is not None and "device" not in report:
                report["device"] = {
                    key: str(getattr(api, attr, None))
                    for key, attr in (
                        ("name", "nvr_name"),
                        ("model", "model"),
                        ("hardware", "hardware_version"),
                        ("firmware", "sw_version"),
                        ("is_nvr", "is_nvr"),
                    )
                }

            routes: dict[str, str] = {}
            probe_path = self._root / "diagnose.part"
            written = await self._async_library_download(
                descriptor, probe_path, max_bytes=65536
            )
            await self.hass.async_add_executor_job(_unlink, probe_path)
            routes["library/download_vod"] = (
                f"OK - {written} bytes" if written else self._last_error or "failed"
            )
            for route in DIRECT_ROUTES:
                request_type, plain_http = split_route(route)
                url = await self._async_direct_source(
                    descriptor, request_type, plain_http
                )
                routes[f"direct/{route}"] = (
                    await self._async_probe(
                        url, DIRECT_HEADERS, ranged=request_type != "FLV"
                    )
                    if url
                    else "no URL for this request type"
                )
            native = await self._async_native_file(descriptor)
            entry["native_file"] = native or self._last_error
            for route in NATIVE_ROUTES:
                if native is None:
                    routes[f"native/{route}"] = "no native file name"
                    continue
                is_flv = route == NATIVE_FLV
                url = await self._async_direct_source(
                    descriptor, "FLV" if is_flv else "DOWNLOAD", False, native["name"]
                )
                if url and is_flv:
                    url = with_seek(url, native["offset"])
                routes[f"native/{route}"] = (
                    await self._async_probe(url, DIRECT_HEADERS, ranged=not is_flv)
                    if url
                    else "no URL for this request type"
                )
            proxy = await self._async_source_url(descriptor["media_content_id"])
            routes["home assistant proxy"] = (
                await self._async_probe(proxy, DOWNLOAD_HEADERS)
                if proxy
                else "could not be resolved"
            )

            entry["clip"] = descriptor.get("title")
            entry["start"] = descriptor.get("start")
            entry["event_types"] = descriptor.get("event_types")
            entry["routes"] = routes
            _LOGGER.warning("Clip cache diagnosis for %s: %s", camera.name, routes)

        return report

    async def _async_probe(
        self, url: str, headers: dict[str, str], ranged: bool = True
    ) -> str:
        """Ask for the first slice of a clip and describe what came back.

        A playback stream (FLV) is not a file, so it is asked for without a
        byte range and simply read until enough has arrived.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        probe = {**headers, "Range": "bytes=0-65535"} if ranged else dict(headers)
        try:
            async with asyncio.timeout(30):
                async with session.get(url, headers=probe) as response:
                    if response.status not in OK_STATUSES:
                        body = await response.content.read(65536)
                        detail = body[:200].decode("utf-8", "replace").strip()
                        return redact(
                            f"HTTP {response.status} ({response.content_type}) {detail}"
                        )
                    body = b""
                    while len(body) < 65536:
                        chunk = await response.content.read(65536 - len(body))
                        if not chunk:
                            break
                        body += chunk
                    kind = ", FLV data" if body.startswith(FLV_MAGIC) else ""
                    if not body:
                        return f"HTTP {response.status} but no data"
                    return (
                        f"OK - HTTP {response.status}, {len(body)} bytes, "
                        f"{response.content_type}{kind}"
                    )
        except TimeoutError:
            return "timed out after 30s"
        except Exception as err:  # noqa: BLE001
            return redact(f"{type(err).__name__}: {err}")

    def stats(self) -> dict[str, Any]:
        """Return cache statistics for the status command and the sensors."""
        total_size = 0
        per_camera: dict[str, int] = {}
        latest: dict[str, Any] | None = None

        for record in self._index.values():
            if not record.get("cached_at"):
                continue
            total_size += int(record.get("size") or 0)
            name = record.get("camera_name") or record.get("camera") or "unknown"
            per_camera[name] = per_camera.get(name, 0) + 1
            if latest is None or (record.get("start") or "") > (
                latest.get("start") or ""
            ):
                latest = record

        return {
            "total_clips": sum(per_camera.values()),
            "total_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cameras": per_camera,
            "cache_days": self.cache_days,
            "max_cache_size_mb": self.max_cache_bytes // (1024 * 1024),
            "stream": self.stream,
            "event_types": self.event_types,
            "storage_path": str(self._root),
            "last_clip": (
                {
                    "camera": latest.get("camera_name"),
                    "event_type": latest.get("event_type"),
                    "start": latest.get("start"),
                }
                if latest
                else None
            ),
        }


# ── Executor helpers ───────────────────────────────────────────────────


def _open_write(path: Path):
    """Open a file for binary writing (executor)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("wb")


def _read_head(path: Path, size: int) -> bytes:
    """Return the first bytes of a file, or nothing if it is gone (executor)."""
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _unlink(path: Path) -> None:
    """Delete a file if it exists (executor)."""
    path.unlink(missing_ok=True)


def _size_of(path: Path) -> int:
    """Return a file's size, or 0 if it is gone (executor)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _list_ids(directory: Path, suffix: str) -> set[str]:
    """Return the stems of every file with the given suffix (executor)."""
    if not directory.exists():
        return set()
    return {path.stem for path in directory.glob(f"*{suffix}")}


def _remove_ids(clips_dir: Path, thumbs_dir: Path, clip_ids: set[str]) -> None:
    """Delete the clip and thumbnail for each id (executor)."""
    for clip_id in clip_ids:
        (clips_dir / f"{clip_id}.mp4").unlink(missing_ok=True)
        (clips_dir / f"{clip_id}.part").unlink(missing_ok=True)
        (thumbs_dir / f"{clip_id}.jpg").unlink(missing_ok=True)
