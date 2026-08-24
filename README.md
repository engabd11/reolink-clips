# Reolink Clip Cache

**Instant playback for your Reolink NVR event clips in Home Assistant.**

[![Validate](https://github.com/engabd11/reolink-clips/actions/workflows/validate.yml/badge.svg)](https://github.com/engabd11/reolink-clips/actions/workflows/validate.yml)

## The problem

Playing a recorded clip from a Reolink NVR through Home Assistant takes 20–30 seconds,
and sometimes fails outright until you refresh the card a few times. Every play makes the
NVR seek through its continuous recording, remux the segment and stream it back through a
proxy — from scratch, every single time.

## The solution

This integration watches your NVR for new Person / Vehicle / Animal recordings and pulls
them into local storage ahead of time, with the MP4 index moved to the front so a browser
can start playing on the first chunk. The companion card then plays from local storage.

```
Reolink NVR finishes writing an event recording
         │
         ▼
Sweep spots it in the media source (every 2 min, or ~20 s after a detection ends)
         │
         ▼
Downloaded → ffmpeg faststart → poster frame → indexed
         │
         ▼
Card plays it over an authenticated local URL — no NVR round trip
         (uncached clips still play from the NVR, and cache themselves for next time)
```

## Features

- **Cache-first playback** — one WebSocket call returns a whole day of clips with local
  URLs and thumbnails already attached
- **Correct clip matching** — recordings are identified by the start/end times the Reolink
  media source encodes, not by guessing from when a sensor fired
- **Automatic camera discovery** — cameras and their detection sensors are found through
  the Reolink integration; nothing to name by hand
- **Private storage** — clips live outside `www/` and are served over authenticated,
  short-lived signed URLs
- **Thumbnail filmstrip** — scrub the day at a glance, click any frame to jump to it
- **Live updates** — the card refreshes itself when a new clip is cached
- **Retention by age and size** — oldest clips are evicted once either limit is hit
- **Graceful fallback** — an uncached clip plays from the NVR and caches in the background

## Requirements

- Home Assistant 2024.11 or newer
- The Reolink integration set up, with an NVR that has a working hard disk
- ffmpeg (bundled with Home Assistant OS, Container and Supervised)

## Installation

### HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/engabd11/reolink-clips` with category **Integration**
3. Install **Reolink Clip Cache** and restart Home Assistant

### Manual

Copy `custom_components/reolink_clip_cache/` into your `<config>/custom_components/`
directory and restart Home Assistant.

## Setup

1. **Settings → Devices & Services → Add Integration → Reolink Clip Cache**
2. Pick the event types to cache, the stream resolution and the retention limits
3. That's it — cameras are discovered automatically

The dashboard card is registered by the integration itself, so there is **no dashboard
resource to add**. Add a card, search for *Reolink Clips Card*, and configure it in the
visual editor.

### Card options

Everything is optional; the defaults work.

```yaml
type: custom:reolink-clips-card
title: Events
cameras: [carport, back_door]   # omit for every discovered camera
default_event_type: all         # all | person | vehicle | animal | package | visitor
thumbnails: true                # thumbnail filmstrip under the player
autoplay: false                 # play the newest clip on load
```

| Option | Default | Description |
|---|---|---|
| `title` | `Events` | Card heading |
| `cameras` | all | Camera keys to show, in tab order. Camera names also work. |
| `default_event_type` | `all` | Filter selected when the card loads |
| `thumbnails` | `true` | Show the filmstrip and prefetch adjacent clips |
| `autoplay` | `false` | Start the newest clip automatically |

### Integration options

| Option | Default | Description |
|---|---|---|
| Cameras | all | Which cameras to cache. Leave empty for every discovered camera. |
| Event types | Person, Vehicle, Animal | What to cache. Motion is not offered — it fires far too often to be worth caching. |
| Stream | Low resolution | `sub` caches fast and small; `main` is the full-quality recording. **If every download fails, try the other one** — some NVRs will not serve downloads for one of the streams. |
| Keep clips for | 7 days | Age limit |
| Maximum cache size | 2048 MB | Size ceiling; oldest clips are evicted first |
| Check for new clips every | 2 min | Sweep interval. A detection also triggers a check ~20 s after the event ends. |

## Services

| Service | Description |
|---|---|
| `reolink_clip_cache.sweep_now` | Look for new recordings immediately. Takes optional `camera` and `days` (for backfilling earlier days). |
| `reolink_clip_cache.purge_cache` | Apply the age and size limits now |
| `reolink_clip_cache.refresh_cache` | Re-discover cameras and reconcile the index with the disk |

## Storage and privacy

Clips are stored in `<config>/reolink_clip_cache/` — deliberately **not** in
`<config>/www/`. Anything under `www/` is served at `/local/...` to anyone who can reach
your Home Assistant, with no login. This integration serves clips from an authenticated
endpoint instead, and the card is handed short-lived signed URLs.

Rough sizing at low resolution: ~2 MB per clip, so 5 events/day across 4 cameras is about
40 MB/day, or ~280 MB at the default 7-day retention.

## Sensors

- **Cache size** — megabytes currently held
- **Cached clips** — number of clips, with a per-camera breakdown and the most recent clip
  in its attributes

## What this is not

- Not a replacement for your NVR recordings — it is a cache in front of them
- Not a Frigate replacement — it uses your Reolink cameras' own detection
- Not a re-encoder — the stream is copied as-is, only the MP4 index is moved

## Upgrading from 1.x

Version 2.0 is a rewrite; 1.x never actually served anything from its cache.

- Cached clips moved from `<config>/www/reolink_cache/` to `<config>/reolink_clip_cache/`.
  **Delete the old `www/reolink_cache/` folder** — nothing reads it any more, and it is
  publicly readable.
- The card no longer needs a dashboard resource entry. Remove the old
  `/local/reolink-clips-card.js` resource, and delete that file from `www/`.
- Card config changed: `sensors:` and `resolution:` are gone (both are discovered or set
  in the integration options). A 1.x `cameras:` list is still understood — camera names
  are matched to discovered cameras.
- The `reolink_clip_cache/browse` and `.../thumbnail` WebSocket commands were replaced by
  `cameras`, `dates`, `clips`, `resolve` and `status`.
- Your existing config entry migrates automatically; `resolution: clear` becomes the
  high-resolution stream, anything else becomes low resolution.

## Troubleshooting

Turn on debug logging:

```yaml
logger:
  logs:
    custom_components.reolink_clip_cache: debug
```

- **No cameras discovered** — the Reolink integration must be loaded and the NVR needs a
  working hard disk; cameras without playback support are skipped by the media source.
- **Clips never cache** — call `reolink_clip_cache.sweep_now` and check the log. Verify the
  chosen event types actually appear as folders under the day in Media → Reolink.
- **Clips play but slowly** — they are not cached yet. The `CACHED` badge and the green dot
  on a thumbnail tell you which clips are local.
- **`Clip download ... Server disconnected`** — the NVR hung up. Clips are fetched from the
  NVR directly when possible and via Home Assistant's proxy otherwise, retried with a
  backoff, and picked up again on later sweeps. The log names which route failed. If it
  happens for every clip, switch the stream option to the other resolution: some NVRs
  refuse to serve downloads for one of them. Reducing how often the sweep runs also helps
  a busy NVR.

## Credits

Built by [engabd11](https://github.com/engabd11). Earthy Dark card design carried over from
the original Reolink Clips Card.
