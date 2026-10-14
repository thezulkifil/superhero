"""The question backlog: what people ask, kept so it can become a guide.

Social listening tells you which posts mattered, but a post is not something you
can publish. The question inside one is. This module takes the questions out of
the posts the relevance filter already approved, folds away the duplicates, and
keeps what is left as a list you can write guides from.

The list lives in a JSON file rather than in memory because the point is to
accumulate over time. A topic asked once in March is still worth a guide in
August, so entries survive restarts and are only trimmed once they have been
delivered.

Nothing here talks to the network or reads the clock directly. The caller
supplies the session and the timestamp, which keeps the storage testable and lets
one batch share a single moment.
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, TypedDict

#: How many entries the backlog file may hold. Only delivered questions are ever
#: dropped to get back under this, so the ones still waiting on a guide survive
#: however long the list runs.
QUESTIONS_MAX = 2000

#: Source posts kept per question. Enough to show a question is real without
#: letting one popular topic dominate the file.
MAX_EXAMPLES = 3

#: The guide-topic prompt. This default is about eyewear because that is what the
#: site covers; a bot about anything else supplies its own via ``questions_prompt``
#: on its config.
QUESTIONS_PROMPT = """\
You find article topics for a website about eyeglasses and vision care.
You are given post titles with a short snippet of the body, if the feed had one.
Return the questions those people are actually asking, phrased the way a person \
would type them into a search box.

Keep a question only when a whole article could answer it for a stranger. That \
means it names its subject and asks for information, advice, a comparison, a \
recommendation, a price, or an explanation. "What is the difference between blue \
light blocking lenses and regular lenses?" is a good topic. "Any recommendations?" \
is not, because the answer depends on details the reader does not have.

Each question must make sense on its own, with no reference to the post, the \
poster, or their particular pair of glasses. Supply the subject from the snippet \
when the post left it implicit, but never invent a detail the post does not \
support.

Drop a question when:
- it asks whether something looks good on the poster, such as which frame suits \
their face shape or what colour to pick;
- it needs the poster's own measurements, prescription, budget, or face to be \
answerable;
- it is about the poster's own situation rather than the subject, such as \
"should I take these glasses back";
- it is not a question at all, or is a joke, a poll, or a complaint;
- it is so vague or so tied to the original thread that no article could answer it.

Keep each question under fifteen words, and return at most three per post, and \
only the ones worth writing about.

