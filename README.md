# Game Shelf

Your Steam, PlayStation and Nintendo games in one list, ranked by Metacritic, OpenCritic, Backloggd,
IGDB and Steam review scores, with filters by platform and genre. A GitHub Actions job rebuilds it
every day and publishes it to GitHub Pages, so it stays up to date on its own.

## How it works

| Piece | Source | Notes |
|---|---|---|
| Steam library + playtime | Steam Web API (official) | Needs your profile's game details set to Public |
| PlayStation library + playtime | PSNAWP (unofficial PSN API) | Only lists games you have launched at least once |
| Nintendo digital games | eShop purchase emails in your Gmail | Read daily, read-only; add-ons and memberships are skipped |
| Physical copies and anything else | "Add or hide a game" issue form, or `config/nintendo.yml` | Only you can use the form; anyone else's issues are ignored |
| Genres, release year, covers, IGDB critic and player ratings | IGDB (free API via Twitch) | Refreshed every run |
| Metacritic | Steam store data, falling back to the Metacritic game page | No official API |
| OpenCritic | OpenCritic API on RapidAPI | Optional; free tier is small, so lookups are capped per run |
| Backloggd | Public game page | No API; best effort. Stars x 20 = score out of 100 |
| Steam reviews | Steam store (public) | Percent positive, shown when a game has 10+ reviews |

Scores are cached in `cache/scores.json` and re-checked every 30 days; new games are scored on the
first run after they show up. IGDB ratings refresh every day.

## Setup (about 20 minutes, once)

### 1. Create the repository
1. On GitHub, create a new **public** repository (for example `game-shelf`).
2. Upload everything in this folder, keeping the folder structure (including `.github/workflows`).
3. Go to **Settings > Pages** and set **Source** to **GitHub Actions**.

### 2. Add your keys as secrets
Go to **Settings > Secrets and variables > Actions > New repository secret** and add:

| Secret | Where to get it |
|---|---|
| `STEAM_API_KEY` | https://steamcommunity.com/dev/apikey (any domain name works, e.g. `localhost`) |
| `STEAM_ID` | Your profile URL, vanity name, or 17-digit SteamID. Also set **Steam profile > Privacy > Game details** to **Public**. |
| `PSN_NPSSO` | Sign in at https://www.playstation.com, then open https://ca.account.sony.com/api/v1/ssocookie and copy the `npsso` value. It lasts about two months. |
| `IGDB_CLIENT_ID` and `IGDB_CLIENT_SECRET` | https://dev.twitch.tv/console > Register Your Application (OAuth redirect `http://localhost`, category "Application Integration", client type Confidential). Twitch requires two-factor auth on your account first. |
| `GMAIL_ADDRESS` | The Gmail address that gets your Nintendo receipts. |
| `GMAIL_APP_PASSWORD` | Create one at https://myaccount.google.com/apppasswords (needs 2-Step Verification on). Name it "Game Shelf". You can revoke it there anytime. |
| `OPENCRITIC_RAPIDAPI_KEY` (optional) | Subscribe to the free plan of the OpenCritic API on https://rapidapi.com and copy your key. |

Treat these like passwords: they only ever live in GitHub's encrypted secrets, never in the files.

### 3. Run it
1. Open the **Actions** tab and enable workflows if GitHub asks.
2. Pick **Update game shelf > Run workflow**.
3. When it finishes, your site is at `https://<your-username>.github.io/<repo-name>/`.

A big library takes a few daily runs to fully score: each run looks up at most 250 games
(change `MAX_SCORE_LOOKUPS` in the workflow if you want more).

## Day to day
- **Nothing to do.** It updates every morning around 5 AM Dallas time.
- **Nintendo eShop purchases:** nothing to do; the receipt email is picked up on the next daily run.
- **Physical games (or anything missing):** in the repo, open **Issues > New issue > Add or hide a game**, fill in the title and platform, and submit. It works from the GitHub phone app. A bot adds the game, closes the issue, and rebuilds the site in a few minutes.
- **Hide something** (a demo, a DLC that slipped through): same form, choose **Hide**.
- **Wrong match or missing score:** add a fix in `config/overrides.yml`. The run log in the Actions tab lists games it couldn't match.
- **PlayStation warning on the site:** your PSN token expired. Get a new `npsso` value (step 2) and update the `PSN_NPSSO` secret. The site keeps your last PlayStation list until then.
- GitHub emails you if a run fails.

## Limits worth knowing
- The site is public: anyone with the link can see your game list (never your keys).
- PlayStation games you own but never launched don't appear; PSN doesn't expose them.
- Metacritic and Backloggd are read from their public pages. If either site changes its layout, those scores stop updating (the rest keeps working) until the parser is adjusted in `pipeline/scores.py`.
- The PlayStation and OpenCritic access relies on unofficial or third-party APIs that can change.
- A Gmail app password can read your whole mailbox. The code only opens it read-only and only fetches Nintendo receipt emails, but if you want tighter isolation, set a Gmail filter that forwards Nintendo receipts to a separate Gmail account and use that account's app password instead.

## Run locally
```bash
pip install -r requirements.txt
export STEAM_API_KEY=... STEAM_ID=... IGDB_CLIENT_ID=... IGDB_CLIENT_SECRET=...
python -m pipeline.build
python -m http.server -d site 8000   # then open http://localhost:8000
```
