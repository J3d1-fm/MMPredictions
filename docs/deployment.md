# Deployment

## Local

```bash
cp config/mmpredictions.example.json config/mmpredictions.json
export ADJUST_API_TOKEN="..."
export ADJUST_APP_TOKENS="..."
export MMPRED_SYNC_TOKEN="..."
MMPRED_SYNC_ON_STARTUP=0 PORT=8766 python3 -m mmpredictions.app
```

## Cloud Run

Recommended baseline:

- Cloud Run service for the dashboard.
- Cloud Scheduler for ingestion.
- Secret Manager for API tokens.
- Cloud Storage bucket for SQLite snapshots and compact artifacts.
- IAP for browser access.

Environment variables:

```bash
MMPRED_CONFIG=/app/config/mmpredictions.json
MMPRED_DB_PATH=/var/lib/mmpredictions/mmpredictions.sqlite3
MMPRED_SYNC_ON_STARTUP=0
MMPRED_GCS_BUCKET=your-bucket
MMPRED_GCS_PREFIX=mmpredictions
MMPRED_ADMIN_EMAILS=admin@example.com
MMPRED_IAP_RESOURCE=projects/PROJECT_NUMBER/iap_web/cloud_run-REGION/services/SERVICE_NAME
```

Use one writer instance for SQLite. If you need concurrent writes or direct analyst access, move the store to Cloud SQL Postgres or BigQuery partitioned tables.

## Scheduler

Use `/api/sync` with `X-Sync-Token`.

Examples:

```bash
POST /api/sync?mode=daily&days=3
POST /api/sync?mode=weekly&weeks=8
POST /api/sync?mode=all
```

Avoid full syncs from browser requests. The dashboard returns warming-up payloads while background syncs run.
