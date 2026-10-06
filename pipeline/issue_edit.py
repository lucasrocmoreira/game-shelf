"""Apply an 'Add or hide a game' issue form to config/library_edits.yml."""
import os
import re

import yaml

from .util import CONFIG_DIR, norm

PATH = CONFIG_DIR / "library_edits.yml"


def parse_form(body: str) -> dict:
    """GitHub issue forms render as '### Label' followed by the answer."""
    fields = {}
    for label, value in re.findall(r"^###\s+(.+?)\s*\n+(.*?)(?=^###\s|\Z)", body or "", re.M | re.S):
        value = value.strip()
        fields[label.strip().lower()] = "" if value == "_No response_" else value
    return fields


def apply(fields: dict) -> str:
    data = (yaml.safe_load(PATH.read_text(encoding="utf-8")) if PATH.exists() else None) or {}
    added = data.get("added") or []
    hidden = data.get("hidden") or []
    title = fields.get("game title", "").strip()
    if not title:
        return "No game title found, nothing changed"
    action = fields.get("action", "Add").lower()

    if action.startswith("hide"):
        added = [g for g in added if norm(g["title"]) != norm(title)]
        if norm(title) not in {norm(h) for h in hidden}:
            hidden.append(title)
        result = f"Hid {title}"
    else:
        hidden = [h for h in hidden if norm(h) != norm(title)]
        platform = fields.get("platform") or "Switch"
        entry = {"title": title, "platform": platform}
        if fields.get("format"):
            entry["format"] = fields["format"].lower()
        hours = re.sub(r"[^\d.]", "", fields.get("hours played (optional)", ""))
        if hours:
            entry["hours"] = float(hours)
        added = [g for g in added if not (norm(g["title"]) == norm(title) and g.get("platform") == platform)]
        added.append(entry)
        result = f"Added {title} ({platform})"

    header = "# Managed by the 'Add or hide a game' issue form. You can also edit it by hand.\n"
    PATH.write_text(
        header + yaml.safe_dump({"added": added, "hidden": hidden}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    result = apply(parse_form(os.environ.get("ISSUE_BODY", "")))
    print(result)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"result={result}\n")
