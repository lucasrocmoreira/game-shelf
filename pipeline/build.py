"""Daily build: fetch libraries, match to IGDB, refresh scores, write site/data.json.

Run locally:  python -m pipeline.build
Environment:  STEAM_API_KEY, STEAM_ID, PSN_NPSSO, IGDB_CLIENT_ID, IGDB_CLIENT_SECRET,
              GMAIL_ADDRESS, GMAIL_APP_PASSWORD, OPENCRITIC_RAPIDAPI_KEY (all optional),
              plus the tuning knobs below.
"""
import datetime as dt
import os
import sys
import traceback

import yaml

from . import edits, scores, sources, titles, wikidata
from .igdb import IGDB, genres_of, metadata_of
from .util import CACHE_DIR, CONFIG_DIR, SITE_DIR, days_since, load_json, log, now_iso, save_json

MATCHER_VERSION = 4          # bump to re-match games with an improved matcher (high-confidence ones are kept)
METACRITIC_VERSION = 2       # bump to re-check every Metacritic score
SCORE_REFRESH_DAYS = int(os.environ.get("SCORE_REFRESH_DAYS", "30"))
MAX_SCORE_LOOKUPS = int(os.environ.get("MAX_SCORE_LOOKUPS", "250"))
MAX_RECHECKS = int(os.environ.get("MAX_RECHECKS", "200"))
UNMATCHED_RETRY_DAYS = 3
OPENCRITIC_REFRESH_DAYS = 60
OPENCRITIC_MAX_REQUESTS = int(os.environ.get("OPENCRITIC_MAX_REQUESTS", "150"))
OPENCRITIC_MAX_SEARCHES = int(os.environ.get("OPENCRITIC_MAX_SEARCHES", "20"))


def env(name):
    v = os.environ.get(name, "").strip()
    return v or None


