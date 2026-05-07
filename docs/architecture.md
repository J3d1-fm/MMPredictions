# Architecture

MMPredictions is built as a small data product with three layers.

## 1. Ingestion

The ingestion layer pulls MMP cohort reports into `cohort_rows`.

Default Adjust dimensions:

- app
- country / country_code
- partner_name
- campaign_network
- campaign_id_network

Default metrics include spend, installs, ROAS horizons, revenue horizons, and retained-user/cohort-size metrics for pRetention.

Sync modes:

- `daily`: last closed daily windows, usually 3-8 days.
- `weekly`: recent closed weekly windows and maturity checkpoints.
- `all`: bootstrap mode for historical backfill.

Keep scheduled syncs incremental. Full backfills should be explicit operator actions.

## 2. Modeling

The modeling layer computes:

- pROAS: predicted revenue divided by spend.
- pLTV: predicted revenue divided by network installs.
- pRetention: predicted retained-user share.
- Confidence and error bands from historical backtests.

The current production baseline is a multiplier model:

1. Take an early anchor metric, such as D1/D3/D7.
2. Find historical cohorts with the same or broader traffic slice.
3. Estimate horizon multipliers by hierarchy: campaign-country, campaign, channel-country, channel, country, platform, global.
4. Apply the multiplier to the selected cohort.
5. Use percentile bands and confidence scoring for uncertainty.

The shrinkage model is available for benchmark comparison but should be promoted only after backtesting.

## 3. Serving

The dashboard reads compact summary artifacts where possible:

- latest cohort summary
- channel overview
- campaign drilldown
- backtest artifacts
- persistent SQLite snapshot

Cloud Storage can act as a low-cost persistent store for Cloud Run deployments. SQLite remains a single-writer store; move to Cloud SQL or BigQuery when concurrent writes, BI access, or large multi-tenant workloads become important.

## Multi-Project Registry

The dashboard can serve multiple projects from one deployment. The registry lives in Cloud Storage at `projects/registry.json` when `MMPRED_GCS_BUCKET` is set, or at `MMPRED_PROJECTS_PATH` / the database directory for local deployments.

Each project has:

- independent MMP connector metadata;
- independent Google Ads connector metadata;
- independent SQLite path derived from the base DB path;
- independent Cloud Storage prefix derived from the base prefix.

Browser requests pass `project_id` to `/api/summary`, `/api/status`, `/api/backtest`, and sync endpoints. This keeps UI project switching cheap and prevents project data from mixing in compact artifacts.

## Data Safety Rules

- Never store API tokens in Git.
- Never pass sync/dashboard tokens in URLs.
- Keep raw snapshots or compressed JSONL where possible before modeling.
- Use additive migrations and tested primary-key rebuilds.
- Cumulative metrics should be monotonic across horizons.
- Dashboard requests should not trigger expensive full historical API pulls.
