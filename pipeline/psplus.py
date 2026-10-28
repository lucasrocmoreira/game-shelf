"""PlayStation Plus catalogs: the Game Catalog (Extra and Premium) and the Classics Catalog (Premium).

Sony's PS Plus pages on playstation.com load their game lists from a public JSON endpoint.
If that is unreachable from GitHub's servers, a public mirror that copies the same lists every
day (github.com/koddsson/ps-plus-games-list) is used instead. If both fail, the last list is kept.
"""
import re

import requests

from . import titles
from .util import log

LISTS = {
    "Game Catalog": "plus-games-list",       # Extra and Premium
    "Classics": "plus-classics-list",        # Premium only
}
DIRECT = "https://www.playstation.com/bin/imagic/gameslist?locale=en-us&categoryList={}"
MIRROR = "https://raw.githubusercontent.com/koddsson/ps-plus-games-list/main/data/{}.json"
MINIMUM = {"Game Catalog": 100, "Classics": 30}
HEADERS = {"User-Agent": "Mozilla/5.0 (GameShelf personal library; https://github.com/lucasrocmoreira/game-shelf)",
           "Accept": "application/json"}

_ADDON = re.compile(r"\b(starter pack|pack for playstation|season pass|add-?on|upgrade|bundle of|currency)\b", re.I)


def _games(payload):
    """The lists come grouped by first letter: [{"catalogKey": "A", "games": [...]}, ...]."""
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("lists") or [payload]
    out = []
    for block in payload or []:
        if isinstance(block, dict) and isinstance(block.get("games"), list):
            out += block["games"]
        elif isinstance(block, dict) and block.get("name"):
            out.append(block)
    return out


def _download(list_id):
    errors = []
    for url in (DIRECT.format(list_id), MIRROR.format(list_id)):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            return _games(r.json()), url.split("/")[2]
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
    raise RuntimeError("; ".join(errors))


def fetch():
    """Catalog entries in the same shape as library entries, plus catalog details."""
    out, seen = [], set()
    for tier, list_id in LISTS.items():
        games, host = _download(list_id)
        if len(games) < MINIMUM[tier]:
            raise RuntimeError(f"PS Plus {tier}: only {len(games)} games from {host}; the list format may have changed")
        log(f"PS Plus {tier}: {len(games)} games (from {host})")
        for g in games:
            name = g.get("nameEn") or g.get("name") or ""
            if not name or _ADDON.search(name) or titles.looks_like_non_game(name):
                continue
            concept = str(g.get("conceptId") or g.get("productId") or name)
            if concept in seen:
                continue
            seen.add(concept)
            devices = [d for d in (g.get("device") or []) if d in ("PS4", "PS5")] or ["PS4"]
            devices.sort(key=lambda d: d != "PS5")  # PS5 first
            out.append(
                {
                    "platform": devices[0],
                    "also_platform": devices[1] if len(devices) > 1 else None,
                    "source_id": f"psplus:{concept}",
                    "title": name,
                    "playtime_hours": 0.0,
                    "last_played": None,
                    "tier": tier,
                    "concept": concept,
                    "store_url": g.get("conceptUrl"),
                    "image": g.get("imageUrl"),
                    "streaming": bool(g.get("streamingSupported")),
                }
            )
    return out
