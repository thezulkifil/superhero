import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from app.main import get_new_entries, notification_time_today


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


    def test_notification_time_today_returns_correct_time(self):
        with patch('app.main.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 8, 30, tzinfo=timezone.utc)
            result = notification_time_today()
            self.assertEqual(result.hour, 7)
            self.assertEqual(result.minute, 0)
            self.assertEqual(result.second, 0)
            self.assertEqual(result.date(), datetime(2024, 1, 1, tzinfo=timezone.utc).date())

    def test_notification_time_today_before_notification_time(self):
        with patch('app.main.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)
            result = notification_time_today()
            self.assertEqual(result.hour, 7)
            self.assertEqual(result.minute, 0)


if __name__ == "__main__":
    unittest.main()