Reply with only a JSON object shaped like
{"questions": [{"id": "<post id>", "question": "<the question>"}]}.
Never invent an id that was not given to you, and never repeat a question.\
"""


class Question(TypedDict):
    """One guide topic, plus the evidence that people actually asked for it."""

    key: str
    question: str
    #: How many posts asked this. Two people asking the same thing is a stronger
    #: signal than one, and it is what the backlog is ordered by.
    asked: int
    first_seen: str
    last_seen: str
    #: When this was emailed out, or "" while it is still waiting to be written
    #: about. Never a null: the file is hand-editable and a missing key should
    #: read as "not sent yet".
    sent_on: str
    examples: list[dict[str, str]]


#: Insertion-ordered by key so the oldest can be dropped first when trimming,
#: mirroring the seen-ids set in :mod:`app.main`.
Questions = dict[str, Question]

#: A question a stranger could search for, keyed so near-identical phrasings of
#: the same ask collapse into one entry.
_FINDING = re.compile(r"[^\w\s]", re.UNICODE)

#: Conversational openers that carry no meaning, and that would otherwise stop
#: two phrasings of the same question from matching. Phrases are matched as whole
#: words so "sow" survives in "how do I sow seeds".
_FILLER_PHRASES = (
    "quick question",
    "just wondering",
    "anyone know",
    "any advice",
)

_FILLER_OPENERS = frozenset(
    {
        "so",
        "hey",
        "hi",
        "hello",
        "ok",
        "okay",
        "question",
        "wondering",
        "advice",
        "help",
        "pls",
        "please",
    }
)


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}")


#: Punctuation stripped from a word before it is compared against the opener
#: lists. Only the comparison is stripped: the stored wording keeps it, since
#: "What's a pupillary distance?" reads better than "whats a pupillary distance".
_BOUNDARY_MARKS = "\"'“”‘’`.,;:!?()[]{}…"


def _strip_fillers(words: list[str]) -> list[str]:
    """Drop the conversational openers from the front of a question.

    Both the display text and the matching key run through this, which is why it
    compares a punctuation-stripped casefold while leaving the words themselves
    untouched.
    """
    # Both forms are stripped in one loop because they chain: in "so anyone know"
    # the phrase has to go before the next opener is reachable.
    while words:
        bare = [word.casefold().strip(_BOUNDARY_MARKS) for word in words]
        joined = " ".join(bare)
        phrase = next((p for p in _FILLER_PHRASES if joined.startswith(f"{p} ")), None)
        if phrase is not None:
            del words[: len(phrase.split())]
        elif bare[0] in _FILLER_OPENERS:
            words.pop(0)
        else:
            break
    return words


def clean_question(question: str) -> str:
    """Drop the conversational openers, keeping the wording as it was written.

    The digest headline is what gets typed into a CMS, so it has to read as a
    title. The prompt already asks for search phrasing, but a model that passes
    the opener through from the post should not leak it into a guide title.
    """
    return " ".join(_strip_fillers((question or "").split()))


def question_key(question: str) -> str:
    """Fold a question down to a key that ignores phrasing noise.

    Case, punctuation and spacing all go, so "Is blue light blocking worth it?"
    and "is blue light blocking worth it" collapse to one entry. Leading openers
    go too, so "So, anyone know how much are lenses?" and "How much are lenses?"
    are one entry rather than two near-identical guide ideas.

    The key is for matching only. It matches wording, not meaning, so a genuine
    paraphrase such as "w/o insurance" against "without insurance" stays two
    entries until the model rewrites both into one phrasing.
    """
    return " ".join(_strip_fillers(_FINDING.sub(" ", question or "").casefold().split()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _coerce(entry: Any) -> Question | None:
    """Rebuild a complete entry from stored JSON, or drop it.

    The backlog file is meant to be hand-editable so a question can be re-opened
    by clearing its sent_on. That means it can also be hand-broken, so every
    field is checked here rather than trusted further down where a missing key
    would raise deep inside a digest.
    """
    if not isinstance(entry, dict):
        return None

    question = entry.get("question")
    if not isinstance(question, str) or not clean_question(question).strip():
        return None

    raw_examples = entry.get("examples")
    examples = [
        {"title": str(example.get("title") or ""), "link": str(example["link"])}
        for example in (raw_examples if isinstance(raw_examples, list) else [])
        if isinstance(example, dict) and example.get("link")
    ][:MAX_EXAMPLES]

    key = entry.get("key")
    key = key.strip().casefold() if isinstance(key, str) and key.strip() else ""

    return {
        "key": key or question_key(question),
        "question": clean_question(question),
        "asked": _positive_int(entry.get("asked"), 1),
        "first_seen": str(entry.get("first_seen") or ""),
        "last_seen": str(entry.get("last_seen") or ""),
        "sent_on": str(entry.get("sent_on") or ""),
        "examples": examples,
    }


def load_questions(path: str) -> Questions:
    """Restore the backlog so a restart neither loses topics nor re-sends them."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}

    entries = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        log(f"Ignoring malformed {path}; starting with an empty question backlog.")
        return {}

    restored: Questions = {}
    for entry in entries:
        question = _coerce(entry)
        if question is not None:
            restored[question["key"]] = question
    return restored


def save_questions(path: str, questions: Questions) -> None:
    """Persist the backlog, trimming delivered entries to keep the file bounded.

    Undelivered questions are never dropped. They are the ones still waiting on a
    guide, so the file is allowed to sit above the cap rather than discard work.
    """
    excess = len(questions) - QUESTIONS_MAX
    if excess > 0:
        delivered = sorted(
            (entry for entry in questions.values() if entry["sent_on"]),
            key=lambda entry: (entry["sent_on"], entry["key"]),
        )
        for entry in delivered[:excess]:
            del questions[entry["key"]]

    temp_path = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump({"questions": list(questions.values())}, handle, indent=2)
        os.replace(temp_path, path)
    except OSError as exc:
        log(f"Could not save the question backlog to {path}: {exc}")


