"""
Vinted → Discord Alert Bot
==========================
- Reads searches from vinted_searches.json
- Uses Discord Bot Token for real working buttons
- Loops every 5 seconds for 55 seconds per run
- Supports exclude words, multiple keywords, all condition types
- Uses Vinted catalogue HTML instead of the broken internal API endpoint
"""

import time
import json
import os
import re
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode


# ──────────────────────────────────────────────
# DISCORD CONFIG
# ──────────────────────────────────────────────

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")


# ──────────────────────────────────────────────
# FALLBACK SEARCHES
# ──────────────────────────────────────────────

FALLBACK_SEARCHES = [
    {
        "label": "Xbox Controller",
        "search_text": "xbox controller",
        "max_price": 15,
        "min_price": None,
        "size_ids": [],
        "brand_ids": [],
        "status_ids": [1, 2, 3, 4],
        "order": "newest_first",
        "exclude_words": [],
        "enabled": True,
    },
]


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

CHECK_INTERVAL = 5
RUN_DURATION = 55

VINTED_DOMAIN = "www.vinted.co.uk"
VINTED_BASE_URL = f"https://{VINTED_DOMAIN}"

CURRENCY_SYMBOL = "£"

STATE_FILE = "vinted_seen_ids.json"
SEARCHES_FILE = "vinted_searches.json"


# ──────────────────────────────────────────────
# VINTED HEADERS
# ──────────────────────────────────────────────

VINTED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": f"{VINTED_BASE_URL}/",
}


# ──────────────────────────────────────────────
# CONDITION LABELS
# ──────────────────────────────────────────────

CONDITION_LABELS = {
    1: "New without tags",
    2: "Very good condition",
    3: "Good condition",
    4: "Satisfactory condition",
    5: "Not specified",
    6: "New with tags",
}


# ──────────────────────────────────────────────
# COLOURS
# ──────────────────────────────────────────────

COLOURS = [
    0x09B1BA,
    0xF5A623,
    0x7ED321,
    0xD0021B,
    0x9B59B6,
    0x3498DB,
]


# ──────────────────────────────────────────────
# SESSION
# ──────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update(VINTED_HEADERS)


# ──────────────────────────────────────────────
# SEARCHES
# ──────────────────────────────────────────────

def load_searches() -> list:
    if os.path.exists(SEARCHES_FILE):
        try:
            with open(SEARCHES_FILE, encoding="utf-8") as f:
                all_searches = json.load(f)

            enabled = [
                s for s in all_searches
                if s.get("enabled", True)
            ]

            if enabled:
                print(
                    f" Loaded {len(enabled)} search(es) "
                    f"from {SEARCHES_FILE}"
                )
                return enabled

        except Exception as e:
            print(
                f" [!] Could not read {SEARCHES_FILE}: {e}"
            )

    print(" Using fallback searches")
    return FALLBACK_SEARCHES


# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────

def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict):
                return data

        except Exception as e:
            print(f" [!] Could not read {STATE_FILE}: {e}")

    return {}


def save_seen(seen: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f)
    except Exception as e:
        print(f" [!] Could not save {STATE_FILE}: {e}")


# ──────────────────────────────────────────────
# VINTED SESSION
# ──────────────────────────────────────────────

def get_vinted_session_cookie():
    """
    Load the Vinted homepage first so that the requests.Session
    receives whatever anonymous cookies Vinted currently requires.

    This is deliberately separate from the catalogue request.
    """

    try:
        r = SESSION.get(
            VINTED_BASE_URL + "/",
            timeout=15,
            allow_redirects=True,
        )

        print(
            f" [Vinted] Session bootstrap: HTTP {r.status_code}"
        )

        if r.status_code >= 400:
            print(
                f" [Vinted] Homepage returned {r.status_code}"
            )

        return True

    except requests.RequestException as e:
        print(
            f" [!] Vinted session bootstrap failed: {e}"
        )
        return False


# ──────────────────────────────────────────────
# VINTED URL
# ──────────────────────────────────────────────

