"""Bot definitions and lookup."""

from __future__ import annotations

from app.bots.business import BUSINESS
from app.bots.superhero import SUPERHERO
from app.config import BotConfig

BOTS: dict[str, BotConfig] = {
    SUPERHERO.name: SUPERHERO,
    BUSINESS.name: BUSINESS,
}

DEFAULT_BOT = SUPERHERO.name


def get_bot(name: str) -> BotConfig:
    try:
        return BOTS[name]
    except KeyError:
        available = ", ".join(sorted(BOTS))
        raise KeyError(f"Unknown bot {name!r}. Available: {available}") from None