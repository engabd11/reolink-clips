"""Coordinator for Reolink Clip Cache.

Manages event listeners, clip downloading, metadata, WebSocket API,
and automatic cleanup. This is the brain of the integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CACHE_DIR_NAME,
    CACHEABLE_EVENT_TYPES,
    CLIP_FILENAME_PATTERN,
    DEFAULT_CACHE_DAYS,
    DEFAULT_RESOLUTION,
    DOMAIN,
    EVENT_CLIP_CACHED,
    META_FILENAME_PATTERN,
    REOLINK_MEDIA_PREFIX,
    WS_BROWSE,
    WS_RESOLVE,
    WS_STATUS,
    WS_THUMBNAIL,
)

_LOGGER = logging.getLogger(__name__)

# Cooldown per camera+event_type: don't re-download within 30 seconds
DOWNLOAD_COOLDOWN = timedelta(seconds=30)


class ReolinkClipCacheCoordinator:
    """Central coordinator for Reolink Clip Cache.

    Responsibilities:
    - Listen to Reolink binary_sensor state changes
    - Download clips via Reolink media_source API on detection events
    - Faststart (moov_to_start) downloaded clips with ffmpeg
    - Store metadata JSON alongside each clip
    - Expose WebSocket API for the card to query cached clips
    - Periodically purge old clips
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options = entry.options

        # Cache directory: /config/www/reolink_cache/
        self._www_dir = Path(hass.config.path("www"))
        self._cache_dir = self._www_dir / CACHE_DIR_NAME

        # In-memory index: {camera_name: {date_str: [clip_meta, ...]}}
        self._index: dict[str, dict[str, list[dict]]] = {}

        # Download tracking: {(camera, event_type, timestamp): asyncio.Task}
        self._downloads: dict[tuple, asyncio.Task] = {}

        # Cooldown tracking: {(camera, event_type): last_download_time}
        self._last_download: dict[tuple, datetime] = {}

        # Unsub handles
        self._unsub_listeners: list = []
        self._unsub_purge = None

        # Camera config from the integration options
        # Maps camera name → {sensors, event_types}
        self._cameras: dict[str, dict] = {}

        # Store for persistence
        self._store = Store(hass, 1, f"{DOMAIN}_index")

    # ── Setup / Teardown ──────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Set up the coordinator."""
        # Create cache directory
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Load persisted index
        await self._load_index()

        # Build camera config from Reolink entities
        await self._discover_cameras()

        # Register WebSocket API
        self._register_websocket()

        # Start event listeners for detection binary_sensors
        self._start_event_listeners()

        # Schedule periodic purge
        cache_days = self.options.get("cache_days", DEFAULT_CACHE_DAYS)
        self._unsub_purge = async_track_time_interval(
            self.hass,
            self._async_purge_old_clips,
            timedelta(hours=6),
        )

        # Also run purge once at startup
        await self._async_purge_old_clips(datetime.now())

        _LOGGER.info(
            "Reolink Clip Cache setup complete. Cameras: %s. Cache dir: %s",
            list(self._cameras.keys()),
            self._cache_dir,
        )

    async def async_unload(self) -> None:
        """Unload the coordinator."""
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        if self._unsub_purge:
            self._unsub_purge()
            self._unsub_purge = None

        # Cancel pending downloads
        for task in self._downloads.values():
            task.cancel()
        self._downloads.clear()

        _LOGGER.info("Reolink Clip Cache unloaded")

    # ── Camera Discovery ───────────────────────────────────────────────

    async def _discover_cameras(self) -> None:
        """Discover Reolink cameras and their detection sensors from HA entities.

        Maps binary_sensor entities to camera names based on naming patterns.
        Your sensors follow these patterns:
          - binary_sensor.back_door_person → camera "BACK DOOR"
          - binary_sensor.reolink_duo_3_poe_person → camera "Carport"
          - binary_sensor.doorbell_person → camera "Doorbell"
        """
        # Define known camera mappings based on your setup
        # These map binary_sensor prefixes to the camera name used in Reolink media source
        known_cameras = {
            "back_door": {
                "name": "BACK DOOR",
                "event_types": ["Person", "Vehicle"],
            },
            "reolink_duo_3_poe": {
                "name": "Carport",
                "event_types": ["Person", "Vehicle", "Animal"],
            },
            "doorbell": {
                "name": "Doorbell",
                "event_types": ["Person", "Vehicle", "Visitor", "Package"],
            },
        }

        # Also allow override from integration options
        custom_cameras = self.options.get("cameras", {})

        # Scan binary_sensor entities for Reolink detection sensors
        for state in self.hass.states.async_all("binary_sensor"):
            entity_id = state.entity_id

            for prefix, cam_config in known_cameras.items():
                if entity_id.startswith(f"binary_sensor.{prefix}_"):
                    # Extract event type from entity
                    # e.g., binary_sensor.back_door_person → "Person"
                    suffix = entity_id.replace(f"binary_sensor.{prefix}_", "")
                    event_type = suffix.capitalize()

                    if event_type in cam_config["event_types"] or event_type in CACHEABLE_EVENT_TYPES:
                        cam_name = cam_config["name"]
                        if cam_name not in self._cameras:
                            self._cameras[cam_name] = {
                                "sensors": {},
                                "event_types": [],
                            }
                        self._cameras[cam_name]["sensors"][event_type] = entity_id
                        if event_type not in self._cameras[cam_name]["event_types"]:
                            self._cameras[cam_name]["event_types"].append(event_type)

        _LOGGER.info("Discovered cameras: %s", self._cameras)

    # ── Event Listeners ────────────────────────────────────────────────

    def _start_event_listeners(self) -> None:
        """Register state change listeners for all detection sensors."""
        for cam_name, cam_config in self._cameras.items():
            for event_type, entity_id in cam_config["sensors"].items():
                if event_type not in CACHEABLE_EVENT_TYPES and event_type not in ["Visitor", "Package"]:
                    continue

                async def _on_detection(event: Event, _cam=cam_name, _etype=event_type) -> None:
                    await self._handle_detection(_cam, _etype)

                unsub = self.hass.bus.async_listen(
                    f"state_changed",
                    self._make_state_filter(entity_id, _on_detection),
                )
                self._unsub_listeners.append(unsub)
                _LOGGER.debug("Listening for %s on %s (%s)", event_type, cam_name, entity_id)

    def _make_state_filter(self, entity_id: str, callback_fn):
        """Create a bus listener that only fires for specific entity_id going to 'on'."""
        @callback
        def _filter(event: Event) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if (
                new_state
                and new_state.entity_id == entity_id
                and new_state.state == "on"
                and (not old_state or old_state.state != "on")
            ):
                # Fire the async callback
                self.hass.async_create_task(callback_fn(event))

        return _filter

    # ── Detection Handler ──────────────────────────────────────────────

    async def _handle_detection(self, camera: str, event_type: str) -> None:
        """Handle a detection event: queue clip download from NVR."""
        # Cooldown check
        key = (camera, event_type)
        now = datetime.now()
        if key in self._last_download and (now - self._last_download[key]) < DOWNLOAD_COOLDOWN:
            _LOGGER.debug("Cooldown active for %s %s, skipping", camera, event_type)
            return

        self._last_download[key] = now

        # Avoid duplicate downloads
        if key in self._downloads and not self._downloads[key].done():
            _LOGGER.debug("Download already in progress for %s %s", camera, event_type)
            return

        task = self.hass.async_create_task(
            self._download_clip(camera, event_type)
        )
        self._downloads[key] = task

    # ── Clip Download ──────────────────────────────────────────────────

    async def _download_clip(self, camera: str, event_type: str) -> None:
        """Download a clip from the Reolink NVR via media_source API.

        1. Browse to camera → resolution → today's date → event type
        2. Get the most recent clip
        3. Resolve the media URL
        4. Download the file
        5. Faststart it with ffmpeg
        6. Save metadata JSON
        """
        resolution = self.options.get("resolution", DEFAULT_RESOLUTION)
        now = datetime.now()
        date_str = f"{now.year}/{now.month}/{now.day}"

        try:
            # Step 1: Browse to the event folder
            clip_info = await self._find_clip_in_media_source(
                camera, event_type, date_str, resolution
            )
            if not clip_info:
                _LOGGER.warning("No clip found for %s %s on %s", camera, event_type, date_str)
                return

            # Step 2: Resolve media URL
            resolved = await self.hass.components.media_source.async_resolve_media(
                self.hass, clip_info["media_content_id"]
            )
            if not resolved or not resolved.url:
                _LOGGER.warning("Could not resolve media URL for %s %s", camera, event_type)
                return

            # Step 3: Download the file
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            safe_camera = camera.lower().replace(" ", "_")
            safe_event = event_type.lower()
            filename = CLIP_FILENAME_PATTERN.format(
                camera=safe_camera,
                event_type=safe_event,
                timestamp=timestamp,
            )
            local_path = self._cache_dir / filename
            meta_path = self._cache_dir / META_FILENAME_PATTERN.format(
                camera=safe_camera,
                event_type=safe_event,
                timestamp=timestamp,
            )

            # Check if we already have a clip for this time window (within 60s)
            if self._is_recently_cached(camera, event_type, now, threshold_seconds=60):
                _LOGGER.debug("Recently cached clip exists for %s %s, skipping download", camera, event_type)
                return

            _LOGGER.info("Downloading clip: %s %s → %s", camera, event_type, filename)

            # Download via aiohttp
            session = async_get_clientsession(self.hass)
            async with session.get(resolved.url) as resp:
                if resp.status != 200:
                    _LOGGER.error("Download failed with status %d", resp.status)
                    return
                content = await resp.read()

            # Save raw clip
            local_path.write_bytes(content)

            # Step 4: Faststart with ffmpeg (move moov atom to front)
            await self._faststart_clip(local_path)

            # Step 5: Write metadata
            meta = {
                "camera": camera,
                "event_type": event_type,
                "timestamp": now.isoformat(),
                "date": date_str,
                "filename": filename,
                "resolution": resolution,
                "media_content_id": clip_info["media_content_id"],
                "size_bytes": local_path.stat().st_size,
                "cached_at": now.isoformat(),
            }
            meta_path.write_text(json.dumps(meta, indent=2))

            # Update in-memory index
            self._add_to_index(camera, date_str, meta)

            # Save index to disk
            await self._save_index()

            # Fire custom event for the card
            self.hass.bus.async_fire(EVENT_CLIP_CACHED, {
                "camera": camera,
                "event_type": event_type,
                "date": date_str,
                "filename": filename,
            })

            _LOGGER.info("Clip cached: %s (%d bytes)", filename, len(content))

        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error downloading clip for %s %s", camera, event_type)

    async def _find_clip_in_media_source(
        self,
        camera: str,
        event_type: str,
        date_str: str,
        resolution: str,
    ) -> dict | None:
        """Navigate the Reolink media source tree to find the latest clip.

        Hierarchy: reolink → camera → resolution → date → event_type → clips
        """
        try:
            # Browse root
            root = await self.hass.components.media_source.async_browse_media(
                self.hass, REOLINK_MEDIA_PREFIX
            )
            if not root or not root.children:
                return None

            # Find camera folder
            cam_folder = None
            cam_lower = camera.lower()
            for child in root.children:
                if child.title.lower() == cam_lower or cam_lower in child.title.lower():
                    cam_folder = child
                    break
            if not cam_folder:
                return None

            # Browse camera → find resolution folder
            cam_result = await self.hass.components.media_source.async_browse_media(
                self.hass, cam_folder.media_content_id
            )
            res_folder = None
            for child in cam_result.children:
                if resolution.lower() in child.title.lower():
                    res_folder = child
                    break
            if not res_folder:
                return None

            # Browse resolution → find date folder
            res_result = await self.hass.components.media_source.async_browse_media(
                self.hass, res_folder.media_content_id
            )
            date_folder = None
            for child in res_result.children:
                if date_str in child.title:
                    date_folder = child
                    break
            if not date_folder:
                return None

            # Browse date → find event type folder
            date_result = await self.hass.components.media_source.async_browse_media(
                self.hass, date_folder.media_content_id
            )
            event_folder = None
            for child in date_result.children:
                if child.title.lower() == event_type.lower():
                    event_folder = child
                    break
            if not event_folder:
                return None

            # Browse event type → get clips
            event_result = await self.hass.components.media_source.async_browse_media(
                self.hass, event_folder.media_content_id
            )
            clips = [c for c in (event_result.children or []) if c.can_play or c.media_content_type == "video/mp4"]
            if not clips:
                return None

            # Return the most recent clip (sorted by title descending = newest first)
            clips.sort(key=lambda c: c.title, reverse=True)
            return {"media_content_id": clips[0].media_content_id, "title": clips[0].title}

        except Exception:
            _LOGGER.exception("Error navigating media source for %s %s", camera, event_type)
            return None

    async def _faststart_clip(self, clip_path: Path) -> None:
        """Run ffmpeg -moov_to_start to move MP4 index to front for instant playback."""
        import subprocess

        tmp_path = clip_path.with_suffix(".tmp.mp4")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(clip_path),
                "-c", "copy", "-movflags", "+faststart",
                str(tmp_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                _LOGGER.error("ffmpeg faststart failed: %s", stderr.decode()[-200:])
                return

            # Replace original with faststarted version
            tmp_path.rename(clip_path)
            _LOGGER.debug("Faststarted: %s", clip_path.name)
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg faststart timed out for %s", clip_path.name)
            tmp_path.unlink(missing_ok=True)
        except Exception:
            _LOGGER.exception("ffmpeg faststart error for %s", clip_path.name)
            tmp_path.unlink(missing_ok=True)

    def _is_recently_cached(
        self, camera: str, event_type: str, now: datetime, threshold_seconds: int = 60
    ) -> bool:
        """Check if we already have a cached clip within the threshold."""
        safe_camera = camera.lower().replace(" ", "_")
        safe_event = event_type.lower()
        prefix = f"{safe_camera}_{safe_event}_"

        if not self._cache_dir.exists():
            return False

        for f in self._cache_dir.glob(f"{prefix}*.mp4"):
            # Parse timestamp from filename
            try:
                # Format: back_door_person_20260815_143252.mp4
                ts_str = f.stem.replace(prefix, "")
                ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                if (now - ts).total_seconds() < threshold_seconds:
                    return True
            except (ValueError, IndexError):
                continue
        return False

    # ── Index Management ────────────────────────────────────────────────

    def _add_to_index(self, camera: str, date_str: str, meta: dict) -> None:
        """Add a clip metadata entry to the in-memory index."""
        if camera not in self._index:
            self._index[camera] = {}
        if date_str not in self._index[camera]:
            self._index[camera][date_str] = []
        self._index[camera][date_str].append(meta)

    async def _load_index(self) -> None:
        """Load the clip index from disk."""
        try:
            data = await self._store.async_load()
            if data:
                self._index = data
                _LOGGER.info("Loaded cache index with %d cameras", len(self._index))
        except Exception:
            _LOGGER.debug("No existing cache index found, starting fresh")

    async def _save_index(self) -> None:
        """Persist the clip index to disk."""
        try:
            await self._store.async_save(self._index)
        except Exception:
            _LOGGER.exception("Error saving cache index")

    async def _rebuild_index_from_disk(self) -> None:
        """Rebuild the in-memory index by scanning cache directory for JSON files."""
        self._index = {}
        if not self._cache_dir.exists():
            return

        for meta_file in self._cache_dir.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text())
                camera = meta["camera"]
                date_str = meta.get("date", "")
                self._add_to_index(camera, date_str, meta)
            except Exception:
                _LOGGER.warning("Could not parse meta file: %s", meta_file)

        await self._save_index()
        _LOGGER.info("Rebuilt cache index: %d cameras, %d total clips",
                     len(self._index),
                     sum(len(v) for dates in self._index.values() for v in dates.values()))

    # ── Cache Purge ────────────────────────────────────────────────────

    async def _async_purge_old_clips(self, now: datetime) -> None:
        """Remove clips older than cache_days."""
        cache_days = self.options.get("cache_days", DEFAULT_CACHE_DAYS)
        cutoff = now - timedelta(days=cache_days)

        if not self._cache_dir.exists():
            return

        purged = 0
        for meta_file in list(self._cache_dir.glob("*.json")):
            try:
                meta = json.loads(meta_file.read_text())
                ts = datetime.fromisoformat(meta["timestamp"])
                if ts < cutoff:
                    # Delete both meta and clip
                    clip_file = self._cache_dir / meta["filename"]
                    clip_file.unlink(missing_ok=True)
                    meta_file.unlink()
                    purged += 1
            except Exception:
                # If we can't parse it, leave it
                pass

        if purged > 0:
            _LOGGER.info("Purged %d old clips (older than %d days)", purged, cache_days)
            await self._rebuild_index_from_disk()

    async def purge_cache(self) -> None:
        """Manually purge all old clips."""
        await self._async_purge_old_clips(datetime.now())

    async def refresh_all(self) -> None:
        """Force refresh — rebuild index from disk."""
        await self._rebuild_index_from_disk()

    # ── WebSocket API ───────────────────────────────────────────────────

    def _register_websocket(self) -> None:
        """Register WebSocket commands for the card to query cached clips."""

        async def handle_browse(hass: HomeAssistant, connection, msg):
            """Return cached clips for a given camera and optional date/event_type filter.

            Message format:
              {type: "reolink_clip_cache/browse", camera: "BACK DOOR",
               date?: "2026/8/15", event_type?: "Person"}
            """
            camera = msg.get("camera", "")
            date_filter = msg.get("date")
            event_filter = msg.get("event_type")

            clips = []

            # If no specific camera, return all
            cameras = [camera] if camera else list(self._index.keys())

            for cam in cameras:
                if cam not in self._index:
                    continue
                for date_str, date_clips in self._index[cam].items():
                    if date_filter and date_filter not in date_str:
                        continue
                    for clip in date_clips:
                        if event_filter and clip.get("event_type", "").lower() != event_filter.lower():
                            continue
                        # Build URL for local serving
                        local_url = f"/local/{CACHE_DIR_NAME}/{clip['filename']}"
                        clips.append({
                            **clip,
                            "url": local_url,
                            "cached": True,
                        })

            # Sort by timestamp descending (newest first)
            clips.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
            connection.send_result(msg["id"], {"clips": clips, "total": len(clips)})

        async def handle_resolve(hass: HomeAssistant, connection, msg):
            """Resolve a cached clip's local URL.

            Message format:
              {type: "reolink_clip_cache/resolve", filename: "back_door_person_20260815_143252.mp4"}
            """
            filename = msg.get("filename", "")
            clip_path = self._cache_dir / filename

            if clip_path.exists():
                local_url = f"/local/{CACHE_DIR_NAME}/{filename}"
                meta_path = self._cache_dir / filename.replace(".mp4", ".json")
                meta = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                    except Exception:
                        pass
                connection.send_result(msg["id"], {
                    "url": local_url,
                    "cached": True,
                    **meta,
                })
            else:
                connection.send_result(msg["id"], {"cached": False, "url": None})

        async def handle_thumbnail(hass: HomeAssistant, connection, msg):
            """Return thumbnail URL for a cached clip (if available).

            Message format:
              {type: "reolink_clip_cache/thumbnail", filename: "..."}
            """
            filename = msg.get("filename", "").replace(".mp4", ".jpg")
            thumb_path = self._cache_dir / filename

            if thumb_path.exists():
                local_url = f"/local/{CACHE_DIR_NAME}/{filename}"
                connection.send_result(msg["id"], {"thumbnail_url": local_url, "exists": True})
            else:
                connection.send_result(msg["id"], {"thumbnail_url": None, "exists": False})

        async def handle_status(hass: HomeAssistant, connection, msg):
            """Return cache status: disk usage, clip counts, camera status."""
            total_size = 0
            total_clips = 0
            camera_stats = {}

            if self._cache_dir.exists():
                for f in self._cache_dir.glob("*.mp4"):
                    total_size += f.stat().st_size
                    total_clips += 1

            for cam, dates in self._index.items():
                cam_clips = sum(len(v) for v in dates.values())
                camera_stats[cam] = {"clips": cam_clips, "dates": list(dates.keys())}

            connection.send_result(msg["id"], {
                "total_clips": total_clips,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "cameras": camera_stats,
                "cache_dir": str(self._cache_dir),
                "cache_days": self.options.get("cache_days", DEFAULT_CACHE_DAYS),
                "resolution": self.options.get("resolution", DEFAULT_RESOLUTION),
            })

        self.hass.components.websocket_api.async_register_command(
            WS_BROWSE, handle_browse
        )
        self.hass.components.websocket_api.async_register_command(
            WS_RESOLVE, handle_resolve
        )
        self.hass.components.websocket_api.async_register_command(
            WS_THUMBNAIL, handle_thumbnail
        )
        self.hass.components.websocket_api.async_register_command(
            WS_STATUS, handle_status
        )

        _LOGGER.info("WebSocket API registered: browse, resolve, thumbnail, status")