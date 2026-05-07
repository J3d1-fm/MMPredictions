import sqlite3
import unittest

from mmpredictions import engine


def row(network_installs: float, network_cost: float) -> sqlite3.Row:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("create table sample(network_installs real, network_cost real)")
    db.execute("insert into sample values (?, ?)", (network_installs, network_cost))
    result = db.execute("select * from sample").fetchone()
    db.close()
    return result


def config() -> dict:
    return {
        "sync": {
            "minimum_network_installs": 10,
            "minimum_predicted_revenue": 20,
            "minimum_training_samples": 5,
        },
        "confidence": engine.CONFIDENCE_DEFAULTS,
    }


class ConfidenceScoreTest(unittest.TestCase):
    def test_all_good_is_high(self) -> None:
        result = engine.confidence_score(
            row(1000, 1000),
            predicted=0.5,
            low=0.45,
            high=0.55,
            stats={"sample_size": 100, "mape": 0.05},
            config=config(),
        )
        self.assertEqual(result["level"], "high")

    def test_all_bad_is_low(self) -> None:
        result = engine.confidence_score(
            row(1, 1),
            predicted=0.1,
            low=0.0,
            high=1.0,
            stats={"sample_size": 1, "mape": 0.95},
            config=config(),
        )
        self.assertEqual(result["level"], "low")

    def test_zero_prediction_has_very_wide_interval(self) -> None:
        result = engine.confidence_score(
            row(100, 100),
            predicted=0.0,
            low=0.0,
            high=0.0,
            stats={"sample_size": 10, "mape": 0.1},
            config=config(),
        )
        self.assertEqual(result["interval_width"], 9.99)
        self.assertLess(result["components"]["interval_width"], 0.01)

    def test_bad_weights_raise(self) -> None:
        bad = config()
        bad["confidence"] = {
            "weights": {
                "network_installs": 1.0,
                "predicted_revenue": 1.0,
                "training_samples": 0.0,
                "historical_error": 0.0,
                "interval_width": 0.0,
            }
        }
        with self.assertRaises(ValueError):
            engine.validate_config(bad)


if __name__ == "__main__":
    unittest.main()
