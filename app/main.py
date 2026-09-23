import argparse
import time
from datetime import datetime
from typing import Any

import feedparser
import requests

SUBREDDIT = "python"
CHECK_INTERVAL_SECONDS = 60
USER_AGENT = "superhero-bot/0.1"

DEFAULT_SUBREDDIT = SUBREDDIT
DEFAULT_INTERVAL_SECONDS = CHECK_INTERVAL_SECONDS


def fetch_subreddit_feed(subreddit: str) -> dict[str, Any]:
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return feedparser.parse(response.text)


def get_entry_id(entry: dict[str, Any]) -> str:
    for key in ("id", "guid", "link", "title"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def get_new_entries(feed: dict[str, Any], seen_ids: set[str]) -> list[dict[str, str]]:
    new_entries: list[dict[str, str]] = []

    for entry in feed.get("entries", []):
        entry_id = get_entry_id(entry)
        if not entry_id or entry_id in seen_ids:
            continue

        seen_ids.add(entry_id)
        new_entries.append(
            {
                "id": entry_id,
                "title": entry.get("title", "(untitled)"),
                "link": entry.get("link", ""),
            }
        )

    return new_entries


def run_bot(subreddit: str, interval_seconds: int = DEFAULT_INTERVAL_SECONDS, once: bool = False) -> None:
    seen_ids: set[str] = set()

    while True:
        try:
            feed = fetch_subreddit_feed(subreddit)
            new_entries = get_new_entries(feed, seen_ids)

            if not new_entries:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] No new posts in r/{subreddit}.")
            else:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] New posts in r/{subreddit}:")
                for post in new_entries:
                    print(f"- {post['title']} -> {post['link']}")
        except requests.RequestException as exc:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] Request error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive catch for unexpected feed issues
            print(f"[{datetime.now().isoformat(timespec='seconds')}] Error checking r/{subreddit}: {exc}")

        if once:
            return

        time.sleep(interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll a Reddit subreddit RSS feed every minute.")
    parser.add_argument("--subreddit", default=DEFAULT_SUBREDDIT, help="Reddit subreddit to monitor, e.g. python")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Polling interval in seconds. Default is 60.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once and exit instead of polling continuously.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_bot(args.subreddit, args.interval, args.once)
