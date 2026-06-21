# Real estate monitor

A self-hosted, free real estate monitoring dashboard built entirely from primary
public data (Zillow Research, Redfin Data Center, FRED, Census). It downloads
public housing data on a monthly schedule, filters it to a small set of ZIP
codes and counties, computes market metrics, and renders a single static HTML
dashboard published to GitHub Pages.

It is an **independent tool built from public sources only**. It does not
connect to, scrape, or replicate any paid service.

---

## What it tracks

Markets and ZIP codes are defined in [`config.yaml`](config.yaml) — the only
file you edit to change coverage.

- **Toledo, OH** — Lucas County (FIPS 39095), West/North/South Toledo ZIPs
- **San Francisco, CA** — San Francisco County (FIPS 06075), residential ZIPs

---

## Honest data-coverage note (read this once)

Free public data is excellent at some geographies and thin at others. The
dashboard labels every metric with its true geography and source date. Summary:

| Metric | ZIP level? | Source / reality |
|---|---|---|
| Home value (ZHVI) | ✅ yes | Zillow, monthly, ~2000→ |
| Rent (ZORI) | ⚠️ partial | Zillow, ~2015→, only ZIPs above a listing-volume threshold; some smaller Toledo ZIPs have none |
| Price-to-rent | ⚠️ where both exist | derived; skipped where ZORI absent |
| Inventory / new listings | ❌ metro only | Zillow publishes at metro, not ZIP |
| Price cuts %, days on market, sale-to-list, above/below list | ❌ county/metro | Redfin reliable at county/metro, thin at ZIP |
| Net migration | ❌ county only | Census flows: county-level, annual, lagged |
| Median income / population | county solid | Census ACS; ZIP (ZCTA) possible but coarse/annual |
| Mortgage rate (30-yr) | national | FRED `MORTGAGE30US` |
| FHFA House Price Index | state/metro | FRED; ZIP only as annual developmental file |

**Forecast module:** ZHVI time-series forecasting is on firm ground at ZIP. The
driver model uses county/annual features (migration, income) broadcast to
ZIP-month — documented as a limitation, not hidden.

---

## Disclaimers (built into the dashboard, repeated here)

