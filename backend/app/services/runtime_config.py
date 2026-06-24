"""Runtime-editable operational settings.

A thin override layer on top of the static ``.env``-backed
:class:`~app.config.Settings`, for the few knobs an admin needs to change
*without* a restart — currently which trial categories get LLM criteria
parsing, and the per-sync parse cap.

Overrides persist to a small JSON file (``backend/runtime_config.json``)
so they survive restarts and are shared across the web + worker
processes.  When a key is absent we fall back to the Settings default.
This deliberately covers only operational, non-secret toggles — secrets
and infrastructure config stay in ``.env``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# backend/app/services/runtime_config.py -> parents[2] == backend/
_OVERRIDE_PATH = Path(__file__).resolve().parents[2] / "runtime_config.json"

# Only these keys may be overridden at runtime.
_ALLOWED_KEYS = {"trial_parse_categories", "trial_parse_max_per_sync"}

_lock = threading.Lock()


def _read() -> dict[str, Any]:
    """Load the override file (no locking — callers hold ``_lock``)."""
    try:
        return json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        logger.warning("runtime_config.json is malformed; ignoring overrides.")
        return {}


def get_overrides() -> dict[str, Any]:
    """Return the currently-persisted overrides (a copy)."""
    with _lock:
        return dict(_read())


def set_overrides(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the persisted overrides and return the result.

    Unknown keys are ignored so callers can't smuggle arbitrary settings
    through this surface.
    """
    with _lock:
        data = _read()
        for key, value in updates.items():
            if key in _ALLOWED_KEYS:
                data[key] = value
        _OVERRIDE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Runtime config updated: %s", {k: data.get(k) for k in updates})
        return dict(data)


# ── Effective accessors (override → Settings default) ──────────────────

def parse_categories() -> list[str]:
    """Categories eligible for LLM criteria parsing.  Empty list = all."""
    ov = get_overrides()
    if "trial_parse_categories" in ov and ov["trial_parse_categories"] is not None:
        return list(ov["trial_parse_categories"])
    return get_settings().trial_parse_categories


def parse_max_per_sync() -> int:
    """Hard cap on trials parsed per sync run.  0 = no cap."""
    ov = get_overrides()
    if "trial_parse_max_per_sync" in ov and ov["trial_parse_max_per_sync"] is not None:
        return int(ov["trial_parse_max_per_sync"])
    return get_settings().TRIAL_PARSE_MAX_PER_SYNC
