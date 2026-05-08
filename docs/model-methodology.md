# Model Methodology and Technical Reference

This document describes the default MMPredictions data flow, storage model, prediction formulas, backtest methodology, and serving behavior. It is written for engineers and data practitioners who need to review, operate, or extend the system.

## System Overview

MMPredictions is a small self-hosted data product:

```text
Scheduler
  -> dashboard sync API
  -> MMP cohort API
  -> normalized SQLite cohort store
  -> persistent snapshot and compact artifacts
  -> browser dashboard
```

Main modules:

- `mmpredictions/engine.py`: ingestion, schema management, prediction models, backtests, artifact generation.
- `mmpredictions/app.py`: HTTP server, dashboard APIs, sync orchestration, access management.
- `mmpredictions/gcs_store.py`: Cloud Storage snapshot/artifact IO and lease locking.
- `mmpredictions/scheduler_proxy.py`: optional scheduler entrypoint for protected deployments.
- `mmpredictions/static/*`: dashboard UI.

## Normalized Cohort Row

Each stored row represents one traffic cohort at a chosen granularity:

```text
granularity, cohort_start, cohort_end, app, platform,
country, country_code, partner_name, campaign_network, campaign_id_network
```

Important metrics:

```text
network_cost
installs
network_installs
network_ecpi
roas_d0, roas_d1, roas_d3, roas_d7, roas_d30, roas_d60, roas_d90, roas_d120
roas_m3, roas_m4, roas_m6, roas_m12, roas_m18, roas_m24
retention_d1, retention_d3, retention_d7, retention_d14, retention_d30, retention_d60, retention_d90, retention_d120
retention_m6, retention_m12, retention_m18, retention_m24
event feature metrics from configured feature packs
raw_json
```

ROAS values are stored as ratios. For example, 35% is stored as `0.35`.

## Horizon Mapping

Default pROAS horizons map to these metrics:

```text
H7   -> roas_d7
H30  -> roas_d30
H60  -> roas_d60
H90  -> roas_d90
H120 -> roas_d120
H180 -> roas_m6
H360 -> roas_m12
H540 -> roas_m18
H720 -> roas_m24
```

Long horizons can use monthly MMP metrics as proxies. If `roas_d60` is not present, the engine can interpolate it:

```text
roas_d60_proxy = roas_d30 + (roas_d90 - roas_d30) * 0.5
```

Retention uses the same horizon pattern through `retention_d*` and `retention_m*` metrics.

## pROAS and pLTV Formulas

For a subject cohort:

```text
C = network_cost
N = network_installs
```

The model predicts ROAS first:

```text
predicted_revenue_h = predicted_roas_h * C
predicted_ltv_h     = predicted_revenue_h / N
```

If the horizon is mature and actual ROAS is present, the dashboard displays actual values:

```text
display_revenue_h = display_roas_h * C
display_ltv_h     = display_revenue_h / N
```

The default LTV denominator is `network_installs`, because it matches paid-network attribution and prevents organic installs from diluting paid traffic quality.

## Anchor Selection

For production pROAS predictions, the engine chooses the earliest configured anchor that is:

```text
anchor < target horizon
anchor <= cohort age in days
roas_d<anchor> is present
roas_d<anchor> >= minimum_anchor_roas
```

Default anchors:

```text
D0, D1, D3, D7
```

This favors early decisioning. Later anchors normally reduce uncertainty, but the product goal is to produce useful directional signals quickly.

Feature-model backtests may choose a later decision anchor when feature vectors are available. That path is benchmark-only unless explicitly promoted.

## Baseline Multiplier Model

`baseline_multiplier_v1` is the default production model.

For subject cohort `s`, horizon `h`, and selected anchor `a`:

```text
R_s,a = subject anchor ROAS
R_i,a = candidate historical anchor ROAS
R_i,h = candidate historical target ROAS
```

Candidate historical multiplier:

```text
M_i,h,a = R_i,h / R_i,a
```

The engine searches candidate cohorts in this fallback order:

```text
campaign+country -> campaign -> channel+country -> channel -> country -> platform -> global
```

A candidate must:

- match the current group predicate;
- not be the same cohort;
- be mature for horizon `h`;
- have the selected anchor;
- have the target metric;
- pass the minimum anchor threshold.

Invalid or extreme multipliers are removed:

```text
0 < M_i,h,a < 100
```

