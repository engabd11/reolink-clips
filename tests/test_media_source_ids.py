#!/usr/bin/env python3
"""Check media source id handling against real Home Assistant objects.

The Reolink media source's own identifiers look like ``CAM|entry|0``, but what
a browse result hands back is the full URI ``media-source://reolink/CAM|entry|0``
— and ``async_browse_media`` only accepts that URI form. Getting this wrong
silently finds no cameras and caches nothing.

The tree here is built with HA's own ``BrowseMediaSource``, so the id format
comes from Home Assistant rather than from an assumption in this file.

    pip install homeassistant && python tests/test_media_source_ids.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from homeassistant.components.media_source.models import BrowseMediaSource
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.reolink_clip_cache import coordinator as mod

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def node(identifier, title, children=None, can_play=False):
    """Build a browse node the way the Reolink media source does."""
    return BrowseMediaSource(
        domain="reolink",
        identifier=identifier,
        media_class="directory",
        media_content_type="playlist",
        title=title,
        can_play=can_play,
        can_expand=not can_play,
        children=children,
    )


NVR, DOORBELL = "nvrentryid", "doorbellentryid"
FILE_EARLY = f"FILE|{NVR}|1|sub|rec_a.mp4|20260824141203|20260824141233"
FILE_LATE = f"FILE|{NVR}|1|sub|rec_b.mp4|20260824151000|20260824151030"

ROOT = node(None, "Reolink", [
    node(f"CAM|{NVR}|0", "BACK DOOR"),
    node(f"CAM|{NVR}|1", "Carport"),
    node(f"CAM|{DOORBELL}|0", "Doorbell"),
])
STREAM = node(f"RES|{NVR}|1|sub", "Carport Low res.", [
    node(f"DAY|{NVR}|1|sub|2026|8|23", "2026/8/23"),
    node(f"DAY|{NVR}|1|sub|2026|8|24", "2026/8/24"),
])
DAY = node(f"DAY|{NVR}|1|sub|2026|8|24", "day", [
    node(f"EVE|{NVR}|1|sub|2026|8|24|PERSON", "Person"),
    node(f"EVE|{NVR}|1|sub|2026|8|24|MOTION", "Motion"),
])
PERSON = node(f"EVE|{NVR}|1|sub|2026|8|24|PERSON", "Person", [
    node(FILE_EARLY, "14:12:03 0:00:30 Person", can_play=True),
    node(FILE_LATE, "15:10:00 0:00:30 Person", can_play=True),
])
MOTION = node(f"EVE|{NVR}|1|sub|2026|8|24|MOTION", "Motion", [
    node(f"FILE|{NVR}|1|sub|rec_c.mp4|20260824160000|20260824160030", "16:00:00 0:00:30 Motion", can_play=True),
])

TREE = {item.media_content_id: item for item in (ROOT, STREAM, DAY, PERSON, MOTION)}
REQUESTED: list[str] = []


async def fake_browse(_hass, media_content_id):
    """Serve the tree, rejecting anything that is not a valid media source URI."""
    REQUESTED.append(media_content_id)
    if media_content_id not in TREE:
        raise ValueError(f"not browsable: {media_content_id!r}")
    return TREE[media_content_id]


def registry_entity(unique_id, entity_id):
    """A minimal entity registry entry."""
    entry = types.SimpleNamespace()
    entry.unique_id, entry.entity_id = unique_id, entity_id
    entry.domain, entry.disabled, entry.device_id = "binary_sensor", False, None
    return entry


ENTITIES = {
    NVR: [
        registry_entity("aa:bb:cc:dd:ee:ff_0_person", "binary_sensor.back_door_person"),
        registry_entity("aa:bb:cc:dd:ee:ff_1_person", "binary_sensor.carport_person"),
        registry_entity("aa:bb:cc:dd:ee:ff_1_pet", "binary_sensor.carport_pet"),
    ],
    DOORBELL: [registry_entity("11:22:33:44:55:66_0_visitor", "binary_sensor.doorbell_visitor")],
}


def build_coordinator():
    """Assemble a coordinator with just enough around it to browse."""
    tmp = tempfile.mkdtemp()
    hass = types.SimpleNamespace(
        data={},
        config=types.SimpleNamespace(path=lambda *p: str(pathlib.Path(tmp).joinpath(*p))),
        config_entries=types.SimpleNamespace(
            async_entries=lambda domain: [
                types.SimpleNamespace(entry_id=NVR),
                types.SimpleNamespace(entry_id=DOORBELL),
            ]
        ),
    )
    entry = types.SimpleNamespace(
        entry_id="test",
        options={
            "event_types": ["person", "vehicle", "animal"],
            "stream": "sub",
            "cache_days": 7,
            "max_cache_size_mb": 100,
            "sweep_minutes": 2,
        },
    )
    coordinator = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
    coordinator.hass, coordinator.entry = hass, entry
    coordinator._cameras, coordinator._index = {}, {}
    coordinator._unsub_detection, coordinator._unsubs = None, []
    # Detection listeners need a real event bus; this test is about ids.
    coordinator._restart_listeners = lambda: None
    return coordinator


def main() -> int:
    """Run the checks."""
    print(f"Home Assistant {HA_VERSION}")
    print(f"browse id as HA builds it: {ROOT.children[0].media_content_id}\n")

    mod.async_browse_media = fake_browse
    er.async_get = lambda hass: None
    dr.async_get = lambda hass: None
    er.async_entries_for_config_entry = lambda reg, entry_id: ENTITIES.get(entry_id, [])
    mod.er, mod.dr = er, dr

    coordinator = build_coordinator()

    print("== discovery ==")
    asyncio.run(coordinator.async_discover())
    cameras = coordinator.cameras
    check("cameras discovered", set(cameras) == {"back_door", "carport", "doorbell"}, str(sorted(cameras)))
    check("channel parsed", cameras.get("carport") and cameras["carport"].channel == "1")
    check("config entry parsed", cameras.get("carport") and cameras["carport"].entry_id == NVR)
    check("detection sensors joined on", cameras.get("carport") and "person" in cameras["carport"].sensors)
    if not cameras.get("carport"):
        print("\nFAILED EARLY")
        return 1

    print("\n== manual camera selection ==")
    for label, selection, expected in (
        ("only the selected camera is cached", ["carport"], {"carport"}),
        ("an empty selection still means every camera", [], {"back_door", "carport", "doorbell"}),
        ("an unknown selection yields nothing rather than everything", ["nope"], set()),
    ):
        other = mod.ReolinkClipCacheCoordinator.__new__(mod.ReolinkClipCacheCoordinator)
        other.hass = coordinator.hass
        other.entry = types.SimpleNamespace(
            entry_id="test",
            options={**coordinator.entry.options, "cameras": selection},
        )
        other._cameras, other._index = {}, {}
        other._unsub_detection, other._unsubs = None, []
        other._restart_listeners = lambda: None
        asyncio.run(other.async_discover())
        check(label, set(other.cameras) == expected, str(sorted(other.cameras)))

    print("\n== clip listing ==")
    REQUESTED.clear()
    clips = asyncio.run(coordinator.async_list_day(cameras["carport"], dt.date(2026, 8, 24)))
    check("browsed only valid media source URIs",
          all(item.startswith("media-source://reolink/") for item in REQUESTED), str(REQUESTED))
    check("clips parsed", len(clips) == 2, f"{len(clips)} clips")
    check("motion excluded", all("motion" not in clip["event_types"] for clip in clips))
    check("newest first with real start times", clips and clips[0]["start"].startswith("2026-08-24T15:10:00"),
          clips[0]["start"] if clips else "no clips")
    check("media_content_id stored in URI form",
          clips and clips[0]["media_content_id"].startswith("media-source://reolink/"))

    print("\n== dates ==")
    REQUESTED.clear()
    dates = asyncio.run(coordinator.async_dates("carport"))
    check("dates listed newest first", [d["date"] for d in dates] == ["2026-08-24", "2026-08-23"], str(dates))
    check("dates browsed by URI", all(item.startswith("media-source://reolink/") for item in REQUESTED))

    print("\n== clip ids ==")
    check("same id whichever form is passed",
          mod.clip_id_for(FILE_EARLY) == mod.clip_id_for(f"media-source://reolink/{FILE_EARLY}"))

    print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
