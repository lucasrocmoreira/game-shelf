"""The shelf's score: a "two-pillar midpoint" (decided by the rating council, Nov 2026).

Every source is put on the Metacritic scale with a fixed linear map. Critics (Metacritic and
OpenCritic, which share most reviews; IGDB critics only when neither exists) and players (Backloggd
and IGDB users) are combined separately, and the score is the midpoint of the two. A side with no
data is estimated from the other and labelled "est."; a side the user switched off is left out.

site/index.html has a line-for-line copy of compute() (function rateGame) so the source toggles
work in the browser. Keep the two in step; the page warns in the console if they disagree.
"""
from pathlib import Path
import json

MODEL = json.loads((Path(__file__).resolve().parent.parent / "config" / "rating.json").read_text())
CRITICS = ("metacritic", "opencritic", "igdb_critic")
PLAYERS = ("backloggd", "igdb_user")


def _m(model, src, x):
    a, b = model["map"][src]
    return a * x + b


def compute(scores, on=None, model=None):
    """scores: {source: 0-100 or None}. on: sources switched on (default all).
    Returns None when nothing is known, else {score, C, P, c_est, p_est, c_weak, p_weak, conf, split, notes}."""
    M = model or MODEL
    on = set(on) if on is not None else set(CRITICS + PLAYERS)
    v = {k: scores.get(k) for k in CRITICS + PLAYERS if k in on and isinstance(scores.get(k), (int, float))}
    mu, notes = M["mu"], []

    # Critics
    mc, oc = v.get("metacritic"), v.get("opencritic")
    oc_ = _m(M, "opencritic", oc) if oc is not None else None
    igc = v.get("igdb_critic")
    igc_ = _m(M, "igdb_critic", igc) if igc is not None else None
    C, c_weak = None, False
    if mc is not None and oc_ is not None:
        if abs(mc - oc_) >= M["clash_critic"] and igc_ is not None:
            C = sorted([mc, oc_, igc_])[1]
            notes.append("Metacritic and OpenCritic disagree, so the middle of the three critic scores is used")
        else:
            C = (mc + oc_) / 2
    elif mc is not None or oc_ is not None:
        C = mc if mc is not None else oc_
    elif igc_ is not None:
        lam = M["lam_igc_round"] if igc % 10 == 0 else M["lam_igc"]
        C, c_weak = mu + lam * (igc_ - mu), True
    if igc is not None and (mc is not None or oc is not None):
        notes.append(f"IGDB critics {round(igc)}: not used, Metacritic/OpenCritic available")

    # Players
    bl = v.get("backloggd")
    bl_ = _m(M, "backloggd", bl) if bl is not None else None
    igu = v.get("igdb_user")
    igu_ = _m(M, "igdb_user", igu) if igu is not None else None
    P, p_weak = None, False
    if bl_ is not None and igu_ is not None and abs(bl_ - igu_) >= M["clash_player"]:
        notes.append(f"IGDB players {round(igu)}: set aside, disagrees with Backloggd")
        igu_ = None
    if bl_ is not None and igu_ is not None:
        P = (bl_ + igu_) / 2
    elif bl_ is not None or igu_ is not None:
        P, p_weak = mu + M["lam_player_single"] * ((bl_ if bl_ is not None else igu_) - mu), True

    c_on = any(k in on for k in CRITICS)
    p_on = any(k in on for k in PLAYERS)
    if C is None and P is None:
        return None
    c_est = p_est = False
    if C is None and c_on:
        C, c_est = mu + M["rho"] * (P - mu), True
    if P is None and p_on:
        P, p_est = mu + M["rho"] * (C - mu), True
    parts = [x for x in (C, P) if x is not None]
    score = sum(parts) / len(parts)
    if c_est or p_est:
        conf = "low"
    elif c_weak or p_weak or C is None or P is None:
        conf = "medium"
    else:
        conf = "high"
    split = C is not None and P is not None and not (c_est or p_est) and abs(C - P) >= M["split_badge"]
    r = lambda x: None if x is None else round(x, 1)
    return {"score": r(score), "C": r(C), "P": r(P), "c_est": c_est, "p_est": p_est,
            "c_weak": c_weak, "p_weak": p_weak, "conf": conf, "split": split, "notes": notes}