The first group with at least `minimum_training_samples` candidates is selected. Prediction and interval:

```text
M_med  = median(M_i,h,a)
M_low  = percentile_20(M_i,h,a)
M_high = percentile_80(M_i,h,a)

predicted_roas_h = R_s,a * M_med
low_roas_h       = R_s,a * min(M_low, M_med)
high_roas_h      = R_s,a * max(M_high, M_med)
```

Group error:

```text
candidate_pred_i = R_i,a * M_med
APE_i            = abs(candidate_pred_i - R_i,h) / R_i,h
group_mape       = median(APE_i)
```

After predictions are built, the serving layer enforces cumulative ROAS monotonicity for each cohort/campaign key.

## Shrinkage Multiplier Benchmark

`shrinkage_multiplier_v1` is a benchmark model. It blends narrow group behavior with broader priors in log space.

The hierarchy is evaluated broad-to-narrow:

```text
global -> platform -> country -> channel -> channel+country -> campaign -> campaign+country
```

For each group `g`:

```text
n_g      = sample size
M_g      = raw median multiplier
k_g      = configured prior strength
P_parent = previous posterior log multiplier

w_g = n_g / (n_g + k_g)
P_g = w_g * log(M_g) + (1 - w_g) * P_parent
```

Final multiplier:

```text
M_shrink = exp(P_leaf)
```

The low/high interval is blended in the same log-space way. Sparse campaign samples are pulled toward broader history instead of dominating the prediction.

Default prior strengths:

```text
global=0
platform=40
country=32
channel=24
channel_country=18
campaign=12
campaign_country=8
```

## Feature Multiplier Benchmark

`feature_multiplier_v1` tests whether early behavior signals improve forecasts.

Typical feature pack metrics:

- revenue event counts;
- revenue events per user;
- revenue events per active user;
- first paying users;
- payer conversion rate;
- paying user size;
- revenue per paying user;
- sessions;
- sessions per user;
- time spent;
- ad impressions;
- ad revenue;
- ad RPM.

Feature values are normalized:

```text
install_rate feature = raw_metric / network_installs
cost_rate feature    = raw_metric / network_cost
raw feature          = raw_metric
transformed feature  = log1p(max(value, 0))
```

The model compares the subject vector with candidate vectors using only features available by the decision anchor:

```text
distance_i = mean(abs(feature_s,j - feature_i,j)) over common features
weight_i   = exp(-distance_i / temperature)
```

The feature-neighbor multiplier is a weighted median:

```text
M_feature = weighted_percentile(M_i,h,a, weight_i, 0.5)
```

It is blended with a base model, usually shrinkage:

```text
sample_factor = min(1, feature_sample_size / (min_feature_samples * 4))
blend         = min(blend_strength * sample_factor, 0.85)

M_final = exp((1 - blend) * log(M_base) + blend * log(M_feature))
```

Feature-model rows expose sample size, feature count, feature ratio, and blend weight for debugging.

## Retention Multiplier Model

`retention_multiplier_v1` predicts retained-user share separately from pROAS/pLTV.

Retention metrics are rates:

```text
retention_h = retained_users_h / cohort_size_h
```

For anchor `a` and horizon `h`:

```text
Q_s,a = subject anchor retention
Q_i,a = candidate anchor retention
Q_i,h = candidate target retention

M_ret_i = Q_i,h / Q_i,a
predicted_retention_h = clamp(Q_s,a * median(M_ret_i), 0, 1)
```

Retention uses the same fallback hierarchy as pROAS. It is exposed as its own curve and backtest family. It can later become an input feature for pLTV models.

## Confidence Score

Confidence is a 0-1 composite score displayed as `conf 0-100`.

Component scores:

```text
install_score  = min(1, network_installs / (minimum_network_installs * 5))
revenue_score  = min(1, predicted_revenue / (minimum_predicted_revenue * 5))
sample_score   = min(1, sample_size / (minimum_training_samples * 4))
error_score    = missing_error_score if group_mape is null else clamp(1 - group_mape, 0, 1)
interval_score = clamp(1 - ((high_roas - low_roas) / predicted_roas), 0, 1)
```

Default weighted score:

```text
confidence =
  0.22 * install_score
+ 0.18 * revenue_score
+ 0.24 * sample_score
+ 0.22 * error_score
+ 0.14 * interval_score
```

Default levels:

```text
high   >= 0.72
medium >= 0.48
low    < 0.48
```

