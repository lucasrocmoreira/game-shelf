"""Nintendo Switch Online classics (the Nintendo Classics apps), read from Wikipedia.

Nintendo has no public catalog API. Wikipedia's "Nintendo Classics" article keeps one table per
app (NES, SNES, Game Boy, Game Boy Advance, Nintendo 64, Sega Genesis, GameCube, Virtual Boy) and
is updated quickly when Nintendo adds games. The daily run reads it, so new additions appear and
removed ones drop off. If the page can't be read, the last good list is kept.
"""
import json
import re
import time

import yaml
import requests
from bs4 import BeautifulSoup

from .util import CONFIG_DIR, log

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
            if not text:
                # Availability cells are check/cross icons: read their sort value or the icon's alt text.
                img = c.find("img")
                text = (c.get("data-sort-value") or (img.get("alt") if img else "") or "").strip()
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
        ri = next((i for i, h in enumerate(header) if "na" in re.split(r"[/ ,]+", h) or "north america" in h or h in ("us", "americas")), None)
        if gi is None:
            continue
        if ri is None:
            # Without a North America column we can't tell Japan-only games apart: skip the table rather than guess.
            log(f"  Switch Online: no North America column in the {app[0]} table; skipped")
            continue
        for row in grid[1:]:
            if len(row) <= gi or not row[gi]:
                continue
            avail = row[ri].strip().lower() if len(row) > ri else ""
            if not avail or avail.startswith(("no", "n/a")) or avail in ("—", "–", "-", "✗", "✘", "×"):
                continue  # not released in North America (Japan- or Europe-only)
            if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", row[gi]):
                continue  # Japanese-language title
            title = re.sub(r"\s*\[[^\]]*\]$", "", row[gi]).strip(" *†‡")
            if title and title.lower() not in ("game", "title"):
                out.append((title, app[0], app[1]))
    return out


# Wikimedia asks automated clients to identify themselves with a way to reach the operator.
HEADERS = {"User-Agent": "GameShelf/1.0 (https://github.com/lucasrocmoreira/game-shelf; personal library site)",
           "Api-User-Agent": "GameShelf/1.0 (https://github.com/lucasrocmoreira/game-shelf)"}


def _page_html():
    """The article's HTML, trying the Action API first and the REST API as a backup, with retries."""
    attempts = [
        (API, {"action": "parse", "page": PAGE, "prop": "text", "format": "json", "formatversion": 2, "redirects": 1}, "parse"),
        (f"https://en.wikipedia.org/api/rest_v1/page/html/{PAGE}", None, "rest"),
    ]
    last = None
    for url, params, kind in attempts:
        for wait in (0, 10, 30):
            if wait:
                time.sleep(wait)
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            except requests.RequestException as e:
                last = e
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                body = re.sub(r"<[^>]+>", " ", r.text or "")
                body = re.sub(r"\s+", " ", body).strip()[:220]
                last = requests.HTTPError(f"{r.status_code} from Wikipedia ({kind}): {body}")
                continue
            r.raise_for_status()
            return r.json()["parse"]["text"] if kind == "parse" else r.text
    raise last or RuntimeError("Wikipedia unavailable")


def fetch():
    enabled, apps = settings()
    if not enabled:
        return []
    try:
        games = parse(_page_html())
        source = "Wikipedia"
    except Exception as e:
        # Wikipedia often refuses requests from data centers like GitHub's. The web app fetches the
        # same list from your own device and saves it to config/nso_list.json; use that instead.
        saved = CONFIG_DIR / "nso_list.json"
        if not saved.exists():
            raise
        data = json.loads(saved.read_text(encoding="utf-8"))
        games = [(g["title"], g["tag"], g["console"]) for g in data.get("games", [])]
        source = f"the list saved by the app on {data.get('fetched', '?')[:10]} (Wikipedia refused: {str(e)[:80]})"
        log(f"  Switch Online: using {source}")
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
    log(f"Switch Online: {len(out)} classics across {len({g['platform'] for g in out})} apps (from {source})")
    return out
