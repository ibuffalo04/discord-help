"""
Dedicated PS5 / DualSense Vinted -> Discord scanner.

This intentionally reuses the existing Xbox scanner implementation so the
Discord embed, seller/rating enrichment, direct @mention, Cloudflare handling,
5-second interval and telemetry behaviour stay visually/functionally the same.

It runs as a separate Python process/workflow, so it does not add any searches
or requests to the Xbox scanner's loop.
"""

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

    # Stands, docks, chargers and cases.
    "controller stand",
    "wall mount",
    "holder",
    "charging station",
    "charging dock",
    "charger only",
    "dock only",
    "charging cable",
    "usb cable",
    "usb-c cable",
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
    "thumb grips",
    "thumbgrips",
    "stick caps",
    "joystick caps",
    "replacement sticks",
    "replacement joystick",
    "analogue module",
    "analog module",
    "paddles only",
    "back buttons",
    "attachment only",
    "controller grip",
    "grip cover",

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