def build_catalog_url(search: dict) -> str:
    """
    Build the normal Vinted catalogue/search URL.

    This replaces the old:
        /api/v2/catalog/items

    endpoint which is currently returning 404.
    """

    params = {
        "search_text": search.get("search_text", ""),
        "order": search.get("order", "newest_first"),
        "page": 1,
        "per_page": 40,
    }

    if search.get("max_price") is not None:
        params["price_to"] = search["max_price"]

    if search.get("min_price") is not None:
        params["price_from"] = search["min_price"]

    if search.get("size_ids"):
        params["size_ids[]"] = search["size_ids"]

    if search.get("brand_ids"):
        params["brand_ids[]"] = search["brand_ids"]

    if search.get("status_ids"):
        params["status_ids[]"] = search["status_ids"]

    query = urlencode(
        params,
        doseq=True,
    )

    return f"{VINTED_BASE_URL}/catalog?{query}"


# ──────────────────────────────────────────────
# VINTED HTML PARSER
# ──────────────────────────────────────────────

def extract_items_from_html(html: str) -> list:
    """
    Extract Vinted's catalogue items from the HTML response.

    Vinted currently embeds the catalogue payload in the page.
    We first look for the normal:
        "items":[...],"pagination":{...}

    structure and then try a couple of alternate structures.

    Returns:
        list[dict]
    """

    if not html:
        return []

    # ----------------------------------------------------------
    # Method 1
    #
    # This is the common catalogue payload:
    #
    # "items":[...],"pagination":{...}
    #
    # ----------------------------------------------------------

    patterns = [
        r'"items"\s*:\s*(\[.*?\])\s*,\s*"pagination"\s*:\s*(\{.*?\})',
        r'"items"\s*:\s*(\[.*?\])\s*,\s*"pagination"',
    ]

    for pattern in patterns:
        try:
            match = re.search(
                pattern,
                html,
                re.DOTALL,
            )

            if not match:
                continue

            items_json = match.group(1)

            try:
                items = json.loads(items_json)

                if isinstance(items, list):
                    return [
                        item
                        for item in items
                        if isinstance(item, dict)
                    ]

            except json.JSONDecodeError:
                pass

        except Exception:
            pass

    # ----------------------------------------------------------
    # Method 2
    #
    # Try to find an escaped JSON items array.
    # ----------------------------------------------------------

    escaped_pattern = (
        r'\\"items\\"\s*:\s*'
        r'(\[.*?\])'
        r'\s*,\s*\\"pagination\\"'
    )

    try:
        match = re.search(
            escaped_pattern,
            html,
            re.DOTALL,
        )

        if match:
            items_json = match.group(1)

            # Unescape JSON quotes.
            items_json = items_json.replace('\\"', '"')

            try:
                items = json.loads(items_json)

                if isinstance(items, list):
                    return [
                        item
                        for item in items
                        if isinstance(item, dict)
                    ]

            except json.JSONDecodeError:
                pass

    except Exception:
        pass

    # ----------------------------------------------------------
    # Method 3
    #
    # Look for a larger JSON blob containing "items".
    #
    # This is intentionally conservative so we don't accidentally
    # turn random page text into listings.
    # ----------------------------------------------------------

    try:
        marker = '"items":'

        start = html.find(marker)

        if start != -1:
            array_start = html.find("[", start)

            if array_start != -1:
                depth = 0
                in_string = False
                escaped = False

                for i in range(
                    array_start,
                    len(html),
                ):
                    char = html[i]

                    if escaped:
                        escaped = False
                        continue

                    if char == "\\":
                        escaped = True
                        continue

                    if char == '"':
                        in_string = not in_string
                        continue

                    if in_string:
                        continue

                    if char == "[":
                        depth += 1

                    elif char == "]":
                        depth -= 1

                        if depth == 0:
                            items_json = html[
                                array_start:i + 1
                            ]

                            try:
                                items = json.loads(
                                    items_json
                                )

                                if isinstance(items, list):
                                    valid_items = [
                                        item
                                        for item in items
                                        if isinstance(
                                            item,
                                            dict,
                                        )
                                        and item.get("id")
                                    ]

                                    if valid_items:
                                        return valid_items

                            except json.JSONDecodeError:
                                pass

                            break

    except Exception:
        pass

    return []


