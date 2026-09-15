import os
import json
import time
import re
import html
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

VINTED_DOMAIN = "www.vinted.co.uk"
BASE_URL = f"https://{VINTED_DOMAIN}"

SEARCHES_FILE = "vinted_searches.json"
SEEN_FILE = "vinted_seen_ids.json"

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
VINTED_COOKIE = os.getenv("VINTED_COOKIE", "")

CHECK_INTERVAL = 5
RUN_TIME = 55

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": BASE_URL + "/",
    "Connection": "keep-alive",
})


def bootstrap_session():
    """
    Loads Vinted's catalogue page to establish cookies/session data.
    """

    try:
        r = session.get(
            f"{BASE_URL}/catalog",
            timeout=20,
            allow_redirects=True
        )

        print(f" [Vinted] Session bootstrap: HTTP {r.status_code}")

        if VINTED_COOKIE:
            # Allow a manually supplied cookie to be used as well.
            for part in VINTED_COOKIE.split(";"):
                if "=" in part:
                    name, value = part.strip().split("=", 1)
                    session.cookies.set(
                        name,
                        value,
                        domain=VINTED_DOMAIN
                    )

        token = None

        # Try to locate access_token_web in the returned HTML.
        patterns = [
            r'"access_token_web"\s*:\s*"([^"]+)"',
            r'access_token_web["\']?\s*[:=]\s*["\']([^"\']+)',
            r'access_token_web\\?["\']?\s*[:=]\s*["\']([^"\']+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, r.text)
            if match:
                token = match.group(1)
                break

        if token:
            session.headers.update({
                "Authorization": f"Bearer {token}"
            })
            print(" [Vinted] access_token_web obtained.")
        else:
            print(" [Vinted] No access_token_web found.")

        return True

    except requests.RequestException as e:
        print(f" [!] Session bootstrap failed: {e}")
        return False


# ============================================================
# LOAD SEARCH CONFIG
# ============================================================

def load_searches():
    try:
        with open(SEARCHES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            searches = data.get("searches", data)
        else:
            searches = data

        return searches

    except Exception as e:
        print(f" [!] Failed to load searches: {e}")
        return []


# ============================================================
# SEEN IDS
# ============================================================

def load_seen_ids():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(str(x) for x in data)

        if isinstance(data, dict):
            return set(str(x) for x in data.keys())

    except Exception:
        pass

    return set()


def save_seen_ids(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(
                list(seen)[-10000:],
                f,
                indent=2
            )
    except Exception as e:
        print(f" [!] Failed saving seen IDs: {e}")


# ============================================================
# SEARCH CONFIG HELPERS
# ============================================================

def get_search_value(search, *names, default=None):
    if not isinstance(search, dict):
        return default

    for name in names:
        if name in search:
            return search[name]

    return default


def normalise_words(value):
    if value is None:
        return []

    if isinstance(value, str):
        return [
            x.strip().lower()
            for x in re.split(r"[,|\n]", value)
            if x.strip()
        ]

    if isinstance(value, list):
        return [
            str(x).strip().lower()
            for x in value
            if str(x).strip()
        ]

    return []


# ============================================================
# API SEARCH
# ============================================================

def api_search(search):
    """
    Try Vinted's internal API first.

    This is retained because it may still work for some sessions/IPs.
    If Vinted returns 404/403/challenge HTML, the caller falls back
    to the catalogue page.
    """

    search_text = get_search_value(
        search,
        "search_text",
        "query",
        "search",
        "name",
        default=""
    )

    params = {
        "search_text": search_text,
        "order": "newest_first",
        "per_page": 40,
        "page": 1,
    }

    price_to = get_search_value(
        search,
        "price_to",
        "max_price",
        "maxPrice"
    )

    if price_to is not None and str(price_to) != "":
        try:
            params["price_to"] = int(float(price_to))
        except Exception:
            pass

    status_ids = get_search_value(
        search,
        "status_ids",
        "statuses",
        default=[1, 2, 3, 4]
    )

    if isinstance(status_ids, list):
        for status in status_ids:
            params.setdefault("status_ids[]", []).append(status)

    try:
        r = session.get(
            f"{BASE_URL}/api/v2/catalog/items",
            params=params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/catalog",
            },
            timeout=20,
        )

        if r.status_code != 200:
            print(
                f" [!] Vinted API returned HTTP {r.status_code}."
            )

            # A ~9KB response is commonly the HTML challenge/error
            # page rather than the requested JSON.
            return None

        content_type = r.headers.get("content-type", "").lower()

        if "json" not in content_type:
            try:
                return r.json()
            except Exception:
                return None

        return r.json()

    except requests.RequestException as e:
        print(f" [!] Vinted API request failed: {e}")
        return None

    except ValueError:
        print(" [!] Vinted API returned invalid JSON.")
        return None


# ============================================================
# HTML CATALOGUE FALLBACK
# ============================================================

def extract_next_data(text):
    """
    Extract Next.js __NEXT_DATA__ JSON if present.
    """

    soup = BeautifulSoup(text, "html.parser")

    script = soup.find(
        "script",
        id="__NEXT_DATA__"
    )

    if not script:
        return None

    try:
        return json.loads(script.string or script.get_text())
    except Exception:
        return None


def recursively_find_items(obj):
    """
    Find dictionaries that look like Vinted catalogue items
    anywhere inside a JSON structure.
    """

    results = []

    if isinstance(obj, dict):

        # A normal Vinted item has an id and title.
        if (
            ("id" in obj)
            and (
                "title" in obj
                or "photo" in obj
                or "photos" in obj
            )
        ):
            results.append(obj)

        for value in obj.values():
            results.extend(recursively_find_items(value))

    elif isinstance(obj, list):

        for value in obj:
            results.extend(recursively_find_items(value))

    return results


def parse_price(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value)

    match = re.search(
        r"(\d+(?:[.,]\d{1,2})?)",
        text
    )

    if not match:
        return None

    try:
        return float(
            match.group(1).replace(",", ".")
        )
    except Exception:
        return None


def find_first_value(item, keys):
    for key in keys:
        value = item.get(key)

        if value not in (None, ""):
            return value

    return None


def normalise_item(item):
    """
    Convert the various formats Vinted may return into one
    consistent structure used by the filtering + Discord code.
    """

    item_id = find_first_value(
        item,
        ["id", "item_id"]
    )

    if item_id is None:
        return None

    title = find_first_value(
        item,
        [
            "title",
            "name",
        ]
    ) or "Vinted listing"

    price = find_first_value(
        item,
        [
            "price",
            "price_numeric",
            "total_item_price",
        ]
    )

    if isinstance(price, dict):
        price = find_first_value(
            price,
            ["amount", "value"]
        )

    price = parse_price(price)

    url = find_first_value(
        item,
        [
            "url",
            "item_url",
        ]
    )

    if not url:
        url = f"{BASE_URL}/items/{item_id}"

    photo = None

    photo_data = find_first_value(
        item,
        [
            "photo",
            "photos",
        ]
    )

    if isinstance(photo_data, dict):
        photo = find_first_value(
            photo_data,
            [
                "url",
                "full_size_url",
                "high_resolution_url",
                "thumbnails",
            ]
        )

    elif isinstance(photo_data, list) and photo_data:
        first = photo_data[0]

        if isinstance(first, dict):
            photo = find_first_value(
                first,
                [
                    "url",
                    "full_size_url",
                    "high_resolution_url",
                ]
            )
        elif isinstance(first, str):
            photo = first

    elif isinstance(photo_data, str):
        photo = photo_data

    if isinstance(photo, list):
        photo = photo[0] if photo else None

    # Other common photo fields.
    if not photo:
        photo = find_first_value(
            item,
            [
                "photo_url",
                "image_url",
                "thumbnail",
            ]
        )

    seller = (
        item.get("user")
        or item.get("seller")
        or {}
    )

    if not isinstance(seller, dict):
        seller = {}

    seller_name = find_first_value(
        seller,
        [
            "login",
            "username",
            "name",
        ]
    )

    seller_id = find_first_value(
        seller,
        ["id", "user_id"]
    )

    rating = find_first_value(
        seller,
        [
            "feedback_reputation",
            "rating",
            "rating_value",
        ]
    )

    if isinstance(rating, dict):
        rating = find_first_value(
            rating,
            [
                "value",
                "rating",
            ]
        )

    item.update({
        "_id": str(item_id),
        "_title": str(title),
        "_price": price,
        "_url": url,
        "_photo": photo,
        "_seller_name": seller_name,
        "_seller_id": seller_id,
        "_rating": rating,
    })

    return item


def html_catalog_search(search):
    """
    Public catalogue fallback.

    This does NOT depend on /api/v2/catalog/items.
    """

    search_text = get_search_value(
        search,
        "search_text",
        "query",
        "search",
        "name",
        default=""
    )

    params = {
        "search_text": search_text,
        "order": "newest_first",
        "page": 1,
    }

    price_to = get_search_value(
        search,
        "price_to",
        "max_price",
        "maxPrice"
    )

    if price_to is not None and str(price_to) != "":
        try:
            params["price_to"] = int(float(price_to))
        except Exception:
            pass

    status_ids = get_search_value(
        search,
        "status_ids",
        "statuses",
        default=[1, 2, 3, 4]
    )

    if isinstance(status_ids, list):
        params["status_ids[]"] = status_ids

    url = (
        f"{BASE_URL}/catalog?"
        + urlencode(params, doseq=True)
    )

    try:
        r = session.get(
            url,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Referer": BASE_URL + "/",
            },
            timeout=25,
        )

        print(
            f" [Vinted] Catalogue fallback: HTTP {r.status_code}"
        )

        if r.status_code != 200:
            return []

        text = r.text

        items = []

        # ----------------------------------------------------
        # Method 1: Next.js JSON
        # ----------------------------------------------------

        next_data = extract_next_data(text)

        if next_data:
            items.extend(
                recursively_find_items(next_data)
            )

        # ----------------------------------------------------
        # Method 2: Search for JSON blobs containing IDs
        # ----------------------------------------------------

        if not items:

            patterns = [
                r'"id"\s*:\s*(\d+).*?"title"\s*:\s*"([^"]+)"',
                r'"title"\s*:\s*"([^"]+)".*?"id"\s*:\s*(\d+)',
            ]

            for pattern in patterns:
                for match in re.finditer(
                    pattern,
                    text,
                    re.S
                ):
                    groups = match.groups()

                    if len(groups) != 2:
                        continue

                    if groups[0].isdigit():
                        item_id = groups[0]
                        title = groups[1]
                    else:
                        title = groups[0]
                        item_id = groups[1]

                    items.append({
                        "id": item_id,
                        "title": html.unescape(title),
                    })

        # ----------------------------------------------------
        # Method 3: HTML item links
        # ----------------------------------------------------

        if not items:

            soup = BeautifulSoup(
                text,
                "html.parser"
            )

            seen = set()

            for a in soup.find_all(
                "a",
                href=True
            ):

                href = a.get("href", "")

                match = re.search(
                    r"/items/(\d+)",
                    href
                )

                if not match:
                    continue

                item_id = match.group(1)

                if item_id in seen:
                    continue

                seen.add(item_id)

                title = (
                    a.get_text(
                        " ",
                        strip=True
                    )
                    or "Vinted listing"
                )

                img = a.find("img")

                photo = None

                if img:
                    photo = (
                        img.get("src")
                        or img.get("data-src")
                    )

                items.append({
                    "id": item_id,
                    "title": title,
                    "url": (
                        href
                        if href.startswith("http")
                        else BASE_URL + href
                    ),
                    "photo_url": photo,
                })

        # Remove duplicates.
        unique = {}
        for item in items:
            if not isinstance(item, dict):
                continue

            item_id = item.get("id")

            if item_id is not None:
                unique[str(item_id)] = item

        return [
            normalise_item(item)
            for item in unique.values()
            if normalise_item(item)
        ]

    except requests.RequestException as e:
        print(
            f" [!] Catalogue request failed: {e}"
        )
        return []


# ============================================================
# UNIFIED SEARCH
# ============================================================

def fetch_listings(search):
    """
    API first, catalogue fallback second.
    """

    data = api_search(search)

    if data is not None:

        if isinstance(data, dict):
            raw_items = (
                data.get("items")
                or data.get("catalog_items")
                or data.get("data")
                or []
            )
        elif isinstance(data, list):
            raw_items = data
        else:
            raw_items = []

        if isinstance(raw_items, dict):
            raw_items = (
                raw_items.get("items")
                or []
            )

        listings = []

        for item in raw_items:

            if not isinstance(item, dict):
                continue

            normalised = normalise_item(item)

            if normalised:
                listings.append(normalised)

        if listings:
            return listings

    # API failed / blocked.
    print(" [Vinted] Falling back to catalogue HTML...")

    bootstrap_session()

    return html_catalog_search(search)


# ============================================================
# BLOCK WORD FILTERING
# ============================================================

def listing_text(item):
    parts = []

    for key in [
        "_title",
        "title",
        "name",
        "description",
    ]:

        value = item.get(key)

        if value:
            parts.append(str(value))

    return " ".join(parts).lower()


def is_blocked(item, search):
    text = listing_text(item)

    block_words = normalise_words(
        get_search_value(
            search,
            "exclude_words",
            "block_words",
            "blocked_words",
            "exclude",
            "excludeWords",
            default=[]
        )
    )

    for word in block_words:
        if word and word in text:
            return True

    return False


# ============================================================
# PRICE FILTER
# ============================================================

def passes_price(item, search):

    price = item.get("_price")

    max_price = get_search_value(
        search,
        "price_to",
        "max_price",
        "maxPrice"
    )

    min_price = get_search_value(
        search,
        "price_from",
        "min_price",
        "minPrice"
    )

    if price is None:
        # If we don't know the price, don't reject it here.
        return True

    try:
        price = float(price)
    except Exception:
        return True

    if max_price is not None:
        try:
            if price > float(max_price):
                return False
        except Exception:
            pass

    if min_price is not None:
        try:
            if price < float(min_price):
                return False
        except Exception:
            pass

    return True


# ============================================================
# SEARCH MATCHING
# ============================================================

def passes_search(item, search):

    search_text = get_search_value(
        search,
        "search_text",
        "query",
        "search",
        "name",
        default=""
    )

    if not search_text:
        return True

    words = [
        x.strip().lower()
        for x in re.split(
            r"\s+",
            str(search_text)
        )
        if x.strip()
    ]

    title = listing_text(item)

    # All search words must appear.
    for word in words:
        if word not in title:
            return False

    return True


# ============================================================
# USER PROFILE
# ============================================================

def fetch_user_profile(user_id):

    if not user_id:
        return {}

    try:
        r = session.get(
            f"{BASE_URL}/api/v2/users/{user_id}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": BASE_URL + "/",
            },
            timeout=15,
        )

        if r.status_code != 200:
            return {}

        data = r.json()

        if isinstance(data, dict):
            return (
                data.get("user")
                or data
            )

    except Exception:
        pass

    return {}


