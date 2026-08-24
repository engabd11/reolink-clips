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
import shutil
from dataclasses import dataclass, field
from datetime import date as dt_date, datetime, timedelta
from pathlib import Path
from typing import Any

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
    CONSECUTIVE_FAILURE_LIMIT,
    DEFAULT_CACHE_DAYS,
    DEFAULT_EVENT_TYPES,
    DEFAULT_MAX_CACHE_MB,
    DEFAULT_STREAM,
    DEFAULT_SWEEP_MINUTES,
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
    MAX_CLIPS_PER_SWEEP,
    MAX_CONCURRENT_DOWNLOADS,
    OK_STATUSES,
    REOLINK_DOMAIN,
    REOLINK_MEDIA_PREFIX,
    SIGNAL_INDEX_UPDATED,
    SIGNED_URL_TTL,
    STORAGE_DIR_NAME,
    STORAGE_KEY,
    STORAGE_VERSION,
    STREAMS,
    SWEEP_COOLDOWN,
    THUMBS_DIR_NAME,
    THUMB_URL,
    TRIGGER_ALIASES,
    VOD_TYPE_LADDER,
)

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 10


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
        # camera key -> the VOD request type that actually works for it
        self._vod_types: dict[str, str] = {}
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
        """Create the cache directories (executor)."""
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)

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
    ) -> int:
        """Cache every event clip not already held locally.

        Returns the number of clips newly cached.
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
                    cached += await self._async_sweep_camera(camera, day)

        if cached:
            self._schedule_save()
            async_dispatcher_send(self.hass, SIGNAL_INDEX_UPDATED)
        return cached

    async def _async_sweep_camera(self, camera: CameraInfo, day: dt_date) -> int:
        """Diff one camera-day against the index and cache what is missing."""
        now = dt_util.utcnow()
        marker = f"{camera.key}|{day.isoformat()}"
        if (last := self._last_sweep.get(marker)) and now - last < SWEEP_COOLDOWN:
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
                continue

            consecutive_failures += 1
            # With retries and backoff, grinding through a whole batch against
            # an NVR that is refusing everything would tie up the sweep for
            # many minutes. Give up early and try again next time.
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                _LOGGER.warning(
                    "%s: %d clips failed in a row, abandoning this sweep",
                    camera.name,
                    consecutive_failures,
                )
                break
        return cached

    async def async_list_day(
        self, camera: CameraInfo, day: dt_date
    ) -> list[dict[str, Any]]:
        """Return descriptors for every wanted clip on one camera-day.

        Browses the media source only - that is the fast Reolink call. It is
        *resolving* a clip for playback that is slow, which is exactly what
        this cache exists to avoid.
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

        wanted = set(self.event_types)
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
                if trigger not in wanted:
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
                self._collect(descriptors, camera, child, day, None, wanted)

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
                    return False

                await self._async_faststart(part_path, clip_path)
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

    def _vod_type_order(self, descriptor: dict[str, Any]) -> list[str]:
        """Return the VOD request types to try for a camera, best first.

        Home Assistant always asks an NVR for ``Download``, but plenty of NVRs
        refuse that and hang up, and want the recording prepared through
        ``NvrDownload`` first. Once one works for a camera it is used on its
        own.
        """
        if known := self._vod_types.get(self._camera_marker(descriptor)):
            return [known]
        return list(VOD_TYPE_LADDER)

    @staticmethod
    def _camera_marker(descriptor: dict[str, Any]) -> str:
        """Return a per-camera key for remembering what works."""
        return f"{descriptor.get('camera')}"

    async def _async_direct_source(
        self, descriptor: dict[str, Any], request_type_name: str = "DOWNLOAD"
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

        return url if url.startswith(("http://", "https://")) else None

    async def _async_download_with_retries(
        self, descriptor: dict[str, Any], dest: Path
    ) -> int:
        """Fetch a clip, trying the NVR directly before Home Assistant's proxy.

        The proxy hands our request straight to the NVR over a pooled
        connection with a five second read timeout, which is where "Server
        disconnected" comes from. Fetching the NVR's own URL ourselves removes
        that hop, refuses connection reuse and allows a longer read window.
        """
        marker = self._camera_marker(descriptor)
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            tried_anything = False
            for type_name in self._vod_type_order(descriptor):
                direct = await self._async_direct_source(descriptor, type_name)
                if direct is None:
                    continue
                tried_anything = True
                written = await self._async_download(
                    direct,
                    dest,
                    headers=DIRECT_HEADERS,
                    label=f"the NVR directly ({type_name})",
                )
                if written:
                    if self._vod_types.get(marker) != type_name:
                        _LOGGER.info(
                            "Using the %s request type for %s",
                            type_name,
                            descriptor.get("camera_name") or marker,
                        )
                        self._vod_types[marker] = type_name
                    return written
                await self.hass.async_add_executor_job(_unlink, dest)

            source = await self._async_source_url(descriptor["media_content_id"])
            if source is None:
                if not tried_anything:
                    return 0
            elif written := await self._async_download(
                source, dest, label="the Home Assistant proxy"
            ):
                return written

            await self.hass.async_add_executor_job(_unlink, dest)
            if attempt < DOWNLOAD_ATTEMPTS:
                delay = DOWNLOAD_RETRY_BACKOFF[
                    min(attempt - 1, len(DOWNLOAD_RETRY_BACKOFF) - 1)
                ]
                _LOGGER.debug(
                    "Retrying clip download in %ss (attempt %d of %d)",
                    delay,
                    attempt + 1,
                    DOWNLOAD_ATTEMPTS,
                )
                await asyncio.sleep(delay)

        _LOGGER.warning(
            "Giving up on a clip after %d attempts; it will be retried on a "
            "later sweep. If this keeps happening for every clip, try the "
            "other stream in the integration options - some NVRs will not "
            "serve downloads for one of them",
            DOWNLOAD_ATTEMPTS,
        )
        return 0

    async def _async_download(
        self,
        url: str,
        dest: Path,
        headers: dict[str, str] | None = None,
        label: str = "the Home Assistant proxy",
    ) -> int:
        """Stream a URL to disk. Returns the number of bytes written."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        written = 0
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
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
                        _LOGGER.warning(
                            "Clip download from %s failed: HTTP %s (%s) - %s",
                            label,
                            response.status,
                            response.content_type,
                            detail or "<empty body>",
                        )
                        return 0
                    handle = await self.hass.async_add_executor_job(_open_write, dest)
                    try:
                        async for chunk in response.content.iter_chunked(
                            DOWNLOAD_CHUNK_SIZE
                        ):
                            await self.hass.async_add_executor_job(handle.write, chunk)
                            written += len(chunk)
                    finally:
                        await self.hass.async_add_executor_job(handle.close)
        except TimeoutError:
            _LOGGER.warning(
                "Clip download from %s timed out after %ss", label, DOWNLOAD_TIMEOUT
            )
            return 0
        except ClientError as err:
            # Naming the exception type matters: a dropped connection, a
            # refused one and a bad response all read alike without it.
            _LOGGER.warning(
                "Clip download from %s failed: %s: %s", label, type(err).__name__, err
            )
            return 0
        return written

    async def _async_faststart(self, src: Path, dest: Path) -> None:
        """Move the MP4 index to the front so playback can start immediately.

        Without this the browser has to fetch the whole file before the first
        frame, which is a large part of the delay this integration removes.
        """
        if binary := self._ffmpeg_binary():
            ok = await self._run_ffmpeg(
                binary,
                "-y",
                "-i",
                str(src),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            )
            if ok:
                await self.hass.async_add_executor_job(_unlink, src)
                return
            _LOGGER.debug("Faststart failed for %s, storing as downloaded", src.name)

        await self.hass.async_add_executor_job(shutil.move, str(src), str(dest))

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

        return {"cached": False, "clip_id": clip_id, "url": media.url}

    async def _async_cache_on_demand(self, media_content_id: str) -> None:
        """Cache a single clip the card asked for."""
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
            "cameras": {},
        }

        for camera in cameras:
            try:
                clips = await self.async_list_day(camera, dt_util.now().date())
            except Exception as err:  # noqa: BLE001
                report["cameras"][camera.name] = {"error": f"listing failed: {err}"}
                continue
            if not clips:
                report["cameras"][camera.name] = {"error": "no clips found today"}
                continue

            descriptor = clips[0]
            routes: dict[str, str] = {}
            for type_name in VOD_TYPE_LADDER:
                url = await self._async_direct_source(descriptor, type_name)
                routes[f"direct/{type_name}"] = (
                    await self._async_probe(url, DIRECT_HEADERS)
                    if url
                    else "no URL for this request type"
                )
            proxy = await self._async_source_url(descriptor["media_content_id"])
            routes["home assistant proxy"] = (
                await self._async_probe(proxy, DOWNLOAD_HEADERS)
                if proxy
                else "could not be resolved"
            )

            report["cameras"][camera.name] = {
                "clip": descriptor.get("title"),
                "start": descriptor.get("start"),
                "routes": routes,
            }
            _LOGGER.warning("Clip cache diagnosis for %s: %s", camera.name, routes)

        return report

    async def _async_probe(self, url: str, headers: dict[str, str]) -> str:
        """Ask for the first slice of a clip and describe what came back."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        probe = {**headers, "Range": "bytes=0-65535"}
        try:
            async with asyncio.timeout(30):
                async with session.get(url, headers=probe) as response:
                    body = await response.content.read(65536)
                    if response.status in OK_STATUSES:
                        return f"OK - HTTP {response.status}, {len(body)} bytes, {response.content_type}"
                    detail = body[:200].decode("utf-8", "replace").strip()
                    return f"HTTP {response.status} ({response.content_type}) {detail}"
        except TimeoutError:
            return "timed out after 30s"
        except ClientError as err:
            return f"{type(err).__name__}: {err}"
        except Exception as err:  # noqa: BLE001
            return f"{type(err).__name__}: {err}"

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
