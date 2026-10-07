"""Wikidata links games' IGDB ids to their OpenCritic and Metacritic pages.

That gives exact OpenCritic ids (so the small OpenCritic quota is spent on scores, not searches)
and the right Metacritic page for games that share a name with an older game.
"""
import re
import time

from .util import days_since, log, now_iso, session

ENDPOINT = "https://query.wikidata.org/sparql"
RECHECK_DAYS = 45


def _run(slugs):
    values = " ".join('"' + s.replace('"', "") + '"' for s in slugs)
    query = f"""SELECT ?igdb ?oc ?mc WHERE {{
      VALUES ?igdb {{ {values} }}
      ?item wdt:P5794 ?igdb .
      OPTIONAL {{ ?item wdt:P2864 ?oc . }}
      OPTIONAL {{ ?item wdt:P1712 ?mc . }}
    }}"""
    r = session.get(
        ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json", "User-Agent": "game-shelf/1.0 (personal library site)"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def _oc_id(value):
    m = re.search(r"\d+", value or "")
    return int(m.group(0)) if m else None


def _mc_slug(value):
    v = (value or "").strip("/")
    return v.split("/")[-1] if v else None


def lookup(slugs, cache: dict) -> dict:
    """Fill cache[slug] = {"opencritic_id", "metacritic_slug", "checked"} for slugs that need it."""
    todo = [s for s in slugs if s and days_since((cache.get(s) or {}).get("checked")) >= RECHECK_DAYS]
    if not todo:
        return cache
    log(f"Wikidata: looking up {len(todo)} games")
    for i in range(0, len(todo), 120):
        batch = todo[i : i + 120]
        try:
            rows = _run(batch)
        except Exception as e:
            log(f"  Wikidata lookup failed ({type(e).__name__}: {str(e)[:120]}); will retry next run")
            return cache
        found = {}
        for row in rows:
            slug = row["igdb"]["value"]
            entry = found.setdefault(slug, {})
            if "oc" in row and not entry.get("opencritic_id"):
                entry["opencritic_id"] = _oc_id(row["oc"]["value"])
            if "mc" in row and not entry.get("metacritic_slug"):
                entry["metacritic_slug"] = _mc_slug(row["mc"]["value"])
        for slug in batch:
            cache[slug] = {**found.get(slug, {}), "checked": now_iso()}
        time.sleep(1)
    return cache
