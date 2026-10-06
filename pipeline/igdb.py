"""IGDB client (free, via a Twitch developer app). Provides matching, genres, slugs and critic/user ratings."""
import difflib
import time

import requests

from .util import log, norm, session

GAME_FIELDS = (
    "name,slug,url,genres.name,aggregated_rating,aggregated_rating_count,"
    "rating,rating_count,first_release_date,cover.image_id,version_parent"
)

GENRE_SHORT = {
    "Role-playing (RPG)": "RPG",
    "Real Time Strategy (RTS)": "RTS",
    "Turn-based strategy (TBS)": "Turn-based strategy",
    "Hack and slash/Beat 'em up": "Hack and slash",
    "Card & Board Game": "Card & board",
    "Quiz/Trivia": "Quiz",
    "Point-and-click": "Point-and-click",
}


class IGDB:
    def __init__(self, client_id: str, client_secret: str):
        r = session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {r.json()['access_token']}",
            "Accept": "application/json",
        }
        self._last = 0.0

    def query(self, endpoint: str, body: str):
        for attempt in range(4):
            wait = 0.3 - (time.time() - self._last)  # IGDB allows 4 requests/second
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            r = session.post(
                f"https://api.igdb.com/v4/{endpoint}", headers=self.headers, data=body, timeout=30
            )
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return []

    # -------------------------------------------------------------- lookups
    def games_by_ids(self, ids):
        ids = sorted({int(i) for i in ids if i})
        out = {}
        for i in range(0, len(ids), 400):
            chunk = ",".join(map(str, ids[i : i + 400]))
            for g in self.query("games", f"fields {GAME_FIELDS}; where id = ({chunk}); limit 500;"):
                out[g["id"]] = g
        return out

    def steam_appids_to_games(self, appids):
        """Map Steam app ids to IGDB game ids using IGDB's external_games table."""
        appids = [str(a) for a in appids]
        out = {}
        for i in range(0, len(appids), 200):
            uids = ",".join(f'"{a}"' for a in appids[i : i + 200])
            rows = None
            for where in (f"external_game_source = 1", f"category = 1"):
                try:
                    rows = self.query(
                        "external_games", f"fields game,uid; where uid = ({uids}) & {where}; limit 500;"
                    )
                    break
                except requests.HTTPError:
                    continue
            for row in rows or []:
                if row.get("game"):
                    out[str(row["uid"])] = row["game"]
        return out

    def search(self, title: str):
        """Best IGDB match for a title, or None when nothing is close enough."""
        q = title.replace('"', '\\"')
        try:
            results = self.query("games", f'search "{q}"; fields {GAME_FIELDS}; limit 15;')
        except requests.HTTPError as e:
            log(f"  IGDB search failed for {title!r}: {e}")
            return None
        target = norm(title)
        best, best_score = None, 0.0
        for g in results:
            score = difflib.SequenceMatcher(None, target, norm(g.get("name", ""))).ratio()
            if g.get("version_parent"):
                score -= 0.05  # prefer the main game over editions/ports
            if norm(g.get("name", "")) == target:
                score += 0.1
            if score > best_score:
                best, best_score = g, score
        return best if best_score >= 0.86 else None


def genres_of(game) -> list[str]:
    return [GENRE_SHORT.get(g["name"], g["name"]) for g in (game or {}).get("genres", []) if g.get("name")]
