"""Franchise guides: the main games of the big series you own, in release order.

Two layers:

1. Curated lists (config/franchise_curation.json), checked against Wikipedia: for those franchises the
   main series (or its sub-series, for umbrellas like Mario) is exactly what the file says, matched to
   IGDB ids. Games released after a franchise was curated still go through the automatic rules below,
   so new entries appear on their own.

2. Automatic rules from IGDB, for every other franchise:
   - main games, standalone expansions, remakes and expanded games (Pokémon Yellow, attached to their
     original when it is listed); no DLC, ports, remasters, editions, bundles, demos or mobile-only games;
   - crossovers dropped: another franchise's game whose name doesn't carry this franchise's name;
   - spin-offs (shown behind a checkbox): little-known games, measured against a typical main game rather
     than the series' biggest hit; standalone expansions; "vs."/"featuring" crossovers; games named like a
     spin-off line (Warriors, Heroes, Tactics, Stories...); games without the series name; a game outside
     the series named like the franchise AND in an unusual genre. Numbered entries ("Final Fantasy III")
     are never hidden for having few ratings, and 0 ratings counts as "unknown", not "unpopular";
   - a remake follows its original (a remake of a spin-off is a spin-off);
   - umbrella franchises (no dominant series named like the franchise) are split into sub-series.
A franchise is shown when it has at least 5 main games, at least 3 of them widely rated on IGDB, and you
own at least 2 of its games. Played/playing status is worked out in the app, so it is always current.
"""
import re
import time

from .util import CACHE_DIR, CONFIG_DIR, days_since, load_json, log, now_iso, save_json

REFRESH_DAYS = 14
MIN_MAIN = 5
MIN_POPULAR = 3
POPULAR_RATINGS = 50     # IGDB total_rating_count for "widely rated"
SPINOFF_RATINGS = 10     # fewer ratings than this (but more than 0): a spin-off
SPINOFF_SHARE = 0.1      # ...or fewer than 10% of a typical well-known main game
RECENT_DAYS = 548        # games out for less than ~18 months haven't collected ratings yet
# Licensed umbrellas rather than game series; more can be added in config/franchises.json
SKIP = {"star wars", "marvel", "lego", "batman", "dc comics", "disney", "warhammer", "warhammer 40,000",
        "spider-man", "transformers", "teenage mutant ninja turtles", "the simpsons", "harry potter",
        "lord of the rings", "the lord of the rings", "dungeons & dragons", "nintendo sports"}
KIDS_LICENSED = ("lego", "disney", "nickelodeon", "nicktoons", "pixar", "barbie", "cartoon network", "dreamworks")
STOP = {"the", "and", "of", "a", "an", "series", "game", "games"}
SPINOFF_WORDS = {"warriors", "heroes", "builders", "monsters", "tactics", "stories", "racing", "kart", "party",
                 "pinball", "puzzle", "picross", "tennis", "golf", "olympic", "strikers", "chronicles", "musou",
                 "rumble", "shuffle", "quest", "smash", "dash", "trilogy", "collection", "anthology", "riders", "rivals",
                 "battle", "spinball", "pocket", "mini", "edition"}
CROSSOVER = re.compile(r"\b(vs\.?|versus|featuring|feat\.|x)\b|all[- ]stars", re.I)
DEMO = re.compile(r"\b(demo|trial version|beta|prototype|test drive edition|tech demo|soundtrack)\b", re.I)
NUMBERED = re.compile(r"\b(\d{1,2}|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|zero|0)\b", re.I)
MIN_OWNED = 2
MAIN_TYPES = {0, 4, 8, 10}   # main game, standalone expansion, remake, expanded game
MOBILE_ONLY = {34, 39, 55, 82, 74}  # Android, iOS, legacy mobile, browser, Windows Phone
DEBUG = None  # set to {} to record why each game was dropped or made a spin-off

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


def _year(g):
    ts = (g or {}).get("first_release_date")
    return time.gmtime(ts).tm_year if ts else None


def _cover(g):
    c = g.get("cover")
    return f"https://images.igdb.com/igdb/image/upload/t_cover_small/{c['image_id']}.jpg" \
        if isinstance(c, dict) and c.get("image_id") else None


