"""
Vinted → Discord Alert Bot
==========================
- Reads searches from vinted_searches.json
- Uses Discord Bot Token for real working buttons
- Loops every 5 seconds for 55 seconds per run
- Supports exclude words, multiple keywords, all condition types
- Uses Vinted's internal catalogue API with the web session cookie
- Falls back to public item pages when individual item details
  are unavailable

GitHub Secrets needed:
DISCORD_BOT_TOKEN — your Discord bot token
DISCORD_CHANNEL_ID — right-click channel in Discord → Copy Channel ID

Optional:
VINTED_COOKIE — a Vinted cookie string containing access_token_web
                (recommended if GitHub Actions cannot obtain it
                 automatically)
"""

import time
import json
import os
import re
import requests
from datetime import datetime, timezone


# ──────────────────────────────────────────────
# DISCORD CONFIG
# ──────────────────────────────────────────────

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")


# ──────────────────────────────────────────────
# VINTED CONFIG
# ──────────────────────────────────────────────

VINTED_DOMAIN = "www.vinted.co.uk"
VINTED_BASE_URL = f"https://{VINTED_DOMAIN}"

# Optional GitHub Secret.
#
# If supplied, this should look something like:
#
# access_token_web=XXXX; refresh_token_web=XXXX; anon_id=XXXX
#
# Do NOT put your cookie directly into this source file.
VINTED_COOKIE = os.environ.get("VINTED_COOKIE", "")


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


CHECK_INTERVAL = 5
RUN_DURATION = 55

CURRENCY_SYMBOL = "£"

STATE_FILE = "vinted_seen_ids.json"
SEARCHES_FILE = "vinted_searches.json"


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
# HEADERS
# ──────────────────────────────────────────────

VINTED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": f"{VINTED_BASE_URL}/",
    "Origin": VINTED_BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


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

SESSION.headers.update(
    VINTED_HEADERS
)


# ──────────────────────────────────────────────
# SEARCHES
# ──────────────────────────────────────────────

def load_searches() -> list:

    if os.path.exists(SEARCHES_FILE):

        try:

            with open(
                SEARCHES_FILE,
                encoding="utf-8",
            ) as f:

                all_searches = json.load(f)

            enabled = [
                s
                for s in all_searches
                if s.get("enabled", True)
            ]

            if enabled:

                print(
                    f" Loaded {len(enabled)} "
                    f"search(es) from "
                    f"{SEARCHES_FILE}"
                )

                return enabled

        except Exception as e:

            print(
                f" [!] Could not read "
                f"{SEARCHES_FILE}: {e}"
            )

    print(
        " Using fallback searches"
    )

    return FALLBACK_SEARCHES


# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────

def load_seen() -> dict:

    if os.path.exists(STATE_FILE):

        try:

            with open(
                STATE_FILE,
                encoding="utf-8",
            ) as f:

                data = json.load(f)

            if isinstance(
                data,
                dict,
            ):

                return data

        except Exception as e:

            print(
                f" [!] Could not read "
                f"{STATE_FILE}: {e}"
            )

    return {}


def save_seen(seen: dict):

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                seen,
                f,
            )

    except Exception as e:

        print(
            f" [!] Could not save "
            f"{STATE_FILE}: {e}"
        )


# ──────────────────────────────────────────────
# VINTED COOKIE HELPERS
# ──────────────────────────────────────────────

def cookie_header_from_session() -> str:

    cookies = []

    for cookie in SESSION.cookies:

        cookies.append(
            f"{cookie.name}={cookie.value}"
        )

    return "; ".join(
        cookies
    )


def apply_manual_vinted_cookie():

    if not VINTED_COOKIE:
        return

    # Keep the raw Cookie header as well as putting the
    # individual cookies into the requests session.
    SESSION.headers["Cookie"] = (
        VINTED_COOKIE
    )

    for part in VINTED_COOKIE.split(";"):

        part = part.strip()

        if "=" not in part:
            continue

        name, value = part.split(
            "=",
            1,
        )

        name = name.strip()
        value = value.strip()

        if name:

            SESSION.cookies.set(
                name,
                value,
                domain=VINTED_DOMAIN,
            )

    print(
        " [Vinted] Manual VINTED_COOKIE loaded."
    )


# ──────────────────────────────────────────────
# VINTED SESSION
# ──────────────────────────────────────────────

