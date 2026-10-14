import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

import app.main as main
from app.questions import (
    MAX_EXAMPLES,
    QUESTIONS_MAX,
    add_questions,
    build_questions_email_html,
    build_questions_subject,
    clean_question,
    due_questions,
    load_questions,
    mark_sent,
    question_key,
    save_questions,
)


def _path(name="questions.json"):
    return os.path.join(tempfile.mkdtemp(), name)


def _question(post_id="a1"):
    return {
        "id": post_id,
        "title": f"Thread {post_id}",
        "link": f"https://example.com/{post_id}",
        "summary": "",
        "relevant": True,
    }


class QuestionKeyTests(unittest.TestCase):
    def test_case_and_punctuation_do_not_change_the_key(self):
        self.assertEqual(
            question_key("Is blue light blocking worth it?"),
            question_key("is blue light blocking worth it"),
        )

    def test_filler_openers_are_dropped(self):
        self.assertEqual(
            question_key("So, anyone know how much are lenses?"),
            question_key("how much are lenses"),
        )

    def test_only_leading_filler_is_dropped(self):
        # "sow" starts the sentence here, so matching on the word "so" must not
        # eat it the way it eats the opener in "so how do I".
        self.assertEqual(question_key("how do I sow seeds"), "how do i sow seeds")

    def test_blank_question_has_no_key(self):
        self.assertEqual(question_key("   "), "")
        self.assertEqual(question_key("???"), "")

    def test_different_questions_keep_different_keys(self):
        self.assertNotEqual(question_key("how much are lenses"), question_key("how much is a exam"))


class CleanQuestionTests(unittest.TestCase):
    def test_the_stored_headline_drops_the_opener(self):
        # This string becomes a guide title, so it cannot open with "so anyone know".
        self.assertEqual(
            clean_question("so anyone know how much is an eye exam?"),
            "how much is an eye exam?",
        )

    def test_cleaning_keeps_the_wording_and_punctuation(self):
        self.assertEqual(
            clean_question("Hey, What's a pupillary distance?"),
            "What's a pupillary distance?",
        )

    def test_cleaning_collapses_runs_of_whitespace(self):
        self.assertEqual(clean_question("  Is   blue   light   worth it?  "), "Is blue light worth it?")

    def test_a_question_with_no_opener_is_untouched(self):
        self.assertEqual(clean_question("What is prism correction?"), "What is prism correction?")

    def test_a_stored_opener_never_reaches_the_digest(self):
        backlog = {}
        add_questions(backlog, {"a1": ["hey so what is indexation?"]}, {}, when="t1")
        entry = backlog[question_key("hey so what is indexation?")]
        self.assertEqual(entry["question"], "what is indexation?")