def _score(g):
    scores = [v for v in (g.get("aggregated_rating"), g.get("rating")) if isinstance(v, (int, float)) and v > 0]
    return round(sum(scores) / len(scores)) if scores else None


def _pair_versions(entries):
    """Paired releases (Pokémon Red / Blue, Omega Ruby / Alpha Sapphire, Oracle of Ages / Seasons) become one
    entry: released within days of each other on the same consoles, names differing by one or two words."""
    out = []
    for e in entries:
        mate = None
        for o in out:
            if o["spin"] != e["spin"] or o.get("section") != e.get("section") or o.get("_paired"):
                continue
            if _norm(o["title"]) == _norm(e["title"]) and o["year"] == e["year"]:
                mate = o  # the same game listed twice
                break
            close = abs((o.get("date") or 0) - (e.get("date") or 1e12)) <= 3 * 86400
            versions = o["year"] == e["year"] and (o["title"] or "").endswith("Version") and (e["title"] or "").endswith("Version")
            if not (close or versions) or o.get("plats") != e.get("plats"):
                continue  # paired versions come out together on the same consoles (not Rogue and Unity)
            a, b = (o["title"] or "").split(), (e["title"] or "").split()
            if len(a) == len(b) and 1 <= sum(x != y for x, y in zip(a, b)) <= 2 and a[0] == b[0]:
                mate = o
                break
        if not mate:
            out.append(e)
            continue
        a, b = mate["title"].split(), e["title"].split()
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if diff and len(a) == len(b):
            i, j = diff[0], diff[-1] + 1
            mate["title"] = " ".join(a[:i] + [" ".join(a[i:j]) + " / " + " ".join(b[i:j])] + a[j:])
        mate["keys"] = sorted(set(mate["keys"]) | set(e["keys"]))
        mate["ids"] = mate.get("ids", [mate["id"]]) + [e["id"]]
        mate["ratings"] = max(mate["ratings"], e["ratings"])
        mate["_paired"] = True
    for e in out:
        e.pop("_paired", None)
    return out


def _fetch(igdb_client, fid, collection=False):
    # IGDB has renamed fields over time (category -> game_type); fall back to the older names.
    if collection:
        attempts = ((FIELDS, f"collections = ({fid})"), (FIELDS.replace("game_type", "category"), f"collections = ({fid})"))
    else:
        attempts = ((FIELDS, f"franchises = ({fid}) | franchise = {fid}"),
                    (FIELDS.replace("game_type", "category"), f"franchises = ({fid}) | franchise = {fid}"),
                    (FIELDS.replace(",franchise,", ","), f"franchises = ({fid})"))
    for fields, where in attempts:
        try:
            games, offset = [], 0
            while True:  # big franchises (Mario: 900+ games) come in pages of 500
                page = igdb_client.query("games", f"fields {fields}; where {where}; limit 500; offset {offset};")
                games += page
                if len(page) < 500 or offset >= 2500:
                    return games
                offset += 500
        except Exception as e:
            if "400" not in str(e):
                raise
    raise RuntimeError("IGDB rejected the franchise query")


def _lookup_extra(igdb_client, cache, curation, known):
    """Curated games IGDB doesn't list under the franchise: fetch by id, or find by name and year."""
    extra = cache.setdefault("_extra", {"ids": {}, "names": {}})
    want_ids, want_names = set(), {}
    for cur in curation.values():
        for e in _curated_items(cur):
            if e.get("id") and e["id"] not in known and str(e["id"]) not in extra["ids"]:
                want_ids.add(e["id"])
            elif not e.get("id") and e.get("name"):
                k = f"{e['name']}|{e.get('year')}"
                if k not in extra["names"]:
                    want_names[k] = e
    if igdb_client is None:
        return extra
    ids = sorted(want_ids)
    for i in range(0, len(ids), 200):
        try:
            for g in igdb_client.query("games", f"fields {FIELDS}; where id = ({','.join(map(str, ids[i:i + 200]))}); limit 500;"):
                extra["ids"][str(g["id"])] = g
        except Exception as e:
            log(f"  curated id lookup failed: {type(e).__name__}")
    for k, e in want_names.items():
        q = e["name"].replace('"', " ")
        try:
            res = igdb_client.query("games", f'search "{q}"; fields {FIELDS}; limit 20;')
        except Exception as err:
            log(f"  curated lookup failed for {e['name']!r}: {type(err).__name__}")
            continue
        best = None
        for g in res:
            if _norm(g.get("name")) != _norm(e["name"]):
                continue
            if e.get("year") and _year(g) and abs(_year(g) - e["year"]) > 1:
                continue
            if best is None or (g.get("total_rating_count") or 0) > (best.get("total_rating_count") or 0):
                best = g
        extra["names"][k] = best  # None is remembered too, so it isn't searched every day
        if best:
            extra["ids"][str(best["id"])] = best
        else:
            log(f"  curated game not found on IGDB: {e['name']} ({e.get('year')})")
    return extra