def get_vinted_session_cookie():

    """
    Bootstrap the Vinted session.

    The catalogue API requires Vinted's web authentication
    cookie. A normal requests GET may not always be enough to
    mint the cookie because Vinted can generate it through
    client-side/browser flows.

    Therefore:
      1. Use VINTED_COOKIE if supplied.
      2. Otherwise visit the homepage.
      3. Preserve all cookies returned by Vinted.
    """

    apply_manual_vinted_cookie()

    if VINTED_COOKIE:

        if (
            "access_token_web="
            in VINTED_COOKIE
        ):

            print(
                " [Vinted] "
                "access_token_web supplied."
            )

        return True

    try:

        r = SESSION.get(
            VINTED_BASE_URL + "/",
            headers=VINTED_HEADERS,
            timeout=15,
            allow_redirects=True,
        )

        print(
            f" [Vinted] Session bootstrap: "
            f"HTTP {r.status_code}"
        )

        # Explicitly rebuild the Cookie header from the session.
        cookie_header = (
            cookie_header_from_session()
        )

        if cookie_header:

            SESSION.headers[
                "Cookie"
            ] = cookie_header

        has_access_token = any(
            cookie.name
            == "access_token_web"
            for cookie in SESSION.cookies
        )

        if has_access_token:

            print(
                " [Vinted] "
                "access_token_web obtained."
            )

        else:

            print(
                " [Vinted] WARNING: "
                "access_token_web was not "
                "obtained by the plain HTTP "
                "homepage request."
            )

            print(
                " [Vinted] If the catalogue API "
                "returns 404/401, add your "
                "Vinted cookie as the "
                "VINTED_COOKIE GitHub secret."
            )

        return True

    except requests.RequestException as e:

        print(
            f" [!] Vinted session bootstrap "
            f"failed: {e}"
        )

        return False


# ──────────────────────────────────────────────
# API PARAMS
# ──────────────────────────────────────────────

def build_search_params(
    search: dict,
) -> dict:

    params = {
        "search_text": search.get(
            "search_text",
            "",
        ),

        "order": search.get(
            "order",
            "newest_first",
        ),

        "per_page": 40,

        "page": 1,
    }

    if search.get(
        "max_price"
    ) is not None:

        params[
            "price_to"
        ] = search[
            "max_price"
        ]

    if search.get(
        "min_price"
    ) is not None:

        params[
            "price_from"
        ] = search[
            "min_price"
        ]

    if search.get(
        "size_ids"
    ):

        params[
            "size_ids[]"
        ] = search[
            "size_ids"
        ]

    if search.get(
        "brand_ids"
    ):

        params[
            "brand_ids[]"
        ] = search[
            "brand_ids"
        ]

    if search.get(
        "status_ids"
    ):

        params[
            "status_ids[]"
        ] = search[
            "status_ids"
        ]

    return params


# ──────────────────────────────────────────────
# VINTED LISTINGS
# ──────────────────────────────────────────────

def fetch_listings(
    search: dict,
) -> list:

    params = build_search_params(
        search
    )

    url = (
        f"{VINTED_BASE_URL}"
        "/api/v2/catalog/items"
    )

    # Make sure the current session cookies are sent.
    if not VINTED_COOKIE:

        cookie_header = (
            cookie_header_from_session()
        )

        if cookie_header:

            SESSION.headers[
                "Cookie"
            ] = cookie_header

    try:

        r = SESSION.get(
            url,
            params=params,
            headers=VINTED_HEADERS,
            timeout=20,
        )

        if r.status_code == 200:

            try:

                data = r.json()

            except ValueError:

                print(
                    " [!] Vinted returned HTTP 200 "
                    "but the response was not JSON."
                )

                return []

            items = data.get(
                "items",
                [],
            )

            if not isinstance(
                items,
                list,
            ):

                return []

            return items

        # ------------------------------------------------------
        # 401 / 403 / 404
        #
        # Do NOT blindly retry the same request.
        #
        # Refresh the Vinted session first.
        # ------------------------------------------------------

        if r.status_code in (
            401,
            403,
            404,
        ):

            print(
                f" [!] Vinted API returned "
                f"{r.status_code}."
            )

            print(
                " [!] Refreshing Vinted "
                "session and retrying..."
            )

            # Remove an automatically-created Cookie header
            # before rebuilding it.
            if not VINTED_COOKIE:

                SESSION.headers.pop(
                    "Cookie",
                    None,
                )

                SESSION.cookies.clear()

                get_vinted_session_cookie()

            else:

                apply_manual_vinted_cookie()

            if not VINTED_COOKIE:

                cookie_header = (
                    cookie_header_from_session()
                )

                if cookie_header:

                    SESSION.headers[
                        "Cookie"
                    ] = cookie_header

            r = SESSION.get(
                url,
                params=params,
                headers=VINTED_HEADERS,
                timeout=20,
            )

            if r.status_code == 200:

                try:

                    data = r.json()

                except ValueError:

                    print(
                        " [!] Vinted returned "
                        "HTTP 200 but "
                        "non-JSON data."
                    )

                    return []

                return data.get(
                    "items",
                    [],
                )

            print(
                f" [!] Vinted API still "
                f"returned HTTP "
                f"{r.status_code}."
            )

            # Give useful diagnostics without dumping
            # cookies/tokens into the GitHub log.
            try:

                data = r.json()

                print(
                    " [debug] Vinted response:"
                    f" code={data.get('code')},"
                    f" message_code="
                    f"{data.get('message_code')}"
                )

            except Exception:

                print(
                    " [debug] Response was "
                    f"{len(r.text)} characters."
                )

            return []

        r.raise_for_status()

        return []

    except requests.HTTPError as e:

        print(
            f" [!] HTTP error: {e}"
        )

    except requests.RequestException as e:

        print(
            f" [!] Request error: {e}"
        )

    except Exception as e:

        print(
            f" [!] Unexpected Vinted "
            f"error: {e}"
        )

    return []