class QuestionBacklogTests(unittest.TestCase):
    def test_new_question_is_added_with_its_source(self):
        backlog = {}
        added = add_questions(
            backlog,
            {"a1": ["How much do progressive lenses cost?"]},
            {"a1": ("Progressive thread", "https://example.com/a1")},
            when="2026-10-14T10:00:00+00:00",
        )

        self.assertEqual(len(added), 1)
        self.assertEqual(len(backlog), 1)
        entry = added[0]
        self.assertEqual(entry["question"], "How much do progressive lenses cost?")
        self.assertEqual(entry["asked"], 1)
        self.assertEqual(entry["sent_on"], "")
        self.assertEqual(entry["examples"], [{"title": "Progressive thread", "link": "https://example.com/a1"}])

    def test_repeat_question_raises_the_tally(self):
        backlog = {}
        sources = {
            "a1": ("First", "https://example.com/a1"),
            "b2": ("Second", "https://example.com/b2"),
        }
        add_questions(backlog, {"a1": ["How much is an eye exam?"]}, {k: sources[k] for k in ("a1",)}, when="t1")
        add_questions(backlog, {"b2": ["How much is an eye exam?"]}, {k: sources[k] for k in ("b2",)}, when="t2")

        self.assertEqual(len(backlog), 1)
        entry = backlog[question_key("How much is an eye exam?")]
        self.assertEqual(entry["asked"], 2)
        self.assertEqual(entry["first_seen"], "t1")
        self.assertEqual(entry["last_seen"], "t2")

    def test_rephrasing_collapses_into_one_entry(self):
        backlog = {}
        add_questions(backlog, {"a1": ["How much is an eye exam?"]}, {}, when="t1")
        add_questions(
            backlog,
            {"b2": ["hey so how much is an eye exam!!!"]},
            {},
            when="t2",
        )

        self.assertEqual(len(backlog), 1)
        # The first phrasing a real person wrote is the one kept, since that is
        # what reads best in the digest.
        self.assertEqual(
            backlog[question_key("how much is an eye exam")]["question"],
            "How much is an eye exam?",
        )

    def test_examples_are_capped(self):
        backlog = {}
        for index in range(MAX_EXAMPLES + 2):
            post_id = f"p{index}"
            add_questions(
                backlog,
                {post_id: ["Which frames last longest?"]},
                {post_id: (f"Thread {index}", f"https://example.com/{post_id}")},
                when=f"t{index}",
            )

        entry = backlog[question_key("which frames last longest")]
        self.assertEqual(len(entry["examples"]), MAX_EXAMPLES)
        # The oldest example is the one dropped.
        self.assertEqual(entry["examples"][-1]["link"], f"https://example.com/p{MAX_EXAMPLES + 1}")

    def test_same_post_twice_does_not_double_list_the_source(self):
        backlog = {}
        sources = {"a1": ("Thread", "https://example.com/a1")}
        add_questions(backlog, {"a1": ["Are thinner lenses stronger?"]}, sources, when="t1")
        add_questions(backlog, {"a1": ["Are thinner lenses stronger?"]}, sources, when="t2")

        entry = backlog[question_key("are thinner lenses stronger")]
        self.assertEqual(entry["asked"], 2)
        self.assertEqual(len(entry["examples"]), 1)

    def test_blank_questions_are_not_stored(self):
        backlog = {}
        add_questions(backlog, {"a1": ["  ", "???"]}, {}, when="t1")
        self.assertEqual(backlog, {})

    def test_due_questions_exclude_already_sent(self):
        backlog = {}
        add_questions(backlog, {"a1": ["Which coating blocks glare?"]}, {}, when="t1")
        add_questions(backlog, {"a1": ["Can I use my old frames?"]}, {}, when="t1")

        mark_sent(backlog, [question_key("which coating blocks glare")], when="t2")

        self.assertEqual(
            [entry["question"] for entry in due_questions(backlog)],
            ["Can I use my old frames?"],
        )

    def test_due_questions_lead_with_the_most_asked(self):
        backlog = {}
        add_questions(backlog, {"a1": ["Rarely asked question?"]}, {}, when="t1")
        add_questions(backlog, {"a1": ["Popular question one?"]}, {}, when="t1")
        add_questions(backlog, {"a1": ["Popular question one?"]}, {}, when="t1")

        self.assertEqual(due_questions(backlog)[0]["question"], "Popular question one?")

    def test_mark_sent_leaves_the_entry_in_place(self):
        backlog = {}
        add_questions(backlog, {"a1": ["Do I need a pupillary distance?"]}, {}, when="t1")
        mark_sent(backlog, [question_key("do i need a pupillary distance")], when="t2")

        entry = backlog[question_key("do i need a pupillary distance")]
        self.assertEqual(entry["sent_on"], "t2")
        self.assertEqual(entry["question"], "Do I need a pupillary distance?")

    def test_mark_sent_ignores_unknown_keys(self):
        backlog = {}
        add_questions(backlog, {"a1": ["What is prism correction?"]}, {}, when="t1")
        mark_sent(backlog, ["never seen"], when="t2")
        self.assertEqual(len(backlog), 1)


