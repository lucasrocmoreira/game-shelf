"""Franchise guides: the main games of the big series you own, in release order.

For every IGDB franchise with at least two of your games, the full list of the franchise's games is
fetched from IGDB (cached for two weeks) and filtered down to the main games:
  - main games and remakes only: no DLC, ports, remasters, editions, bundles or mods;
  - released, and not mobile-only or browser-only;
  - crossovers dropped: a game whose own main franchise is a different one (SoulCalibur listed
    under God of War, Kingdom Hearts under Final Fantasy) doesn't count;
  - spin-offs: when IGDB has a series named like the franchise ("Resident Evil"), games outside it
    are spin-offs; little-known games (few IGDB ratings) are spin-offs too.
A franchise is shown when it has at least 5 main games, at least 3 of them widely rated on IGDB,
and you own at least 2 of its games. Played/playing status is worked out in the app, so it is
always current.
"""
import time

from .util import CACHE_DIR, CONFIG_DIR, days_since, load_json, log, now_iso, save_json

REFRESH_DAYS = 14
MIN_MAIN = 5
MIN_POPULAR = 3
POPULAR_RATINGS = 50     # IGDB total_rating_count for "widely rated"
SPINOFF_RATINGS = 10     # fewer ratings than this: treated as a spin-off
SPINOFF_SHARE = 0.05     # ...or fewer than 5% of the ratings of the series' most rated game
# Licensed umbrellas rather than game series; more can be added in config/franchises.yml
SKIP = {"star wars", "marvel", "lego", "batman", "dc comics", "disney", "warhammer", "warhammer 40,000",
        "spider-man", "transformers", "teenage mutant ninja turtles", "the simpsons", "harry potter",
        "lord of the rings", "the lord of the rings", "dungeons & dragons", "nintendo sports", "mario"}
STOP = {"the", "and", "of", "a", "an", "series", "game", "games"}
MIN_OWNED = 2
MAIN_TYPES = {0, 4, 8}   # main game, standalone expansion, remake
MOBILE_ONLY = {34, 39, 55, 82, 74}  # Android, iOS, legacy mobile, browser, Windows Phone

FIELDS = ("name,slug,first_release_date,game_type,cover.image_id,total_rating_count,aggregated_rating,"
          "rating,parent_game,version_parent,platforms,collections.name,franchise,franchises")


def _id(v):
    return v.get("id") if isinstance(v, dict) else v


def _type(g):
    t = g.get("game_type", g.get("category"))
    return _id(t)


def _main_franchise(g):
    if g.get("franchise"):
        return _id(g["franchise"])
    fr = g.get("franchises") or []
    return _id(fr[0]) if fr else None


def _norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _words(s):
    return [w for w in "".join(ch if ch.isalnum() else " " for ch in (s or "").lower()).split() if len(w) > 2 and w not in STOP]


def _pair_versions(entries):
    """Paired releases (Pokémon Red / Blue, Oracle of Ages / Seasons) become one entry:
    same year, names that differ by one word."""
    out = []
    for e in entries:
        mate = None
        for o in out:
            if o["year"] != e["year"] or o["spin"] != e["spin"] or o.get("_paired"):
                continue
            a, b = (o["title"] or "").split(), (e["title"] or "").split()
            if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
                mate = o
                break
        if not mate:
            out.append(e)
            continue
        a, b = mate["title"].split(), e["title"].split()
        i = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
        mate["title"] = " ".join(a[:i] + [f"{a[i]} / {b[i]}"] + a[i + 1:])
        mate["keys"] = sorted(set(mate["keys"]) | set(e["keys"]))
        mate["ids"] = mate.get("ids", [mate["id"]]) + [e["id"]]
        mate["ratings"] = max(mate["ratings"], e["ratings"])
        mate["_paired"] = True
    for e in out:
        e.pop("_paired", None)
    return out


def _year(g):
    ts = g.get("first_release_date")
    return time.gmtime(ts).tm_year if ts else None


def _fetch(igdb_client, fid):
    # IGDB has renamed fields over time (category -> game_type); fall back to the older names.
    for fields, where in ((FIELDS, f"franchises = ({fid}) | franchise = {fid}"),
                          (FIELDS.replace("game_type", "category"), f"franchises = ({fid}) | franchise = {fid}"),
                          (FIELDS.replace(",franchise,", ","), f"franchises = ({fid})")):
        try:
            return igdb_client.query("games", f"fields {fields}; where {where}; limit 500;")
        except Exception as e:
            if "400" not in str(e):
                raise
    raise RuntimeError("IGDB rejected the franchise query")


