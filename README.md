# MMPredictions

Open-source constructor for mobile marketing pROAS, pLTV, and pRetention dashboards.

MMPredictions lets app teams connect their own MMP cohort API credentials, optionally connect Google Ads reporting, and build a self-hosted prediction dashboard over their own traffic data. The default implementation is dependency-light Python 3.12 with SQLite, Cloud Storage snapshots, and a static HTML/CSS/JS dashboard.

## What It Does

- Syncs paid cohort data from an MMP cohort API.
- Stores normalized daily, weekly, and monthly cohorts in SQLite.
- Builds pROAS, pLTV, and pRetention forecasts for configurable horizons.
- Labels mature values as `actual` and forecasted values as `pred`.
- Supports campaign/source/country/platform filters, campaign exclusions, CSV/XLS exports, and model backtests.
- Lets admins manage multiple projects and connector metadata from the dashboard UI.
- Persists compact dashboard artifacts so the UI reads small payloads instead of recomputing full history.
- Can run locally, in Docker, or on Google Cloud Run with IAP.

## Current Model Engines

- `baseline_multiplier_v1`: historical cohort multiplier model.
- `shrinkage_multiplier_v1`: hierarchical shrinkage multiplier benchmark.

The baseline remains the default production engine. Use `/api/backtest` to compare predicted versus actual performance before switching model families.

## Quick Start

```bash
git clone https://github.com/J3d1-fm/MMPredictions.git
cd MMPredictions
cp config/mmpredictions.example.json config/mmpredictions.json
export ADJUST_API_TOKEN="your_adjust_api_token"
export ADJUST_APP_TOKENS="android_app_token,ios_app_token"
export MMPRED_SYNC_TOKEN="local-sync-secret"
MMPRED_SYNC_ON_STARTUP=0 PORT=8766 python3 -m mmpredictions.app
```

Open `http://localhost:8766`.

Trigger a small sync:

```bash
curl -X POST \
  -H "X-Sync-Token: local-sync-secret" \
  "http://localhost:8766/api/sync?mode=daily&days=3"
```

For first historical bootstrap, start small:

```bash
curl -X POST \
  -H "X-Sync-Token: local-sync-secret" \
  "http://localhost:8766/api/sync?mode=weekly&weeks=8"
```

## Configuration

Edit `config/mmpredictions.json` or use environment variables:

- `ADJUST_API_TOKEN`: Adjust/MMP cohort API token.
- `ADJUST_APP_TOKENS`: comma-separated MMP app tokens.
- `ADJUST_APP_TOKEN_LABELS_JSON`: optional app-token label map.
- `MMPRED_CONFIG`: path to JSON config.
- `MMPRED_DB_PATH`: SQLite path.
- `MMPRED_SYNC_TOKEN`: required for `/api/sync`.
- `MMPRED_SYNC_ON_STARTUP`: defaults to `0`.
- `MMPRED_GCS_BUCKET`: optional Cloud Storage bucket for persistent snapshots/artifacts.
- `MMPRED_GCS_PREFIX`: optional object prefix.
- `MMPRED_ADMIN_EMAILS`: bootstrap dashboard admins when using IAP.
- `MMPRED_IAP_RESOURCE`: IAP resource path for access-management UI.
- `MMPRED_PROJECTS_PATH`: optional local project registry path when Cloud Storage is not enabled.

## Multi-Project Connectors

Admins can open `Connectors` in the dashboard to add projects for different apps or publishers. Each project can define:

- MMP provider.
- MMP API token environment variable name.
- MMP app tokens and labels.
- Google Ads config path and customer ids.

The registry stores connector metadata only. Raw API secrets should stay in environment variables, Secret Manager, or another secret store. Each project gets its own SQLite database path and Cloud Storage artifact prefix, so selecting another project does not mix cohorts or predictions.

Admin users can trigger small MMP syncs from the project cards:

- `Sync daily`: recent closed daily cohorts.
- `Sync weekly`: recent weekly cohorts.

Google Ads connector metadata is stored now; production Google Ads ingestion should normalize Google campaign rows into a separate source-of-truth table before joining it into predictions.

## Google Ads

Google Ads should normally be a separate reporting source of truth for Google campaign spend/conversions. This repository includes the dashboard-side source-exclusion and architecture hooks, but Google Ads production connector setup depends on each user’s Google Ads developer token, OAuth client, manager account, and customer IDs.

See [docs/connectors.md](docs/connectors.md).

## Local Checks

```bash
python3 -m py_compile $(git ls-files '*.py')
python3 -m unittest discover mmpredictions
node --check mmpredictions/static/dashboard.js
```

## Docker

```bash
docker build -t mmpredictions:local .
docker run --rm -p 8080:8080 \
  -e ADJUST_API_TOKEN="your_adjust_api_token" \
  -e ADJUST_APP_TOKENS="android_app_token,ios_app_token" \
  -e MMPRED_SYNC_TOKEN="local-sync-secret" \
  mmpredictions:local
```

## License

MIT. See [LICENSE](LICENSE).
