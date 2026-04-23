# WA Foraging Monitor

Two automated trackers for Washington State foraging opportunities, running in one repo:

1. **Morel monitor** — watches post-wildfire burn scars for weather conditions conducive to morel fruiting, publishes multi-day foraging *windows* to `calendar.ics`.
2. **Razor clam monitor** — scrapes WDFW-announced dig dates, scores each dig by tide depth and daylight alignment, publishes events to `razor-clams.ics`.

Both run automatically every morning via GitHub Actions and publish to:

- **`index.html`** — a dashboard at `https://shroomer-hq.github.io/morel-monitor` (morels only for now — razor clams are calendar-only)
- **`calendar.ics`** — morel foraging windows
- **`razor-clams.ics`** — razor clam dig events

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

### 5. Subscribe Google Calendar to the feed(s)
- **Morel windows:** `https://shroomer-hq.github.io/morel-monitor/calendar.ics`
- **Razor clam digs:** `https://shroomer-hq.github.io/morel-monitor/razor-clams.ics`
- Google Calendar → left sidebar → **Other calendars** → **+** → **From URL**
- Paste each URL and **Add calendar** (subscribe to both, or just the one you want)
- Google polls the feed every few hours — new windows/digs show up automatically, and cancelled ones disappear.

## How it works

### Morel monitor
- **`burns.json`** — list of burn locations with coordinates, elevation, year.
- **`criteria.json`** — qualifying criteria for a foraging day (temperature band, trailing rainfall, no-freeze window) and window definition (3+ consecutive qualifying days).
- **`agent.py`** — fetches 14 days of trailing observations + 7 days of forecast from Open-Meteo for each burn, evaluates each day, finds consecutive runs.

### Razor clam monitor
- **`razor_clams.py`** — scrapes WDFW's razor clam seasons page, parses dig date entries, scores each by tide depth and daylight alignment, generates calendar events 2h before to 1h after each low tide.
- **Priority classification:**
  - 🦪 **priority** — tide ≤ -0.5 ft AND dig time in daylight
  - 🌊 **standard** — tide ≤ +0.5 ft AND dig time in daylight
  - 🌙 **marginal (dark)** — tide ≤ +0.5 ft but dig is in the dark
  - **·  low** — shallower tide, unlikely to be productive

### Scheduling
- **`.github/workflows/morning-check.yml`** — runs both scripts at 7am Pacific daily (14:00 UTC).

## Tuning

**Morels:** Edit `criteria.json` to adjust what counts as a qualifying day. Changes take effect on the next run.

Edit `burns.json` to add/remove burn locations or refine coordinates. The `pioneer` entry has placeholder coordinates — verify and update before relying on it.

**Razor clams:** The scoring thresholds live at the top of `razor_clams.py` in the `score_dig` function. Adjust `tide` cutoffs or daylight logic there if you want to re-classify what counts as priority vs standard.

## Running locally

```bash
python3 agent.py          # Morel monitor
python3 razor_clams.py    # Razor clam monitor
```

No dependencies beyond the Python standard library.

## Time zone note

The cron schedule is in UTC. `0 14 * * *` = 7am PDT (March–Nov) but 6am PST (Nov–March). To keep it at 7am year-round, edit the workflow to `0 15 * * *` when DST ends.
