"""Nintendo Switch Online classics (the Nintendo Classics apps), read from Wikipedia.

Nintendo has no public catalog API. Wikipedia's "Nintendo Classics" article keeps one table per
app (NES, SNES, Game Boy, Game Boy Advance, Nintendo 64, Sega Genesis, GameCube, Virtual Boy) and
is updated quickly when Nintendo adds games. The daily run reads it, so new additions appear and
removed ones drop off. If the page can't be read, the last good list is kept.
"""
import re

import yaml
from bs4 import BeautifulSoup

from .util import CONFIG_DIR, log, session

API = "https://en.wikipedia.org/w/api.php"
PAGE = "Nintendo_Classics"

# app name -> (tag shown in the app, console it runs on). Order matters: check "Super NES" before "NES".
APPS = [
    ("Game Boy Advance", ("GBA", "Switch")),
    ("Game Boy", ("Game Boy", "Switch")),
    ("Super NES", ("SNES", "Switch")),
    ("Super Nintendo", ("SNES", "Switch")),
    ("SNES", ("SNES", "Switch")),
    ("Nintendo Entertainment System", ("NES", "Switch")),
    ("NES", ("NES", "Switch")),
    ("Nintendo 64", ("N64", "Switch")),
    ("N64", ("N64", "Switch")),
    ("Genesis", ("Genesis", "Switch")),
    ("Mega Drive", ("Genesis", "Switch")),
    ("GameCube", ("GameCube", "Switch 2")),  # GameCube classics only run on Switch 2
    ("Virtual Boy", ("Virtual Boy", "Switch")),
]
DEFAULT_APPS = ["NES", "SNES", "Game Boy", "GBA", "N64", "Genesis", "GameCube", "Virtual Boy"]


def settings():
    path = CONFIG_DIR / "nso.yml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    return bool(data.get("enabled", True)), set(data.get("apps") or DEFAULT_APPS)


def _app_for(text):
    for needle, app in APPS:
        if re.search(r"\b" + re.escape(needle) + r"\b", text, re.I):
            return app
    return None


def _grid(table):
    """Expand a wikitable into rows of cell texts, honouring rowspan/colspan."""
    grid, carry = [], {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        row, col, i = [], 0, 0
        while i < len(cells) or col in carry:
            if col in carry:
                text, left = carry[col]
                row.append(text)
                carry[col] = (text, left - 1)
                if left - 1 <= 0:
                    del carry[col]
                col += 1
                continue
            c = cells[i]
            i += 1
            for sup in c.find_all("sup"):
                sup.decompose()
            text = re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip()
            span = int(c.get("colspan", 1) or 1)
            rs = int(c.get("rowspan", 1) or 1)
            for _ in range(span):
                row.append(text)
                if rs > 1:
                    carry[col] = (text, rs - 1)
                col += 1
        grid.append(row)
    return grid


def parse(html):
    """Returns [(title, tag, console)] for games released in North America."""
    soup = BeautifulSoup(html, "html.parser")
    out, current = [], None
    for el in soup.find_all(["h2", "h3", "h4", "table"]):
        if el.name != "table":
            current = _app_for(el.get_text(" ", strip=True)) or current
            continue
        if "wikitable" not in (el.get("class") or []):
            continue
        caption = el.find("caption")
        app = _app_for(caption.get_text(" ", strip=True)) if caption else None
        app = app or current
        if not app:
            continue
        grid = _grid(el)
        if not grid:
            continue
        header = [h.lower() for h in grid[0]]
        gi = next((i for i, h in enumerate(header) if h in ("game", "title", "game title")), None)
        ri = next((i for i, h in enumerate(header) if "na" in h.split("/") or h in ("na", "na/pal", "north america")), None)
        if gi is None:
            continue
        for row in grid[1:]:
            if len(row) <= gi or not row[gi]:
                continue
            if ri is not None and len(row) > ri and row[ri].strip().lower().startswith("no"):
                continue  # Japan/Europe-only release
            title = re.sub(r"\s*\[[^\]]*\]$", "", row[gi]).strip(" *†‡")
            if title and title.lower() not in ("game", "title"):
                out.append((title, app[0], app[1]))
    return out


def fetch():
    enabled, apps = settings()
    if not enabled:
        return []
    r = session.get(
        API,
        params={"action": "parse", "page": PAGE, "prop": "text", "format": "json", "formatversion": 2, "redirects": 1},
        headers={"User-Agent": "game-shelf/1.0 (personal library site)"},
        timeout=60,
    )
    r.raise_for_status()
    games = parse(r.json()["parse"]["text"])
    games = [g for g in games if g[1] in apps]
    if len(games) < 50:
        raise RuntimeError(f"only {len(games)} games found on Wikipedia; the page layout may have changed")
    seen, out = set(), []
    for title, tag, console in games:
        k = (title.lower(), tag)
        if k in seen:
            continue
        seen.add(k)
        out.append(
            {
                "platform": tag,           # e.g. "SNES": used for the tag and for matching on IGDB
                "also_platform": console,  # e.g. "Switch": the console it's played on
                "source_id": f"nso:{tag}:{title}",
                "title": title,
                "playtime_hours": 0.0,
                "last_played": None,
                "nso": True,
            }
        )
    log(f"Switch Online: {len(out)} classics across {len({g['platform'] for g in out})} apps")
    return out
