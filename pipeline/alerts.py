"""Open a GitHub issue when a library source stops working (and close it once it works again).

GitHub emails you about new issues in your repository, with the issue text in the email, so an expired
PlayStation sign-in token or Gmail app password doesn't go unnoticed. Run by the workflow after each
update; needs GH_TOKEN. `python -m pipeline.alerts --preview playstation` prints an alert without opening it.
"""
import json
import os
import subprocess
import sys

from .util import SITE_DIR, now_iso

REPO = os.environ.get("GITHUB_REPOSITORY", "lucasrocmoreira/game-shelf")
URL = f"https://github.com/{REPO}"
SECRETS = f"{URL}/settings/secrets/actions"
RUN = f"{URL}/actions/workflows/update.yml"
SITE = f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}/"

FINISH = f"""
### Last step: run the update and check
1. Open the update workflow: {RUN}
2. Click **Run workflow** (top right of the list), then the green **Run workflow** button.
3. Wait for the run to get a green check mark (5 to 30 minutes), then reload your app: {SITE}
4. This issue closes by itself after that run. If it stays open, the new value didn't work: repeat the steps above.
"""

GUIDES = {
    "playstation": ("PlayStation library", f"""
Your PlayStation sign-in token (called **NPSSO**) has most likely expired. Sony makes them last about two
months, so this is expected every so often. Your PlayStation games are still on your shelf (the last list
is kept); only new purchases, PS Plus claims and playtime are paused until you update it. Takes about 3 minutes.

### 1. Sign in to PlayStation in a browser
- On a computer (easiest), open **https://www.playstation.com** and click **Sign In** (top right).
- Use the same account as your PS5. If you're already signed in, sign out and back in to get a fresh token.

### 2. Copy your new token
- In the **same browser**, open: **https://ca.account.sony.com/api/v1/ssocookie**
- You'll see one line of text like `{{"npsso":"aBcD...xYz"}}`.
- Copy **only the long value between the quotes after `npsso`** (64 letters and numbers, no quotes, no spaces).
- If you see an error instead, the sign-in didn't stick: go back to step 1, sign in, then try this link again.
- Don't sign out of playstation.com afterwards (signing out can cancel the token). Just close the tab.
- Treat this value like a password: don't paste it anywhere except the GitHub secret below.

### 3. Save it in GitHub
- Open the secret directly: **{SECRETS}/PSN_NPSSO**
  (or {SECRETS} and click the pencil next to **PSN_NPSSO**)
- Paste the value into **Value** and click **Update secret**.
"""),
    "nintendo_eshop": ("Nintendo eShop receipts", f"""
The app couldn't read your Nintendo eShop receipt emails in Gmail. Usually the Gmail **app password** stopped
working: Google cancels app passwords when you change your Google password, and sometimes after security reviews.
Your Nintendo games stay on your shelf; only new eShop purchases are paused. Takes about 3 minutes.

### 1. Create a new app password
- Open **https://myaccount.google.com/apppasswords** and sign in with the Gmail account that gets your Nintendo receipts.
- If the page says app passwords aren't available, turn on 2-Step Verification first:
  **https://myaccount.google.com/signinoptions/twosv**, then open the app passwords link again.
- Delete the old **Game Shelf** entry if it's listed, then type `Game Shelf` as the app name and click **Create**.
- Copy the 16-letter password Google shows (you can leave out the spaces). It's only shown once.

### 2. Save it in GitHub
- Open **{SECRETS}/GMAIL_APP_PASSWORD**, paste it into **Value**, click **Update secret**.
- If you changed which Gmail address gets the receipts, also update **{SECRETS}/GMAIL_ADDRESS**.
"""),
    "steam": ("Steam library", f"""
The app couldn't read your Steam library. Your Steam games stay on your shelf; only new purchases and playtime are paused.

### 1. Check your Steam privacy setting (the usual cause)
- Open **https://steamcommunity.com/my/edit/settings** (sign in if asked).
- Set **Game details** to **Public**, and make sure it isn't overridden by **My profile: Private**. Click **Save** if you changed anything.

### 2. If that was already public, renew the API key
- Open **https://steamcommunity.com/dev/apikey**, revoke the old key if shown, and register a new one
  (any domain name works, for example `localhost`). Copy the key.
- Open **{SECRETS}/STEAM_API_KEY**, paste it into **Value**, click **Update secret**.
- Your **STEAM_ID** secret should be your 17-digit SteamID or profile name: **{SECRETS}/STEAM_ID**
"""),
    "igdb": ("IGDB (game info and matching)", f"""
The app couldn't sign in to IGDB, which provides covers, genres, scores and game matching. Your shelf keeps
yesterday's data; new games won't be matched until this is fixed.

### 1. Make a new client secret on Twitch
- Open **https://dev.twitch.tv/console/apps** and sign in (Twitch requires two-factor authentication).
- Click **Manage** next to your Game Shelf application, then **New Secret**, and copy it.
- The **Client ID** is on the same page; copy it too if it changed.

### 2. Save it in GitHub
- Open **{SECRETS}/IGDB_CLIENT_SECRET**, paste the secret, click **Update secret**.
- If needed, also **{SECRETS}/IGDB_CLIENT_ID**.
"""),
}
LABEL = "shelf-alert"


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def body_for(key, status):
    name, guide = GUIDES[key]
    return (f"**The daily update on {now_iso()[:10]} couldn't read your {name}.**\n\n"
            f"What the update reported: `{status}`\n" + guide + FINISH +
            f"\n---\nYour app: {SITE} · All your secrets: {SECRETS} · Recent updates: {URL}/actions")


def main():
    if "--preview" in sys.argv:
        key = sys.argv[-1]
        print(body_for(key, "example error text"))
        return
    status = json.loads((SITE_DIR / "data.json").read_text(encoding="utf-8")).get("status", {})
    gh("label", "create", LABEL, "--color", "D93F0B", "--description", "A Game Shelf source needs attention")
    open_issues = json.loads(gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number,title").stdout or "[]")
    for key, (name, _) in GUIDES.items():
        title = f"Game Shelf: action needed, {name} stopped updating"
        existing = [i for i in open_issues if i["title"] == title]
        st = status.get(key)
        broken = st not in (None, "ok", "not configured")
        if broken and not existing:
            gh("issue", "create", "--title", title, "--body", body_for(key, st), "--label", LABEL)
            print(f"opened an issue: {title}")
        elif not broken and existing:
            for i in existing:
                gh("issue", "close", str(i["number"]), "--comment", f"{name} is working again. Thanks!")
                print(f"closed issue #{i['number']}")


if __name__ == "__main__":
    main()