class QuestionPersistenceTests(unittest.TestCase):
    def test_round_trips_entries(self):
        path = _path()
        backlog = {}
        add_questions(
            backlog,
            {"a1": ["Should I get blue light lenses?"]},
            {"a1": ("Blue light thread", "https://example.com/a1")},
            when="2026-10-14T10:00:00+00:00",
        )
        save_questions(path, backlog)

        restored = load_questions(path)
        self.assertEqual(len(restored), 1)
        entry = restored[question_key("should i get blue light lenses")]
        self.assertEqual(entry["question"], "Should I get blue light lenses?")
        self.assertEqual(entry["asked"], 1)
        self.assertEqual(entry["first_seen"], "2026-10-14T10:00:00+00:00")

    def test_missing_file_starts_empty(self):
        self.assertEqual(load_questions(_path()), {})

    def test_corrupt_file_starts_empty(self):
        path = _path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(load_questions(path), {})

    def test_wrongly_shaped_file_starts_empty(self):
        path = _path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(["a", "b"], handle)
        self.assertEqual(load_questions(path), {})

    def test_unusable_entries_are_dropped_and_the_rest_survive(self):
        # The file is meant to be hand-editable, so it can be hand-broken. One
        # bad entry must not take the whole backlog with it.
        path = _path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "questions": [
                        {"question": "Real question?", "key": "real question"},
                        {"key": "no-text-here"},
                        "not even an object",
                        {"question": "   "},
                        {"question": "Recovered?", "asked": "not a number"},
                    ]
                },
                handle,
            )

        restored = load_questions(path)
        self.assertEqual(len(restored), 2)
        self.assertIn("real question", restored)
        self.assertIn("recovered", restored)
        self.assertEqual(restored["recovered"]["asked"], 1)

    def test_entry_without_a_key_gets_one_derived(self):
        path = _path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"questions": [{"question": "Where should I get fitted?"}]}, handle)

        restored = load_questions(path)
        self.assertEqual(list(restored), [question_key("where should i get fitted")])

    def test_a_sent_question_can_be_reopened_by_hand(self):
        # Clearing sent_on is how a topic gets back on the list once the guide is
        # ready for another look, so it has to survive a save and reload.
        path = _path()
        backlog = {}
        add_questions(backlog, {"a1": ["Can I use my old frames?"]}, {}, when="t1")
        mark_sent(backlog, [question_key("can i use my old frames")], when="t2")
        save_questions(path, backlog)

        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["questions"][0]["sent_on"] = ""
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)

        self.assertEqual(len(due_questions(load_questions(path))), 1)

    def test_save_trims_delivered_entries_first(self):
        path = _path()
        backlog = {}
        for index in range(QUESTIONS_MAX + 10):
            key = f"question number {index}"
            backlog[key] = {
                "key": key,
                "question": key,
                "asked": 1,
                "first_seen": "t",
                "last_seen": "t",
                "sent_on": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
                "examples": [],
            }

        save_questions(path, backlog)

        self.assertLessEqual(len(backlog), QUESTIONS_MAX)
        self.assertNotIn("question number 0", backlog)
        self.assertIn(f"question number {QUESTIONS_MAX + 9}", backlog)

    def test_undelivered_questions_are_never_trimmed(self):
        # These are the ones still waiting on a guide, so the file is allowed to
        # sit above the cap rather than silently discard work.
        path = _path()
        backlog = {}
        for index in range(QUESTIONS_MAX + 5):
            key = f"pending question {index}"
            backlog[key] = {
                "key": key,
                "question": key,
                "asked": 1,
                "first_seen": "t",
                "last_seen": "t",
                "sent_on": "",
                "examples": [],
            }

        save_questions(path, backlog)

        self.assertEqual(len(load_questions(path)), QUESTIONS_MAX + 5)

    def test_no_temp_file_left_behind(self):
        path = _path()
        backlog = {}
        add_questions(backlog, {"a1": ["What is indexation?"]}, {}, when="t1")
        save_questions(path, backlog)
        self.assertFalse(os.path.exists(f"{path}.tmp"))

    def test_creates_missing_parent_directory(self):
        base = tempfile.mkdtemp()
        path = os.path.join(base, "data", "nested", "questions.json")
        backlog = {}
        add_questions(backlog, {"a1": ["What is indexation?"]}, {}, when="t1")
        save_questions(path, backlog)
        self.assertEqual(len(load_questions(path)), 1)


