"""IGDB client (free, via a Twitch developer app): matching, metadata and ratings.

Matching strategy, best evidence first:
  1. Manual fixes from the app ("Wrong match?") or config/overrides.yml
  2. Steam app id -> IGDB through IGDB's external_games table (exact)
  3. Title search: several cleaned variants of the store title, IGDB's search, exact-name
     and alternative-name lookups (stopping early once a match is clear). Every candidate is scored on
     name similarity, platform (a PS5 game must exist on PS5), game type (no DLC/packs),
     year hints and popularity. Weak matches are rejected rather than guessed.
"""
import math
import time

import requests

from . import titles
from .util import log, session

GAME_FIELDS = [
    "name", "slug", "url", "summary", "first_release_date", "platforms", "version_parent", "parent_game",
    "aggregated_rating", "aggregated_rating_count", "rating", "rating_count", "total_rating_count",
    "cover.image_id", "genres.name", "themes.name", "keywords.name", "franchises.name", "franchise.name",
    "collections.name", "game_modes.name", "player_perspectives.name", "alternative_names.name",
    "involved_companies.company.name", "involved_companies.developer", "involved_companies.publisher",
    "game_type",
]
# IGDB renames fields from time to time; if one is rejected, retry with its older name (or without it).
FIELD_FALLBACKS = {"game_type": "category", "collections.name": "collection.name"}

PLATFORMS = {
    "Steam": {6, 14, 3},          # PC, Mac, Linux
    "PS4": {48},
    "PS5": {167},
    "PlayStation": {48, 167},
    "Switch": {130},
    "Switch 2": {508, 130},
    "3DS": {37, 137},
    "Wii U": {41},
}
VR_PLATFORMS = {165, 390}        # PS VR, PS VR2
NEAR_PLATFORMS = {"PS5": {48}, "PS4": {165}}  # e.g. a PS4 game owned through a PS5 entitlement

BAD_TYPES = {1, 2, 5, 6, 7, 13, 14}  # DLC, expansion, mod, episode, season, pack, update
BUNDLE = 3

GENRE_SHORT = {
    "Role-playing (RPG)": "RPG",
    "Real Time Strategy (RTS)": "RTS",
    "Turn-based strategy (TBS)": "Turn-based strategy",
    "Hack and slash/Beat 'em up": "Hack and slash",
    "Card & Board Game": "Card & board",
    "Quiz/Trivia": "Quiz",
}

ACCEPT = 0.86
HIGH = 0.95


def _q(s: str) -> str:
    return s.replace("\\", " ").replace('"', '\\"')


