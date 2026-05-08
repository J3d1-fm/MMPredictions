# MMPredictions User Guide

MMPredictions helps marketing and analytics teams read cohort performance earlier than the final LTV horizon is available. It turns MMP cohort data into pROAS, pLTV, pRetention, confidence scores, backtests, and campaign drilldowns.

This document is written for marketers, UA managers, analysts, and product operators.

## What The Dashboard Predicts

The dashboard works with cohorts. A cohort is a group of users acquired from a traffic slice during a selected period.

Example traffic slice:

- platform: Android or iOS;
- country: United States;
- source: a network such as Meta, TikTok, Mintegral, AppLovin, Moloco, Unity, Apple Search Ads, or another MMP partner;
- campaign: one paid campaign;
- cohort window: for example `2026-03-01 - 2026-03-07`.

For each cohort, the dashboard reads early observed signals such as D0, D1, D3, or D7 ROAS and predicts cumulative performance at later horizons:

- H7;
- H30;
- H60;
- H90;
- H120;
- H180;
- H360;
- H540;
- H720.

ROAS is cumulative. For the same cohort, later horizons should not display below earlier horizons. The serving layer enforces monotonic prediction curves to avoid impossible decreasing cumulative ROAS.

## Core Metrics

### pROAS

```text
pROAS = predicted revenue / spend
```

If a campaign spent `US$10,000` and the model predicts `80% pROAS`, expected revenue for that horizon is:

```text
US$10,000 * 80% = US$8,000
```

### pLTV

```text
pLTV = predicted revenue / network installs
```

If the same campaign generated `20,000 network installs`, expected pLTV is:

```text
US$8,000 / 20,000 = US$0.40
```

### pRetention

```text
pRetention = predicted retained users / cohort users
```

pRetention is useful when retention carries information that early revenue alone does not capture.

## actual, pred, and proxy

Dashboard horizon cells can have three statuses:

- `actual`: the horizon is mature and the value is observed from the connected data source.
- `pred`: the value is predicted by the model.
- `proxy`: the value uses a related metric when the exact horizon metric is unavailable, for example a monthly cohort metric for a long day-based horizon.

For decisions, treat `actual` as observed data and treat `pred`/`proxy` as model output that must be read together with confidence and interval width.

## Cohort Scope

The cohort scope controls speed versus stability:

- `1-day cohorts`: fastest signal, usually noisiest.
- `7-day cohorts`: default operational view for weekly marketing decisions.
- `30-day cohorts`: more stable, usually better for strategic budget decisions.
- `Custom dates`: manual cohort window for ad hoc analysis.

Larger cohorts usually produce more stable predictions because they contain more spend, installs, and revenue events. Smaller cohorts can be useful for speed, but confidence matters more.

## Filters

Use filters to narrow the traffic slice:

- platform;
- country;
- source;
- campaign;
- custom date range;
- campaign exclusions.

Campaign exclusions are useful for traffic that should not be mixed into normal UA predictions, such as retargeting, reactivation, brand tests, or campaigns with a materially different optimization logic.

## Channel Overview

Channel Overview is the fastest way to compare sources.

Each source card shows:

- cohort window;
- spend;
- confidence score;
- pROAS by horizon;
- pLTV by horizon;
- actual/pred/proxy status.

Use it to answer:

- Which source is currently strongest?
- Which source is promising but uncertain?
- Which source needs campaign-level inspection?
- Which source has no spend or missing data for the selected window?

## Campaign Drilldown

Campaign Drilldown breaks source performance into campaign rows. Horizons are shown as columns such as H7, H30, H60, H90, H120, H180, H360, H540, and H720.

Use it to:

- sort by spend, pROAS, pLTV, or decision quality;
- inspect campaigns behind a source card;
- resize columns for long campaign names;
- export campaign-level data;
- identify outliers and low-confidence rows.

## Confidence Score

Confidence is a model-readiness score from 0 to 100. It is not a probability that the prediction is correct.

Confidence considers:

- network installs;
- predicted revenue;
- number of historical training samples;
- historical model error;
- interval width.

Practical interpretation:

- `70+`: strong enough for normal operational decisions when business context agrees.
- `50-70`: usable, but prefer gradual decisions and inspect the underlying campaigns.
- `<50`: avoid hard budget decisions based only on the prediction.

A high pROAS with low confidence can be a noisy signal. A lower pROAS with high confidence can be more actionable.

## Prediction Intervals

Intervals show the likely range around the forecast. Wide intervals mean similar historical cohorts behaved differently, or the current cohort has weak support.

When comparing two campaigns:

- prefer high pROAS with narrow intervals;
- be careful when intervals overlap heavily;
- do not overreact to low-spend rows with very wide intervals.

## Backtest

Backtest is historical validation.

The system:

1. Takes an old cohort whose final result is already known.
2. Pretends it is looking at that cohort in the past with only early data available.
3. Builds a prediction using only historical cohorts that would have been mature at that past decision date.
4. Compares predicted versus actual.

Backtest helps decide whether a model should be trusted or promoted.

Important columns:

- `Actual ROAS`: mature observed result.
- `Pred ROAS`: what the model would have predicted.
- `Weighted MAPE`: spend-weighted relative error.
- `Median APE`: median row-level percentage error.
- `Coverage`: share of actual values that landed inside the prediction interval.
- `Delta vs baseline`: whether a benchmark model improved over the baseline.

## Model Families In The UI

### Baseline multiplier

The default production model. It learns how much historical cohorts grew from an early anchor day to a later horizon.

Example:

```text
Historical D7 ROAS = 20%
Historical D30 ROAS = 50%
Multiplier = 50% / 20% = 2.5x

New campaign D7 ROAS = 18%
Predicted H30 = 18% * 2.5 = 45%
```

### Shrinkage multiplier

A more conservative benchmark. If campaign-level history is sparse, it pulls the estimate toward broader groups such as source, country, platform, or global history.

### Feature multiplier

A benchmark model that also compares early behavior signals such as revenue events, payer conversion, sessions, ad impressions, ad revenue, and ad RPM. It is useful when early behavior explains future LTV beyond early ROAS alone.

Feature models should be promoted only after backtests show stable improvement by horizon and segment.

## Export

Use `Export CSV` or `Export XLS` to export the active tab.

Typical use cases:

- share a weekly source overview;
- audit campaign predictions in a spreadsheet;
- compare backtest model quality outside the dashboard;
- attach data to a marketing decision document.

## Recommended Workflow

1. Start with 7-day cohorts and the main platform.
2. Check Channel Overview for source-level direction.
3. Use confidence to separate actionable signals from noisy signals.
4. Drill into Campaigns for sources that need explanation.
5. Exclude campaigns with different logic, such as retargeting.
6. Check Backtest before trusting a new model family.
7. Export the relevant tab when sharing decisions.

## Common Questions

### Why does a source disappear for a selected period?

It may have no spend in that period, be excluded by filters, or be excluded by source/campaign rules.

### Does Refresh pull all MMP data again?

No. Refresh reloads prepared dashboard payloads. MMP ingestion runs through scheduled syncs or explicit admin sync actions.

### Why can H180/H360/H540 be proxy values?

Some MMPs expose long horizons as monthly cohort metrics rather than exact day metrics. The dashboard labels these as proxy horizons.

### Why can early event features improve forecasts?

Two campaigns can have the same early ROAS but different payer counts, session depth, ad monetization, or retention. Those differences can correlate with future LTV and help a feature-aware model.

### Should the best backtest model be enabled immediately?

No. First validate by horizon and by business segment. A model can improve total WMAPE while still performing poorly for a specific source, country, platform, or low-volume cohort size.