def load_overrides(ed):
    path = CONFIG_DIR / "overrides.yml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    igdb_fix = {edits.source_key(k): v for k, v in (data.get("igdb") or {}).items()}
    igdb_fix.update(ed["match"])  # fixes made in the app win
    return {
        "exclude": {edits.source_key(t) for t in data.get("exclude") or []},
        "igdb": igdb_fix,
        "metacritic": {edits.source_key(k): v for k, v in (data.get("metacritic") or {}).items()},
        "title": {edits.source_key(k): v for k, v in (data.get("rename") or {}).items()},
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


def ekey(e):
    return f"{e['platform']}:{e['source_id']}"


# ---------------------------------------------------------------- step 2: IGDB matching
def match_to_igdb(entries, igdb, overrides):
    matches = load_json(CACHE_DIR / "igdb_matches.json", {})

    def fix_for(e):
        return overrides["igdb"].get(edits.source_key(e["title"]))

    def needs(e):
        m = matches.get(ekey(e))
        fix = fix_for(e)
        if fix:
            return not m or m.get("via") != "fix" or m.get("slug") != fix
        if not m or m.get("via") == "fix":
            return True
        if m.get("v") != MATCHER_VERSION:
            # v2 matches that were confident (or exact Steam ids) stay; close calls and misses are redone.
            return not (m.get("v") in (2, 3) and m.get("conf") in ("high", "exact"))
        if m.get("via") == "search" and m.get("id") is None and not m.get("candidates"):
            return True  # IGDB returned nothing at all: treat as "not checked yet", never as "no match"
        return m.get("id") is None and days_since(m.get("checked")) >= UNMATCHED_RETRY_DAYS

    todo = [e for e in entries if needs(e)]
    log(f"IGDB matching: {len(todo)} entries to (re)match")

    def record(e, gid, via, score=None, conf=None, slug=None, candidates=None):
        matches[ekey(e)] = {
            "id": gid, "via": via, "v": MATCHER_VERSION, "checked": now_iso(),
            "score": score, "conf": conf, "slug": slug, "title": e["title"], "candidates": candidates,
        }

    # 1) Fixes made in the app (or config/overrides.yml) win over everything.
    remaining = []
    for e in todo:
        fix = fix_for(e)
        if not fix:
            remaining.append(e)
            continue
        if fix == "none":
            record(e, None, "fix", slug="none")
            continue
        try:
            g = igdb.game_by_slug(fix)
            record(e, g["id"] if g else None, "fix", conf="manual", slug=fix)
            if not g:
                log(f"  Match fix: no IGDB game with slug '{fix}' (for {e['title']})")
        except Exception as e2:
            log(f"  Match fix lookup failed for {e['title']}: {e2}")

    # 2) Steam app ids map exactly via IGDB's external_games table.
    steam = [e for e in remaining if e.get("steam_appid")]
    found = {}
    if steam:
        try:
            found = igdb.steam_appids_to_games([e["steam_appid"] for e in steam])
        except Exception as e2:
            log(f"  Steam id mapping failed ({e2}); falling back to title search")

    # 3) Everything else: scored title search, once per distinct title.
    groups = {}
    for e in remaining:
        gid = found.get(str(e.get("steam_appid")))
        if gid:
            record(e, gid, "steam", conf="exact")
            continue
        if titles.looks_like_non_game(e["title"]):
            record(e, None, "non-game")
            continue
        groups.setdefault(edits.source_key(e["title"]), []).append(e)

    log(f"  searching IGDB for {len(groups)} distinct titles")
    failures = empties = done = 0
    for i, group in enumerate(groups.values(), 1):
        title = group[0]["title"]
        platforms = {e["platform"] for e in group}
        try:
            g, score, conf, n = igdb.best_match(title, platforms)
        except Exception as e2:  # not cached: retried on the next run
            failures += 1
            log(f"  IGDB search failed for {title!r}: {str(e2)[:160]}")
            if failures >= 25:
                log("  Too many IGDB errors; stopping matching for this run")
                break
            continue
        done += 1
        if n == 0:
            empties += 1
            log(f"  IGDB returned nothing for {title!r}; will retry next run")
            if done >= 20 and empties == done:
                save_json(CACHE_DIR / "igdb_matches.json", matches)
                raise RuntimeError("IGDB search returned no results for the first 20 titles; search looks broken")
            continue
        for e in group:
            record(e, g["id"] if g else None, "search", score, conf, candidates=n)
        if not g:
            log(f"  no confident match: {title!r} (best {score} of {n} candidates)")
        if i % 50 == 0:
            save_json(CACHE_DIR / "igdb_matches.json", matches)

    save_json(CACHE_DIR / "igdb_matches.json", matches)
    return {ekey(e): matches.get(ekey(e)) or {} for e in entries}


# ---------------------------------------------------------------- step 3: merge platforms
def merge(entries, match_info, igdb_games, overrides, ed):
    merged = {}
    for e in entries:
        info = match_info.get(ekey(e)) or {}
        gid = info.get("id")
        g = igdb_games.get(gid) if gid else None
        sk = edits.source_key(e["title"])
        k = f"igdb:{gid}" if g else f"t:{sk}"
        row = merged.setdefault(
            k,
            {
                "key": k,
                "title": (g or {}).get("name") or titles.clean(e["title"]),
                "platforms": [],
                "playtime_hours": 0.0,
                "last_played": None,
                "steam_appid": None,
                "igdb": g,
                "cover": None,
                "ps_plus": True,
                "src": set(),
                "conf": None,
                "non_game": True,
                "manual": False,
            },
        )
        row["src"].add(sk)
        row.setdefault("raw", set()).add(e["title"])
        row["ps_plus"] = row["ps_plus"] and bool(e.get("ps_plus"))
        row["non_game"] = row["non_game"] and titles.looks_like_non_game(e["title"])
        row["manual"] = row["manual"] or bool(e.get("manual"))
        if info.get("conf") and (row["conf"] in (None, "medium")):
            row["conf"] = info["conf"]
        if e["platform"] not in row["platforms"]:
            row["platforms"].append(e["platform"])
        row["playtime_hours"] = round(row["playtime_hours"] + (e.get("playtime_hours") or 0), 1)
        if e.get("last_played") and (row["last_played"] or "") < e["last_played"]:
            row["last_played"] = e["last_played"]
        if e.get("steam_appid") and not row["steam_appid"]:
            row["steam_appid"] = e["steam_appid"]
        if e.get("cover") and not row["cover"]:
            row["cover"] = e["cover"]

    for row in merged.values():
        rename = overrides["title"].get(edits.source_key(row["title"]))
        if rename:
            row["title"] = rename
        keys = {row["key"]} | {f"t:{s}" for s in row["src"]} | {f"t:{edits.source_key(row['title'])}"}
        if keys & set(ed["unhidden"]):
            row["hidden"] = None
        elif keys & set(ed["hidden"]) or row["src"] & overrides["exclude"]:
            row["hidden"] = "you"
        elif row["non_game"] and not row["igdb"]:
            row["hidden"] = "auto"
        else:
            row["hidden"] = None
        played = [ed["played"][k] for k in keys if k in ed["played"]]
        row["played"], row["pending_key"] = (played[0] if played else None), None
        if not played:  # "I've played it" ticked when adding a game in the app, before it had a key
            for t in row.get("raw", ()):
                pk = "pending:" + t.lower()
                if pk in ed["played"]:
                    row["played"], row["pending_key"] = ed["played"][pk], pk
    return merged


# ---------------------------------------------------------------- step 4: scores
def _year(g):
    ts = (g or {}).get("first_release_date")
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).year if ts else None


