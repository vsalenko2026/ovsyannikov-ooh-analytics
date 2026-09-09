import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.fake_api import FakeResponse, FakeSession, weekly_series
from ws.api import WordstatClient, WordstatError, WordstatHTTPError, dynamics_rows, validate_phrase


def client(session, **kwargs):
    return WordstatClient(
        api_key="test", folder_id="folder", session=session,
        pause_seconds=0, backoff_base_seconds=0, sleep=lambda _s: None, **kwargs
    )


class TestPhrase(unittest.TestCase):
    def test_operators_rejected(self):
        for phrase in ('"овсянников мыло"', "!овсянников", "[овсянников мыло]", "мыло|крем"):
            with self.assertRaises(ValueError):
                validate_phrase(phrase)

    def test_plus_allowed(self):
        validate_phrase("овсянников +мыло")


class TestRetries(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        session = FakeSession([
            FakeResponse(429, text="too many"),
            FakeResponse(payload={"results": []}),
        ])
        api = client(session)
        api.get_dynamics("овсянников мыло", "2026-03-30T00:00:00Z", "2026-09-06T23:59:59Z",
                         "PERIOD_WEEKLY", regions=["1"], devices=["DEVICE_ALL"])
        self.assertEqual(api.stats["http_calls"], 2)
        self.assertEqual(api.stats["retries"], 1)
        # 429 не тарифицируется — платный только успешный вызов
        self.assertEqual(api.stats["billable_calls"], 1)
        self.assertAlmostEqual(api.cost_rub, 0.02)

    def test_gives_up_after_max_attempts(self):
        session = FakeSession([FakeResponse(503, text="unavailable") for _ in range(3)])
        api = client(session, max_attempts=3)
        with self.assertRaises(WordstatError):
            api.get_regions_tree()
        self.assertEqual(api.stats["http_calls"], 3)
        # 5xx не тарифицируется
        self.assertEqual(api.stats["billable_calls"], 0)

    def test_client_error_is_not_retried(self):
        session = FakeSession([FakeResponse(400, text="InvalidArgument")])
        api = client(session)
        with self.assertRaises(WordstatHTTPError):
            api.get_regions_tree()
        self.assertEqual(api.stats["http_calls"], 1)

    def test_folder_and_auth_are_sent(self):
        session = FakeSession(generator=weekly_series)
        api = client(session)
        api.get_dynamics("овсянников мыло", "2026-08-31T00:00:00Z", "2026-09-06T23:59:59Z",
                         "PERIOD_WEEKLY", regions=["11162"], devices=["DEVICE_ALL"])
        sent = session.requests[0]
        self.assertTrue(sent["url"].endswith("/v2/wordstat/dynamics"))
        self.assertEqual(sent["headers"]["Authorization"], "Api-Key test")
        self.assertEqual(sent["body"]["folderId"], "folder")
        self.assertEqual(sent["body"]["regions"], ["11162"])


class TestParsing(unittest.TestCase):
    def test_count_comes_as_string(self):
        rows = dynamics_rows({"results": [{"date": "2026-08-31T00:00:00Z", "count": "42", "share": 0.1}]})
        self.assertEqual(rows[0]["count"], 42)
        self.assertIsInstance(rows[0]["count"], int)


if __name__ == "__main__":
    unittest.main()
