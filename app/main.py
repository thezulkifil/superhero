import argparse
import os
import time
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

SUBREDDITS = [
    "glasses",
    "glassesadvice",
    "eyeglasses",
    "optometry",
    "Frugal",
    "BuyItForLife",
    "Budget",
    "GoodValue",
    "FrugalFemaleFashion",
    "FrugalMaleFashion",
]
CHECK_INTERVAL_SECONDS = 600  # 10 min; covers 10 subreddits at 1 req/60s
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "python:superhero:1.0.0 (by /u/superhero_bot)")
REQUEST_GAP_SECONDS = 60
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 900
KEYWORDS = [
    "affordable glasses",
    "cheap glasses",
    "affordable eyeglasses",
    "bifocals",
    "progressive",
    "prescription glasses",
    "eyeglasses",
    "prescription eyeglasses",
    "sunglasses",
    "prescription sunglasses",
    "lenses",
    "prescription lenses",
    "lens options",
    "tinted lens",
    "progressive lens",
    "bifocals lens",
    "coating",
    "blue light coating",
]

DEFAULT_SUBREDDITS = SUBREDDITS
DEFAULT_INTERVAL_SECONDS = CHECK_INTERVAL_SECONDS
DEFAULT_KEYWORDS = KEYWORDS


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}")


SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml,application/xml,text/xml,*/*",
    }
)


class RateLimiter:
    """Paces Reddit requests and escalates backoff when throttled.

    The cooldown is shared across every request and persists between poll
    cycles, so a 429 cannot be retried into a permanent lockout.
    """

    def __init__(
        self,
        gap: float = REQUEST_GAP_SECONDS,
        base_backoff: float = BASE_BACKOFF_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self.gap = gap
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.last_request_at = 0.0
        self.consecutive_429 = 0
        self.penalty_until = 0.0

    def wait_turn(self) -> None:
        """Block until it is safe to issue the next request."""
        deadline = max(self.last_request_at + self.gap, self.penalty_until)
        now = time.monotonic()
        if deadline > now:
            time.sleep(deadline - now)
        self.last_request_at = time.monotonic()

    def record_success(self) -> None:
        self.consecutive_429 = 0
        self.penalty_until = 0.0

    def record_throttled(self, retry_after: str | None = None) -> float:
        self.consecutive_429 += 1
        delay = min(self.base_backoff * 2 ** (self.consecutive_429 - 1), self.max_backoff)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        self.penalty_until = time.monotonic() + delay
        return delay


def fetch_subreddit_feed(subreddit: str, limiter: RateLimiter) -> dict[str, Any] | None:
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    limiter.wait_turn()
    response = SESSION.get(url, timeout=30)

    if response.status_code == 429:
        delay = limiter.record_throttled(response.headers.get("Retry-After"))
        log(
            f"Rate limited on r/{subreddit} (429), backing off "
            f"{delay:.0f}s (attempt {limiter.consecutive_429})"
        )
        return None

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        log(f"Failed to fetch r/{subreddit}: {exc}")
        return None

    limiter.record_success()
    return feedparser.parse(response.text)


def fetch_multiple_subreddit_feeds(
    subreddits: list[str], limiter: RateLimiter | None = None
) -> list[tuple[str, dict[str, Any]]]:
    limiter = limiter or RateLimiter()
    feeds = []
    for subreddit in subreddits:
        feed = fetch_subreddit_feed(subreddit, limiter)
        if feed is not None:
            feeds.append((subreddit, feed))
        else:
            log(f"Skipping r/{subreddit} for this cycle")
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


def parse_email_list(raw: str | None) -> list[str]:
    """Parse a comma-separated list of emails into a clean list."""
    if not raw:
        return []
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def send_email_notification(collected_posts: list[dict[str, str]]) -> None:
    """Send email notification with collected posts via Brevo API."""
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("SENDER_EMAIL")
    recipient_email = os.getenv("RECIPIENT_EMAIL")
    cc_emails = parse_email_list(os.getenv("CC_EMAILS"))
    
    if not api_key or not sender_email or not recipient_email:
        log("Email not configured. Skipping notification.")
        log(f"Collected {len(collected_posts)} posts:")
        for post in collected_posts:
            print(f"  - {post['title']} -> {post['link']}")
        return
    
    # Build email content
    post_count = len(collected_posts)
    post_label = "post" if post_count == 1 else "posts"
    subject = f"Superhero found {post_count} new {post_label}"
    
    if not collected_posts:
        html_body = "<p>No matching posts found.</p>"
    else:
        html_body = f"<p>Found <strong>{post_count}</strong> matching {post_label}:</p>"
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

    if cc_emails:
        payload["cc"] = [{"email": addr} for addr in cc_emails]
    
    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"accept": "application/json", "api-key": api_key},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        cc_note = f" (cc: {', '.join(cc_emails)})" if cc_emails else ""
        log(f"Email sent successfully to {recipient_email}{cc_note}")
    except requests.RequestException as exc:
        log(f"Failed to send email: {exc}")


def run_bot(subreddits: list[str], interval_seconds: int = DEFAULT_INTERVAL_SECONDS, once: bool = False, keywords: list[str] | None = None) -> None:
    seen_ids: set[str] = set()
    collected_posts: list[dict[str, str]] = []
    last_notification_hour = None
    limiter = RateLimiter()
    
    while True:
        cycle_started_at = time.monotonic()

        # Check if it's time to send notification (hourly, only when posts were found)
        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        if last_notification_hour != current_hour:
            if collected_posts:
                log(f"Sending hourly notification with {len(collected_posts)} post(s)...")
                send_email_notification(collected_posts)
                collected_posts = []  # Reset collection
                last_notification_hour = current_hour
            else:
                # Nothing collected yet this hour; retry next cycle without marking the hour as done
                log("No posts collected, skipping hourly notification.")
        
        # Fetch and process feeds
        feeds = fetch_multiple_subreddit_feeds(subreddits, limiter)
        
        for subreddit, feed in feeds:
            try:
                new_entries = get_new_entries(feed, seen_ids, keywords)

                if not new_entries:
                    log(f"No new posts in r/{subreddit}.")
                else:
                    log(f"Found {len(new_entries)} new post(s) in r/{subreddit}:")
                    for post in new_entries:
                        print(f"  - {post['title']} -> {post['link']}")
                        collected_posts.append(post)
                        log(f"Saved post: {post['title']}")
            except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - defensive catch for unexpected feed issues
                log(f"Error checking r/{subreddit}: {exc}")

        if once:
            return

        # Sleep for the remaining time to maintain consistent interval
        elapsed = time.monotonic() - cycle_started_at
        sleep_duration = interval_seconds - elapsed
        if sleep_duration > 0:
            time.sleep(sleep_duration)
        else:
            log(
                f"Cycle took {elapsed:.0f}s, exceeding {interval_seconds}s interval. "
                f"Increasing --interval to at least {int(elapsed) + 60}s."
            )


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
        help="Polling interval in seconds. Default is 600 (10 minutes).",
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
