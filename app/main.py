import argparse
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

import feedparser
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Post(TypedDict):
    """A Reddit post plus the model's verdict on whether it is relevant."""

    id: str
    title: str
    link: str
    summary: str
    relevant: bool

SUBREDDITS = [
    "glasses",
    "glassesadvice",
    "optometry",
    "Frugal",
    "BuyItForLife",
    "Budget",
    "GoodValue",
    "FrugalFemaleFashion",
    "FrugalMaleFashion",
]
CHECK_INTERVAL_SECONDS = 600  # 10 min; covers 9 subreddits at 1 req/60s
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "python:superhero:1.0.0 (by /u/superhero_bot)")
REQUEST_GAP_SECONDS = 60
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 900
# Transient network failures are retried separately from 429s, which the
# RateLimiter already handles on its own escalating schedule.
FETCH_RETRY_DELAYS = (5.0, 15.0)

# Relevance filtering via a small reasoning model on Groq's free plan.
# Free limits are 30 req/min and 1,000 req/day per model, with an 8,000 token
# per minute ceiling that counts the prompt plus whatever we allow the model to
# generate. That ceiling is why outputs are capped and the keyword pass runs first.
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
RELEVANCE_MODEL = os.getenv("RELEVANCE_MODEL", "openai/gpt-oss-20b")
RELEVANCE_BATCH_SIZE = int(os.getenv("RELEVANCE_BATCH_SIZE", "20"))
RELEVANCE_TIMEOUT_SECONDS = int(os.getenv("RELEVANCE_TIMEOUT_SECONDS", "60"))
RELEVANCE_MAX_TOKENS = int(os.getenv("RELEVANCE_MAX_TOKENS", "1024"))
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")
SUMMARY_SNIPPET_CHARS = 100
# The keyword pass is local and free, so it gets a wider window than the model.
SUMMARY_MAX_CHARS = 600
# Groq's token-per-minute window refills within a minute, so a couple of patient
# retries recover batches that would otherwise be dropped for good.
RELEVANCE_RETRY_DELAYS = (20.0, 40.0)

def resolve_seen_ids_path() -> str:
    """Pick where to remember post ids.

    Railway injects RAILWAY_VOLUME_MOUNT_PATH pointing at the attached volume,
    so prefer that over hardcoding a path. Do not set RAILWAY_VOLUME_MOUNT_PATH
    by hand, Railway overwrites it.
    """
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume_path:
        return os.path.join(volume_path.rstrip("/"), "seen_ids.json")
    return "seen_ids.json"


SEEN_IDS_PATH = os.getenv("SEEN_IDS_PATH", "").strip() or resolve_seen_ids_path()
SEEN_IDS_MAX = 5000

DEFAULT_SUBREDDIT_INTERVAL_SECONDS = 600
# Slower subs are polled far less often than the busy frugality ones. Their feeds
# only carry the newest ~25 posts, so a long gap still surfaces everything, just
# later. This is where most of the wall-clock time goes: 9 subs at 1 req/60s would
# otherwise spend 8 of every 10 minutes sleeping on Reddit's rate limit.
SUBREDDIT_INTERVAL_SECONDS = {
    "glasses": 3600,
    "glassesadvice": 3600,
    "optometry": 3600,
}

# Insertion-ordered set of post ids, so the oldest can be dropped first when
# trimming rather than evicted at random.
SeenIds = dict[str, None]

RELEVANCE_SYSTEM_PROMPT = (
    "You triage Reddit posts for a bot that only cares about affordable eyewear.\n"
    "You are given post titles with a short snippet of the body, if the feed had one.\n"
    "Judge each post on what the title and snippet show, and never assume anything "
    "about content that is not shown.\n"
    "Mark a post relevant if it is about any of: eyeglasses, sunglasses, prescription "
    "lenses or frames, lens options or coatings, vision and eye exams, contact lenses, "
    "or buying, repairing, or recommending any of those.\n"
    "Relevant posts give or ask for objective information: prices, deals, brand or model "
    "comparisons, lens specs and coatings, where to buy or repair, insurance and exam "
    "costs, or what other people recommend and had good or bad experiences with.\n"
    "Mark a post irrelevant if it is about anything else, including general frugality, "
    "budgeting, deals, and shopping advice for other products, memes, politics, and "
    "off-topic chatter. A post about cheap clothes or cheap laptops is irrelevant even "
    "though it is cheap.\n"
    "Also mark a post irrelevant if it is mainly asking for a subjective opinion about "
    "how something looks, such as whether a pair of glasses suits the poster, what colour "
    "or frame shape to pick, whether a frame suits their face shape, or whether an outfit "
    "works. Style and taste polls are irrelevant.\n"
    "The distinction is whether the answer is useful to a stranger who cannot see the "
    "poster. A question about which of two named models is cheaper, better made, or wider "
    "is relevant. A question about which of two pairs looks better on the poster is not.\n"
    "Return only the ids of relevant posts. Never invent an id that was not given to you, "
    "and never repeat an id.\n"
    'Reply with only a JSON object shaped like {"relevant_ids": ["<id>", ...]}.'
)

