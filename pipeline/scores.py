"""Score sources. Every function returns None-filled results on failure instead of raising,
so one flaky site never breaks the daily run."""
import json
import os
import re

from bs4 import BeautifulSoup

from . import titles
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


_MC_YEAR = [
    re.compile(r'"datePublished"\s*:\s*"((?:19|20)\d\d)'),
    re.compile(r"Released On:?\s*</?[^>]*>?\s*[A-Za-z]{3,9}\.? \d{1,2},? ((?:19|20)\d\d)", re.I),
    re.compile(r"Release Date:?.{0,80}?((?:19|20)\d\d)", re.I | re.S),
]


def metacritic(slug_or_title, is_slug=False, expect_year=None):
    """Metascore from a Metacritic game page. With expect_year, a page for a different game that
    happens to share the name (e.g. a 2010 "Humanity" vs the 2023 one) is rejected."""
    slug = slug_or_title if is_slug else slugify(slug_or_title)
    if not slug:
        return None
    r = polite_get(f"https://www.metacritic.com/game/{slug}/", host_delay=2.0)
    if r is None or r.status_code != 200:
        return None
    if expect_year:
        page_year = None
        for pat in _MC_YEAR:
            m = pat.search(r.text)
            if m:
                page_year = int(m.group(1))
                break
        if page_year and abs(page_year - expect_year) > 1:
            log(f"    Metacritic: /{slug}/ is a {page_year} game, expected {expect_year}; skipped")
            return None
    m = _MC_TITLE.search(r.text)
    if m:
        return int(m.group(1))
    ld = _ld_json_rating(r.text)
    if ld and (ld[1] in (None, 100.0)) and ld[0] > 10:
        return round(ld[0])
    return None


# ---------------------------------------------------------------- OpenCritic (RapidAPI, free tier)
class OpenCritic:
    """OpenCritic's API is only offered through RapidAPI. The free plan allows roughly
    25 searches and 200 requests a day, so ids come from Wikidata whenever possible and
    searches are rationed. Quota headers from RapidAPI are respected if present."""

    HOST = "opencritic-api.p.rapidapi.com"

    def __init__(self, key, max_requests=150, max_searches=20):
        self.key = key
        self.requests_left = max_requests
        self.searches_left = max_searches
        self.exhausted = False

    @property
    def active(self):
        return bool(self.key) and not self.exhausted and self.requests_left > 0

    def _get(self, path, **params):
        if not self.active:
            return None
        self.requests_left -= 1
        r = polite_get(
            f"https://{self.HOST}{path}",
            host_delay=1.2,
            retries=2,
            params=params,
            headers={"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": self.HOST},
        )
        if r is None:
            return None
        for h in ("X-RateLimit-Requests-Remaining", "x-ratelimit-requests-remaining"):
            if r.headers.get(h, "").isdigit() and int(r.headers[h]) <= 2:
                self.exhausted = True
        if r.status_code in (401, 403):
            log(f"  OpenCritic: key rejected (HTTP {r.status_code}); check the OPENCRITIC_RAPIDAPI_KEY secret")
            self.exhausted = True
            return None
        if r.status_code == 429:
            log("  OpenCritic: daily quota used up; continuing tomorrow")
            self.exhausted = True
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def find_id(self, title, year=None):
        if self.searches_left <= 0:
            return None
        self.searches_left -= 1
        hits = self._get("/game/search", criteria=titles.clean(title)) or []
        best, best_s = None, 0.0
        for h in hits if isinstance(hits, list) else []:
            s = titles.similarity(title, h.get("name", ""))
            if s > best_s:
                best, best_s = h, s
        return best["id"] if best and best_s >= 0.93 else None

    def game(self, oc_id):
        """Returns {"opencritic", "opencritic_pct", "opencritic_tier", "opencritic_year"} or None."""
        g = self._get(f"/game/{oc_id}")
        if not isinstance(g, dict) or not g:
            return None
        score = g.get("topCriticScore")
        pct = g.get("percentRecommended")
        year = None
        if isinstance(g.get("firstReleaseDate"), str) and g["firstReleaseDate"][:4].isdigit():
            year = int(g["firstReleaseDate"][:4])
        tier = g.get("tier")
        if isinstance(tier, dict):
            tier = tier.get("name")
        return {
            "opencritic": round(score) if isinstance(score, (int, float)) and score > 0 else None,
            "opencritic_pct": round(pct) if isinstance(pct, (int, float)) and pct >= 0 else None,
            "opencritic_tier": tier if isinstance(tier, str) else None,
            "opencritic_year": year,
        }


# ---------------------------------------------------------------- Backloggd
BACKLOGGD_PARSER_VERSION = 2  # bump when the parser changes, so missing scores get retried

_BL_LABEL = r"(?:Avg\.?|Average)\s*Rating"
_BL_PATTERNS = [
    # The page shows a label "Avg Rating" next to a heading with the number, e.g. <h1>4.4</h1>.
    re.compile(_BL_LABEL + r".{0,600}?>\s*([0-5](?:\.\d{1,2})?)\s*<", re.I | re.S),
    re.compile(r">\s*([0-5]\.\d{1,2})\s*<.{0,600}?" + _BL_LABEL, re.I | re.S),
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
        log(f"    Backloggd: HTTP {getattr(r, 'status_code', 'no response')} for {igdb_slug}")
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
    if not getattr(backloggd, "_warned", False):
        backloggd._warned = True
        log(f"    Backloggd: page loaded but no rating found for {igdb_slug} (layout may have changed)")
    return None
