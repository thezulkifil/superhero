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

    #: Filename used when SEEN_IDS_PATH is not set in the environment.
    default_seen_ids_file: str = "seen_ids.json"

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
        return self.env("SEEN_IDS_PATH") or self.default_seen_ids_file

    @property
    def relevance_model(self) -> str:
        return self.env("RELEVANCE_MODEL") or "openai/gpt-oss-20b"

    @property
    def recipient_email(self) -> str:
        return self.env("RECIPIENT_EMAIL")

    @property
    def cycle_interval(self) -> int:
        return self.env_int("INTERVAL", 600)