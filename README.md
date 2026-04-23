# Morel Monitor

An automated foraging-window detector for post-wildfire burn scars in Washington State. Pulls daily weather from Open-Meteo for each tracked burn, evaluates qualifying conditions against a tunable criteria file, identifies multi-day foraging windows, and publishes them to:

- **`index.html`** — a dashboard at `https://shroomer-hq.github.io/morel-monitor`
- **`calendar.ics`** — an iCal feed you can subscribe to in Google Calendar

Runs automatically every morning via GitHub Actions.

## Setup (one time)

### 1. Push these files to your repo
Drop everything into the `morel-monitor` repo on your `main` branch. See the parent conversation for walkthrough.

### 2. Enable GitHub Pages
- Repo **Settings** → **Pages**
- Source: **Deploy from a branch**
- Branch: **main** / folder: **/ (root)** → **Save**
- Wait ~1 min; your site will be live at `https://shroomer-hq.github.io/morel-monitor`

### 3. Enable GitHub Actions
- Repo **Settings** → **Actions** → **General**
- Under **Workflow permissions**, select **Read and write permissions** → **Save**
  (This lets the daily job commit updated `data.json` and `calendar.ics` back to the repo.)

### 4. Trigger the first run manually
- Repo **Actions** tab → **Morning Check** → **Run workflow**
- Once it finishes, `data.json` and `calendar.ics` will exist in the repo and the dashboard will have data.

### 5. Subscribe Google Calendar to the feed
- Copy this URL: `https://shroomer-hq.github.io/morel-monitor/calendar.ics`
- Google Calendar → left sidebar → **Other calendars** → **+** → **From URL**
- Paste the URL and **Add calendar**
- Google polls the feed every few hours — new windows show up automatically, and cancelled windows disappear.

## How it works

- **`burns.json`** — list of burn locations with coordinates, elevation, year.
- **`criteria.json`** — qualifying criteria for a foraging day (temperature band, trailing rainfall, no-freeze window) and window definition (3+ consecutive qualifying days).
- **`agent.py`** — fetches 14 days of trailing observations + 7 days of forecast from Open-Meteo for each burn, evaluates each day, finds consecutive runs.
- **`.github/workflows/morning-check.yml`** — runs `agent.py` at 7am Pacific daily (14:00 UTC).

## Tuning

Edit `criteria.json` to adjust what counts as a qualifying day. Changes take effect on the next run.

Edit `burns.json` to add/remove burn locations or refine coordinates. The `pioneer` entry has placeholder coordinates — verify and update before relying on it.

## Running locally

```bash
python3 agent.py
```

No dependencies beyond the Python standard library.

## Time zone note

The cron schedule is in UTC. `0 14 * * *` = 7am PDT (March–Nov) but 6am PST (Nov–March). To keep it at 7am year-round, edit the workflow to `0 15 * * *` when DST ends.