RELEVANCE_SCHEMA = {
    "name": "relevance_decisions",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"relevant_ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["relevant_ids"],
        "additionalProperties": False,
    },
}

# Tried in order until a model accepts one, so swapping in a free model that lacks
# strict structured output degrades instead of failing every request.
RELEVANCE_RESPONSE_FORMATS: list[dict[str, Any] | None] = [
    {"type": "json_schema", "json_schema": RELEVANCE_SCHEMA},
    {"type": "json_object"},
    None,
]
_FORMAT_INDEX_BY_MODEL: dict[str, int] = {}

# The keyword pass is a two-tier intake gate. A single eyewear term admits a
# post on its own, which is what keeps intake high. Ambient words are far too
# common on r/BuyItForLife and r/Frugal to admit anything, so they only count
# when several of them agree. Matching is substring, so stems cover variants:
# "glass" catches glasses and glassing, "lens" catches lens and lenses.
EYEWEAR_KEYWORDS = [
    "glass",
    "eyeglass",
    "spectacl",
    "eyewear",
    "lens",
    "frame",
    "prescription",
    "rx",
    "optical",
    "coating",
    "anti-reflective",
    "blue light",
    "bifocal",
    "progressiv",
    "polariz",
    "single vision",
    "reading glass",
    "contact lens",
    "vision",
    "optician",
    "optometrist",
    "optometry",
    "eye exam",
    "eye doctor",
    "eye test",
    "contact",
    "insert",
    "varifocal",
]

# Generic words that describe the topic only vaguely. One of these is not enough
# on its own; a post needs several, or one of the eyewear terms above.
AMBIENT_KEYWORDS = [
    "repair",
    "repairs",
    "fix",
    "fixing",
    "broken",
    "durable",
    "lasts",
    "lasting",
    "cheap",
    "cheapest",
    "budget",
    "bargain",
    "worth it",
    "deal",
    "value",
    "sharpener",
    "tool",
    "tools",
]
AMBIENT_MATCHES_REQUIRED = 2


def _keyword_list(env_name: str, defaults: list[str]) -> list[str]:
    """Read a comma-separated keyword override, falling back to the defaults."""
    override = [
        keyword.strip().lower()
        for keyword in os.getenv(env_name, "").split(",")
        if keyword.strip()
    ]
    return override or [keyword.lower() for keyword in defaults]


KEYWORDS = _keyword_list("KEYWORDS", EYEWEAR_KEYWORDS)
AMBIENT = _keyword_list("AMBIENT_KEYWORDS", AMBIENT_KEYWORDS)

DEFAULT_SUBREDDITS = SUBREDDITS
DEFAULT_INTERVAL_SECONDS = CHECK_INTERVAL_SECONDS
DEFAULT_RELEVANCE_MODEL = RELEVANCE_MODEL


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

