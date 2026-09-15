"""
Vinted → Discord Alert Bot
==========================
- Reads searches from vinted_searches.json (managed via dashboard)
- Uses Discord Bot Token for real working buttons
- Loops every 5 seconds for 55 seconds per run
- Supports exclude words, multiple keywords, all condition types

GitHub Secrets needed:
DISCORD_BOT_TOKEN — your Discord bot token
DISCORD_CHANNEL_ID — right-click channel in Discord → Copy Channel ID
"""

import time
import json
import os
import requests
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

CHECK_INTERVAL = 5
RUN_DURATION = 55
VINTED_DOMAIN = "www.vinted.co.uk"
CURRENCY_SYMBOL = "£"

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
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": f"https://{VINTED_DOMAIN}/",
    "Origin": f"https://{VINTED_DOMAIN}",
}

COLOURS = [0x09B1BA, 0xF5A623, 0x7ED321, 0xD0021B, 0x9B59B6, 0x3498DB]

SESSION = requests.Session()

# ── Searches ───────────────────────────────────
def load_searches() -> list:
    if os.path.exists(SEARCHES_FILE):
        with open(SEARCHES_FILE) as f:
            all_searches = json.load(f)
        enabled = [s for s in all_searches if s.get("enabled", True)]
        if enabled:
            print(f" Loaded {len(enabled)} search(es) from {SEARCHES_FILE}")
            return enabled
    print(f" Using fallback searches")
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

# ── Vinted API ─────────────────────────────────
def get_vinted_session_cookie():
    try:
        SESSION.get(f"https://{VINTED_DOMAIN}/", headers=VINTED_HEADERS, timeout=10)
    except requests.RequestException:
        pass

def fetch_listings(search: dict) -> list:
    params = {
        "search_text": search["search_text"],
        "order": search.get("order", "newest_first"),
        "per_page": 40,
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

    url = f"https://{VINTED_DOMAIN}/api/v2/catalog/items"

    # Try once, and if we get a 401/403/404 (session dead / blocked),
    # refresh the session cookie and retry a single time before giving up.
    for attempt in range(2):
        try:
            r = SESSION.get(url, params=params, headers=VINTED_HEADERS, timeout=15)
            r.raise_for_status()
            return r.json().get("items", [])
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403, 404) and attempt == 0:
                print(f" [!] Got {status} — refreshing session cookie and retrying...")
                get_vinted_session_cookie()
                continue
            print(f" [!] HTTP error: {e}")
        except requests.RequestException as e:
            print(f" [!] Request error: {e}")
        break
    return []

def fetch_user_profile(user_id) -> dict:
    """Fetch user profile to get feedback rating."""
    url = f"https://{VINTED_DOMAIN}/api/v2/users/{user_id}"
    try:
        r = SESSION.get(url, headers=VINTED_HEADERS, timeout=10)
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
        f"https://{VINTED_DOMAIN}/api/v2/items/{item_id}",
        f"https://{VINTED_DOMAIN}/api/v2/catalog/items/{item_id}",
    ]
    for url in urls:
        try:
            r = SESSION.get(url, headers=VINTED_HEADERS, timeout=10)
            print(f" [debug] {url} -> {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f" [debug] top keys = {list(data.keys())}")
                item = data.get("item", data)
                user = item.get("user", {})
                print(f" [debug] item keys = {list(item.keys())[:15]}")
                print(f" [debug] user keys = {list(user.keys())[:15]}")
                print(f" [debug] created_at = {repr(item.get('created_at'))}")
                print(f" [debug] created_at_ts = {repr(item.get('created_at_ts'))}")
                print(f" [debug] feedback_reputation = {repr(user.get('feedback_reputation'))}")
                return item
        except Exception as e:
            print(f" [debug] exception: {e}")
    return {}

def matches_exclude_words(item: dict, exclude_words: list) -> bool:
    """Returns True if item title contains any excluded word."""
    if not exclude_words:
        return False
    title = (item.get("title") or "").lower()
    for word in exclude_words:
        if word.lower().strip() in title:
            return True
    return False

# ── Discord helpers ────────────────────────────
def time_ago(value) -> str:
    if not value:
        return "Unknown"
    ts = None
    # Try Unix timestamp
    try:
        ts = int(float(str(value)))
    except (TypeError, ValueError):
        pass
    # Try ISO 8601 string e.g. "2024-01-15T10:30:00+00:00"
    if ts is None:
        try:
            from datetime import timezone as tz
            s = str(value)[:19]  # take just "2024-01-15T10:30:00"
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=tz.utc)
            ts = int(dt.timestamp())
        except Exception:
            return "Unknown"
    diff = int(time.time()) - ts
    if diff < 0:
        return "just now"
    if diff < 60:
        return f"{diff} second{'s' if diff != 1 else ''} ago"
    elif diff < 3600:
        m = diff // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    elif diff < 86400:
        h = diff // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    else:
        d = diff // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"

def star_rating(reputation) -> str:
    """
    Vinted feedback_reputation is a float between 0.0 and 1.0.
    Convert to 0-5 stars.
    """
    if reputation is None:
        return "No ratings"
    try:
        score = float(reputation)
        # reputation is 0.0 to 1.0 — multiply by 5 for star count
        stars = round(score * 5)
        stars = max(0, min(5, stars))
        return "⭐" * stars + "✩" * (5 - stars)
    except (TypeError, ValueError):
        return "No ratings"

def get_item_url(item: dict) -> str:
    url = item.get("url", "")
    if url and not url.startswith("http"):
        url = f"https://{VINTED_DOMAIN}{url}"
    return url

def build_payload(label: str, item: dict, colour: int) -> dict:
    item_url = get_item_url(item)
    item_id = item.get("id", "")

    # Fetch user profile for feedback rating (public endpoint)
    user_id = item.get("user", {}).get("id")
    if user_id:
        user_profile = fetch_user_profile(user_id)
        if user_profile:
            item["user"] = {**item.get("user", {}), **user_profile}

    # Construct specific action URLs
    buy_url = f"https://{VINTED_DOMAIN}/transaction/buy/item/{item_id}" if item_id else item_url
    negotiate_url = f"https://{VINTED_DOMAIN}/items/{item_id}/make_offer" if item_id else item_url
    details_url = item_url

    # Seller
    user = item.get("user", {})
    seller = user.get("login", "Unknown seller")
    seller_id = user.get("id")
    seller_url = f"https://{VINTED_DOMAIN}/member/{seller_id}" if seller_id else item_url

    # Price
    price_obj = item.get("price", {})
    amount = price_obj.get("amount", "?")
    price_str = f"{CURRENCY_SYMBOL}{amount}"

    # Fields
    brand = item.get("brand_title") or "—"
    size = item.get("size_title") or "—"

    # Condition — use label map
    raw_status = item.get("status") or ""
    status_id = item.get("status_id")
    condition = CONDITION_LABELS.get(status_id, raw_status) if status_id else raw_status or "—"

    # Published — use Discord relative timestamp (<t:unix:R> = "X minutes ago")
    # Vinted search API does not include created_at so we use current time
    created_at = (
        item.get("created_at_ts")
        or item.get("created_at")
        or item.get("updated_at_ts")
        or item.get("updated_at