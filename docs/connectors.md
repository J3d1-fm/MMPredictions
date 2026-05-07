# Connectors

## Adjust MMP

The built-in connector uses Adjust-style cohort requests. Configure:

- `ADJUST_API_TOKEN`
- `ADJUST_APP_TOKENS`
- `adjust.dimensions`
- `adjust.metrics`

The connector expects cohort metrics such as `roas_d7`, `roas_d30`, `roas_m6`, and retained-user/cohort-size metrics if pRetention is enabled.

## Other MMPs

AppsFlyer, Singular, Kochava, Branch, or custom attribution sources can be added by implementing the same normalized row contract used by `engine.upsert_rows`.

Required normalized fields:

- cohort_start
- cohort_end
- granularity
- app
- platform
- country
- country_code
- partner_name
- campaign_network
- campaign_id_network
- installs
- network_installs
- network_cost
- horizon ROAS/revenue/retention metrics where available

Recommended connector behavior:

- Fetch closed periods only.
- Make requests idempotent.
- Use overlapping recent windows for late-arriving data.
- Preserve raw API payloads in `raw_json`.
- Do not map missing spend/revenue to non-zero values.

## Google Ads

Google Ads is not an MMP cohort replacement. It is a campaign reporting source for spend, clicks, impressions, conversions, and conversion value.

To let users connect their own Google Ads account:

```bash
python3 -m pip install -r requirements-google-ads.txt
export GOOGLE_ADS_DEVELOPER_TOKEN="..."
python3 scripts/setup_google_ads_oauth.py \
  --oauth-client ~/google-ads-oauth-client.json \
  --login-customer-id 1234567890
```

This writes `~/.google-ads.yaml` with a refresh token for the authorized Google user. Store equivalent credentials in your own secret manager for hosted deployments.

Recommended Google Ads API fields:

- `segments.date`, `segments.week`, or `segments.month`
- `campaign.id`
- `campaign.name`
- `campaign.status`
- `campaign.advertising_channel_type`
- `campaign.app_campaign_setting.app_id`
- `campaign.app_campaign_setting.app_store`
- `metrics.cost_micros`
- `metrics.impressions`
- `metrics.clicks`
- `metrics.biddable_app_install_conversions`
- `metrics.biddable_app_post_install_conversions`
- `metrics.conversions`
- `metrics.conversions_value`
- `metrics.all_conversions`
- `metrics.all_conversions_value`

Use Google Ads as the source of truth for Google campaign spend when MMP attribution is incomplete, and keep MMP cohort revenue/retention as the non-Google attribution source unless your data contracts prove otherwise.

## Access Pattern

For hosted deployments:

- Users authorize Google access outside the dashboard through their own OAuth/IAM setup.
- API tokens and refresh tokens should live in a secret manager.
- The dashboard should receive only the normalized reporting data and never expose raw credentials to the browser.

## Dashboard Connector UI

Admins can use the dashboard `Connectors` section to create or update project connector metadata:

- `Connect MMP API` adds a project with MMP provider, token env var name, app tokens, and app labels.
- `Connect Google Ads` records Google Ads config path and customer ids for the selected project.
- `Sync daily` and `Sync weekly` trigger MMP ingestion for that project without exposing `MMPRED_SYNC_TOKEN` to the browser.

The UI intentionally does not store raw API tokens. Put secrets in environment variables or Secret Manager, then reference those env var names from the project settings.
