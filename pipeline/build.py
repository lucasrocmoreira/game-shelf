"""Daily build: fetch libraries, match to IGDB, refresh scores, write site/data.json.

Run locally:  python -m pipeline.build
Environment:  STEAM_API_KEY, STEAM_ID, PSN_NPSSO, IGDB_CLIENT_ID, IGDB_CLIENT_SECRET,
              OPENCRITIC_RAPIDAPI_KEY (optional), plus the tuning knobs below.
"""
import os
import re
import sys
import traceback

import yaml

from . import scores, sources
from .igdb import IGDB, genres_of
from .util import CACHE_DIR, CONFIG_DIR, SITE_DIR, days_since, load_json, log, norm, now_iso, save_json

SCORE_REFRESH_DAYS = int(os.environ.get("SCORE_REFRESH_DAYS", "30"))
MAX_SCORE_LOOKUPS = int(os.environ.get("MAX_SCORE_LOOKUPS", "250"))
UNMATCHED_RETRY_DAYS = 14
OPENCRITIC_MAX_CALLS = int(os.environ.get("OPENCRITIC_MAX_CALLS", "20"))


def env(name):
    v = os.environ.get(name, "").strip()
    return v or None


def load_overrides():
    path = CONFIG_DIR / "overrides.yml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    return {
        "exclude": {norm(t) for t in (data.get("exclude") or []) + sources.hidden_titles()},
        "igdb": {norm(k): v for k, v in (data.get("igdb") or {}).items()},
        "metacritic": {norm(k): v for k, v in (data.get("metacritic") or {}).items()},
        "title": {norm(k): v for k, v in (data.get("rename") or {}).items()},
    }


