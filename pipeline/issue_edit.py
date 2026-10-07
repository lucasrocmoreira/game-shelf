"""Apply an 'Add or hide a game' issue form to config/library_edits.json."""
import os
import re

from . import edits, titles


def parse_form(body: str) -> dict:
    """GitHub issue forms render as '### Label' followed by the answer."""
    fields = {}
    for label, value in re.findall(r"^###\s+(.+?)\s*\n+(.*?)(?=^###\s|\Z)", body or "", re.M | re.S):
        value = value.strip()
        fields[label.strip().lower()] = "" if value == "_No response_" else value
    return fields


def apply(fields: dict) -> str:
    data = edits.load()
    title = fields.get("game title", "").strip()
    if not title:
        return "No game title found, nothing changed"
    action = fields.get("action", "Add").lower()

    if action.startswith("hide"):
        # Hide by title; the build also matches this against IGDB names.
        edits.hide(data, "t:" + titles.key(titles.clean(title)), title)
        data["added"] = [g for g in data["added"] if titles.key(g.get("title", "")) != titles.key(title)]
        result = f"Hid {title}"
    else:
        platform = fields.get("platform") or "Switch"
        hours = re.sub(r"[^\d.]", "", fields.get("hours played (optional)", ""))
        edits.add(data, title, platform, (fields.get("format") or "").lower() or None, float(hours) if hours else None)
        result = f"Added {title} ({platform})"
    edits.save(data)
    return result


if __name__ == "__main__":
    result = apply(parse_form(os.environ.get("ISSUE_BODY", "")))
    print(result)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"result={result}\n")
