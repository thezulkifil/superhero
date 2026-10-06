import json
import os
import tempfile
import time
import unittest
from unittest import mock

import requests

from app.main import (
    REASONING_EFFORT,
    RELEVANCE_MAX_TOKENS,
    RELEVANCE_RESPONSE_FORMATS,
    RateLimiter,
    build_email_html,
    build_email_subject,
    build_relevance_payload,
    fetch_due_subreddit_feeds,
    filter_relevant_posts,
    get_new_entries,
    matches_keywords,
    parse_email_list,
    parse_relevant_ids,
    strip_html,
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

        seen = {}
        new_entries = get_new_entries(feed, seen)

        self.assertEqual(len(new_entries), 2)
        self.assertEqual(sorted(item["id"] for item in new_entries), ["a1", "b2"])
        self.assertEqual(len(get_new_entries(feed, seen)), 0)

    def test_get_new_entries_uses_title_when_missing_id(self):
        feed = {
            "entries": [{"title": "No ID here", "link": "https://example.com/xyz"}]
        }

        seen = {}
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
        # Real time elapses between arming and reading the cooldown, so the
        # remaining wait is a hair under the full 60s rather than exactly 60.
        self.assertAlmostEqual(sleep.call_args[0][0], 60, delta=1)

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

    def test_connection_error_does_not_crash_the_bot(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(
            main.SESSION, "get", side_effect=requests.ConnectionError("Remote end closed")
        ) as get:
            with mock.patch("app.main.time.sleep"):
                feeds = main.fetch_subreddit_feed("glasses", limiter)

        self.assertIsNone(feeds)
        self.assertEqual(get.call_count, len(main.FETCH_RETRY_DELAYS) + 1)
        self.assertEqual(limiter.consecutive_429, 0)

    def test_read_timeout_does_not_crash_the_bot(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(main.SESSION, "get", side_effect=requests.Timeout("timed out")):
            with mock.patch("app.main.time.sleep"):
                self.assertIsNone(main.fetch_subreddit_feed("glasses", limiter))

    def test_transient_failure_then_success_is_recovered(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(
            main.SESSION,
            "get",
            side_effect=[requests.ConnectionError("reset"), self._response(200)],
        ):
            with mock.patch("app.main.time.sleep"):
                feeds = main.fetch_subreddit_feed("glasses", limiter)

        self.assertIsNotNone(feeds)
        self.assertEqual(limiter.consecutive_429, 0)

    def test_404_is_not_retried(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(main.SESSION, "get", return_value=self._response(404)) as get:
            with mock.patch("app.main.time.sleep"):
                self.assertIsNone(main.fetch_subreddit_feed("glasses", limiter))

        self.assertEqual(get.call_count, 1)

    def test_429_is_not_retried_here(self):
        from app import main

        limiter = RateLimiter(gap=0)
        with mock.patch.object(main.SESSION, "get", return_value=self._response(429)) as get:
            with mock.patch("app.main.time.sleep"):
                self.assertIsNone(main.fetch_subreddit_feed("glasses", limiter))

        self.assertEqual(get.call_count, 1)
        self.assertEqual(limiter.consecutive_429, 1)

    def test_throttled_subreddit_is_dropped_not_raised(self):
        from app import main

        limiter = RateLimiter(gap=0)

        def get(url, **kwargs):
            if "glasses" in url and "glassesadvice" not in url:
                return self._response(429)
            return self._response(200)

        with mock.patch.object(main.SESSION, "get", side_effect=get):
            with mock.patch("app.main.time.sleep"):
                feeds = fetch_due_subreddit_feeds(
                    ["glasses", "glassesadvice"], limiter, {}
                )

        self.assertEqual([name for name, _ in feeds], ["glassesadvice"])


class RelevanceFilterTests(unittest.TestCase):
    def _posts(self, *ids):
        return [
            {"id": post_id, "title": f"Post {post_id}", "link": f"https://example.com/{post_id}", "summary": ""}
            for post_id in ids
        ]

    def test_strip_html_flattens_markup(self):
        raw = "<div>Great <b>deals</b> here</div>  <a href='/x'>link</a>"
        self.assertEqual(strip_html(raw), "Great deals here link")

    def test_strip_html_handles_empty_input(self):
        self.assertEqual(strip_html(""), "")

    def test_parse_relevant_ids_reads_ids(self):
        content = '{"relevant_ids": ["a1", "b2"]}'
        self.assertEqual(parse_relevant_ids(content, {"a1", "b2", "c3"}), {"a1", "b2"})

    def test_parse_relevant_ids_drops_hallucinated_ids(self):
        content = '{"relevant_ids": ["a1", "made-up-id"]}'
        self.assertEqual(parse_relevant_ids(content, {"a1", "b2"}), {"a1"})

    def test_parse_relevant_ids_tolerates_reasoning_around_json(self):
        content = "Let me check each post...\n```json\n{\"relevant_ids\": [\"b2\"]}\n```"
        self.assertEqual(parse_relevant_ids(content, {"a1", "b2"}), {"b2"})

    def test_parse_relevant_ids_returns_empty_on_malformed_json(self):
        self.assertEqual(parse_relevant_ids("{not json", {"a1"}), set())

    def test_parse_relevant_ids_returns_empty_when_missing_json(self):
        self.assertEqual(parse_relevant_ids("I refuse to answer", {"a1"}), set())

    def test_parse_relevant_ids_ignores_reasoning_braces_before_answer(self):
        content = (
            'Draft: {"relevant_ids": ["a1"]} as a placeholder.\n'
            'Final answer: {"relevant_ids": ["b2"]}'
        )
        self.assertEqual(parse_relevant_ids(content, {"a1", "b2"}), {"b2"})

    def test_parse_relevant_ids_ignores_non_decodable_brace_prefix(self):
        content = "{relevant_ids: nope}\n{\"relevant_ids\": [\"b2\"]}"
        self.assertEqual(parse_relevant_ids(content, {"a1", "b2"}), {"b2"})

    def test_parse_relevant_ids_returns_empty_on_wrong_type(self):
        self.assertEqual(parse_relevant_ids('{"relevant_ids": "a1"}', {"a1"}), set())

    def test_parse_relevant_ids_returns_empty_when_absent(self):
        self.assertEqual(parse_relevant_ids('{"other": 1}', {"a1"}), set())

    def test_parse_relevant_ids_returns_empty_on_blank_content(self):
        self.assertEqual(parse_relevant_ids("", {"a1"}), set())

    def test_payload_carries_ids_and_schema(self):
        payload = build_relevance_payload(self._posts("a1", "b2"), "openai/gpt-oss-20b", RELEVANCE_RESPONSE_FORMATS[0])
        self.assertEqual(payload["model"], "openai/gpt-oss-20b")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertIn('"a1"', payload["messages"][1]["content"])
        self.assertIn('"b2"', payload["messages"][1]["content"])

    def test_payload_omits_response_format_when_none(self):
        payload = build_relevance_payload(self._posts("a1"), "m", None)
        self.assertNotIn("response_format", payload)

    def test_payload_sends_title_and_short_snippet_only(self):
        from app.main import Post

        post: Post = {
            "id": "a1",
            "title": "Cheap glasses thread",
            "link": "https://example.com/a1",
            "summary": "body " * 100,
            "relevant": False,
        }
        payload = build_relevance_payload([post], "m", None)
        content = payload["messages"][1]["content"]

        self.assertIn("Cheap glasses thread", content)
        self.assertIn("summary", content)
        self.assertNotIn("link", content)

    def test_payload_snippet_is_capped(self):
        from app.main import SUMMARY_SNIPPET_CHARS

        posts = [
            {
                "id": "a1",
                "title": "t",
                "link": "l",
                "summary": "x" * 5000,
                "relevant": False,
            }
        ]
        content = build_relevance_payload(posts, "m", None)["messages"][1]["content"]
        self.assertLessEqual(content.count("x"), SUMMARY_SNIPPET_CHARS)

    def test_prompt_notes_it_only_sees_titles(self):
        from app.main import RELEVANCE_SYSTEM_PROMPT

        self.assertIn("short snippet", RELEVANCE_SYSTEM_PROMPT)

    def test_payload_caps_generated_tokens(self):
        payload = build_relevance_payload(self._posts("a1"), "m", None)
        self.assertEqual(payload["max_tokens"], RELEVANCE_MAX_TOKENS)
        self.assertEqual(payload["reasoning_effort"], REASONING_EFFORT)

    def test_default_model_is_on_the_groq_free_plan(self):
        from app.main import GROQ_URL, RELEVANCE_MODEL

        self.assertEqual(RELEVANCE_MODEL, "openai/gpt-oss-20b")
        self.assertTrue(GROQ_URL.startswith("https://api.groq.com/"))

    def test_filter_keeps_only_relevant_posts(self):
        with mock.patch("app.main.request_relevant_ids", return_value={"a1"}):
            relevant = filter_relevant_posts(self._posts("a1", "b2"))
        self.assertEqual(relevant, {"a1"})

    def test_filter_batches_large_sets_and_unions_results(self):
        posts = self._posts("a1", "a2", "a3", "a4", "a5")

        def fake_request(batch, model):
            return {batch[0]["id"]}

        with mock.patch("app.main.RELEVANCE_BATCH_SIZE", 2):
            with mock.patch("app.main.request_relevant_ids", side_effect=fake_request) as call:
                relevant = filter_relevant_posts(posts)

        self.assertEqual(call.call_count, 3)
        self.assertEqual(relevant, {"a1", "a3", "a5"})

    def test_filter_fails_closed_when_request_fails(self):
        with mock.patch("app.main.request_relevant_ids", return_value=None):
            relevant = filter_relevant_posts(self._posts("a1", "b2"))
        self.assertEqual(relevant, set())

    def test_filter_fails_closed_without_api_key(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": ""}, clear=False):
            from app import main

            self.assertIsNone(main.request_relevant_ids(self._posts("a1"), "no-key-model"))

    def _mock_response(self, content, status_code=200):
        response = mock.Mock()
        response.status_code = status_code
        response.text = content if isinstance(content, str) else ""
        if status_code >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(
                f"{status_code} error", response=response
            )
        else:
            response.raise_for_status.return_value = None
            response.json.return_value = {"choices": [{"message": {"content": content}}]}
        return response

    def _with_key(self):
        return mock.patch.dict("os.environ", {"GROQ_API_KEY": "test-key"})

    def test_request_parses_successful_response(self):
        from app import main

        response = self._mock_response('{"relevant_ids": ["a1"]}')
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", return_value=response) as post:
                result = main.request_relevant_ids(self._posts("a1"), "strict-model")

        self.assertEqual(result, {"a1"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "strict-model")

    def test_request_falls_back_when_schema_is_rejected(self):
        from app import main

        rejected = self._mock_response('{"error":{"message":"response_format not supported"}}', 400)
        accepted = self._mock_response('{"relevant_ids": ["a1"]}')
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", side_effect=[rejected, accepted]) as post:
                result = main.request_relevant_ids(self._posts("a1"), "loose-model")

        self.assertEqual(result, {"a1"})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["json"]["response_format"]["type"], "json_object")

    def test_request_remembers_accepted_format(self):
        from app import main

        main._FORMAT_INDEX_BY_MODEL.pop("sticky-model", None)
        rejected = self._mock_response('{"error":{"message":"json schema unsupported"}}', 400)
        accepted = self._mock_response('{"relevant_ids": []}')
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", side_effect=[rejected, accepted]):
                main.request_relevant_ids(self._posts("a1"), "sticky-model")
            with mock.patch("app.main.API_SESSION.post", return_value=accepted) as post:
                main.request_relevant_ids(self._posts("a1"), "sticky-model")

        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["response_format"]["type"], "json_object")

    def test_request_gives_up_when_every_format_is_rejected(self):
        from app import main

        rejected = self._mock_response('{"error":{"message":"response_format not supported"}}', 400)
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", return_value=rejected) as post:
                result = main.request_relevant_ids(self._posts("a1"), "hopeless-model")

        self.assertIsNone(result)
        self.assertEqual(post.call_count, len(RELEVANCE_RESPONSE_FORMATS))

    def test_reasoning_effort_rejection_triggers_fallback(self):
        from app import main

        rejected = self._mock_response(
            '{"error":{"message":"Unsupported parameter: reasoning_effort"}}', 400
        )
        self.assertTrue(main._is_shape_rejection(
            requests.HTTPError("400", response=rejected)
        ))

    def test_unrelated_400_does_not_trigger_fallback(self):
        from app import main

        rejected = self._mock_response('{"error":{"message":"invalid model name"}}', 400)
        self.assertFalse(main._is_shape_rejection(
            requests.HTTPError("400", response=rejected)
        ))

    def test_non_400_never_triggers_fallback(self):
        from app import main

        rejected = self._mock_response("rate limited", 429)
        self.assertFalse(main._is_shape_rejection(
            requests.HTTPError("429", response=rejected)
        ))

    def test_request_returns_none_on_http_error(self):
        from app import main

        response = self._mock_response("rate limited", 429)
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", return_value=response):
                with mock.patch("app.main.time.sleep"):
                    result = main.request_relevant_ids(self._posts("a1"), "err-model")

        self.assertIsNone(result)

    def test_request_retries_after_rate_limit(self):
        from app import main

        throttled = self._mock_response("rate limited", 429)
        accepted = self._mock_response('{"relevant_ids": ["a1"]}')
        with self._with_key():
            with mock.patch(
                "app.main.API_SESSION.post", side_effect=[throttled, accepted]
            ) as post:
                with mock.patch("app.main.time.sleep") as sleep:
                    result = main.request_relevant_ids(self._posts("a1"), "retry-model")

        self.assertEqual(result, {"a1"})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_args_list[0][0][0], main.RELEVANCE_RETRY_DELAYS[0])

    def test_request_gives_up_after_retry_budget(self):
        from app import main

        throttled = self._mock_response("rate limited", 429)
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", return_value=throttled) as post:
                with mock.patch("app.main.time.sleep") as sleep:
                    result = main.request_relevant_ids(self._posts("a1"), "doomed-model")

        self.assertIsNone(result)
        self.assertEqual(post.call_count, len(main.RELEVANCE_RETRY_DELAYS) + 1)
        self.assertEqual(len(sleep.call_args_list), len(main.RELEVANCE_RETRY_DELAYS))

    def test_request_returns_none_on_malformed_body(self):
        from app import main

        response = self._mock_response('{"error": "nope"}')
        response.json.return_value = {"error": "nope"}
        with self._with_key():
            with mock.patch("app.main.API_SESSION.post", return_value=response):
                result = main.request_relevant_ids(self._posts("a1"), "bad-model")

        self.assertIsNone(result)

    def test_rejected_posts_are_marked_seen_and_never_rebilled(self):
        feed = {
            "entries": [
                {"id": "a1", "title": "Cheap laptop deals", "link": "https://example.com/a1"}
            ]
        }

        seen = {}
        candidates = get_new_entries(feed, seen)
        with mock.patch("app.main.request_relevant_ids", return_value=set()):
            relevant = filter_relevant_posts(candidates)

        self.assertEqual(relevant, set())
        self.assertIn("a1", seen)
        self.assertEqual(get_new_entries(feed, seen), [])

    def test_summary_is_carried_for_filtering(self):
        feed = {
            "entries": [
                {
                    "id": "a1",
                    "title": "Budget frames",
                    "link": "https://example.com/a1",
                    "summary": "<div>Where I found <b>cheap</b> frames</div>",
                }
            ]
        }
        new_entries = get_new_entries(feed, {})
        self.assertEqual(new_entries[0]["summary"], "Where I found cheap frames")

    def test_eyeglasses_subreddit_is_not_monitored(self):
        from app.main import SUBREDDITS

        self.assertNotIn("eyeglasses", SUBREDDITS)
        self.assertIn("glasses", SUBREDDITS)


class EmailDigestLayoutTests(unittest.TestCase):
    def _post(self, post_id, relevant, title=None):
        return {
            "id": post_id,
            "title": title or f"Post {post_id}",
            "link": f"https://example.com/{post_id}",
            "summary": "",
            "relevant": relevant,
        }

    def test_relevant_posts_are_listed_first(self):
        body = build_email_html([self._post("a1", True), self._post("b2", False)])
        self.assertLess(body.index("Post a1"), body.index("Post b2"))

    def test_filtered_posts_are_still_present(self):
        body = build_email_html([self._post("a1", True), self._post("b2", False)])
        self.assertIn("Post a1", body)
        self.assertIn("Post b2", body)

    def test_no_interactive_controls_are_emitted(self):
        # Gmail strips form inputs and does not support :checked on any platform,
        # so a checkbox toggle could never work there. Keep the markup client-safe.
        body = build_email_html(
            [self._post("a1", True), self._post("b2", False), self._post("c3", False)]
        )
        self.assertNotIn("<input", body)
        self.assertNotIn("<label", body)
        self.assertNotIn(":checked", body)
        self.assertNotIn("<style", body)

    def test_filtered_section_reports_the_count(self):
        body = build_email_html(
            [self._post("a1", True), self._post("b2", False), self._post("c3", False)]
        )
        self.assertIn("All 3 posts", body)
        self.assertIn("including the 2 the filter passed over", body)

    def test_filtered_posts_are_visually_secondary(self):
        body = build_email_html([self._post("a1", True), self._post("b2", False)])
        self.assertIn("color:#666666", body.split("Post b2")[0])

    def test_no_secondary_section_when_nothing_was_filtered(self):
        body = build_email_html([self._post("a1", True), self._post("b2", True)])
        self.assertNotIn("passed over", body)
        self.assertNotIn("<hr", body)
        self.assertIn("Post a1", body)
        self.assertIn("Post b2", body)

    def test_intro_reports_relevant_and_total(self):
        body = build_email_html([self._post("a1", True), self._post("b2", False)])
        self.assertIn("<strong>1</strong> relevant post out of 2 seen", body)

    def test_titles_are_escaped(self):
        body = build_email_html([self._post("a1", True, title="Fish & <chips>")])
        self.assertIn("Fish &amp; &lt;chips&gt;", body)
        self.assertNotIn("<chips>", body)

    def test_empty_collection_is_handled(self):
        body = build_email_html([])
        self.assertIn("No relevant posts found.", body)

    def test_subject_counts_only_relevant_posts(self):
        posts = [self._post("a1", True), self._post("b2", False), self._post("c3", True)]
        self.assertEqual(build_email_subject(posts), "Superhero found 2 new posts")

    def test_subject_is_singular_for_one_post(self):
        self.assertEqual(
            build_email_subject([self._post("a1", True), self._post("b2", False)]),
            "Superhero found 1 new post",
        )


class KeywordPrefilterTests(unittest.TestCase):
    def _post(self, post_id, title="", summary=""):
        return {
            "id": post_id,
            "title": title,
            "link": f"https://example.com/{post_id}",
            "summary": summary,
            "relevant": False,
        }

    def test_matches_keyword_in_title(self):
        from app.main import matches_keywords

        post = self._post("a1", title="Cheap glasses recommendation?")
        self.assertTrue(matches_keywords(post, ["cheap glasses"]))

    def test_matches_keyword_in_summary(self):
        from app.main import matches_keywords

        post = self._post("a1", title="Help me", summary="I need prescription LENSES")
        self.assertTrue(matches_keywords(post, ["lenses"]))

    def test_matching_is_case_insensitive(self):
        from app.main import matches_keywords

        post = self._post("a1", title="PROGRESSIVE LENSES thread")
        self.assertTrue(matches_keywords(post, ["progressive lenses"]))

    def test_unrelated_post_does_not_match(self):
        from app.main import matches_keywords

        post = self._post("a1", title="Cheap laptop thread", summary="laptops")
        self.assertFalse(matches_keywords(post, ["cheap glasses", "lenses"]))

    def test_model_only_sees_keyword_matches(self):
        posts = [
            self._post("a1", title="Cheap glasses deals"),
            self._post("b2", title="Cat pictures"),
        ]
        candidates = [p for p in posts if matches_keywords(p, ["cheap glasses"])]
        with mock.patch("app.main.request_relevant_ids", return_value={"a1"}) as call:
            filter_relevant_posts(candidates)
        self.assertEqual(call.call_args[0][0], [posts[0]])

    def test_keyword_misses_are_never_sent_to_the_model(self):
        posts = [self._post("b2", title="Cat pictures")]
        with mock.patch("app.main.request_relevant_ids") as call:
            filter_relevant_posts([])
        call.assert_not_called()

    def test_defaults_are_used_when_env_keyword_list_is_blank(self):
        from app.main import AMBIENT, KEYWORDS

        self.assertIn("glass", KEYWORDS)
        self.assertIn("lens", KEYWORDS)
        self.assertIn("rx", KEYWORDS)
        self.assertIn("repair", AMBIENT)
        self.assertNotIn("repair", KEYWORDS)

    def test_single_eyewear_term_is_enough(self):
        from app.main import matches_keywords

        post = self._post("a1", title="Anyone had their glasses repaired cheaply?")
        self.assertTrue(matches_keywords(post, ["glass"], ["repair", "durable"]))

    def test_lone_ambient_term_is_not_enough(self):
        from app.main import matches_keywords

        post = self._post("a1", title="I repaired my espresso machine")
        self.assertFalse(matches_keywords(post, ["glass"], ["repair"]))

    def test_two_ambient_terms_are_enough(self):
        from app.main import matches_keywords

        post = self._post("a1", title="Cheap durable repair kit that lasts")
        self.assertTrue(
            matches_keywords(post, ["glass"], ["repair", "durable", "cheapest"])
        )

    def test_backpack_post_is_rejected_by_defaults(self):
        from app.main import AMBIENT, KEYWORDS, matches_keywords

        post = self._post(
            "a1",
            title="BuyItForLife: my 10 year old backpack with a patch",
            summary="durable and still going",
        )
        self.assertFalse(matches_keywords(post, KEYWORDS, AMBIENT))

    def test_variations_are_caught_by_stems(self):
        from app.main import matches_keywords

        for title in ("cheap glasses", "eyeglasses tip", "sunglasses", "my lens cracked"):
            self.assertTrue(matches_keywords(self._post("x", title=title), ["glass", "lens"]), title)

    def test_rx_and_prescription_are_recognised(self):
        from app.main import KEYWORDS, matches_keywords

        for title in ("Is rx insurance worth it", "prescription renewal costs"):
            self.assertTrue(matches_keywords(self._post("x", title=title), KEYWORDS), title)

    def test_ambient_is_ignored_when_not_supplied(self):
        from app.main import matches_keywords

        post = self._post("a1", title="durable repair lasting deal")
        self.assertFalse(matches_keywords(post, ["glass"]))

    def test_prompt_rules_out_subjective_appearance_questions(self):
        from app.main import RELEVANCE_SYSTEM_PROMPT

        prompt = RELEVANCE_SYSTEM_PROMPT.lower()
        self.assertIn("subjective", prompt)
        self.assertIn("suit", prompt)
        self.assertIn("face shape", prompt)

    def test_prompt_still_allows_objective_comparisons(self):
        from app.main import RELEVANCE_SYSTEM_PROMPT

        self.assertIn("objective", RELEVANCE_SYSTEM_PROMPT.lower())


class SeenIdsPersistenceTests(unittest.TestCase):
    def _path(self, name):
        return os.path.join(tempfile.mkdtemp(), "seen_ids.json")

    def test_round_trips_ids(self):
        from app import main

        path = self._path("t")
        main.save_seen_ids(path, {"a1": None, "a2": None})

        self.assertEqual(list(main.load_seen_ids(path)), ["a1", "a2"])

    def test_missing_file_starts_empty(self):
        from app import main

        self.assertEqual(main.load_seen_ids(self._path("t")), {})

    def test_corrupt_file_starts_empty(self):
        from app import main

        path = self._path("t")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertEqual(main.load_seen_ids(path), {})

    def test_non_list_payload_starts_empty(self):
        from app import main

        path = self._path("t")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"a1": True}, handle)

        self.assertEqual(main.load_seen_ids(path), {})

    def test_save_trims_oldest_first(self):
        from app import main

        path = self._path("t")
        seen = {f"id{i}": None for i in range(main.SEEN_IDS_MAX + 10)}
        main.save_seen_ids(path, seen)

        self.assertEqual(len(seen), main.SEEN_IDS_MAX)
        self.assertNotIn("id0", seen)
        self.assertIn(f"id{main.SEEN_IDS_MAX + 9}", seen)

    def test_restored_ids_are_not_reprocessed(self):
        from app import main

        path = self._path("t")
        main.save_seen_ids(path, {"a1": None})
        restored = main.load_seen_ids(path)

        feed = {"entries": [{"id": "a1", "title": "Already seen", "link": "u"}]}
        self.assertEqual(main.get_new_entries(feed, restored), [])

    def test_no_temp_file_left_behind(self):
        from app import main

        path = self._path("t")
        main.save_seen_ids(path, {"a1": None})

        self.assertFalse(os.path.exists(f"{path}.tmp"))

    def test_creates_missing_parent_directory(self):
        from app import main

        base = tempfile.mkdtemp()
        path = os.path.join(base, "data", "nested", "seen.json")
        main.save_seen_ids(path, {"a1": None})

        self.assertEqual(list(main.load_seen_ids(path)), ["a1"])


class SubredditIntervalTests(unittest.TestCase):
    def _response(self):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://www.reddit.com/r/glasses/.rss"
        response._content = b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
        return response

    def test_first_pass_fetches_everything(self):
        from app import main

        with mock.patch.object(main.SESSION, "get", return_value=self._response()):
            with mock.patch("app.main.time.sleep"):
                feeds = main.fetch_due_subreddit_feeds(
                    main.SUBREDDITS, RateLimiter(gap=0), {}
                )

        self.assertEqual(len(feeds), len(main.SUBREDDITS))

    def test_not_due_subreddits_are_skipped(self):
        from app import main

        just_fetched = time.monotonic()
        with mock.patch("app.main.time.sleep"):
            with mock.patch.object(
                main, "fetch_subreddit_feed", return_value={}
            ) as fetch:
                main.fetch_due_subreddit_feeds(
                    ["glasses"], RateLimiter(gap=0), {"glasses": just_fetched}, {"glasses": 3600}
                )

        fetch.assert_not_called()

    def test_due_subreddit_is_fetched_again(self):
        from app import main

        long_ago = time.monotonic() - 10_000
        with mock.patch("app.main.time.sleep"):
            with mock.patch.object(
                main, "fetch_subreddit_feed", return_value={}
            ) as fetch:
                main.fetch_due_subreddit_feeds(
                    ["glasses"], RateLimiter(gap=0), {"glasses": long_ago}, {"glasses": 100}
                )

        fetch.assert_called_once()

    def test_default_interval_applies_to_unlisted_subs(self):
        from app import main

        just_fetched = time.monotonic()
        with mock.patch("app.main.time.sleep"):
            with mock.patch.object(
                main, "fetch_subreddit_feed", return_value={}
            ) as fetch:
                main.fetch_due_subreddit_feeds(
                    ["rumbly"], RateLimiter(gap=0), {"rumbly": just_fetched}, {}
                )

        fetch.assert_not_called()

    def test_last_fetched_is_recorded(self):
        from app import main

        last_fetched: dict[str, float] = {}
        with mock.patch("app.main.time.sleep"):
            with mock.patch.object(main, "fetch_subreddit_feed", return_value={}):
                main.fetch_due_subreddit_feeds(
                    ["Rumbly"], RateLimiter(gap=0), last_fetched, {}
                )

        self.assertIn("rumbly", last_fetched)

    def test_slow_subs_have_a_longer_interval_than_busy_ones(self):
        from app import main

        self.assertGreater(main.SUBREDDIT_INTERVAL_SECONDS["glasses"], 600)
        self.assertNotIn("Frugal".lower(), main.SUBREDDIT_INTERVAL_SECONDS)


class SeenIdsPathTests(unittest.TestCase):
    def _resolve(self, env):
        from app import main

        with mock.patch.dict("os.environ", env, clear=True):
            return main.resolve_seen_ids_path()

    def test_falls_back_to_relative_path_locally(self):
        self.assertEqual(self._resolve({}), "seen_ids.json")

    def test_uses_railway_volume_mount_path(self):
        resolved = self._resolve({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})
        self.assertEqual(resolved.replace("\\", "/"), "/data/seen_ids.json")

    def test_handles_trailing_slash_on_mount_path(self):
        resolved = self._resolve({"RAILWAY_VOLUME_MOUNT_PATH": "/data/"})
        self.assertEqual(resolved.replace("\\", "/"), "/data/seen_ids.json")

    def test_ignores_blank_mount_path(self):
        self.assertEqual(self._resolve({"RAILWAY_VOLUME_MOUNT_PATH": "  "}), "seen_ids.json")


if __name__ == "__main__":
    unittest.main()
