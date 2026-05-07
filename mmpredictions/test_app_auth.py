import importlib
import os
import unittest


class AppAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("MMPRED_CONFIG", "config/mmpredictions.example.json")
        cls.app = importlib.import_module("mmpredictions.app")

    def test_token_matches_correct(self) -> None:
        self.assertTrue(self.app.token_matches("secret", "secret"))

    def test_token_matches_wrong_same_length(self) -> None:
        self.assertFalse(self.app.token_matches("secRet", "secret"))

    def test_token_matches_wrong_shorter(self) -> None:
        self.assertFalse(self.app.token_matches("sec", "secret"))

    def test_token_matches_missing_expected_is_false(self) -> None:
        self.assertFalse(self.app.token_matches(None, None))

    def test_sync_auth_closed_when_sync_token_missing(self) -> None:
        self.assertFalse(self.app.token_matches(None, "sync-secret"))


if __name__ == "__main__":
    unittest.main()