def fetch_metacritic(row, c, wd, overrides):
    """Most reliable source first: your fix, Wikidata, Steam's link, then IGDB slug / title with a year check."""
    g = row["igdb"] or {}
    year = _year(g)
    fix = None
    for s in row["src"] | {edits.source_key(row["title"])}:
        fix = fix or overrides["metacritic"].get(s)
    tries = []
    if fix:
        tries.append((fix, None))
    else:
        if wd.get("metacritic_slug"):
            tries.append((wd["metacritic_slug"], None))
        if c.get("steam_mc_slug"):
            tries.append((c["steam_mc_slug"], None))
        if g.get("slug"):
            tries.append((g["slug"], year))
        tries.append((scores.slugify(titles.clean(row["title"])), year))
    c["metacritic"], c["metacritic_slug"] = None, None
    c["mc_v"] = METACRITIC_VERSION
    if not fix and c.get("steam_mc"):  # Steam lists the exact Metacritic score for its own games
        c["metacritic"], c["metacritic_slug"] = c["steam_mc"], c.get("steam_mc_slug")
        return
    seen = set()
    for slug, expect in tries:
        if not slug or slug in seen:
            continue
        seen.add(slug)
        score = scores.metacritic(slug, is_slug=True, expect_year=expect)
        if score:
            c["metacritic"], c["metacritic_slug"] = score, slug
            break
    c["mc_v"] = METACRITIC_VERSION


