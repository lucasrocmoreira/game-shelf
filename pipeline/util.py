"""Shared helpers: paths, HTTP session with polite rate limiting, JSON cache, title normalization."""
import datetime as dt
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
SITE_DIR = ROOT / "site"
CONFIG_DIR = ROOT / "config"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

session = requests.Session()
session.headers["User-Agent"] = BROWSER_UA
session.headers["Accept-Language"] = "en-US,en;q=0.9"

_last_call: dict[str, float] = {}


def polite_get(url, host_delay=1.0, retries=3, **kw):
    """GET with a per-host minimum delay and simple backoff on 429/5xx."""
    host = url.split("/")[2]
    kw.setdefault("timeout", 30)
    r = None
    for attempt in range(retries):
        wait = host_delay - (time.time() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            r = session.get(url, **kw)
        except requests.RequestException as e:
            log(f"  network error on {host}: {e}")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        return r
    return r


def log(msg):
    print(msg, flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data, pretty=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1 if pretty else None, sort_keys=pretty)
    path.write_text(text + "\n", encoding="utf-8")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def days_since(iso):
    if not iso:
        return 10_000
    try:
        then = dt.datetime.fromisoformat(iso)
    except ValueError:
        return 10_000
    return (dt.datetime.now(dt.timezone.utc) - then).days


_PLATFORM_TAG = re.compile(
    r"\s*[\(\[]?\b(ps4|ps5|ps4\s*(&|and|/)\s*ps5|playstation\s*[45]|nintendo switch(\s*2)?)\b"
    r"(\s*(edition|version))?[\)\]]?\s*$",
    re.I,
)
_EDITION = re.compile(
    r"[\s:\-–]*\b(digital\s+)?(deluxe|ultimate|complete|definitive|game of the year|goty|gold|premium|"
    r"standard|anniversary|director'?s cut|enhanced|special|collector'?s|launch)\s+edition\b.*$",
    re.I,
)


def clean_title(title: str) -> str:
    t = re.sub(r"[™®©]", "", title or "").strip()
    for _ in range(2):
        t = _PLATFORM_TAG.sub("", t).strip(" -–:")
        t = _EDITION.sub("", t).strip(" -–:")
    return t


def norm(title: str) -> str:
    t = clean_title(title).lower()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slugify(title: str) -> str:
    t = clean_title(title).lower()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"['’.]", "", t)
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")
