#!/usr/bin/env python3
"""Import every module of the integration against a real Home Assistant.

A name that exists in an HA submodule but is not re-exported from its package
imports fine nowhere and fails only at runtime, where it surfaces as the
unhelpful "Config flow could not be loaded: Invalid handler specified". This
catches that in CI instead.

    pip install homeassistant && python scripts/check_imports.py
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "custom_components.reolink_clip_cache"


def main() -> int:
    """Import every module, then confirm the config flow handler registered."""
    sys.path.insert(0, str(ROOT))

    component = ROOT / "custom_components" / "reolink_clip_cache"
    modules = [PACKAGE] + sorted(
        f"{PACKAGE}.{path.stem}"
        for path in component.glob("*.py")
        if path.stem != "__init__"
    )

    failed = 0
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
        else:
            print(f"  OK    {name}")

    from homeassistant.config_entries import HANDLERS
    from homeassistant.const import __version__

    from custom_components.reolink_clip_cache.const import DOMAIN

    print(f"\nHome Assistant {__version__}")

    if DOMAIN in HANDLERS:
        print(f"config flow handler registered for {DOMAIN!r}")
    else:
        print(f"config flow handler NOT registered for {DOMAIN!r}")
        failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
