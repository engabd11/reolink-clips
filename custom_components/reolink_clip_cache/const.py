"""Constants for the Reolink Clip Cache integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "reolink_clip_cache"
VERSION: Final = "2.3.0"

PLATFORMS: Final = [Platform.SENSOR]

# ── Reolink integration interop ──────────────────────────────────────────

REOLINK_DOMAIN: Final = "reolink"
REOLINK_MEDIA_PREFIX: Final = "media-source://reolink"

# Reolink binary_sensor entity description keys that represent a recorded
# event we can cache. These are the trailing segment of the entity unique_id
# (``{mac}_{channel}_{key}``) and also match the media-source EVE trigger name.
CACHEABLE_TRIGGERS: Final = [
    "person",
    "vehicle",
    "pet",
    "animal",
    "package",
    "visitor",
    "face",
]

# Triggers cached by default. Every value must be one the config flow offers:
# a default the form cannot show stays hidden in the selection and fails
# validation ("value must be one of ..."). Pets are covered by "animal"
# through TRIGGER_ALIASES.
DEFAULT_EVENT_TYPES: Final = ["person", "vehicle", "animal"]

# Reolink groups pets and animals under different keys depending on model and
# HA version; both surface as the same thing to a user.
TRIGGER_ALIASES: Final = {"pet": "animal", "dog_cat": "animal"}

# ── Streams ──────────────────────────────────────────────────────────────

# Media-source stream identifiers (the ``RES|entry|ch|<stream>`` segment).
STREAM_SUB: Final = "sub"
STREAM_MAIN: Final = "main"
STREAMS: Final = [STREAM_SUB, STREAM_MAIN]
DEFAULT_STREAM: Final = STREAM_SUB

# ── Options ──────────────────────────────────────────────────────────────

CONF_CACHE_DAYS: Final = "cache_days"
CONF_STREAM: Final = "stream"
CONF_MAX_CACHE_MB: Final = "max_cache_size_mb"
CONF_EVENT_TYPES: Final = "event_types"
CONF_SWEEP_MINUTES: Final = "sweep_minutes"
CONF_CAMERAS: Final = "cameras"

DEFAULT_CACHE_DAYS: Final = 7
DEFAULT_MAX_CACHE_MB: Final = 2048
DEFAULT_SWEEP_MINUTES: Final = 2

# Reolink NVRs drop connections when they are busy, so a clip that fails is
# retried a few times with a widening gap before it is left for a later sweep.
DOWNLOAD_ATTEMPTS: Final = 2
DOWNLOAD_RETRY_BACKOFF: Final = (5, 20)

# Breathing room between clips so a sweep does not hammer the NVR.
DOWNLOAD_SPACING: Final = 2

# How many days back the diagnose service looks for a clip to test.
DIAGNOSE_DAYS: Final = 3

# VOD request types to try against the NVR, best first. Home Assistant always
# asks an NVR for Download, but many will not serve that and hang up; those
# want the recording prepared through NvrDownload first. Others still serve
# FLV, the playback stream the Reolink web client uses: it is remuxed to MP4
# once downloaded.
VOD_TYPE_LADDER: Final = ("DOWNLOAD", "NVR_DOWNLOAD", "FLV", "PLAYBACK")

# These are also tried over the NVR's plain HTTP port when the Reolink
# integration talks to it over HTTPS. An RLN8-410 on firmware 3.6.5 answers API
# calls over HTTPS but hangs up on every media request there, Home Assistant's
# own player included, while its HTTP port streams FLV fine.
PLAIN_HTTP_TYPES: Final = ("DOWNLOAD", "NVR_DOWNLOAD", "FLV")
PLAIN_HTTP_SUFFIX: Final = "/HTTP"

# Every direct route, in the order tried: each request type over the
# integration's own connection, then over plain HTTP.
DIRECT_ROUTES: Final = tuple(
    route
    for request_type in VOD_TYPE_LADDER
    for route in (
        (request_type, f"{request_type}{PLAIN_HTTP_SUFFIX}")
        if request_type in PLAIN_HTTP_TYPES
        else (request_type,)
    )
)

# FLV is a playback stream rather than a file, so it may arrive at about real
# time and may not end with the recording. Read for the clip's length plus this
# much, then keep what arrived.
FLV_GRACE_SECONDS: Final = 15
# Used when a clip's length cannot be worked out from its id.
FLV_FALLBACK_SECONDS: Final = 120

# Routes that ask the NVR for a recording by its native file name, as its
# Search command reports it (for example 1-0-0-01260906130000-00000), rather
# than by time. An RLN8-410 on firmware 3.6.5 hangs up on every other request
# for a recording, Home Assistant's own player included: NvrDownload hands back
# a temporary fragment_*.mp4 name that its Download then refuses
# (home-assistant/core#179099). Native names are hour-long segments, so
# NATIVE_FLV streams from the clip's offset into the segment, and
# NATIVE_DOWNLOAD fetches the segment once and cuts every clip it holds out
# of it.
NATIVE_FLV: Final = "NATIVE_FLV"
NATIVE_DOWNLOAD: Final = "NATIVE_DOWNLOAD"
NATIVE_ROUTES: Final = (NATIVE_FLV, NATIVE_DOWNLOAD)
# Fetching a whole segment takes longer than one clip.
NATIVE_DOWNLOAD_TIMEOUT: Final = 600
# Segments are kept briefly for the other clips they hold, then deleted.
SEGMENTS_DIR_NAME: Final = "segments"
SEGMENTS_KEPT: Final = 2
SEGMENT_KEEP_MINUTES: Final = 30
# A segment still being recorded is only reused if fetched this long after
# the clip ended.
SEGMENT_SETTLE_SECONDS: Final = 15

# Abandon a sweep once this many clips fail back to back.
CONSECUTIVE_FAILURE_LIMIT: Final = 3

# After a sweep gives up on a camera, leave that camera alone for this long
# (minutes, growing with each failed sweep, reset by any success). An NVR that
# is refusing downloads only gets busier if it is asked again every 2 minutes.
CAMERA_BACKOFF_MINUTES: Final = (10, 20, 40, 80, 120)

# A clip that failed this many sweeps is skipped until a manual sweep_now.
CLIP_FAILURE_LIMIT: Final = 3

# ── Timing ───────────────────────────────────────────────────────────────

# Delay after a detection sensor clears before sweeping for the new clip. The
# NVR only finalises the recording once the event has ended.
EVENT_SETTLE_DELAY: Final = 20

# Never sweep the same camera more often than this, however many events fire.
SWEEP_COOLDOWN: Final = timedelta(seconds=15)

# Reolink NVRs serve a limited number of playback sessions and get unhappy when
# several VOD downloads overlap, so clips are pulled one at a time.
MAX_CONCURRENT_DOWNLOADS: Final = 1

# The Reolink playback proxy forwards these headers straight on to the NVR,
# which is fussy about what it receives. The same proxy serves the media
# browser happily, so send something close to what a browser sends rather than
# aiohttp's defaults (notably no gzip, and not a Python user agent).
DOWNLOAD_HEADERS: Final = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "User-Agent": "Mozilla/5.0 (compatible; HomeAssistant reolink_clip_cache)",
    # Never reuse a pooled connection: an NVR that hung up on the previous
    # transfer is the classic source of "Server disconnected".
    "Connection": "close",
}

# Talking to the NVR directly, where there is no proxy to imitate a browser for.
DIRECT_HEADERS: Final = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

# A ranged or chunked reply is still a good reply.
OK_STATUSES: Final = (200, 206)

# Cap the work one sweep takes on. Descriptors are newest-first, so a large
# backfill still caches the most recent clips first and finishes over a few
# sweeps instead of blocking everything else for minutes.
MAX_CLIPS_PER_SWEEP: Final = 25
DOWNLOAD_CHUNK_SIZE: Final = 64 * 1024
DOWNLOAD_TIMEOUT: Final = 180
FFMPEG_TIMEOUT: Final = 60

# How long a signed clip URL handed to the card stays valid.
SIGNED_URL_TTL: Final = 1800

# Seconds to wait before caching a clip someone just played from the NVR, so the
# download does not compete with their stream for the NVR's playback sessions.
ON_DEMAND_CACHE_DELAY: Final = 90

# ── Storage ──────────────────────────────────────────────────────────────

STORAGE_DIR_NAME: Final = "reolink_clip_cache"
CLIPS_DIR_NAME: Final = "clips"
THUMBS_DIR_NAME: Final = "thumbs"

STORAGE_KEY: Final = f"{DOMAIN}.index"
STORAGE_VERSION: Final = 2

# ── HTTP ─────────────────────────────────────────────────────────────────

URL_BASE: Final = f"/api/{DOMAIN}"
CLIP_URL: Final = f"{URL_BASE}/clip"
THUMB_URL: Final = f"{URL_BASE}/thumb"

CARD_FILENAME: Final = "reolink-clips-card.js"
CARD_URL: Final = f"/{DOMAIN}/{CARD_FILENAME}"

# ── WebSocket commands ───────────────────────────────────────────────────

WS_CAMERAS: Final = f"{DOMAIN}/cameras"
WS_DATES: Final = f"{DOMAIN}/dates"
WS_CLIPS: Final = f"{DOMAIN}/clips"
WS_RESOLVE: Final = f"{DOMAIN}/resolve"
WS_STATUS: Final = f"{DOMAIN}/status"

# ── Events / signals ─────────────────────────────────────────────────────

EVENT_CLIP_CACHED: Final = f"{DOMAIN}_clip_cached"
SIGNAL_INDEX_UPDATED: Final = f"{DOMAIN}_index_updated"

# ── Services ─────────────────────────────────────────────────────────────

SERVICE_PURGE_CACHE: Final = "purge_cache"
SERVICE_REFRESH_CACHE: Final = "refresh_cache"
SERVICE_SWEEP_NOW: Final = "sweep_now"
SERVICE_DIAGNOSE: Final = "diagnose"