# ──────────────────────────────────────────────
# VINTED LISTINGS
# ──────────────────────────────────────────────

def fetch_listings(search: dict) -> list:
    """
    Fetch listings from Vinted's normal catalogue page.

    The old bot used:
        /api/v2/catalog/items

    That endpoint is currently returning HTTP 404, so we no
    longer depend on it.
    """

    url = build_catalog_url(search)

    try:
        r = SESSION.get(
            url,
            timeout=20,
            allow_redirects=True,
        )

        if r.status_code != 200:
            print(
                f"\n [!] Vinted catalogue HTTP "
                f"{r.status_code}: {url}"
            )

            # If the session has expired/changed, bootstrap once
            # and retry the catalogue page.
            if r.status_code in (401, 403):
                print(
                    " [!] Refreshing Vinted session "
                    "and retrying..."
                )

                get_vinted_session_cookie()

                r = SESSION.get(
                    url,
                    timeout=20,
                    allow_redirects=True,
                )

            if r.status_code != 200:
                print(
                    f" [!] Vinted catalogue still returned "
                    f"HTTP {r.status_code}"
                )
                return []

        items = extract_items_from_html(
            r.text
        )

        if not items:
            print(
                "\n [!] Vinted page loaded, but no "
                "catalogue items could be extracted."
            )

            # Helpful diagnostic without dumping the entire page.
            print(
                f" [debug] Response length: "
                f"{len(r.text):,} characters"
            )

            if "captcha" in r.text.lower():
                print(
                    " [!] Vinted appears to have returned "
                    "a challenge/captcha page."
                )

            return []

        return items

    except requests.Timeout:
        print(
            "\n [!] Vinted catalogue request timed out."
        )

    except requests.RequestException as e:
        print(
            f"\n [!] Vinted request error: {e}"
        )

    except Exception as e:
        print(
            f"\n [!] Unexpected Vinted error: {e}"
        )

    return []


# ──────────────────────────────────────────────
# EXCLUDE WORDS
# ──────────────────────────────────────────────

def matches_exclude_words(
    item: dict,
    exclude_words: list,
) -> bool:
    """
    Returns True if the item title contains an excluded word.
    """

    if not exclude_words:
        return False

    title = (
        item.get("title") or ""
    ).lower()

    for word in exclude_words:
        word = str(word).lower().strip()

        if word and word in title:
            return True

    return False


# ──────────────────────────────────────────────
# DISCORD HELPERS
# ──────────────────────────────────────────────

def time_ago(value) -> str:
    if not value:
        return "Unknown"

    ts = None

    # Try Unix timestamp.
    try:
        ts = int(float(str(value)))

    except (TypeError, ValueError):
        pass

    # Try ISO 8601 string.
    if ts is None:
        try:
            s = str(value)

            # Remove timezone information for simple parsing.
            s = s[:19]

            dt = datetime.strptime(
                s,
                "%Y-%m-%dT%H:%M:%S",
            ).replace(
                tzinfo=timezone.utc
            )

            ts = int(
                dt.timestamp()
            )

        except Exception:
            return "Unknown"

    diff = int(time.time()) - ts

    if diff < 0:
        return "just now"

    if diff < 60:
        return (
            f"{diff} second"
            f"{'s' if diff != 1 else ''} ago"
        )

    if diff < 3600:
        m = diff // 60

        return (
            f"{m} minute"
            f"{'s' if m != 1 else ''} ago"
        )

    if diff < 86400:
        h = diff // 3600

        return (
            f"{h} hour"
            f"{'s' if h != 1 else ''} ago"
        )

    d = diff // 86400

    return (
        f"{d} day"
        f"{'s' if d != 1 else ''} ago"
    )


def star_rating(reputation) -> str:
    """
    Vinted feedback_reputation is normally a float between
    0.0 and 1.0.

    Convert it to 0-5 stars.
    """

    if reputation is None:
        return "No ratings"

    try:
        score = float(reputation)

        stars = round(
            score * 5
        )

        stars = max(
            0,
            min(5, stars),
        )

        return (
            "⭐" * stars
            + "✩" * (5 - stars)
        )

    except (
        TypeError,
        ValueError,
    ):
        return "No ratings"