Confidence is a decision-quality score, not a guarantee.

## Backtest Methodology

Backtest avoids future leakage.

For each mature historical subject cohort:

```text
decision_date = subject.cohort_end + anchor_day
eligible candidate only if candidate.cohort_end + horizon <= decision_date
```

The model is therefore evaluated using only information that would have existed at that historical decision point.

Summary metrics:

```text
actual_revenue_h    = sum(actual_roas_h * cost)
predicted_revenue_h = sum(predicted_roas_h * cost)
weighted_mape       = sum(abs(predicted_roas_h - actual_roas_h) * cost) / actual_revenue_h
median_ape          = median(abs(predicted_roas_h - actual_roas_h) / actual_roas_h)
bias                = (predicted_revenue_h - actual_revenue_h) / total_cost
coverage            = share of rows where actual_roas_h is inside [low_roas_h, high_roas_h]
```

Segment summaries group rows by source, country, platform, spend bucket, and cohort-size bucket. Use segment stability before promoting a model.

## Sync Lifecycle

Daily sync refreshes recent closed daily windows:

```text
mode=daily -> last daily_refresh_days complete days
```

Weekly sync is incremental:

```text
mode=weekly without weeks -> latest weekly_refresh_weeks
                           + maturity checkpoint windows
```

Full historical sync is explicit:

```text
mode=weekly&weeks=52&force=1
```

Use full sync only when the historical feature table must be rebuilt.

## Replace-Partition Ingestion

Each MMP response is treated as complete for its cohort window. After successful fetch and parse:

```text
BEGIN IMMEDIATE
delete from cohort_rows where granularity=? and cohort_start=?
insert fetched rows
upsert sync_window_manifests
COMMIT
```

If the MMP request fails, the old partition remains. This prevents stale rows after source recalculation while avoiding data loss on failed syncs.

`sync_window_manifests` records source row count, stored row count, checksum, request metadata, and fetched timestamp.

## Persistent Store and Artifacts

For low-cost Cloud Run deployments, SQLite is a working cache and Cloud Storage is the persistent store.

Startup:

```text
if local SQLite is missing:
  restore snapshots/latest.sqlite3.gz
```

Successful sync:

```text
upload compressed SQLite snapshot
upload snapshot manifest
build versioned summary/backtest artifacts
move latest manifest pointer after batch success
```

Artifacts are immutable run batches:

```text
artifacts/runs/<run_id>/summary_week_android.json.gz
artifacts/runs/<run_id>/summary_week_ios.json.gz
artifacts/runs/<run_id>/summary_day_android.json.gz
artifacts/runs/<run_id>/summary_day_ios.json.gz
artifacts/runs/<run_id>/summary_month_android.json.gz
artifacts/runs/<run_id>/summary_month_ios.json.gz
artifacts/runs/<run_id>/model_stats.json.gz
artifacts/runs/<run_id>/backtest_baseline.json.gz
artifacts/runs/<run_id>/backtest_compact.json.gz
artifacts/runs/<run_id>/manifest.json.gz
```

Serving pointers:

```text
artifacts/latest/manifest.json.gz
artifacts/latest/daily_manifest.json.gz
```

The UI reads only manifest-approved artifact paths. If a sync fails mid-batch, the pointer is not updated.

## Model Signature

Every artifact includes `model_signature`, a SHA-256 hash over prediction-relevant configuration:

- prediction horizons and metric mapping;
- retention settings;
- confidence weights and thresholds;
- feature packs and feature-model settings;
- shrinkage prior strengths;
- backtest models;
- sync thresholds;
- excluded sources.

Stale signature artifacts are rejected instead of silently serving old model output.

## Storage Scaling Path

SQLite plus Cloud Storage is intended for low-cost single-writer deployments. Move to Cloud SQL Postgres or BigQuery partitioned tables when you need:

- concurrent writers;
- external BI access;
- multi-tenant workload isolation;
- direct SQL analytics over raw history;
- larger historical data windows;
- strict warehouse-level governance.

## Security Guidelines

- Do not store API tokens in Git.
- Do not pass dashboard or sync tokens in URLs.
- Store raw API credentials in environment variables or a secret manager.
- Store connector metadata separately from secrets.
- Protect browser access with IAP or equivalent SSO.
- Protect sync endpoints with machine tokens and service identity.
- Avoid triggering full historical syncs from browser page loads.