# Shared by the Groq and Brevo calls so neither pays a fresh TLS handshake.
API_SESSION = requests.Session()
API_SESSION.headers.update({"Content-Type": "application/json", "User-Agent": USER_AGENT})


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
    """Fetch one feed, tolerating transient network failures.

    Retrying matters more than it looks: a slow subreddit is only polled once an
    hour, so losing one dropped connection would otherwise cost a full cycle.
    """
    url = f"https://www.reddit.com/r/{subreddit}/.rss"

    # The final None marks the last attempt, which does not sleep afterwards.
    for delay in (*FETCH_RETRY_DELAYS, None):
        limiter.wait_turn()
        try:
            response = SESSION.get(url, timeout=30)
        except requests.RequestException as exc:
            # Connection resets and read timeouts land here. Without this the
            # exception escapes run_bot and takes the whole bot down.
            failure = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 429:
                backoff = limiter.record_throttled(response.headers.get("Retry-After"))
                log(
                    f"Rate limited on r/{subreddit} (429), backing off "
                    f"{backoff:.0f}s (attempt {limiter.consecutive_429})"
                )
                return None

            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                log(f"Failed to fetch r/{subreddit}: {exc}")
                return None

            limiter.record_success()
            return feedparser.parse(response.text)

        if delay is None:
            log(f"Giving up on r/{subreddit} after {len(FETCH_RETRY_DELAYS)} retries: {failure}")
            return None

        log(f"Fetch of r/{subreddit} failed ({failure}); retrying in {delay:.0f}s.")
        time.sleep(delay)

    return None


