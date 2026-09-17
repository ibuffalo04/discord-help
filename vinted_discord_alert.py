"""
Vinted → Discord Alert Bot
==========================
- Reads searches from vinted_searches.json (managed via dashboard)
- Uses Discord Bot Token for real working buttons
- Runs continuously for 55 minutes per GitHub Actions run
- Checks Vinted every 10 seconds
- Supports exclude words, multiple keywords, all condition types
- Automatically backs off after Vinted HTTP 403 responses
- First 403 waits 2 minutes
- Second consecutive 403 waits 5 minutes
- Third consecutive 403 ends the runner early
- Prints Cloudflare diagnostics when Vinted returns HTTP 403

GitHub Secrets needed:
DISCORD_BOT_TOKEN — your Discord bot token
DISCORD_CHANNEL_ID — right-click channel in Discord → Copy Channel ID
"""

import time
import json
import os
import re
import html
import requests
from html.parser import HTMLParser
from urllib.parse import urljoin
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# DISCORD CONFIG
# ──────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

# ──────────────────────────────────────────────
# FALLBACK SEARCHES (used if vinted_searches.json missing)
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

CHECK_INTERVAL = 10

# One Python process runs for the full 55-minute GitHub job.
RUN_DURATION = 3300

# 403 handling:
# First 403 = 2-minute cooldown
# Second consecutive 403 = 5-minute cooldown
# Third consecutive 403 = end this runner early
FIRST_403_COOLDOWN = 120
SECOND_403_COOLDOWN = 300
MAX_403_STREAK = 3

VINTED_DOMAIN = "www.vinted.co.uk"
CURRENCY_SYMBOL = "£"

# Shared Vinted block state.
VINTED_COOLDOWN_UNTIL = 0
VINTED_403_STREAK = 0
STOP_SCANNER = False

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

STATE_FILE = "vinted_seen_ids.json"
SEARCHES_FILE = "vinted_searches.json"

VINTED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": f"https://{VINTED_DOMAIN}/",
}

COLOURS = [
    0x09B1BA,
    0xF5A623,
    0x7ED321,
    0xD0021B,
    0x9B59B6,
    0x3498DB,
]

SESSION = requests.Session()


# ── Searches ───────────────────────────────────
def load_searches() -> list:
    if os.path.exists(SEARCHES_FILE):
        with open(SEARCHES_FILE) as f:
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

    print(" Using fallback searches")
    return FALLBACK_SEARCHES


# ── State ──────────────────────────────────────
def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)

    return {}


