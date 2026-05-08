#!/usr/bin/env python3
"""Adjust cohort ingestion and pROAS prediction engine for MMPredictions."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import shlex
import sqlite3
import ssl
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

from mmpredictions import gcs_store


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/mmpredictions.json"
DEFAULT_SECRET_ENV = pathlib.Path.home() / ".codex/secrets/mmpredictions.env"
MIGRATION_LOCK = threading.Lock()
MIGRATED_DATABASES: set[str] = set()
_LOCAL = threading.local()

EARLY_FEATURE_METRICS = (
    "revenue_events_total_d1",
    "revenue_events_total_d3",
    "revenue_events_total_d7",
    "revenue_events_total_d30",
    "revenue_events_per_user_d7",
    "revenue_events_per_active_user_d7",
    "first_paying_users_total_d7",
    "cumulative_paying_users_conversion_rate_d7",
    "paying_user_size_d7",
    "revenue_total_per_paying_user_d7",
    "sessions_d1",
    "sessions_d3",
    "sessions_d7",
    "sessions_per_user_d7",
    "time_spent_per_user_d7",
    "ad_impressions_total_d7",
    "ad_revenue_total_d7",
    "ad_rpm_d7",
)
FEATURE_COLUMNS = tuple((metric, "real") for metric in EARLY_FEATURE_METRICS) + (
    ("event_features_json", "text not null default '{}'"),
)

COHORT_COLUMNS = (
    ("cohort_start", "text not null"),
    ("cohort_end", "text not null"),
    ("granularity", "text not null"),
    ("app", "text not null"),
    ("platform", "text not null"),
    ("country", "text not null default 'All countries'"),
    ("country_code", "text not null default 'ZZ'"),
    ("partner_name", "text not null"),
    ("campaign_network", "text not null"),
    ("campaign_id_network", "text not null"),
    ("installs", "real not null default 0"),
    ("network_installs", "real not null default 0"),
    ("network_cost", "real not null default 0"),
    ("network_ecpi", "real not null default 0"),
    ("roas_d0", "real"),
    ("roas_d1", "real"),
    ("roas_d3", "real"),
    ("roas_d7", "real"),
    ("roas_d30", "real"),
    ("roas_d60", "real"),
    ("roas_d90", "real"),
    ("roas_d120", "real"),
    ("roas_m3", "real"),
    ("roas_m4", "real"),
    ("roas_m6", "real"),
    ("roas_m12", "real"),
    ("roas_m18", "real"),
    ("roas_m24", "real"),
    ("revenue_d7", "real"),
    ("revenue_d30", "real"),
    ("retention_d1", "real"),
    ("retention_d3", "real"),
    ("retention_d7", "real"),
    ("retention_d14", "real"),
    ("retention_d30", "real"),
    ("retention_d60", "real"),
    ("retention_d90", "real"),
    ("retention_d120", "real"),
    ("retention_m6", "real"),
    ("retention_m12", "real"),
    ("retention_m18", "real"),
    ("retention_m24", "real"),
    *FEATURE_COLUMNS,
    ("raw_json", "text not null"),
    ("fetched_at", "text not null"),
)
COHORT_PK = (
    "cohort_start",
    "granularity",
    "app",
    "country_code",
    "partner_name",
    "campaign_network",
    "campaign_id_network",
)
CONFIDENCE_DEFAULTS = {
    "weights": {
        "network_installs": 0.22,
        "predicted_revenue": 0.18,
        "training_samples": 0.24,
        "historical_error": 0.22,
        "interval_width": 0.14,
    },
    "thresholds": {"high": 0.72, "medium": 0.48},
    "missing_error_score": 0.35,
}
RETENTION_DAYS = (1, 3, 7, 14, 30, 60, 90, 120)
RETENTION_MONTHS = (6, 12, 18, 24)
RETENTION_DEFAULTS = {
    "enabled": True,
    "anchors": [1, 3, 7],
    "horizons": [7, 30, 60, 90, 120, 180, 360, 540, 720],
    "horizon_metric_map": {
        "7": "retention_d7",
        "30": "retention_d30",
        "60": "retention_d60",
        "90": "retention_d90",
        "120": "retention_d120",
        "180": "retention_m6",
        "360": "retention_m12",
        "540": "retention_m18",
        "720": "retention_m24",
    },
    "metric_labels": {
        "retention_m6": "M6 proxy for D180",
        "retention_m12": "M12 proxy for D360",
        "retention_m18": "M18 proxy for D540",
        "retention_m24": "M24 proxy for D720",
    },
}


def load_local_env(path: pathlib.Path = DEFAULT_SECRET_ENV) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                try:
                    os.environ[key] = shlex.split(value, posix=True)[0]
                except (IndexError, ValueError):
                    os.environ[key] = value.strip("\"'")


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    config_path = pathlib.Path(path or os.environ.get("MMPRED_CONFIG", DEFAULT_CONFIG))
    if not config_path.exists() and config_path.name == "mmpredictions.json":
        fallback = config_path.with_name("mmpredictions.example.json")
        if fallback.exists():
            print(f"MMPRED_CONFIG missing, using example fallback: {fallback}")
            config_path = fallback
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    apply_env_overrides(config)
    normalize_config(config)
    validate_config(config)
    return config


def ordered_unique_ints(values: list[Any]) -> list[int]:
    return sorted({int(value) for value in values})


def insert_after(values: list[Any], item: int, after: int) -> list[int]:
    ordered = [int(value) for value in values]
    if item in ordered:
        return ordered
    if after in ordered:
        index = ordered.index(after) + 1
        return ordered[:index] + [item] + ordered[index:]
    return ordered_unique_ints([*ordered, item])


def normalize_config(config: dict[str, Any]) -> None:
    prediction = config.setdefault("prediction", {})
    prediction["horizons"] = insert_after(prediction.get("horizons", [7, 30, 90, 120, 180, 360, 540, 720]), 60, 30)
    prediction.setdefault("horizon_metric_map", {}).setdefault("60", "roas_d60")
    retention = config.setdefault("retention", {})
    for key, value in RETENTION_DEFAULTS.items():
        if isinstance(value, dict):
            retention.setdefault(key, {}).update({k: v for k, v in value.items() if k not in retention.get(key, {})})
        else:
            retention.setdefault(key, value)
    adjust = config.setdefault("adjust", {})
    metrics = [str(metric) for metric in adjust.get("metrics", [])]
    if metrics and "roas_d60" not in metrics:
        if "roas_d30" in metrics:
            index = metrics.index("roas_d30") + 1
            metrics = metrics[:index] + ["roas_d60"] + metrics[index:]
        else:
            metrics.append("roas_d60")
        adjust["metrics"] = metrics
    retention_metrics = []
    for day in RETENTION_DAYS:
        retention_metrics.extend([f"cohort_size_d{day}", f"retained_users_d{day}"])
    for month in RETENTION_MONTHS:
        retention_metrics.extend([f"cohort_size_m{month}", f"retained_users_m{month}"])
    if metrics:
        for metric in retention_metrics:
            if metric not in metrics:
                metrics.append(metric)
        feature_cfg = config.setdefault("feature_metric_packs", {})
        feature_cfg.setdefault("enabled", True)
        feature_cfg.setdefault("active", ["early_features_v1"])
        packs = feature_cfg.setdefault("packs", {})
        packs.setdefault("early_features_v1", list(EARLY_FEATURE_METRICS))
        packs.setdefault("custom_events_v1", [])
        if feature_cfg.get("enabled", True):
            for pack_name in feature_cfg.get("active", []):
                for metric in packs.get(str(pack_name), []):
                    if metric not in metrics:
                        metrics.append(str(metric))
        adjust["metrics"] = metrics
    sync = config.setdefault("sync", {})
    sync.setdefault("excluded_sources", [])
    if "maturity_refresh_days" in sync:
        sync["maturity_refresh_days"] = ordered_unique_ints([*sync.get("maturity_refresh_days", []), 60])


def apply_env_overrides(config: dict[str, Any]) -> None:
    adjust = config.setdefault("adjust", {})
    app_tokens = os.environ.get("ADJUST_APP_TOKENS")
    if app_tokens:
        adjust["app_tokens"] = [token.strip() for token in app_tokens.split(",") if token.strip()]
    labels = os.environ.get("ADJUST_APP_TOKEN_LABELS_JSON")
    if labels:
        adjust["app_token_labels"] = json.loads(labels)


def confidence_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(CONFIDENCE_DEFAULTS))
    configured = config.get("confidence", {})
    merged["weights"].update(configured.get("weights", {}))
    merged["thresholds"].update(configured.get("thresholds", {}))
    if "missing_error_score" in configured:
        merged["missing_error_score"] = configured["missing_error_score"]
    return merged


def validate_config(config: dict[str, Any]) -> None:
    weights = confidence_config(config)["weights"]
    total = sum(float(value) for value in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"confidence weights must sum to 1.0, got {total:.8f}")


def database_path(config: dict[str, Any]) -> pathlib.Path:
    if config.get("_database_path_override"):
        return pathlib.Path(str(config["_database_path_override"]))
    return pathlib.Path(os.environ.get("MMPRED_DB_PATH", config.get("database_path", "/tmp/mmpredictions.sqlite3")))


def connect(config: dict[str, Any]) -> sqlite3.Connection:
    path = database_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path = str(path.resolve())
    handle = getattr(_LOCAL, "handle", None)
    handle_path = getattr(_LOCAL, "path", None)
    if handle is not None and handle_path == resolved_path:
        return handle
    if handle is not None:
        handle.close()
    restored = gcs_store.restore_sqlite_snapshot(config, path)
    if restored:
        print(f"restored pROAS SQLite snapshot from gs://{gcs_store.bucket_name(config)}/{gcs_store.object_name(config, 'snapshots/latest.sqlite3.gz')}")
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=WAL")
    db.execute("pragma synchronous=NORMAL")
    db.execute("pragma busy_timeout=10000")
    migrate_schema_once(db, resolved_path)
    _LOCAL.handle = db
    _LOCAL.path = resolved_path
    return db


def close_thread_connection() -> None:
    handle = getattr(_LOCAL, "handle", None)
    if handle is not None:
        handle.close()
    _LOCAL.handle = None
    _LOCAL.path = None


def init_db(db: sqlite3.Connection) -> None:
    migrate_schema(db)


def create_cohort_table_sql(table_name: str) -> str:
    columns = ",\n          ".join(f"{name} {definition}" for name, definition in COHORT_COLUMNS)
    pk = ",\n            ".join(COHORT_PK)
    return f"""
        create table if not exists {table_name} (
          {columns},
          primary key (
            {pk}
          )
        )
    """


def create_sync_runs(db: sqlite3.Connection) -> None:
    db.execute(
        """
        create table if not exists sync_runs (
          id integer primary key autoincrement,
          started_at text not null,
          finished_at text,
          status text not null,
          weeks_requested integer not null default 0,
          rows_upserted integer not null default 0,
          warning text
        )
        """
    )


def table_columns(db: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    return {row["name"] if isinstance(row, sqlite3.Row) else row[1]: row for row in db.execute(f"pragma table_info({table_name})")}


def primary_key_columns(db: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    columns = table_columns(db, table_name).values()
    return tuple(
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in sorted(
            (row for row in columns if (row["pk"] if isinstance(row, sqlite3.Row) else row[5])),
            key=lambda row: row["pk"] if isinstance(row, sqlite3.Row) else row[5],
        )
    )


def backfill_country_from_raw_json(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "select rowid, raw_json from cohort_rows where country='All countries' or country_code='ZZ'"
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        country = str(raw.get("country") or "All countries")
        country_code = str(raw.get("country_code") or "ZZ")
        db.execute(
            "update cohort_rows set country=?, country_code=? where rowid=?",
            (country, country_code, row["rowid"]),
        )


def migrate_schema_once(db: sqlite3.Connection, database_key: str) -> None:
    with MIGRATION_LOCK:
        if database_key in MIGRATED_DATABASES:
            return
        migrate_schema(db)
        MIGRATED_DATABASES.add(database_key)


def migrate_schema(db: sqlite3.Connection) -> None:
    table_exists = db.execute(
        "select 1 from sqlite_master where type='table' and name='cohort_rows'"
    ).fetchone()
    try:
        db.execute("begin immediate")
        create_sync_runs(db)
        if not table_exists:
            db.execute(create_cohort_table_sql("cohort_rows"))
            db.commit()
            return

        existing_columns = table_columns(db, "cohort_rows")
        for name, definition in COHORT_COLUMNS:
            if name not in existing_columns:
                db.execute(f"alter table cohort_rows add column {name} {definition}")
        backfill_country_from_raw_json(db)

        current_pk = primary_key_columns(db, "cohort_rows")
        if current_pk != COHORT_PK:
            db.execute("drop table if exists cohort_rows_v2")
            db.execute(create_cohort_table_sql("cohort_rows_v2"))
            column_names = ", ".join(name for name, _definition in COHORT_COLUMNS)
            db.execute(
                f"""
                insert or replace into cohort_rows_v2 ({column_names})
                select {column_names}
                from cohort_rows
                """
            )
            migrated = db.execute("select count(*) from cohort_rows_v2").fetchone()[0]
            db.execute("drop table cohort_rows")
            db.execute("alter table cohort_rows_v2 rename to cohort_rows")
            print(f"migrated cohort_rows schema; rows_migrated={migrated}")
        db.commit()
    except Exception:
        db.rollback()
        raise


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_rate(value: Any) -> float | None:
    rate = as_float(value)
    if rate is None:
        return None
    if rate > 1.0:
        rate = rate / 100.0
    return max(0.0, min(rate, 1.0))


def retention_rate_from_row(row: dict[str, Any], period: str) -> float | None:
    direct = as_rate(row.get(f"retention_{period}") or row.get(f"retention_rate_{period}"))
    if direct is not None:
        return direct
    retained = as_float(row.get(f"retained_users_{period}"))
    cohort = as_float(row.get(f"cohort_size_{period}"))
    if retained is None or cohort is None or cohort <= 0:
        return None
    return max(0.0, min(retained / cohort, 1.0))


def platform_from_app(app: str) -> str:
    lower = app.lower()
    if "android" in lower:
        return "Android"
    if "ios" in lower:
        return "iOS"
    return "Unknown"


def week_windows(
    config: dict[str, Any], weeks: int | None = None, week_offset: int = 0
) -> list[tuple[dt.date, dt.date]]:
    tz = ZoneInfo(config.get("timezone", "UTC"))
    today = dt.datetime.now(tz).date()
    last_complete_week_end = today - dt.timedelta(days=today.weekday() + 1)
    lookback = int(weeks or config.get("sync", {}).get("lookback_weeks", 52))
    windows: list[tuple[dt.date, dt.date]] = []
    for offset in range(week_offset + lookback - 1, week_offset - 1, -1):
        end = last_complete_week_end - dt.timedelta(days=offset * 7)
        start = end - dt.timedelta(days=6)
        windows.append((start, end))
    return windows


def incremental_week_windows(config: dict[str, Any], week_offset: int = 0) -> list[tuple[dt.date, dt.date]]:
    sync = config.get("sync", {})
    recent_weeks = int(sync.get("weekly_refresh_weeks", 8))
    checkpoint_days = [int(day) for day in sync.get("maturity_refresh_days", [7, 30, 90, 120, 180, 360, 540, 720])]
    windows = set(week_windows(config, recent_weeks, week_offset))
    tz = ZoneInfo(config.get("timezone", "UTC"))
    today = dt.datetime.now(tz).date()
    for days in checkpoint_days:
        target_end = today - dt.timedelta(days=days)
        last_complete_week_end = target_end - dt.timedelta(days=target_end.weekday() + 1)
        if last_complete_week_end < today:
            windows.add((last_complete_week_end - dt.timedelta(days=6), last_complete_week_end))
    return sorted(windows)


def sync_week_windows(
    config: dict[str, Any], weeks: int | None = None, week_offset: int = 0
) -> tuple[list[tuple[dt.date, dt.date]], bool]:
    if weeks is None:
        return incremental_week_windows(config, week_offset), True
    return week_windows(config, weeks, week_offset), False


def day_windows(config: dict[str, Any], days: int | None = None) -> list[tuple[dt.date, dt.date]]:
    tz = ZoneInfo(config.get("timezone", "UTC"))
    today = dt.datetime.now(tz).date()
    default_days = config.get("sync", {}).get("daily_refresh_days", config.get("sync", {}).get("recent_days", 3))
    lookback = int(days or default_days)
    return [(today - dt.timedelta(days=offset), today - dt.timedelta(days=offset)) for offset in range(lookback, 0, -1)]


def fetch_adjust_period(config: dict[str, Any], start: dt.date, end: dt.date) -> dict[str, Any]:
    adjust = config["adjust"]
    token = os.environ.get(adjust.get("api_token_env", "ADJUST_API_TOKEN"))
    if not token:
        raise RuntimeError(f"Missing {adjust.get('api_token_env', 'ADJUST_API_TOKEN')}")
    params = {
        "ad_spend_mode": adjust.get("ad_spend_mode", "network"),
        "date_period": f"{start.isoformat()}:{end.isoformat()}",
        "dimensions": ",".join(adjust.get("dimensions", [])),
        "metrics": ",".join(adjust.get("metrics", [])),
    }
    app_tokens = adjust.get("app_tokens", [])
    if app_tokens:
        params["app_token__in"] = ",".join(app_tokens)
    url = "https://automate.adjust.com/reports-service/report?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Adjust HTTP {exc.code}: {body[:500]}") from exc


def upsert_rows(
    db: sqlite3.Connection,
    rows: list[dict[str, Any]],
    start: dt.date,
    end: dt.date,
    granularity: str = "week",
) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    count = 0
    try:
        db.execute("begin immediate")
        for row in rows:
            app = str(row.get("app") or "Unknown")
            values = {
                "cohort_start": start.isoformat(),
                "cohort_end": end.isoformat(),
                "granularity": granularity,
                "app": app,
                "platform": platform_from_app(app),
                "country": str(row.get("country") or "All countries"),
                "country_code": str(row.get("country_code") or "ZZ"),
                "partner_name": str(row.get("partner_name") or "unknown"),
                "campaign_network": str(row.get("campaign_network") or "unknown"),
                "campaign_id_network": str(row.get("campaign_id_network") or "unknown"),
                "installs": as_float(row.get("installs")) or 0.0,
                "network_installs": as_float(row.get("network_installs")) or 0.0,
                "network_cost": as_float(row.get("network_cost")) or 0.0,
                "network_ecpi": as_float(row.get("network_ecpi")) or 0.0,
                "roas_d0": as_float(row.get("roas_d0")),
                "roas_d1": as_float(row.get("roas_d1")),
                "roas_d3": as_float(row.get("roas_d3")),
                "roas_d7": as_float(row.get("roas_d7")),
                "roas_d30": as_float(row.get("roas_d30")),
                "roas_d60": as_float(row.get("roas_d60")),
                "roas_d90": as_float(row.get("roas_d90")),
                "roas_d120": as_float(row.get("roas_d120")),
                "roas_m3": as_float(row.get("roas_m3")),
                "roas_m4": as_float(row.get("roas_m4")),
                "roas_m6": as_float(row.get("roas_m6")),
                "roas_m12": as_float(row.get("roas_m12")),
                "roas_m18": as_float(row.get("roas_m18")),
                "roas_m24": as_float(row.get("roas_m24")),
                "revenue_d7": as_float(row.get("revenue_d7")),
                "revenue_d30": as_float(row.get("revenue_d30")),
                "retention_d1": retention_rate_from_row(row, "d1"),
                "retention_d3": retention_rate_from_row(row, "d3"),
                "retention_d7": retention_rate_from_row(row, "d7"),
                "retention_d14": retention_rate_from_row(row, "d14"),
                "retention_d30": retention_rate_from_row(row, "d30"),
                "retention_d60": retention_rate_from_row(row, "d60"),
                "retention_d90": retention_rate_from_row(row, "d90"),
                "retention_d120": retention_rate_from_row(row, "d120"),
                "retention_m6": retention_rate_from_row(row, "m6"),
                "retention_m12": retention_rate_from_row(row, "m12"),
                "retention_m18": retention_rate_from_row(row, "m18"),
                "retention_m24": retention_rate_from_row(row, "m24"),
                **{metric: as_float(row.get(metric)) for metric in EARLY_FEATURE_METRICS},
                "event_features_json": json.dumps(
                    {metric: row.get(metric) for metric in EARLY_FEATURE_METRICS if row.get(metric) is not None},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                "raw_json": json.dumps(row, ensure_ascii=True, sort_keys=True),
                "fetched_at": now,
            }
            column_names = [name for name, _definition in COHORT_COLUMNS]
            insert_columns = ", ".join(column_names)
            insert_values = ", ".join(f":{name}" for name in column_names)
            update_columns = [name for name in column_names if name not in COHORT_PK]
            update_clause = ", ".join(f"{name}=excluded.{name}" for name in update_columns)
            conflict_columns = ", ".join(COHORT_PK)
            db.execute(
                f"""
                insert into cohort_rows ({insert_columns})
                values ({insert_values})
                on conflict ({conflict_columns}) do update set {update_clause}
                """,
                values,
            )
            count += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return count


def sync_windows(
    db: sqlite3.Connection,
    config: dict[str, Any],
    windows: list[tuple[dt.date, dt.date]],
    granularity: str,
    force: bool,
) -> int:
    rows_upserted = 0
    for start, end in windows:
        if not force:
            existing = db.execute(
                "select count(*) from cohort_rows where granularity=? and cohort_start=?",
                (granularity, start.isoformat()),
            ).fetchone()[0]
            if existing:
                continue
        payload = fetch_adjust_period(config, start, end)
        rows_upserted += upsert_rows(db, payload.get("rows", []), start, end, granularity)
        time.sleep(0.15)
    return rows_upserted


def sync_adjust(
    config: dict[str, Any],
    force: bool = False,
    mode: str = "all",
    days: int | None = None,
    weeks: int | None = None,
    week_offset: int = 0,
) -> dict[str, Any]:
    load_local_env()
    db = connect(config)
    if mode not in {"all", "weekly", "daily"}:
        raise ValueError("mode must be one of: all, weekly, daily")
    weekly_windows: list[tuple[dt.date, dt.date]] = []
    weekly_force = force
    if mode in {"all", "weekly"}:
        weekly_windows, incremental_force = sync_week_windows(config, weeks, week_offset)
        weekly_force = force or incremental_force
    daily_windows = day_windows(config, days) if mode in {"all", "daily"} else []
    windows_requested = 0
    if mode in {"all", "weekly"}:
        windows_requested += len(weekly_windows)
    if mode in {"all", "daily"}:
        windows_requested += len(daily_windows)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    run = db.execute(
        "insert into sync_runs(started_at, status, weeks_requested) values (?, ?, ?)",
        (started_at, "running", windows_requested),
    )
    run_id = int(run.lastrowid)
    db.commit()
    rows_upserted = 0
    warnings: list[str] = []
    try:
        if mode in {"all", "weekly"}:
            rows_upserted += sync_windows(
                db, config, weekly_windows, "week", weekly_force
            )
        if mode in {"all", "daily"}:
            daily_force = force or mode == "daily"
            rows_upserted += sync_windows(db, config, daily_windows, "day", daily_force)
        status = "ok"
        warning_text = "; ".join(warnings[:5]) if warnings else None
    except Exception as exc:  # noqa: BLE001 - operational sync should be visible.
        status = "error"
        warning_text = str(exc)
    db.execute(
        """
        update sync_runs
        set finished_at=?, status=?, rows_upserted=?, warning=?
        where id=?
        """,
        (dt.datetime.now(dt.timezone.utc).isoformat(), status, rows_upserted, warning_text, run_id),
    )
    db.commit()
    try:
        store_result = persist_store(config, mode=mode) if status == "ok" else {"enabled": False}
    except Exception as exc:  # noqa: BLE001 - sync data is committed; storage failure should be visible but non-fatal.
        store_result = {"enabled": gcs_store.enabled(config), "status": "error", "warning": str(exc)}
    return {
        "status": status,
        "run_id": run_id,
        "rows_upserted": rows_upserted,
        "warning": warning_text,
        "database_path": str(database_path(config)),
        "mode": mode,
        "weeks": weeks,
        "windows_requested": windows_requested,
        "incremental": weeks is None and mode in {"all", "weekly"},
        "week_offset": week_offset,
        "store": store_result,
    }


def latest_sync(db: sqlite3.Connection) -> dict[str, Any] | None:
    row = db.execute("select * from sync_runs order by id desc limit 1").fetchone()
    return dict(row) if row else None


def ensure_seeded(config: dict[str, Any]) -> None:
    db = connect(config)
    count = db.execute("select count(*) from cohort_rows").fetchone()[0]
    weekly_count = db.execute("select count(*) from cohort_rows where granularity='week'").fetchone()[0]
    daily_count = db.execute("select count(*) from cohort_rows where granularity='day'").fetchone()[0]
    recent = latest_sync(db)
    if count == 0 or weekly_count == 0 or daily_count == 0 or not recent or recent["status"] != "ok":
        bootstrap_weeks = int(config.get("sync", {}).get("bootstrap_weeks", 52))
        sync_adjust(config, force=False, mode="all", weeks=bootstrap_weeks)


def metric_for_horizon(config: dict[str, Any], horizon: int) -> str:
    return config["prediction"]["horizon_metric_map"][str(horizon)]


def retention_metric_for_horizon(config: dict[str, Any], horizon: int) -> str:
    return config.get("retention", {}).get("horizon_metric_map", RETENTION_DEFAULTS["horizon_metric_map"])[str(horizon)]


def row_metric_value(row: sqlite3.Row | dict[str, Any], metric: str) -> float | None:
    try:
        value = row[metric]
    except (KeyError, IndexError):
        value = None
    if value is not None:
        return float(value)
    if metric == "roas_d60":
        d30 = row_metric_value(row, "roas_d30")
        d90 = row_metric_value(row, "roas_d90")
        if d30 is not None and d90 is not None:
            return d30 + (d90 - d30) * 0.5
    if metric == "retention_d60":
        d30 = row_metric_value(row, "retention_d30")
        d90 = row_metric_value(row, "retention_d90")
        if d30 is not None and d90 is not None:
            return d30 + (d90 - d30) * 0.5
    return None


def row_has_direct_metric(row: sqlite3.Row | dict[str, Any], metric: str) -> bool:
    try:
        return row[metric] is not None
    except (KeyError, IndexError):
        return False


def cohort_age_days(row: sqlite3.Row, config: dict[str, Any]) -> int:
    tz = ZoneInfo(config.get("timezone", "UTC"))
    today = dt.datetime.now(tz).date()
    cohort_end = dt.date.fromisoformat(row["cohort_end"])
    return max(0, (today - cohort_end).days)


def best_anchor(row: sqlite3.Row, horizon: int, age_days: int, config: dict[str, Any]) -> tuple[int, float] | None:
    minimum_anchor = float(config.get("sync", {}).get("minimum_anchor_roas", 0.0))
    anchors = [int(anchor) for anchor in config.get("prediction", {}).get("anchors", [0, 1, 3, 7])]
    for anchor in sorted(anchors):
        if anchor >= horizon or anchor > age_days:
            continue
        value = row[f"roas_d{anchor}"]
        if value is not None and value >= minimum_anchor:
            return anchor, float(value)
    return None


def best_retention_anchor(row: sqlite3.Row | dict[str, Any], horizon: int, age_days: int, config: dict[str, Any]) -> tuple[int, float] | None:
    minimum_anchor = float(config.get("retention", {}).get("minimum_anchor_retention", 0.0001))
    anchors = [int(anchor) for anchor in config.get("retention", {}).get("anchors", RETENTION_DEFAULTS["anchors"])]
    for anchor in sorted(anchors):
        if anchor >= horizon or anchor > age_days:
            continue
        value = row_metric_value(row, f"retention_d{anchor}")
        if value is not None and value >= minimum_anchor:
            return anchor, float(value)
    return None


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def weighted_percentile(values: list[float], weights: list[float], q: float) -> float:
    if not values:
        return 0.0
    pairs = sorted(
        (float(value), max(float(weight), 0.0))
        for value, weight in zip(values, weights, strict=False)
        if math.isfinite(float(value)) and math.isfinite(float(weight)) and float(weight) > 0
    )
    if not pairs:
        return percentile(values, q)
    total = sum(weight for _value, weight in pairs)
    threshold = max(0.0, min(q, 1.0)) * total
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= threshold:
            return value
    return pairs[-1][0]


def ratio_group_specs(row: sqlite3.Row) -> list[tuple[str, Any]]:
    return [
        ("campaign_country", lambda r: r["platform"] == row["platform"] and r["country_code"] == row["country_code"] and r["partner_name"] == row["partner_name"] and r["campaign_network"] == row["campaign_network"]),
        ("campaign", lambda r: r["platform"] == row["platform"] and r["partner_name"] == row["partner_name"] and r["campaign_network"] == row["campaign_network"]),
        ("channel_country", lambda r: r["platform"] == row["platform"] and r["country_code"] == row["country_code"] and r["partner_name"] == row["partner_name"]),
        ("channel", lambda r: r["platform"] == row["platform"] and r["partner_name"] == row["partner_name"]),
        ("country", lambda r: r["platform"] == row["platform"] and r["country_code"] == row["country_code"]),
        ("platform", lambda r: r["platform"] == row["platform"]),
        ("global", lambda r: True),
    ]


def same_cohort(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    return (
        left["cohort_start"] == right["cohort_start"]
        and left["granularity"] == right["granularity"]
        and left["app"] == right["app"]
        and left["country_code"] == right["country_code"]
        and left["partner_name"] == right["partner_name"]
        and left["campaign_network"] == right["campaign_network"]
        and left["campaign_id_network"] == right["campaign_id_network"]
    )


def ratio_candidates(
    rows: list[sqlite3.Row],
    row: sqlite3.Row,
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
    predicate,
) -> list[sqlite3.Row]:
    metric = metric_for_horizon(config, horizon)
    minimum_anchor = float(config.get("sync", {}).get("minimum_anchor_roas", 0.0))
    return [
        candidate
        for candidate in rows
        if predicate(candidate)
        and cohort_age_days(candidate, config) >= horizon
        and candidate[f"roas_d{anchor_day}"] is not None
        and candidate[f"roas_d{anchor_day}"] >= minimum_anchor
        and row_metric_value(candidate, metric) is not None
        and row_metric_value(candidate, metric) > 0
        and not same_cohort(candidate, row)
    ]


def ratio_values(cohorts: list[sqlite3.Row], metric: str, anchor_day: int) -> list[float]:
    ratios = [float(row_metric_value(row, metric) or 0) / float(row[f"roas_d{anchor_day}"]) for row in cohorts]
    return [ratio for ratio in ratios if math.isfinite(ratio) and 0 < ratio < 100]


def ratio_mape(cohorts: list[sqlite3.Row], metric: str, anchor_day: int, ratio: float) -> float | None:
    residuals = []
    for cohort in cohorts:
        actual = float(row_metric_value(cohort, metric) or 0)
        predicted = float(cohort[f"roas_d{anchor_day}"] or 0) * ratio
        if actual > 0:
            residuals.append(abs(predicted - actual) / actual)
    return statistics.median(residuals) if residuals else None


def ratio_stats(
    rows: list[sqlite3.Row],
    row: sqlite3.Row,
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    metric = metric_for_horizon(config, horizon)
    cohorts: list[sqlite3.Row] = []
    group_name = "global"
    for name, predicate in ratio_group_specs(row):
        candidates = ratio_candidates(rows, row, horizon, anchor_day, config, predicate)
        if len(candidates) >= 5 or name == "global":
            cohorts = candidates
            group_name = name
            break
    ratios = ratio_values(cohorts, metric, anchor_day)
    if not ratios:
        return {"ratio": 1.0, "low_ratio": 0.6, "high_ratio": 1.6, "sample_size": 0, "group": group_name, "mape": None}
    median = statistics.median(ratios)
    low = percentile(ratios, 0.2)
    high = percentile(ratios, 0.8)
    mape = ratio_mape(cohorts, metric, anchor_day, median)
    return {
        "ratio": median,
        "low_ratio": min(low, median),
        "high_ratio": max(high, median),
        "sample_size": len(ratios),
        "group": group_name,
        "mape": mape,
    }


def retention_ratio_candidates(
    rows: list[sqlite3.Row],
    row: sqlite3.Row | dict[str, Any],
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
    predicate,
) -> list[sqlite3.Row]:
    metric = retention_metric_for_horizon(config, horizon)
    minimum_anchor = float(config.get("retention", {}).get("minimum_anchor_retention", 0.0001))
    return [
        candidate
        for candidate in rows
        if predicate(candidate)
        and cohort_age_days(candidate, config) >= horizon
        and row_metric_value(candidate, f"retention_d{anchor_day}") is not None
        and float(row_metric_value(candidate, f"retention_d{anchor_day}") or 0) >= minimum_anchor
        and row_metric_value(candidate, metric) is not None
        and row_metric_value(candidate, metric) > 0
        and not same_cohort(candidate, row)  # type: ignore[arg-type]
    ]


def retention_ratio_values(cohorts: list[sqlite3.Row], metric: str, anchor_day: int) -> list[float]:
    ratios = [
        float(row_metric_value(row, metric) or 0) / float(row_metric_value(row, f"retention_d{anchor_day}") or 1)
        for row in cohorts
    ]
    return [ratio for ratio in ratios if math.isfinite(ratio) and 0 < ratio < 10]


def retention_ratio_mape(cohorts: list[sqlite3.Row], metric: str, anchor_day: int, ratio: float) -> float | None:
    residuals = []
    for cohort in cohorts:
        actual = float(row_metric_value(cohort, metric) or 0)
        predicted = float(row_metric_value(cohort, f"retention_d{anchor_day}") or 0) * ratio
        if actual > 0:
            residuals.append(abs(predicted - actual) / actual)
    return statistics.median(residuals) if residuals else None


def retention_ratio_stats(
    rows: list[sqlite3.Row],
    row: sqlite3.Row | dict[str, Any],
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    metric = retention_metric_for_horizon(config, horizon)
    cohorts: list[sqlite3.Row] = []
    group_name = "global"
    for name, predicate in ratio_group_specs(row):  # type: ignore[arg-type]
        candidates = retention_ratio_candidates(rows, row, horizon, anchor_day, config, predicate)
        if len(candidates) >= 5 or name == "global":
            cohorts = candidates
            group_name = name
            break
    ratios = retention_ratio_values(cohorts, metric, anchor_day)
    if not ratios:
        return {"ratio": 1.0, "low_ratio": 0.6, "high_ratio": 1.2, "sample_size": 0, "group": group_name, "mape": None}
    median = statistics.median(ratios)
    low = percentile(ratios, 0.2)
    high = percentile(ratios, 0.8)
    mape = retention_ratio_mape(cohorts, metric, anchor_day, median)
    return {
        "ratio": median,
        "low_ratio": min(low, median),
        "high_ratio": max(high, median),
        "sample_size": len(ratios),
        "group": group_name,
        "mape": mape,
    }


def shrinkage_prior_strength(config: dict[str, Any]) -> dict[str, float]:
    defaults = {
        "global": 0.0,
        "platform": 40.0,
        "country": 32.0,
        "channel": 24.0,
        "channel_country": 18.0,
        "campaign": 12.0,
        "campaign_country": 8.0,
    }
    configured = config.get("shrinkage", {}).get("prior_strength", {})
    return {name: float(configured.get(name, value)) for name, value in defaults.items()}


def blend_log_ratio(raw: float, prior: float, sample_size: int, prior_strength: float) -> float:
    if sample_size <= 0:
        return prior
    weight = sample_size / max(sample_size + prior_strength, 1.0)
    return weight * math.log(max(raw, 1e-9)) + (1.0 - weight) * prior


def ratio_stats_shrinkage(
    rows: list[sqlite3.Row],
    row: sqlite3.Row,
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    metric = metric_for_horizon(config, horizon)
    strengths = shrinkage_prior_strength(config)
    posterior_log = 0.0
    posterior_low_log = math.log(0.6)
    posterior_high_log = math.log(1.6)
    selected_group = "global"
    selected_candidates: list[sqlite3.Row] = []
    selected_sample_size = 0
    effective_sample_size = 0
    components = []

    for name, predicate in reversed(ratio_group_specs(row)):
        candidates = ratio_candidates(rows, row, horizon, anchor_day, config, predicate)
        ratios = ratio_values(candidates, metric, anchor_day)
        if not ratios:
            continue
        sample_size = len(ratios)
        prior_strength = strengths.get(name, 12.0)
        raw_median = statistics.median(ratios)
        raw_low = min(percentile(ratios, 0.2), raw_median)
        raw_high = max(percentile(ratios, 0.8), raw_median)
        posterior_log = blend_log_ratio(raw_median, posterior_log, sample_size, prior_strength)
        posterior_low_log = blend_log_ratio(raw_low, posterior_low_log, sample_size, prior_strength)
        posterior_high_log = blend_log_ratio(raw_high, posterior_high_log, sample_size, prior_strength)
        selected_group = name
        selected_candidates = candidates
        selected_sample_size = sample_size
        effective_sample_size = max(effective_sample_size, sample_size)
        components.append(
            {
                "group": name,
                "sample_size": sample_size,
                "raw_ratio": raw_median,
                "prior_strength": prior_strength,
            }
        )

    if not components:
        return {
            "ratio": 1.0,
            "low_ratio": 0.6,
            "high_ratio": 1.6,
            "sample_size": 0,
            "leaf_sample_size": 0,
            "group": "global",
            "mape": None,
            "components": [],
        }

    ratio = math.exp(posterior_log)
    low = min(math.exp(posterior_low_log), ratio)
    high = max(math.exp(posterior_high_log), ratio)
    mape = ratio_mape(selected_candidates, metric, anchor_day, ratio)
    return {
        "ratio": ratio,
        "low_ratio": low,
        "high_ratio": high,
        "sample_size": effective_sample_size,
        "leaf_sample_size": selected_sample_size,
        "group": selected_group,
        "mape": mape,
        "components": components,
    }


FEATURE_METRIC_SPECS = (
    (1, "revenue_events_total_d1", "install_rate"),
    (1, "sessions_d1", "install_rate"),
    (3, "revenue_events_total_d3", "install_rate"),
    (3, "sessions_d3", "install_rate"),
    (7, "revenue_events_total_d7", "install_rate"),
    (7, "revenue_events_per_user_d7", "raw"),
    (7, "revenue_events_per_active_user_d7", "raw"),
    (7, "first_paying_users_total_d7", "install_rate"),
    (7, "cumulative_paying_users_conversion_rate_d7", "raw"),
    (7, "paying_user_size_d7", "raw"),
    (7, "revenue_total_per_paying_user_d7", "raw"),
    (7, "sessions_d7", "install_rate"),
    (7, "sessions_per_user_d7", "raw"),
    (7, "time_spent_per_user_d7", "raw"),
    (7, "ad_impressions_total_d7", "install_rate"),
    (7, "ad_revenue_total_d7", "install_rate"),
    (7, "ad_rpm_d7", "raw"),
)


def feature_model_config(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "base_model": "shrinkage_multiplier_v1",
        "decision_days": [1, 3, 7],
        "min_features": 2,
        "min_feature_samples": 5,
        "temperature": 0.75,
        "blend_strength": 0.45,
    }
    merged = dict(defaults)
    merged.update(config.get("feature_model", {}))
    return merged


def feature_metric_value(row: sqlite3.Row | dict[str, Any], metric: str, denominator: str) -> float | None:
    value = row_metric_value(row, metric)
    if value is None or value < 0:
        return None
    if denominator == "install_rate":
        installs = float(row_metric_value(row, "network_installs") or row_metric_value(row, "installs") or 0)
        if installs <= 0:
            return None
        return value / installs
    if denominator == "cost_rate":
        cost = float(row_metric_value(row, "network_cost") or 0)
        if cost <= 0:
            return None
        return value / cost
    return value


def feature_vector(row: sqlite3.Row | dict[str, Any], anchor_day: int) -> dict[str, float]:
    vector: dict[str, float] = {}
    for day, metric, denominator in FEATURE_METRIC_SPECS:
        if day > anchor_day:
            continue
        value = feature_metric_value(row, metric, denominator)
        if value is not None and math.isfinite(value):
            vector[metric] = math.log1p(max(value, 0.0))
    return vector


def feature_decision_anchor(row: sqlite3.Row, horizon: int, age_days: int, config: dict[str, Any]) -> tuple[int, float] | None:
    minimum_anchor = float(config.get("sync", {}).get("minimum_anchor_roas", 0.0))
    days = [int(day) for day in feature_model_config(config).get("decision_days", [1, 3, 7])]
    for day in sorted(days, reverse=True):
        if day >= horizon or day > age_days:
            continue
        value = row_metric_value(row, f"roas_d{day}")
        if value is not None and value >= minimum_anchor and feature_vector(row, day):
            return day, float(value)
    return best_anchor(row, horizon, age_days, config)


def anchor_for_model(model: str, row: sqlite3.Row, horizon: int, age_days: int, config: dict[str, Any]) -> tuple[int, float] | None:
    if model == "feature_multiplier_v1":
        return feature_decision_anchor(row, horizon, age_days, config)
    return best_anchor(row, horizon, age_days, config)


def feature_weighted_ratio_stats(
    base_stats: dict[str, Any],
    candidates: list[sqlite3.Row],
    row: sqlite3.Row,
    metric: str,
    anchor_day: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    cfg = feature_model_config(config)
    subject_vector = feature_vector(row, anchor_day)
    min_features = int(cfg.get("min_features", 2))
    min_samples = int(cfg.get("min_feature_samples", 5))
    if len(subject_vector) < min_features:
        result = dict(base_stats)
        result.update({"feature_sample_size": 0, "feature_count": len(subject_vector), "feature_blend": 0.0})
        return result

    weighted: list[tuple[float, float]] = []
    temperature = max(float(cfg.get("temperature", 0.75)), 0.05)
    for candidate in candidates:
        candidate_vector = feature_vector(candidate, anchor_day)
        common = sorted(set(subject_vector) & set(candidate_vector))
        if len(common) < min_features:
            continue
        distance = sum(abs(subject_vector[name] - candidate_vector[name]) for name in common) / len(common)
        ratio = float(row_metric_value(candidate, metric) or 0) / float(candidate[f"roas_d{anchor_day}"] or 1)
        if math.isfinite(ratio) and 0 < ratio < 100:
            weighted.append((ratio, math.exp(-distance / temperature)))

    if len(weighted) < min_samples:
        result = dict(base_stats)
        result.update({"feature_sample_size": len(weighted), "feature_count": len(subject_vector), "feature_blend": 0.0})
        return result

    ratios = [ratio for ratio, _weight in weighted]
    weights = [weight for _ratio, weight in weighted]
    feature_ratio = weighted_percentile(ratios, weights, 0.5)
    feature_low = weighted_percentile(ratios, weights, 0.2)
    feature_high = weighted_percentile(ratios, weights, 0.8)
    sample_factor = min(1.0, len(weighted) / max(min_samples * 4.0, 1.0))
    blend = max(0.0, min(float(cfg.get("blend_strength", 0.45)) * sample_factor, 0.85))
    ratio = math.exp((1.0 - blend) * math.log(max(float(base_stats["ratio"]), 1e-9)) + blend * math.log(max(feature_ratio, 1e-9)))
    low = math.exp((1.0 - blend) * math.log(max(float(base_stats["low_ratio"]), 1e-9)) + blend * math.log(max(min(feature_low, feature_ratio), 1e-9)))
    high = math.exp((1.0 - blend) * math.log(max(float(base_stats["high_ratio"]), 1e-9)) + blend * math.log(max(max(feature_high, feature_ratio), 1e-9)))
    result = dict(base_stats)
    result.update(
        {
            "ratio": ratio,
            "low_ratio": min(low, ratio),
            "high_ratio": max(high, ratio),
            "mape": ratio_mape(candidates, metric, anchor_day, ratio),
            "feature_sample_size": len(weighted),
            "feature_count": len(subject_vector),
            "feature_ratio": feature_ratio,
            "feature_blend": blend,
        }
    )
    return result


def ratio_stats_feature(
    rows: list[sqlite3.Row],
    row: sqlite3.Row,
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    base_model = str(feature_model_config(config).get("base_model", "shrinkage_multiplier_v1"))
    if base_model == "feature_multiplier_v1":
        base_model = "shrinkage_multiplier_v1"
    base_stats = ratio_stats_for_model(base_model, rows, row, horizon, anchor_day, config)
    metric = metric_for_horizon(config, horizon)
    minimum_samples = int(config.get("sync", {}).get("minimum_training_samples", 5))
    selected_candidates: list[sqlite3.Row] = []
    for _name, predicate in ratio_group_specs(row):
        candidates = ratio_candidates(rows, row, horizon, anchor_day, config, predicate)
        if len(candidates) >= minimum_samples:
            selected_candidates = candidates
            break
    if not selected_candidates:
        selected_candidates = ratio_candidates(rows, row, horizon, anchor_day, config, lambda _candidate: True)
    return feature_weighted_ratio_stats(base_stats, selected_candidates, row, metric, anchor_day, config)


def ratio_stats_for_model(
    model: str,
    rows: list[sqlite3.Row],
    row: sqlite3.Row,
    horizon: int,
    anchor_day: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    if model == "baseline_multiplier_v1":
        return ratio_stats(rows, row, horizon, anchor_day, config)
    if model == "shrinkage_multiplier_v1":
        return ratio_stats_shrinkage(rows, row, horizon, anchor_day, config)
    if model == "feature_multiplier_v1":
        return ratio_stats_feature(rows, row, horizon, anchor_day, config)
    raise ValueError(f"unknown model: {model}")


def confidence_score(
    row: sqlite3.Row,
    predicted: float,
    low: float,
    high: float,
    stats: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    network_installs = float(row["network_installs"] or 0)
    cost = float(row["network_cost"] or 0)
    predicted_revenue = predicted * cost
    min_installs = float(config.get("sync", {}).get("minimum_network_installs", 10))
    min_revenue = float(config.get("sync", {}).get("minimum_predicted_revenue", 20.0))
    sample_size = float(stats.get("sample_size") or 0)
    mape = stats.get("mape")
    conf = confidence_config(config)
    weights = conf["weights"]
    thresholds = conf["thresholds"]
    interval_width = (high - low) / predicted if predicted > 0 else 9.99
    install_score = min(1.0, network_installs / max(min_installs * 5.0, 1.0))
    revenue_score = min(1.0, predicted_revenue / max(min_revenue * 5.0, 1.0))
    sample_score = min(1.0, sample_size / max(float(config.get("sync", {}).get("minimum_training_samples", 5)) * 4.0, 1.0))
    error_score = float(conf["missing_error_score"]) if mape is None else max(0.0, min(1.0, 1.0 - float(mape)))
    interval_score = max(0.0, min(1.0, 1.0 - interval_width))
    score = (
        float(weights["network_installs"]) * install_score
        + float(weights["predicted_revenue"]) * revenue_score
        + float(weights["training_samples"]) * sample_score
        + float(weights["historical_error"]) * error_score
        + float(weights["interval_width"]) * interval_score
    )
    if score >= float(thresholds["high"]):
        level = "high"
    elif score >= float(thresholds["medium"]):
        level = "medium"
    else:
        level = "low"
    return {
        "score": score,
        "level": level,
        "network_installs": network_installs,
        "predicted_revenue": predicted_revenue,
        "interval_width": interval_width,
        "components": {
            "network_installs": install_score,
            "predicted_revenue": revenue_score,
            "training_samples": sample_score,
            "historical_error": error_score,
            "interval_width": interval_score,
        },
    }


def cohort_end_date(row: sqlite3.Row | dict[str, Any]) -> dt.date:
    return dt.date.fromisoformat(str(row_value(row, "cohort_end")))


def read_training_rows(db: sqlite3.Connection, config: dict[str, Any]) -> list[sqlite3.Row]:
    minimum_cost = float(config.get("sync", {}).get("minimum_cost", 20.0))
    rows = list(
        db.execute(
            """
            select * from cohort_rows
            where granularity='week' and network_cost >= ?
            order by cohort_start desc, network_cost desc
            """,
            (minimum_cost,),
        )
    )
    return filter_excluded_sources(rows, config)  # type: ignore[return-value]


ROAS_COLUMNS = (
    "roas_d0",
    "roas_d1",
    "roas_d3",
    "roas_d7",
    "roas_d30",
    "roas_d60",
    "roas_d90",
    "roas_d120",
    "roas_m3",
    "roas_m4",
    "roas_m6",
    "roas_m12",
    "roas_m18",
    "roas_m24",
)
RETENTION_COLUMNS = (
    "retention_d1",
    "retention_d3",
    "retention_d7",
    "retention_d14",
    "retention_d30",
    "retention_d60",
    "retention_d90",
    "retention_d120",
    "retention_m6",
    "retention_m12",
    "retention_m18",
    "retention_m24",
)


def rows_in_date_range(rows: list[sqlite3.Row], date_from: str | None, date_to: str | None) -> list[sqlite3.Row]:
    if not date_from and not date_to:
        return rows
    return [
        row
        for row in rows
        if (not date_from or row["cohort_start"] >= date_from)
        and (not date_to or row["cohort_end"] <= date_to)
    ]


def rows_overlapping_date_range(rows: list[sqlite3.Row], date_from: str | None, date_to: str | None) -> list[sqlite3.Row]:
    if not date_from and not date_to:
        return rows
    return [
        row
        for row in rows
        if (not date_from or row["cohort_end"] >= date_from)
        and (not date_to or row["cohort_start"] <= date_to)
    ]


def row_value(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


def source_channel(partner_name: str | None) -> str:
    value = (partner_name or "unknown").strip()
    normalized = value.lower().replace("_", " ").replace("-", " ")
    if "google" in normalized or "adwords" in normalized or "admob" in normalized:
        return "Google Ads"
    if "facebook" in normalized or "meta" in normalized or normalized in {"fb", "fb ads"}:
        return "Facebook"
    if "mintegral" in normalized:
        return "Mintegral"
    if "applovin" in normalized or "app lovin" in normalized:
        return "AppLovin"
    if "unity" in normalized:
        return "Unity"
    if "iron source" in normalized or "ironsource" in normalized:
        return "ironSource"
    if "tiktok" in normalized or "tik tok" in normalized:
        return "TikTok"
    if "moloco" in normalized:
        return "Moloco"
    if "liftoff" in normalized:
        return "Liftoff"
    return value or "unknown"


def excluded_sources(config: dict[str, Any]) -> set[str]:
    return {str(source).strip() for source in config.get("sync", {}).get("excluded_sources", []) if str(source).strip()}


def filter_excluded_sources(
    rows: list[sqlite3.Row | dict[str, Any]],
    config: dict[str, Any],
) -> list[sqlite3.Row | dict[str, Any]]:
    excluded = excluded_sources(config)
    if not excluded:
        return rows
    return [row for row in rows if source_channel(str(row_value(row, "partner_name") or "")) not in excluded]


def filter_subject_rows(
    rows: list[sqlite3.Row | dict[str, Any]],
    options: dict[str, Any],
) -> list[sqlite3.Row | dict[str, Any]]:
    platform = str(options.get("platform") or "")
    country = str(options.get("country") or "")
    source = str(options.get("source") or "")
    campaign = str(options.get("campaign") or "")
    if not any([platform, country, source, campaign]):
        return rows
    return [
        row
        for row in rows
        if (not platform or row_value(row, "platform") == platform)
        and (not country or row_value(row, "country_code") == country)
        and (not source or source_channel(str(row_value(row, "partner_name") or "")) == source)
        and (not campaign or row_value(row, "campaign_network") == campaign)
    ]


def read_rows_by_granularity(db: sqlite3.Connection, config: dict[str, Any], granularity: str) -> list[sqlite3.Row]:
    minimum_cost = float(config.get("sync", {}).get("minimum_cost", 20.0))
    rows = list(
        db.execute(
            """
            select * from cohort_rows
            where granularity=?
              and network_cost >= ?
            order by cohort_start desc, network_cost desc
            """,
            (granularity, minimum_cost),
        )
    )
    return filter_excluded_sources(rows, config)  # type: ignore[return-value]


def read_all_rows_by_granularity(db: sqlite3.Connection, granularity: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            """
            select * from cohort_rows
            where granularity=?
            order by cohort_start desc, network_cost desc
            """,
            (granularity,),
        )
    )


def recent_rows(rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    starts = sorted({r["cohort_start"] for r in rows}, reverse=True)[:limit]
    return [r for r in rows if r["cohort_start"] in set(starts)]


def weighted_roas(values: list[sqlite3.Row], column: str, total_cost: float) -> float | None:
    if total_cost <= 0:
        return None
    revenue = 0.0
    seen = False
    for row in values:
        value = row[column]
        if value is None:
            continue
        revenue += float(value) * float(row["network_cost"] or 0)
        seen = True
    return revenue / total_cost if seen else None


def weighted_rate(values: list[sqlite3.Row], column: str, total_weight: float) -> float | None:
    if total_weight <= 0:
        return None
    weighted = 0.0
    seen = False
    for row in values:
        value = row[column]
        if value is None:
            continue
        weight = float(row["network_installs"] or row["installs"] or 0)
        weighted += float(value) * weight
        seen = True
    return weighted / total_weight if seen else None


def aggregate_subject_rows(rows: list[sqlite3.Row], granularity: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            row["app"],
            row["platform"],
            row["country"],
            row["country_code"],
            row["partner_name"],
            row["campaign_network"],
            row["campaign_id_network"],
        )
        groups.setdefault(key, []).append(row)
    result: list[dict[str, Any]] = []
    for key, values in groups.items():
        app, platform, country, country_code, partner, campaign, campaign_id = key
        cost = sum(float(row["network_cost"] or 0) for row in values)
        installs = sum(float(row["installs"] or 0) for row in values)
        network_installs = sum(float(row["network_installs"] or 0) for row in values)
        row: dict[str, Any] = {
            "cohort_start": min(value["cohort_start"] for value in values),
            "cohort_end": max(value["cohort_end"] for value in values),
            "granularity": granularity,
            "app": app,
            "platform": platform,
            "country": country,
            "country_code": country_code,
            "partner_name": partner,
            "campaign_network": campaign,
            "campaign_id_network": campaign_id,
            "installs": installs,
            "network_installs": network_installs,
            "network_cost": cost,
            "network_ecpi": cost / network_installs if network_installs else 0.0,
            "revenue_d7": sum(float(value["revenue_d7"] or 0) for value in values),
            "revenue_d30": sum(float(value["revenue_d30"] or 0) for value in values),
            "raw_json": "{}",
            "fetched_at": max(value["fetched_at"] for value in values),
        }
        for column in ROAS_COLUMNS:
            row[column] = weighted_roas(values, column, cost)
        for column in RETENTION_COLUMNS:
            row[column] = weighted_rate(values, column, network_installs or installs)
        result.append(row)
    return result


def override_cohort_window(rows: list[dict[str, Any]], date_from: str | None, date_to: str | None) -> list[dict[str, Any]]:
    if not date_from and not date_to:
        return rows
    for row in rows:
        if date_from:
            row["cohort_start"] = date_from
        if date_to:
            row["cohort_end"] = date_to
    return rows


def read_subject_rows(
    db: sqlite3.Connection,
    config: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> list[sqlite3.Row | dict[str, Any]]:
    options = options or {}
    scope = str(options.get("scope") or "auto")
    date_from = options.get("date_from")
    date_to = options.get("date_to")
    if scope == "day":
        rows = rows_in_date_range(read_rows_by_granularity(db, config, "day"), date_from, date_to)
        rows = rows if date_from or date_to else recent_rows(rows, int(config.get("sync", {}).get("recent_days", 21)))
        return filter_subject_rows(rows, options)
    if scope == "week":
        rows = rows_in_date_range(read_rows_by_granularity(db, config, "week"), date_from, date_to)
        rows = rows if date_from or date_to else recent_rows(rows, int(config.get("sync", {}).get("recent_weeks", 10)))
        return filter_subject_rows(rows, options)
    if scope in {"month", "custom"}:
        if scope == "custom" and (date_from or date_to):
            daily_rows = rows_in_date_range(read_rows_by_granularity(db, config, "day"), date_from, date_to)
            if daily_rows:
                return filter_subject_rows(override_cohort_window(aggregate_subject_rows(daily_rows, scope), date_from, date_to), options)
            weekly_rows = rows_overlapping_date_range(read_rows_by_granularity(db, config, "week"), date_from, date_to)
            return filter_subject_rows(override_cohort_window(aggregate_subject_rows(weekly_rows, scope), date_from, date_to), options)
        rows = rows_in_date_range(read_rows_by_granularity(db, config, "week"), date_from, date_to)
        if not rows and scope == "month":
            rows = recent_rows(read_rows_by_granularity(db, config, "week"), 4)
        elif not rows and not date_from and not date_to:
            rows = read_rows_by_granularity(db, config, "week")
        elif not date_from and not date_to and scope == "month":
            rows = recent_rows(rows, 4)
        return filter_subject_rows(aggregate_subject_rows(rows, scope), options)
    daily_rows = read_rows_by_granularity(db, config, "day")
    if daily_rows:
        return filter_subject_rows(recent_rows(daily_rows, int(config.get("sync", {}).get("recent_days", 21))), options)
    recent_weeks = int(config.get("sync", {}).get("recent_weeks", 10))
    weekly_rows = read_training_rows(db, config)
    recent_starts = sorted({r["cohort_start"] for r in weekly_rows}, reverse=True)[:recent_weeks]
    return filter_subject_rows([r for r in weekly_rows if r["cohort_start"] in set(recent_starts)], options)


def source_presence(config: dict[str, Any], db: sqlite3.Connection, options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    scope = str(options.get("scope") or "auto")
    date_from = options.get("date_from")
    date_to = options.get("date_to")
    basis = scope
    fallback = False

    if scope == "day":
        rows = rows_in_date_range(read_all_rows_by_granularity(db, "day"), date_from, date_to)
        rows = rows if date_from or date_to else recent_rows(rows, int(config.get("sync", {}).get("recent_days", 21)))
        basis = "day"
    elif scope == "week":
        rows = rows_in_date_range(read_all_rows_by_granularity(db, "week"), date_from, date_to)
        rows = rows if date_from or date_to else recent_rows(rows, int(config.get("sync", {}).get("recent_weeks", 10)))
        basis = "week"
    elif scope == "custom" and (date_from or date_to):
        rows = rows_in_date_range(read_all_rows_by_granularity(db, "day"), date_from, date_to)
        if rows:
            basis = "day"
        else:
            rows = rows_overlapping_date_range(read_all_rows_by_granularity(db, "week"), date_from, date_to)
            basis = "week"
            fallback = True
    elif scope == "month":
        rows = rows_in_date_range(read_all_rows_by_granularity(db, "week"), date_from, date_to)
        if not rows and not date_from and not date_to:
            rows = recent_rows(read_all_rows_by_granularity(db, "week"), 4)
        elif not date_from and not date_to:
            rows = recent_rows(rows, 4)
        basis = "week"
    else:
        rows = read_all_rows_by_granularity(db, "day")
        if rows:
            rows = recent_rows(rows, int(config.get("sync", {}).get("recent_days", 21)))
            basis = "day"
        else:
            rows = recent_rows(read_all_rows_by_granularity(db, "week"), int(config.get("sync", {}).get("recent_weeks", 10)))
            basis = "week"

    rows = filter_subject_rows(rows, options)
    excluded = excluded_sources(config)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = source_channel(str(row["partner_name"] or ""))
        item = grouped.setdefault(
            source,
            {
                "source": source,
                "cost": 0.0,
                "rows": 0,
                "campaigns": set(),
                "cohort_start": None,
                "cohort_end": None,
                "excluded": source in excluded,
            },
        )
        item["cost"] += float(row["network_cost"] or 0)
        item["rows"] += 1
        item["campaigns"].add(row["campaign_network"])
        item["cohort_start"] = min([value for value in [item["cohort_start"], row["cohort_start"]] if value])
        item["cohort_end"] = max([value for value in [item["cohort_end"], row["cohort_end"]] if value])

    sources = []
    minimum_cost = float(config.get("sync", {}).get("minimum_cost", 20.0))
    for item in grouped.values():
        cost = float(item["cost"] or 0)
        reason = "excluded" if item["excluded"] else "zero_spend" if cost <= 0 else "below_minimum_spend" if cost < minimum_cost else "paid"
        sources.append(
            {
                "source": item["source"],
                "cost": cost,
                "rows": int(item["rows"]),
                "campaigns": len(item["campaigns"]),
                "cohort_start": item["cohort_start"],
                "cohort_end": item["cohort_end"],
                "excluded": bool(item["excluded"]),
                "status": reason,
            }
        )

    return {
        "sources": sorted(sources, key=lambda item: (bool(item["excluded"]), -float(item["cost"]), str(item["source"]))),
        "data_scope": {
            "requested_scope": scope,
            "used_granularity": basis,
            "fallback": fallback,
            "requested_start": date_from,
            "requested_end": date_to,
            "cohort_start": min((row["cohort_start"] for row in rows), default=None),
            "cohort_end": max((row["cohort_end"] for row in rows), default=None),
        },
        "minimum_cost": minimum_cost,
        "excluded_sources": sorted(excluded),
    }


def clamp_retention(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def retention_prediction_rows_for_subjects(
    config: dict[str, Any],
    training_rows: list[sqlite3.Row],
    subjects: list[sqlite3.Row | dict[str, Any]],
) -> list[dict[str, Any]]:
    retention = config.get("retention", {})
    if not retention.get("enabled", True):
        return []
    horizons = [int(horizon) for horizon in retention.get("horizons", RETENTION_DEFAULTS["horizons"])]
    minimum_samples = int(config.get("sync", {}).get("minimum_training_samples", 5))
    result: list[dict[str, Any]] = []
    for row in subjects:
        age_days = cohort_age_days(row, config)  # type: ignore[arg-type]
        for horizon in horizons:
            metric = retention_metric_for_horizon(config, horizon)
            actual = row_metric_value(row, metric) if age_days >= horizon else None
            direct_actual = actual is not None and row_has_direct_metric(row, metric)
            anchor = best_retention_anchor(row, horizon, age_days, config)
            if anchor is None:
                continue
            anchor_day, anchor_value = anchor
            stats = retention_ratio_stats(training_rows, row, horizon, anchor_day, config)
            if stats["sample_size"] < minimum_samples:
                continue
            predicted = clamp_retention(anchor_value * float(stats["ratio"]))
            low = clamp_retention(anchor_value * float(stats["low_ratio"]))
            high = clamp_retention(anchor_value * float(stats["high_ratio"]))
            display = clamp_retention(float(actual) if actual is not None else predicted)
            target_label = retention.get("metric_labels", {}).get(metric, f"D{horizon}")
            if metric == "retention_d60" and not row_has_direct_metric(row, metric):
                target_label = "D30-D90 proxy for D60"
            result.append(
                {
                    "cohort_start": row["cohort_start"],
                    "cohort_end": row["cohort_end"],
                    "granularity": row["granularity"],
                    "app": row["app"],
                    "platform": row["platform"],
                    "country": row["country"],
                    "country_code": row["country_code"],
                    "partner_name": row["partner_name"],
                    "source_channel": source_channel(row["partner_name"]),
                    "campaign_network": row["campaign_network"],
                    "campaign_id_network": row["campaign_id_network"],
                    "installs": float(row["installs"] or 0),
                    "network_installs": float(row["network_installs"] or 0),
                    "cost": float(row["network_cost"] or 0),
                    "horizon": horizon,
                    "target_metric": metric,
                    "target_label": target_label,
                    "anchor_day": anchor_day,
                    "anchor_retention": anchor_value,
                    "predicted_retention": predicted,
                    "low_retention": min(low, predicted),
                    "high_retention": max(high, predicted),
                    "actual_retention": float(actual) if actual is not None else None,
                    "display_retention": display,
                    "retention_source": "actual" if direct_actual else "proxy" if actual is not None else "predicted",
                    "sample_size": stats["sample_size"],
                    "error_mape": stats["mape"],
                    "model_group": stats["group"],
                }
            )
    return enforce_monotonic_retention(result)


def prediction_rows(config: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    db = connect(config)
    rows = read_training_rows(db, config)
    horizons = [int(h) for h in config["prediction"]["horizons"]]
    recent = read_subject_rows(db, config, options)
    presence = source_presence(config, db, options)
    retention_predictions = retention_prediction_rows_for_subjects(config, rows, recent)
    predictions: list[dict[str, Any]] = []
    for row in recent:
        age_days = cohort_age_days(row, config)
        for horizon in horizons:
            anchor = best_anchor(row, horizon, age_days, config)
            metric = metric_for_horizon(config, horizon)
            actual = row_metric_value(row, metric) if age_days >= horizon else None
            direct_actual = actual is not None and row_has_direct_metric(row, metric)
            if anchor is None:
                continue
            anchor_day, anchor_value = anchor
            stats = ratio_stats(rows, row, horizon, anchor_day, config)
            if stats["sample_size"] < int(config.get("sync", {}).get("minimum_training_samples", 5)):
                continue
            predicted = anchor_value * stats["ratio"]
            low = anchor_value * stats["low_ratio"]
            high = anchor_value * stats["high_ratio"]
            adjust_installs = float(row["installs"] or 0)
            network_installs = float(row["network_installs"] or 0)
            cost = float(row["network_cost"] or 0)
            actual_roas = float(actual) if actual is not None else None
            target_label = config["prediction"].get("metric_labels", {}).get(metric, f"D{horizon}")
            if metric == "roas_d60" and not row_has_direct_metric(row, metric):
                target_label = "D30-D90 proxy for D60"
            display_roas = actual_roas if actual_roas is not None else predicted
            predicted_revenue = predicted * cost
            predicted_ltv = predicted_revenue / network_installs if network_installs > 0 else None
            display_revenue = display_roas * cost
            display_ltv = display_revenue / network_installs if network_installs > 0 else None
            confidence = confidence_score(row, predicted, low, high, stats, config)
            predictions.append(
                {
                    "cohort_start": row["cohort_start"],
                    "cohort_end": row["cohort_end"],
                    "granularity": row["granularity"],
                    "app": row["app"],
                    "platform": row["platform"],
                    "country": row["country"],
                    "country_code": row["country_code"],
                    "partner_name": row["partner_name"],
                    "source_channel": source_channel(row["partner_name"]),
                    "campaign_network": row["campaign_network"],
                    "campaign_id_network": row["campaign_id_network"],
                    "installs": adjust_installs,
                    "network_installs": network_installs,
                    "pltv_denominator": "network_installs",
                    "revenue_definition": config.get("sync", {}).get("revenue_definition", "all_revenue"),
                    "cost": cost,
                    "horizon": horizon,
                    "target_metric": metric,
                    "target_label": target_label,
                    "anchor_day": anchor_day,
                    "anchor_roas": anchor_value,
                    "predicted_roas": predicted,
                    "low_roas": low,
                    "high_roas": high,
                    "actual_roas": actual_roas,
                    "display_roas": display_roas,
                    "display_revenue": display_revenue,
                    "display_ltv": display_ltv,
                    "roas_source": "actual" if direct_actual else "proxy" if actual_roas is not None else "predicted",
                    "predicted_revenue": predicted_revenue,
                    "predicted_ltv": predicted_ltv,
                    "confidence_score": confidence["score"],
                    "confidence_level": confidence["level"],
                    "confidence_components": confidence["components"],
                    "interval_width": confidence["interval_width"],
                    "sample_size": stats["sample_size"],
                    "error_mape": stats["mape"],
                    "model_group": stats["group"],
                }
            )
    predictions = enforce_monotonic_horizons(predictions)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "latest_sync": latest_sync(db),
        "row_count": len(rows),
        "subject_row_count": len(recent),
        "cohort_weeks": len({r["cohort_start"] for r in rows}),
        "predictions": predictions,
        "retention_predictions": retention_predictions,
        "horizons": horizons,
        "retention_horizons": [int(h) for h in config.get("retention", {}).get("horizons", RETENTION_DEFAULTS["horizons"])],
        "scope": (options or {}).get("scope", "auto"),
        "date_from": (options or {}).get("date_from"),
        "date_to": (options or {}).get("date_to"),
        "excluded_sources": sorted(excluded_sources(config)),
        "source_presence": presence["sources"],
        "data_scope": presence["data_scope"],
        "minimum_cost": presence["minimum_cost"],
    }


def enforce_monotonic_horizons(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in predictions:
        key = (
            row["cohort_start"],
            row["cohort_end"],
            row["granularity"],
            row["app"],
            row["platform"],
            row["country_code"],
            row["partner_name"],
            row["campaign_network"],
            row["campaign_id_network"],
        )
        groups.setdefault(key, []).append(row)

    for rows in groups.values():
        prev_predicted = 0.0
        prev_display = 0.0
        prev_low = 0.0
        prev_high = 0.0
        for row in sorted(rows, key=lambda item: int(item["horizon"])):
            raw_predicted = float(row["predicted_roas"] or 0)
            raw_display = float(row.get("display_roas") or raw_predicted)
            raw_low = float(row["low_roas"] or 0)
            raw_high = float(row["high_roas"] or 0)
            predicted = max(raw_predicted, prev_predicted)
            display = max(raw_display, prev_display, predicted if row.get("roas_source") != "actual" else prev_display)
            low = max(raw_low, prev_low)
            high = max(raw_high, prev_high, display)
            adjusted = any(
                abs(after - before) > 1e-12
                for before, after in (
                    (raw_predicted, predicted),
                    (raw_display, display),
                    (raw_low, low),
                    (raw_high, high),
                )
            )
            if adjusted:
                row["raw_predicted_roas"] = raw_predicted
                row["raw_display_roas"] = raw_display
                row["raw_low_roas"] = raw_low
                row["raw_high_roas"] = raw_high
                row["monotonic_adjusted"] = True
            else:
                row["monotonic_adjusted"] = False
            row["predicted_roas"] = predicted
            row["display_roas"] = display
            row["low_roas"] = low
            row["high_roas"] = high
            cost = float(row["cost"] or 0)
            installs = float(row["network_installs"] or 0)
            row["predicted_revenue"] = predicted * cost
            row["predicted_ltv"] = row["predicted_revenue"] / installs if installs > 0 else None
            row["display_revenue"] = display * cost
            row["display_ltv"] = row["display_revenue"] / installs if installs > 0 else None
            prev_predicted = predicted
            prev_display = display
            prev_low = low
            prev_high = high
    return predictions


def enforce_monotonic_retention(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in predictions:
        key = (
            row["cohort_start"],
            row["cohort_end"],
            row["granularity"],
            row["app"],
            row["platform"],
            row["country_code"],
            row["partner_name"],
            row["campaign_network"],
            row["campaign_id_network"],
        )
        groups.setdefault(key, []).append(row)

    for rows in groups.values():
        prev_predicted = 1.0
        prev_display = 1.0
        prev_low = 1.0
        prev_high = 1.0
        for row in sorted(rows, key=lambda item: int(item["horizon"])):
            row["predicted_retention"] = min(clamp_retention(row["predicted_retention"]), prev_predicted)
            row["display_retention"] = min(clamp_retention(row["display_retention"]), prev_display)
            row["low_retention"] = min(clamp_retention(row["low_retention"]), row["predicted_retention"], prev_low)
            row["high_retention"] = min(clamp_retention(row["high_retention"]), prev_high)
            row["high_retention"] = max(row["high_retention"], row["predicted_retention"])
            prev_predicted = row["predicted_retention"]
            prev_display = row["display_retention"]
            prev_low = row["low_retention"]
            prev_high = row["high_retention"]
    return predictions


def summary(config: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = prediction_rows(config, options)
    recompute_summary_by_horizon(payload)
    return payload


def mature_training_rows_at_decision(
    rows: list[sqlite3.Row],
    subject: sqlite3.Row,
    horizon: int,
    anchor_day: int,
) -> list[sqlite3.Row]:
    decision_date = cohort_end_date(subject) + dt.timedelta(days=anchor_day)
    return [
        row
        for row in rows
        if cohort_end_date(row) + dt.timedelta(days=horizon) <= decision_date
    ]


def backtest_models(config: dict[str, Any]) -> list[str]:
    configured = config.get("backtest", {}).get(
        "models",
        ["baseline_multiplier_v1", "shrinkage_multiplier_v1", "feature_multiplier_v1"],
    )
    models = [str(model) for model in configured]
    return models or ["baseline_multiplier_v1"]


def build_backtest_prediction_row(
    model: str,
    row: sqlite3.Row,
    horizon: int,
    metric: str,
    anchor_day: int,
    anchor_value: float,
    actual: float,
    stats: dict[str, Any],
) -> dict[str, Any]:
    predicted = anchor_value * float(stats["ratio"])
    low = anchor_value * float(stats["low_ratio"])
    high = anchor_value * float(stats["high_ratio"])
    cost = float(row["network_cost"] or 0)
    network_installs = float(row["network_installs"] or 0)
    ape = abs(predicted - actual) / actual
    payload = {
        "model": model,
        "cohort_start": row["cohort_start"],
        "cohort_end": row["cohort_end"],
        "granularity": row["granularity"],
        "platform": row["platform"],
        "country": row["country"],
        "country_code": row["country_code"],
        "partner_name": row["partner_name"],
        "source_channel": source_channel(row["partner_name"]),
        "campaign_network": row["campaign_network"],
        "campaign_id_network": row["campaign_id_network"],
        "cost": cost,
        "network_installs": network_installs,
        "horizon": horizon,
        "target_metric": metric,
        "anchor_day": anchor_day,
        "anchor_roas": anchor_value,
        "actual_roas": actual,
        "predicted_roas": predicted,
        "low_roas": low,
        "high_roas": high,
        "absolute_error": abs(predicted - actual),
        "ape": ape,
        "bias": predicted - actual,
        "covered": low <= actual <= high,
        "sample_size": stats["sample_size"],
        "model_group": stats["group"],
    }
    if "leaf_sample_size" in stats:
        payload["leaf_sample_size"] = stats["leaf_sample_size"]
    for key in ("feature_sample_size", "feature_count", "feature_ratio", "feature_blend"):
        if key in stats:
            payload[key] = stats[key]
    return payload


def build_retention_backtest_prediction_row(
    row: sqlite3.Row,
    horizon: int,
    metric: str,
    anchor_day: int,
    anchor_value: float,
    actual: float,
    stats: dict[str, Any],
) -> dict[str, Any]:
    predicted = clamp_retention(anchor_value * float(stats["ratio"]))
    low = clamp_retention(anchor_value * float(stats["low_ratio"]))
    high = clamp_retention(anchor_value * float(stats["high_ratio"]))
    installs = float(row["network_installs"] or row["installs"] or 0)
    ape = abs(predicted - actual) / actual
    return {
        "model": "retention_multiplier_v1",
        "cohort_start": row["cohort_start"],
        "cohort_end": row["cohort_end"],
        "granularity": row["granularity"],
        "platform": row["platform"],
        "country": row["country"],
        "country_code": row["country_code"],
        "partner_name": row["partner_name"],
        "source_channel": source_channel(row["partner_name"]),
        "campaign_network": row["campaign_network"],
        "campaign_id_network": row["campaign_id_network"],
        "cost": float(row["network_cost"] or 0),
        "network_installs": installs,
        "horizon": horizon,
        "target_metric": metric,
        "anchor_day": anchor_day,
        "anchor_retention": anchor_value,
        "actual_retention": actual,
        "predicted_retention": predicted,
        "low_retention": min(low, predicted),
        "high_retention": max(high, predicted),
        "absolute_error": abs(predicted - actual),
        "ape": ape,
        "bias": predicted - actual,
        "covered": low <= actual <= high,
        "sample_size": stats["sample_size"],
        "model_group": stats["group"],
    }


def backtest_rows(config: dict[str, Any], options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    db = connect(config)
    training_rows = read_training_rows(db, config)
    subject_rows = filter_subject_rows(training_rows, options or {})
    horizons = [int(horizon) for horizon in config["prediction"]["horizons"]]
    models = backtest_models(config)
    minimum_samples = int(config.get("sync", {}).get("minimum_training_samples", 5))
    max_cohorts = int(config.get("backtest", {}).get("max_cohorts", 2500))
    max_rows = int(config.get("backtest", {}).get("max_rows", 20000))
    if max_cohorts > 0:
        subject_rows = sorted(subject_rows, key=lambda row: (row["cohort_start"], row["network_cost"]), reverse=True)[:max_cohorts]
    result: list[dict[str, Any]] = []
    for row in sorted(subject_rows, key=lambda item: (item["cohort_start"], item["network_cost"]), reverse=True):
        age_days = cohort_age_days(row, config)
        for horizon in horizons:
            if len(result) >= max_rows:
                return result
            metric = metric_for_horizon(config, horizon)
            actual = row_metric_value(row, metric) if age_days >= horizon else None
            if actual is None or actual <= 0:
                continue
            for model in models:
                anchor = anchor_for_model(model, row, horizon, age_days, config)
                if anchor is None:
                    continue
                anchor_day, anchor_value = anchor
                candidates = mature_training_rows_at_decision(training_rows, row, horizon, anchor_day)
                stats = ratio_stats_for_model(model, candidates, row, horizon, anchor_day, config)
                if stats["sample_size"] < minimum_samples:
                    continue
                result.append(build_backtest_prediction_row(model, row, horizon, metric, anchor_day, anchor_value, float(actual), stats))
    return result


def retention_backtest_rows(config: dict[str, Any], options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not config.get("retention", {}).get("enabled", True):
        return []
    db = connect(config)
    training_rows = read_training_rows(db, config)
    subject_rows = filter_subject_rows(training_rows, options or {})
    horizons = [int(horizon) for horizon in config.get("retention", {}).get("horizons", RETENTION_DEFAULTS["horizons"])]
    minimum_samples = int(config.get("sync", {}).get("minimum_training_samples", 5))
    max_cohorts = int(config.get("backtest", {}).get("max_cohorts", 2500))
    max_rows = int(config.get("backtest", {}).get("max_rows", 20000))
    if max_cohorts > 0:
        subject_rows = sorted(subject_rows, key=lambda row: (row["cohort_start"], row["network_cost"]), reverse=True)[:max_cohorts]
    result: list[dict[str, Any]] = []
    for row in sorted(subject_rows, key=lambda item: (item["cohort_start"], item["network_cost"]), reverse=True):
        age_days = cohort_age_days(row, config)
        for horizon in horizons:
            if len(result) >= max_rows:
                return result
            metric = retention_metric_for_horizon(config, horizon)
            actual = row_metric_value(row, metric) if age_days >= horizon else None
            if actual is None or actual <= 0:
                continue
            anchor = best_retention_anchor(row, horizon, age_days, config)
            if anchor is None:
                continue
            anchor_day, anchor_value = anchor
            candidates = mature_training_rows_at_decision(training_rows, row, horizon, anchor_day)
            stats = retention_ratio_stats(candidates, row, horizon, anchor_day, config)
            if stats["sample_size"] < minimum_samples:
                continue
            result.append(build_retention_backtest_prediction_row(row, horizon, metric, anchor_day, anchor_value, float(actual), stats))
    return result


def median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def backtest_summary(rows: list[dict[str, Any]], horizons: list[int]) -> dict[int, dict[str, Any]]:
    by_horizon: dict[int, dict[str, Any]] = {}
    for horizon in horizons:
        items = [row for row in rows if int(row["horizon"]) == horizon]
        cost = sum(float(row["cost"] or 0) for row in items)
        actual_revenue = sum(float(row["actual_roas"] or 0) * float(row["cost"] or 0) for row in items)
        predicted_revenue = sum(float(row["predicted_roas"] or 0) * float(row["cost"] or 0) for row in items)
        weighted_abs_error = sum(abs(float(row["predicted_roas"] or 0) - float(row["actual_roas"] or 0)) * float(row["cost"] or 0) for row in items)
        by_horizon[horizon] = {
            "count": len(items),
            "cost": cost,
            "actual_roas": actual_revenue / cost if cost else None,
            "predicted_roas": predicted_revenue / cost if cost else None,
            "median_ape": median([float(row["ape"]) for row in items]),
            "mean_ape": sum(float(row["ape"]) for row in items) / len(items) if items else None,
            "weighted_mape": weighted_abs_error / actual_revenue if actual_revenue > 0 else None,
            "bias": (predicted_revenue - actual_revenue) / cost if cost else None,
            "coverage": sum(1 for row in items if row["covered"]) / len(items) if items else None,
            "avg_sample_size": sum(float(row["sample_size"] or 0) for row in items) / len(items) if items else None,
        }
    return by_horizon


def backtest_summary_by_model(rows: list[dict[str, Any]], horizons: list[int], models: list[str]) -> dict[str, dict[int, dict[str, Any]]]:
    return {
        model: backtest_summary([row for row in rows if row["model"] == model], horizons)
        for model in models
    }


def retention_backtest_summary(rows: list[dict[str, Any]], horizons: list[int]) -> dict[int, dict[str, Any]]:
    by_horizon: dict[int, dict[str, Any]] = {}
    for horizon in horizons:
        items = [row for row in rows if int(row["horizon"]) == horizon]
        installs = sum(float(row["network_installs"] or 0) for row in items)
        actual_retained = sum(float(row["actual_retention"] or 0) * float(row["network_installs"] or 0) for row in items)
        predicted_retained = sum(float(row["predicted_retention"] or 0) * float(row["network_installs"] or 0) for row in items)
        weighted_abs_error = sum(abs(float(row["predicted_retention"] or 0) - float(row["actual_retention"] or 0)) * float(row["network_installs"] or 0) for row in items)
        by_horizon[horizon] = {
            "count": len(items),
            "network_installs": installs,
            "actual_retention": actual_retained / installs if installs else None,
            "predicted_retention": predicted_retained / installs if installs else None,
            "median_ape": median([float(row["ape"]) for row in items]),
            "mean_ape": sum(float(row["ape"]) for row in items) / len(items) if items else None,
            "weighted_mape": weighted_abs_error / actual_retained if actual_retained > 0 else None,
            "bias": (predicted_retained - actual_retained) / installs if installs else None,
            "coverage": sum(1 for row in items if row["covered"]) / len(items) if items else None,
            "avg_sample_size": sum(float(row["sample_size"] or 0) for row in items) / len(items) if items else None,
        }
    return by_horizon


def backtest_comparison(summary_by_model: dict[str, dict[int, dict[str, Any]]], horizons: list[int], baseline: str) -> dict[str, dict[int, dict[str, Any]]]:
    comparison: dict[str, dict[int, dict[str, Any]]] = {}
    baseline_summary = summary_by_model.get(baseline, {})
    for model, model_summary in summary_by_model.items():
        if model == baseline:
            continue
        comparison[model] = {}
        for horizon in horizons:
            base = baseline_summary.get(horizon, {})
            current = model_summary.get(horizon, {})
            base_wmape = base.get("weighted_mape")
            current_wmape = current.get("weighted_mape")
            comparison[model][horizon] = {
                "weighted_mape_delta": None if base_wmape is None or current_wmape is None else float(current_wmape) - float(base_wmape),
                "median_ape_delta": None if base.get("median_ape") is None or current.get("median_ape") is None else float(current["median_ape"]) - float(base["median_ape"]),
                "coverage_delta": None if base.get("coverage") is None or current.get("coverage") is None else float(current["coverage"]) - float(base["coverage"]),
            }
    return comparison


def backtest_pair_key(row: dict[str, Any]) -> str:
    return "\x1f".join(
        str(row.get(key, ""))
        for key in (
            "cohort_start",
            "cohort_end",
            "granularity",
            "platform",
            "country_code",
            "partner_name",
            "campaign_network",
            "campaign_id_network",
            "horizon",
        )
    )


def backtest_bucket_label(value: Any, buckets: list[tuple[float, str]]) -> str:
    number = float(value or 0)
    for limit, label in buckets:
        if number < limit:
            return label
    return buckets[-1][1]


def backtest_segment_label(row: dict[str, Any], dimension: str) -> str:
    if dimension == "country":
        return str(row.get("country") or "All countries") if row.get("country_code") == "ZZ" else f"{row.get('country') or 'Unknown'} ({row.get('country_code')})"
    if dimension == "platform":
        return str(row.get("platform") or "Unknown platform")
    if dimension == "cohort_size":
        return backtest_bucket_label(
            row.get("network_installs"),
            [
                (100, "<100 installs"),
                (500, "100-499 installs"),
                (2000, "500-1,999 installs"),
                (10000, "2,000-9,999 installs"),
                (math.inf, "10,000+ installs"),
            ],
        )
    if dimension == "spend":
        return backtest_bucket_label(
            row.get("cost"),
            [
                (100, "<$100 spend"),
                (500, "$100-499 spend"),
                (2000, "$500-1,999 spend"),
                (10000, "$2,000-9,999 spend"),
                (math.inf, "$10,000+ spend"),
            ],
        )
    return str(row.get("source_channel") or row.get("partner_name") or "Unknown source")


def backtest_weighted_mape(rows: list[dict[str, Any]]) -> float | None:
    denominator = sum(float(row.get("actual_roas") or 0) * float(row.get("cost") or 0) for row in rows)
    numerator = sum(abs(float(row.get("predicted_roas") or 0) - float(row.get("actual_roas") or 0)) * float(row.get("cost") or 0) for row in rows)
    return numerator / denominator if denominator > 0 else None


def backtest_coverage(rows: list[dict[str, Any]]) -> float | None:
    return sum(1 for row in rows if row.get("covered")) / len(rows) if rows else None


def backtest_segment_summaries(rows: list[dict[str, Any]], models: list[str], baseline: str) -> dict[str, list[dict[str, Any]]]:
    feature_model = "feature_multiplier_v1" if "feature_multiplier_v1" in models else None
    if not feature_model:
        return {}
    shrinkage_model = "shrinkage_multiplier_v1" if "shrinkage_multiplier_v1" in models else None
    required = [baseline, feature_model] + ([shrinkage_model] if shrinkage_model else [])
    grouped_pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        pair = grouped_pairs.setdefault(backtest_pair_key(row), {})
        pair[str(row.get("model"))] = row
    pairs = [pair for pair in grouped_pairs.values() if all(model and model in pair for model in required)]
    summaries: dict[str, list[dict[str, Any]]] = {}
    for dimension in ("source", "country", "platform", "cohort_size", "spend"):
        groups: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            base = pair[baseline]
            segment = backtest_segment_label(base, dimension)
            group = groups.setdefault(segment, {"segment": segment, "pairs": [], "spend": 0.0})
            group["pairs"].append(pair)
            group["spend"] += float(base.get("cost") or 0)
        rows_out = []
        for group in groups.values():
            if len(group["pairs"]) < 5:
                continue
            baseline_rows = [pair[baseline] for pair in group["pairs"]]
            feature_rows = [pair[feature_model] for pair in group["pairs"]]
            shrinkage_rows = [pair[shrinkage_model] for pair in group["pairs"]] if shrinkage_model else []
            baseline_mape = backtest_weighted_mape(baseline_rows)
            feature_mape = backtest_weighted_mape(feature_rows)
            wins = sum(
                1
                for pair in group["pairs"]
                if abs(float(pair[feature_model].get("predicted_roas") or 0) - float(pair[feature_model].get("actual_roas") or 0))
                < abs(float(pair[baseline].get("predicted_roas") or 0) - float(pair[baseline].get("actual_roas") or 0))
            )
            rows_out.append(
                {
                    "segment": group["segment"],
                    "pairs": len(group["pairs"]),
                    "spend": group["spend"],
                    "baselineMape": baseline_mape,
                    "shrinkageMape": backtest_weighted_mape(shrinkage_rows) if shrinkage_rows else None,
                    "featureMape": feature_mape,
                    "featureDelta": None if baseline_mape is None or feature_mape is None else feature_mape - baseline_mape,
                    "featureWins": wins / len(group["pairs"]) if group["pairs"] else None,
                    "featureCoverage": backtest_coverage(feature_rows),
                }
            )
        summaries[dimension] = sorted(rows_out, key=lambda row: float(row["spend"] or 0), reverse=True)[:50]
    return summaries


def backtest(config: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = backtest_rows(config, options)
    retention_rows = retention_backtest_rows(config, options)
    horizons = [int(horizon) for horizon in config["prediction"]["horizons"]]
    retention_horizons = [int(horizon) for horizon in config.get("retention", {}).get("horizons", RETENTION_DEFAULTS["horizons"])]
    models = backtest_models(config)
    summary_by_model = backtest_summary_by_model(rows, horizons, models)
    retention_summary = retention_backtest_summary(retention_rows, retention_horizons)
    baseline = models[0]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": "comparison_v1" if len(models) > 1 else baseline,
        "baseline_model": baseline,
        "models": models,
        "horizons": horizons,
        "excluded_sources": sorted(excluded_sources(config)),
        "rows": rows,
        "summary_by_horizon": summary_by_model.get(baseline, {}),
        "summary_by_model_horizon": summary_by_model,
        "comparison": backtest_comparison(summary_by_model, horizons, baseline),
        "retention_model": "retention_multiplier_v1",
        "retention_horizons": retention_horizons,
        "retention_rows": retention_rows,
        "retention_summary_by_horizon": retention_summary,
    }


def compact_backtest_payload(payload: dict[str, Any], row_limit: int = 500, retention_row_limit: int = 500) -> dict[str, Any]:
    compact = dict(payload)
    rows = list(payload.get("rows") or [])
    retention_rows = list(payload.get("retention_rows") or [])
    models = [str(model) for model in payload.get("models", [])]
    baseline = str(payload.get("baseline_model") or payload.get("model") or (models[0] if models else "baseline_multiplier_v1"))
    compact["row_count"] = len(rows)
    compact["retention_row_count"] = len(retention_rows)
    compact["rows"] = rows[: max(0, row_limit)]
    compact["retention_rows"] = retention_rows[: max(0, retention_row_limit)]
    compact["row_limit"] = row_limit
    compact["retention_row_limit"] = retention_row_limit
    compact["is_compact"] = True
    compact["segment_summaries"] = payload.get("segment_summaries") or backtest_segment_summaries(rows, models, baseline)
    return compact


def recompute_summary_by_horizon(payload: dict[str, Any]) -> None:
    predictions = payload["predictions"]
    by_horizon: dict[int, dict[str, float]] = {}
    for horizon in payload["horizons"]:
        items = [p for p in predictions if p["horizon"] == horizon]
        cost = sum(float(p["cost"] or 0) for p in items)
        network_installs = sum(float(p["network_installs"] or 0) for p in items)
        revenue = sum(float(p.get("display_revenue") or p["predicted_revenue"] or 0) for p in items)
        low_revenue = sum(float(p["low_roas"] or 0) * float(p["cost"] or 0) for p in items)
        high_revenue = sum(float(p["high_roas"] or 0) * float(p["cost"] or 0) for p in items)
        by_horizon[horizon] = {
            "cost": cost,
            "network_installs": network_installs,
            "predicted_roas": revenue / cost if cost else 0.0,
            "predicted_ltv": revenue / network_installs if network_installs else None,
            "low_roas": low_revenue / cost if cost else 0.0,
            "high_roas": high_revenue / cost if cost else 0.0,
            "items": len(items),
        }
    payload["summary_by_horizon"] = by_horizon


def synthesize_artifact_d60(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    expected_horizons = {int(horizon) for horizon in config.get("prediction", {}).get("horizons", [])}
    artifact_horizons = {int(horizon) for horizon in payload.get("horizons", [])}
    if 60 not in expected_horizons or 60 in artifact_horizons:
        return payload
    missing = expected_horizons - artifact_horizons
    if missing != {60}:
        return payload

    groups: dict[tuple[Any, ...], dict[int, dict[str, Any]]] = {}
    for row in payload.get("predictions", []):
        key = (
            row.get("cohort_start"),
            row.get("cohort_end"),
            row.get("granularity"),
            row.get("app"),
            row.get("platform"),
            row.get("country_code"),
            row.get("partner_name"),
            row.get("campaign_network"),
            row.get("campaign_id_network"),
        )
        groups.setdefault(key, {})[int(row.get("horizon") or 0)] = row

    synthesized: list[dict[str, Any]] = []
    for rows in groups.values():
        d30 = rows.get(30)
        d90 = rows.get(90)
        if not d30 or not d90:
            continue
        row = dict(d30)
        row["horizon"] = 60
        row["target_metric"] = "roas_d60"
        row["target_label"] = "D30-D90 proxy for D60"
        row["roas_source"] = "proxy"
        for field in ("predicted_roas", "display_roas", "low_roas", "high_roas"):
            row[field] = (float(d30.get(field) or 0) + float(d90.get(field) or 0)) / 2.0
        row["actual_roas"] = None
        row["sample_size"] = min(int(d30.get("sample_size") or 0), int(d90.get("sample_size") or 0))
        if d30.get("error_mape") is not None and d90.get("error_mape") is not None:
            row["error_mape"] = (float(d30["error_mape"]) + float(d90["error_mape"])) / 2.0
        if d30.get("confidence_score") is not None and d90.get("confidence_score") is not None:
            row["confidence_score"] = min(float(d30["confidence_score"]), float(d90["confidence_score"]))
        cost = float(row.get("cost") or 0)
        installs = float(row.get("network_installs") or 0)
        row["predicted_revenue"] = float(row["predicted_roas"]) * cost
        row["display_revenue"] = float(row["display_roas"]) * cost
        row["predicted_ltv"] = row["predicted_revenue"] / installs if installs > 0 else None
        row["display_ltv"] = row["display_revenue"] / installs if installs > 0 else None
        row["artifact_synthesized"] = True
        synthesized.append(row)

    if not synthesized:
        return payload
    payload = dict(payload)
    payload["predictions"] = [*payload.get("predictions", []), *synthesized]
    payload["horizons"] = sorted(artifact_horizons | {60})
    payload["artifact_synthesized_horizons"] = sorted(set(payload.get("artifact_synthesized_horizons", [])) | {60})
    recompute_summary_by_horizon(payload)
    return payload


def apply_artifact_runtime_filters(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    expected_excluded = sorted(excluded_sources(config))
    if payload.get("excluded_sources") == expected_excluded:
        return payload
    predictions = payload.get("predictions", [])
    if expected_excluded and isinstance(predictions, list):
        excluded = set(expected_excluded)
        payload = dict(payload)
        payload["predictions"] = [
            row
            for row in predictions
            if source_channel(str(row.get("partner_name") or row.get("source_channel") or "")) not in excluded
        ]
        filtered_subject_rows = len({(
            row.get("cohort_start"),
            row.get("granularity"),
            row.get("app"),
            row.get("country_code"),
            row.get("partner_name"),
            row.get("campaign_network"),
            row.get("campaign_id_network"),
        ) for row in payload["predictions"]})
        payload["artifact_runtime_filtered"] = True
        payload["artifact_original_prediction_rows"] = len(predictions)
        payload["subject_row_count"] = filtered_subject_rows
        payload["row_count"] = filtered_subject_rows
        if isinstance(payload.get("retention_predictions"), list):
            original_retention = payload["retention_predictions"]
            payload["retention_predictions"] = [
                row
                for row in original_retention
                if source_channel(str(row.get("partner_name") or row.get("source_channel") or "")) not in excluded
            ]
        recompute_summary_by_horizon(payload)
    payload["excluded_sources"] = expected_excluded
    return payload


def artifact_options_supported(options: dict[str, Any]) -> bool:
    scope = str(options.get("scope") or "auto")
    platform = str(options.get("platform") or "").strip()
    if scope not in {"day", "week", "month"}:
        return False
    if not platform:
        return False
    return not any(options.get(key) for key in ("date_from", "date_to", "country", "source", "campaign"))


def artifact_key(options: dict[str, Any]) -> str:
    scope = str(options.get("scope") or "auto")
    platform = str(options.get("platform") or "all").lower().replace(" ", "-")
    return f"summary_{scope}_{platform}"


def artifact_path(options: dict[str, Any]) -> str:
    return f"artifacts/latest/{artifact_key(options)}.json.gz"


def backtest_artifact_path() -> str:
    return "artifacts/latest/backtest_baseline.json.gz"


def load_summary_artifact(config: dict[str, Any], options: dict[str, Any]) -> dict[str, Any] | None:
    if not artifact_options_supported(options):
        return None
    try:
        payload = gcs_store.download_gzip_json(config, artifact_path(options))
    except Exception as exc:  # noqa: BLE001 - artifacts are a cache; SQLite summary is the source of truth.
        print(f"summary artifact fallback: {exc}")
        return None
    if payload is not None:
        payload = synthesize_artifact_d60(payload, config)
        expected_horizons = {int(horizon) for horizon in config.get("prediction", {}).get("horizons", [])}
        artifact_horizons = {int(horizon) for horizon in payload.get("horizons", [])}
        if expected_horizons and not expected_horizons.issubset(artifact_horizons):
            print("summary artifact fallback: artifact horizons are stale")
            return None
        payload = apply_artifact_runtime_filters(payload, config)
        payload["artifact_source"] = "gcs"
    return payload


def load_backtest_artifact(config: dict[str, Any]) -> dict[str, Any] | None:
    if not gcs_store.enabled(config):
        return None
    try:
        payload = gcs_store.download_gzip_json(config, backtest_artifact_path())
    except Exception as exc:  # noqa: BLE001 - artifacts are a cache; live backtest is the source of truth.
        print(f"backtest artifact fallback: {exc}")
        return None
    if not payload:
        return None
    expected_horizons = {int(horizon) for horizon in config.get("prediction", {}).get("horizons", [])}
    artifact_horizons = {int(horizon) for horizon in payload.get("horizons", [])}
    if expected_horizons and not expected_horizons.issubset(artifact_horizons):
        print("backtest artifact fallback: artifact horizons are stale")
        return None
    expected_models = set(backtest_models(config))
    artifact_models = set(str(model) for model in payload.get("models", []))
    if expected_models and not expected_models.issubset(artifact_models):
        print("backtest artifact fallback: artifact models are stale")
        return None
    if config.get("retention", {}).get("enabled", True) and "retention_summary_by_horizon" not in payload:
        print("backtest artifact fallback: retention benchmark missing")
        return None
    if payload.get("excluded_sources") != sorted(excluded_sources(config)):
        print("backtest artifact fallback: excluded sources changed")
        return None
    payload["artifact_source"] = "gcs"
    return payload


def build_model_stats_artifact(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[tuple[Any, ...], dict[str, Any]] = {}
    for payload in payloads:
        for row in payload.get("predictions", []):
            key = (
                row.get("platform"),
                row.get("source_channel"),
                row.get("country_code"),
                row.get("campaign_network"),
                row.get("horizon"),
                row.get("anchor_day"),
                row.get("model_group"),
            )
            current = stats.setdefault(
                key,
                {
                    "platform": row.get("platform"),
                    "source_channel": row.get("source_channel"),
                    "country_code": row.get("country_code"),
                    "campaign_network": row.get("campaign_network"),
                    "horizon": row.get("horizon"),
                    "anchor_day": row.get("anchor_day"),
                    "model_group": row.get("model_group"),
                    "sample_size": 0,
                    "error_mape_values": [],
                    "confidence_values": [],
                },
            )
            current["sample_size"] = max(int(current["sample_size"] or 0), int(row.get("sample_size") or 0))
            if row.get("error_mape") is not None:
                current["error_mape_values"].append(float(row["error_mape"]))
            if row.get("confidence_score") is not None:
                current["confidence_values"].append(float(row["confidence_score"]))
    result = []
    for row in stats.values():
        errors = sorted(row.pop("error_mape_values"))
        confidence = row.pop("confidence_values")
        row["median_error_mape"] = errors[len(errors) // 2] if errors else None
        row["avg_confidence"] = sum(confidence) / len(confidence) if confidence else None
        result.append(row)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rows": sorted(result, key=lambda r: (str(r["platform"]), str(r["source_channel"]), str(r["country_code"]), str(r["campaign_network"]), int(r["horizon"] or 0))),
    }


def artifact_scopes_for_mode(config: dict[str, Any], mode: str = "all") -> list[str]:
    configured = [str(scope) for scope in config.get("storage", {}).get("artifact_scopes", ["week", "day", "month"])]
    if mode == "daily":
        return [scope for scope in configured if scope == "day"]
    if mode == "weekly":
        return [scope for scope in configured if scope in {"week", "month"}]
    return configured


def build_compact_artifacts(config: dict[str, Any], mode: str = "all") -> dict[str, Any]:
    if not gcs_store.enabled(config):
        return {"enabled": False, "artifacts": []}
    db = connect(config)
    platforms = [row["platform"] for row in db.execute("select distinct platform from cohort_rows order by platform")]
    scopes = artifact_scopes_for_mode(config, mode)
    artifact_records = []
    payloads = []
    for scope in scopes:
        for platform in platforms:
            options = {"scope": scope, "platform": platform, "date_from": None, "date_to": None, "country": None, "source": None, "campaign": None}
            payload = summary(config, options)
            payload["artifact_source"] = "generated"
            path = artifact_path(options)
            gcs_store.upload_gzip_json(config, path, payload)
            artifact_records.append({"key": artifact_key(options), "path": path, "rows": len(payload.get("predictions", [])), "options": options})
            payloads.append(payload)
    model_stats_path = None
    backtest_path = None
    if mode != "daily":
        model_stats_path = "artifacts/latest/model_stats.json.gz"
        model_stats = build_model_stats_artifact(payloads)
        gcs_store.upload_gzip_json(config, model_stats_path, model_stats)
        backtest_path = backtest_artifact_path()
        backtest_payload = backtest(config)
        gcs_store.upload_gzip_json(config, backtest_path, backtest_payload)
    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
        "artifacts": artifact_records,
        "model_stats_path": model_stats_path,
        "backtest_path": backtest_path,
    }
    manifest_path = "artifacts/latest/daily_manifest.json.gz" if mode == "daily" else "artifacts/latest/manifest.json.gz"
    gcs_store.upload_gzip_json(config, manifest_path, manifest)
    manifest["manifest_path"] = manifest_path
    return manifest


def persist_sqlite_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    db = connect(config)
    row_count = db.execute("select count(*) from cohort_rows").fetchone()[0]
    weekly_count = db.execute("select count(*) from cohort_rows where granularity='week'").fetchone()[0]
    daily_count = db.execute("select count(*) from cohort_rows where granularity='day'").fetchone()[0]
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
        snapshot_path = pathlib.Path(handle.name)
    try:
        target = sqlite3.connect(snapshot_path)
        try:
            db.backup(target)
        finally:
            target.close()
        manifest = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "row_count": row_count,
            "weekly_count": weekly_count,
            "daily_count": daily_count,
            "source_database_path": str(database_path(config)),
        }
        gcs_store.upload_sqlite_snapshot(config, snapshot_path, manifest)
        return manifest
    finally:
        snapshot_path.unlink(missing_ok=True)


def persist_store(config: dict[str, Any], mode: str = "all") -> dict[str, Any]:
    if not gcs_store.enabled(config):
        return {"enabled": False}
    artifacts = build_compact_artifacts(config, mode=mode)
    snapshot = persist_sqlite_snapshot(config)
    return {"enabled": True, "artifacts": artifacts, "snapshot": snapshot}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "summary", "backtest"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--mode", choices=["all", "weekly", "daily"], default="all")
    parser.add_argument("--days", type=int)
    parser.add_argument("--weeks", type=int)
    parser.add_argument("--week-offset", type=int, default=0)
    parser.add_argument("--no-seed", action="store_true", help="Do not backfill missing data before summary.")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "sync":
        print(
            json.dumps(
                sync_adjust(
                    config,
                    force=args.force,
                    mode=args.mode,
                    days=args.days,
                    weeks=args.weeks,
                    week_offset=args.week_offset,
                ),
                indent=2,
            )
        )
    elif args.command == "summary":
        if not args.no_seed:
            ensure_seeded(config)
        print(json.dumps(summary(config), indent=2))
    else:
        if not args.no_seed:
            ensure_seeded(config)
        print(json.dumps(backtest(config), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
