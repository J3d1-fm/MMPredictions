import pathlib
import tempfile
import unittest

from mmpredictions import gcs_store


class GCSStoreTest(unittest.TestCase):
    def config(self) -> dict:
        return {"storage": {"gcs_bucket": "bucket", "gcs_prefix": "prefix"}}

    def test_restore_snapshot_fails_closed_on_gcs_unavailable(self) -> None:
        original = gcs_store.download_bytes

        def unavailable(config: dict, relative: str) -> bytes | None:
            raise gcs_store.GCSUnavailable("metadata unavailable")

        try:
            gcs_store.download_bytes = unavailable  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as tempdir:
                db_path = pathlib.Path(tempdir) / "mmpredictions.sqlite3"
                with self.assertRaises(gcs_store.GCSUnavailable):
                    gcs_store.restore_sqlite_snapshot(self.config(), db_path)
                self.assertFalse(db_path.exists())
        finally:
            gcs_store.download_bytes = original  # type: ignore[assignment]

    def test_snapshot_regression_guard_rejects_smaller_history(self) -> None:
        original = gcs_store.download_gzip_json

        def latest_manifest(config: dict, relative: str) -> dict:
            self.assertEqual(relative, "snapshots/latest.manifest.json.gz")
            return {"row_count": 1000, "weekly_count": 900}

        try:
            gcs_store.download_gzip_json = latest_manifest  # type: ignore[assignment]
            with self.assertRaises(gcs_store.GCSSnapshotRegression):
                gcs_store.validate_snapshot_regression(self.config(), {"row_count": 20, "weekly_count": 0})
        finally:
            gcs_store.download_gzip_json = original  # type: ignore[assignment]

    def test_snapshot_upload_uses_generation_precondition(self) -> None:
        original_validate = gcs_store.validate_snapshot_regression
        original_metadata = gcs_store.object_metadata
        original_upload_bytes = gcs_store.upload_bytes
        original_upload_json = gcs_store.upload_gzip_json
        calls = []

        def no_regression(config: dict, manifest: dict) -> None:
            return None

        def metadata(config: dict, relative: str) -> dict:
            self.assertEqual(relative, "snapshots/latest.sqlite3.gz")
            return {"generation": "42"}

        def record_upload(
            config: dict,
            relative: str,
            payload: bytes,
            content_type: str,
            if_generation_match: int | None = None,
        ) -> None:
            calls.append((relative, content_type, if_generation_match))

        try:
            gcs_store.validate_snapshot_regression = no_regression  # type: ignore[assignment]
            gcs_store.object_metadata = metadata  # type: ignore[assignment]
            gcs_store.upload_bytes = record_upload  # type: ignore[assignment]
            gcs_store.upload_gzip_json = lambda config, relative, payload: None  # type: ignore[assignment]
            with tempfile.NamedTemporaryFile() as handle:
                handle.write(b"sqlite bytes")
                handle.flush()
                gcs_store.upload_sqlite_snapshot(self.config(), pathlib.Path(handle.name), {"row_count": 1})

            self.assertEqual(calls, [("snapshots/latest.sqlite3.gz", "application/gzip", 42)])
        finally:
            gcs_store.validate_snapshot_regression = original_validate  # type: ignore[assignment]
            gcs_store.object_metadata = original_metadata  # type: ignore[assignment]
            gcs_store.upload_bytes = original_upload_bytes  # type: ignore[assignment]
            gcs_store.upload_gzip_json = original_upload_json  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