# ──────────────────────────────────────────────
# USER PROFILE
# ──────────────────────────────────────────────

def fetch_user_profile(
    user_id,
) -> dict:

    """
    Fetch user profile to get feedback rating.

    This is kept from the original bot because the original
    Discord embed uses the seller's reputation.
    """

    if not user_id:
        return {}

    url = (
        f"{VINTED_BASE_URL}"
        f"/api/v2/users/{user_id}"
    )

    try:

        r = SESSION.get(
            url,
            headers=VINTED_HEADERS,
            timeout=10,
        )

        if r.status_code == 200:

            data = r.json()

            return data.get(
                "user",
                {},
            )

    except Exception:
        pass

    return {}


# ──────────────────────────────────────────────
# ITEM DETAILS
# ──────────────────────────────────────────────

def fetch_item_details(
    item_id,
) -> dict:

    """
    Fetch complete item information.

    The catalogue result normally already contains everything
    required. This is retained as a fallback/enrichment step.
    """

    if not item_id:
        return {}

    url = (
        f"{VINTED_BASE_URL}"
        f"/api/v2/items/{item_id}"
    )

    try:

        r = SESSION.get(
            url,
            headers=VINTED_HEADERS,
            timeout=10,
        )

        if r.status_code == 200:

            data = r.json()

            return data.get(
                "item",
                data,
            )

    except Exception:
        pass

    return {}


# ──────────────────────────────────────────────
# EXCLUDE WORDS
# ──────────────────────────────────────────────

def matches_exclude_words(
    item: dict,
    exclude_words: list,
) -> bool:

    """
    Returns True if the item title contains any excluded word.

    This is deliberately applied BEFORE the listing is added
    to the alert pipeline.
    """

    if not exclude_words:
        return False

    title = (
        item.get("title")
        or ""
    ).lower()

    for word in exclude_words:

        word = str(
            word
        ).lower().strip()

        if (
            word
            and word in title
        ):

            return True

    return False


# ──────────────────────────────────────────────
# DISCORD HELPERS
# ──────────────────────────────────────────────

