"""Time to beat from HowLongToBeat, with IGDB's own time-to-beat data as a fallback.

HowLongToBeat has no official API. The howlongtobeatpy library reads the same search the
website uses and is kept up to date when the site changes. Every lookup is best-effort:
if HowLongToBeat is unreachable or changes, the run carries on and IGDB fills the gaps.
"""
import time

from . import titles
from .util import days_since, log, now_iso

REFRESH_DAYS = 90       # times settle soon after release
MISS_RETRY_DAYS = 30    # look again for games HowLongToBeat didn't have
MIN_SIMILARITY = 0.8


def _hours(v):
    return round(float(v) * 2) / 2 if isinstance(v, (int, float)) and v > 0 else None


def lookup(client, title, year=None):
    """Best HowLongToBeat entry for a title, or None."""
    query = titles.clean(title)
    results = client.search(query) or []
    best, best_s = None, 0.0
    for r in results:
        names = [r.game_name or ""] + [a.strip() for a in (r.game_alias or "").split(",") if a.strip()]
        s = max(titles.similarity(query, n) for n in names)
        ry = r.release_world if isinstance(r.release_world, int) else None
        if year and ry:
            s += 0.05 if abs(ry - year) <= 1 else -0.15
        if not (r.main_story or r.main_extra or r.completionist):
            s -= 0.1  # no times submitted yet
        if s > best_s:
            best, best_s = r, s
    if not best or best_s < MIN_SIMILARITY:
        return None
    return {
        "main": _hours(best.main_story),
        "extra": _hours(best.main_extra),
        "complete": _hours(best.completionist),
        "hltb_id": best.game_id,
        "hltb_name": best.game_name,
    }


def refresh(rows, cache, budget=200, delay=1.0):
    """rows: [(key, title, year)] for visible games. Updates cache in place; returns a status string."""
    try:
        from howlongtobeatpy import HowLongToBeat
    except ImportError:
        return "not installed"
    client = HowLongToBeat(0.4)

    def due(key):
        c = cache.get(key)
        if not c:
            return True
        return days_since(c.get("checked")) >= (REFRESH_DAYS if c.get("hltb_id") else MISS_RETRY_DAYS)

    todo = [r for r in rows if due(r[0])]
    todo.sort(key=lambda r: r[0] in cache)  # never-checked games first
    log(f"HowLongToBeat: {len(todo)} games due, doing up to {budget} this run")
    done = errors = found = 0
    for key, title, year in todo[:budget]:
        try:
            res = lookup(client, title, year)
        except Exception as e:
            errors += 1
            log(f"  HowLongToBeat lookup failed for {title!r}: {type(e).__name__}: {str(e)[:120]}")
            if errors >= 10 and done == 0:
                return f"unreachable ({type(e).__name__})"
            continue
        done += 1
        cache[key] = {**(res or {}), "checked": now_iso()}
        found += bool(res)
        time.sleep(delay)
    log(f"HowLongToBeat: found times for {found} of {done} games checked")
    if done == 0 and errors:
        return f"failing ({errors} errors)"
    return "ok"