def refresh_scores(merged, overrides, wd_cache, offline=False):
    cache = load_json(CACHE_DIR / "scores.json", {})
    rows = [r for r in merged.values() if r["hidden"] is None]

    def due(r):
        c = cache.get(r["key"])
        return c is None or days_since(c.get("checked")) >= SCORE_REFRESH_DAYS

    stale = sorted((r for r in rows if due(r)), key=lambda r: (r["key"] in cache, cache.get(r["key"], {}).get("checked", "")))
    if offline:
        stale = []
    log(f"Scores: {len(stale)} games due, doing up to {MAX_SCORE_LOOKUPS} this run")
    done = set()
    for i, row in enumerate(stale[:MAX_SCORE_LOOKUPS], 1):
        c = cache.get(row["key"], {})
        g = row["igdb"] or {}
        log(f"  [{i}] {row['title']}")
        if row["steam_appid"]:
            st = scores.steam_store(row["steam_appid"])
            c.update({k: v for k, v in st.items() if k not in ("metacritic_slug", "metacritic")})
            c["steam_mc"], c["steam_mc_slug"] = st.get("metacritic"), st.get("metacritic_slug")
        fetch_metacritic(row, c, wd_cache.get(g.get("slug"), {}), overrides)
        bl = scores.backloggd(g.get("slug"))
        if bl is not None:
            c["backloggd"] = bl
        c["bl_v"] = scores.BACKLOGGD_PARSER_VERSION
        c["checked"] = now_iso()
        cache[row["key"]] = c
        done.add(row["key"])
        if i % 25 == 0:
            save_json(CACHE_DIR / "scores.json", cache)

    if not offline:
        # Re-check games scored by older parsers (cheap passes, capped per run).
        recheck_mc = [r for r in rows if r["key"] not in done and r["key"] in cache and cache[r["key"]].get("mc_v") != METACRITIC_VERSION]
        if recheck_mc:
            log(f"Metacritic: re-checking {min(len(recheck_mc), MAX_RECHECKS)} of {len(recheck_mc)} games with the stricter matcher")
            for row in recheck_mc[:MAX_RECHECKS]:
                fetch_metacritic(row, cache[row["key"]], wd_cache.get((row["igdb"] or {}).get("slug"), {}), overrides)
        recheck_bl = [
            r for r in rows if r["key"] not in done and r["key"] in cache and (r["igdb"] or {}).get("slug")
            and cache[r["key"]].get("bl_v") != scores.BACKLOGGD_PARSER_VERSION
        ]
        if recheck_bl:
            log(f"Backloggd: re-checking {min(len(recheck_bl), MAX_RECHECKS)} games")
            for row in recheck_bl[:MAX_RECHECKS]:
                c = cache[row["key"]]
                bl = scores.backloggd(row["igdb"]["slug"])
                if bl is not None:
                    c["backloggd"] = bl
                c["bl_v"] = scores.BACKLOGGD_PARSER_VERSION
        save_json(CACHE_DIR / "scores.json", cache)
        refresh_opencritic(rows, cache, wd_cache)

    save_json(CACHE_DIR / "scores.json", cache)
    return cache


def refresh_opencritic(rows, cache, wd_cache):
    oc = scores.OpenCritic(env("OPENCRITIC_RAPIDAPI_KEY"), OPENCRITIC_MAX_REQUESTS, OPENCRITIC_MAX_SEARCHES)
    if not oc.key:
        return
    todo = []
    for r in rows:
        if not r["igdb"]:
            continue
        c = cache.setdefault(r["key"], {})
        if days_since(c.get("oc_checked")) < OPENCRITIC_REFRESH_DAYS:
            continue
        oc_id = c.get("opencritic_id") or wd_cache.get(r["igdb"].get("slug"), {}).get("opencritic_id")
        # Known ids first (1 request each), then searches for the games you play most.
        todo.append((0 if oc_id else 1, -(r["playtime_hours"] or 0), -(r["igdb"].get("total_rating_count") or 0), r, oc_id))
    todo.sort(key=lambda t: t[:3])
    log(f"OpenCritic: {len(todo)} games due; daily budget {oc.requests_left} requests / {oc.searches_left} searches")
    got = 0
    for _, _, _, r, oc_id in todo:
        if not oc.active:
            break
        c = cache[r["key"]]
        if not oc_id:
            if oc.searches_left <= 0:
                continue
            oc_id = oc.find_id(r["title"])
            if not oc_id:
                c["oc_checked"] = now_iso()  # not on OpenCritic; look again in 60 days
                continue
        res = oc.game(oc_id)
        if res is None:
            continue
        year = _year(r["igdb"])
        if year and res.get("opencritic_year") and abs(res["opencritic_year"] - year) > 1:
            c["oc_checked"] = now_iso()
            continue
        c.update({k: v for k, v in res.items() if k != "opencritic_year"})
        c["opencritic_id"] = oc_id
        c["oc_checked"] = now_iso()
        got += 1
    log(f"OpenCritic: updated {got} games")


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

    out = {
        "key": row["key"],
        "src": sorted(row["src"] | {edits.source_key(row["title"])}),
        "title": row["title"],
        "platforms": sorted(row["platforms"]),
        "genres": genres_of(g) or sc.get("steam_genres") or [],
        "year": _year(g),
        "playtime": row["playtime_hours"],
        "last_played": row["last_played"],
        "cover": cover,
        "matched": bool(g),
        "match": row["conf"],
        "slug": g.get("slug"),
        "hidden": row["hidden"],
        "played": row["played"],
        "pending_key": row["pending_key"],
        "ps_plus": row["ps_plus"],
        "manual": row["manual"],
        "scores": {
            "metacritic": sc.get("metacritic"),
            "opencritic": sc.get("opencritic"),
            "backloggd": round(bl * 20) if isinstance(bl, (int, float)) else None,
            "igdb_critic": r1(g.get("aggregated_rating")),
            "igdb_user": r1(g.get("rating")),
            "steam": sc.get("steam_pct"),
        },
        "backloggd_stars": bl,
        "opencritic_pct": sc.get("opencritic_pct"),
        "opencritic_tier": sc.get("opencritic_tier"),
        "links": links,
    }
    if g:
        out.update(metadata_of(g))
    return out