def time_ago(
    value,
) -> str:

    if not value:
        return "Unknown"

    ts = None

    try:

        ts = int(
            float(
                str(value)
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        pass

    if ts is None:

        try:

            from datetime import (
                timezone as tz
            )

            s = str(
                value
            )[:19]

            dt = datetime.strptime(
                s,
                "%Y-%m-%dT%H:%M:%S",
            ).replace(
                tzinfo=tz.utc
            )

            ts = int(
                dt.timestamp()
            )

        except Exception:

            return "Unknown"

    diff = (
        int(time.time())
        - ts
    )

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


def star_rating(
    reputation,
) -> str:

    if reputation is None:
        return "No ratings"

    try:

        score = float(
            reputation
        )

        stars = round(
            score * 5
        )

        stars = max(
            0,
            min(
                5,
                stars,
            ),
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

def get_item_url(
    item: dict,
) -> str:

    url = item.get(
        "url",
        "",
    )

    if url and not url.startswith(
        "http"
    ):

        url = (
            f"{VINTED_BASE_URL}"
            f"{url}"
        )

    # Some Vinted responses provide a "path"
    # rather than a full URL.
    if not url:

        path = item.get(
            "path",
            "",
        )

        if path:

            if path.startswith(
                "http"
            ):

                url = path

            else:

                url = (
                    f"{VINTED_BASE_URL}"
                    f"{path}"
                )

    # Final fallback.
    if not url and item.get(
        "id"
    ):

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

    item_url = get_item_url(
        item
    )

    item_id = item.get(
        "id",
        "",
    )

    # ----------------------------------------------------------
    # Seller profile enrichment
    # ----------------------------------------------------------

    user = item.get(
        "user",
        {},
    )

    if not isinstance(
        user,
        dict,
    ):

        user = {}

    user_id = user.get(
        "id"
    )

    if user_id:

        user_profile = (
            fetch_user_profile(
                user_id
            )
        )

        if user_profile:

            user = {
                **user,
                **user_profile,
            }

            item["user"] = user

    # ----------------------------------------------------------
    # Seller
    # ----------------------------------------------------------

    seller = (
        user.get(
            "login"
        )
        or user.get(
            "username"
        )
        or "Unknown seller"
    )

    seller_id = user.get(
        "id"
    )

    seller_url = (
        f"{VINTED_BASE_URL}"
        f"/member/{seller_id}"
        if seller_id
        else item_url
    )

    # ----------------------------------------------------------
    # Price
    # ----------------------------------------------------------

    price_obj = item.get(
        "price",
        {},
    )

    if isinstance(
        price_obj,
        dict,
    ):

        amount = (
            price_obj.get(
                "amount"
            )
            or price_obj.get(
                "value"
            )
            or "?"
        )

    else:

        amount = (
            price_obj
            or "?"
        )

    price_str = (
        f"{CURRENCY_SYMBOL}"
        f"{amount}"
    )

    # ----------------------------------------------------------
    # Brand / size
    # ----------------------------------------------------------

    brand = (
        item.get(
            "brand_title"
        )
        or item.get(
            "brand"
        )
        or "—"
    )

    if isinstance(
        brand,
        dict,
    ):

        brand = (
            brand.get(
                "title"
            )
            or brand.get(
                "name"
            )
            or "—"
        )

    size = (
        item.get(
            "size_title"
        )
        or item.get(
            "size"
        )
        or "—"
    )

    if isinstance(
        size,
        dict,
    ):

        size = (
            size.get(
                "title"
            )
            or size.get(
                "name"
            )
            or "—"
        )

    # ----------------------------------------------------------
    # Condition
    # ----------------------------------------------------------

    raw_status = (
        item.get(
            "status"
        )
        or ""
    )

    status_id = item.get(
        "status_id"
    )

    try:

        status_id = int(
            status_id
        )

    except (
        TypeError,
        ValueError,
    ):

        status_id = None

    condition = (
        CONDITION_LABELS.get(
            status_id,
            raw_status,
        )
        if status_id
        else (
            raw_status
            or "—"
        )
    )

    # ----------------------------------------------------------
    # Published
    # ----------------------------------------------------------

    created_at = (
        item.get(
            "created_at_ts"
        )
        or item.get(
            "created_at"
        )
        or item.get(
            "updated_at_ts"
        )
        or item.get(
            "updated_at"
        )
        or (
            item.get(
                "item_box"
            )
            or {}
        ).get(
            "created_at_ts"
        )
        or (
            item.get(
                "item_box"
            )
            or {}
        ).get(
            "created_at"
        )
    )

    if created_at:

        try:

            unix_ts = int(
                float(
                    str(
                        created_at
                    )
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            try:

                from datetime import (
                    timezone as tz
                )

                s = str(
                    created_at
                )[:19]

                dt = datetime.strptime(
                    s,
                    "%Y-%m-%dT%H:%M:%S",
                ).replace(
                    tzinfo=tz.utc
                )

                unix_ts = int(
                    dt.timestamp()
                )

            except Exception:

                unix_ts = int(
                    time.time()
                )

    else:

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
        user.get(
            "feedback_reputation"
        )
        or user.get(
            "feedback_score"
        )
        or item.get(
            "feedback_reputation"
        )
    )

    feedback_count = (
        user.get(
            "positive_feedback_count"
        )
        or user.get(
            "feedback_count"
        )
        or user.get(
            "feedback_count_total"
        )
        or 0
    )

    stars = star_rating(
        feedback_score
    )

    feedback_str = (
        f"{stars} "
        f"({feedback_count})"
    )

    # ----------------------------------------------------------
    # Photo
    # ----------------------------------------------------------

    photos = item.get(
        "photos",
        [],
    )

    image_url = None

    if isinstance(
        photos,
        list,
    ) and photos:

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

                if (
                    isinstance(
                        thumbnails,
                        list,
                    )
                    and thumbnails
                ):

                    for thumbnail in reversed(
                        thumbnails
                    ):

                        if not isinstance(
                            thumbnail,
                            dict,
                        ):

                            continue

                        image_url = (
                            thumbnail.get(
                                "url"
                            )
                        )

                        if image_url:
                            break

    # Newer/alternate response format.
    if not image_url:

        photo = item.get(
            "photo"
        )

        if isinstance(
            photo,
            dict,
        ):

            image_url = (
                photo.get(
                    "full_size_url"
                )
                or photo.get(
                    "url"
                )
            )

    if not image_url:

        image_url = (
            item.get(
                "photo_url"
            )
            or item.get(
                "image_url"
            )
        )

    # ----------------------------------------------------------
    # Buy / offer URLs
    # ----------------------------------------------------------

    buy_url = (
        f"{VINTED_BASE_URL}"
        f"/transaction/buy/item/"
        f"{item_id}"
        if item_id
        else item_url
    )

    negotiate_url = (
        f"{VINTED_BASE_URL}"
        f"/items/{item_id}/make_offer"
        if item_id
        else item_url
    )

    # Keep these variables because the original bot created them.
    # The Discord message itself continues to use the listing URL.
    details_url = item_url

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

        "url": details_url,

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

        embed[
            "image"
        ] = {
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

                    "url": details_url,
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

    target_channel = (
        channel_id
        if channel_id
        else DISCORD_CHANNEL_ID
    )

    if not target_channel:

        print(
            f" [!] No channel ID "
            f"configured for '{label}' "
            f"— skipping"
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

        "Content-Type": (
            "application/json"
        ),
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
            "DISCORD_BOT_TOKEN "
            "secret is missing"
        )

    if not DISCORD_CHANNEL_ID:

        errors.append(
            "DISCORD_CHANNEL_ID "
            "secret is missing"
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
            "No searches configured "
            "— nothing to do."
        )

        return

    print(
        "=" * 55
    )

    print(
        " Vinted -> Discord Alert Bot"
    )

    print(
        f" Checking every "
        f"{CHECK_INTERVAL}s "
        f"for {RUN_DURATION}s"
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
            f" | exclude: "
            f"{', '.join(excl)}"
            if excl
            else ""
        )

        ch_str = (
            f" | channel: "
            f"{s['channel_id']}"
            if s.get(
                "channel_id"
            )
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

    # Bootstrap Vinted before searching.
    get_vinted_session_cookie()

    seen = load_seen()

    # ----------------------------------------------------------
    # FIRST RUN
    # ----------------------------------------------------------

    first_run = not bool(
        seen
    )

    if first_run:

        print(
            "First run — seeding "
            "existing listings "
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
                [],
            )

            for item in items:

                if not item.get(
                    "id"
                ):

                    continue

                iid = str(
                    item[
                        "id"
                    ]
                )

                if iid not in seen[
                    key
                ]:

                    seen[
                        key
                    ].append(
                        iid
                    )

        save_seen(
            seen
        )

        print(
            "Done. Future runs "
            "will alert on new "
            "listings.\n"
        )

        return

    # ----------------------------------------------------------
    # SEARCH COLOURS
    # ----------------------------------------------------------

    label_colours = {
        s["label"]: COLOURS[
            i % len(COLOURS)
        ]

        for i, s in enumerate(
            searches
        )
    }

    # ----------------------------------------------------------
    # POLLING LOOP
    # ----------------------------------------------------------

    start_time = time.time()

    checks = 0

    while (
        time.time()
        - start_time
        < RUN_DURATION
    ):

        checks += 1

        ts = datetime.now().strftime(
            "%H:%M:%S"
        )

        print(
            f"[{ts}] "
            f"Check #{checks}...",
            end=" ",
            flush=True,
        )

        found_new = 0

        for search in searches:

            key = search[
                "label"
            ]

            colour = (
                label_colours.get(
                    key,
                    COLOURS[0],
                )
            )

            exclude_words = (
                search.get(
                    "exclude_words",
                    [],
                )
            )

            items = fetch_listings(
                search
            )

            seen.setdefault(
                key,
                [],
            )

            for item in items:

                if not item.get(
                    "id"
                ):

                    continue

                iid = str(
                    item[
                        "id"
                    ]
                )

                if iid in seen[
                    key
                ]:

                    continue

                # --------------------------------------------------
                # IMPORTANT:
                #
                # Apply block words BEFORE sending to Discord.
                #
                # We still mark the item as seen so that a blocked
                # listing doesn't repeatedly get checked.
                # --------------------------------------------------

                seen[
                    key
                ].append(
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
                f"{found_new} "
                f"new item(s)."
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