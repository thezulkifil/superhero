"""The business listener: small businesses signalling they need a service.

This is lead generation rather than news monitoring. A post only counts when the
poster is signalling a need someone can be hired for, not merely talking about
running a business.
"""

from __future__ import annotations

from app.config import BotConfig

RELEVANCE_PROMPT = """\
You triage Reddit posts for a bot that finds business service leads.
You are given post titles with a short snippet of the body, if the feed had one. \
Judge each post on what the title and snippet show, and never assume anything \
about content that is not shown.
The bot looks for people who need to hire help. Mark a post relevant when the \
poster signals a need someone can be paid to solve, for example: looking for a \
web designer, developer, or agency; needs a logo, brand identity, or design work; \
needs help with marketing, SEO, ads, social media, or email campaigns; needs a \
virtual assistant, customer support, or outsourced help; struggles with getting \
more customers, leads, or sales; is choosing between hiring an employee and \
contracting; is searching for tools or recommendations for any of the above.
Prefer posts where the need is explicit and the poster is the decision maker, such \
as "looking for someone to", "need help with", "any recommendations for", "how do I \
find a", or "recommend an agency or freelancer".
Mark a post irrelevant when the poster is not seeking help: success stories and \
announcements, questions about regulations or tax that need an accountant or lawyer \
rather than a service you would sell, hiring or job-seeking posts, students asking \
for school assignments, venting without asking for help, memes, and general business \
philosophy or motivation discussion.
A post asking which hosting platform to use is only relevant if the poster is \
choosing for a real business they run. A post about starting a business in general, \
with no service need, is irrelevant.
Return only the ids of relevant posts. Never invent an id that was not given to you, \
and never repeat an id.
Reply with only a JSON object shaped like {"relevant_ids": ["<id>", ...]}.
"""

BUSINESS = BotConfig(
    name="business",
    # Must not reuse the superhero agent; Reddit expects one per application.
    user_agent="python:businessleads:1.0.0 (by /u/superhero_bot)",
    env_prefix="BUSINESS_",
    subreddits=[
        # Owner-pain subs, the densest source of people with a real problem.
        "smallbusiness",
        "smallbusinessadvice",
        "Entrepreneur",
        "EntrepreneurRideAlong",
        # Product and commerce businesses that buy services by default.
        "startups",
        "SaaS",
        "EtsySellers",
        "Shopify",
        "ecommerce",
        "printondemand",
        # The service categories themselves, where prospects ask directly.
        "marketing",
        "branding",
        "logodesign",
        "seo",
        "PPC",
        "web_design",
        "webdev",
        "freelance",
        "agencies",
    ],
    # Every sub stays on the fast cadence. A lead an hour late is one that
    # someone else has already answered. At this breadth the cycle is paced by
    # Reddit's 1 req/60s rather than by the work, so keep the interval at 600 and
    # let the RateLimiter hold the line.
    subreddit_intervals={},
    keywords=[
        "need",
        "needs",
        "looking for",
        "searching for",
        "anyone know",
        "recommend",
        "recommendation",
        "recommendations",
        "hire",
        "hiring",
        "contractor",
        "contractor",
        "freelance",
        "freelancer",
        "agency",
        "agency",
        "studio",
        "consultant",
        "consultancy",
        "outsource",
        "outsourcing",
        "help",
        "website",
        "web design",
        "webdesigner",
        "web developer",
        "developer",
        "redesign",
        "logo",
        "branding",
        "brand",
        "identity",
        "design",
        "designer",
        "marketing",
        "seo",
        "ads",
        "advertising",
        "social media",
        "email campaign",
        "lead",
        "leads",
        "customers",
        "sales",
        "conversion",
        "virtual assistant",
        "customer support",
        "customer service",
        "outsourced",
        "shopify",
        "etsy",
        "ecommerce",
    ],
    # Business-speak that appears constantly without any intent to hire.
    ambient_keywords=[
        "small business",
        "my business",
        "my store",
        "my shop",
        "my company",
        "side hustle",
        "client",
        "clients",
        "customer",
        "customers",
        "revenue",
        "profit",
        "growth",
        "struggling",
        "frustrated",
        "stuck",
        "advice",
        "tips",
        "beginner",
        "solo",
        "burnout",
        "overwhelmed",
        "busy",
        "expensive",
        "budget",
        "cheap",
        "worth it",
        "tool",
        "tools",
        "software",
        "platform",
    ],
    relevance_prompt=RELEVANCE_PROMPT,
    # Nineteen subreddits at one request per minute need eighteen minutes to
    # sweep. A shorter interval would overrun every cycle.
    default_interval=1200,
    default_seen_ids_file="business_seen_ids.json",
)