"""
Dedicated Xbox Elite Series 2 Vinted -> Discord scanner.

This reuses the existing Xbox scanner engine so Discord embeds, seller/rating
enrichment, Cloudflare handling, 5-second polling and telemetry stay identical.

It runs as a separate Python process/workflow, so it does not add requests to
or slow down the normal Xbox controller scanner or the PS5 scanner.
"""

import re

import vinted_discord_alert as bot


# ──────────────────────────────────────────────
# ELITE SERIES 2 SCANNER CONFIG
# ──────────────────────────────────────────────
ELITE_DISCORD_CHANNEL_ID = "1569081169625223168"

# Keep the same teal embed colour as the other controller alerts.
bot.COLOURS = [0x09B1BA]

# Keep Elite state/config completely separate from the normal Xbox scanner.
bot.STATE_FILE = "vinted_elite_seen_ids.json"
bot.SEARCHES_FILE = "vinted_elite_searches.json"
bot.RUNNER_STATS_FILE = "vinted_elite_runner_stats.json"

# The base scanner validates a default Discord channel ID before it starts.
bot.DISCORD_CHANNEL_ID = ELITE_DISCORD_CHANNEL_ID


# Start with the Xbox junk/merch exclusions, then add Elite-specific accessory
# and Series 1 terms. We deliberately do NOT ban faulty/broken/drift/repairs,
# because those are exactly the cheap repairable controllers we want to see.
ELITE_EXCLUDE_WORDS = list(
    getattr(bot, "XBOX_ALWAYS_EXCLUDE_WORDS", [])
) + [
    # Explicit Elite Series 1 identifiers.
    "elite series 1",
    "series 1 elite",
    "elite 1 controller",
    "elite controller series 1",
    "model 1698",
    "1698",

    # Cases / storage only.
    "case only",
    "carry case",
    "hard case",
    "empty case",
    "controller case",
    "elite case",

    # Elite accessories / replacement parts only.
    "paddles only",
    "paddle set",
    "replacement paddles",
    "thumbsticks only",
    "thumb sticks only",
    "stick set",
    "sticks only",
    "d pad only",
    "d-pad only",
    "dpad only",
    "button kit",
    "buttons only",
    "replacement buttons",
    "accessory kit",
    "accessories only",
    "accessory only",
    "parts kit",
    "repair kit",

    # Chargers / docks / cables only.
    "charging dock",
    "dock only",
    "charger only",
    "charging cable",
    "usb cable",
    "usb-c cable",
    "usb c cable",
    "cable only",

    # Cosmetic shells / housings only.
    "shell only",
    "faceplate only",
    "replacement shell",
    "housing only",
    "controller shell",
]


# Remove duplicate exclusions while preserving the readable order above.
_seen_exclusions = set()
ELITE_EXCLUDE_WORDS = [
    word
    for word in ELITE_EXCLUDE_WORDS
    if not (
        word.lower() in _seen_exclusions
        or _seen_exclusions.add(word.lower())
    )
]


def _normalise_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def is_elite_series_2_candidate(item: dict) -> bool:
    """
    Keep listings that plausibly describe an Xbox Elite Series 2 controller.

    This intentionally allows vague titles such as "Xbox elite controller" and
    "Elite Xbox controller" because two of the user's real Series 2 purchases
    were listed that way. An unlabeled Series 1 can therefore occasionally slip
    through; explicit Series 1/1698 listings are removed by the exclusion list.
    """
    title = _normalise_text(item.get("title", ""))
    brand = _normalise_text(item.get("brand_title", ""))

    if not title:
        return False

    # Exact Series 2 model number.
    if re.search(r"\b1797\b", title):
        return True

    # Strong Series 2 wording.
    strong_terms = (
        "elite series 2",
        "series 2 elite",
        "elite 2 controller",
        "xbox elite 2",
        "elite series two",
        "series two elite",
        "series 2 core",
        "elite core",
    )

    if any(term in title for term in strong_terms):
        return True

    recognised_brand = (
        "xbox" in brand
        or "microsoft" in brand
    )

    has_elite = "elite" in title
    has_controller = any(
        term in title
        for term in (
            "controller",
            "gamepad",
            "game pad",
            "joypad",
            "joy pad",
            "control pad",
        )
    )

    # Vague but realistic seller wording seen in actual purchases:
    # "Xbox elite controller" / "Elite Xbox controller".
    if has_elite and has_controller:
        if "xbox" in title or recognised_brand:
            return True

    # Some sellers may simply write "Elite controller" while Vinted supplies
    # Xbox/Microsoft as the brand.
    if recognised_brand and has_elite and has_controller:
        return True

    # Accept "Series 2 controller" only where the title or Vinted brand also
    # anchors it to Xbox/Microsoft/Elite.
    if "series 2" in title and has_controller:
        if (
            "xbox" in title
            or "elite" in title
            or recognised_brand
        ):
            return True

    return False


# Keep the base exclusion matcher, then add the Elite-specific positive filter.
# This monkey-patch affects only this dedicated Elite Python process.
_base_matches_exclude_words = bot.matches_exclude_words


def elite_should_skip(item: dict, exclude_words: list) -> bool:
    if _base_matches_exclude_words(item, exclude_words):
        return True

    if not is_elite_series_2_candidate(item):
        return True

    return False


bot.matches_exclude_words = elite_should_skip


# Multiple searches share one label/seen bucket. If Vinted returns the same
# listing for more than one phrase, it is sent only once.
bot.FALLBACK_SEARCHES = [
    {
        "label": "Xbox Elite Series 2",
        "search_text": "xbox elite controller",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "elite xbox controller",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "elite series 2",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "xbox elite series 2",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "elite 2 controller",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "series 2 controller",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "xbox elite 2",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "Xbox Elite Series 2",
        "search_text": "1797 controller",
        "max_price": 20,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": ELITE_EXCLUDE_WORDS,
        "channel_id": ELITE_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
]


if __name__ == "__main__":
    bot.run()