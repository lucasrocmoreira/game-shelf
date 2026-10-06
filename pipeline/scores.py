"""Score sources. Every function returns None-filled results on failure instead of raising,
so one flaky site never breaks the daily run."""
import json
import os
import re

from bs4 import BeautifulSoup

from .util import log, norm, polite_get, slugify


def _ld_json_rating(html):
    """Pull aggregateRating from schema.org JSON-LD blocks, if present."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            agg = item.get("aggregateRating")
            if isinstance(agg, dict) and agg.get("ratingValue") not in (None, ""):
                try:
                    return (
                        float(agg["ratingValue"]),
                        float(agg.get("bestRating") or 0) or None,
                        int(agg.get("ratingCount") or agg.get("reviewCount") or 0) or None,
                    )
                except (TypeError, ValueError):
                    pass
    return None


# ---------------------------------------------------------------- Steam store
def steam_store(appid):
    """Metacritic score (when Steam lists it), Steam genres and Steam user review %."""
    out = {"metacritic": None, "metacritic_slug": None, "steam_genres": [], "steam_pct": None, "steam_reviews": None}
    r = polite_get(
        "https://store.steampowered.com/api/appdetails",
        host_delay=1.6,  # Steam allows ~200 store calls per 5 minutes
        params={"appids": appid, "cc": "us", "l": "english"},
    )
    try:
        data = r.json()[str(appid)]
        if data.get("success"):
            d = data["data"]
            mc = d.get("metacritic") or {}
            out["metacritic"] = mc.get("score")
            if mc.get("url"):
                out["metacritic_slug"] = mc["url"].rstrip("/").split("/")[-1].split("?")[0]
            out["steam_genres"] = [g["description"] for g in d.get("genres", [])]
    except Exception:
        pass

    r = polite_get(
        f"https://store.steampowered.com/appreviews/{appid}",
        host_delay=1.6,
        params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
    )
    try:
        s = r.json()["query_summary"]
        total = s.get("total_reviews") or 0
        if total >= 10:
            out["steam_pct"] = round(100 * s["total_positive"] / total)
            out["steam_reviews"] = total
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- Metacritic
_MC_TITLE = re.compile(r"Metascore\s+(\d{1,3})\s+out of 100", re.I)


def metacritic(slug_or_title, is_slug=False):
    slug = slug_or_title if is_slug else slugify(slug_or_title)
    if not slug:
        return None
    r = polite_get(f"https://www.metacritic.com/game/{slug}/", host_delay=2.0)
    if r is None or r.status_code != 200:
        return None
    m = _MC_TITLE.search(r.text)
    if m:
        return int(m.group(1))
    ld = _ld_json_rating(r.text)
    if ld and (ld[1] in (None, 100.0)) and ld[0] > 10:
        return round(ld[0])
    return None


# ---------------------------------------------------------------- OpenCritic (optional, RapidAPI)
class OpenCritic:
    HOST = "opencritic-api.p.rapidapi.com"

    def __init__(self, key, max_calls):
        self.key = key
        self.calls_left = max_calls

    def _get(self, path, **params):
        if not self.key or self.calls_left <= 0:
            return None
        self.calls_left -= 1
        r = polite_get(
            f"https://{self.HOST}{path}",
            host_delay=1.2,
            params=params,
            headers={"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": self.HOST},
        )
        if r is None or r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def lookup(self, title, known_id=None):
        """Returns (opencritic_id, top_critic_score) or (known_id, None)."""
        oc_id = known_id
        if not oc_id:
            hits = self._get("/game/search", criteria=title) or []
            target = norm(title)
            for h in hits:
                if norm(h.get("name", "")) == target or (h.get("dist", 1) <= 0.1):
                    oc_id = h["id"]
                    break
        if not oc_id:
            return None, None
        g = self._get(f"/game/{oc_id}") or {}
        score = g.get("topCriticScore")
        return oc_id, (round(score) if isinstance(score, (int, float)) and score > 0 else None)


# ---------------------------------------------------------------- Backloggd
_BL_PATTERNS = [
    # Rating rendered as a standalone number in a heading (e.g. <h1 ...>4.3</h1>) near "Average".
    re.compile(r"Average\s*Rating.{0,400}?>\s*([0-5](?:\.\d)?)\s*<", re.I | re.S),
    re.compile(r">\s*([0-5]\.\d)\s*<.{0,400}?Average\s*Rating", re.I | re.S),
    re.compile(r'"(?:average_?rating|avg_?rating|rating_avg)"\s*:\s*"?([0-5](?:\.\d+)?)', re.I),
    re.compile(r'data-(?:avg|average|rating)="([0-5](?:\.\d+)?)"', re.I),
]


def backloggd(igdb_slug):
    """Backloggd community average (0-5). Backloggd uses IGDB slugs for game URLs.

    Backloggd has no API, so this reads the public game page. It is best-effort:
    if the page layout changes, scores come back empty and the run carries on.
    """
    if not igdb_slug or os.environ.get("BACKLOGGD", "1") == "0":
        return None
    r = polite_get(f"https://backloggd.com/games/{igdb_slug}/", host_delay=2.5)
    if r is None or r.status_code != 200:
        return None
    ld = _ld_json_rating(r.text)
    if ld and ld[0] <= 5:
        return round(ld[0], 2)
    for pat in _BL_PATTERNS:
        m = pat.search(r.text)
        if m:
            v = float(m.group(1))
            if 0 < v <= 5:
                return round(v, 2)
    return None
