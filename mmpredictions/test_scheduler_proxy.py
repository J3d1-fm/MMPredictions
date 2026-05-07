import io
import unittest
import urllib.error

from mmpredictions import scheduler_proxy


class SchedulerProxyTest(unittest.TestCase):
    def test_html_http_error_becomes_structured_payload(self) -> None:
        exc = urllib.error.HTTPError(
            url="https://example.test/sync",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(b"<html>bad gateway</html>"),
        )
        payload = scheduler_proxy.http_error_payload(exc)
        self.assertEqual(payload["error"], "upstream")
        self.assertEqual(payload["status_code"], 502)
        self.assertIn("bad gateway", payload["body_preview"])

    def test_scheduler_token_compare(self) -> None:
        self.assertTrue(scheduler_proxy.token_matches("token", "token"))
        self.assertFalse(scheduler_proxy.token_matches("tokEn", "token"))
        self.assertFalse(scheduler_proxy.token_matches("tok", "token"))


if __name__ == "__main__":
    unittest.main()