def save_seen(seen: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(seen, f)


# ── Vinted HTML parser ─────────────────────────
class VintedListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.items = []
        self.current = None
        self.stack = []

        self.capture = None
        self.capture_tag = None
        self.capture_text = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        testid = attrs.get("data-testid", "")

        # Current Vinted catalogue cards use:
        # div[data-testid^="product-item-id-"]
        if (
            tag == "div"
            and testid.startswith("product-item-id-")
        ):
            item_id = testid.replace(
                "product-item-id-",
                "",
                1
            )

            if item_id.isdigit():
                self.current = {
                    "id": item_id,
                    "title": "",
                    "url": "",
                    "image_url": "",
                    "brand_title": "",
                    "size_title": "",
                    "status": "",
                    "price": {
                        "amount": ""
                    },
                    "user": {},
                    "photos": [],
                }

                self.stack = ["card"]
                return

        if self.current is None:
            return

        self.stack.append(tag)

        # Listing URL + ACTUAL LISTING TITLE
        if tag == "a":
            href = attrs.get("href", "")

            if (
                href
                and "/items/" in href
                and not self.current["url"]
            ):
                self.current["url"] = urljoin(
                    f"https://{VINTED_DOMAIN}/",
                    href
                )

            # The overlay link contains the real listing title,
            # followed by extra Vinted metadata such as:
            # ", Brand: Xbox, Condition: ..., 11.00 £, 12.25 £"
            if testid.endswith("--overlay-link"):
                link_title = attrs.get("title", "")

                if link_title:
                    link_title = html.unescape(
                        link_title
                    ).strip()

                    # Remove Vinted's appended metadata while
                    # preserving commas in the genuine title.
                    metadata_match = re.search(
                        r",\s*(?:Brand|Condition|Size):\s*",
                        link_title,
                        flags=re.IGNORECASE,
                    )

                    if metadata_match:
                        link_title = link_title[
                            :metadata_match.start()
                        ].strip()

                    self.current["title"] = link_title

        # Main image
        if tag == "img":
            src = (
                attrs.get("src")
                or attrs.get("data-src")
            )

            if (
                src
                and not self.current["image_url"]
            ):
                self.current["image_url"] = src

            # Fallback if the overlay title isn't available.
            if not self.current["title"]:
                alt = attrs.get("alt", "")

                if alt:
                    self.current["title"] = html.unescape(
                        alt
                    ).strip()

        # Text fields
        # IMPORTANT:
        # description-title is the brand/short title,
        # NOT the actual listing title.
        if testid.endswith("--description-title"):
            self.capture = "brand"
            self.capture_tag = tag
            self.capture_text = ""

        elif testid.endswith("--description-subtitle"):
            self.capture = "subtitle"
            self.capture_tag = tag
            self.capture_text = ""

        elif testid.endswith("--price-text"):
            self.capture = "price"
            self.capture_tag = tag
            self.capture_text = ""

    def handle_endtag(self, tag):
        if self.current is None:
            return

        # Only finish capturing when the SAME element that
        # started the capture has closed.
        if (
            self.capture
            and tag == self.capture_tag
        ):
            text = " ".join(
                self.capture_text.split()
            )

            if (
                self.capture == "brand"
                and text
            ):
                self.current["brand_title"] = text

            elif (
                self.capture == "subtitle"
                and text
            ):
                self._parse_subtitle(text)

            elif (
                self.capture == "price"
                and text
            ):
                self._parse_price(text)

            self.capture = None
            self.capture_tag = None
            self.capture_text = ""

        if self.stack:
            self.stack.pop()

        if tag == "div" and not self.stack:
            self.items.append(self.current)
            self.current = None

    def handle_data(self, data):
        if (
            self.current is not None
            and self.capture
        ):
            self.capture_text += data

    def _parse_price(self, text):
        # Handles £12.00, £12, 12.00 £ etc.
        match = re.search(
            r"([0-9]+(?:[.,][0-9]+)?)",
            text
        )

        if match:
            amount = (
                match
                .group(1)
                .replace(",", ".")
            )

            self.current["price"]["amount"] = amount

    def _parse_subtitle(self, text):
        # Vinted normally puts size/condition together.
        parts = [
            p.strip()
            for p in text.split("·")
            if p.strip()
        ]

        if len(parts) >= 1:
            self.current["size_title"] = parts[0]

        if len(parts) >= 2:
            self.current["status"] = parts[1]

        if len(parts) >= 3:
            self.current["status"] = parts[2]


def parse_vinted_catalogue(page_html: str) -> list:
    parser = VintedListingParser()
    parser.feed(page_html)

    # Only return genuine catalogue items.
    items = []
    seen_ids = set()

    for item in parser.items:
        item_id = str(
            item.get("id", "")
        )

        if (
            not item_id
            or item_id in seen_ids
        ):
            continue

        seen_ids.add(item_id)

        if item.get("image_url"):
            item["photos"] = [
                {
                    "url": item["image_url"],
                    "full_size_url": item["image_url"],
                }
            ]

        items.append(item)

    return items


# ── Vinted API ─────────────────────────────────
def get_vinted_session_cookie():
    try:
        SESSION.get(
            f"https://{VINTED_DOMAIN}/",
            headers=VINTED_HEADERS,
            timeout=10
        )

    except requests.RequestException:
        pass


def print_403_diagnostics(response):
    """
    Print safe response headers that help identify
    whether Cloudflare/Vinted is challenging the runner.
    """
    server = response.headers.get(
        "Server",
        "Not provided"
    )

    cf_mitigated = response.headers.get(
        "CF-Mitigated",
        "Not provided"
    )

    cf_ray = response.headers.get(
        "CF-Ray",
        "Not provided"
    )

    content_type = response.headers.get(
        "Content-Type",
        "Not provided"
    )

    retry_after = response.headers.get(
        "Retry-After",
        "Not provided"
    )

    print(
        f" [403 debug] Server: {server}"
    )

    print(
        f" [403 debug] CF-Mitigated: {cf_mitigated}"
    )

    print(
        f" [403 debug] CF-Ray: {cf_ray}"
    )

    print(
        f" [403 debug] Content-Type: {content_type}"
    )

    print(
        f" [403 debug] Retry-After: {retry_after}"
    )


def fetch_listings(search: dict) -> list:
    global VINTED_COOLDOWN_UNTIL
    global VINTED_403_STREAK
    global STOP_SCANNER

    now = time.time()

    # If Vinted has recently returned HTTP 403,
    # don't send another request until the cooldown expires.
    if now < VINTED_COOLDOWN_UNTIL:
        remaining = max(
            1,
            int(VINTED_COOLDOWN_UNTIL - now)
        )

        print(
            f" [!] Vinted cooldown active "
            f"({remaining}s remaining)."
        )

        return []

    # Cooldown has expired.
    # Keep the existing Vinted session and simply retry
    # the catalogue instead of clearing cookies and
    # making an extra homepage request.
    if VINTED_COOLDOWN_UNTIL:
        print(
            " [Vinted] Cooldown finished — "
            "retrying catalogue."
        )

        VINTED_COOLDOWN_UNTIL = 0

    params = {
        "search_text": search["search_text"],
        "order": search.get(
            "order",
            "newest_first"
        ),
        "page": 1,
    }

    if search.get("max_price"):
        params["price_to"] = search["max_price"]

    if search.get("min_price"):
        params["price_from"] = search["min_price"]

    if search.get("size_ids"):
        params["size_ids[]"] = search["size_ids"]

    if search.get("brand_ids"):
        params["brand_ids[]"] = search["brand_ids"]

    if search.get("status_ids"):
        params["status_ids[]"] = search["status_ids"]

    url = (
        f"https://{VINTED_DOMAIN}/catalog"
    )

    try:
        r = SESSION.get(
            url,
            params=params,
            headers=VINTED_HEADERS,
            timeout=20
        )

        if r.status_code == 403:
            print_403_diagnostics(r)

            VINTED_403_STREAK += 1

            # First consecutive 403:
            # wait 2 minutes.
            if VINTED_403_STREAK == 1:
                cooldown_seconds = FIRST_403_COOLDOWN

                VINTED_COOLDOWN_UNTIL = (
                    time.time()
                    + cooldown_seconds
                )

                print(
                    f" [!] Vinted catalogue returned "
                    f"HTTP 403. Cooling down for "
                    f"{cooldown_seconds}s."
                )

                return []

            # Second consecutive 403:
            # wait 5 minutes.
            if VINTED_403_STREAK == 2:
                cooldown_seconds = SECOND_403_COOLDOWN

                VINTED_COOLDOWN_UNTIL = (
                    time.time()
                    + cooldown_seconds
                )

                print(
                    f" [!] Vinted catalogue returned "
                    f"HTTP 403 again. Cooling down for "
                    f"{cooldown_seconds}s."
                )

                return []

            # Third consecutive 403:
            # this runner appears to be stuck behind a
            # Cloudflare challenge. End it cleanly so the
            # next queued GitHub runner can take over.
            if VINTED_403_STREAK >= MAX_403_STREAK:
                STOP_SCANNER = True

                print(
                    " [!] Third consecutive Vinted HTTP 403."
                )

                print(
                    " [!] Ending this scanner early so the "
                    "next queued GitHub run can take over."
                )

                return []

        if r.status_code != 200:
            print(
                f" [!] Vinted catalogue returned HTTP "
                f"{r.status_code}."
            )

            return []

        # Successful request means the temporary 403 block
        # has cleared. Reset everything so a future block
        # starts again with the 2-minute cooldown.
        if VINTED_403_STREAK:
            print(
                " [Vinted] 403 block cleared — "
                "cooldown reset."
            )

            VINTED_403_STREAK = 0
            VINTED_COOLDOWN_UNTIL = 0

        items = parse_vinted_catalogue(
            r.text
        )

        if not items:
            print(
                " [!] Catalogue loaded but no listing "
                "cards were found."
            )

            return []

        print(
            f" [Vinted] Catalogue returned "
            f"{len(items)} listing(s)."
        )

        return items

    except requests.RequestException as e:
        print(
            f" [!] Vinted request error: {e}"
        )

        return []


def fetch_user_profile(user_id) -> dict:
    """Fetch user profile to get feedback rating."""
    url = (
        f"https://{VINTED_DOMAIN}/api/v2/"
        f"users/{user_id}"
    )

    try:
        r = SESSION.get(
            url,
            headers=VINTED_HEADERS,
            timeout=10
        )

        if r.status_code == 200:
            data = r.json()
            return data.get("user", {})

    except Exception:
        pass

    return {}


def fetch_item_details(item_id) -> dict:
    """Fetch full item details to get rating, date etc."""
    # Try both known Vinted API endpoints
    urls = [
        (
            f"https://{VINTED_DOMAIN}/api/v2/"
            f"items/{item_id}"
        ),
        (
            f"https://{VINTED_DOMAIN}/api/v2/"
            f"catalog/items/{item_id}"
        ),
    ]

    for url in urls:
        try:
            r = SESSION.get(
                url,
                headers=VINTED_HEADERS,
                timeout=10
            )

            print(
                f" [debug] {url} -> "
                f"{r.status_code}"
            )

            if r.status_code == 200:
                data = r.json()

                print(
                    f" [debug] top keys = "
                    f"{list(data.keys())}"
                )

                item = data.get(
                    "item",
                    data
                )

                user = item.get(
                    "user",
                    {}
                )

                print(
                    f" [debug] item keys = "
                    f"{list(item.keys())[:15]}"
                )

                print(
                    f" [debug] user keys = "
                    f"{list(user.keys())[:15]}"
                )

                print(
                    f" [debug] created_at = "
                    f"{repr(item.get('created_at'))}"
                )

                print(
                    f" [debug] created_at_ts = "
                    f"{repr(item.get('created_at_ts'))}"
                )

                print(
                    f" [debug] feedback_reputation = "
                    f"{repr(user.get('feedback_reputation'))}"
                )

                return item

        except Exception as e:
            print(
                f" [debug] exception: {e}"
            )

    return {}


def matches_exclude_words(
    item: dict,
    exclude_words: list
) -> bool:
    """
    Returns True if item title contains
    any excluded word.
    """
    if not exclude_words:
        return False

    title = (
        item.get("title")
        or ""
    ).lower()

    for word in exclude_words:
        if (
            word.lower().strip()
            in title
        ):
            return True

    return False


# ── Discord helpers ────────────────────────────
def time_ago(value) -> str:
    if not value:
        return "Unknown"

    ts = None

    # Try Unix timestamp
    try:
        ts = int(
            float(str(value))
        )

    except (TypeError, ValueError):
        pass

    # Try ISO 8601 string
    # e.g. "2024-01-15T10:30:00+00:00"
    if ts is None:
        try:
            from datetime import timezone as tz

            s = str(value)[:19]

            dt = datetime.strptime(
                s,
                "%Y-%m-%dT%H:%M:%S"
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

    elif diff < 3600:
        m = diff // 60

        return (
            f"{m} minute"
            f"{'s' if m != 1 else ''} ago"
        )

    elif diff < 86400:
        h = diff // 3600

        return (
            f"{h} hour"
            f"{'s' if h != 1 else ''} ago"
        )

    else:
        d = diff // 86400

        return (
            f"{d} day"
            f"{'s' if d != 1 else ''} ago"
        )


def star_rating(reputation) -> str:
    """
    Vinted feedback_reputation is a float
    between 0.0 and 1.0.

    Convert to 0-5 stars.
    """
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
            min(5, stars)
        )

        return (
            "⭐" * stars
            + "✩" * (5 - stars)
        )

    except (TypeError, ValueError):
        return "No ratings"


def get_item_url(item: dict) -> str:
    url = item.get(
        "url",
        ""
    )

    if (
        url
        and not url.startswith("http")
    ):
        url = (
            f"https://{VINTED_DOMAIN}"
            f"{url}"
        )

    return url


def build_payload(
    label: str,
    item: dict,
    colour: int
) -> dict:
    item_url = get_item_url(
        item
    )

    item_id = item.get(
        "id",
        ""
    )

    # Fetch user profile for feedback rating
    # (public endpoint)
    user_id = (
        item
        .get("user", {})
        .get("id")
    )

    if user_id:
        user_profile = fetch_user_profile(
            user_id
        )

        if user_profile:
            item["user"] = {
                **item.get("user", {}),
                **user_profile
            }

    # Construct specific action URLs
    buy_url = (
        f"https://{VINTED_DOMAIN}/"
        f"transaction/buy/item/{item_id}"
        if item_id
        else item_url
    )

    negotiate_url = (
        f"https://{VINTED_DOMAIN}/"
        f"items/{item_id}/make_offer"
        if item_id
        else item_url
    )

    details_url = item_url

    # Seller
    user = item.get(
        "user",
        {}
    )

    seller = user.get(
        "login",
        "Unknown seller"
    )

    seller_id = user.get(
        "id"
    )

    seller_url = (
        f"https://{VINTED_DOMAIN}/"
        f"member/{seller_id}"
        if seller_id
        else item_url
    )

    # Price
    price_obj = item.get(
        "price",
        {}
    )

    amount = price_obj.get(
        "amount",
        "?"
    )

    price_str = (
        f"{CURRENCY_SYMBOL}{amount}"
    )

    # Fields
    brand = (
        item.get("brand_title")
        or "—"
    )

    size = (
        item.get("size_title")
        or "—"
    )

    # Condition — use label map
    raw_status = (
        item.get("status")
        or ""
    )

    status_id = item.get(
        "status_id"
    )

    condition = (
        CONDITION_LABELS.get(
            status_id,
            raw_status
        )
        if status_id
        else raw_status or "—"
    )

    # Published
    created_at = (
        item.get("created_at_ts")
        or item.get("created_at")
        or item.get("updated_at_ts")
        or item.get("updated_at")
        or (
            item.get("item_box")
            or {}
        ).get("created_at_ts")
        or (
            item.get("item_box")
            or {}
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
            ValueError
        ):
            try:
                from datetime import timezone as tz

                s = str(
                    created_at
                )[:19]

                dt = datetime.strptime(
                    s,
                    "%Y-%m-%dT%H:%M:%S"
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

    # Feedback
    feedback_score = (
        user.get("feedback_reputation")
        or user.get("feedback_score")
        or item.get(
            "user",
            {}
        ).get(
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
        or 0
    )

    stars = star_rating(
        feedback_score
    )

    feedback_str = (
        f"{stars} ({feedback_count})"
    )

    # Photo
    photos = item.get(
        "photos",
        []
    )

    image_url = None

    if photos:
        image_url = (
            photos[0].get(
                "full_size_url"
            )
            or photos[0].get(
                "url"
            )
            or (
                photos[0].get(
                    "thumbnails"
                )
                or [{}]
            )[-1].get(
                "url"
            )
        )

    embed = {
        "author": {
            "name": f"👤 {seller}",
            "url": seller_url
        },

        "title": item.get(
            "title",
            "New listing"
        ),

        "url": item_url,
        "color": colour,

        "fields": [
            {
                "name": "⏳ Published",
                "value": published,
                "inline": True
            },
            {
                "name": "🏷️ Brand",
                "value": brand,
                "inline": True
            },
            {
                "name": "📐 Size",
                "value": size,
                "inline": True
            },
            {
                "name": "⭐ Feedbacks",
                "value": feedback_str,
                "inline": True
            },
            {
                "name": "💎 Status",
                "value": condition,
                "inline": True
            },
            {
                "name": "💰 Price",
                "value": price_str,
                "inline": True
            },
        ],

        "footer": {
            "text": (
                f"🔍 Search: {label}"
            )
        },

        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    if image_url:
        embed["image"] = {
            "url": image_url
        }

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
                    "url": item_url
                },
            ],
        }
    ]

    return {
        "embeds": [embed],
        "components": components
    }


def send_discord(
    label: str,
    item: dict,
    colour: int,
    channel_id: str = None
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
            f" [!] No channel ID configured for "
            f"'{label}' — skipping"
        )

        return

    payload = build_payload(
        label,
        item,
        colour
    )

    url = (
        f"https://discord.com/api/v10/"
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
            timeout=10
        )

        r.raise_for_status()

    except requests.HTTPError as e:
        print(
            f" [!] Discord error "
            f"{e.response.status_code}: "
            f"{e.response.text}"
        )

    except requests.RequestException as e:
        print(
            f" [!] Discord error: {e}"
        )


# ── Validation ─────────────────────────────────
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


# ── Main ───────────────────────────────────────
def run():
    global STOP_SCANNER

    validate()

    searches = load_searches()

    if not searches:
        print(
            "No searches configured — "
            "nothing to do."
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
        f"for 55 minutes"
    )

    print(
        "=" * 55
    )

    for s in searches:
        excl = s.get(
            "exclude_words",
            []
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
            if s.get("channel_id")
            else (
                f" | channel: default "
                f"({DISCORD_CHANNEL_ID})"
            )
        )

        print(
            f" * {s['label']}"
            f"{excl_str}"
            f"{ch_str}"
        )

    print()

    # Create one Vinted session at the beginning of the
    # 55-minute process and reuse it throughout the run.
    get_vinted_session_cookie()

    seen = load_seen()

    # Seed on very first run
    first_run = not bool(
        seen
    )

    if first_run:
        print(
            "First run — seeding existing "
            "listings (no alerts)..."
        )

        for search in searches:
            key = search["label"]

            items = fetch_listings(
                search
            )

            seen.setdefault(
                key,
                []
            )

            for item in items:
                seen[key].append(
                    str(
                        item["id"]
                    )
                )

        save_seen(
            seen
        )

        print(
            "Done. Future runs will "
            "alert on new listings.\n"
        )

        return

    label_colours = {
        s["label"]:
        COLOURS[
            i % len(COLOURS)
        ]

        for i, s
        in enumerate(searches)
    }

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
            f"[{ts}] Check #{checks}...",
            end=" ",
            flush=True
        )

        found_new = 0

        for search in searches:
            key = search["label"]

            colour = label_colours.get(
                key,
                COLOURS[0]
            )

            exclude_words = search.get(
                "exclude_words",
                []
            )

            items = fetch_listings(
                search
            )

            seen.setdefault(
                key,
                []
            )

            for item in items:
                iid = str(
                    item["id"]
                )

                if iid in seen[key]:
                    continue

                seen[key].append(
                    iid
                )

                if matches_exclude_words(
                    item,
                    exclude_words
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
                    )
                )

                found_new += 1

            if STOP_SCANNER:
                break

        # Save state before potentially ending early.
        save_seen(
            seen
        )

        if STOP_SCANNER:
            print(
                "Scanner stopped early due to repeated "
                "Cloudflare 403 challenges."
            )

            break

        print(
            f"{found_new} new item(s)."
            if found_new
            else "nothing new."
        )

        time.sleep(
            CHECK_INTERVAL
        )

    print(
        " Vinted scanner finished."
    )


if __name__ == "__main__":
    run()