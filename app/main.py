import argparse
import os
import time
from datetime import datetime
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

SUBREDDITS = ["glasses", "eyeglasses", "optometry", "ContactLenses"]
CHECK_INTERVAL_SECONDS = 300  # 5 minutes - Reddit RSS limit is 1 req per ~60s
USER_AGENT = "superhero-bot/0.1"
KEYWORDS = ["affordable glasses", "cheap glasses", "affordable eyeglasses"]

# Pakistan Standard Time is UTC+5
# 12PM PKT = 7AM UTC
NOTIFY_HOUR_UTC = 7
NOTIFY_MINUTE_UTC = 0

DEFAULT_SUBREDDITS = SUBREDDITS
DEFAULT_INTERVAL_SECONDS = CHECK_INTERVAL_SECONDS
DEFAULT_KEYWORDS = KEYWORDS


def fetch_subreddit_feed(subreddit: str) -> dict[str, Any]:
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return feedparser.parse(response.text)


def fetch_multiple_subreddit_feeds(subreddits: list[str]) -> list[tuple[str, dict[str, Any]]]:
    feeds = []
    for i, subreddit in enumerate(subreddits):
        try:
            feed = fetch_subreddit_feed(subreddit)
            feeds.append((subreddit, feed))
        except requests.RequestException as exc:
            if "429" in str(exc):
                print(f"[{datetime.now().isoformat(timespec='seconds')}] Rate limited on r/{subreddit}, waiting 60s...")
                time.sleep(60)
            else:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] Failed to fetch r/{subreddit}: {exc}")
        
        # Reddit RSS limit: 1 request per ~60 seconds
        if i < len(subreddits) - 1:
            time.sleep(60)
    
    return feeds


def get_entry_id(entry: dict[str, Any]) -> str:
    for key in ("id", "guid", "link", "title"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def matches_keywords(entry: dict[str, Any], keywords: list[str]) -> bool:
    title = entry.get("title", "").lower()
    summary = entry.get("summary", "").lower()
    text = f"{title} {summary}"
    return any(kw.lower() in text for kw in keywords)


def get_new_entries(feed: dict[str, Any], seen_ids: set[str], keywords: list[str] | None = None) -> list[dict[str, str]]:
    new_entries: list[dict[str, str]] = []

    for entry in feed.get("entries", []):
        entry_id = get_entry_id(entry)
        if not entry_id or entry_id in seen_ids:
            continue

        if keywords and not matches_keywords(entry, keywords):
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


def send_email_notification(collected_posts: list[dict[str, str]]) -> None:
    """Send email notification with collected posts via Brevo API."""
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("SENDER_EMAIL")
    recipient_email = os.getenv("RECIPIENT_EMAIL")
    
    if not all([api_key, sender_email, recipient_email]):
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Email not configured. Skipping notification.")
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Collected {len(collected_posts)} posts:")
        for post in collected_posts:
            print(f"  - {post['title']} -> {post['link']}")
        return
    
    # Build email content
    subject = "Superhero: Here is your daily list."
    
    if not collected_posts:
        html_body = "<p>No matching posts found today.</p>"
    else:
        html_body = f"<p>Found <strong>{len(collected_posts)}</strong> matching post(s) today:</p>"
        html_body += "<ul>"
        for post in collected_posts:
            html_body += f'<li><a href="{post["link"]}">{post["title"]}</a></li>'
        html_body += "</ul>"
    
    payload = {
        "sender": {"email": sender_email},
        "to": [{"email": recipient_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    
    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"accept": "application/json", "api-key": api_key},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Email sent successfully to {recipient_email}")
    except requests.RequestException as exc:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Failed to send email: {exc}")


def is_notification_time() -> bool:
    """Check if it's time to send the daily notification (12PM PKT = 7AM UTC)."""
    now = datetime.now(datetime.UTC)
    return now.hour == NOTIFY_HOUR_UTC and now.minute == NOTIFY_MINUTE_UTC


def run_bot(subreddits: list[str], interval_seconds: int = DEFAULT_INTERVAL_SECONDS, once: bool = False, keywords: list[str] | None = None) -> None:
    seen_ids: set[str] = set()
    collected_posts: list[dict[str, str]] = []
    last_notification_date = None
    
    while True:
        # Check if it's time to send notification
        now = datetime.now(datetime.UTC)
        if is_notification_time() and last_notification_date != now.date():
            print(f"[{datetime.now().isoformat(timespec='seconds')}] Sending daily notification...")
            send_email_notification(collected_posts)
            collected_posts = []  # Reset collection
            last_notification_date = now.date()
        
        # Fetch and process feeds
        feeds = fetch_multiple_subreddit_feeds(subreddits)
        
        for subreddit, feed in feeds:
            try:
                new_entries = get_new_entries(feed, seen_ids, keywords)

                if not new_entries:
                    print(f"[{datetime.now().isoformat(timespec='seconds')}] No new posts in r/{subreddit}.")
                else:
                    print(f"[{datetime.now().isoformat(timespec='seconds')}] Found {len(new_entries)} new post(s) in r/{subreddit}:")
                    for post in new_entries:
                        print(f"  - {post['title']} -> {post['link']}")
                        collected_posts.append(post)
            except Exception as exc:  # pragma: no cover - defensive catch for unexpected feed issues
                print(f"[{datetime.now().isoformat(timespec='seconds')}] Error checking r/{subreddit}: {exc}")

        if once:
            return

        time.sleep(interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll Reddit subreddit RSS feeds for keyword matches.")
    parser.add_argument(
        "--subreddits",
        nargs="+",
        default=DEFAULT_SUBREDDITS,
        help="Reddit subreddits to monitor, e.g. glasses eyeglasses optometry",
    )
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
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=DEFAULT_KEYWORDS,
        help="Keywords to filter posts by (case-insensitive).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_bot(args.subreddits, args.interval, args.once, args.keywords)
