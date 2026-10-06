"""The superhero listener: affordable eyewear posts."""

from __future__ import annotations

from app.config import BotConfig

RELEVANCE_PROMPT = """\
You triage Reddit posts for a bot that only cares about affordable eyewear.
You are given post titles with a short snippet of the body, if the feed had one.
Judge each post on what the title and snippet show, and never assume anything \
about content that is not shown.
Mark a post relevant if it is about any of: eyeglasses, sunglasses, prescription \
lenses or frames, lens options or coatings, vision and eye exams, contact lenses, \
or buying, repairing, or recommending any of those.
Relevant posts give or ask for objective information: prices, deals, brand or model \
comparisons, lens specs and coatings, where to buy or repair, insurance and exam \
costs, or what other people recommend and had good or bad experiences with.
Mark a post irrelevant if it is about anything else, including general frugality, \
budgeting, deals, and shopping advice for other products, memes, politics, and \
off-topic chatter. A post about cheap clothes or cheap laptops is irrelevant even \
though it is cheap.
Also mark a post irrelevant if it is mainly asking for a subjective opinion about \
how something looks, such as whether a pair of glasses suits the poster, what colour \
or frame shape to pick, whether a frame suits their face shape, or whether an outfit \
works. Style and taste polls are irrelevant.
The distinction is whether the answer is useful to a stranger who cannot see the \
poster. A question about which of two named models is cheaper, better made, or wider \
is relevant. A question about which of two pairs looks better on the poster is not.
Return only the ids of relevant posts. Never invent an id that was not given to you, \
and never repeat an id.
Reply with only a JSON object shaped like {"relevant_ids": ["<id>", ...]}.
"""

SUPERHERO = BotConfig(
    name="superhero",
    # Reddit requires a distinct User-Agent per application. Two bots sharing one
    # is a rules violation and gets the duplicate throttled or blocked.
    user_agent="python:superhero:1.0.0 (by /u/superhero_bot)",
    env_prefix="SUPERHERO_",
    subreddits=[
        "glasses",
        "glassesadvice",
        "optometry",
        "Frugal",
        "BuyItForLife",
        "Budget",
        "GoodValue",
        "FrugalFemaleFashion",
        "FrugalMaleFashion",
    ],
    # Slow subs are polled hourly; their feeds only carry the newest posts.
    subreddit_intervals={
        "glasses": 3600,
        "glassesadvice": 3600,
        "optometry": 3600,
    },
    keywords=[
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
    ],
    ambient_keywords=[
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
    ],
    relevance_prompt=RELEVANCE_PROMPT,
    default_seen_ids_file="superhero_seen_ids.json",
)