def build(igdb_client, igdb_games, rows):
    """rows: {key: site row} for your library and the PS Plus catalog.
    Returns the franchises.json payload."""
    cache = load_json(CACHE_DIR / "franchises.json", {})
    # Your games per IGDB id (and per original, for ports and remasters)
    by_id = {}
    for key, r in rows.items():
        if not key.startswith("igdb:"):
            continue
        gid = int(key.split(":")[1])
        g = igdb_games.get(gid) or {}
        for i in {gid, g.get("_root")} - {None}:
            by_id.setdefault(i, set()).add(key)

    owned = {}
    names = {}
    for key, r in rows.items():
        if r.get("catalog_only") or not key.startswith("igdb:"):
            continue
        g = igdb_games.get(int(key.split(":")[1])) or {}
        for f in g.get("franchises") or []:
            if isinstance(f, dict) and f.get("id"):
                owned.setdefault(f["id"], set()).add(key)
                names[f["id"]] = f.get("name")
        if isinstance(g.get("franchise"), dict) and g["franchise"].get("id"):
            owned.setdefault(g["franchise"]["id"], set()).add(key)
            names[g["franchise"]["id"]] = g["franchise"].get("name")

    candidates = [fid for fid, keys in owned.items() if len(keys) >= MIN_OWNED]
    todo = [fid for fid in candidates if days_since((cache.get(str(fid)) or {}).get("checked")) >= REFRESH_DAYS]
    log(f"Franchises: {len(candidates)} with 2+ of your games; fetching {len(todo)} from IGDB")
    for fid in todo:
        try:
            games = _fetch(igdb_client, fid)
        except Exception as e:
            log(f"  franchise {fid} failed: {type(e).__name__}")
            continue
        cache[str(fid)] = {"checked": now_iso(), "name": names.get(fid), "games": games}

    save_json(CACHE_DIR / "franchises.json", cache)

    now = time.time()
    extra = load_json(CONFIG_DIR / "franchises.json", {})
    skip = SKIP | {n.lower() for n in extra.get("skip", [])}
    out = []
    for fid in candidates:
        c = cache.get(str(fid))
        if not c:
            continue
        name = names.get(fid) or c.get("name") or ""
        if name.lower() in skip:
            continue
        words = set(_words(name))
        mains = []
        for g in c["games"]:
            t = _type(g)
            if t not in MAIN_TYPES or g.get("version_parent"):
                continue
            if not g.get("first_release_date") or g["first_release_date"] > now:
                continue
            plats = set(g.get("platforms") or [])
            if plats and plats <= MOBILE_ONLY:
                continue
            mf = _main_franchise(g)
            if mf and mf != fid and not (words & set(_words(g.get("name")))):
                continue  # crossover: another franchise's game, and the name doesn't say this one
            mains.append(g)
        # The series named like the franchise is the main line; the rest are spin-offs.
        main_coll = None
        for g in mains:
            for col in g.get("collections") or []:
                if isinstance(col, dict) and _norm(col.get("name")) == _norm(name):
                    main_coll = col["id"]
        top = max([g.get("total_rating_count") or 0 for g in mains] or [0])
        floor = max(SPINOFF_RATINGS, SPINOFF_SHARE * top)
        entries = []
        for g in mains:
            in_main = main_coll is None or any(_id(col) == main_coll for col in g.get("collections") or [])
            spin = not in_main or (g.get("total_rating_count") or 0) < floor
            scores = [v for v in (g.get("aggregated_rating"), g.get("rating")) if isinstance(v, (int, float)) and v > 0]
            entries.append({
                "id": g["id"],
                "title": g.get("name"),
                "slug": g.get("slug"),
                "year": _year(g),
                "cover": f"https://images.igdb.com/igdb/image/upload/t_cover_small/{g['cover']['image_id']}.jpg"
                         if isinstance(g.get("cover"), dict) and g["cover"].get("image_id") else None,
                "score": round(sum(scores) / len(scores)) if scores else None,
                "ratings": g.get("total_rating_count") or 0,
                "remake_of": _id(g.get("parent_game")) if _type(g) == 8 else None,
                "spin": spin,
                "keys": sorted(by_id.get(g["id"], [])),
            })
        main_entries = [e for e in entries if not e["spin"]]
        if len(main_entries) < MIN_MAIN or sum(e["ratings"] >= POPULAR_RATINGS for e in main_entries) < MIN_POPULAR:
            continue
        if sum(bool(e["keys"]) for e in entries) < MIN_OWNED:
            continue
        entries.sort(key=lambda e: (e["year"] or 9999, e["title"] or ""))
        entries = _pair_versions(entries)
        years = [e["year"] for e in main_entries if e["year"]]
        out.append({"id": fid, "name": name, "years": [min(years), max(years)] if years else None, "games": entries})
    out.sort(key=lambda f: -sum(bool(e["keys"]) for e in f["games"] if not e["spin"]))
    log(f"Franchises: {len(out)} shown")
    return {"generated": now_iso(), "franchises": out}
