"""Nintendo eShop purchases, read from the purchase-confirmation emails Nintendo sends to Gmail.

Uses IMAP with a Google app password (secrets GMAIL_ADDRESS and GMAIL_APP_PASSWORD).
Opens the mailbox read-only and only fetches emails from Nintendo's receipt sender.
"""
import email
import imaplib
import re
from email.header import decode_header, make_header

from .util import log

SENDER = "no-reply@accounts.nintendo.com"
# Subjects seen so far: "Confirmation of digital purchase from Nintendo" (2023+) and
# "Digital Purchase Confirmation from Nintendo" (older 3DS / Wii U era).
SUBJECT_WORDS = ("digital", "purchase")
GMAIL_QUERY = f"from:{SENDER} subject:(digital purchase)"

# Purchases that are add-ons rather than games. Anything else can be hidden with the issue form.
_ADDON = re.compile(
    r"\b(expansion pass|season pass|dlc|booster course|add-?on|upgrade pack|"
    r"nintendo switch 2 edition upgrade|in-game|currency|coins?|gems?|v-bucks)\b",
    re.I,
)


def _text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                return re.sub(r"<[^>]+>", "\n", html)
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


def parse_receipt(text: str) -> list[dict]:
    """Extract purchased games from one receipt body."""
    device = re.search(r"Device Type:\s*(.+)", text)
    device = device.group(1).strip().lower() if device else ""
    if "switch 2" in device:
        platform = "Switch 2"
    elif "switch" in device:
        platform = "Switch"
    elif "wii u" in device:
        platform = "Wii U"
    elif "3ds" in device or re.search(r"Serial Number:", text):
        platform = "3DS"  # old 3DS/Wii U eShop receipts have a serial number and no device type
    else:
        platform = "Switch"
    date = re.search(r"Transaction Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
    iso = f"{date.group(3)}-{int(date.group(1)):02d}-{int(date.group(2)):02d}" if date else None
    txn = re.search(r"Transaction ID:\s*(\d+)", text)

    games = []
    for m in re.finditer(r"Purchased Items?:\s*(.+)", text):  # "Purchased Membership:" is skipped
        title = m.group(1).strip()
        if not title or _ADDON.search(title):
            continue
        games.append(
            {
                "title": title,
                "platform": platform,
                "purchased": iso,
                "transaction": txn.group(1) if txn else None,
            }
        )
    return games


def fetch_receipts(address: str, app_password: str) -> list[dict]:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(address, app_password.replace(" ", ""))
        # The "All Mail" folder name depends on Gmail's language, so find it by its \All flag.
        all_mail = None
        _, folders = imap.list()
        for f in folders or []:
            line = f.decode(errors="replace")
            if "\\All" in line:
                all_mail = line.rsplit(' "/" ', 1)[-1]
                break
        status, _ = imap.select(all_mail or "INBOX", readonly=True)
        if status != "OK":
            imap.select("INBOX", readonly=True)
        status, data = imap.uid("SEARCH", "X-GM-RAW", f'"{GMAIL_QUERY}"')
        uids = data[0].split() if status == "OK" and data and data[0] else []
        games = []
        for i in range(0, len(uids), 50):
            batch = b",".join(uids[i : i + 50])
            status, parts = imap.uid("FETCH", batch, "(BODY.PEEK[])")  # PEEK: never marks as read
            for part in parts:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                subject = str(make_header(decode_header(msg.get("Subject", ""))))
                if not all(w in subject.lower() for w in SUBJECT_WORDS):
                    continue
                games.extend(parse_receipt(_text_body(msg)))
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    # Same game bought twice (e.g. re-download) only counts once per platform.
    seen, out = set(), []
    for g in sorted(games, key=lambda g: g.get("purchased") or ""):
        k = (g["title"].lower(), g["platform"])
        if k not in seen:
            seen.add(k)
            out.append(g)
    log(f"Nintendo receipts: {len(out)} games from {len(uids)} emails")
    return out