- The **overvaluation proxy** is a directional indicator built from public
  ratios (price-to-rent and price-to-income vs. a ZIP's own history). It is
  **not a predictive model** and not anyone's proprietary score.
- The **forecast panel** is a transparent public-data model that shows its own
  inputs and its own backtest error. It is **not a guarantee** and not any paid
  service's forecast score.

---

## Setup

### 1. Python

Code runs on Python 3.9+ locally; the scheduled GitHub Actions job uses 3.11.

```bash
cd real-estate-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Free API keys

Two free keys are needed (FRED and Census). Both are instant.

- FRED: https://fredaccount.stlouisfed.org/apikeys
- Census: https://api.census.gov/data/key_signup.html

Copy the template and paste your keys (the `.env` file is git-ignored):

```bash
cp .env.example .env
# then edit .env:
#   FRED_API_KEY=your_key_here
#   CENSUS_API_KEY=your_key_here
```

### 3. Verify the setup

```bash
python run.py check
```

This prints the tracked markets/ZIPs, whether each API key is detected, and the
cache/output paths. (Phase-1 acceptance test.)

---

## Running the pipeline

Three scripts run in order; each is independently runnable for debugging:

```bash
python fetch.py      # download + cache raw files to data/raw/ (date-stamped)
python compute.py    # filter to my markets, compute metrics -> data/processed/metrics.csv
python build.py      # render docs/index.html + chart assets + CSV export
```

Or all at once (what the scheduler runs):

```bash
python run.py
```

To force a fresh download even if today's cache exists:

```bash
REMON_NO_CACHE=1 python fetch.py
```

You can also fetch one source at a time, e.g. `python fetch.py --source redfin`
(choices: `zillow`, `redfin`, `fred`, `census`).

---

## How it stays robust

- Every download is wrapped in retry-with-backoff.
- Raw downloads are cached date-stamped in `data/raw/`; a fresh cache is reused,
  so a failed compute step never forces a re-download.
- If a source is unreachable, the last good cached copy is used and marked
  **stale** in the dashboard footer rather than failing the whole run.
- After loading, data is validated: row count > 0, expected columns present **by
  name** (never by position), dates parse, no all-null metric columns — and it
  **fails loudly** naming the source if a check fails.
- Every step logs to the console with timestamps.

---

## Automation (GitHub Actions + Pages)

The workflow [.github/workflows/monthly.yml](.github/workflows/monthly.yml) runs
on the **5th of each month** (and on a manual button), executes `run.py`, and
commits the refreshed `/docs` so GitHub Pages republishes. The two API keys come
from **repository secrets** — never committed.

### One-time setup (do this once)

1. **Create the repository on GitHub.**
   - Go to <https://github.com/new>.
   - Name it `real-estate-monitor`.
   - Set visibility to **Public** (required for free GitHub Pages). The repo
     contains only public market data — no personal info, and the API keys are
     stored as encrypted secrets, not in the code.
   - Do **not** add a README/.gitignore (this project already has them).

2. **Push this project to it.** From the project folder:
   ```bash
   git remote add origin https://github.com/Domrodd23/real-estate-monitor.git
   git branch -M main
   git push -u origin main
   ```

3. **Add the two API keys as secrets.**
   - Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
   - Add `FRED_API_KEY` (paste your FRED key).
   - Add `CENSUS_API_KEY` (paste your Census key).

4. **Enable GitHub Pages.**
   - Repo → **Settings** → **Pages**.
   - Under "Build and deployment", set **Source = Deploy from a branch**.
   - Branch = **main**, folder = **/docs**. Save.
   - After the next run, your dashboard is live at
     `https://domrodd23.github.io/real-estate-monitor/`.

5. **Do the first run now (don't wait for the 5th).**
   - Repo → **Actions** tab → **Monthly data refresh** → **Run workflow**.
   - When it finishes (a few minutes), the page publishes/updates.

### After setup

- It refreshes itself monthly. To refresh on demand, click **Run workflow** again.
- To change which ZIPs/markets are tracked, edit `config.yaml`, commit, and push —
  the next run picks up the change.

---

## Repository layout

```
real-estate-monitor/
├── config.yaml          # markets, ZIPs, sources, cache + output settings
├── requirements.txt     # dependency-light, grouped by phase
├── .env.example         # template for the two free API keys
├── run.py               # orchestrator: fetch -> compute -> build (+ `check`)
├── fetch.py             # downloads + caches raw data
├── compute.py           # computes metrics -> data/processed/metrics.csv
├── build.py             # renders docs/index.html + CSV export
├── src/remon/           # shared utilities
│   ├── config.py        # load + validate config.yaml and .env
│   ├── logging_setup.py # timestamped logging
│   ├── http.py          # cached, retry-with-backoff downloads
│   └── validate.py      # name-based column checks + dataframe validation
├── data/raw/            # date-stamped raw downloads (git-ignored)
├── data/processed/      # computed metrics.csv (git-ignored)
└── docs/                # generated dashboard (served by GitHub Pages)
```

---

## Build status

- [x] **Phase 1** — scaffold, config, env handling, README
- [x] **Phase 2** — Zillow fetch (ZHVI, ZORI, metro inventory/new-listings)
- [x] **Phase 3** — Redfin, FRED, Census fetch
- [x] **Phase 4** — compute metrics (data/processed/metrics.csv)
- [x] **Phase 5** — dashboard (docs/index.html + charts + CSV export)
- [ ] Phase 6 — GitHub Actions + Pages
- [ ] Phase 7 — expanded data + comparison table
- [ ] Phase 8 — cash-flow + reverse-offer calculators
- [ ] Phase 9 — PDF + Excel exports
- [ ] Phase 10 — forecast module