class IGDB:
    def __init__(self, client_id: str, client_secret: str):
        r = session.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {r.json()['access_token']}",
            "Accept": "application/json",
        }
        self._last = 0.0
        self.fields = list(GAME_FIELDS)
        self.errors = 0

    # -------------------------------------------------------------- transport
    def query(self, endpoint: str, body: str):
        last_err = None
        for attempt in range(6):
            wait = 0.28 - (time.time() - self._last)  # IGDB allows 4 requests/second
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = session.post(f"https://api.igdb.com/v4/{endpoint}", headers=self.headers, data=body, timeout=60)
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = requests.HTTPError(f"{r.status_code} from IGDB")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 400:
                raise requests.HTTPError(f"400 from IGDB: {r.text[:300]}", response=r)
            r.raise_for_status()
            return r.json()
        self.errors += 1
        raise last_err or RuntimeError("IGDB request failed")

    def _fields(self, prefix=""):
        return ",".join(prefix + f for f in self.fields)

    def _with_field_fallback(self, fn):
        for _ in range(len(FIELD_FALLBACKS) + 1):
            try:
                return fn()
            except requests.HTTPError as e:
                text = str(e)
                if "400" not in text:
                    raise
                swapped = False
                for new, old in FIELD_FALLBACKS.items():
                    base = new.split(".")[0]
                    if new in self.fields and base in text:
                        self.fields = [old if f == new else f for f in self.fields]
                        log(f"  IGDB rejected field '{new}', using '{old}' instead")
                        swapped = True
                        break
                if not swapped:
                    raise
        return fn()

    # -------------------------------------------------------------- lookups
    def games_by_ids(self, ids):
        ids = sorted({int(i) for i in ids if i})
        out = {}

        def run(chunk):
            return self.query("games", f"fields {self._fields()}; where id = ({chunk}); limit 500;")

        for i in range(0, len(ids), 200):
            chunk = ",".join(map(str, ids[i : i + 200]))
            for g in self._with_field_fallback(lambda: run(chunk)):
                out[g["id"]] = g
        return out

    def game_by_slug(self, slug):
        res = self._with_field_fallback(
            lambda: self.query("games", f'fields {self._fields()}; where slug = "{_q(slug)}"; limit 1;')
        )
        return res[0] if res else None

    def steam_appids_to_games(self, appids):
        """Map Steam app ids to IGDB game ids using IGDB's external_games table."""
        appids = [str(a) for a in appids]
        out = {}
        for i in range(0, len(appids), 200):
            uids = ",".join(f'"{a}"' for a in appids[i : i + 200])
            rows = None
            for where in ("external_game_source = 1", "category = 1"):
                try:
                    rows = self.query("external_games", f"fields game,uid; where uid = ({uids}) & {where}; limit 500;")
                    break
                except requests.HTTPError:
                    continue
            for row in rows or []:
                if row.get("game"):
                    out[str(row["uid"])] = row["game"]
        return out

    # -------------------------------------------------------------- matching
    def _games(self, where_or_search: str, limit: int):
        return self._with_field_fallback(
            lambda: self.query("games", f"{where_or_search} fields id,{self._fields()}; limit {limit};")
        )

    def best_match(self, title: str, platforms: set, year: int | None = None):
        """Returns (game, score, confidence, n_candidates).

        Tries the most faithful search first and stops as soon as a confident match turns up,
        so most games cost one or two requests."""
        variants = titles.search_variants(title)
        found = {}

        def add(items):
            for g in items or []:
                if isinstance(g, dict) and g.get("id") is not None:
                    found[g["id"]] = g

        best = (None, 0.0, None)
        for i, (v, _) in enumerate(variants[:6]):
            add(self._games(f'search "{_q(v)}";', 25))
            if i == 0:
                add(self._games(f'where name ~ "{_q(v)}";', 10))
            best = pick(title, list(found.values()), variants, platforms, year)
            if best[2] == "high":
                return (*best, len(found))
        # Still unsure: IGDB's alternative names (regional titles, old names).
        alt_ids = set()
        for v, _ in variants[:2]:
            rows = self.query("alternative_names", f'fields game; where name ~ "{_q(v)}"; limit 10;')
            alt_ids |= {r["game"] for r in rows or [] if r.get("game")}
        missing = alt_ids - set(found)
        if missing:
            found.update(self.games_by_ids(missing))
            best = pick(title, list(found.values()), variants, platforms, year)
        return (*best, len(found))


def _year(g):
    ts = g.get("first_release_date")
    if not ts:
        return None
    return time.gmtime(ts).tm_year


def _game_type(g):
    t = g.get("game_type", g.get("category"))
    if isinstance(t, dict):
        t = t.get("id")
    return t