def fetch_due_subreddit_feeds(
    subreddits: list[str],
    limiter: RateLimiter,
    last_fetched: dict[str, float],
    intervals: dict[str, int] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Fetch only the subreddits whose own polling interval has elapsed.

    The first pass fetches everything, since last_fetched starts empty.
    """
    intervals = intervals or SUBREDDIT_INTERVAL_SECONDS
    feeds: list[tuple[str, dict[str, Any]]] = []
    now = time.monotonic()

    for subreddit in subreddits:
        key = subreddit.lower()
        interval = intervals.get(key, DEFAULT_SUBREDDIT_INTERVAL_SECONDS)
        elapsed = now - last_fetched.get(key, 0.0)

        if last_fetched and elapsed < interval:
            log(f"r/{subreddit} is not due yet, skipping for {interval - elapsed:.0f}s.")
            continue

        feed = fetch_subreddit_feed(subreddit, limiter)
        last_fetched[key] = time.monotonic()
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


def strip_html(text: str) -> str:
    """Flatten Reddit's HTML-heavy RSS summaries into plain text."""
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", without_tags).strip()


def matches_keywords(post: Post, keywords: list[str], ambient: list[str] | None = None) -> bool:
    """First-pass filter on title and summary text.

    One eyewear term is enough. Ambient terms only admit a post when several of
    them agree, so a lone "repair" in a toaster thread does not get promoted.
    """
    haystack = f"{post.get('title', '')} {post.get('summary', '')}".lower()

    if any(keyword.lower() in haystack for keyword in keywords):
        return True

    if not ambient:
        return False

    hits = {keyword for keyword in ambient if keyword.lower() in haystack}
    return len(hits) >= AMBIENT_MATCHES_REQUIRED


def build_relevance_payload(
    posts: list[Post], model: str, response_format: dict[str, Any] | None
) -> dict[str, Any]:
    """Build a Groq chat-completions request for one batch of posts.

    Sends the title plus a short snippet of the body, never the whole thing.
    Full bodies cost several times more in tokens than they add in signal for
    this judgement, and the free tier counts every token.
    """
    candidates = [
        {
            "id": post["id"],
            "title": post.get("title", ""),
            "summary": strip_html(post.get("summary", ""))[:SUMMARY_SNIPPET_CHARS],
        }
        for post in posts
    ]
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Decide which of these post titles are relevant.\n\n"
                    + json.dumps(candidates, indent=2)
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": RELEVANCE_MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    if response_format:
        payload["response_format"] = response_format
    return payload


def extract_json_objects(content: str) -> list[dict[str, Any]]:
    """Decode every JSON object embedded in a model response.

    Uses incremental decoding rather than a greedy regex so a reasoning trace
    with braces before the answer cannot swallow the real payload.
    """
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", content):
        try:
            payload, _ = decoder.raw_decode(content[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            objects.append(payload)
    return objects


def parse_relevant_ids(content: str, known_ids: set[str]) -> set[str]:
    """Pull relevant ids out of a model response, ignoring anything unknown.

    Tolerates reasoning traces and markdown fences around the JSON object so a
    model that ignores the response_format still gets parsed. The last decision
    wins, since a final answer comes after any scratch examples.
    """
    if not content:
        return set()

    payloads = extract_json_objects(content)

    for payload in reversed(payloads):
        if "relevant_ids" not in payload:
            continue
        ids = payload["relevant_ids"]
        if not isinstance(ids, list):
            log("Relevance filter returned 'relevant_ids' as a non-list; ignoring batch.")
            return set()
        return {str(post_id) for post_id in ids if str(post_id) in known_ids}

    log("Relevance filter response had no 'relevant_ids' field; treating batch as irrelevant.")
    return set()


class _RateLimited(Exception):
    """Signals a 429 so the caller can back off instead of dropping the batch."""


def request_relevant_ids(
    posts: list[Post], model: str, timeout: int = RELEVANCE_TIMEOUT_SECONDS
) -> set[str] | None:
    """Ask the model which posts in one batch are relevant.

    Returns None when the call fails, so callers can distinguish "nothing was
    relevant" from "we never got an answer".
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log("GROQ_API_KEY is not set, so posts cannot be filtered. Skipping them.")
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    known_ids = {post["id"] for post in posts}

    # The final None marks the last attempt, which does not sleep afterwards.
    for delay in (*RELEVANCE_RETRY_DELAYS, None):
        try:
            return _attempt_relevance_formats(posts, model, headers, known_ids, timeout)
        except _RateLimited:
            if delay is None:
                log("Groq kept rate limiting us; skipping this batch.")
                return None
            log(f"Rate limited by Groq; retrying in {delay:.0f}s.")
            time.sleep(delay)

    return None


def _attempt_relevance_formats(
    posts: list[Post],
    model: str,
    headers: dict[str, str],
    known_ids: set[str],
    timeout: int,
) -> set[str] | None:
    """Try each request shape once against the model."""
    start = _FORMAT_INDEX_BY_MODEL.get(model, 0)

    for index in range(start, len(RELEVANCE_RESPONSE_FORMATS)):
        response_format = RELEVANCE_RESPONSE_FORMATS[index]

        try:
            response = API_SESSION.post(
                GROQ_URL,
                headers=headers,
                json=build_relevance_payload(posts, model, response_format),
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                raise _RateLimited from exc
            if _is_shape_rejection(exc) and response_format is not None:
                log(f"{model} rejected the request shape; retrying with fewer options.")
                continue
            log(f"Relevance filter request failed: {exc}")
            return None
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            log(f"Relevance filter request failed: {exc}")
            return None

        _FORMAT_INDEX_BY_MODEL[model] = index
        return parse_relevant_ids(content, known_ids)

    log(f"{model} rejected every request shape; skipping this batch.")
    return None


def _is_shape_rejection(exc: requests.HTTPError) -> bool:
    """True when a 400 is the provider refusing optional request fields.

    Groq does not accept strict JSON schemas or a reasoning_effort on every
    model, so step down to a simpler request rather than failing the batch.
    """
    response = exc.response
    if response is None or response.status_code != 400:
        return False
    body = (response.text or "").lower()
    return any(
        token in body
        for token in ("response_format", "json_schema", "reasoning_effort", "unsupported")
    )


def filter_relevant_posts(
    posts: list[Post], model: str = DEFAULT_RELEVANCE_MODEL
) -> set[str]:
    """Return the ids of posts a small reasoning model judges relevant.

    Fails closed: posts are only kept when the model positively identifies them,
    so a missing key or an API outage never floods the inbox.
    """
    if not posts:
        return set()

    relevant: set[str] = set()
    for start in range(0, len(posts), RELEVANCE_BATCH_SIZE):
        batch = posts[start : start + RELEVANCE_BATCH_SIZE]
        batch_ids = request_relevant_ids(batch, model)
        if batch_ids is None:
            log(f"Skipping {len(batch)} post(s) that could not be filtered.")
            continue
        relevant.update(batch_ids)
        log(f"Relevance filter kept {len(batch_ids)}/{len(batch)} post(s).")

    return relevant


def load_seen_ids(path: str) -> SeenIds:
    """Restore post ids from disk so a restart does not re-pay for old posts."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        log(f"Ignoring malformed {path}; starting with no remembered posts.")
        return {}
    return {str(post_id): None for post_id in raw[:SEEN_IDS_MAX]}


def save_seen_ids(path: str, seen_ids: SeenIds) -> None:
    """Persist post ids, trimming the oldest entries to keep the file bounded."""
    excess = len(seen_ids) - SEEN_IDS_MAX
    if excess > 0:
        for stale in list(seen_ids)[:excess]:
            del seen_ids[stale]

    temp_path = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(list(seen_ids), handle)
        os.replace(temp_path, path)
    except OSError as exc:
        log(f"Could not save seen post ids to {path}: {exc}")


def get_new_entries(feed: dict[str, Any], seen_ids: SeenIds) -> list[Post]:
    """Return posts not seen before, marking them seen so they are never re-checked.

    Posts start as not relevant; the model filter flips the flag.
    """
    new_entries: list[Post] = []

    for entry in feed.get("entries", []):
        entry_id = get_entry_id(entry)
        if not entry_id or entry_id in seen_ids:
            continue

        seen_ids[entry_id] = None
        new_entries.append(
            {
                "id": entry_id,
                "title": entry.get("title", "(untitled)"),
                "link": entry.get("link", ""),
                "summary": strip_html(entry.get("summary", ""))[:SUMMARY_MAX_CHARS],
                "relevant": False,
            }
        )

    return new_entries


def parse_email_list(raw: str | None) -> list[str]:
    """Parse a comma-separated list of emails into a clean list."""
    if not raw:
        return []
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def render_post_list(posts: list[Post], show_verdict: bool) -> str:
    """Render posts as a linked list, optionally tagging the filtered ones."""
    items = []
    for post in posts:
        title = html.escape(post.get("title") or "(untitled)")
        link = html.escape(post.get("link") or "", quote=True)
        verdict = ""
        if show_verdict and not post.get("relevant"):
            verdict = ' <span style="color:#888;">(filtered out)</span>'
        items.append(f'<li style="margin:0 0 8px;"><a href="{link}" style="color:#1155cc;">{title}</a>{verdict}</li>')
    if not items:
        return "<p>Nothing to show.</p>"
    return f'<ul style="margin:0;padding-left:20px;">{"".join(items)}</ul>'


# A checkbox and the :checked sibling selector is the only toggle that survives
# email clients, which strip scripts. Unchecked by default, so the filtered list
# is what everyone sees; clients without :checked support simply keep it that way.
EMAIL_CSS = """
.sh-toggle{position:absolute;opacity:0;width:1px;height:1px;overflow:hidden}
.sh-all{display:none}
.sh-toggle:checked~.sh-all{display:block}
.sh-toggle:checked~.sh-filtered{display:none}
.sh-btn{display:inline-block;padding:8px 14px;border:1px solid #ccc;border-radius:6px;
background:#f6f6f6;color:#333;font-size:13px;cursor:pointer}
"""


def build_email_html(posts: list[Post]) -> str:
    """Build the digest body: filtered posts by default, all posts behind a toggle."""
    relevant = [post for post in posts if post.get("relevant")]
    total = len(posts)
    relevant_label = "post" if len(relevant) == 1 else "posts"
    total_label = "post" if total == 1 else "posts"
    hidden = total - len(relevant)

    intro = (
        f"<p>Found <strong>{len(relevant)}</strong> relevant {relevant_label}"
        f" out of {total} seen this hour.</p>"
    )

    if not relevant:
        body = "<p>No relevant posts found.</p>"
        toggle = ""
    elif hidden == 0:
        body = render_post_list(relevant, show_verdict=False)
        toggle = ""
    else:
        toggle = (
            f'<input type="checkbox" id="shAll" class="sh-toggle">'
            f'<p style="margin:16px 0 8px;">'
            f'<label for="shAll" class="sh-btn">Show all {total} {total_label} '
            f"({hidden} filtered out)</label></p>"
        )
        body = (
            f'<div class="sh-filtered">{render_post_list(relevant, show_verdict=False)}</div>'
            f'<div class="sh-all">{render_post_list(posts, show_verdict=True)}</div>'
        )

    return (
        f"<style>{EMAIL_CSS}</style>"
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'color:#1a1a1a;font-size:15px;line-height:1.5;">'
        f"{intro}{toggle}{body}</div>"
    )


def build_email_subject(posts: list[Post]) -> str:
    relevant_count = sum(1 for post in posts if post.get("relevant"))
    label = "post" if relevant_count == 1 else "posts"
    return f"Superhero found {relevant_count} new {label}"


def send_email_notification(collected_posts: list[Post]) -> None:
    """Send email notification with collected posts via Brevo API."""
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("SENDER_EMAIL")
    recipient_email = os.getenv("RECIPIENT_EMAIL")
    cc_emails = parse_email_list(os.getenv("CC_EMAILS"))

    if not api_key or not sender_email or not recipient_email:
        log("Email not configured. Skipping notification.")
        log(f"Collected {len(collected_posts)} posts:")
        for post in collected_posts:
            verdict = "" if post.get("relevant") else " (filtered out)"
            print(f"  - {post['title']} -> {post['link']}{verdict}")
        return

    html_body = build_email_html(collected_posts)
    subject = build_email_subject(collected_posts)

    payload = {
        "sender": {"email": sender_email},
        "to": [{"email": recipient_email}],
        "subject": subject,
        "htmlContent": html_body,
    }

    if cc_emails:
        payload["cc"] = [{"email": addr} for addr in cc_emails]

    try:
        response = API_SESSION.post(
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


def run_bot(subreddits: list[str], interval_seconds: int = DEFAULT_INTERVAL_SECONDS, once: bool = False, relevance_model: str = DEFAULT_RELEVANCE_MODEL, keywords: list[str] | None = None) -> None:
    keywords = list(keywords or KEYWORDS)
    seen_ids = load_seen_ids(SEEN_IDS_PATH)
    if seen_ids:
        log(f"Remembered {len(seen_ids)} previously seen post(s).")
    collected_posts: list[Post] = []
    last_notification_hour = None
    limiter = RateLimiter()
    last_fetched: dict[str, float] = {}
    
    while True:
        cycle_started_at = time.monotonic()

        # Check if it's time to send notification (hourly, only when relevant posts were found)
        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        if last_notification_hour != current_hour:
            if any(post["relevant"] for post in collected_posts):
                kept = sum(1 for post in collected_posts if post["relevant"])
                log(f"Sending hourly notification with {kept} relevant of {len(collected_posts)} post(s)...")
                send_email_notification(collected_posts)
                collected_posts = []  # Reset collection
                last_notification_hour = current_hour
            else:
                # Nothing relevant yet this hour; retry next cycle without marking the hour as done
                log("No relevant posts collected, skipping hourly notification.")
        
        # Fetch every due feed, then judge them in one pass so the model sees a
        # single batch per cycle instead of one request per subreddit.
        feeds = fetch_due_subreddit_feeds(subreddits, limiter, last_fetched)

        new_entries: list[Post] = []
        candidates: list[Post] = []

        for subreddit, feed in feeds:
            try:
                entries = get_new_entries(feed, seen_ids)

                if not entries:
                    log(f"No new posts in r/{subreddit}.")
                    continue

                log(f"Found {len(entries)} new post(s) in r/{subreddit}.")
                hits = [
                    post
                    for post in entries
                    if matches_keywords(post, keywords, AMBIENT)
                ]
                if len(hits) < len(entries):
                    log(f"{len(hits)}/{len(entries)} matched a keyword.")

                new_entries.extend(entries)
                candidates.extend(hits)
            except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - defensive catch for unexpected feed issues
                log(f"Error checking r/{subreddit}: {exc}")

        if candidates:
            log(f"Asking the model about {len(candidates)} candidate post(s)...")
            relevant_ids = filter_relevant_posts(candidates, relevance_model)
        else:
            relevant_ids = set()

        for post in new_entries:
            post["relevant"] = post["id"] in relevant_ids

        if new_entries:
            kept = [post for post in new_entries if post["relevant"]]
            for post in kept:
                print(f"  - {post['title']} -> {post['link']}")
                log(f"Saved post: {post['title']}")

            # Keep every post so the email toggle can reveal what was filtered out.
            collected_posts.extend(new_entries)
            save_seen_ids(SEEN_IDS_PATH, seen_ids)
            log(f"{len(kept)}/{len(new_entries)} post(s) were relevant.")

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
    parser = argparse.ArgumentParser(
        description="Poll Reddit subreddit RSS feeds and keep the posts a model considers relevant."
    )
    parser.add_argument(
        "--subreddits",
        nargs="+",
        default=DEFAULT_SUBREDDITS,
        help="Reddit subreddits to monitor, e.g. glasses glassesadvice optometry",
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
        "--relevance-model",
        default=DEFAULT_RELEVANCE_MODEL,
        help=(
            "Groq model used to judge whether a post is relevant. "
            f"Default is {DEFAULT_RELEVANCE_MODEL}."
        ),
    )
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=KEYWORDS,
        help="Keywords for the first-pass filter (case-insensitive). The model checks these.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_bot(args.subreddits, args.interval, args.once, args.relevance_model, args.keywords)