def _curated_items(cur):
    for e in cur.get("main") or []:
        yield e
    for s in cur.get("sections") or []:
        for e in s.get("games") or []:
            yield e
    for e in cur.get("spin") or []:
        yield e


def build(igdb_client, igdb_games, rows):
    """rows: {key: site row} for your library and the PS Plus catalog.
    Returns the franchises.json payload."""
    cache = load_json(CACHE_DIR / "franchises.json", {})
    curation = load_json(CONFIG_DIR / "franchise_curation.json", {})
    curation = {k: v for k, v in curation.items() if isinstance(v, dict)}
    # Your games per IGDB id (and per original, for ports and remasters)
    by_id = {}
    for key, r in rows.items():
        if not key.startswith("igdb:"):
            continue
        gid = int(key.split(":")[1])
        g = igdb_games.get(gid) or {}
        for i in {gid, g.get("_root")} - {None}:
            by_id.setdefault(i, set()).add(key)

    owned, names, cowned, cnames = {}, {}, {}, {}
    for key, r in rows.items():
        if r.get("catalog_only") or not key.startswith("igdb:"):
            continue
        g = igdb_games.get(int(key.split(":")[1])) or {}
        frs = [f for f in g.get("franchises") or [] if isinstance(f, dict) and f.get("id")]
        if isinstance(g.get("franchise"), dict) and g["franchise"].get("id"):
            frs.append(g["franchise"])
        for f in frs:
            owned.setdefault(f["id"], set()).add(key)
            names[f["id"]] = f.get("name")
        for col in g.get("collections") or []:
            if isinstance(col, dict) and col.get("id"):
                cowned.setdefault(col["id"], set()).add(key)
                cnames[col["id"]] = col.get("name") or ""

    cfg = load_json(CONFIG_DIR / "franchises.json", {})
    skip = SKIP | {n.lower() for n in cfg.get("skip", [])}
    # Series IGDB splits across franchises (Harvest Moon / Story of Seasons) are merged into one guide.
    merge_to = {}
    for target, parts in (cfg.get("merge") or {}).items():
        for p in parts:
            merge_to[_norm(p)] = target
    groups = {}
    for fid, keys in owned.items():
        gname = merge_to.get(_norm(names.get(fid)), names.get(fid) or "")
        grp = groups.setdefault(gname, {"name": gname, "fids": set(), "owned": set(), "collection": None})
        grp["fids"].add(fid)
        grp["owned"] |= keys
    candidates = [g for g in groups.values() if len(g["owned"]) >= MIN_OWNED and g["name"].lower() not in skip]
    todo = [fid for g in candidates for fid in g["fids"]
            if days_since((cache.get(str(fid)) or {}).get("checked")) >= REFRESH_DAYS or (cache.get(str(fid)) or {}).get("v") != CACHE_VERSION]
    log(f"Franchises: {len(candidates)} with 2+ of your games; fetching {len(todo)} from IGDB")
    for fid in todo:
        try:
            games = _fetch(igdb_client, fid)
        except Exception as e:
            log(f"  franchise {fid} failed: {type(e).__name__}")
            continue
        cache[str(fid)] = {"checked": now_iso(), "v": CACHE_VERSION, "name": names.get(fid), "games": games}

    # Series IGDB only knows as a collection (Etrian Odyssey, Oddworld, Batman: Arkham): candidates when
    # you own 3+ of their games and a franchise guide doesn't already cover them (checked further down).
    ccands = [cid for cid, keys in cowned.items() if len(keys) >= 3 and not any(w in (cnames[cid] or "").lower() for w in KIDS_LICENSED)]
    ctodo = [cid for cid in ccands if days_since((cache.get(f"c{cid}") or {}).get("checked")) >= REFRESH_DAYS
             or (cache.get(f"c{cid}") or {}).get("v") != CACHE_VERSION]
    log(f"Series (IGDB collections): {len(ccands)} with 3+ of your games; fetching {len(ctodo)}")
    for cid in ctodo:
        try:
            cache[f"c{cid}"] = {"checked": now_iso(), "v": CACHE_VERSION, "name": cnames[cid],
                                "games": _fetch(igdb_client, cid, collection=True)}
        except Exception as e:
            log(f"  series {cid} failed: {type(e).__name__}")

    known = {}
    for k, v in cache.items():
        if k != "_extra":
            for g in v.get("games") or []:
                known[g["id"]] = g
    extra = _lookup_extra(igdb_client, cache, curation, known)
    for g in extra["ids"].values():
        known.setdefault(g["id"], g)
    save_json(CACHE_DIR / "franchises.json", cache)

    now = time.time()
    out = []
    for grp in candidates:
        games, seen = [], set()
        for fid in sorted(grp["fids"]):
            for g in (cache.get(str(fid)) or {}).get("games") or []:
                if g["id"] not in seen:
                    seen.add(g["id"])
                    games.append(g)
        if not games:
            continue
        f = _auto(min(grp["fids"]), grp["name"], games, known, by_id, now, curation.get(grp["name"]), fids=grp["fids"])
        if f:
            out.append(f)
    # Collections not covered by a franchise guide become guides of their own.
    covered = {k for f in out for e in f["games"] for k in e["keys"]}
    shown_names = {_norm(f["name"]) for f in out}
    for cid in ccands:
        c = cache.get(f"c{cid}")
        if not c or _norm(cnames[cid]) in shown_names:
            continue
        mine = cowned[cid]
        if len(mine & covered) >= 0.5 * len(mine):
            continue
        f = _auto(-cid, cnames[cid], c["games"], known, by_id, now, curation.get(cnames[cid]), fids=None)
        if f:
            out.append(f)
            shown_names.add(_norm(cnames[cid]))
            covered |= {k for e in f["games"] for k in e["keys"]}
    # Overlapping guides (Mario and Mario Bros.): keep the bigger one.
    mains_of = {f["id"]: {i for e in f["games"] if not e["spin"] for i in e.get("ids", [e["id"]])} for f in out}
    drop = set()
    for f in out:
        for o in out:
            a, b = mains_of[f["id"]], mains_of[o["id"]]
            if f is not o and o["id"] not in drop and len(a) < len(b) and len(a & b) >= 0.6 * len(a):
                drop.add(f["id"])
    out = [f for f in out if f["id"] not in drop]
    out.sort(key=lambda f: -sum(bool(e["keys"]) for e in f["games"] if not e["spin"]))
    log(f"Franchises: {len(out)} shown ({sum(bool(f.get('curated')) for f in out)} curated)")
    return {"generated": now_iso(), "franchises": out}


