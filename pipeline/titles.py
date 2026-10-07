"""Title cleaning, comparison and non-game detection.

Store titles are messy ("Call of Duty®: WWII", "It Takes Two  PS4™ & PS5™", "FAR CRY6",
"KINGDOM HEARTS Ⅲ", "resident evil 4 (2005)"). Everything here turns them into something
IGDB can search for and that compares fairly against IGDB names.
"""
import difflib
import re
import unicodedata

_SYMBOLS = re.compile(r"[™®©℠]")
_PLATFORM_TAG = re.compile(
    r"[\s\-–:]*[\(\[]?\s*\b("
    r"ps4\s*(?:&|and|/|\+)\s*ps5|ps5\s*(?:&|and|/|\+)\s*ps4|ps4|ps5|ps\s*vr2?|playstation\s*(?:vr2?|[45])"
    r"|nintendo\s*switch(?:\s*2)?(?:\s*edition)?|switch\s*2\s*edition"
    r")\b\s*(?:edition|version)?\s*[\)\]]?\s*$",
    re.I,
)
_EDITION = re.compile(
    r"[\s:\-–]*[\(\[]?\b(?:digital\s+)?(?:deluxe|ultimate|complete|definitive|game of the year|goty|gold|premium|"
    r"standard|anniversary|director'?s cut|enhanced|special|collector'?s|launch|console|"
    r"cross-?gen|digital|classic|remastered|hd|next-?gen|ps[45]|playstation\s*[45]|vr)\s+edition\b[\)\]]?.*$",
    re.I,
)
_TRAILING_JUNK = re.compile(r"[\s\-–:]+$")
_YEAR_HINT = re.compile(r"\s*[\(\[]((?:19|20)\d\d)[\)\]]\s*")
_PARENS = re.compile(r"\s*[\(\[][^\)\]]{1,30}[\)\]]\s*")
_PUBLISHER_PREFIX = re.compile(r"^(?:ea\s+sports|ea|2k|ubisoft|sega|bandai namco)\s+(?=\S)", re.I)

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
          "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16}


def ascii_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")  # also turns "Ⅲ" into "III"
    s = s.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return s.encode("ascii", "ignore").decode()


def clean(title: str, strip_edition: bool = True) -> str:
    """Display/search form: no ™®, no platform tags and (optionally) no edition suffixes."""
    t = ascii_fold(_SYMBOLS.sub("", title or ""))
    t = re.sub(r"\s+", " ", t).strip()
    for _ in range(3):
        before = t
        t = _PLATFORM_TAG.sub("", t)
        if strip_edition:
            t = _EDITION.sub("", t)
        t = _TRAILING_JUNK.sub("", t).strip()
        if t == before:
            break
    t = re.sub(r"^[\-–:\s]+|[\-–:\s]+$", "", t)  # "KINGDOM HEARTS - HD 1.5+2.5 ReMIX -"
    return t or (title or "").strip()


def year_hint(title: str):
    m = _YEAR_HINT.search(title or "")
    return int(m.group(1)) if m else None