# ============================================================
# DISCORD
# ============================================================

def discord_timestamp(value=None):

    if value:
        try:
            if isinstance(value, (int, float)):
                return int(value)

            parsed = datetime.fromisoformat(
                str(value).replace(
                    "Z",
                    "+00:00"
                )
            )

            return int(
                parsed.timestamp()
            )

        except Exception:
            pass

    return int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )


def build_discord_payload(item, search):

    title = (
        item.get("_title")
        or item.get("title")
        or "New Vinted listing"
    )

    url = (
        item.get("_url")
        or f"{BASE_URL}/items/{item.get('_id')}"
    )

    price = item.get("_price")

    if price is not None:
        price_text = f"£{float(price):.2f}"
    else:
        price_text = "Unknown"

    seller = (
        item.get("_seller_name")
        or "Unknown seller"
    )

    rating = item.get("_rating")

    if rating is None:
        rating_text = "Unknown"
    else:
        rating_text = str(rating)

    embed = {
        "title": title[:256],
        "url": url,
        "fields": [
            {
                "name": "💷 Price",
                "value": price_text,
                "inline": True,
            },
            {
                "name": "👤 Seller",
                "value": str(seller)[:1024],
                "inline": True,
            },
            {
                "name": "⭐ Rating",
                "value": rating_text[:1024],
                "inline": True,
            },
        ],
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    photo = item.get("_photo")

    if photo:
        embed["image"] = {
            "url": photo
        }

    payload = {
        "embeds": [embed],
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 5,
                        "label": "View Listing",
                        "url": url,
                    }
                ],
            }
        ],
    }

    return payload


