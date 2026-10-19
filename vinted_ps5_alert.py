"""
Dedicated PS5 / DualSense Vinted -> Discord scanner.

This intentionally reuses the existing Xbox scanner implementation so the
Discord embed, seller/rating enrichment, direct @mention, Cloudflare handling,
5-second interval and telemetry behaviour stay visually/functionally the same.

It runs as a separate Python process/workflow, so it does not add any searches
or requests to the Xbox scanner's loop.
"""

import re

import vinted_discord_alert as bot


# ──────────────────────────────────────────────
# PS5 SCANNER CONFIG
# ──────────────────────────────────────────────
PS5_DISCORD_CHANNEL_ID = "1561823974046965870"

# Keep the same teal embed colour as the Xbox controller alerts.
bot.COLOURS = [0x09B1BA]

# Keep PS5 state/config completely separate from the Xbox scanner.
bot.STATE_FILE = "vinted_ps5_seen_ids.json"
bot.SEARCHES_FILE = "vinted_ps5_searches.json"
bot.RUNNER_STATS_FILE = "vinted_ps5_runner_stats.json"

# The base bot's validation requires a default channel ID. Every PS5 search
# below also has the channel explicitly set, so all PS5 alerts are isolated
# from the Xbox Discord channel.
bot.DISCORD_CHANNEL_ID = PS5_DISCORD_CHANNEL_ID


# Search terms are deliberately limited to two broad genuine-controller terms.
# They share the same label, so a listing returned by both searches is only
# alerted once because both searches use the same seen-ID bucket.
PS5_EXCLUDE_WORDS = [
    # Common third-party / accessory brands we do not want for the DualSense trial.
    "powera",
    "power a",
    "pdp",
    "nacon",
    "hori",
    "razer",
    "gamesir",
    "game sir",
    "turtle beach",
    "victrix",
    "snakebyte",
    "venom",
    "cableguys",
    "cable guys",

    # Other platforms / controller types.
    "ps4 controller",
    "dualshock",
    "dual shock",
    "ps3 controller",
    "ps2 controller",
    "xbox controller",
    "switch controller",
    "backbone",
    "psvr",
    "psvr2",
    "vr controller",
    "media remote",

    # Stands, docks, chargers, cables and cases.
    "controller stand",
    "phone stand",
    "wall mount",
    "holder",
    "charging station",
    "charging dock",
    "charging stand",
    "docking station",
    "charger",
    "dock only",
    "charging cable",
    "controller cable",
    "usb cable",
    "usb-c cable",
    "usb c cable",
    "cable only",
    "carry case",
    "travel case",
    "hard case",
    "case only",
    "empty box",
    "box only",

    # Covers, skins, shells and cosmetic-only parts.
    "controller cover",
    "cover only",
    "protective cover",
    "protective case",
    "silicone cover",
    "silicone case",
    "skin only",
    "controller skin",
    "shell only",
    "replacement shell",
    "housing only",
    "faceplate only",

    # Small replacement parts / add-ons.
    "button kit",
    "buttons only",
    "thumb cap",
    "thumb grip",
    "thumbgrip",
    "stick cap",
    "joystick cap",
    "replacement sticks",
    "replacement joystick",
    "analogue module",
    "analog module",
    "paddles only",
    "back buttons",
    "attachment only",
    "controller grip",
    "grip cover",

    # Generic accessory listings.
    "gaming accessory",
    "gaming accessories",
    "controller accessory",
    "controller accessories",
    "accessory only",
    "accessories only",

    # Misc accessories.
    "adapter",
    "receiver",
    "dongle",
    "phone mount",
    "headset",
    "sticker",
    "stickers",
    "decal",
    "decals",
]


# Vinted's catalogue HTML can contain unrelated recommendation cards mixed in
# with the actual search results. The Xbox search is narrow enough that this is
# less obvious, but PS5/DualSense is currently returning lots of clothing and
# accessories. Keep an item only if its TITLE looks like an actual controller.
#
# We deliberately do NOT reject words such as faulty, broken, drift, repairs,
# spares, not working, untested, no power, etc. Those are exactly the cheap
# repairable controllers we want TechXpress to see.
def _normalise_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def is_ps5_controller_candidate(item: dict) -> bool:
    title = _normalise_text(item.get("title", ""))
    brand = _normalise_text(item.get("brand_title", ""))

    if not title:
        return False

    # The strongest identifier and worth accepting on its own.
    if "dualsense" in title or "dual sense" in title:
        return True

    controller_words = (
        "controller",
        "gamepad",
        "joypad",
        "control pad",
    )

    has_controller_word = any(
        word in title
        for word in controller_words
    )

    ps5_words = (
        "ps5",
        "ps 5",
        "playstation 5",
        "playstation5",
    )

    has_ps5_word = any(
        word in title
        for word in ps5_words
    )

    # Normal titles such as "PS5 controller", "PlayStation 5 gamepad", etc.
    if has_ps5_word and has_controller_word:
        return True

    # Vinted sometimes returns very short titles such as "Sony controller"
    # where the PlayStation/Sony identity sits in the brand field instead.
    recognised_brand = (
        "playstation" in brand
        or brand == "sony"
        or brand.startswith("sony ")
    )

    if recognised_brand and has_controller_word:
        return True

    return False


# Keep the base Xbox exclusion logic, then add the PS5-only positive filter.
# This monkey-patch affects ONLY this dedicated PS5 Python process. The Xbox
# scanner imports/runs its own process and is completely unchanged.
_base_matches_exclude_words = bot.matches_exclude_words


def ps5_should_skip(item: dict, exclude_words: list) -> bool:
    if _base_matches_exclude_words(item, exclude_words):
        return True

    if not is_ps5_controller_candidate(item):
        return True

    return False


bot.matches_exclude_words = ps5_should_skip


bot.FALLBACK_SEARCHES = [
    {
        "label": "PS5 Controller",
        "search_text": "ps5 controller",
        "max_price": 15,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": PS5_EXCLUDE_WORDS,
        "channel_id": PS5_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
    {
        "label": "PS5 Controller",
        "search_text": "dualsense",
        "max_price": 15,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": PS5_EXCLUDE_WORDS,
        "channel_id": PS5_DISCORD_CHANNEL_ID,
        "enabled": True,
    },
]


if __name__ == "__main__":
    bot.run()