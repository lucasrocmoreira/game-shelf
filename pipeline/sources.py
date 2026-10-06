"""Library sources: Steam (official API), PlayStation (PSNAWP), Nintendo (manual list)."""
import datetime as dt

import yaml

from .util import CONFIG_DIR, log, session


def _ts(epoch):
    if not epoch:
        return None
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).date().isoformat()


# ---------------------------------------------------------------- Steam
def fetch_steam(api_key: str, steam_id: str):
    steam_id = steam_id.strip().rstrip("/").split("/")[-1]
    if not steam_id.isdigit():
        r = session.get(
            "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
            params={"key": api_key, "vanityurl": steam_id},
            timeout=30,
        )
        r.raise_for_status()
        resolved = r.json().get("response", {})
        if resolved.get("success") != 1:
            raise RuntimeError(f"Could not resolve Steam vanity name '{steam_id}'")
        steam_id = resolved["steamid"]

    r = session.get(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
        params={
            "key": api_key,
            "steamid": steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "format": "json",
        },
        timeout=60,
    )
    r.raise_for_status()
    games = r.json().get("response", {}).get("games", [])
    if not games:
        raise RuntimeError(
            "Steam returned no games. Check that Profile > Privacy > 'Game details' is set to Public."
        )
    out = []
    for g in games:
        out.append(
            {
                "platform": "Steam",
                "source_id": str(g["appid"]),
                "title": g.get("name") or f"App {g['appid']}",
                "playtime_hours": round(g.get("playtime_forever", 0) / 60, 1),
                "last_played": _ts(g.get("rtime_last_played")),
                "steam_appid": g["appid"],
            }
        )
    log(f"Steam: {len(out)} games")
    return out


# ---------------------------------------------------------------- PlayStation
def _platform_from_category(cat) -> str:
    c = str(getattr(cat, "value", cat) or "").lower()
    if "ps5" in c:
        return "PS5"
    if "ps4" in c:
        return "PS4"
    return "PlayStation"


def _entitlement_platform(ent) -> str:
    plats = {str(a.get("platformId", "")).lower() for a in ent.get("entitlementAttributes") or []}
    pkg = str((ent.get("gameMeta") or {}).get("packageType", "")).upper()
    if "ps5" in plats or pkg == "PSGD":
        return "PS5"
    if "ps4" in plats or pkg == "PS4GD":
        return "PS4"
    return "PlayStation"


def _is_ps_plus(ent) -> bool:
    reward = ent.get("rewardMeta") or {}
    return bool(ent.get("isSubscription") or reward.get("rewardServiceType"))


def fetch_psn(npsso: str):
    """PS4/PS5 games on the account.

    Combines two PlayStation sources:
      - title stats: every game launched at least once, with playtime
      - entitlements: every game owned digitally, including PS Plus monthly claims, played or not
    Physical discs only appear once they have been played.
    """
    from psnawp_api import PSNAWP  # imported lazily so the rest works without it

    psn = PSNAWP(npsso)
    me = psn.me()

    by_id = {}
    try:
        stats = me.title_stats(limit=None)
    except TypeError:
        stats = me.title_stats()
    for t in stats:
        name = getattr(t, "name", None) or getattr(t, "title_name", None)
        if not name:
            continue
        dur = getattr(t, "play_duration", None)
        last = getattr(t, "last_played_date_time", None)
        tid = str(getattr(t, "title_id", None) or name)
        by_id[tid] = {
            "platform": _platform_from_category(getattr(t, "category", "")),
            "source_id": tid,
            "title": name,
            "playtime_hours": round(dur.total_seconds() / 3600, 1) if dur else 0.0,
            "last_played": last.date().isoformat() if last else None,
            "cover": getattr(t, "image_url", None),
            "ps_plus": False,
        }
    played = len(by_id)

    owned_only = 0
    try:
        for ent in me.game_entitlements(page_size=200):
            if ent.get("isGame") is False or ent.get("isBeta") or ent.get("activeFlag") is False:
                continue
            title_meta = ent.get("titleMeta") or {}
            concept = ent.get("conceptMeta") or {}
            name = concept.get("name") or title_meta.get("name") or (ent.get("gameMeta") or {}).get("name")
            tid = title_meta.get("titleId") or ent.get("productId") or name
            if not name:
                continue
            plus = _is_ps_plus(ent)
            if tid in by_id:
                by_id[tid]["ps_plus"] = by_id[tid]["ps_plus"] or plus
                continue
            by_id[tid] = {
                "platform": _entitlement_platform(ent),
                "source_id": tid,
                "title": name,
                "playtime_hours": 0.0,
                "last_played": None,
                "cover": title_meta.get("imageUrl") or concept.get("iconUrl"),
                "ps_plus": plus,
            }
            owned_only += 1
    except Exception as e:  # owned list is a bonus; never lose the played list over it
        log(f"  PlayStation owned-games list unavailable ({type(e).__name__}: {e}); using played games only")

    # The same game can show up once per platform version; keep one entry per platform and name.
    out, seen = [], set()
    for g in sorted(by_id.values(), key=lambda g: -g["playtime_hours"]):
        k = (g["platform"], g["title"].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(g)
    log(f"PlayStation: {len(out)} games ({played} played, {owned_only} owned but never launched)")
    return out


# ---------------------------------------------------------------- Manual lists
def _entries(path):
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("games") or data.get("added") or []


def load_manual():
    """Games from config/nintendo.yml plus anything added with the 'Add or hide a game' issue form."""
    out = []
    for path in (CONFIG_DIR / "nintendo.yml", CONFIG_DIR / "library_edits.yml"):
        for e in _entries(path):
            if isinstance(e, str):
                e = {"title": e}
            if not e.get("title"):
                continue
            out.append(
                {
                    "platform": e.get("platform", "Switch"),
                    "source_id": f"manual:{e['title']}",
                    "title": str(e["title"]),
                    "playtime_hours": float(e.get("hours") or 0),
                    "last_played": None,
                    "format": e.get("format"),
                }
            )
    log(f"Manual list: {len(out)} games")
    return out


def hidden_titles():
    path = CONFIG_DIR / "library_edits.yml"
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("hidden") or []


def load_nintendo_receipts(address, app_password):
    from .receipts import fetch_receipts

    return [
        {
            "platform": g["platform"],
            "source_id": f"eshop:{g['title']}",
            "title": g["title"],
            "playtime_hours": 0.0,
            "last_played": None,
            "format": "digital",
        }
        for g in fetch_receipts(address, app_password)
    ]
