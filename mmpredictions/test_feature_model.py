import datetime as dt
import sqlite3
import unittest

from mmpredictions import engine


class FeatureModelTest(unittest.TestCase):
    def test_feature_multiplier_uses_similar_early_event_cohorts(self) -> None:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        engine.init_db(db)
        cfg = {
            "timezone": "UTC",
            "sync": {"minimum_training_samples": 5, "minimum_anchor_roas": 0.0},
            "prediction": {
                "anchors": [0, 1, 3, 7],
                "horizons": [30],
                "horizon_metric_map": {"30": "roas_d30"},
                "metric_labels": {},
            },
            "feature_model": {
                "base_model": "feature_multiplier_v1",
                "decision_days": [1, 3, 7],
                "min_features": 2,
                "min_feature_samples": 5,
                "blend_strength": 0.8,
            },
        }
        start = dt.date(2026, 1, 5)
        rows = []
        for index in range(6):
            high_feature = index < 3
            rows.append(
                {
                    "app": "Sample Android App",
                    "country": "United States",
                    "country_code": "US",
                    "partner_name": "Paid Network",
                    "campaign_network": "Launch",
                    "campaign_id_network": f"c{index}",
                    "network_installs": 100,
                    "network_cost": 100,
                    "roas_d7": 0.1,
                    "roas_d30": 0.4 if high_feature else 0.2,
                    "revenue_events_total_d7": 120 if high_feature else 10,
                    "sessions_d7": 800 if high_feature else 80,
                }
            )
        subject = {
            "app": "Sample Android App",
            "country": "United States",
            "country_code": "US",
            "partner_name": "Paid Network",
            "campaign_network": "Launch",
            "campaign_id_network": "subject",
            "network_installs": 100,
            "network_cost": 100,
            "roas_d7": 0.1,
            "roas_d30": 0.4,
            "revenue_events_total_d7": 115,
            "sessions_d7": 820,
        }
        for index, row in enumerate([*rows, subject]):
            engine.upsert_rows(db, [row], start + dt.timedelta(days=index * 7), start + dt.timedelta(days=index * 7 + 6), "week")
        sqlite_rows = db.execute("select * from cohort_rows order by cohort_start").fetchall()
        subject_row = sqlite_rows[-1]
        candidates = sqlite_rows[:-1]

        self.assertEqual(engine.feature_decision_anchor(subject_row, 30, 45, cfg), (7, 0.1))
        baseline = engine.ratio_stats(candidates, subject_row, 30, 7, cfg)
        featured = engine.ratio_stats_feature(candidates, subject_row, 30, 7, cfg)

        self.assertGreater(featured["ratio"], baseline["ratio"])
        self.assertGreater(featured["feature_sample_size"], 0)
        self.assertGreater(featured["feature_blend"], 0)

    def test_same_horizon_roas_is_not_a_prediction_anchor(self) -> None:
        row = {"roas_d0": 0.01, "roas_d1": 0.02, "roas_d3": 0.03, "roas_d7": 0.07}
        cfg = {
            "sync": {"minimum_anchor_roas": 0.0},
            "prediction": {"anchors": [7]},
        }
        self.assertIsNone(engine.best_anchor(row, 7, 30, cfg))

    def test_same_horizon_retention_is_not_a_prediction_anchor(self) -> None:
        row = {"retention_d1": 0.2, "retention_d7": 0.1}
        cfg = {
            "retention": {
                "anchors": [7],
                "minimum_anchor_retention": 0.0,
            },
        }
        self.assertIsNone(engine.best_retention_anchor(row, 7, 30, cfg))


if __name__ == "__main__":
    unittest.main()