def key(title: str) -> str:
    """Comparison form: lowercase words, arabic numerals, no punctuation, no leading 'the'."""
    t = ascii_fold(_SYMBOLS.sub("", title or "")).lower()
    t = t.replace("&", " and ").replace("+", " plus ")
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", t)  # "cry6" -> "cry 6", "persona5" -> "persona 5"
    t = re.sub(r"'s\b", "s", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [str(_ROMAN[w]) if w in _ROMAN and w not in ("i",) else w for w in t.split()]
    # A lone "i" is only a numeral at the end of a title ("Final Fantasy I"), not "I Am Bread".
    if len(words) > 1 and words[-1] == "i":
        words[-1] = "1"
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


SUBTITLE_DROP = 0.2  # penalty for matching after cutting the store title's subtitle


def search_variants(title: str) -> list[tuple[str, float]]:
    """(query, penalty) pairs, most faithful first. A match found through a looser variant
    scores slightly lower, so "Mass Effect Legendary Edition" beats plain "Mass Effect"."""
    no_year = _YEAR_HINT.sub(" ", title or "")
    full = clean(no_year, strip_edition=False)
    base = clean(no_year)
    out = [(full, 0.0), (base, 0.04)]
    no_parens = clean(_PARENS.sub(" ", base))
    out.append((no_parens, 0.04))
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", no_parens)  # "FAR CRY6" -> "FAR CRY 6"
    out.append((spaced, 0.04))
    out.append((_PUBLISHER_PREFIX.sub("", spaced), 0.05))  # "EA SPORTS FIFA 23" -> "FIFA 23"
    no_vr = re.sub(r"\s*[\(\[]?\b(?:PS\s*VR2?|PlayStation\s*VR2?|VR)\b[\)\]]?\s*$", "", spaced, flags=re.I)
    out.append((no_vr, 0.05))  # "Moss VR" -> "Moss", "Alien: Rogue Incursion VR" -> "Alien: Rogue Incursion"
    out.append((re.sub(r"\bVR\b", "Virtual Reality", spaced), 0.05))  # "...Far From Home VR" -> "...Virtual Reality"
    # Remasters are usually their own IGDB entry ("Assassin's Creed III Remastered"); if not, fall
    # back to the original game. The platform check keeps it honest.
    no_remaster = re.sub(r"[\s:\-–]*\b(?:remastered|remaster|hd remaster|hd|remake|redux|reloaded)\b\s*$", "", no_vr, flags=re.I)
    out.append((no_remaster, 0.07))
    if ":" in spaced:
        out.append((spaced.split(":")[0], SUBTITLE_DROP))  # "Fall Guys: Ultimate Knockout" -> "Fall Guys"
    if " - " in spaced:
        out.append((spaced.split(" - ")[0], SUBTITLE_DROP))
    seen, res = set(), []
    for v, pen in out:
        v = re.sub(r"\s+", " ", v).strip(" -:")
        if v and len(v) >= 2 and v.lower() not in seen:
            seen.add(v.lower())
            res.append((v, pen))
    return res


def is_vr(title: str) -> bool:
    return bool(re.search(r"\bvr\b|playstation\s*vr|ps\s*vr", ascii_fold(title), re.I))


# ---------------------------------------------------------------- non-games
NON_GAMES = {
    key(n) for n in [
        "Netflix", "YouTube", "YouTube TV", "Spotify", "Hulu", "Disney+", "Disney Plus", "Prime Video",
        "Amazon Prime Video", "HBO Max", "Max", "Crunchyroll", "Funimation", "Twitch", "VEVO", "Peacock",
        "Fandango at Home", "Vudu", "Media Player", "SONY PICTURES CORE", "Apple TV", "Apple TV+", "Apple Music",
        "Paramount+", "Plex", "Tubi", "Pluto TV", "ESPN", "DAZN", "Amazon Music", "Deezer", "iHeartRadio",
        "Discovery+", "MUBI", "Rakuten TV", "NOW", "Showtime", "Starz", "Sling TV", "Philo", "fuboTV",
        "Netflix (PS4)", "Live from PlayStation", "PlayStation Plus", "PS Plus", "PlayStation Video",
        "PlayStation Music", "Share Factory", "SHAREfactory", "SHAREfactory Studio", "Web Browser",
        "PlayStation VR Demo Disc", "PlayStationVR Demo Disc", "Demo Disc", "Hulu + Live TV",
        "Sling", "Pandora", "Crackle", "Redbox", "Spectrum TV", "Xfinity Stream", "AT&T TV", "DirecTV Stream",
        "NBA League Pass", "NFL Sunday Ticket", "MLB.TV", "NHL.TV", "UFC Fight Pass", "WWE Network",
    ]
}
_NON_GAME_PATTERNS = re.compile(
    r"\b(demo disc|trial version|free trial|soundtrack|ost\b|beta\b|test server|benchmark|wallpaper|"
    r"avatar|theme pack|dynamic theme|season pass|expansion pass|upgrade pack|currency pack|"
    r"virtual currency|coins? pack|points pack)\b",
    re.I,
)


def looks_like_non_game(title: str) -> bool:
    return key(clean(title)) in NON_GAMES or bool(_NON_GAME_PATTERNS.search(ascii_fold(title)))
