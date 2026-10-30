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
        "lord of the rings", "the lord of the rings", "dungeons & dragons", "nintendo sports"}
STOP = {"the", "and", "of", "a", "an", "series", "game", "games"}
MIN_OWNED = 2
DEBUG = None  # set to {} to record why each game was dropped or made a spin-off
MAIN_TYPES = {0, 4, 8}   # main game, standalone expansion, remake
MOBILE_ONLY = {34, 39, 55, 82, 74}  # Android, iOS, legacy mobile, browser, Windows Phone

FIELDS = ("name,slug,first_release_date,game_type,cover.image_id,total_rating_count,aggregated_rating,"
          "rating,parent_game,version_parent,platforms,collections.name,franchise,franchises,genres")
CACHE_VERSION = 3  # bump when FIELDS change, to fetch everything again


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
            close = abs((o.get("date") or 0) - (e.get("date") or 1e12)) <= 3 * 86400  # released together
            versions = o["year"] == e["year"] and (o["title"] or "").endswith("Version") and (e["title"] or "").endswith("Version")
            if o["spin"] != e["spin"] or o.get("section") != e.get("section") or o.get("_paired"):
                continue
            if _norm(o["title"]) == _norm(e["title"]) and o["year"] == e["year"]:
                mate = o  # the same game listed twice
                break
            if not (close or versions) or o.get("plats") != e.get("plats"):
                continue  # paired versions come out together on the same consoles (not Rogue and Unity)
            a, b = (o["title"] or "").split(), (e["title"] or "").split()
            if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
                mate = o
                break
        if not mate:
            out.append(e)
            continue
        a, b = mate["title"].split(), e["title"].split()
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if diff and len(a) == len(b):
            i = diff[0]
            mate["title"] = " ".join(a[:i] + [f"{a[i]} / {b[i]}"] + a[i + 1:])
        mate["keys"] = sorted(set(mate["keys"]) | set(e["keys"]))
        mate["ids"] = mate.get("ids", [mate["id"]]) + [e["id"]]
        mate["ratings"] = max(mate["ratings"], e["ratings"])
        mate["_paired"] = True
    for e in out:
        e.pop("_paired", None)
        e.pop("date", None)
        e.pop("plats", None)
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
            games, offset = [], 0
            while True:  # big franchises (Mario: 600+ games) come in pages of 500
                page = igdb_client.query("games", f"fields {fields}; where {where}; limit 500; offset {offset};")
                games += page
                if len(page) < 500 or offset >= 2500:
                    return games
                offset += 500
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
    todo = [fid for fid in candidates if days_since((cache.get(str(fid)) or {}).get("checked")) >= REFRESH_DAYS
            or (cache.get(str(fid)) or {}).get("v") != CACHE_VERSION]
    log(f"Franchises: {len(candidates)} with 2+ of your games; fetching {len(todo)} from IGDB")
    for fid in todo:
        try:
            games = _fetch(igdb_client, fid)
        except Exception as e:
            log(f"  franchise {fid} failed: {type(e).__name__}")
            continue
        cache[str(fid)] = {"checked": now_iso(), "v": CACHE_VERSION, "name": names.get(fid), "games": games}

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
        by_gid = {g["id"]: g for g in c["games"]}
        # A game's popularity includes its expanded editions, remasters and ports
        # (Code: Veronica's ratings are split with Code: Veronica X).
        pop = {}
        for g in c["games"]:
            pop[g["id"]] = pop.get(g["id"], 0) + (g.get("total_rating_count") or 0)
            par = _id(g.get("version_parent")) or (_id(g.get("parent_game")) if _type(g) in (9, 10, 11) else None)
            if par:
                pop[par] = pop.get(par, 0) + (g.get("total_rating_count") or 0)
        mains = []
        dbg = DEBUG.setdefault(name, {}) if DEBUG is not None else None
        for g in c["games"]:
            t = _type(g)
            why = None
            if t not in MAIN_TYPES or g.get("version_parent"):
                why = f"type {t}" + (" (edition of another game)" if g.get("version_parent") else "")
            elif not g.get("first_release_date") or g["first_release_date"] > now:
                why = "unreleased or no date"
            elif (set(g.get("platforms") or []) and set(g.get("platforms") or []) <= MOBILE_ONLY):
                why = "mobile/browser only"
            else:
                mf = _main_franchise(g)
                if mf and mf != fid and not (words & set(_words(g.get("name")))):
                    why = f"crossover (main franchise {mf})"  # another franchise's game, name doesn't say this one
            if why:
                if dbg is not None:
                    dbg[g["id"]] = {"name": g.get("name"), "year": _year(g), "ratings": g.get("total_rating_count"), "dropped": why}
                continue
            mains.append(g)
        # Sub-series (Mario Kart, Mario Party, Paper Mario): IGDB collections with 3+ of these games.
        # A collection whose name contains a bigger one's ("New Super Mario Bros." in "Super Mario")
        # is folded into it; each game goes to its most specific sub-series.
        size, cname = {}, {}
        for g in mains:
            for col in g.get("collections") or []:
                if isinstance(col, dict) and col.get("id"):
                    size[col["id"]] = size.get(col["id"], 0) + 1
                    cname[col["id"]] = col.get("name") or ""
        cand = {cid for cid, n in size.items() if n >= 3}
        target = {}
        for cid in cand:
            bigger = [o for o in cand if o != cid and size[o] > size[cid]
                      and (_norm(cname[o]) in _norm(cname[cid]) or _norm(cname[cid]) in _norm(cname[o]))]
            target[cid] = max(bigger, key=lambda o: size[o]) if bigger else cid
        for _ in range(3):
            target = {k: target.get(v, v) for k, v in target.items()}
        section_of = {}
        for g in mains:
            ts = {target[_id(col)] for col in g.get("collections") or [] if _id(col) in target}
            section_of[g["id"]] = min(ts, key=lambda t: (size[t], t)) if ts else None
        counts = {}
        for t in section_of.values():
            if t:
                counts[t] = counts.get(t, 0) + 1
        # Only umbrella franchises get sub-series: those with no series named like the franchise
        # (Mario has Super Mario, Mario Kart, Mario Party...; Resident Evil has "Resident Evil").
        eponymous = any(_norm(cname[c]) == _norm(name) for c in size)
        sectioned = not eponymous and len([t for t, n in counts.items() if n >= 3]) >= 2
        if not sectioned:
            section_of = {k: None for k in section_of}
        # Popularity floor per sub-series (a Mario Kart game is compared with Mario Kart games).
        tops = {}
        for g in mains:
            k = section_of[g["id"]]
            tops[k] = max(tops.get(k, 0), pop.get(g["id"], 0))
        # Without sub-series, the series named like the franchise is the main line.
        main_coll = None
        if not sectioned:
            for g in mains:
                for col in g.get("collections") or []:
                    if isinstance(col, dict) and _norm(col.get("name")) == _norm(name):
                        main_coll = col["id"]
        # The series' usual genres, weighted by popularity: a game in none of them is a spin-off
        # (Pokémon Pinball, Hyrule Warriors). Not used with sub-series, which differ in genre.
        weight, total = {}, 0
        for g in mains:
            w = pop.get(g["id"], 0)
            total += w
            for gen in g.get("genres") or []:
                weight[_id(gen)] = weight.get(_id(gen), 0) + w
        usual = set() if sectioned else {gen for gen, w in weight.items() if total and w >= 0.4 * total}
        entries = []
        for g in mains:
            sec = section_of[g["id"]]
            floor = max(SPINOFF_RATINGS, SPINOFF_SHARE * tops.get(sec, 0))
            if g["first_release_date"] > now - 548 * 86400:
                floor = SPINOFF_RATINGS  # new games haven't collected ratings yet (Black Flag Resynced)
            in_main = main_coll is None or any(_id(col) == main_coll for col in g.get("collections") or [])
            gens = {_id(x) for x in g.get("genres") or []}
            off_genre = bool(usual) and bool(gens) and not (gens & usual)
            # A main game carries the series name (Smash Bros. and Captain Toad are Mario spin-offs).
            unnamed = bool(words) and not any(w in (g.get("name") or "").lower() for w in words)
            spin = not in_main or pop.get(g["id"], 0) < floor or off_genre or unnamed
            if dbg is not None and spin:
                dbg[g["id"]] = {"name": g.get("name"), "year": _year(g), "ratings": pop.get(g["id"], 0),
                                "spin_because": [r for r, on in (("not in the series named like the franchise", not in_main),
                                                 (f"few ratings ({pop.get(g['id'], 0)} < {round(floor)})", pop.get(g["id"], 0) < floor),
                                                 ("unusual genre for the series", off_genre), ("name lacks the series name", unnamed)) if on]}
            scores = [v for v in (g.get("aggregated_rating"), g.get("rating")) if isinstance(v, (int, float)) and v > 0]
            entries.append({
                "id": g["id"],
                "title": g.get("name"),
                "slug": g.get("slug"),
                "year": _year(g),
                "cover": f"https://images.igdb.com/igdb/image/upload/t_cover_small/{g['cover']['image_id']}.jpg"
                         if isinstance(g.get("cover"), dict) and g["cover"].get("image_id") else None,
                "score": round(sum(scores) / len(scores)) if scores else None,
                "ratings": pop.get(g["id"], 0),
                "date": g.get("first_release_date"),
                "plats": sorted(g.get("platforms") or []),
                "remake_of": _id(g.get("parent_game")) if _type(g) == 8 else None,
                "orig_year": _year(by_gid.get(_id(g.get("parent_game"))) or {}) if _type(g) == 8 else None,
                "spin": spin,
                "section": (cname.get(sec) or f"Other {name} games") if sectioned else None,
                "keys": sorted(by_id.get(g["id"], [])),
            })
        # A remake of a main game is a main game too, however few ratings it has so far.
        by_entry = {e["id"]: e for e in entries}
        for e in entries:
            o = by_entry.get(e["remake_of"])
            if e["spin"] and o and not o["spin"] and e["ratings"] >= SPINOFF_RATINGS:
                e["spin"] = False
                e["section"] = o.get("section")
        # Sub-series with fewer than 3 main games go under "Other ... games".
        if sectioned:
            per = {}
            for e in entries:
                if not e["spin"]:
                    per[e["section"]] = per.get(e["section"], 0) + 1
            for e in entries:
                if per.get(e["section"], 0) < 3:
                    e["section"] = f"Other {name} games"
        # Same title within a year (Sonic the Hedgehog on Mega Drive and on Master System): the far
        # less known one is a spin-off.
        for e in entries:
            for o in entries:
                if o is not e and _norm(o["title"]) == _norm(e["title"]) and abs((o["year"] or 0) - (e["year"] or 0)) <= 1 \
                        and e["ratings"] < 0.25 * o["ratings"]:
                    e["spin"] = True
        main_entries = [e for e in entries if not e["spin"]]
        if len(main_entries) < MIN_MAIN or sum(e["ratings"] >= POPULAR_RATINGS for e in main_entries) < MIN_POPULAR:
            continue
        if sum(bool(e["keys"]) for e in entries) < MIN_OWNED:
            continue
        entries.sort(key=lambda e: (e["year"] or 9999, e["title"] or ""))
        entries = _pair_versions(entries)
        years = [e["year"] for e in main_entries if e["year"]]
        out.append({"id": fid, "name": name, "years": [min(years), max(years)] if years else None, "games": entries})
    # Overlapping franchises (Mario and Mario Bros.): keep the bigger one.
    mains_of = {f["id"]: {i for e in f["games"] if not e["spin"] for i in e.get("ids", [e["id"]])} for f in out}
    drop = set()
    for f in out:
        for o in out:
            a, b = mains_of[f["id"]], mains_of[o["id"]]
            if f is not o and o["id"] not in drop and len(a) < len(b) and len(a & b) >= 0.6 * len(a):
                drop.add(f["id"])
    out = [f for f in out if f["id"] not in drop]
    out.sort(key=lambda f: -sum(bool(e["keys"]) for e in f["games"] if not e["spin"]))
    log(f"Franchises: {len(out)} shown")
    return {"generated": now_iso(), "franchises": out}