def main():
    offline = "--offline" in sys.argv
    status = {}
    ed = edits.load()
    overrides = load_overrides(ed)
    entries = fetch_libraries(status)
    if not entries:
        log("No games from any source; leaving the existing site data untouched.")
        return 1

    igdb_games, match_info = {}, {}
    if env("IGDB_CLIENT_ID") and env("IGDB_CLIENT_SECRET") and not offline:
        try:
            igdb = IGDB(env("IGDB_CLIENT_ID"), env("IGDB_CLIENT_SECRET"))
            match_info = match_to_igdb(entries, igdb, overrides)
            igdb_games = igdb.games_by_ids(m.get("id") for m in match_info.values() if m.get("id"))
            status["igdb"] = "ok"
        except Exception as e:
            log(f"!! IGDB failed: {e}")
            traceback.print_exc()
            status["igdb"] = f"failed ({type(e).__name__})"
            cached = load_json(CACHE_DIR / "igdb_matches.json", {})
            match_info = {ekey(e2): cached.get(ekey(e2)) or {} for e2 in entries}
    else:
        status["igdb"] = "not configured" if not offline else "skipped (offline)"

    merged = merge(entries, match_info, igdb_games, overrides, ed)

    wd_cache = load_json(CACHE_DIR / "wikidata.json", {})
    if not offline:
        slugs = [r["igdb"]["slug"] for r in merged.values() if r["igdb"] and r["hidden"] is None]
        wd_cache = wikidata.lookup(slugs, wd_cache)
        save_json(CACHE_DIR / "wikidata.json", wd_cache)

    cache = refresh_scores(merged, overrides, wd_cache, offline=offline)
    if env("OPENCRITIC_RAPIDAPI_KEY"):
        status["opencritic"] = "ok"

    games = [to_site_row(r, cache.get(r["key"], {})) for r in merged.values()]
    games.sort(key=lambda g: g["title"].lower())
    save_json(
        SITE_DIR / "data.json",
        {"generated": now_iso(), "status": status, "count": len(games), "games": games},
        pretty=False,
    )
    visible = [g for g in games if not g["hidden"]]
    unmatched = [g["title"] for g in visible if not g["matched"]]
    log(f"Done: {len(games)} games ({len(visible)} visible), {len(unmatched)} visible without an IGDB match")
    if unmatched:
        log("Unmatched (use 'Wrong match?' in the app to fix): " + "; ".join(unmatched[:80]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
