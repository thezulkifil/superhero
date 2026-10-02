import unittest
from unittest import mock

import requests

from app.main import (
    RateLimiter,
    fetch_multiple_subreddit_feeds,
    get_new_entries,
    parse_email_list,
)


class RedditFeedBotTests(unittest.TestCase):
    def test_get_new_entries_ignores_duplicates(self):
        feed = {
            "entries": [
                {"id": "a1", "title": "First post", "link": "https://example.com/a1"},
                {"guid": "b2", "title": "Second post", "link": "https://example.com/b2"},
                {"id": "a1", "title": "First post", "link": "https://example.com/a1"},
            ]
        }

        seen = set()
        new_entries = get_new_entries(feed, seen)

        self.assertEqual(len(new_entries), 2)
        self.assertEqual(sorted(item["id"] for item in new_entries), ["a1", "b2"])
        self.assertEqual(len(get_new_entries(feed, seen)), 0)

    def test_get_new_entries_uses_title_when_missing_id(self):
        feed = {
            "entries": [{"title": "No ID here", "link": "https://example.com/xyz"}]
        }

        seen = set()
        new_entries = get_new_entries(feed, seen)

        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["title"], "No ID here")
        self.assertIn("https://example.com/xyz", seen)

    def test_parse_email_list_handles_commas_and_whitespace(self):
        result = parse_email_list("a@example.com, b@example.com ,c@example.com")
        self.assertEqual(result, ["a@example.com", "b@example.com", "c@example.com"])

    def test_parse_email_list_handles_empty_values(self):
        self.assertEqual(parse_email_list(None), [])
        self.assertEqual(parse_email_list(""), [])
        self.assertEqual(parse_email_list("  ,  "), [])


class RateLimiterTests(unittest.TestCase):
    def test_wait_turn_spaces_requests_by_gap(self):
        limiter = RateLimiter(gap=60)
        with mock.patch("app.main.time.sleep") as sleep:
            limiter.wait_turn()
            limiter.wait_turn()
        self.assertEqual(len(sleep.call_args_list), 1)
        self.assertGreater(sleep.call_args[0][0], 0)

    def test_wait_turn_does_not_sleep_first_request(self):
        limiter = RateLimiter(gap=60)
        with mock.patch("app.main.time.sleep") as sleep:
            limiter.wait_turn()
        sleep.assert_not_called()

    def test_throttle_backoff_escalates_and_caps(self):
        limiter = RateLimiter(base_backoff=60, max_backoff=600)
        delays = [limiter.record_throttled() for _ in range(6)]
        self.assertEqual(delays, [60, 120, 240, 480, 600, 600])

    def test_throttle_honors_retry_after_header(self):
        limiter = RateLimiter(base_backoff=60, max_backoff=600)
        self.assertEqual(limiter.record_throttled("300"), 300)

    def test_throttle_ignores_malformed_retry_after(self):
        limiter = RateLimiter(base_backoff=60, max_backoff=600)
        self.assertEqual(limiter.record_throttled("soon"), 60)

    def test_cooldown_persists_after_backoff_window_elapses(self):
        limiter = RateLimiter(gap=0, base_backoff=60, max_backoff=600)
        limiter.record_throttled()
        with mock.patch("app.main.time.sleep") as sleep:
            limiter.wait_turn()
        self.assertEqual(sleep.call_args[0][0], 60)

    def test_success_resets_backoff_state(self):
        limiter = RateLimiter(gap=0, base_backoff=60, max_backoff=600)
        limiter.record_throttled()
        limiter.record_throttled()
        limiter.record_success()
        self.assertEqual(limiter.consecutive_429, 0)
        self.assertEqual(limiter.penalty_until, 0.0)
        self.assertEqual(limiter.record_throttled(), 60)


class FetchFeedTests(unittest.TestCase):
    def _response(self, status_code, headers=None):
        response = requests.Response()
        response.status_code = status_code
        response.headers.update(headers or {})
        response.url = "https://www.reddit.com/r/glasses/.rss"
        response._content = b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
        return response

    def test_429_is_detected_by_status_code(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(main.SESSION, "get", return_value=self._response(429)):
            with mock.patch("app.main.time.sleep"):
                feeds = main.fetch_subreddit_feed("glasses", limiter)
        self.assertIsNone(feeds)
        self.assertEqual(limiter.consecutive_429, 1)

    def test_404_does_not_count_as_throttling(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(main.SESSION, "get", return_value=self._response(404)):
            with mock.patch("app.main.time.sleep"):
                feeds = main.fetch_subreddit_feed("glasses", limiter)
        self.assertIsNone(feeds)
        self.assertEqual(limiter.consecutive_429, 0)

    def test_successful_fetch_resets_counter(self):
        from app import main

        limiter = RateLimiter(gap=0)
        limiter.record_throttled()
        with mock.patch.object(main.SESSION, "get", return_value=self._response(200)):
            with mock.patch("app.main.time.sleep"):
                main.fetch_subreddit_feed("glasses", limiter)
        self.assertEqual(limiter.consecutive_429, 0)

    def test_throttled_subreddit_is_dropped_not_raised(self):
        from app import main

        limiter = RateLimiter(gap=0)

        def get(url, **kwargs):
            if "glasses" in url and "glassesadvice" not in url:
                return self._response(429)
            return self._response(200)

        with mock.patch.object(main.SESSION, "get", side_effect=get):
            with mock.patch("app.main.time.sleep"):
                feeds = fetch_multiple_subreddit_feeds(["glasses", "glassesadvice"], limiter)

        self.assertEqual([name for name, _ in feeds], ["glassesadvice"])


if __name__ == "__main__":
    unittest.main()
