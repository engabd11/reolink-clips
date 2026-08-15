"""Constants for the Reolink Clip Cache integration."""

DOMAIN = "reolink_clip_cache"

# Event types we cache (not motion)
CACHEABLE_EVENT_TYPES = ["Person", "Vehicle", "Animal"]

# Default settings
DEFAULT_CACHE_DAYS = 7
DEFAULT_RESOLUTION = "low"

# WebSocket commands
WS_BROWSE = "reolink_clip_cache/browse"
WS_RESOLVE = "reolink_clip_cache/resolve"
WS_THUMBNAIL = "reolink_clip_cache/thumbnail"
WS_STATUS = "reolink_clip_cache/status"

# Custom events
EVENT_CLIP_CACHED = f"{DOMAIN}_clip_cached"

# Media source prefix
REOLINK_MEDIA_PREFIX = "media-source://reolink"

# File naming pattern: {camera}_{event_type}_{timestamp}.mp4
CLIP_FILENAME_PATTERN = "{camera}_{event_type}_{timestamp}.mp4"
META_FILENAME_PATTERN = "{camera}_{event_type}_{timestamp}.json"

# Cache directory (under HA www for static file serving)
CACHE_DIR_NAME = "reolink_cache"

# Platforms
PLATFORMS = ["sensor"]