def _auto(fid, name, games, known, by_id, now, cur=None, fids=None):
    words = set(_words(name))
    named = lambda g: not words or any(w in (g.get("name") or "").lower() for w in words)  # prefix match: WarioWare
    by_gid = {g["id"]: g for g in games}
    dbg = DEBUG.setdefault(name, {}) if DEBUG is not None else None

    # A game's popularity includes its expanded editions, remasters and ports.
    pop = {}
    for g in games:
        pop[g["id"]] = pop.get(g["id"], 0) + (g.get("total_rating_count") or 0)
        par = _id(g.get("version_parent")) or (_id(g.get("parent_game")) if _type(g) in (9, 10, 11) else None)
        if par:
            pop[par] = pop.get(par, 0) + (g.get("total_rating_count") or 0)

    mains = []
    for g in games:
        t = _type(g)
        why = None
        if t not in MAIN_TYPES or g.get("version_parent"):
            why = f"type {t}" + (" (edition of another game)" if g.get("version_parent") else "")
        elif t == 10 and _id(g.get("parent_game")) in by_gid:
            why = "expanded edition of a listed game"  # Pokémon Yellow belongs with Red / Blue
        elif not g.get("first_release_date") or g["first_release_date"] > now:
            why = "unreleased or no date"
        elif set(g.get("platforms") or []) and set(g.get("platforms") or []) <= MOBILE_ONLY:
            why = "mobile/browser only"
        elif DEMO.search(g.get("name") or ""):
            why = "demo or similar"
        else:
            mf = _main_franchise(g)
            if fids is not None and mf and mf not in fids and not named(g):
                why = f"crossover (main franchise {mf})"
        if why:
            if dbg is not None:
                dbg[g["id"]] = {"name": g.get("name"), "year": _year(g), "ratings": g.get("total_rating_count"), "dropped": why}
            continue
        mains.append(g)
    # Expanded editions count as their original's (owning Yellow means you own a "Red / Blue / Yellow" entry).
    editions = {}
    for g in games:
        if _type(g) == 10 and _id(g.get("parent_game")) in by_gid:
            editions.setdefault(_id(g["parent_game"]), []).append(g["id"])

    # Collections: the series named like the franchise, and sub-series.
    size, cname, cweight = {}, {}, {}
    for g in mains:
        for col in g.get("collections") or []:
            if isinstance(col, dict) and col.get("id"):
                size[col["id"]] = size.get(col["id"], 0) + 1
                cname[col["id"]] = col.get("name") or ""
                cweight[col["id"]] = cweight.get(col["id"], 0) + pop.get(g["id"], 0)
    total_w = sum(pop.get(g["id"], 0) for g in mains) or 1
    epon = [c for c in size if _norm(cname[c]) == _norm(name)]
    starts = {c for c in size if _norm(cname[c]).startswith(_norm(name))}  # "Need for Speed: Shift" counts too
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
    # Umbrella: no series named like the franchise carrying most of it (Mario, Wario, Mega Man).
    dominant = any(cweight.get(c, 0) >= 0.6 * total_w for c in epon)
    sectioned = not dominant and len([t for t, n in counts.items() if n >= 3]) >= 2
    if not sectioned:
        section_of = {k: None for k in section_of}

    # Popularity: compared with a typical well-known main game of the same sub-series.
    def typical(sec):
        ws = sorted((pop.get(g["id"], 0) for g in mains if section_of[g["id"]] == sec), reverse=True)[:5]
        return ws[len(ws) // 2] if ws else 0
    typ = {sec: typical(sec) for sec in set(section_of.values())}
    # The series' usual genre: the single most weighted one.
    weight = {}
    for g in mains:
        for gen in g.get("genres") or []:
            weight[_id(gen)] = weight.get(_id(gen), 0) + pop.get(g["id"], 0)
    usual = {max(weight, key=weight.get)} if weight and not sectioned else set()

    entries = []
    for g in mains:
        sec = section_of[g["id"]]
        nm = g.get("name") or ""
        low = nm.lower()
        r = pop.get(g["id"], 0)
        recent = g["first_release_date"] > now - RECENT_DAYS * 86400
        numbered = named(g) and bool(NUMBERED.search(low.replace(name.lower(), " ", 1) if name.lower() in low else low))
        floor = max(SPINOFF_RATINGS, SPINOFF_SHARE * typ.get(sec, 0))
        reasons = []
        if r and r < floor and not numbered and not (recent and named(g)):
            reasons.append(f"few ratings ({r} < {round(floor)})")
        if r == 0 and not numbered and not (recent and named(g)):
            reasons.append("no ratings")
        if _type(g) == 4:
            reasons.append("standalone expansion")
        if CROSSOVER.search(nm) and not CROSSOVER.search(name):
            reasons.append("crossover title")
        spin_words = {w for w in SPINOFF_WORDS if w not in words} if not sectioned else set()
        if any(re.search(rf"\b{w}\b", low) for w in spin_words):
            reasons.append("spin-off line name")
        if not named(g):
            reasons.append("name lacks the series name")
        if not sectioned and epon:
            in_series = any(_id(col) in epon or _id(col) in starts for col in g.get("collections") or [])
            gens = {_id(x) for x in g.get("genres") or []}
            off_genre = bool(usual) and bool(gens) and not (gens & usual)
            if not in_series and (off_genre or (r and r < floor)):
                reasons.append("outside the main series, unusual genre or little known")
            # A spin-off line of its own: another collection extending the franchise name (Pokémon Mystery Dungeon)
            subs = [c for c in (_id(col) for col in g.get("collections") or []) if c in starts and c not in epon and size.get(c, 0) >= 2]
            if subs and not any(_id(col) in epon for col in g.get("collections") or []):
                reasons.append(f"in the spin-off line {cname[subs[0]]}")
        if dbg is not None and reasons:
            dbg[g["id"]] = {"name": nm, "year": _year(g), "ratings": r, "spin_because": reasons}
        entries.append({
            "id": g["id"],
            "title": nm,
            "slug": g.get("slug"),
            "year": _year(g),
            "date": g.get("first_release_date"),
            "cover": _cover(g),
            "score": _score(g),
            "ratings": r,
            "plats": sorted(g.get("platforms") or []),
            "remake_of": _id(g.get("parent_game")) if _type(g) == 8 else None,
            "orig_year": _year(by_gid.get(_id(g.get("parent_game"))) or known.get(_id(g.get("parent_game"))) or {}) if _type(g) == 8 else None,
            "spin": bool(reasons),
            "section": None,
            "_sec": sec,
            "_vr": " vr" in f" {low}",
            "keys": sorted(set().union(*(by_id.get(i, set()) for i in [g["id"], *editions.get(g["id"], [])]))),
        })
    # A remake follows its original: main if the original is main, spin-off if it is a spin-off.
    by_entry = {e["id"]: e for e in entries}
    for e in entries:
        o = by_entry.get(e["remake_of"])
        if o is not None and not e["_vr"]:
            e["spin"] = o["spin"]
            e["_sec"] = o["_sec"]
        elif e["_vr"]:
            e["spin"] = True
    # Same title within a year on overlapping consoles (8-bit and 16-bit Sonic are different games).
    for e in entries:
        for o in entries:
            if o is not e and _norm(o["title"]) == _norm(e["title"]) and abs((o["year"] or 0) - (e["year"] or 0)) <= 1 \
                    and set(o["plats"]) & set(e["plats"]) and e["ratings"] < 0.25 * o["ratings"]:
                e["spin"] = True
    if sectioned:
        for e in entries:
            e["section"] = cname.get(e["_sec"]) or f"Other {name} games"
        per = {}
        for e in entries:
            if not e["spin"]:
                per[e["section"]] = per.get(e["section"], 0) + 1
        for e in entries:
            if per.get(e["section"], 0) < 3:
                e["section"] = f"Other {name} games"

    curated = False
    if cur:
        entries = _apply_curation(cur, entries, known, by_id, editions, now)
        curated = True
    else:
        main_entries = [e for e in entries if not e["spin"]]
        popular = sum(e["ratings"] >= POPULAR_RATINGS for e in main_entries)
        mine = sum(bool(e["keys"]) for e in entries)
        big = len(main_entries) >= MIN_MAIN and popular >= MIN_POPULAR
        small = len(main_entries) >= 3 and popular >= 2 and mine >= 3  # BioShock, The Last of Us, Golden Sun
        if not (big or small):
            return None
    if sum(bool(e["keys"]) for e in entries) < MIN_OWNED:
        return None
    entries.sort(key=lambda e: (e.get("date") or 9e12, e["title"] or ""))
    entries = _pair_versions(entries)
    for e in entries:
        for k in ("plats", "_sec", "_vr"):
            e.pop(k, None)
    main_entries = [e for e in entries if not e["spin"]]
    years = [e["year"] for e in main_entries if e["year"]]
    out = {"id": fid, "name": name, "years": [min(years), max(years)] if years else None, "games": entries}
    if curated:
        out["curated"] = cur.get("source") or True
    return out


def _apply_curation(cur, entries, known, by_id, editions, now):
    """Main series exactly as curated; everything else from IGDB becomes a spin-off, except games
    released after the curation date, which keep the automatic verdict."""
    since = cur.get("curated_at")
    since_ts = time.mktime(time.strptime(since[:10], "%Y-%m-%d")) if since else now
    drop = {int(i) for i in cur.get("drop") or []}
    by_entry = {e["id"]: e for e in entries}
    # Some originals have no ratings or cover on IGDB, while their enhanced edition does (Kingdom Hearts
    # Final Mix, Final Fantasy VII International): borrow the best-rated edition's score and cover.
    kids = {}
    for x in known.values():
        if _type(x) in (9, 10, 11) or x.get("version_parent"):
            par = _id(x.get("version_parent")) or _id(x.get("parent_game"))
            if par:
                kids.setdefault(par, []).append(x)
    chosen, order = {}, []
    groups = [(s.get("name"), s.get("games") or []) for s in cur.get("sections") or []] or [(None, cur.get("main") or [])]
    for sec, items in groups:
        for it in items:
            g = known.get(it.get("id")) if it.get("id") else None
            if g is None and it.get("name"):
                g = next((x for x in known.values() if _norm(x.get("name")) == _norm(it["name"])
                          and (not it.get("year") or not _year(x) or abs(_year(x) - it["year"]) <= 1)), None)
            if g is None:
                continue
            e = by_entry.get(g["id"]) or {
                "id": g["id"], "title": g.get("name"), "slug": g.get("slug"), "year": _year(g),
                "date": g.get("first_release_date"), "cover": _cover(g), "score": _score(g),
                "ratings": g.get("total_rating_count") or 0, "plats": sorted(g.get("platforms") or []),
                "remake_of": _id(g.get("parent_game")) if _type(g) == 8 else None, "orig_year": None,
                "keys": sorted(set().union(*(by_id.get(i, set()) for i in [g["id"], *editions.get(g["id"], [])]))),
            }
            e = dict(e)
            e["spin"] = False
            e["section"] = sec
            best = max(kids.get(g["id"], []), key=lambda x: x.get("total_rating_count") or 0, default=None)
            if best and (e.get("score") is None or not e.get("cover")):
                e["score"] = e.get("score") if e.get("score") is not None else _score(best)
                e["cover"] = e.get("cover") or _cover(best)
                e["ratings"] = max(e.get("ratings") or 0, best.get("total_rating_count") or 0)
            if it.get("title"):
                e["title"] = it["title"]
            if it.get("remake_of"):
                e["remake_of"] = int(it["remake_of"])
            chosen[g["id"]] = e
            order.append(g["id"])
    if not chosen:
        return entries
    out = [chosen[i] for i in dict.fromkeys(order)]
    for it in cur.get("spin") or []:
        g = known.get(it.get("id")) if it.get("id") else None
        if g and g["id"] not in chosen and g["id"] not in by_entry:
            by_entry[g["id"]] = {"id": g["id"], "title": it.get("title") or g.get("name"), "slug": g.get("slug"),
                                 "year": _year(g), "date": g.get("first_release_date"), "cover": _cover(g),
                                 "score": _score(g), "ratings": g.get("total_rating_count") or 0, "plats": [],
                                 "remake_of": None, "orig_year": None, "spin": True, "section": None,
                                 "keys": sorted(by_id.get(g["id"], set()))}
    sections = [s for s, _ in groups if s]
    for e in by_entry.values():
        if e["id"] in chosen or e["id"] in drop:
            continue
        e = dict(e)
        new = (e.get("date") or 0) > since_ts
        if not (new and not e["spin"]):
            e["spin"] = True
        # New main games in an umbrella go to the sub-series they were auto-sorted into, if curated has it.
        e["section"] = (e.get("section") if e.get("section") in sections else (sections[-1] if sections else None)) \
            if not e["spin"] else None
        out.append(e)
    # Remake years for originals found through curation
    ids = {e["id"]: e for e in out}
    for e in out:
        if e.get("remake_of") and not e.get("orig_year"):
            o = ids.get(e["remake_of"]) or known.get(e["remake_of"])
            e["orig_year"] = (o or {}).get("year") or _year(o or {})
    return out
