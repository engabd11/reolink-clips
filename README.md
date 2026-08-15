# Reolink Clip Cache

**Instant playback for your Reolink NVR event clips in Home Assistant.**

## The Problem

Viewing recorded clips from a Reolink NVR through Home Assistant takes 20+ seconds per clip. The NVR has to seek through continuous recordings, remux the segment, and serve it over HTTP — every single time you click play.

## The Solution

This integration **pre-caches event clips** (Person, Vehicle, Animal) to local storage when your Reolink cameras detect them. When you browse clips on your dashboard, cached clips load in **1-2 seconds** instead of 20+.

### How It Works

```
Reolink detects person/vehicle/animal
         │
         ▼
HA binary_sensor triggers ON
         │
         ▼
Integration downloads clip from NVR → ffmpeg faststart → saves locally
         │
         ▼
Card plays from /local/reolink_cache/ → INSTANT playback
         (falls back to NVR if not yet cached)
```

## Features

- **Cache-first playback** — Card tries local cache before hitting NVR API
- **Automatic caching** — Clips downloaded on detection events (Person, Vehicle, Animal only)
- **Faststart optimization** — `ffmpeg -moov_to_start` on every clip for instant seek
- **Auto-purge** — Configurable retention (default 7 days)
- **WebSocket API** — Custom endpoints for the card to query cached clips
- **Graceful fallback** — If a clip isn't cached yet, falls back to NVR (same 20s, but only for very recent events)
- **Cache badge** — Green "CACHED" indicator on clips played from local storage
- **Status sensor** — Monitor cache size and clip counts

## Installation

### Via HACS (Recommended)

1. Add this repository as a custom repository in HACS
2. Install "Reolink Clip Cache"
3. Restart Home Assistant

### Manual

1. Copy `custom_components/reolink_clip_cache/` to your `<config>/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Reolink Clip Cache**
3. Configure retention days and resolution
4. The integration auto-discovers your Reolink cameras

## Dashboard Card

Install the companion card alongside the integration. Add this to your dashboard resources:

```yaml
resources:
  - url: /local/reolink-clips-card.js
    type: module
```

Then add the card with cache support enabled:

```yaml
type: custom:reolink-clips-card
cameras:
  - name: BACK DOOR
    sensors:
      person: binary_sensor.back_door_person
      vehicle: binary_sensor.back_door_vehicle
      animal: binary_sensor.back_door_animal
  - name: Carport
    sensors:
      person: binary_sensor.carport_person
      vehicle: binary_sensor.carport_vehicle
      animal: binary_sensor.carport_animal
  - name: Doorbell
    sensors:
      person: binary_sensor.doorbell_person
      visitor: binary_sensor.doorbell_visitor
      package: binary_sensor.doorbell_package
      vehicle: binary_sensor.doorbell_vehicle
resolution: low
cache_enabled: true    # ← Enables instant playback from local cache
```

### New Config Option

| Option | Default | Description |
|--------|---------|-------------|
| `cache_enabled` | `false` | Enable cache-first clip loading |

When enabled, the card queries the integration's WebSocket API before falling back to the NVR. Cached clips show a green **CACHED** badge.

## Services

| Service | Description |
|---------|-------------|
| `reolink_clip_cache.purge_cache` | Manually purge old clips |
| `reolink_clip_cache.refresh_cache` | Rebuild cache index from disk |

## How It Doesn't Work

- ❌ Does NOT replace your NVR recordings — it's a cache layer
- ❌ Does NOT require Frigate — uses your Reolink NVR's existing detection
- ❌ Does NOT cache motion clips — only Person, Vehicle, Animal
- ❌ Does NOT re-encode — copies stream as-is, just adds faststart

## Storage

- Clips stored in `/config/www/reolink_cache/`
- Typical: ~5 events/day × 4 cameras × ~2MB per clip = ~40MB/day
- With 7-day retention: ~280MB total
- Configurable retention period

## Requirements

- Home Assistant 2024.1+
- Reolink integration configured with media source
- ffmpeg (included with Home Assistant)

## Credits

Built by [engabd11](https://github.com/engabd11). Earthy Dark card design inspired by the original Reolink Clips Card v4.3.