class QuestionExtractionTests(unittest.TestCase):
    def _posts(self, *post_ids):
        return [
            {"id": post_id, "title": f"Post {post_id}", "link": "u", "summary": "", "relevant": True}
            for post_id in post_ids
        ]

    def test_parse_groups_questions_by_their_post(self):
        content = json.dumps(
            {
                "questions": [
                    {"id": "a1", "question": "How much is an eye exam?"},
                    {"id": "b2", "question": "What is pupillary distance?"},
                ]
            }
        )

        found = main.parse_questions(content, {"a1", "b2"})

        self.assertEqual(found["a1"], ["How much is an eye exam?"])
        self.assertEqual(found["b2"], ["What is pupillary distance?"])

    def test_parse_drops_ids_that_were_never_given(self):
        content = json.dumps(
            {
                "questions": [
                    {"id": "made-up", "question": "Where did this come from?"},
                    {"id": "a1", "question": "A real one?"},
                ]
            }
        )

        found = main.parse_questions(content, {"a1"})

        self.assertEqual(list(found), ["a1"])

    def test_parse_dedupes_within_a_post(self):
        content = json.dumps(
            {
                "questions": [
                    {"id": "a1", "question": "How much is an eye exam?"},
                    {"id": "a1", "question": "how much is an EYE exam"},
                ]
            }
        )

        self.assertEqual(main.parse_questions(content, {"a1"})["a1"], ["How much is an eye exam?"])

    def test_parse_keeps_a_post_that_asked_several_distinct_questions(self):
        content = json.dumps(
            {
                "questions": [
                    {"id": "a1", "question": "How much is an eye exam?"},
                    {"id": "a1", "question": "Does insurance cover it?"},
                ]
            }
        )

        self.assertEqual(len(main.parse_questions(content, {"a1"})["a1"]), 2)

    def test_parse_omits_posts_with_no_usable_question(self):
        content = json.dumps(
            {
                "questions": [
                    {"id": "a1", "question": "   "},
                    {"id": "a1"},
                    {"not": "an entry"},
                ]
            }
        )

        self.assertEqual(main.parse_questions(content, {"a1"}), {})

    def test_parse_tolerates_a_reasoning_trace(self):
        content = (
            "Let me look at each post.\n```json\n"
            '{"questions": [{"id": "a1", "question": "Which coating blocks glare?"}]}\n```'
        )

        self.assertEqual(
            main.parse_questions(content, {"a1"})["a1"], ["Which coating blocks glare?"]
        )

    def test_parse_rejects_a_non_list_payload(self):
        self.assertEqual(main.parse_questions('{"questions": "nope"}', {"a1"}), {})

    def test_parse_handles_a_response_with_no_questions_field(self):
        self.assertEqual(main.parse_questions('{"other": 1}', {"a1"}), {})

    def test_parse_handles_blank_content(self):
        self.assertEqual(main.parse_questions("", {"a1"}), {})

    def test_payload_sends_the_title_and_snippet(self):
        payload = main.build_questions_payload(self._posts("a1"), "m", None)

        content = payload["messages"][1]["content"]
        self.assertIn('"id": "a1"', content)
        self.assertIn("Post a1", content)
        self.assertIn("summary", content)
        self.assertNotIn("link", content)

    def test_payload_gives_a_wider_snippet_than_relevance(self):
        # The question usually sits in the body, not the title, so this pass gets
        # more context than triage does.
        self.assertGreater(main.QUESTION_SNIPPET_CHARS, main.SUMMARY_SNIPPET_CHARS)

    def test_payload_caps_generated_tokens(self):
        payload = main.build_questions_payload(self._posts("a1"), "m", None)
        self.assertEqual(payload["max_tokens"], main.QUESTIONS_MAX_TOKENS)

    def test_collect_unions_every_batch(self):
        def fake_request(batch, model, timeout=60, prompt=""):
            return {post["id"]: [f"Question from {post['id']}?"] for post in batch}

        with mock.patch("app.main.QUESTION_BATCH_SIZE", 2):
            with mock.patch("app.main.request_questions", side_effect=fake_request) as call:
                found = main.collect_questions_from_posts(self._posts("a1", "b2", "c3"))

        self.assertEqual(call.call_count, 2)
        self.assertEqual(sorted(found), ["a1", "b2", "c3"])

    def test_collect_returns_none_when_every_batch_fails(self):
        with mock.patch("app.main.request_questions", return_value=None):
            self.assertIsNone(main.collect_questions_from_posts(self._posts("a1", "b2")))

    def test_collect_keeps_a_partial_result(self):
        def fake_request(batch, model, timeout=60, prompt=""):
            if batch[0]["id"] == "a1":
                return None
            return {batch[0]["id"]: ["A question?"]}

        with mock.patch("app.main.QUESTION_BATCH_SIZE", 1):
            with mock.patch("app.main.request_questions", side_effect=fake_request):
                found = main.collect_questions_from_posts(self._posts("a1", "b2"))

        # The good batch is kept so its posts are not paid for twice, and only the
        # caller decides what to retry.
        self.assertEqual(list(found), ["b2"])

    def test_collect_skips_the_model_entirely_with_no_posts(self):
        with mock.patch("app.main.request_questions") as request:
            self.assertEqual(main.collect_questions_from_posts([]), {})
        request.assert_not_called()

    def test_request_without_an_api_key_returns_none(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": ""}, clear=False):
            self.assertIsNone(main.request_questions(self._posts("a1"), "no-key-model"))

    def test_request_parses_a_successful_response(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = ""
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"questions": [{"id": "a1", "question": "Which frame shape suits me?"}]}'
                    }
                }
            ]
        }

        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with mock.patch("app.main.API_SESSION.post", return_value=response) as post:
                found = main.request_questions(self._posts("a1"), "strict-model")

        self.assertEqual(found["a1"], ["Which frame shape suits me?"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_request_falls_back_when_the_schema_is_rejected(self):
        def _response(content, status_code=200):
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

        main._QUESTION_FORMAT_INDEX_BY_MODEL.pop("loose-q-model", None)
        rejected = _response('{"error":{"message":"response_format not supported"}}', 400)
        accepted = _response('{"questions": [{"id": "a1", "question": "A question?"}]}')

        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with mock.patch("app.main.API_SESSION.post", side_effect=[rejected, accepted]) as post:
                found = main.request_questions(self._posts("a1"), "loose-q-model")

        self.assertEqual(found["a1"], ["A question?"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["json"]["response_format"]["type"], "json_object")

    def test_the_two_passes_do_not_share_a_request_shape_cache(self):
        # Both passes usually run the same model. If they shared one cache, the
        # first to be served would pin the other to a shape it never agreed to.
        main._FORMAT_INDEX_BY_MODEL.pop("shared-model", None)
        main._QUESTION_FORMAT_INDEX_BY_MODEL.pop("shared-model", None)
        main._FORMAT_INDEX_BY_MODEL["shared-model"] = 2

        response = mock.Mock()
        response.status_code = 200
        response.text = ""
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": '{"questions": []}'}}]}

        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with mock.patch("app.main.API_SESSION.post", return_value=response) as post:
                main.request_questions(self._posts("a1"), "shared-model")

        self.assertEqual(
            post.call_args.kwargs["json"]["response_format"]["type"],
            "json_schema",
        )
        main._FORMAT_INDEX_BY_MODEL.pop("shared-model", None)


class HarvestRetryTests(unittest.TestCase):
    def test_posts_are_held_when_the_model_cannot_be_reached(self):
        # They are already marked as seen, so without holding them their
        # questions would never be offered again.
        backlog = {}
        retry = []
        with mock.patch("app.main.collect_questions_from_posts", return_value=None):
            main.harvest_questions([_question()], "m", "p", backlog, _path(), retry)

        self.assertEqual(len(retry), 1)
        self.assertEqual(backlog, {})

    def test_held_posts_are_cleared_once_they_are_read(self):
        backlog = {}
        held = _question("b2")
        retry = [held]
        with mock.patch(
            "app.main.collect_questions_from_posts",
            return_value={"b2": ["What is prism correction?"]},
        ):
            main.harvest_questions([_question("a1"), held], "m", "p", backlog, _path(), retry)

        self.assertEqual(retry, [])
        self.assertEqual(
            [entry["question"] for entry in backlog.values()],
            ["What is prism correction?"],
        )

    def test_a_post_the_model_read_but_found_nothing_in_is_not_retried(self):
        backlog = {}
        retry = []
        with mock.patch("app.main.collect_questions_from_posts", return_value={}):
            main.harvest_questions([_question()], "m", "p", backlog, _path(), retry)

        self.assertEqual(retry, [])
        self.assertEqual(backlog, {})

    def test_new_questions_are_written_to_disk(self):
        path = _path()
        backlog = {}
        with mock.patch(
            "app.main.collect_questions_from_posts",
            return_value={"a1": ["Are rimless frames more fragile?"]},
        ):
            main.harvest_questions([_question()], "m", "p", backlog, path, [])

        self.assertEqual(len(load_questions(path)), 1)

    def test_no_posts_is_a_no_op(self):
        with mock.patch("app.main.collect_questions_from_posts") as collect:
            main.harvest_questions([], "m", "p", {}, _path(), [])
        collect.assert_not_called()


class QuestionDigestTests(unittest.TestCase):
    def _entry(self, question, asked=1, examples=()):
        return {
            "key": question_key(question),
            "question": question,
            "asked": asked,
            "first_seen": "t",
            "last_seen": "t",
            "sent_on": "",
            "examples": list(examples),
        }

    def test_the_question_is_the_headline_not_a_link(self):
        # It gets typed into a CMS, so it has to be readable as plain text.
        body = build_questions_email_html([self._entry("How much is an eye exam?")], 1)
        self.assertIn("How much is an eye exam?", body)

    def test_source_posts_are_linked(self):
        entry = self._entry(
            "How much is an eye exam?",
            examples=[{"title": "Exam thread", "link": "https://example.com/a1"}],
        )
        body = build_questions_email_html([entry], 1)

        self.assertIn('href="https://example.com/a1"', body)
        self.assertIn("Exam thread", body)

    def test_the_ask_count_shows_only_when_it_is_more_than_one(self):
        once = build_questions_email_html([self._entry("A question?")], 1)
        twice = build_questions_email_html([self._entry("A question?", asked=4)], 1)

        self.assertIn("asked once", once)
        self.assertNotIn("asked once", twice)
        self.assertIn("asked 4x", twice)

    def test_questions_are_escaped(self):
        body = build_questions_email_html([self._entry("Fish & <chips>?")], 1)
        self.assertIn("Fish &amp; &lt;chips&gt;", body)
        self.assertNotIn("<chips>", body)

    def test_source_links_are_escaped(self):
        entry = self._entry(
            "A question?",
            examples=[{"title": "T", "link": 'https://example.com/a"b'}],
        )
        body = build_questions_email_html([entry], 1)
        self.assertNotIn('a"b', body)

    def test_an_empty_digest_says_so(self):
        body = build_questions_email_html([], 0)
        self.assertIn("No new questions worth a guide.", body)

    def test_the_backlog_size_is_reported(self):
        body = build_questions_email_html([self._entry("A question?")], 37)
        self.assertIn("37 question(s) in the backlog", body)

    def test_no_interactive_controls_are_emitted(self):
        body = build_questions_email_html([self._entry("A question?")], 1)
        self.assertNotIn("<input", body)
        self.assertNotIn("<style", body)

    def test_subject_counts_the_questions(self):
        self.assertEqual(build_questions_subject(3), "3 new questions worth a guide")

    def test_subject_is_singular_for_one(self):
        self.assertEqual(build_questions_subject(1), "1 new question worth a guide")


class DailyQuestionListTests(unittest.TestCase):
    """The end-of-day send, driven through run_bot with a frozen clock."""

    def _run(self, backlog, when, **overrides):
        path = _path()
        save_questions(path, backlog)

        settings = {
            "collect_questions": True,
            "questions_path": path,
            "questions_hour": 23,
        }
        settings.update(overrides)

        with mock.patch("app.main.datetime") as clock:
            clock.now.return_value = when
            with mock.patch("app.main.fetch_due_subreddit_feeds", return_value=[]):
                with mock.patch(
                    "app.main.send_questions_email", return_value=True
                ) as send:
                    main.run_bot(["glasses"], once=True, **settings)

        return send, load_questions(path)

    def _entry(self, question):
        return {
            "key": question_key(question),
            "question": question,
            "asked": 1,
            "first_seen": "t",
            "last_seen": "t",
            "sent_on": "",
            "examples": [],
        }

    def test_nothing_is_sent_before_the_hour(self):
        backlog = {self._entry("How much is an eye exam?")["key"]: self._entry("How much is an eye exam?")}
        send, restored = self._run(
            backlog, datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
        )

        send.assert_not_called()
        self.assertEqual(len(due_questions(restored)), 1)

    def test_the_whole_backlog_goes_out_after_the_hour(self):
        backlog = {}
        for question in ("How much is an eye exam?", "Which frames last longest?"):
            entry = self._entry(question)
            backlog[entry["key"]] = entry

        send, restored = self._run(
            backlog, datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc)
        )

        send.assert_called_once()
        self.assertEqual(due_questions(restored), [])

    def test_a_failed_send_leaves_everything_queued_for_tomorrow(self):
        entry = self._entry("How much is an eye exam?")
        path = _path()
        save_questions(path, {entry["key"]: entry})

        with mock.patch("app.main.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc)
            with mock.patch("app.main.fetch_due_subreddit_feeds", return_value=[]):
                with mock.patch("app.main.send_questions_email", return_value=False):
                    main.run_bot(["glasses"], once=True, collect_questions=True, questions_path=path)

        self.assertEqual(len(due_questions(load_questions(path))), 1)

    def test_an_empty_backlog_does_not_send_a_bare_email(self):
        send, _ = self._run({}, datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc))
        send.assert_not_called()

    def test_a_later_hour_setting_is_respected(self):
        entry = self._entry("Which coatings block glare?")
        send, _ = self._run(
            {entry["key"]: entry},
            datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc),
            questions_hour=6,
        )

        send.assert_called_once()

    def test_nothing_is_collected_when_the_pass_is_off(self):
        with mock.patch("app.main.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc)
            with mock.patch("app.main.fetch_due_subreddit_feeds", return_value=[]):
                with mock.patch("app.main.collect_questions_from_posts") as collect:
                    with mock.patch("app.main.send_questions_email") as send:
                        main.run_bot(["glasses"], once=True, collect_questions=False)

        collect.assert_not_called()
        send.assert_not_called()


class QuestionRecipientTests(unittest.TestCase):
    """The guide list is one person's work queue and must stay that way."""

    _ENV = {
        "BREVO_API_KEY": "test-key",
        "SENDER_EMAIL": "support@ollglasses.com",
        "RECIPIENT_EMAIL": "digest@example.com",
        "QUESTIONS_RECIPIENT_EMAIL": "writer@example.com",
        # The post digest cc's several people. The guide list must not.
        "CC_EMAILS": "someone@example.com, another@example.com",
    }

    def _entry(self, question="How much is an eye exam?"):
        return {
            "key": question_key(question),
            "question": question,
            "asked": 1,
            "first_seen": "t",
            "last_seen": "t",
            "sent_on": "",
            "examples": [],
        }

    def _sent_payload(self, questions_email, env=None):
        """Run the real send path and return the payload Brevo was handed."""
        path = _path()
        entry = self._entry()
        save_questions(path, {entry["key"]: entry})

        sent = mock.Mock()
        sent.status_code = 200
        sent.raise_for_status.return_value = None

        with mock.patch.dict("os.environ", env or self._ENV, clear=True):
            with mock.patch("app.main.datetime") as clock:
                clock.now.return_value = datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc)
                with mock.patch("app.main.fetch_due_subreddit_feeds", return_value=[]):
                    with mock.patch("app.main.API_SESSION.post", return_value=sent) as post:
                        main.run_bot(
                            ["glasses"],
                            once=True,
                            collect_questions=True,
                            questions_path=path,
                            questions_email=questions_email,
                        )

        return post.call_args.kwargs["json"]

    def test_the_cli_hands_run_bot_the_configured_recipient(self):
        with mock.patch.dict("os.environ", self._ENV, clear=True):
            with mock.patch("app.main.run_bot") as run:
                main.main(["--bot", "superhero"])

        self.assertEqual(run.call_args.kwargs["questions_email"], "writer@example.com")

    def test_the_list_goes_only_to_the_questions_recipient(self):
        from app.bots import SUPERHERO

        with mock.patch.dict("os.environ", self._ENV, clear=True):
            recipient = SUPERHERO.questions_recipient_email

        payload = self._sent_payload(recipient)

        self.assertEqual(payload["to"], [{"email": "writer@example.com"}])

    def test_the_list_never_inherits_the_digest_cc_list(self):
        # A cc here would leak the work queue to everyone on the post digest.
        payload = self._sent_payload("writer@example.com")

        self.assertNotIn("cc", payload)

    def test_it_falls_back_to_the_digest_recipient_when_unset(self):
        # Without the dedicated setting the list goes to whoever gets the post
        # digest, since that is the same person deciding what to write about.
        env = {k: v for k, v in self._ENV.items() if k != "QUESTIONS_RECIPIENT_EMAIL"}

        from app.bots import SUPERHERO

        with mock.patch.dict("os.environ", env, clear=True):
            recipient = SUPERHERO.questions_recipient_email

        self.assertEqual(recipient, "digest@example.com")

        payload = self._sent_payload(recipient, env=env)

        self.assertEqual(payload["to"], [{"email": "digest@example.com"}])
        self.assertNotIn("cc", payload)

    def test_the_post_digest_still_uses_the_cc_list(self):
        # Guards the fix above from being taken too far: only the guide list is
        # narrowed to one person.
        path = _path()
        posts = [{
            "id": "a1",
            "title": "A thread",
            "link": "https://example.com/a1",
            "summary": "",
            "relevant": True,
        }]

        sent = mock.Mock()
        sent.status_code = 200
        sent.raise_for_status.return_value = None

        with mock.patch.dict("os.environ", self._ENV, clear=True):
            with mock.patch("app.main.datetime") as clock:
                clock.now.return_value = datetime(2026, 10, 14, 1, 5, tzinfo=timezone.utc)
                with mock.patch("app.main.API_SESSION.post", return_value=sent) as post:
                    main.send_email_notification(posts, path)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["cc"],
            [{"email": "someone@example.com"}, {"email": "another@example.com"}],
        )


if __name__ == "__main__":
    unittest.main()
