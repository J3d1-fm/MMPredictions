import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from mmpredictions import app, engine


PACKAGE_DIR = Path(__file__).resolve().parent


class HandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.projects_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("MMPRED_DB_PATH")
        self.old_projects_path = os.environ.get("MMPRED_PROJECTS_PATH")
        os.environ["MMPRED_DB_PATH"] = self.tmp.name
        os.environ["MMPRED_PROJECTS_PATH"] = os.path.join(self.projects_dir.name, "projects.json")
        engine.close_thread_connection()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        engine.close_thread_connection()
        if self.old_db_path is None:
            os.environ.pop("MMPRED_DB_PATH", None)
        else:
            os.environ["MMPRED_DB_PATH"] = self.old_db_path
        if self.old_projects_path is None:
            os.environ.pop("MMPRED_PROJECTS_PATH", None)
        else:
            os.environ["MMPRED_PROJECTS_PATH"] = self.old_projects_path
        self.tmp.close()
        self.projects_dir.cleanup()

    def get_json(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_healthz_no_auth(self) -> None:
        status, payload = self.get_json("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    def test_status_shape(self) -> None:
        status, payload = self.get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {"project", "sync_in_progress", "sync_started_at", "latest_sync", "row_count"},
        )
        self.assertEqual(payload["project"]["id"], "default")

    def test_sync_bad_mode_returns_400(self) -> None:
        old_token = app.SYNC_TOKEN
        app.SYNC_TOKEN = "sync-secret"
        try:
            request = urllib.request.Request(
                f"{self.base_url}/api/sync?mode=garbage",
                headers={"X-Sync-Token": "sync-secret"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            app.SYNC_TOKEN = old_token

    def test_projects_list_includes_default_project(self) -> None:
        status, payload = self.get_json("/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(payload["active_project_id"], "default")
        self.assertEqual(payload["projects"][0]["id"], "default")
        self.assertIn("mmp_api_token_env", payload["projects"][0])

    def test_frontend_has_session_summary_cache(self) -> None:
        script = (PACKAGE_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")
        html = (PACKAGE_DIR / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("const summaryMemoryCache = new Map();", script)
        self.assertIn('const summarySessionCacheDbName = "mmpredictions-session-cache-v1";', script)
        self.assertIn("async function readCachedSummary(cacheKey)", script)
        self.assertIn("writeSummarySessionCache(cacheKey, payload);", script)
        self.assertIn('cached.cacheSource === "session" ? "session cached" : "cached"', script)
        self.assertIn("20260520-session-cache", html)
        self.assertIn(">v0.8</span>", html)

    def test_admin_can_save_project_connector(self) -> None:
        old_admins = app.ADMIN_EMAILS
        old_stored = app.stored_admin_emails
        try:
            app.ADMIN_EMAILS = {"admin@example.com"}
            app.stored_admin_emails = lambda: set()  # type: ignore[assignment]
            request = urllib.request.Request(
                f"{self.base_url}/api/projects",
                data=json.dumps(
                    {
                        "id": "test-app",
                        "name": "Test App",
                        "mmp_provider": "adjust",
                        "mmp_api_token_env": "TEST_ADJUST_TOKEN",
                        "app_tokens": ["android", "ios"],
                        "google_ads_enabled": True,
                        "google_ads_customer_ids": ["1234567890"],
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Goog-Authenticated-User-Email": "accounts.google.com:admin@example.com"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            by_id = {project["id"]: project for project in payload["projects"]}
            self.assertEqual(by_id["test-app"]["app_token_count"], 2)
            self.assertTrue(by_id["test-app"]["google_ads_configured"])
        finally:
            app.ADMIN_EMAILS = old_admins
            app.stored_admin_emails = old_stored  # type: ignore[assignment]

    def test_access_state_marks_bootstrap_admin(self) -> None:
        old_admins = app.ADMIN_EMAILS
        old_policy = app.get_iap_policy
        old_stored = app.stored_admin_emails
        try:
            app.ADMIN_EMAILS = {"admin@example.com"}
            app.stored_admin_emails = lambda: set()  # type: ignore[assignment]
            app.get_iap_policy = lambda: {  # type: ignore[assignment]
                "bindings": [
                    {
                        "role": app.IAP_ACCESS_ROLE,
                        "members": ["user:admin@example.com", "user:viewer@example.com"],
                    }
                ]
            }

            request = urllib.request.Request(
                f"{self.base_url}/api/access",
                headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:admin@example.com"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            self.assertEqual(response.status, 200)
            self.assertTrue(payload["is_admin"])
            self.assertEqual(payload["role"], "admin")
            self.assertEqual(
                payload["users"],
                [
                    {"email": "admin@example.com", "role": "admin"},
                    {"email": "viewer@example.com", "role": "user"},
                ],
            )
        finally:
            app.ADMIN_EMAILS = old_admins
            app.get_iap_policy = old_policy  # type: ignore[assignment]
            app.stored_admin_emails = old_stored  # type: ignore[assignment]

    def test_access_add_requires_admin(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/access/users",
            data=json.dumps({"email": "new@example.com", "role": "user"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Goog-Authenticated-User-Email": "accounts.google.com:viewer@example.com"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_add_access_user_updates_iap_policy_and_admin_store(self) -> None:
        old_admins = app.ADMIN_EMAILS
        old_get = app.get_iap_policy
        old_set = app.set_iap_policy
        old_stored = app.stored_admin_emails
        old_write = app.write_stored_admin_emails
        captured_policy = {}
        captured_admins = set()
        try:
            app.ADMIN_EMAILS = {"admin@example.com"}
            app.stored_admin_emails = lambda: set()  # type: ignore[assignment]
            app.get_iap_policy = lambda: {"bindings": []}  # type: ignore[assignment]

            def fake_set(policy: dict) -> dict:
                captured_policy.update(policy)
                return policy

            def fake_write(admins: set[str]) -> None:
                captured_admins.update(admins)

            app.set_iap_policy = fake_set  # type: ignore[assignment]
            app.write_stored_admin_emails = fake_write  # type: ignore[assignment]

            result = app.add_access_user("owner@example.com", "admin", "admin@example.com")

            self.assertIn(
                {"role": app.IAP_ACCESS_ROLE, "members": ["user:owner@example.com"]},
                captured_policy["bindings"],
            )
            self.assertIn("owner@example.com", captured_admins)
            self.assertTrue(result["is_admin"])
        finally:
            app.ADMIN_EMAILS = old_admins
            app.get_iap_policy = old_get  # type: ignore[assignment]
            app.set_iap_policy = old_set  # type: ignore[assignment]
            app.stored_admin_emails = old_stored  # type: ignore[assignment]
            app.write_stored_admin_emails = old_write  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
