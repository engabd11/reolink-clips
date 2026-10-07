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
theme: dark                     # dark | coffee | dark-neon | soft-dark | onyx
view: phone                     # phone | tablet (large touchscreens)
title: Events
cameras: [carport, back_door]   # omit for every discovered camera
default_event_type: all         # all | person | vehicle | animal | package | visitor (doorbell)
thumbnails: true                # thumbnail filmstrip under the player
autoplay: false                 # play the newest clip on load
tab_labels: auto                # auto | names | icons
overlay: always                 # always | hide_fullscreen | never
camera_icons:                   # optional: your own icon per camera
  carport: mdi:garage
```

| Option | Default | Description |
|---|---|---|
| `theme` | `dark` | `dark` is the CAMusic OLED look, `coffee` the warm espresso look, `dark-neon` the blue-black glass with cyan glow, `soft-dark` neutral black with cream text and muted earth colours, `onyx` the dark Weather Advisor look (navy onyx glass, taupe, sand and moss), matching [Cyborg Cards](https://github.com/engabd11/cyborg-cards) |
| `background` | `glow` | Onyx only: `glow` keeps its soft glow and grain, `solid` turns them off |
| `view` | `phone` | `tablet` for wall and kitchen touchscreens: bigger buttons and text, and on a wide card the player on the left with the date, filters and a scrolling list of the clips (thumbnail, event, time and length) beside it |
| `title` | `Events` | Card heading |
| `cameras` | all | Camera keys to show, in tab order. Camera names also work. |
| `default_event_type` | `all` | Filter selected when the card loads |
| `thumbnails` | `true` | Show the filmstrip and prefetch adjacent clips |
| `autoplay` | `false` | Start the newest clip automatically |
| `tab_labels` | `auto` | Camera tabs sit on one row. `auto` shows names while they fit and icons when they would not; `names` or `icons` fixes one |
| `overlay` | `always` | The clip details over the top of the video (event, time, cached). `hide_fullscreen` keeps them off fullscreen video, `never` hides them. The date and time on the right are dropped on a narrow player |
| `camera_icons` | guessed | An icon per camera key. Otherwise it is guessed from the camera's name (door, doorbell, carport, yard, alley, gate…), falling back to a CCTV icon |

### Playback

- A cached clip starts at once from local storage.
- A clip that is not cached yet streams from the NVR when you press Play, so browsing the list never ties up the NVR. It caches itself shortly after, so the next play is instant.
- On an NVR that Home Assistant's own player cannot stream from (see Troubleshooting), Play fetches the clip into the cache instead: the card shows *Fetching this clip from the NVR* and plays it the moment it is ready.
- Fullscreen uses the same player, so nothing downloads twice.
- The speaker button mutes and unmutes; the choice is remembered on that device.
- Zoom with the mouse wheel or a two-finger pinch (up to 5×) and drag to look around. Tap the zoom chip to go back to 1×. Changing clip resets the zoom.
- The header shows how many clips are cached for the card's cameras, and how many the day on show has.
- If a clip will not play, the card says why and offers Try again. A clip that downloads but will not decode is almost always H.265: set the integration to the low resolution stream.

### Integration options

| Option | Default | Description |
|---|---|---|
| Cameras | all | Which cameras to cache. Leave empty for every discovered camera. |
| Event types | Person, Vehicle, Animal | What to cache. **Doorbell press** is a doorbell's ring (Reolink's visitor event). Motion is not offered — it fires far too often to be worth caching. |
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
| `reolink_clip_cache.diagnose` | Try every download route against one real recording and report what each did. Returns a response — run it from Developer Tools → Actions. |

## Storage and privacy

Clips are stored in `<config>/reolink_clip_cache/` — deliberately **not** in
`<config>/www/`. Anything under `www/` is served at `/local/...` to anyone who can reach
your Home Assistant, with no login. This integration serves clips from an authenticated
endpoint instead, and the card is handed short-lived signed URLs.

Rough sizing at low resolution: ~2 MB per clip, so 5 events/day across 4 cameras is about
40 MB/day, or ~280 MB at the default 7-day retention.

## Sensors and buttons

- **Cache size** — megabytes currently held
- **Cached clips** — number of clips, with a per-camera breakdown and the most recent clip
  in its attributes
- **Sweep now** — look for clips across every day the cache keeps, straight away, even for
  a camera paused after failed downloads
- **Clear cache** — delete every cached clip and thumbnail. The next sweep caches them again

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
  backoff, and picked up again on later sweeps. The log names which route failed. NVRs
  differ in which VOD request types they will serve, and a refusal arrives as a dropped
  connection rather than a useful error, so the integration tries the Reolink library's
  own download, then `Download`, `NvrDownload`, `FLV` and `Playback` in turn (the first
  three also over the NVR's plain HTTP port when Home Assistant talks to it over HTTPS),
  and sticks with whichever works. **Run `reolink_clip_cache.diagnose`** to see exactly what each
  route does for one real recording. Switching the stream option to the other
  resolution is also worth a try.
- **Only the `FLV` route works** — some NVRs (an RLN8-410 on firmware 3.6.5, for one)
  refuse every MP4 download, Home Assistant's own media browser included, but still
  serve the FLV playback stream their web client uses. Clips are then recorded from that
  stream, at about real time, and remuxed to MP4 with ffmpeg, so a 30 second clip takes
  about 30 seconds to cache. Home Assistant cannot play these clips from the NVR either,
  so pressing Play on an uncached clip caches it straight away, and the card picks it up
  when it is ready.

## Credits

Built by [engabd11](https://github.com/engabd11). Earthy Dark card design carried over from
the original Reolink Clips Card.