def add_questions(
    questions: Questions,
    found: dict[str, list[str]],
    sources: dict[str, tuple[str, str]],
    when: str = "",
) -> list[Question]:
    """Fold newly asked questions into the backlog and return the new entries.

    A question already in the backlog only has its tally and example list topped
    up, so a topic stays listed however many people ask it. One timestamp is
    passed in for the whole batch rather than read per entry, so a batch stamped
    across a second boundary still sorts consistently.
    """
    when = when or _now()
    added: list[Question] = []

    for post_id, asked in found.items():
        title, link = sources.get(post_id, ("", ""))
        for text in asked:
            key = question_key(text)
            if not key:
                continue

            existing = questions.get(key)
            if existing is not None:
                existing["asked"] += 1
                existing["last_seen"] = when
                known_links = {example["link"] for example in existing["examples"]}
                if link and link not in known_links:
                    existing["examples"].append({"title": title, "link": link})
                    del existing["examples"][:-MAX_EXAMPLES]
                continue

            entry: Question = {
                "key": key,
                "question": clean_question(text),
                "asked": 1,
                "first_seen": when,
                "last_seen": when,
                "sent_on": "",
                "examples": [{"title": title, "link": link}] if link else [],
            }
            questions[key] = entry
            added.append(entry)

    return added


def due_questions(questions: Questions) -> list[Question]:
    """The questions that have not made it into a digest yet.

    Ordered by how many people asked, so the topic with the most demand is at the
    top of the list rather than whichever post happened to arrive first.
    """
    return sorted(
        (entry for entry in questions.values() if not entry["sent_on"]),
        key=lambda entry: (-entry["asked"], entry["first_seen"], entry["key"]),
    )


def mark_sent(questions: Questions, keys: list[str], when: str = "") -> None:
    """Record that these questions reached the inbox.

    Only called once the email is actually away, so a failed send leaves the whole
    list queued for tomorrow.
    """
    when = when or _now()
    for key in keys:
        entry = questions.get(key)
        if entry is not None:
            entry["sent_on"] = when


def build_questions_email_html(daily: list[Question], backlog_count: int) -> str:
    """Render the daily list: the question itself, then where it came from.

    The question is plain text rather than a link, because it is the deliverable
    and gets typed into a CMS. The source posts sit underneath in small grey text
    so a question nobody has actually asked can be spotted and dropped.
    """
    if not daily:
        body = "<p>No new questions worth a guide.</p>"
    else:
        rows = []
        for entry in daily:
            asked = f"asked {entry['asked']}x" if entry["asked"] > 1 else "asked once"
            sources = "".join(
                f'<a href="{html.escape(example["link"], quote=True)}" '
                f'style="color:#1155cc;text-decoration:none;">'
                f'{html.escape(example["title"] or "source post")}</a> &middot; '
                for example in entry["examples"]
            )
            rows.append(
                '<li style="margin:0 0 14px;">'
                f'<span style="font-size:15px;">{html.escape(entry["question"])}</span>'
                f'<br><span style="color:#888;font-size:12px;">{asked}'
                f'{f" &middot; {sources}" if sources else ""}</span>'
                "</li>"
            )
        body = f'<ol style="margin:0;padding-left:20px;">{"".join(rows)}</ol>'

    footer = (
        f'<p style="color:#888;font-size:12px;margin:20px 0 0;">'
        f"{backlog_count} question(s) in the backlog. Anything you have not written "
        f"about yet comes back tomorrow.</p>"
    )

    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'color:#1a1a1a;font-size:15px;line-height:1.5;">'
        f"<p>Questions people are asking that could each be a guide:</p>"
        f"{body}{footer}</div>"
    )


def build_questions_subject(count: int) -> str:
    label = "question" if count == 1 else "questions"
    return f"{count} new {label} worth a guide"


def send_questions_email(
    daily: list[Question],
    backlog_count: int,
    session: Any,
    recipient_email: str | None = None,
    sender_email: str | None = None,
    cc_emails: list[str] | None = None,
    log_line: Callable[[str], None] = log,
) -> bool:
    """Email the daily list of guide topics.

    Delegates the transport to :mod:`app.email` so both digests share one request
    shape, and returns whether it went out so the caller can decide if the list
    is still outstanding.
    """
    from app.email import send_html_email

    return send_html_email(
        build_questions_subject(len(daily)),
        build_questions_email_html(daily, backlog_count),
        session,
        recipient_email=recipient_email,
        sender_email=sender_email,
        cc_emails=cc_emails,
        log=log_line,
    )
