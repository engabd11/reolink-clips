#!/usr/bin/env python3
"""Check that the config and options forms can always be submitted.

A default event type the form does not offer (such as "pet") stays selected
but invisible, and Home Assistant then refuses the whole form with
"value must be one of [...]". This builds the real schema and validates it
the way Home Assistant does.

    pip install homeassistant && python tests/test_config_flow_event_types.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.reolink_clip_cache import config_flow as flow
from custom_components.reolink_clip_cache.const import CONF_EVENT_TYPES, DEFAULT_EVENT_TYPES

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    """Record one assertion."""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


offered = set(flow.EVENT_TYPE_VALUES)
check("every default event type is offered in the form", set(DEFAULT_EVENT_TYPES) <= offered, str(DEFAULT_EVENT_TYPES))

cameras = [{"value": "back_door", "label": "BACK DOOR"}]
schema = flow._schema({}, cameras)

# Submitting the form untouched uses the defaults: it must validate.
try:
    data = schema({})
    check("the untouched form validates", True)
except Exception as err:  # noqa: BLE001
    check("the untouched form validates", False, str(err))

# Ticking only Person (what the user did) must validate.
try:
    data = schema({CONF_EVENT_TYPES: ["person"]})
    check("only Person validates", data[CONF_EVENT_TYPES] == ["person"], str(data))
except Exception as err:  # noqa: BLE001
    check("only Person validates", False, str(err))

# An entry saved by an older version with "pet" opens and saves.
old = flow._schema({CONF_EVENT_TYPES: ["person", "pet"]}, cameras)
try:
    data = old({})
    check("an older entry with pet validates", "pet" not in data[CONF_EVENT_TYPES], str(data))
except Exception as err:  # noqa: BLE001
    check("an older entry with pet validates", False, str(err))

check("pet maps to animal", flow.clean_event_types(["person", "pet"]) == ["person", "animal"], str(flow.clean_event_types(["person", "pet"])))
check("unknown values are dropped", flow.clean_event_types(["person", "motion", "bogus"]) == ["person"])
check("an empty choice falls back to the defaults", flow.clean_event_types([]) == [v for v in DEFAULT_EVENT_TYPES if v in offered])

print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