# ---------------------------------------------------------------- step 1: libraries
def fetch_libraries(status):
    snapshot = load_json(CACHE_DIR / "library_snapshot.json", {})
    jobs = {
        "steam": (
            lambda: sources.fetch_steam(env("STEAM_API_KEY"), env("STEAM_ID")),
            env("STEAM_API_KEY") and env("STEAM_ID"),
        ),
        "playstation": (lambda: sources.fetch_psn(env("PSN_NPSSO")), env("PSN_NPSSO")),
        "nintendo_eshop": (
            lambda: sources.load_nintendo_receipts(env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD")),
            env("GMAIL_ADDRESS") and env("GMAIL_APP_PASSWORD"),
        ),
        "manual": (sources.load_manual, True),
    }
    entries = []
    for name, (fn, configured) in jobs.items():
        if not configured:
            status[name] = "not configured"
            continue
        try:
            got = fn()
            snapshot[name] = got
            status[name] = "ok"
        except Exception as e:  # keep yesterday's list so the site never goes blank
            log(f"!! {name} failed: {e}")
            traceback.print_exc()
            got = snapshot.get(name, [])
            status[name] = f"using last saved list ({type(e).__name__}: {str(e)[:160]})"
        entries.extend(got)
    save_json(CACHE_DIR / "library_snapshot.json", snapshot)
    return entries


# ---------------------------------------------------------------- step 2: IGDB matching
def match_to_igdb(entries, igdb, overrides):
    matches = load_json(CACHE_DIR / "igdb_matches.json", {})

    def key(e):
        return f"{e['platform']}:{e['source_id']}"

    def needs(e):
        m = matches.get(key(e))
        if norm(e["title"]) in overrides["igdb"]:
            return not m or m.get("via") != "override" or m.get("slug") != overrides["igdb"][norm(e["title"])]
        return m is None or (m.get("id") is None and days_since(m.get("checked")) >= UNMATCHED_RETRY_DAYS)

    todo = [e for e in entries if needs(e)]
    log(f"IGDB matching: {len(todo)} new or unmatched entries")

    # 1) Manual overrides by IGDB slug win over everything.
    remaining = []
    for e in todo:
        slug = overrides["igdb"].get(norm(e["title"]))
        if slug:
            res = igdb.query("games", f'fields id; where slug = "{slug}"; limit 1;')
            matches[key(e)] = {"id": res[0]["id"] if res else None, "checked": now_iso(), "via": "override", "slug": slug}
        else:
            remaining.append(e)

    # 2) Steam app ids map exactly via IGDB's external_games table.
    steam = [e for e in remaining if e.get("steam_appid")]
    found = igdb.steam_appids_to_games([e["steam_appid"] for e in steam]) if steam else {}

    # 3) Everything else: fuzzy title search.
    for e in remaining:
        gid = found.get(str(e.get("steam_appid")))
        if gid:
            matches[key(e)] = {"id": gid, "checked": now_iso(), "via": "steam"}
            continue
        g = igdb.search(e["title"])
        matches[key(e)] = {"id": g["id"] if g else None, "checked": now_iso(), "via": "search"}

    save_json(CACHE_DIR / "igdb_matches.json", matches)
    return {key(e): (matches.get(key(e)) or {}).get("id") for e in entries}


# ---------------------------------------------------------------- step 3: merge platforms
def merge(entries, match_ids, igdb_games, overrides):
    merged = {}
    for e in entries:
        if norm(e["title"]) in overrides["exclude"]:
            continue
        gid = match_ids.get(f"{e['platform']}:{e['source_id']}")
        g = igdb_games.get(gid) if gid else None
        k = f"igdb:{gid}" if g else f"title:{norm(e['title'])}"
        row = merged.setdefault(
            k,
            {
                "key": k,
                "title": (g or {}).get("name") or re.sub(r"[™®©]", "", e["title"]).strip(),
                "platforms": [],
                "playtime_hours": 0.0,
                "last_played": None,
                "steam_appid": None,
                "igdb": g,
                "cover": None,
                "ps_plus": True,
            },
        )
        row["ps_plus"] = row["ps_plus"] and bool(e.get("ps_plus"))
        if e["platform"] not in row["platforms"]:
            row["platforms"].append(e["platform"])
        row["playtime_hours"] = round(row["playtime_hours"] + (e.get("playtime_hours") or 0), 1)
        if e.get("last_played") and (row["last_played"] or "") < e["last_played"]:
            row["last_played"] = e["last_played"]
        if e.get("steam_appid") and not row["steam_appid"]:
            row["steam_appid"] = e["steam_appid"]
        if e.get("cover") and not row["cover"]:
            row["cover"] = e["cover"]
        rename = overrides["title"].get(norm(row["title"]))
        if rename:
            row["title"] = rename
    return merged


# ---------------------------------------------------------------- step 4: scores
def refresh_scores(merged, overrides, offline=False):
    cache = load_json(CACHE_DIR / "scores.json", {})
    oc = scores.OpenCritic(env("OPENCRITIC_RAPIDAPI_KEY"), OPENCRITIC_MAX_CALLS)

    def due(r):
        c = cache.get(r["key"], {})
        mc_override = overrides["metacritic"].get(norm(r["title"]))
        return days_since(c.get("checked")) >= SCORE_REFRESH_DAYS or (
            mc_override and c.get("metacritic_slug") != mc_override
        )

    stale = [r for r in merged.values() if due(r)]
    stale.sort(key=lambda r: (r["key"] in cache, cache.get(r["key"], {}).get("checked", "")))  # new games first
    if offline:
        stale = []
    log(f"Scores: {len(stale)} games due for a refresh, doing up to {MAX_SCORE_LOOKUPS} this run")

    for i, row in enumerate(stale[:MAX_SCORE_LOOKUPS], 1):
        c = cache.get(row["key"], {})
        g = row["igdb"] or {}
        title = row["title"]
        log(f"  [{i}] {title}")

        if row["steam_appid"]:
            c.update(scores.steam_store(row["steam_appid"]))

        mc_override = overrides["metacritic"].get(norm(title))
        if mc_override:
            c["metacritic"] = scores.metacritic(mc_override, is_slug=True)
            c["metacritic_slug"] = mc_override
        elif not c.get("metacritic"):
            for candidate, is_slug in ((c.get("metacritic_slug"), True), (g.get("slug"), True), (title, False)):
                if not candidate:
                    continue
                score = scores.metacritic(candidate, is_slug=is_slug)
                if score:
                    c["metacritic"] = score
                    c["metacritic_slug"] = candidate if is_slug else None
                    break

        if oc.key:
            oc_id, oc_score = oc.lookup(title, c.get("opencritic_id"))
            if oc_id:
                c["opencritic_id"] = oc_id
            if oc_score is not None:
                c["opencritic"] = oc_score

        bl = scores.backloggd(g.get("slug"))
        if bl is not None:
            c["backloggd"] = bl
        c["bl_v"] = scores.BACKLOGGD_PARSER_VERSION

        c["checked"] = now_iso()
        cache[row["key"]] = c
        if i % 25 == 0:
            save_json(CACHE_DIR / "scores.json", cache)  # checkpoint for long first runs

    # Retry Backloggd alone for games scored with an older parser (cheap: one request per game).
    done = {r["key"] for r in stale[:MAX_SCORE_LOOKUPS]}
    retry = [
        r for r in merged.values()
        if r["key"] not in done and r["key"] in cache and (r["igdb"] or {}).get("slug")
        and cache[r["key"]].get("bl_v") != scores.BACKLOGGD_PARSER_VERSION
    ]
    if retry and not offline:
        log(f"Backloggd: retrying {len(retry)} games with the updated parser")
        for row in retry[:MAX_SCORE_LOOKUPS]:
            c = cache[row["key"]]
            bl = scores.backloggd(row["igdb"]["slug"])
            if bl is not None:
                c["backloggd"] = bl
            c["bl_v"] = scores.BACKLOGGD_PARSER_VERSION

    save_json(CACHE_DIR / "scores.json", cache)
    return cache


# ---------------------------------------------------------------- step 5: output
def to_site_row(row, sc):
    g = row["igdb"] or {}
    cover = None
    if (g.get("cover") or {}).get("image_id"):
        cover = f"https://images.igdb.com/igdb/image/upload/t_cover_big/{g['cover']['image_id']}.jpg"
    elif row["cover"]:
        cover = row["cover"]
    elif row["steam_appid"]:
        cover = f"https://cdn.akamai.steamstatic.com/steam/apps/{row['steam_appid']}/library_600x900.jpg"

    genres = genres_of(g) or sc.get("steam_genres") or []
    year = None
    if g.get("first_release_date"):
        import datetime as dt

        year = dt.datetime.fromtimestamp(g["first_release_date"], dt.timezone.utc).year

    def r1(v):
        return round(v) if isinstance(v, (int, float)) else None

    bl = sc.get("backloggd")
    links = {}
    if g.get("slug"):
        links["backloggd"] = f"https://backloggd.com/games/{g['slug']}/"
    if g.get("url"):
        links["igdb"] = g["url"]
    if sc.get("metacritic_slug"):
        links["metacritic"] = f"https://www.metacritic.com/game/{sc['metacritic_slug']}/"
    if sc.get("opencritic_id"):
        links["opencritic"] = f"https://opencritic.com/game/{sc['opencritic_id']}/-"
    if row["steam_appid"]:
        links["steam"] = f"https://store.steampowered.com/app/{row['steam_appid']}/"

    return {
        "title": row["title"],
        "platforms": sorted(row["platforms"]),
        "genres": genres,
        "year": year,
        "playtime": row["playtime_hours"],
        "last_played": row["last_played"],
        "cover": cover,
        "matched": bool(g),
        "scores": {
            "metacritic": sc.get("metacritic"),
            "opencritic": sc.get("opencritic"),
            "backloggd": round(bl * 20) if isinstance(bl, (int, float)) else None,
            "igdb_critic": r1(g.get("aggregated_rating")),
            "igdb_user": r1(g.get("rating")),
            "steam": sc.get("steam_pct"),
        },
        "backloggd_stars": bl,
        "ps_plus": row["ps_plus"],
        "links": links,
    }


def main():
    offline = "--offline" in sys.argv
    status = {}
    overrides = load_overrides()
    entries = fetch_libraries(status)
    if not entries:
        log("No games from any source; leaving the existing site data untouched.")
        return 1

    igdb_games, match_ids = {}, {}
    if env("IGDB_CLIENT_ID") and env("IGDB_CLIENT_SECRET") and not offline:
        try:
            igdb = IGDB(env("IGDB_CLIENT_ID"), env("IGDB_CLIENT_SECRET"))
            match_ids = match_to_igdb(entries, igdb, overrides)
            igdb_games = igdb.games_by_ids(v for v in match_ids.values() if v)  # fresh IGDB ratings daily
            status["igdb"] = "ok"
        except Exception as e:
            log(f"!! IGDB failed: {e}")
            traceback.print_exc()
            status["igdb"] = f"failed ({type(e).__name__})"
    else:
        status["igdb"] = "not configured" if not offline else "skipped (offline)"

    merged = merge(entries, match_ids, igdb_games, overrides)
    cache = refresh_scores(merged, overrides, offline=offline)

    games = [to_site_row(r, cache.get(r["key"], {})) for r in merged.values()]
    games.sort(key=lambda g: g["title"].lower())
    save_json(
        SITE_DIR / "data.json",
        {"generated": now_iso(), "status": status, "count": len(games), "games": games},
        pretty=False,
    )
    unmatched = [g["title"] for g in games if not g["matched"]]
    log(f"Done: {len(games)} games, {len(unmatched)} without an IGDB match")
    if unmatched:
        log("Unmatched (fix in config/overrides.yml if needed): " + "; ".join(unmatched[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
