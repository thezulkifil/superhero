"""Per-bot configuration.

The engine in :mod:`app.main` is shared by every bot. A bot only supplies the
things that actually differ: where it looks, what counts as a hit, and which
credentials and state files belong to it.

Each bot reads its own environment variables under a prefix, so two bots can run
as two services without sharing recipients or seen-post files. The prefix falls
back to the unprefixed name, which keeps a single-bot setup working unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Reddit's RSS rate limit for these feeds, in seconds between requests.
REQUEST_GAP_SECONDS = 60

#: Hour of the day, UTC, after which the daily question list is emailed. The bot
#: only checks at the top of a poll cycle, so a run with a 10 minute interval
#: sends somewhere in the hour rather than exactly on it.
DEFAULT_QUESTIONS_HOUR = 23

#: Filename used for post memory when no bot-level setting names one.
DEFAULT_SEEN_IDS_FILE = "seen_ids.json"


def volume_path(filename: str) -> str:
    """Put a state file on the Railway volume when the platform has one.

    Railway injects RAILWAY_VOLUME_MOUNT_PATH pointing at the attached volume.
    Do not set RAILWAY_VOLUME_MOUNT_PATH yourself, Railway overwrites it.

    Every file the bot has to remember has to go here. The container filesystem
    is thrown away on redeploy, so anything stored outside the volume is silently
    reset, and with it the record of which posts and questions have already been
    handled.
    """
    mount_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if mount_path:
        return os.path.join(mount_path.rstrip("/"), filename)
    return filename


def resolve_seen_ids_path() -> str:
    """Pick where to remember post ids when nothing is configured."""
    return volume_path(DEFAULT_SEEN_IDS_FILE)


@dataclass(frozen=True)
class BotConfig:
    """Everything that makes one listener distinct from another."""

    name: str
    user_agent: str
    env_prefix: str

    subreddits: list[str]
    #: Per-subreddit poll interval in seconds. Anything unlisted uses the
    #: cycle interval, so a lead-generation bot can leave everything fast.
    subreddit_intervals: dict[str, int] = field(default_factory=dict)

    #: Vocabulary that on its own marks a post as worth reading.
    keywords: list[str] = field(default_factory=list)
    #: Vague words that only count when several agree, never alone.
    ambient_keywords: list[str] = field(default_factory=list)

    #: Instructions for the relevance model, written for this bot's purpose.
    relevance_prompt: str = ""

    #: Polling cycle length in seconds. Must be long enough to fetch every
    #: subreddit at the rate limit, or the cycle overruns and logs a warning.
    default_interval: int = 600

    #: Filename used when SEEN_IDS_PATH is not set in the environment.
    default_seen_ids_file: str = DEFAULT_SEEN_IDS_FILE

    #: Whether to pull the questions out of relevant posts and email a list of
    #: guide topics each day. Off by default: a lead-generation bot has no guides
    #: to write, and the question pass costs model calls.
    collect_questions: bool = False

    #: Filename used when QUESTIONS_PATH is not set in the environment.
    default_questions_file: str = "questions.json"

    #: How this bot's audience phrases its questions. Empty falls back to the
    #: shared prompt in :mod:`app.questions`.
    questions_prompt: str = ""

    def env(self, name: str) -> str:
        """Read a bot-scoped variable, falling back to the shared name.

        Blank values count as unset, since a stray space would otherwise win
        over a real value configured for the other bot.
        """
        for candidate in (f"{self.env_prefix}{name}", name):
            value = (os.getenv(candidate) or "").strip()
            if value:
                return value
        return ""

    def env_int(self, name: str, default: int) -> int:
        raw = self.env(name)
        try:
            return int(raw)
        except ValueError:
            return default

    @property
    def seen_ids_path(self) -> str:
        return self.env("SEEN_IDS_PATH") or volume_path(self.default_seen_ids_file)

    @property
    def relevance_model(self) -> str:
        return self.env("RELEVANCE_MODEL") or "openai/gpt-oss-20b"

    @property
    def questions_model(self) -> str:
        """The model that reads the questions out of the posts.

        Defaults to the relevance model so a single-key setup needs no extra
        configuration, but it can be pointed at a cheaper or better one on its
        own since this is a different job from triaging.
        """
        return self.env("QUESTIONS_MODEL") or self.relevance_model

    @property
    def questions_path(self) -> str:
        """Where the question backlog lives.

        Goes on the same volume as the seen-ids file so the backlog survives a
        redeploy. Resetting it would not just lose the topics, it would re-send
        every one of them the next evening.
        """
        return self.env("QUESTIONS_PATH") or volume_path(self.default_questions_file)

    @property
    def questions_recipient_email(self) -> str:
        """Where the daily list goes.

        Defaults to the digest recipient, since it is the same person deciding
        what to write about.
        """
        return self.env("QUESTIONS_RECIPIENT_EMAIL") or self.recipient_email

    @property
    def questions_hour(self) -> int:
        raw_hour = self.env_int("QUESTIONS_HOUR", DEFAULT_QUESTIONS_HOUR)
        return max(0, min(23, raw_hour))

    @property
    def recipient_email(self) -> str:
        return self.env("RECIPIENT_EMAIL")

    @property
    def cycle_interval(self) -> int:
        return self.env_int("INTERVAL", self.default_interval)

    @property
    def sweep_seconds(self) -> int:
        """Seconds a full pass over every subreddit costs, at one request per gap."""
        return max(0, len(self.subreddits) - 1) * REQUEST_GAP_SECONDS