def score_candidate(title, g, variants, platforms, year=None):
    names = [g.get("name") or ""] + [a.get("name") for a in g.get("alternative_names") or [] if a.get("name")]
    best = 0.0
    for v, pen in [(titles.clean(title, strip_edition=False), 0.0)] + list(variants):
        kv = titles.key(v)
        for n in names:
            s = titles.similarity(v, n)
            if ":" in n:  # IGDB "Dying Light 2: Stay Human" vs store "Dying Light 2"
                s = max(s, titles.similarity(v, n.split(":")[0]) - 0.03)
            kn = titles.key(n)
            if kn and len(kn.split()) >= 3 and set(kn.split()) <= set(kv.split()):
                s = max(s, 0.9)  # store title adds words: "Tomb Raider I-III Remastered Starring Lara Croft"
            best = max(best, s - pen)

    score = best
    if any(titles.key(n) == titles.key(titles.clean(title)) for n in names):
        score += 0.04  # an exact name beats a subtitle match ("Ghost of Tsushima" vs "...: Director's Cut")
    gp = set(g.get("platforms") or [])
    want = set()
    for p in platforms:
        want |= PLATFORMS.get(p, set())
    if titles.is_vr(title):
        want |= VR_PLATFORMS
    plat_ok = None
    if want and gp:
        if gp & want:
            plat_ok = True
            score += 0.08
        elif any(gp & NEAR_PLATFORMS.get(p, set()) for p in platforms):
            plat_ok = True
            score -= 0.03
        else:
            plat_ok = False
            score -= 0.3

    gt = _game_type(g)
    if gt in BAD_TYPES:
        score -= 0.2
    elif gt == BUNDLE and not any(w in title.lower() for w in ("collection", "bundle", "trilogy", "anthology", "pack", "remix")):
        score -= 0.08
    if g.get("version_parent"):
        score -= 0.02

    gy = _year(g)
    if year and gy:
        score += 0.06 if abs(gy - year) <= 1 else -0.15

    score += 0.03 * min(1.0, math.log10(1 + (g.get("total_rating_count") or 0)) / 3)
    return score, best, plat_ok


def pick(title, cands, variants, platforms, year=None):
    if year is None:
        year = titles.year_hint(title)
    scored = []
    for g in cands:
        s, name_sim, plat_ok = score_candidate(title, g, variants, platforms, year)
        scored.append((s, name_sim, plat_ok, g))
    if not scored:
        return None, 0.0, None
    # If no well-named candidate is listed for the owned platform (retro re-releases, Virtual Console,
    # PS2 classics on PS4, incomplete IGDB platform data), the platform can't tell them apart, so the
    # mismatch penalty is mostly lifted instead of rejecting every candidate.
    if not any(ok for _, sim, ok, _ in scored if sim >= 0.9):
        scored = [(s + 0.22 if ok is False else s, sim, ok, g) for s, sim, ok, g in scored]
    scored.sort(key=lambda x: -x[0])
    s, name_sim, plat_ok, g = scored[0]
    if s < ACCEPT or name_sim < 0.8:
        return None, round(s, 3), None
    conf = "high" if s >= HIGH and name_sim >= 0.95 and plat_ok is not False else "medium"
    return g, round(s, 3), conf


# ---------------------------------------------------------------- metadata helpers
def _names(items):
    return [i["name"] for i in items or [] if isinstance(i, dict) and i.get("name")]


def genres_of(game) -> list[str]:
    return [GENRE_SHORT.get(n, n) for n in _names((game or {}).get("genres"))]


def metadata_of(game) -> dict:
    g = game or {}
    franchises = _names(g.get("franchises"))
    if isinstance(g.get("franchise"), dict) and g["franchise"].get("name") and g["franchise"]["name"] not in franchises:
        franchises.insert(0, g["franchise"]["name"])
    series = _names(g.get("collections")) or _names([g.get("collection")] if isinstance(g.get("collection"), dict) else [])
    devs, pubs = [], []
    for ic in g.get("involved_companies") or []:
        name = (ic.get("company") or {}).get("name") if isinstance(ic.get("company"), dict) else None
        if not name:
            continue
        if ic.get("developer") and name not in devs:
            devs.append(name)
        if ic.get("publisher") and name not in pubs:
            pubs.append(name)
    summary = (g.get("summary") or "").strip()
    if len(summary) > 420:
        summary = summary[:400].rsplit(" ", 1)[0] + "…"
    return {
        "franchises": franchises,
        "series": [s for s in series if s not in franchises],
        "themes": _names(g.get("themes")),
        "keywords": _names(g.get("keywords"))[:15],
        "modes": _names(g.get("game_modes")),
        "perspectives": _names(g.get("player_perspectives")),
        "developers": devs[:3],
        "publishers": pubs[:3],
        "summary": summary,
    }
