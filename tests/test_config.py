import unittest
from unittest import mock

import app.main as main
from app.bots import BOTS, BUSINESS, DEFAULT_BOT, SUPERHERO, get_bot
from app.config import BotConfig


class BotRegistryTests(unittest.TestCase):
    def test_both_bots_are_registered(self):
        self.assertEqual(sorted(BOTS), ["business", "superhero"])

    def test_lookup_returns_the_right_bot(self):
        self.assertIs(get_bot("business"), BUSINESS)
        self.assertIs(get_bot("superhero"), SUPERHERO)

    def test_unknown_bot_raises_with_the_valid_names(self):
        with self.assertRaises(KeyError) as ctx:
            get_bot("nope")
        self.assertIn("business", str(ctx.exception))

    def test_default_bot_is_superhero(self):
        self.assertEqual(DEFAULT_BOT, "superhero")


class BotIsolationTests(unittest.TestCase):
    def test_user_agents_differ(self):
        # Reddit expects one User-Agent per application.
        self.assertNotEqual(SUPERHERO.user_agent, BUSINESS.user_agent)

    def test_seen_id_files_differ(self):
        self.assertNotEqual(
            SUPERHERO.default_seen_ids_file, BUSINESS.default_seen_ids_file
        )

    def test_env_prefixes_differ(self):
        self.assertNotEqual(SUPERHERO.env_prefix, BUSINESS.env_prefix)

    def test_relevance_prompts_differ(self):
        self.assertNotEqual(SUPERHERO.relevance_prompt, BUSINESS.relevance_prompt)

    def test_subreddit_lists_do_not_overlap(self):
        self.assertFalse(set(SUPERHERO.subreddits) & set(BUSINESS.subreddits))

    def test_eyeglasses_is_not_monitored(self):
        self.assertNotIn("eyeglasses", SUPERHERO.subreddits)