# ──────────────────────────────────────────────
# ITEM URL
# ──────────────────────────────────────────────

def get_item_url(item: dict) -> str:
    url = item.get("url", "")

    if url and not url.startswith("http"):
        url = (
            f"{VINTED_BASE_URL}"
            f"{url}"
        )

    # Some catalogue responses use item_id instead.
    if not url and item.get("id"):
        url = (
            f"{VINTED_BASE_URL}"
            f"/items/{item['id']}"
        )

    return url


# ──────────────────────────────────────────────
# DISCORD PAYLOAD
# ──────────────────────────────────────────────

def build_payload(
    label: str,
    item: dict,
    colour: int,
) -> dict:

    item_url = get_item_url(item)

    item_id = item.get(
        "id",
        "",
    )

    # Seller information should already be included in the
    # catalogue response. We no longer make a second API call
    # to /api/v2/users/{id}.
    user = item.get(
        "user",
        {},
    )

    if not isinstance(user, dict):
        user = {}

    # ----------------------------------------------------------
    # Seller
    # ----------------------------------------------------------

    seller = (
        user.get("login")
        or user.get("username")
        or "Unknown seller"
    )

    seller_id = user.get(
        "id"
    )

    if seller_id:
        seller_url = (
            f"{VINTED_BASE_URL}"
            f"/member/{seller_id}"
        )

    else:
        seller_url = item_url

    # ----------------------------------------------------------
    # Price
    # ----------------------------------------------------------

    price_obj = item.get(
        "price",
        {},
    )

    if isinstance(price_obj, dict):
        amount = (
            price_obj.get("amount")
            or price_obj.get("value")
            or "?"
        )

    else:
        amount = price_obj or "?"

    price_str = (
        f"{CURRENCY_SYMBOL}"
        f"{amount}"
    )

    # ----------------------------------------------------------
    # Brand / size
    # ----------------------------------------------------------

    brand = (
        item.get("brand_title")
        or item.get("brand")
        or "—"
    )

    if isinstance(brand, dict):
        brand = (
            brand.get("title")
            or brand.get("name")
            or "—"
        )

    size = (
        item.get("size_title")
        or item.get("size")
        or "—"
    )

    if isinstance(size, dict):
        size = (
            size.get("title")
            or size.get("name")
            or "—"
        )

    # ----------------------------------------------------------
    # Condition
    # ----------------------------------------------------------

    raw_status = (
        item.get("status")
        or ""
    )

    status_id = item.get(
        "status_id"
    )

    try:
        status_id = int(status_id)
    except (
        TypeError,
        ValueError,
    ):
        status_id = None

    if status_id:
        condition = (
            CONDITION_LABELS.get(
                status_id,
                raw_status,
            )
        )
    else:
        condition = (
            raw_status
            or "—"
        )

    # ----------------------------------------------------------
    # Published timestamp
    # ----------------------------------------------------------

    created_at = (
        item.get("created_at_ts")
        or item.get("created_at")
        or item.get("updated_at_ts")
        or item.get("updated_at")
        or (
            item.get("item_box") or {}
        ).get("created_at_ts")
        or (
            item.get("item_box") or {}
        ).get("created_at")
    )

    if created_at:
        try:
            unix_ts = int(
                float(
                    str(created_at)
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            try:
                s = str(created_at)[:19]

                dt = datetime.strptime(
                    s,
                    "%Y-%m-%dT%H:%M:%S",
                ).replace(
                    tzinfo=timezone.utc
                )

                unix_ts = int(
                    dt.timestamp()
                )

            except Exception:
                unix_ts = int(
                    time.time()
                )

    else:
        # Vinted's catalogue payload does not always expose
        # creation time. In that case, don't claim an old
        # listing was definitely published now.
        unix_ts = int(
            time.time()
        )

    published = (
        f"<t:{unix_ts}:R>"
    )

    # ----------------------------------------------------------
    # Feedback
    # ----------------------------------------------------------

    feedback_score = (
        user.get("feedback_reputation")
        or user.get("feedback_score")
        or item.get("feedback_reputation")
    )

    feedback_count = (
        user.get("positive_feedback_count")
        or user.get("feedback_count")
        or user.get("feedback_count_total")
        or 0
    )

    stars = star_rating(
        feedback_score
    )

    feedback_str = (
        f"{stars} ({feedback_count})"
    )

    # ----------------------------------------------------------
    # Photo
    # ----------------------------------------------------------

    photos = item.get(
        "photos",
        [],
    )

    image_url = None

    if isinstance(photos, list) and photos:
        first_photo = photos[0]

        if isinstance(
            first_photo,
            dict,
        ):
            image_url = (
                first_photo.get(
                    "full_size_url"
                )
                or first_photo.get(
                    "url"
                )
            )

            if not image_url:
                thumbnails = (
                    first_photo.get(
                        "thumbnails"
                    )
                    or []
                )

                if isinstance(
                    thumbnails,
                    list,
                ) and thumbnails:
                    for thumbnail in reversed(
                        thumbnails
                    ):
                        if isinstance(
                            thumbnail,
                            dict,
                        ):
                            image_url = (
                                thumbnail.get(
                                    "url"
                                )
                            )

                            if image_url:
                                break

    # Some Vinted responses may provide a direct photo URL.
    if not image_url:
        image_url = (
            item.get("photo_url")
            or item.get("image_url")
        )

    # ----------------------------------------------------------
    # Embed
    # ----------------------------------------------------------

    embed = {
        "author": {
            "name": f"👤 {seller}",
            "url": seller_url,
        },

        "title": item.get(
            "title",
            "New listing",
        ),

        "url": item_url,

        "color": colour,

        "fields": [
            {
                "name": "⏳ Published",
                "value": published,
                "inline": True,
            },

            {
                "name": "🏷️ Brand",
                "value": str(brand),
                "inline": True,
            },

            {
                "name": "📐 Size",
                "value": str(size),
                "inline": True,
            },

            {
                "name": "⭐ Feedbacks",
                "value": feedback_str,
                "inline": True,
            },

            {
                "name": "💎 Status",
                "value": str(condition),
                "inline": True,
            },

            {
                "name": "💰 Price",
                "value": price_str,
                "inline": True,
            },
        ],

        "footer": {
            "text": (
                f"🔍 Search: {label}"
            ),
        },

        "timestamp": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }

    if image_url:
        embed["image"] = {
            "url": image_url
        }

    # ----------------------------------------------------------
    # Discord button
    # ----------------------------------------------------------

    components = [
        {
            "type": 1,

            "components": [
                {
                    "type": 2,
                    "style": 5,
                    "label": "View Listing",
                    "emoji": {
                        "name": "🔗"
                    },
                    "url": item_url,
                }
            ],
        }
    ]

    return {
        "embeds": [
            embed
        ],
        "components": components,
    }


# ──────────────────────────────────────────────
# DISCORD
# ──────────────────────────────────────────────

def send_discord(
    label: str,
    item: dict,
    colour: int,
    channel_id: str = None,
):

    # Use per-search channel if set,
    # otherwise fall back to default.
    target_channel = (
        channel_id
        if channel_id
        else DISCORD_CHANNEL_ID
    )

    if not target_channel:
        print(
            f" [!] No channel ID configured "
            f"for '{label}' — skipping"
        )
        return

    payload = build_payload(
        label,
        item,
        colour,
    )

    url = (
        "https://discord.com/api/v10/"
        f"channels/{target_channel}/messages"
    )

    headers = {
        "Authorization": (
            f"Bot {DISCORD_BOT_TOKEN}"
        ),
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10,
        )

        r.raise_for_status()

    except requests.HTTPError as e:
        status = (
            e.response.status_code
            if e.response is not None
            else "?"
        )

        text = (
            e.response.text
            if e.response is not None
            else ""
        )

        print(
            f" [!] Discord error "
            f"{status}: {text}"
        )

    except requests.RequestException as e:
        print(
            f" [!] Discord error: {e}"
        )


# ──────────────────────────────────────────────
# VALIDATION
# ──────────────────────────────────────────────

def validate():

    errors = []

    if not DISCORD_BOT_TOKEN:
        errors.append(
            "DISCORD_BOT_TOKEN secret is missing"
        )

    if not DISCORD_CHANNEL_ID:
        errors.append(
            "DISCORD_CHANNEL_ID secret is missing"
        )

    if errors:
        print(
            "=" * 55
        )

        for e in errors:
            print(
                f" ERROR: {e}"
            )

        print(
            "=" * 55
        )

        raise SystemExit(1)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def run():

    validate()

    searches = load_searches()

    if not searches:
        print(
            "No searches configured — nothing to do."
        )
        return

    print(
        "=" * 55
    )

    print(
        " Vinted -> Discord Alert Bot"
    )

    print(
        f" Checking every {CHECK_INTERVAL}s "
        f"for {RUN_DURATION}s"
    )

    print(
        " Using Vinted catalogue HTML"
    )

    print(
        "=" * 55
    )

    for s in searches:

        excl = s.get(
            "exclude_words",
            [],
        )

        excl_str = (
            f" | exclude: {', '.join(excl)}"
            if excl
            else ""
        )

        ch_str = (
            f" | channel: {s['channel_id']}"
            if s.get("channel_id")
            else (
                " | channel: default "
                f"({DISCORD_CHANNEL_ID})"
            )
        )

        print(
            f" * {s['label']}"
            f"{excl_str}"
            f"{ch_str}"
        )

    print()

    # Bootstrap the Vinted session.
    get_vinted_session_cookie()

    seen = load_seen()

    # ----------------------------------------------------------
    # FIRST RUN
    # ----------------------------------------------------------

    first_run = not bool(seen)

    if first_run:

        print(
            "First run — seeding existing listings "
            "(no alerts)..."
        )

        for search in searches:

            key = search[
                "label"
            ]

            items = fetch_listings(
                search
            )

            seen.setdefault(
                key,
                []
            )

            for item in items:

                if not item.get("id"):
                    continue

                iid = str(
                    item["id"]
                )

                if iid not in seen[key]:
                    seen[key].append(
                        iid
                    )

        save_seen(
            seen
        )

        print(
            "Done. Future runs will alert "
            "on new listings.\n"
        )

        return

    # ----------------------------------------------------------
    # SEARCH COLOURS
    # ----------------------------------------------------------

    label_colours = {
        s["label"]: COLOURS[
            i % len(COLOURS)
        ]
        for i, s in enumerate(searches)
    }

    # ----------------------------------------------------------
    # POLLING LOOP
    # ----------------------------------------------------------

    start_time = time.time()

    checks = 0

    while (
        time.time() - start_time
        < RUN_DURATION
    ):

        checks += 1

        ts = datetime.now().strftime(
            "%H:%M:%S"
        )

        print(
            f"[{ts}] Check #{checks}...",
            end=" ",
            flush=True,
        )

        found_new = 0

        for search in searches:

            key = search[
                "label"
            ]

            colour = label_colours.get(
                key,
                COLOURS[0],
            )

            exclude_words = search.get(
                "exclude_words",
                [],
            )

            items = fetch_listings(
                search
            )

            seen.setdefault(
                key,
                []
            )

            for item in items:

                if not item.get("id"):
                    continue

                iid = str(
                    item["id"]
                )

                if iid in seen[key]:
                    continue

                # Mark as seen before sending so a Discord
                # problem doesn't cause repeated alerts.
                seen[key].append(
                    iid
                )

                if matches_exclude_words(
                    item,
                    exclude_words,
                ):

                    print(
                        f"\n [skip] "
                        f"'{item.get('title')}' "
                        f"matches exclude words"
                    )

                    continue

                send_discord(
                    key,
                    item,
                    colour,
                    search.get(
                        "channel_id"
                    ),
                )

                found_new += 1

        save_seen(
            seen
        )

        if found_new:
            print(
                f"{found_new} new item(s)."
            )
        else:
            print(
                "nothing new."
            )

        time.sleep(
            CHECK_INTERVAL
        )


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    run()