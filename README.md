# CSFloat Price Dashboard

A small local dashboard for watching CS2 skin prices on [CSFloat](https://csfloat.com), with a cloud-backed price history so trends keep building even when your machine is off.

Tracks: 20 vanilla knives, 2 skinned Butterfly Knives, and 5 gloves (all Field-Tested) — see [`TRACKED_ITEMS`](csfloat_dashboard.py) to add or remove items.

## What it does

- Serves a dashboard at `http://localhost:8000` with current cheapest price + a trend chart per item, windowed to the last 7/14/30 days
- A **Refresh prices** button pulls live prices from CSFloat on demand
- Flags each item BUY-ish / WAIT / NEUTRAL based on where its price sits within its own logged range
- Excludes CSFloat auction listings from price calculations (an auction's `price` field is just the current bid/reserve, not a real sale price, and would otherwise skew the lows down)

## How the data flow works

CSFloat's API has no historical-price endpoint, so the only way to get a real trend is to keep sampling current listings over time — and a personal machine isn't on 24/7. Collection happens in two places that feed the same chart:

1. **In the cloud** (does the heavy lifting): [`.github/workflows/snapshot.yml`](.github/workflows/snapshot.yml) runs `python csfloat_dashboard.py snapshot` every 15 minutes on GitHub's own servers, independent of whether this machine is on. Each run appends rows to `price_history.csv` and commits them — this file is git-tracked, so it's the shared, durable history.
2. **Locally** (supplemental): clicking **Refresh prices** in the browser fetches live prices right now and appends them to `price_history.local.csv` instead. That file is gitignored on purpose, so a local write can never conflict with `git pull`.

The dashboard merges both files and runs `git pull` on every load, refresh, and a background timer, so it always reflects whatever the cloud job collected while you were away.

## Setup

**Run the dashboard locally:**
```bash
export CSFLOAT_API_KEY="your-key"
python3 csfloat_dashboard.py
```

**Set up cloud collection** (recommended — this is what makes history continuous instead of full of gaps):
1. Push this repo to GitHub. Make it public if you also want to read `price_history.csv` from an external tool like Google Sheets via `IMPORTDATA`; private is fine if you only care about the local dashboard.
2. In the repo's Settings → Secrets and variables → Actions, add a secret named `CSFLOAT_API_KEY` with your key.
3. The included workflow runs on its own schedule once pushed — nothing to run locally for this part. You can also trigger it manually from the Actions tab.
4. On any machine where you want the dashboard, `git clone` the repo and run it as above — it pulls the cloud-collected history automatically.

If you'd rather stay fully local/manual, that still works — just skip the GitHub Actions setup. Data only accumulates from whenever you had the dashboard open and clicked Refresh, so history will have gaps.

## Exporting to Google Sheets

Since `price_history.csv` lives in a public repo, you can read it straight into a spreadsheet with no extra setup:

```
=IMPORTDATA("https://raw.githubusercontent.com/<you>/<repo>/main/price_history.csv")
```

Sheets refreshes `IMPORTDATA` automatically roughly every hour, plus on open.

## Note

Market data for your own decisions, not financial advice. The BUY-ish/WAIT signal only compares an item to its own recently logged range — not a valuation.