class EnvScopingTests(unittest.TestCase):
    def test_prefixed_variable_wins(self):
        with mock.patch.dict(
            "os.environ",
            {"RECIPIENT_EMAIL": "shared@example.com", "BUSINESS_RECIPIENT_EMAIL": "leads@example.com"},
            clear=True,
        ):
            self.assertEqual(BUSINESS.recipient_email, "leads@example.com")

    def test_falls_back_to_unprefixed_variable(self):
        with mock.patch.dict(
            "os.environ", {"RECIPIENT_EMAIL": "shared@example.com"}, clear=True
        ):
            self.assertEqual(BUSINESS.recipient_email, "shared@example.com")

    def test_empty_prefixed_value_falls_through(self):
        with mock.patch.dict(
            "os.environ",
            {"RECIPIENT_EMAIL": "shared@example.com", "BUSINESS_RECIPIENT_EMAIL": "  "},
            clear=True,
        ):
            self.assertEqual(BUSINESS.recipient_email, "shared@example.com")

    def test_seen_ids_path_prefers_prefixed_env(self):
        with mock.patch.dict(
            "os.environ",
            {
                "SUPERHERO_SEEN_IDS_PATH": "/data/a.json",
                "BUSINESS_SEEN_IDS_PATH": "/data/b.json",
            },
            clear=True,
        ):
            self.assertEqual(SUPERHERO.seen_ids_path, "/data/a.json")
            self.assertEqual(BUSINESS.seen_ids_path, "/data/b.json")

    def test_seen_ids_path_defaults_per_bot(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(SUPERHERO.seen_ids_path, SUPERHERO.default_seen_ids_file)
            self.assertEqual(BUSINESS.seen_ids_path, BUSINESS.default_seen_ids_file)

    def test_env_int_ignores_garbage(self):
        with mock.patch.dict("os.environ", {"BUSINESS_INTERVAL": "soon"}, clear=True):
            self.assertEqual(BUSINESS.cycle_interval, BUSINESS.default_interval)

    def test_env_int_parses_numbers(self):
        with mock.patch.dict("os.environ", {"BUSINESS_INTERVAL": "900"}, clear=True):
            self.assertEqual(BUSINESS.cycle_interval, 900)


class SessionIsolationTests(unittest.TestCase):
    def test_same_agent_reuses_one_pool(self):
        first = main.get_session("python:test:1.0")
        second = main.get_session("python:test:1.0")
        self.assertIs(first, second)

    def test_different_agents_get_different_pools(self):
        self.assertIsNot(
            main.get_session("python:bot-a:1.0"), main.get_session("python:bot-b:1.0")
        )

    def test_bot_user_agents_get_distinct_sessions(self):
        self.assertIsNot(
            main.get_session(SUPERHERO.user_agent), main.get_session(BUSINESS.user_agent)
        )

    def test_session_sends_the_right_user_agent(self):
        session = main.get_session(BUSINESS.user_agent)
        self.assertEqual(session.headers["User-Agent"], BUSINESS.user_agent)


class LeadGenPromptTests(unittest.TestCase):
    def test_business_prompt_targets_hiring_signals(self):
        prompt = BUSINESS.relevance_prompt.lower()
        self.assertIn("hire", prompt)
        self.assertIn("relevant", prompt)
        self.assertIn("relevant_ids", prompt)

    def test_business_prompt_excludes_noise(self):
        prompt = BUSINESS.relevance_prompt.lower()
        self.assertIn("announcement", prompt)
        self.assertIn("job-seeking", prompt)

    def test_business_keywords_include_service_intent(self):
        for keyword in ("looking for", "website", "logo", "marketing", "seo"):
            self.assertIn(keyword, BUSINESS.keywords)

    def test_business_ambient_separates_generic_business_speak(self):
        self.assertIn("small business", BUSINESS.ambient_keywords)
        self.assertNotIn("small business", BUSINESS.keywords)

    def test_business_polls_everything_fast(self):
        # A lead an hour late is one someone else already answered.
        self.assertEqual(BUSINESS.subreddit_intervals, {})


class SweepBudgetTests(unittest.TestCase):
    def test_sweep_is_gap_times_subreddit_count(self):
        config = BotConfig(
            name="x", user_agent="python:x:1.0", env_prefix="X_", subreddits=["a"] * 5
        )
        self.assertEqual(config.sweep_seconds, 4 * 60)

    def test_single_subreddit_needs_no_sweep_gap(self):
        config = BotConfig(
            name="x", user_agent="python:x:1.0", env_prefix="X_", subreddits=["a"]
        )
        self.assertEqual(config.sweep_seconds, 0)

    def test_every_bot_fits_its_own_interval(self):
        # Otherwise the cycle overruns and the sweep silently rotates instead.
        for config in BOTS.values():
            self.assertLessEqual(
                config.sweep_seconds,
                config.cycle_interval,
                f"{config.name} cannot finish a sweep in one cycle",
            )

    def test_business_bot_uses_a_longer_interval_than_superhero(self):
        self.assertGreater(BUSINESS.cycle_interval, SUPERHERO.cycle_interval)


class BusinessSubredditTests(unittest.TestCase):
    def test_covers_owner_pain_subs(self):
        for sub in ("smallbusiness", "smallbusinessadvice", "Entrepreneur"):
            self.assertIn(sub, BUSINESS.subreddits)

    def test_covers_service_categories(self):
        for sub in ("web_design", "seo", "branding", "marketing"):
            self.assertIn(sub, BUSINESS.subreddits)

    def test_no_duplicate_subreddits(self):
        lowered = [sub.lower() for sub in BUSINESS.subreddits]
        self.assertEqual(len(lowered), len(set(lowered)))

    def test_eyewear_subs_are_not_present(self):
        for sub in ("glasses", "optometry", "glassesadvice"):
            self.assertNotIn(sub, BUSINESS.subreddits)


class ConfigDefaultsTests(unittest.TestCase):
    def test_empty_config_is_usable(self):
        config = BotConfig(
            name="x", user_agent="python:x:1.0", env_prefix="X_", subreddits=["a"]
        )
        self.assertEqual(config.cycle_interval, 600)
        self.assertEqual(config.relevance_model, "openai/gpt-oss-20b")
        self.assertEqual(config.seen_ids_path, "seen_ids.json")

    def test_default_interval_can_be_overridden_per_bot(self):
        config = BotConfig(
            name="x",
            user_agent="python:x:1.0",
            env_prefix="X_",
            subreddits=["a"],
            default_interval=1800,
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(config.cycle_interval, 1800)

    def test_intervals_default_to_empty(self):
        config = BotConfig(
            name="x", user_agent="python:x:1.0", env_prefix="X_", subreddits=["a"]
        )
        self.assertEqual(config.subreddit_intervals, {})


if __name__ == "__main__":
    unittest.main()