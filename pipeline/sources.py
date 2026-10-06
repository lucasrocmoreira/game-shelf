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


def fetch_psn(npsso: str):
    """Games played on the account (PS4/PS5), via PlayStation's title stats.

    Note: PSN only exposes games that have been launched at least once.
    """
    from psnawp_api import PSNAWP  # imported lazily so the rest works without it

    psn = PSNAWP(npsso)
    me = psn.me()
    try:
        stats = me.title_stats(limit=None)
    except TypeError:
        stats = me.title_stats()

    out = []
    for t in stats:
        name = getattr(t, "name", None) or getattr(t, "title_name", None)
        if not name:
            continue
        dur = getattr(t, "play_duration", None)
        hours = round(dur.total_seconds() / 3600, 1) if dur else 0.0
        last = getattr(t, "last_played_date_time", None)
        out.append(
            {
                "platform": _platform_from_category(getattr(t, "category", "")),
                "source_id": str(getattr(t, "title_id", name)),
                "title": name,
                "playtime_hours": hours,
                "last_played": last.date().isoformat() if last else None,
                "cover": getattr(t, "image_url", None),
            }
        )
    log(f"PlayStation: {len(out)} games")
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
