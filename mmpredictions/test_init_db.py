import json
import sqlite3
import tempfile
import unittest

from mmpredictions import engine


class InitDbMigrationTest(unittest.TestCase):
    def create_legacy_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            """
            create table cohort_rows (
              cohort_start text not null,
              cohort_end text not null,
              granularity text not null,
              app text not null,
              platform text not null,
              partner_name text not null,
              campaign_network text not null,
              campaign_id_network text not null,
              installs real not null default 0,
              network_installs real not null default 0,
              network_cost real not null default 0,
              network_ecpi real not null default 0,
              roas_d1 real,
              roas_d3 real,
              roas_d7 real,
              roas_d30 real,
              roas_m3 real,
              roas_m4 real,
              roas_m6 real,
              revenue_d7 real,
              revenue_d30 real,
              raw_json text not null,
              fetched_at text not null,
              primary key (
                cohort_start,
                granularity,
                app,
                partner_name,
                campaign_network,
                campaign_id_network
              )
            )
            """
        )
        return db

    def test_old_cohort_rows_keep_data_and_gain_country_columns(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
            db = sqlite3.connect(handle.name)
            db.row_factory = sqlite3.Row
            legacy = self.create_legacy_db()
            db.executescript("\n".join(legacy.iterdump()))
            legacy.close()
            db.execute(
                """
                insert into cohort_rows (
                  cohort_start, cohort_end, granularity, app, platform,
                  partner_name, campaign_network, campaign_id_network,
                  raw_json, fetched_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-04-27",
                    "2026-05-03",
                    "week",
                    "MMPredictions Android",
                    "Android",
                    "google",
                    "jp_campaign",
                    "123",
                    json.dumps({"country": "Japan", "country_code": "JP"}),
                    "2026-05-04T00:00:00+00:00",
                ),
            )
            db.commit()

            engine.init_db(db)

            columns = {row["name"] for row in db.execute("pragma table_info(cohort_rows)")}
            self.assertIn("country", columns)
            self.assertIn("country_code", columns)
            row = db.execute("select * from cohort_rows").fetchone()
            self.assertEqual(row["country"], "Japan")
            self.assertEqual(row["country_code"], "JP")
            self.assertEqual(row["campaign_network"], "jp_campaign")

    def test_pk_rebuild_preserves_populated_and_default_countries(self) -> None:
        db = self.create_legacy_db()
        for campaign_id, raw_json in (
            ("1", {"country": "Japan", "country_code": "JP"}),
            ("2", {}),
        ):
            db.execute(
                """
                insert into cohort_rows (
                  cohort_start, cohort_end, granularity, app, platform,
                  partner_name, campaign_network, campaign_id_network,
                  raw_json, fetched_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-04-27",
                    "2026-05-03",
                    "week",
                    "MMPredictions Android",
                    "Android",
                    "google",
                    "campaign",
                    campaign_id,
                    json.dumps(raw_json),
                    "2026-05-04T00:00:00+00:00",
                ),
            )
        db.commit()

        engine.init_db(db)

        rows = {
            row["campaign_id_network"]: row
            for row in db.execute("select campaign_id_network, country, country_code from cohort_rows")
        }
        self.assertEqual(rows["1"]["country"], "Japan")
        self.assertEqual(rows["1"]["country_code"], "JP")
        self.assertEqual(rows["2"]["country"], "All countries")
        self.assertEqual(rows["2"]["country_code"], "ZZ")


if __name__ == "__main__":
    unittest.main()
