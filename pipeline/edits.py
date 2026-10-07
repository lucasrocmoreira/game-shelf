"""Your own changes to the library: hidden games, played flags, manually added games and match fixes.

Stored in config/library_edits.json. The web app writes this file directly (when you connect it to
GitHub in its settings), the "Add or hide a game" issue form writes it too, and you can edit it by hand.
"""
import json

import yaml

from . import titles
from .util import CONFIG_DIR, now_iso

PATH = CONFIG_DIR / "library_edits.json"
LEGACY_PATH = CONFIG_DIR / "library_edits.yml"

EMPTY = {"hidden": {}, "unhidden": {}, "played": {}, "added": [], "match": {}}


def source_key(title: str) -> str:
    """Key for a store title, used by match fixes."""
    return titles.key(titles.clean(title))


def load() -> dict:
    data = {}
    if PATH.exists():
        try:
            data = json.loads(PATH.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            data = {}
    out = {k: (data.get(k) if isinstance(data.get(k), type(v)) else type(v)()) for k, v in EMPTY.items()}

    # Older versions kept a YAML file with "added" and "hidden" lists of titles.
    if LEGACY_PATH.exists():
        legacy = yaml.safe_load(LEGACY_PATH.read_text(encoding="utf-8")) or {}
        for g in legacy.get("added") or []:
            if isinstance(g, dict) and g.get("title") and not any(
                titles.key(a.get("title", "")) == titles.key(g["title"]) for a in out["added"]
            ):
                out["added"].append(g)
        for t in legacy.get("hidden") or []:
            out["hidden"].setdefault("t:" + titles.key(titles.clean(str(t))), str(t))
    return out


def save(data: dict):
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def hide(data, key, title):
    data["unhidden"].pop(key, None)
    data["hidden"][key] = title


def add(data, title, platform="Switch", fmt=None, hours=None):
    entry = {"title": title, "platform": platform, "added": now_iso()[:10]}
    if fmt:
        entry["format"] = fmt
    if hours:
        entry["hours"] = hours
    data["added"] = [
        g for g in data["added"] if not (titles.key(g.get("title", "")) == titles.key(title) and g.get("platform") == platform)
    ] + [entry]
