import unittest
from app.main import get_new_entries, parse_email_list


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


if __name__ == "__main__":
    unittest.main()