def send_to_discord(item, search):

    if not DISCORD_WEBHOOK_URL:
        print(
            " [!] DISCORD_WEBHOOK_URL is not configured."
        )
        return False

    payload = build_discord_payload(
        item,
        search
    )

    try:

        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=20,
        )

        if 200 <= r.status_code < 300:
            return True

        print(
            f" [!] Discord returned HTTP {r.status_code}: "
            f"{r.text[:500]}"
        )

    except requests.RequestException as e:
        print(
            f" [!] Discord request failed: {e}"
        )

    return False


# ============================================================
# MAIN CHECK
# ============================================================

def check_all_searches(seen):

    searches = load_searches()

    if not searches:
        print(" [!] No searches configured.")
        return

    for search in searches:

        try:

            listings = fetch_listings(search)

            if not listings:
                continue

            for item in listings:

                item_id = item.get("_id")

                if not item_id:
                    continue

                if item_id in seen:
                    continue

                # IMPORTANT:
                # These filters are applied AFTER retrieval so
                # the fallback cannot bypass them.
                if is_blocked(item, search):
                    seen.add(item_id)
                    continue

                if not passes_search(item, search):
                    seen.add(item_id)
                    continue

                if not passes_price(item, search):
                    seen.add(item_id)
                    continue

                print(
                    f" [NEW] {item.get('_title')} "
                    f"({item.get('_price')})"
                )

                if send_to_discord(
                    item,
                    search
                ):
                    seen.add(item_id)

        except Exception as e:

            print(
                f" [!] Search error: {e}"
            )


# ============================================================
# RUNNER
# ============================================================

def main():

    print("==========================================")
    print("       Vinted Discord Alert Bot")
    print("==========================================")

    if not bootstrap_session():
        print(
            " [!] Initial Vinted session bootstrap failed."
        )

    seen = load_seen_ids()

    print(
        f" [Vinted] Loaded {len(seen)} seen IDs."
    )

    start = time.time()
    check_number = 0

    while (
        time.time() - start
        < RUN_TIME
    ):

        check_number += 1

        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] "
            f"Check #{check_number}..."
        )

        check_all_searches(seen)

        save_seen_ids(seen)

        elapsed = time.time() - start

        if elapsed + CHECK_INTERVAL >= RUN_TIME:
            break

        time.sleep(CHECK_INTERVAL)

    save_seen_ids(seen)

    print("\nFinished.")


if __name__ == "__main__":
    main()