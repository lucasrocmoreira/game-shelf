"""Open a GitHub issue when a library source stops working (and close it once it works again).

GitHub emails you about new issues in your repository, so an expired PlayStation sign-in token or
Gmail app password doesn't go unnoticed. Run by the workflow after each update; needs GH_TOKEN.
"""
import json
import subprocess

from .util import SITE_DIR

WATCH = {
    "playstation": ("PlayStation library", [
        "Your PSN sign-in token (NPSSO) has most likely expired. They last about two months.",
        "1. Sign in at https://www.playstation.com",
        "2. Open https://ca.account.sony.com/api/v1/ssocookie and copy the `npsso` value",
        "3. Repository Settings > Secrets and variables > Actions > PSN_NPSSO > Update",
        "Your last PlayStation list stays on the site until then.",
    ]),
    "steam": ("Steam library", ["Check the STEAM_API_KEY and STEAM_ID secrets, and that your Steam game details are public."]),
    "nintendo_eshop": ("Nintendo eShop receipts", ["Check that the GMAIL_APP_PASSWORD secret is still valid (https://myaccount.google.com/apppasswords)."]),
    "igdb": ("IGDB", ["Check the IGDB_CLIENT_ID and IGDB_CLIENT_SECRET secrets (https://dev.twitch.tv/console)."]),
}
LABEL = "shelf-alert"


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def main():
    status = json.loads((SITE_DIR / "data.json").read_text(encoding="utf-8")).get("status", {})
    gh("label", "create", LABEL, "--color", "D93F0B", "--description", "A Game Shelf source needs attention")
    open_issues = json.loads(gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number,title").stdout or "[]")
    for key, (name, steps) in WATCH.items():
        title = f"Game Shelf: {name} needs attention"
        existing = [i for i in open_issues if i["title"] == title]
        st = status.get(key)
        broken = st not in (None, "ok", "not configured")
        if broken and not existing:
            body = f"The daily update couldn't read your {name}.\n\nError: `{st}`\n\n" + "\n".join(steps) + \
                   "\n\nThis issue closes itself after the first update that works again."
            gh("issue", "create", "--title", title, "--body", body, "--label", LABEL)
            print(f"opened an issue: {title}")
        elif not broken and existing:
            for i in existing:
                gh("issue", "close", str(i["number"]), "--comment", f"{name} works again.")
                print(f"closed issue #{i['number']}")


if __name__ == "__main__":
    main()
