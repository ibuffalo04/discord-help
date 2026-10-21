"""
Vinted → Discord Alert Bot
==========================
- Reads searches from vinted_searches.json (managed via dashboard)
- Uses Discord Bot Token for real working buttons
- Runs continuously for 55 minutes per GitHub Actions run
- Checks Vinted every 5 seconds
- Supports exclude words, multiple keywords, all condition types
- If Cloudflare explicitly challenges the runner, ends the run immediately
- Generic non-Cloudflare 403s still use a short fallback cooldown
- Prints useful diagnostics when Vinted returns HTTP 403
- Passively logs runner IP/country/ASN/provider and survival time

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
DISCORD_MENTION_USER_ID = "477458466257108993"

# ──────────────────────────────────────────────
# XBOX CONTROLLER HARD EXCLUSIONS
# ──────────────────────────────────────────────
# These are always added to the Xbox Controller search, even when searches
# are loaded from vinted_searches.json. This lets us keep obvious Xbox merch
# out of Discord without needing the old dashboard. Keep this list targeted
# so cheap faulty / repairable genuine controllers are not filtered out.
XBOX_ALWAYS_EXCLUDE_WORDS = [
    # Clothing / Xbox merchandise.
    "t-shirt",
    "t shirt",
    "tshirt",
    "shirt",
    "hoodie",
    "hooded blanket",
    "blanket",
    "jumper",
    "sweatshirt",
    "jersey",
    "jacket",
    "coat",
    "gilet",
    "onesie",
    "pyjamas",
    "pajamas",
    "slippers",
    "socks",
    "beanie",
    "baseball cap",
    "vest",
    "shorts",
    "trousers",
    "pants",

    # Controller-adjacent accessories we do not source.
    "chat pad",
    "chatpad",
    "controller keyboard",
    "keyboard attachment",

    # Other obvious Xbox-themed merchandise.
    "chocolate mould",
    "chocolate mold",
    "mould",
    "mold",
    "mug",
    "keyring",
    "key ring",
    "keychain",
    "poster",
    "wall art",
    "coaster",
    "plush",
    "plushie",
    "figurine",
    "figure",
    "toy",
]

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
        "exclude_words": XBOX_ALWAYS_EXCLUDE_WORDS.copy(),
        "enabled": True,
    },
]

CHECK_INTERVAL = 5

# One Python process runs for the full 55-minute GitHub job.
RUN_DURATION = 3300

# Fallback handling for a generic 403 that is NOT
# explicitly identified as a Cloudflare challenge.
FIRST_403_COOLDOWN = 120
SECOND_403_COOLDOWN = 300
MAX_GENERIC_403_STREAK = 3

VINTED_DOMAIN = "www.vinted.co.uk"
CURRENCY_SYMBOL = "£"

# Shared Vinted state.
VINTED_COOLDOWN_UNTIL = 0
VINTED_403_STREAK = 0
STOP_SCANNER = False

# Passive runner telemetry state.
RUN_STARTED_AT = 0.0
ACTIVE_RUN_RECORD_ID = None
ACTIVE_RUN_FINISHED = False
CURRENT_CHECK_COUNT = 0

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
RUNNER_STATS_FILE = "vinted_runner_stats.json"

# Keep enough history to spot patterns without letting
# the repository file grow forever.
MAX_RUNNER_HISTORY = 250

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

        # Add the built-in Xbox merchandise exclusions on top of whatever
        # is already configured in vinted_searches.json. Existing filters
        # stay intact and duplicates are removed case-insensitively.
        for search in enabled:
            if (
                str(search.get("label", "")).strip().lower()
                == "xbox controller"
            ):
                existing = search.get("exclude_words", []) or []
                merged = []
                seen_exclusions = set()

                for word in existing + XBOX_ALWAYS_EXCLUDE_WORDS:
                    clean_word = str(word).strip()
                    key = clean_word.lower()

                    if clean_word and key not in seen_exclusions:
                        merged.append(clean_word)
                        seen_exclusions.add(key)

                search["exclude_words"] = merged

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


# ── Passive runner telemetry ───────────────────
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_runner_stats() -> dict:
    if not os.path.exists(RUNNER_STATS_FILE):
        return {"runs": []}

    try:
        with open(RUNNER_STATS_FILE) as f:
            data = json.load(f)

        # Backward-compatible if the file ever contains
        # just a bare list of runs.
        if isinstance(data, list):
            return {"runs": data}

        if (
            isinstance(data, dict)
            and isinstance(data.get("runs"), list)
        ):
            return data

    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
    ):
        pass

    return {"runs": []}


def save_runner_stats(stats: dict):
    runs = stats.get("runs", [])

    if len(runs) > MAX_RUNNER_HISTORY:
        stats["runs"] = runs[-MAX_RUNNER_HISTORY:]

    with open(RUNNER_STATS_FILE, "w") as f:
        json.dump(
            stats,
            f,
            indent=2,
            ensure_ascii=False,
        )


def get_runner_network_info() -> dict:
    """
    Passive diagnostics only.

    Try several independent IP-information services so a
    temporary block/rate-limit on one provider does not leave
    the runner telemetry full of "Unknown" values.

    These requests do NOT contact Vinted and failure here
    never stops or changes the scanner.
    """
    info = {
        "ip": "Unknown",
        "country": "Unknown",
        "country_code": "Unknown",
        "region": "Unknown",
        "city": "Unknown",
        "asn": "Unknown",
        "network_org": "Unknown",
        "lookup_source": "None",
    }

    # Provider 1: ipapi.co
    try:
        r = requests.get(
            "https://ipapi.co/json/",
            headers={
                "User-Agent": "vinted-runner-telemetry/1.0"
            },
            timeout=8,
        )

        print(
            f" [Runner] ipapi.co lookup -> "
            f"HTTP {r.status_code}"
        )

        if r.status_code == 200:
            data = r.json()

            if not data.get("error"):
                info["ip"] = data.get("ip") or info["ip"]
                info["country"] = (
                    data.get("country_name")
                    or info["country"]
                )
                info["country_code"] = (
                    data.get("country_code")
                    or data.get("country")
                    or info["country_code"]
                )
                info["region"] = (
                    data.get("region")
                    or info["region"]
                )
                info["city"] = (
                    data.get("city")
                    or info["city"]
                )
                info["asn"] = (
                    data.get("asn")
                    or info["asn"]
                )
                info["network_org"] = (
                    data.get("org")
                    or info["network_org"]
                )
                info["lookup_source"] = "ipapi.co"

                if (
                    info["ip"] != "Unknown"
                    and info["country"] != "Unknown"
                    and info["asn"] != "Unknown"
                ):
                    return info

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as e:
        print(
            f" [Runner] ipapi.co lookup unavailable: {e}"
        )

    # Provider 2: IPinfo legacy public API
    try:
        r = requests.get(
            "https://ipinfo.io/json",
            headers={
                "User-Agent": "vinted-runner-telemetry/1.0"
            },
            timeout=8,
        )

        print(
            f" [Runner] ipinfo.io lookup -> "
            f"HTTP {r.status_code}"
        )

        if r.status_code == 200:
            data = r.json()

            info["ip"] = data.get("ip") or info["ip"]
            info["country_code"] = (
                data.get("country")
                or info["country_code"]
            )
            info["region"] = (
                data.get("region")
                or info["region"]
            )
            info["city"] = (
                data.get("city")
                or info["city"]
            )

            org = data.get("org") or ""

            if org:
                match = re.match(
                    r"^(AS\d+)\s*(.*)$",
                    org.strip(),
                    flags=re.IGNORECASE,
                )

                if match:
                    info["asn"] = match.group(1).upper()

                    if match.group(2).strip():
                        info["network_org"] = (
                            match.group(2).strip()
                        )
                else:
                    info["network_org"] = org.strip()

            if info["lookup_source"] == "None":
                info["lookup_source"] = "ipinfo.io"
            else:
                info["lookup_source"] += "+ipinfo.io"

            if (
                info["ip"] != "Unknown"
                and info["country"] != "Unknown"
                and info["asn"] != "Unknown"
            ):
                return info

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as e:
        print(
            f" [Runner] ipinfo.io lookup unavailable: {e}"
        )

    # Provider 3: IPWho self lookup
    try:
        r = requests.get(
            "https://api.ipwho.org/me",
            headers={
                "User-Agent": "vinted-runner-telemetry/1.0"
            },
            timeout=8,
        )

        print(
            f" [Runner] IPWho lookup -> "
            f"HTTP {r.status_code}"
        )

        if r.status_code == 200:
            payload = r.json()

            if payload.get("success") is not False:
                data = payload.get("data", payload)
                connection = data.get("connection") or {}

                asn_number = (
                    connection.get("number")
                    or connection.get("asnNumber")
                    or connection.get("asn_number")
                )

                if asn_number is not None:
                    asn_value = str(asn_number)

                    if not asn_value.upper().startswith("AS"):
                        asn_value = f"AS{asn_value}"

                    info["asn"] = asn_value

                info["ip"] = (
                    data.get("ip")
                    or info["ip"]
                )
                info["country"] = (
                    data.get("country")
                    or info["country"]
                )
                info["country_code"] = (
                    data.get("countryCode")
                    or info["country_code"]
                )
                info["region"] = (
                    data.get("region")
                    or info["region"]
                )
                info["city"] = (
                    data.get("city")
                    or info["city"]
                )
                info["network_org"] = (
                    connection.get("org")
                    or connection.get("asnOrg")
                    or connection.get("asn_org")
                    or info["network_org"]
                )

                if info["lookup_source"] == "None":
                    info["lookup_source"] = "ipwho.org"
                else:
                    info["lookup_source"] += "+ipwho.org"

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as e:
        print(
            f" [Runner] IPWho lookup unavailable: {e}"
        )

    return info


def start_runner_telemetry():
    global RUN_STARTED_AT
    global ACTIVE_RUN_RECORD_ID
    global ACTIVE_RUN_FINISHED

    RUN_STARTED_AT = time.time()
    ACTIVE_RUN_FINISHED = False

    run_id = os.environ.get(
        "GITHUB_RUN_ID",
        "local",
    )

    run_attempt = os.environ.get(
        "GITHUB_RUN_ATTEMPT",
        "1",
    )

    ACTIVE_RUN_RECORD_ID = (
        f"{run_id}-"
        f"{run_attempt}-"
        f"{int(RUN_STARTED_AT)}"
    )

    network = get_runner_network_info()

    record = {
        "record_id": ACTIVE_RUN_RECORD_ID,
        "github_run_id": run_id,
        "github_run_attempt": run_attempt,
        "github_event_name": os.environ.get(
            "GITHUB_EVENT_NAME",
            "local",
        ),
        "started_at": utc_now_iso(),
        "ended_at": None,
        "outcome": "running",
        "survival_seconds": None,
        "survival_minutes": None,
        "checks": 0,
        "cf_ray": None,
        "cf_edge": None,
        **network,
    }

    stats = load_runner_stats()
    stats.setdefault("runs", []).append(record)
    save_runner_stats(stats)

    print(
        f" [Runner] Public IP: {network['ip']}"
    )

    print(
        f" [Runner] Location: "
        f"{network['country']} "
        f"({network['country_code']})"
        f" | {network['region']}"
        f" | {network['city']}"
    )

    print(
        f" [Runner] Network: "
        f"{network['asn']} "
        f"| {network['network_org']}"
    )

    print(
        f" [Runner] Lookup source: "
        f"{network.get('lookup_source', 'Unknown')}"
    )

    print(
        " [Runner] Telemetry mode: passive only "
        "(no IP/country/ASN blocking)."
    )

    print()


def finish_runner_telemetry(
    outcome: str,
    cf_ray: str = None,
):
    global ACTIVE_RUN_FINISHED

    if (
        ACTIVE_RUN_FINISHED
        or not ACTIVE_RUN_RECORD_ID
    ):
        return

    elapsed = max(
        0,
        int(time.time() - RUN_STARTED_AT),
    )

    stats = load_runner_stats()
    runs = stats.setdefault("runs", [])

    record = None

    for candidate in reversed(runs):
        if (
            candidate.get("record_id")
            == ACTIVE_RUN_RECORD_ID
        ):
            record = candidate
            break

    if record is None:
        # Extremely defensive fallback if the telemetry file
        # was unexpectedly replaced during the run.
        record = {
            "record_id": ACTIVE_RUN_RECORD_ID,
            "github_run_id": os.environ.get(
                "GITHUB_RUN_ID",
                "local",
            ),
            "github_run_attempt": os.environ.get(
                "GITHUB_RUN_ATTEMPT",
                "1",
            ),
            "github_event_name": os.environ.get(
                "GITHUB_EVENT_NAME",
                "local",
            ),
            "started_at": None,
            "ip": "Unknown",
            "country": "Unknown",
            "country_code": "Unknown",
            "region": "Unknown",
            "city": "Unknown",
            "asn": "Unknown",
            "network_org": "Unknown",
        }

        runs.append(record)

    record["ended_at"] = utc_now_iso()
    record["outcome"] = outcome
    record["survival_seconds"] = elapsed
    record["survival_minutes"] = round(
        elapsed / 60,
        2,
    )
    record["checks"] = CURRENT_CHECK_COUNT

    if cf_ray:
        record["cf_ray"] = cf_ray

        if "-" in cf_ray:
            record["cf_edge"] = (
                cf_ray.rsplit("-", 1)[-1]
            )

    save_runner_stats(stats)
    ACTIVE_RUN_FINISHED = True

    minutes, seconds = divmod(
        elapsed,
        60,
    )

    print(
        f" [Runner] Result: {outcome}"
        f" | survived {minutes}m {seconds}s"
        f" | checks: {CURRENT_CHECK_COUNT}"
    )


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
            # followed by extra Vinted metadata.
            if testid.endswith("--overlay-link"):
                link_title = attrs.get("title", "")

                if link_title:
                    link_title = html.unescape(
                        link_title
                    ).strip()

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

    # Only used for a generic non-Cloudflare 403.
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

    # Generic cooldown expired.
    # Keep the existing session and retry the catalogue.
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

            cf_mitigated = (
                r.headers
                .get("CF-Mitigated", "")
                .strip()
                .lower()
            )

            # Cloudflare explicitly says this response is
            # a challenge. Do not keep retrying this runner.
            if cf_mitigated == "challenge":
                STOP_SCANNER = True

                print(
                    " [!] Cloudflare challenge confirmed."
                )

                finish_runner_telemetry(
                    "cloudflare_challenge",
                    cf_ray=r.headers.get("CF-Ray"),
                )

                print(
                    " [!] Ending this scanner immediately "
                    "instead of retrying the challenged runner."
                )

                return []

            # If it is a generic 403 rather than a confirmed
            # Cloudflare challenge, retain the fallback
            # cooldown behaviour.
            VINTED_403_STREAK += 1

            if VINTED_403_STREAK == 1:
                cooldown_seconds = FIRST_403_COOLDOWN

                VINTED_COOLDOWN_UNTIL = (
                    time.time()
                    + cooldown_seconds
                )

                print(
                    f" [!] Vinted returned a generic "
                    f"HTTP 403. Cooling down for "
                    f"{cooldown_seconds}s."
                )

                return []

            if VINTED_403_STREAK == 2:
                cooldown_seconds = SECOND_403_COOLDOWN

                VINTED_COOLDOWN_UNTIL = (
                    time.time()
                    + cooldown_seconds
                )

                print(
                    f" [!] Vinted returned another generic "
                    f"HTTP 403. Cooling down for "
                    f"{cooldown_seconds}s."
                )

                return []

            if (
                VINTED_403_STREAK
                >= MAX_GENERIC_403_STREAK
            ):
                STOP_SCANNER = True

                finish_runner_telemetry(
                    "generic_403_stop"
                )

                print(
                    " [!] Third consecutive generic "
                    "Vinted HTTP 403."
                )

                print(
                    " [!] Ending this scanner early."
                )

                return []

        if r.status_code != 200:
            print(
                f" [!] Vinted catalogue returned HTTP "
                f"{r.status_code}."
            )

            return []

        # Any successful catalogue response clears
        # generic 403 fallback state.
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


def _normalise_vinted_page_source(page_html: str) -> str:
    """
    Vinted's public item page currently carries item data inside
    Next.js hydration scripts. Some JSON is escaped inside JS strings,
    so build a text form that is easier to search without needing a
    logged-in Vinted API request.
    """
    text = html.unescape(page_html or "")

    # Unescape the common JSON-in-JavaScript quoting used by
    # self.__next_f.push(...) hydration chunks.
    text = text.replace('\\"', '"')
    text = text.replace('\\u0026', '&')
    text = text.replace('\\u003c', '<')
    text = text.replace('\\u003e', '>')

    return text


def _extract_jsonish_string(text: str, keys: list, start: int, end: int):
    window = text[start:end]

    for key in keys:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"',
            window,
            flags=re.IGNORECASE,
        )

        if match:
            value = match.group(1)
            value = value.replace('\\/', '/')
            value = value.replace('\\"', '"')
            return value

    return None


def _extract_jsonish_number(text: str, keys: list, start: int, end: int):
    window = text[start:end]

    for key in keys:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*(null|-?[0-9]+(?:\.[0-9]+)?)',
            window,
            flags=re.IGNORECASE,
        )

        if match and match.group(1).lower() != 'null':
            raw = match.group(1)

            try:
                if '.' in raw:
                    return float(raw)

                return int(raw)

            except ValueError:
                pass

    return None


def parse_public_item_seller(page_html: str, item_id) -> dict:
    """
    Extract the seller attached to the requested item from Vinted's
    public item-page hydration data.

    This deliberately supports both snake_case and camelCase field
    names because Vinted has used both shapes in frontend payloads.
    """
    text = _normalise_vinted_page_source(page_html)

    if not text:
        return {}

    item_id = str(item_id or '')

    # Find the requested item's ID in the hydration payload. There can
    # also be IDs for recommendations/member items, so use the requested
    # item ID as the anchor and then choose the nearest seller reputation
    # block rather than blindly taking the first seller on the page.
    id_positions = []

    if item_id:
        id_patterns = [
            rf'"id"\s*:\s*"{re.escape(item_id)}"',
            rf'"id"\s*:\s*{re.escape(item_id)}(?:\D|$)',
            rf'/items/{re.escape(item_id)}(?:-|"|/)',
        ]

        for pattern in id_patterns:
            id_positions.extend(
                match.start()
                for match in re.finditer(
                    pattern,
                    text,
                    flags=re.IGNORECASE,
                )
            )

    reputation_positions = [
        match.start()
        for match in re.finditer(
            r'"(?:feedback_reputation|feedbackReputation)"\s*:',
            text,
            flags=re.IGNORECASE,
        )
    ]

    if not reputation_positions:
        return {}

    if id_positions:
        rep_pos = min(
            reputation_positions,
            key=lambda pos: min(
                abs(pos - item_pos)
                for item_pos in id_positions
            ),
        )
    else:
        rep_pos = reputation_positions[0]

    # Seller fields are normally clustered tightly together. The fairly
    # generous window also copes with small frontend schema changes.
    start = max(0, rep_pos - 5000)
    end = min(len(text), rep_pos + 5000)

    login = _extract_jsonish_string(
        text,
        ['login', 'username'],
        start,
        end,
    )

    reputation = _extract_jsonish_number(
        text,
        ['feedback_reputation', 'feedbackReputation'],
        start,
        end,
    )

    feedback_count = _extract_jsonish_number(
        text,
        [
            'feedback_count',
            'feedbackCount',
            'positive_feedback_count',
            'positiveFeedbackCount',
        ],
        start,
        end,
    )

    # Prefer an ID close to the login/reputation block. Seller IDs can be
    # strings or numbers depending on which frontend payload produced it.
    seller_id = None
    block = text[start:end]

    id_matches = list(
        re.finditer(
            r'"id"\s*:\s*"?(\d+)"?',
            block,
            flags=re.IGNORECASE,
        )
    )

    if id_matches:
        local_rep = rep_pos - start
        nearest_id = min(
            id_matches,
            key=lambda match: abs(match.start() - local_rep),
        )
        seller_id = nearest_id.group(1)

    # If the nearest generic ID happened to be the item ID, try a member
    # URL near the seller block, which directly exposes the seller ID.
    if seller_id == item_id or not seller_id:
        member_match = re.search(
            r'/member/(\d+)(?:-[^"\\/?]+)?',
            block,
            flags=re.IGNORECASE,
        )

        if member_match:
            seller_id = member_match.group(1)

    seller = {}

    if seller_id:
        seller['id'] = seller_id

    if login:
        seller['login'] = login

    if reputation is not None:
        seller['feedback_reputation'] = reputation

    if feedback_count is not None:
        seller['feedback_count'] = int(feedback_count)

    return seller


def fetch_public_item_seller(item: dict) -> dict:
    """
    Fetch the public item page only when a genuinely new listing is about
    to be sent to Discord. This means seller enrichment adds no extra
    traffic to the normal 5-second catalogue polling loop.
    """
    item_url = get_item_url(item)

    if not item_url:
        return {}

    try:
        r = SESSION.get(
            item_url,
            headers=VINTED_HEADERS,
            timeout=15,
        )

        if r.status_code != 200:
            print(
                f" [Seller] Item page returned HTTP "
                f"{r.status_code}; sending alert without seller details."
            )
            return {}

        seller = parse_public_item_seller(
            r.text,
            item.get('id'),
        )

        if seller.get('login'):
            print(
                f" [Seller] Found {seller['login']}"
                f" | feedback: {seller.get('feedback_count', 'Unknown')}"
            )
        else:
            print(
                " [Seller] Public item page loaded but seller "
                "details could not be extracted."
            )

        return seller

    except requests.RequestException as e:
        print(
            f" [Seller] Item-page request failed: {e}; "
            f"sending alert without seller details."
        )
        return {}


def fetch_item_details(item_id) -> dict:
    """Fetch full item details to get rating, date etc."""
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


def is_xbox_controller_search(search: dict) -> bool:
    """Return True for the dedicated Xbox-controller search."""
    label = " ".join(
        str(search.get("label", ""))
        .lower()
        .split()
    )

    search_text = " ".join(
        str(search.get("search_text", ""))
        .lower()
        .split()
    )

    return (
        label in {
            "xbox controller",
            "xbox controllers",
        }
        or "xbox controller" in search_text
        or "xbox controllers" in search_text
    )


def is_xbox_controller_candidate(item: dict) -> bool:
    """
    Vinted sometimes mixes unrelated recommendation cards into the catalogue
    HTML. Only allow cards that actually look like Xbox-controller listings.

    Keep this deliberately permissive so short genuine titles such as
    "Xbox One", "Controller", "Elite Series 2" or "1708" can still pass
    when Vinted supplies Xbox/Microsoft as the brand.
    """
    title = " ".join(
        str(item.get("title", ""))
        .lower()
        .split()
    )

    brand = " ".join(
        str(item.get("brand_title", ""))
        .lower()
        .split()
    )

    if not title:
        return False

    # Any title that explicitly says Xbox is potentially relevant. The hard
    # exclusion list removes clothing, merch and controller accessories first.
    if "xbox" in title:
        return True

    recognised_brand = (
        "xbox" in brand
        or "microsoft" in brand
    )

    controller_terms = (
        "controller",
        "gamepad",
        "game pad",
        "joypad",
        "joy pad",
        "control pad",
    )

    if (
        recognised_brand
        and any(term in title for term in controller_terms)
    ):
        return True

    # Common controller-specific titles that may omit the word Xbox.
    if recognised_brand and (
        "elite series 2" in title
        or "elite 2" in title
        or re.search(r"\b(?:1708|1914)\b", title)
    ):
        return True

    return False


# ── Discord helpers ────────────────────────────
def time_ago(value) -> str:
    if not value:
        return "Unknown"

    ts = None

    try:
        ts = int(
            float(str(value))
        )

    except (TypeError, ValueError):
        pass

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
    Vinted feedback_reputation is normally a 0.0-1.0 ratio.
    Show both a star visual and the more useful numeric 5-point score.
    """
    if reputation is None:
        return "No ratings"

    try:
        score = float(
            reputation
        )

        # Be tolerant if Vinted ever supplies a direct 0-5 value.
        score_5 = (
            score * 5
            if score <= 1.0
            else score
        )

        score_5 = max(
            0.0,
            min(5.0, score_5)
        )

        stars = int(
            round(score_5)
        )

        return (
            "⭐" * stars
            + "✩" * (5 - stars)
            + f" {score_5:.1f}/5"
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

    # Catalogue HTML does not currently include the seller object.
    # Enrich only genuinely new listings by opening their public item
    # page and reading the seller data from Vinted's Next.js hydration
    # payload. This is one extra request per alert, not per scan.
    if not item.get("user", {}).get("login"):
        public_seller = fetch_public_item_seller(
            item
        )

        if public_seller:
            item["user"] = {
                **item.get("user", {}),
                **public_seller
            }

    user_id = (
        item
        .get("user", {})
        .get("id")
    )

    # Only touch the older user endpoint if the public item page did not
    # give us enough seller data. In the normal case this keeps enrichment
    # to ONE extra Vinted request per new Discord alert.
    seller_user = item.get("user", {})
    needs_profile_fallback = (
        bool(user_id)
        and (
            not seller_user.get("login")
            or seller_user.get("feedback_reputation") is None
            or seller_user.get("feedback_count") is None
        )
    )

    if needs_profile_fallback:
        user_profile = fetch_user_profile(
            user_id
        )

        if user_profile:
            item["user"] = {
                **item.get("user", {}),
                **user_profile
            }

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

    brand = (
        item.get("brand_title")
        or "—"
    )

    size = (
        item.get("size_title")
        or "—"
    )

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

    # Directly mention Reece on every new-listing alert so Discord
    # treats it as a personal mention instead of only a channel message.
    if DISCORD_MENTION_USER_ID:
        payload["content"] = f"<@{DISCORD_MENTION_USER_ID}>"
        payload["allowed_mentions"] = {
            "users": [DISCORD_MENTION_USER_ID]
        }

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
    global CURRENT_CHECK_COUNT

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

    # Start passive telemetry before contacting Vinted.
    start_runner_telemetry()

    # Create one Vinted session at the beginning
    # and reuse it while this runner remains healthy.
    get_vinted_session_cookie()

    seen = load_seen()

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

            if STOP_SCANNER:
                break

        save_seen(
            seen
        )

        if not ACTIVE_RUN_FINISHED:
            finish_runner_telemetry(
                "seeded_first_run"
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
        CURRENT_CHECK_COUNT = checks

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

            # Guarantee the built-in Xbox exclusions are active even if an
            # older dashboard/config file has an unexpected label or stale
            # exclusion list. This changes filtering only, not scan timing.
            if is_xbox_controller_search(search):
                merged_exclude_words = []
                seen_exclusions = set()

                for word in (
                    list(exclude_words)
                    + XBOX_ALWAYS_EXCLUDE_WORDS
                ):
                    clean_word = str(word).strip()
                    exclusion_key = clean_word.lower()

                    if (
                        clean_word
                        and exclusion_key not in seen_exclusions
                    ):
                        merged_exclude_words.append(clean_word)
                        seen_exclusions.add(exclusion_key)

                exclude_words = merged_exclude_words

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

                if (
                    is_xbox_controller_search(search)
                    and not is_xbox_controller_candidate(item)
                ):
                    print(
                        f"\n [skip] "
                        f"'{item.get('title')}' "
                        f"does not look like an Xbox controller"
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

        # Always save state before exiting.
        save_seen(
            seen
        )

        if STOP_SCANNER:
            print(
                "Scanner stopped because Cloudflare "
                "challenged this runner."
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

    if not ACTIVE_RUN_FINISHED:
        finish_runner_telemetry(
            "completed_full_run"
        )

    print(
        " Vinted scanner finished."
    )


if __name__ == "__main__":
    run()