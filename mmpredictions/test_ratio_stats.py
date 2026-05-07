import datetime as dt
import sqlite3
import unittest

from mmpredictions import engine


CONFIG = {
    "timezone": "UTC",
    "sync": {"minimum_anchor_roas": 0.0},
    "prediction": {"horizon_metric_map": {"7": "roas_d7"}},
}


def make_rows(countries: list[str]) -> tuple[list[sqlite3.Row], sqlite3.Row]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        create table rows (
          cohort_start text, cohort_end text, granularity text, app text,
          platform text, country_code text, partner_name text,
          campaign_network text, campaign_id_network text,
          roas_d1 real, roas_d7 real
        )
        """
    )
    base = dt.date(2025, 1, 6)
    for index, country in enumerate(countries):
        start = base + dt.timedelta(days=index * 7)
        db.execute(
            "insert into rows values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                start.isoformat(),
                (start + dt.timedelta(days=6)).isoformat(),
                "week",
                "MMPredictions Android",
                "Android",
                country,
                "google",
                "launch_campaign",
                str(index),
                0.1,
                0.3,
            ),
        )
    subject_start = base + dt.timedelta(days=400)
    db.execute(
        "insert into rows values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_start.isoformat(),
            (subject_start + dt.timedelta(days=6)).isoformat(),
            "week",
            "MMPredictions Android",
            "Android",
            "BR",
            "google",
            "launch_campaign",
            "subject",
            0.1,
            0.0,
        ),
    )
    rows = list(db.execute("select * from rows where campaign_id_network != 'subject'"))
    subject = db.execute("select * from rows where campaign_id_network = 'subject'").fetchone()
    db.close()
    return rows, subject


class RatioStatsTest(unittest.TestCase):
    def test_campaign_country_group_when_enough_candidates(self) -> None:
        rows, subject = make_rows(["BR", "BR", "BR", "BR", "BR", "JP"])
        stats = engine.ratio_stats(rows, subject, 7, 1, CONFIG)
        self.assertEqual(stats["group"], "campaign_country")

    def test_campaign_group_fallback_when_country_is_sparse(self) -> None:
        rows, subject = make_rows(["JP", "US", "AE", "IN", "DE", "FR"])
        stats = engine.ratio_stats(rows, subject, 7, 1, CONFIG)
        self.assertEqual(stats["group"], "campaign")


if __name__ == "__main__":
    unittest.main()
