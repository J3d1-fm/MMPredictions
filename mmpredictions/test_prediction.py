import datetime as dt
import os
import tempfile
import unittest

from mmpredictions import engine


def config(db_path: str) -> dict:
    return {
        "timezone": "UTC",
        "database_path": db_path,
        "sync": {
            "recent_weeks": 10,
            "recent_days": 0,
            "minimum_cost": 1.0,
            "minimum_anchor_roas": 0.0,
            "minimum_training_samples": 5,
            "minimum_network_installs": 1,
            "minimum_predicted_revenue": 1.0,
            "revenue_definition": "all_revenue",
            "excluded_sources": [],
        },
        "prediction": {
            "anchors": [1],
            "horizons": [7, 30],
            "horizon_metric_map": {"7": "roas_d7", "30": "roas_d30"},
            "metric_labels": {},
        },
    }


class PredictionTest(unittest.TestCase):
    def test_incremental_week_windows_include_recent_and_maturity_checkpoints(self) -> None:
        cfg = config(":memory:")
        cfg["sync"]["weekly_refresh_weeks"] = 2
        cfg["sync"]["maturity_refresh_days"] = [30, 90]

        windows = engine.incremental_week_windows(cfg)

        self.assertGreaterEqual(len(windows), 2)
        self.assertEqual(len(windows), len(set(windows)))
        self.assertTrue(all(start <= end for start, end in windows))

    def test_artifact_scopes_follow_sync_mode(self) -> None:
        cfg = config(":memory:")
        cfg["storage"] = {"artifact_scopes": ["week", "day", "month"]}

        self.assertEqual(engine.artifact_scopes_for_mode(cfg, "daily"), ["day"])
        self.assertEqual(engine.artifact_scopes_for_mode(cfg, "weekly"), ["week", "month"])
        self.assertEqual(engine.artifact_scopes_for_mode(cfg, "all"), ["week", "day", "month"])

    def test_normalize_config_adds_d60_horizon(self) -> None:
        cfg = {
            "sync": {"maturity_refresh_days": [7, 30, 90]},
            "adjust": {"metrics": ["roas_d7", "roas_d30", "roas_d90"]},
            "prediction": {"horizons": [7, 30, 90], "horizon_metric_map": {"7": "roas_d7", "30": "roas_d30", "90": "roas_d90"}},
        }

        engine.normalize_config(cfg)

        self.assertEqual(cfg["prediction"]["horizons"], [7, 30, 60, 90])
        self.assertEqual(cfg["prediction"]["horizon_metric_map"]["60"], "roas_d60")
        self.assertIn("roas_d60", cfg["adjust"]["metrics"])
        self.assertIn(60, cfg["sync"]["maturity_refresh_days"])
        self.assertEqual(cfg["sync"]["excluded_sources"], [])

    def test_sources_are_not_excluded_from_summary_by_default(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                cfg["sync"].pop("excluded_sources", None)
                db = engine.connect(cfg)
                start = dt.date(2025, 1, 6)
                for index in range(8):
                    for partner, campaign in (("google", "google_campaign"), ("facebook", "facebook_campaign")):
                        engine.upsert_rows(
                            db,
                            [
                                {
                                    "app": "MMPredictions Android",
                                    "country": "United States",
                                    "country_code": "US",
                                    "partner_name": partner,
                                    "campaign_network": campaign,
                                    "campaign_id_network": campaign,
                                    "installs": 100 + index,
                                    "network_installs": 100 + index,
                                    "network_cost": 200 + index,
                                    "network_ecpi": 2.0,
                                    "roas_d1": 0.1,
                                    "roas_d7": 0.2,
                                    "roas_d30": 0.4,
                                    "revenue_d7": 40,
                                    "revenue_d30": 80,
                                }
                            ],
                            start + dt.timedelta(days=index * 7),
                            start + dt.timedelta(days=index * 7 + 6),
                            "week",
                        )

                payload = engine.summary(cfg, {"scope": "week", "platform": "Android"})
                sources = {row["source_channel"] for row in payload["predictions"]}

                self.assertEqual(payload["excluded_sources"], [])
                self.assertEqual(sources, {"Facebook", "Google Ads"})
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_d60_metric_falls_back_to_d30_d90_midpoint(self) -> None:
        self.assertAlmostEqual(engine.row_metric_value({"roas_d30": 0.4, "roas_d90": 1.0}, "roas_d60"), 0.7)

    def test_custom_date_range_uses_daily_rows_and_selected_window(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                db = engine.connect(cfg)
                for offset in range(2):
                    day = dt.date(2026, 4, 1) + dt.timedelta(days=offset)
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": "United States",
                                "country_code": "US",
                                "partner_name": "Facebook",
                                "campaign_network": "launch",
                                "campaign_id_network": "launch",
                                "installs": 100,
                                "network_installs": 90,
                                "network_cost": 200,
                                "network_ecpi": 2.22,
                                "roas_d1": 0.1,
                                "roas_d7": 0.2,
                                "roas_d30": 0.4,
                            }
                        ],
                        day,
                        day,
                        "day",
                    )
                engine.upsert_rows(
                    db,
                    [
                        {
                            "app": "MMPredictions Android",
                            "country": "United States",
                            "country_code": "US",
                            "partner_name": "Facebook",
                            "campaign_network": "outside",
                            "campaign_id_network": "outside",
                            "installs": 100,
                            "network_installs": 90,
                            "network_cost": 999,
                            "network_ecpi": 11.1,
                            "roas_d1": 0.1,
                            "roas_d7": 0.2,
                            "roas_d30": 0.4,
                        }
                    ],
                    dt.date(2026, 4, 27),
                    dt.date(2026, 5, 3),
                    "week",
                )

                rows = engine.read_subject_rows(
                    db,
                    cfg,
                    {"scope": "custom", "date_from": "2026-04-01", "date_to": "2026-04-07", "platform": "Android"},
                )

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["cohort_start"], "2026-04-01")
                self.assertEqual(rows[0]["cohort_end"], "2026-04-07")
                self.assertEqual(rows[0]["granularity"], "custom")
                self.assertEqual(rows[0]["campaign_network"], "launch")
                self.assertEqual(rows[0]["network_cost"], 400)
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_custom_date_range_does_not_fallback_to_all_weeks(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                db = engine.connect(cfg)
                engine.upsert_rows(
                    db,
                    [
                        {
                            "app": "MMPredictions Android",
                            "country": "United States",
                            "country_code": "US",
                            "partner_name": "Facebook",
                            "campaign_network": "late",
                            "campaign_id_network": "late",
                            "installs": 100,
                            "network_installs": 90,
                            "network_cost": 999,
                            "network_ecpi": 11.1,
                            "roas_d1": 0.1,
                            "roas_d7": 0.2,
                            "roas_d30": 0.4,
                        }
                    ],
                    dt.date(2026, 4, 27),
                    dt.date(2026, 5, 3),
                    "week",
                )

                rows = engine.read_subject_rows(
                    db,
                    cfg,
                    {"scope": "custom", "date_from": "2026-04-01", "date_to": "2026-04-07", "platform": "Android"},
                )

                self.assertEqual(rows, [])
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_source_presence_keeps_zero_spend_sources_for_custom_weekly_fallback(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                cfg["sync"]["excluded_sources"] = ["Google Ads"]
                db = engine.connect(cfg)
                start = dt.date(2026, 3, 30)
                for partner, campaign, cost in (
                    ("Facebook", "facebook", 200.0),
                    ("TikTok for Business", "tiktok", 0.0),
                    ("Google Ads", "google", 500.0),
                ):
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": "United States",
                                "country_code": "US",
                                "partner_name": partner,
                                "campaign_network": campaign,
                                "campaign_id_network": campaign,
                                "installs": 100,
                                "network_installs": 90,
                                "network_cost": cost,
                                "network_ecpi": cost / 90 if cost else 0,
                                "roas_d1": 0.1,
                                "roas_d7": 0.2,
                                "roas_d30": 0.4,
                            }
                        ],
                        start,
                        start + dt.timedelta(days=6),
                        "week",
                    )

                presence = engine.source_presence(
                    cfg,
                    db,
                    {"scope": "custom", "date_from": "2026-04-01", "date_to": "2026-04-07", "platform": "Android"},
                )
                by_source = {row["source"]: row for row in presence["sources"]}

                self.assertTrue(presence["data_scope"]["fallback"])
                self.assertEqual(presence["data_scope"]["used_granularity"], "week")
                self.assertEqual(by_source["TikTok"]["status"], "zero_spend")
                self.assertEqual(by_source["TikTok"]["cost"], 0)
                self.assertTrue(by_source["Google Ads"]["excluded"])
                self.assertEqual(by_source["Facebook"]["status"], "paid")
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_summary_generates_pretention_predictions_from_retained_users(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                cfg["prediction"]["horizons"] = [7]
                cfg["retention"] = {
                    "enabled": True,
                    "anchors": [1],
                    "horizons": [7],
                    "horizon_metric_map": {"7": "retention_d7"},
                    "metric_labels": {},
                }
                db = engine.connect(cfg)
                start = dt.date(2025, 1, 6)
                for index in range(7):
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": "United States",
                                "country_code": "US",
                                "partner_name": "Facebook",
                                "campaign_network": "launch",
                                "campaign_id_network": "launch",
                                "installs": 100,
                                "network_installs": 100,
                                "network_cost": 200,
                                "network_ecpi": 2.0,
                                "roas_d1": 0.1,
                                "roas_d7": 0.2,
                                "cohort_size_d1": 100,
                                "retained_users_d1": 50,
                                "cohort_size_d7": 100,
                                "retained_users_d7": 25 + index,
                            }
                        ],
                        start + dt.timedelta(days=index * 7),
                        start + dt.timedelta(days=index * 7 + 6),
                        "week",
                    )

                payload = engine.summary(cfg, {"scope": "week", "platform": "Android"})
                retention_rows = payload["retention_predictions"]

                self.assertGreater(len(retention_rows), 0)
                self.assertTrue(all(row["horizon"] == 7 for row in retention_rows))
                self.assertTrue(all(0 <= row["display_retention"] <= 1 for row in retention_rows))
                stored = db.execute("select retention_d1, retention_d7 from cohort_rows limit 1").fetchone()
                self.assertAlmostEqual(stored["retention_d1"], 0.5)
                self.assertGreater(stored["retention_d7"], 0.0)
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_organic_source_presence_exposes_revenue_ltv_contract(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                db = engine.connect(cfg)
                engine.upsert_rows(
                    db,
                    [
                        {
                            "app": "MMPredictions Android",
                            "country": "United States",
                            "country_code": "US",
                            "partner_name": "Organic",
                            "campaign_network": "organic",
                            "campaign_id_network": "organic",
                            "installs": 200,
                            "network_installs": 0,
                            "network_cost": 0,
                            "network_ecpi": 0,
                            "roas_d7": 0.0,
                            "roas_d30": 0.0,
                            "revenue_d7": 80,
                            "revenue_d30": 20,
                        }
                    ],
                    dt.date(2026, 4, 6),
                    dt.date(2026, 4, 12),
                    "week",
                )

                presence = engine.source_presence(cfg, db, {"scope": "week", "platform": "Android"})
                organic = next(row for row in presence["sources"] if row["source"] == "Organic")

                self.assertEqual(organic["cost"], 0)
                self.assertEqual(organic["installs"], 200)
                self.assertEqual(organic["organic_metrics"]["7"]["revenue"], 80)
                self.assertEqual(organic["organic_metrics"]["30"]["revenue"], 80)
                self.assertEqual(organic["organic_metrics"]["30"]["source"], "D7 floor")
                self.assertAlmostEqual(organic["organic_metrics"]["30"]["ltv"], 0.4)
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_backtest_includes_retention_prediction_rows(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                cfg["prediction"]["horizons"] = [7]
                cfg["prediction"]["horizon_metric_map"] = {"7": "roas_d7"}
                cfg["retention"] = {
                    "enabled": True,
                    "anchors": [1],
                    "horizons": [7],
                    "horizon_metric_map": {"7": "retention_d7"},
                    "metric_labels": {},
                }
                db = engine.connect(cfg)

                def add_week(start: dt.date, campaign: str, retention_d7: float) -> None:
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": "United States",
                                "country_code": "US",
                                "partner_name": "Facebook",
                                "campaign_network": campaign,
                                "campaign_id_network": campaign,
                                "installs": 100,
                                "network_installs": 100,
                                "network_cost": 200,
                                "network_ecpi": 2.0,
                                "roas_d1": 0.1,
                                "roas_d7": 0.2,
                                "cohort_size_d1": 100,
                                "retained_users_d1": 50,
                                "cohort_size_d7": 100,
                                "retained_users_d7": retention_d7 * 100,
                            }
                        ],
                        start,
                        start + dt.timedelta(days=6),
                        "week",
                    )

                for index in range(5):
                    add_week(dt.date(2025, 1, 6) + dt.timedelta(days=index * 7), "launch", 0.25)
                target_start = dt.date(2025, 3, 3)
                add_week(target_start, "launch", 0.30)

                payload = engine.backtest(cfg, {"platform": "Android"})
                rows = [
                    row for row in payload["retention_rows"]
                    if row["cohort_start"] == target_start.isoformat() and row["horizon"] == 7
                ]

                self.assertEqual(payload["retention_model"], "retention_multiplier_v1")
                self.assertIn(7, payload["retention_summary_by_horizon"])
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(rows[0]["actual_retention"], 0.30)
                self.assertAlmostEqual(rows[0]["predicted_retention"], 0.25)
                self.assertAlmostEqual(rows[0]["ape"], 1 / 6)
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_artifact_options_require_concrete_platform_and_scope(self) -> None:
        self.assertFalse(engine.artifact_options_supported({"scope": "auto", "platform": "Android"}))
        self.assertFalse(engine.artifact_options_supported({"scope": "week"}))
        self.assertTrue(engine.artifact_options_supported({"scope": "week", "platform": "Android"}))

    def test_artifact_load_falls_back_on_cache_error(self) -> None:
        original = engine.gcs_store.download_gzip_json

        def broken_download(config: dict, relative: str) -> dict:
            raise RuntimeError("corrupt artifact")

        try:
            engine.gcs_store.download_gzip_json = broken_download  # type: ignore[assignment]
            payload = engine.load_summary_artifact(config(":memory:"), {"scope": "week", "platform": "Android"})
            self.assertIsNone(payload)
        finally:
            engine.gcs_store.download_gzip_json = original  # type: ignore[assignment]

    def test_artifact_load_falls_back_when_horizons_are_stale(self) -> None:
        original = engine.gcs_store.download_gzip_json

        def stale_download(config: dict, relative: str) -> dict:
            return {"horizons": [7, 30], "predictions": []}

        try:
            engine.gcs_store.download_gzip_json = stale_download  # type: ignore[assignment]
            cfg = config(":memory:")
            cfg["prediction"]["horizons"] = [7, 30, 60]
            payload = engine.load_summary_artifact(cfg, {"scope": "week", "platform": "Android"})
            self.assertIsNone(payload)
        finally:
            engine.gcs_store.download_gzip_json = original  # type: ignore[assignment]

    def test_artifact_load_synthesizes_d60_from_d30_d90(self) -> None:
        original = engine.gcs_store.download_gzip_json

        def missing_d60_download(config: dict, relative: str) -> dict:
            base = {
                "cohort_start": "2026-01-01",
                "cohort_end": "2026-01-07",
                "granularity": "week",
                "app": "MMPredictions Android",
                "platform": "Android",
                "country_code": "US",
                "partner_name": "Facebook",
                "source_channel": "Facebook",
                "campaign_network": "facebook",
                "campaign_id_network": "facebook",
                "cost": 100.0,
                "network_installs": 50.0,
                "sample_size": 9,
                "confidence_score": 0.7,
                "error_mape": 0.2,
            }
            return {
                "horizons": [7, 30, 90],
                "excluded_sources": [],
                "predictions": [
                    {**base, "horizon": 30, "predicted_roas": 0.4, "display_roas": 0.4, "predicted_revenue": 40.0, "display_revenue": 40.0, "low_roas": 0.3, "high_roas": 0.5},
                    {**base, "horizon": 90, "predicted_roas": 1.0, "display_roas": 1.0, "predicted_revenue": 100.0, "display_revenue": 100.0, "low_roas": 0.8, "high_roas": 1.2},
                ],
            }

        try:
            engine.gcs_store.download_gzip_json = missing_d60_download  # type: ignore[assignment]
            cfg = config(":memory:")
            cfg["prediction"]["horizons"] = [7, 30, 60, 90]
            cfg["prediction"]["horizon_metric_map"]["60"] = "roas_d60"
            payload = engine.load_summary_artifact(cfg, {"scope": "week", "platform": "Android"})

            self.assertIsNotNone(payload)
            d60 = [row for row in payload["predictions"] if row["horizon"] == 60]
            self.assertEqual(len(d60), 1)
            self.assertAlmostEqual(d60[0]["display_roas"], 0.7)
            self.assertAlmostEqual(d60[0]["display_ltv"], 1.4)
            self.assertIn(60, payload["horizons"])
            self.assertEqual(payload["artifact_synthesized_horizons"], [60])
        finally:
            engine.gcs_store.download_gzip_json = original  # type: ignore[assignment]

    def test_artifact_load_filters_stale_excluded_sources_without_sqlite_fallback(self) -> None:
        original = engine.gcs_store.download_gzip_json

        def stale_exclusion_download(config: dict, relative: str) -> dict:
            return {
                "horizons": [7],
                "excluded_sources": None,
                "predictions": [
                    {
                        "horizon": 7,
                        "partner_name": "Google Ads",
                        "source_channel": "Google Ads",
                        "cost": 100.0,
                        "network_installs": 50.0,
                        "display_revenue": 40.0,
                        "predicted_revenue": 40.0,
                        "low_roas": 0.3,
                        "high_roas": 0.6,
                        "cohort_start": "2026-01-01",
                        "granularity": "week",
                        "app": "MMPredictions Android",
                        "country_code": "US",
                        "campaign_network": "google",
                        "campaign_id_network": "google",
                    },
                    {
                        "horizon": 7,
                        "partner_name": "Facebook",
                        "source_channel": "Facebook",
                        "cost": 200.0,
                        "network_installs": 100.0,
                        "display_revenue": 100.0,
                        "predicted_revenue": 100.0,
                        "low_roas": 0.4,
                        "high_roas": 0.7,
                        "cohort_start": "2026-01-01",
                        "granularity": "week",
                        "app": "MMPredictions Android",
                        "country_code": "US",
                        "campaign_network": "facebook",
                        "campaign_id_network": "facebook",
                    },
                ],
            }

        try:
            engine.gcs_store.download_gzip_json = stale_exclusion_download  # type: ignore[assignment]
            cfg = config(":memory:")
            cfg["sync"]["excluded_sources"] = ["Google Ads"]
            cfg["prediction"]["horizons"] = [7]
            payload = engine.load_summary_artifact(cfg, {"scope": "week", "platform": "Android"})

            self.assertIsNotNone(payload)
            self.assertEqual(payload["excluded_sources"], ["Google Ads"])
            self.assertEqual({row["source_channel"] for row in payload["predictions"]}, {"Facebook"})
            self.assertAlmostEqual(payload["summary_by_horizon"][7]["predicted_roas"], 0.5)
            self.assertTrue(payload["artifact_runtime_filtered"])
        finally:
            engine.gcs_store.download_gzip_json = original  # type: ignore[assignment]

    def test_backtest_uses_only_temporally_available_mature_cohorts(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                cfg["prediction"]["horizons"] = [7]
                cfg["prediction"]["horizon_metric_map"] = {"7": "roas_d7"}
                db = engine.connect(cfg)

                def add_week(start: dt.date, campaign: str, roas_d7: float) -> None:
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": "United States",
                                "country_code": "US",
                                "partner_name": "Facebook",
                                "campaign_network": campaign,
                                "campaign_id_network": campaign,
                                "installs": 100,
                                "network_installs": 90,
                                "network_cost": 200,
                                "network_ecpi": 2.22,
                                "roas_d1": 0.1,
                                "roas_d7": roas_d7,
                            }
                        ],
                        start,
                        start + dt.timedelta(days=6),
                        "week",
                    )

                for index in range(5):
                    add_week(dt.date(2025, 1, 6) + dt.timedelta(days=index * 7), "launch", 0.2)
                target_start = dt.date(2025, 3, 3)
                add_week(target_start, "launch", 0.3)
                for index in range(5):
                    add_week(dt.date(2025, 3, 17) + dt.timedelta(days=index * 7), "launch", 0.9)

                payload = engine.backtest(cfg, {"platform": "Android"})
                target_rows = [
                    row for row in payload["rows"]
                    if row["model"] == "baseline_multiplier_v1"
                    and row["cohort_start"] == target_start.isoformat()
                    and row["horizon"] == 7
                ]
                shrinkage_rows = [
                    row for row in payload["rows"]
                    if row["model"] == "shrinkage_multiplier_v1"
                    and row["cohort_start"] == target_start.isoformat()
                    and row["horizon"] == 7
                ]

                self.assertEqual(len(target_rows), 1)
                self.assertEqual(len(shrinkage_rows), 1)
                self.assertEqual(target_rows[0]["sample_size"], 5)
                self.assertEqual(target_rows[0]["model_group"], "campaign_country")
                self.assertAlmostEqual(target_rows[0]["predicted_roas"], 0.2)
                self.assertAlmostEqual(target_rows[0]["actual_roas"], 0.3)
                self.assertAlmostEqual(target_rows[0]["ape"], 1 / 3)
                self.assertIn(7, payload["summary_by_horizon"])
                self.assertEqual(payload["model"], "comparison_v1")
                self.assertEqual(payload["baseline_model"], "baseline_multiplier_v1")
                self.assertIn("shrinkage_multiplier_v1", payload["summary_by_model_horizon"])
                self.assertIn("shrinkage_multiplier_v1", payload["comparison"])
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_shrinkage_multiplier_blends_sparse_campaign_toward_parent(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                cfg["prediction"]["horizons"] = [7]
                cfg["prediction"]["horizon_metric_map"] = {"7": "roas_d7"}
                cfg["shrinkage"] = {"prior_strength": {"campaign_country": 8, "campaign": 12, "channel_country": 18, "channel": 24, "country": 32, "platform": 40, "global": 0}}
                db = engine.connect(cfg)

                def add_week(start: dt.date, country: str, country_code: str, campaign: str, ratio: float) -> None:
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": country,
                                "country_code": country_code,
                                "partner_name": "Facebook",
                                "campaign_network": campaign,
                                "campaign_id_network": campaign,
                                "installs": 100,
                                "network_installs": 90,
                                "network_cost": 200,
                                "network_ecpi": 2.22,
                                "roas_d1": 0.1,
                                "roas_d7": 0.1 * ratio,
                            }
                        ],
                        start,
                        start + dt.timedelta(days=6),
                        "week",
                    )

                for index in range(5):
                    add_week(dt.date(2025, 1, 6) + dt.timedelta(days=index * 7), "Canada", "CA", f"broad-{index}", 2.0)
                add_week(dt.date(2025, 2, 17), "United States", "US", "target", 8.0)
                add_week(dt.date(2025, 3, 3), "United States", "US", "target", 4.0)

                rows = engine.read_training_rows(db, cfg)
                subject = [row for row in rows if row["cohort_start"] == "2025-03-03"][0]
                stats = engine.ratio_stats_shrinkage(rows, subject, 7, 1, cfg)

                self.assertEqual(stats["group"], "campaign_country")
                self.assertEqual(stats["leaf_sample_size"], 1)
                self.assertGreater(stats["ratio"], 2.0)
                self.assertLess(stats["ratio"], 8.0)
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path

    def test_backtest_artifact_falls_back_when_exclusion_config_changes(self) -> None:
        original = engine.gcs_store.download_gzip_json

        def stale_download(config: dict, relative: str) -> dict:
            return {"horizons": [7, 30], "excluded_sources": [], "rows": [], "summary_by_horizon": {}}

        try:
            engine.gcs_store.download_gzip_json = stale_download  # type: ignore[assignment]
            cfg = config(":memory:")
            cfg["storage"] = {"gcs_bucket": "test-bucket"}
            cfg["sync"]["excluded_sources"] = ["Google Ads"]
            payload = engine.load_backtest_artifact(cfg)

            self.assertIsNone(payload)
        finally:
            engine.gcs_store.download_gzip_json = original  # type: ignore[assignment]

    def test_compact_backtest_payload_limits_detail_rows_and_precomputes_segments(self) -> None:
        rows = []
        for index in range(6):
            base = {
                "cohort_start": f"2025-01-{index + 1:02d}",
                "cohort_end": f"2025-01-{index + 1:02d}",
                "granularity": "week",
                "platform": "Android",
                "country": "United States",
                "country_code": "US",
                "partner_name": "Facebook",
                "source_channel": "Facebook",
                "campaign_network": f"campaign-{index}",
                "campaign_id_network": f"campaign-{index}",
                "horizon": 30,
                "actual_roas": 1.0,
                "cost": 100 + index,
                "network_installs": 1000,
                "covered": True,
            }
            for model, predicted in (
                ("baseline_multiplier_v1", 1.5),
                ("shrinkage_multiplier_v1", 1.2),
                ("feature_multiplier_v1", 1.1),
            ):
                rows.append({**base, "model": model, "predicted_roas": predicted})
        payload = {
            "model": "comparison_v1",
            "baseline_model": "baseline_multiplier_v1",
            "models": ["baseline_multiplier_v1", "shrinkage_multiplier_v1", "feature_multiplier_v1"],
            "rows": rows,
            "retention_rows": [{"row": index} for index in range(8)],
        }

        compact = engine.compact_backtest_payload(payload, row_limit=4, retention_row_limit=3)

        self.assertTrue(compact["is_compact"])
        self.assertEqual(compact["row_count"], 18)
        self.assertEqual(len(compact["rows"]), 4)
        self.assertEqual(compact["retention_row_count"], 8)
        self.assertEqual(len(compact["retention_rows"]), 3)
        self.assertEqual(compact["segment_summaries"]["source"][0]["segment"], "Facebook")
        self.assertEqual(compact["segment_summaries"]["source"][0]["pairs"], 6)
        self.assertLess(compact["segment_summaries"]["source"][0]["featureDelta"], 0)

    def test_daily_sync_refetches_existing_recent_cohort(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        old_bucket = os.environ.pop("MMPRED_GCS_BUCKET", None)
        old_prefix = os.environ.pop("MMPRED_GCS_PREFIX", None)
        original_fetch = engine.fetch_adjust_period
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                db = engine.connect(cfg)
                day = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
                engine.upsert_rows(
                    db,
                    [
                        {
                            "app": "MMPredictions Android",
                            "country": "United States",
                            "country_code": "US",
                            "partner_name": "google",
                            "campaign_network": "launch",
                            "campaign_id_network": "main",
                            "installs": 50,
                            "network_installs": 50,
                            "network_cost": 100,
                            "network_ecpi": 2.0,
                            "roas_d1": 0.1,
                            "roas_d7": 0.2,
                            "revenue_d7": 20,
                            "revenue_d30": 40,
                        }
                    ],
                    day,
                    day,
                    "day",
                )

                def fake_fetch(config: dict, start: dt.date, end: dt.date) -> dict:
                    self.assertEqual(start, day)
                    self.assertEqual(end, day)
                    return {
                        "rows": [
                            {
                                "app": "MMPredictions Android",
                                "country": "United States",
                                "country_code": "US",
                                "partner_name": "google",
                                "campaign_network": "launch",
                                "campaign_id_network": "main",
                                "installs": 50,
                                "network_installs": 50,
                                "network_cost": 250,
                                "network_ecpi": 5.0,
                                "roas_d1": 0.12,
                                "roas_d7": 0.24,
                                "revenue_d7": 60,
                                "revenue_d30": 80,
                            }
                        ]
                    }

                engine.fetch_adjust_period = fake_fetch  # type: ignore[assignment]
                result = engine.sync_adjust(cfg, mode="daily", days=1, force=False)

                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["rows_upserted"], 1)
                cost = db.execute(
                    "select network_cost from cohort_rows where granularity='day' and cohort_start=?",
                    (day.isoformat(),),
                ).fetchone()[0]
                self.assertEqual(cost, 250)
        finally:
            engine.fetch_adjust_period = original_fetch  # type: ignore[assignment]
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path
            if old_bucket is not None:
                os.environ["MMPRED_GCS_BUCKET"] = old_bucket
            if old_prefix is not None:
                os.environ["MMPRED_GCS_PREFIX"] = old_prefix

    def test_enforce_monotonic_horizons_clamps_cumulative_curve(self) -> None:
        base = {
            "cohort_start": "2025-01-06",
            "cohort_end": "2025-01-12",
            "granularity": "week",
            "app": "MMPredictions Android",
            "platform": "Android",
            "country_code": "US",
            "partner_name": "google",
            "campaign_network": "launch",
            "campaign_id_network": "1",
            "cost": 100.0,
            "network_installs": 50.0,
            "roas_source": "predicted",
        }
        rows = [
            {**base, "horizon": 120, "predicted_roas": 1.15, "display_roas": 1.15, "low_roas": 0.6, "high_roas": 1.5},
            {**base, "horizon": 180, "predicted_roas": 1.02, "display_roas": 1.02, "low_roas": 0.38, "high_roas": 3.6},
            {**base, "horizon": 360, "predicted_roas": 1.27, "display_roas": 1.27, "low_roas": 0.25, "high_roas": 2.2},
        ]

        fixed = sorted(engine.enforce_monotonic_horizons(rows), key=lambda row: row["horizon"])

        self.assertEqual([row["display_roas"] for row in fixed], [1.15, 1.15, 1.27])
        self.assertEqual([row["predicted_ltv"] for row in fixed], [2.3, 2.3, 2.54])
        self.assertTrue(fixed[1]["monotonic_adjusted"])
        self.assertEqual(fixed[1]["raw_display_roas"], 1.02)

    def test_summary_generates_campaign_level_predictions(self) -> None:
        old_db_path = os.environ.pop("MMPRED_DB_PATH", None)
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                cfg = config(handle.name)
                db = engine.connect(cfg)
                start = dt.date(2025, 1, 6)
                countries = ["JP", "US", "AE", "IN", "DE", "FR"]
                for index in range(30):
                    country = "BR" if index == 29 else countries[index % len(countries)]
                    engine.upsert_rows(
                        db,
                        [
                            {
                                "app": "MMPredictions Android",
                                "country": country,
                                "country_code": country,
                                "partner_name": "google",
                                "campaign_network": "launch_campaign",
                                "campaign_id_network": "main",
                                "installs": 100 + index,
                                "network_installs": 100 + index,
                                "network_cost": 200 + index,
                                "network_ecpi": 2.0,
                                "roas_d1": 0.1,
                                "roas_d7": 0.25,
                                "roas_d30": 0.5,
                                "revenue_d7": 50,
                                "revenue_d30": 100,
                            }
                        ],
                        start + dt.timedelta(days=index * 7),
                        start + dt.timedelta(days=index * 7 + 6),
                        "week",
                    )

                payload = engine.summary(cfg)

                self.assertGreater(len(payload["predictions"]), 0)
                self.assertIn("campaign", {row["model_group"] for row in payload["predictions"]})

                monthly = engine.summary(cfg, {"scope": "month"})
                self.assertGreater(len(monthly["predictions"]), 0)
                self.assertIn("month", {row["granularity"] for row in monthly["predictions"]})
        finally:
            engine.close_thread_connection()
            if old_db_path is not None:
                os.environ["MMPRED_DB_PATH"] = old_db_path


if __name__ == "__main__":
    unittest.main()
