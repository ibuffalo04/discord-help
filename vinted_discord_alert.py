"""
Vinted → Discord Alert Bot
==========================
- Reads searches from vinted_searches.json
- Runs for up to 55 minutes per GitHub Actions run
- Checks Vinted every 5 seconds
- Exits immediately on a confirmed Cloudflare challenge
- Keeps a small fallback cooldown for generic HTTP 403 responses
- Passively records runner IP/country/ASN/provider and survival time

GitHub Secrets needed:
DISCORD_BOT_TOKEN
DISCORD_CHANNEL_ID
"""

import html
import json
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

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
    }
]

CHECK_INTERVAL = 5
RUN_DURATION = 3300

FIRST_403_COOLDOWN = 120
SECOND_403_COOLDOWN = 300
MAX_GENERIC_403_STREAK = 3

VINTED_DOMAIN = "www.vinted.co.uk"
CURRENCY_SYMBOL = "£"

STATE_FILE = "vinted_seen_ids.json"
SEARCHES_FILE = "vinted_searches.json"
RUNNER_STATS_FILE = "vinted_runner_stats.json"
MAX_RUNNER_HISTORY = 250

CONDITION_LABELS = {
    1: "New without tags",
    2: "Very good condition",
    3: "Good condition",
    4: "Satisfactory condition",
    5: "Not specified",
    6: "New with tags",
}

VINTED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

VINTED_COOLDOWN_UNTIL = 0.0
VINTED_403_STREAK = 0
STOP_SCANNER = False

RUN_STARTED_AT = 0.0
ACTIVE_RUN_RECORD_ID = None
ACTIVE_RUN_FINISHED = False
CURRENT_CHECK_COUNT = 0


def load_searches() -> list:
    if os.path.exists(SEARCHES_FILE):
        try:
            with open(SEARCHES_FILE, "r", encoding="utf-8") as f:
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

        except (OSError, json.JSONDecodeError) as e:
            print(
                f" [!] Could not read "
                f"{SEARCHES_FILE}: {e}"
            )

    print(" Using fallback searches")
    return FALLBACK_SEARCHES


def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)

        except (OSError, json.JSONDecodeError):
            pass

    return {}


def save_seen(seen: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_runner_stats() -> dict:
    if not os.path.exists(RUNNER_STATS_FILE):
        return {"runs": []}

    try:
        with open(RUNNER_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

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

    with open(RUNNER_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            stats,
            f,
            indent=2,
            ensure_ascii=False,
        )


def merge_network_info(info: dict, updates: dict):
    for key, value in updates.items():
        if value not in (None, "", "Unknown"):
            info[key] = value


def get_runner_network_info() -> dict:
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

    # Provider 1 — ipapi.co
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
                merge_network_info(
                    info,
                    {
                        "ip": data.get("ip"),
                        "country": data.get("country_name"),
                        "country_code": (
                            data.get("country_code")
                            or data.get("country")
                        ),
                        "region": data.get("region"),
                        "city": data.get("city"),
                        "asn": data.get("asn"),
                        "network_org": data.get("org"),
                        "lookup_source": "ipapi.co",
                    },
                )

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
            f" [Runner] ipapi.co lookup "
            f"unavailable: {e}"
        )

    # Provider 2 — ipwho.is
    try:
        r = requests.get(
            "https://ipwho.is/",
            headers={
                "User-Agent": "vinted-runner-telemetry/1.0"
            },
            timeout=8,
        )

        print(
            f" [Runner] ipwho.is lookup -> "
            f"HTTP {r.status_code}"
        )

        if r.status_code == 200:
            data = r.json()

            if data.get("success", True):
                connection = (
                    data.get("connection")
                    or {}
                )

                asn = connection.get("asn")

                if asn not in (None, ""):
                    asn = str(asn)

                    if not asn.upper().startswith("AS"):
                        asn = f"AS{asn}"

                merge_network_info(
                    info,
                    {
                        "ip": data.get("ip"),
                        "country": data.get("country"),
                        "country_code": data.get("country_code"),
                        "region": data.get("region"),
                        "city": data.get("city"),
                        "asn": asn,
                        "network_org": (
                            connection.get("org")
                            or connection.get("isp")
                        ),
                        "lookup_source": "ipwho.is",
                    },
                )

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
            f" [Runner] ipwho.is lookup "
            f"unavailable: {e}"
        )

    # Provider 3 — ipinfo.io
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

            org = (
                data.get("org")
                or ""
            )

            asn = None
            network_org = None

            if org:
                match = re.match(
                    r"^(AS\d+)\s*(.*)$",
                    org.strip(),
                    flags=re.IGNORECASE,
                )

                if match:
                    asn = (
                        match.group(1)
                        .upper()
                    )

                    network_org = (
                        match.group(2)
                        .strip()
                        or None
                    )

                else:
                    network_org = (
                        org.strip()
                    )

            merge_network_info(
                info,
                {
                    "ip": data.get("ip"),
                    "country_code": data.get("country"),
                    "region": data.get("region"),
                    "city": data.get("city"),
                    "asn": asn,
                    "network_org": network_org,
                    "lookup_source": "ipinfo.io",
                },
            )

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as e:
        print(
            f" [Runner] ipinfo.io lookup "
            f"unavailable: {e}"
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

    stats.setdefault(
        "runs",
        [],
    ).append(
        record
    )

    save_runner_stats(
        stats
    )

    print(
        f" [Runner] Public IP: "
        f"{network['ip']}"
    )

    print(
        f" [Runner] Location: "
        f"{network['country']} "
        f"({network['country_code']}) "
        f"| {network['region']} "
        f"| {network['city']}"
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
        " [Runner] Telemetry mode: "
        "passive only "
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
        int(
            time.time()
            - RUN_STARTED_AT
        ),
    )

    stats = load_runner_stats()

    runs = stats.setdefault(
        "runs",
        [],
    )

    record = None

    for candidate in reversed(runs):
        if (
            candidate.get("record_id")
            == ACTIVE_RUN_RECORD_ID
        ):
            record = candidate
            break

    if record is None:
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
            "lookup_source": "Unknown",
        }

        runs.append(
            record
        )

    record["ended_at"] = (
        utc_now_iso()
    )

    record["outcome"] = (
        outcome
    )

    record["survival_seconds"] = (
        elapsed
    )

    record["survival_minutes"] = (
        round(
            elapsed / 60,
            2,
        )
    )

    record["checks"] = (
        CURRENT_CHECK_COUNT
    )

    if cf_ray:
        record["cf_ray"] = (
            cf_ray
        )

        if "-" in cf_ray:
            record["cf_edge"] = (
                cf_ray
                .rsplit(
                    "-",
                    1,
                )[-1]
            )

    save_runner_stats(
        stats
    )

    ACTIVE_RUN_FINISHED = True

    minutes, seconds = divmod(
        elapsed,
        60,
    )

    print(
        f" [Runner] Result: "
        f"{outcome} "
        f"| survived "
        f"{minutes}m {seconds}s "
        f"| checks: "
        f"{CURRENT_CHECK_COUNT}"
    )


class VintedListingParser(
    HTMLParser
):
    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.items = []
        self.current = None
        self.stack = []

        self.capture = None
        self.capture_tag = None
        self.capture_text = ""

    def handle_starttag(
        self,
        tag,
        attrs,
    ):
        attrs = dict(
            attrs
        )

        testid = attrs.get(
            "data-testid",
            "",
        )

        if (
            tag == "div"
            and testid.startswith(
                "product-item-id-"
            )
        ):
            item_id = (
                testid.replace(
                    "product-item-id-",
                    "",
                    1,
                )
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

                self.stack = [
                    "card"
                ]

                return

        if self.current is None:
            return

        self.stack.append(
            tag
        )

        if tag == "a":
            href = attrs.get(
                "href",
                "",
            )

            if (
                href
                and "/items/" in href
                and not self.current["url"]
            ):
                self.current["url"] = (
                    urljoin(
                        f"https://{VINTED_DOMAIN}/",
                        href,
                    )
                )

            if testid.endswith(
                "--overlay-link"
            ):
                link_title = attrs.get(
                    "title",
                    "",
                )

                if link_title:
                    link_title = (
                        html.unescape(
                            link_title
                        )
                        .strip()
                    )

                    metadata_match = re.search(
                        r",\s*(?:Brand|Condition|Size):\s*",
                        link_title,
                        flags=re.IGNORECASE,
                    )

                    if metadata_match:
                        link_title = (
                            link_title[
                                :metadata_match.start()
                            ]
                            .strip()
                        )

                    self.current["title"] = (
                        link_title
                    )

        if tag == "img":
            src = (
                attrs.get("src")
                or attrs.get("data-src")
            )

            if (
                src
                and not self.current["image_url"]
            ):
                self.current["image_url"] = (
                    src
                )

            if not self.current["title"]:
                alt = attrs.get(
                    "alt",
                    "",
                )

                if alt:
                    self.current["title"] = (
                        html.unescape(
                            alt
                        )
                        .strip()
                    )

        if testid.endswith(
            "--description-title"
        ):
            self.capture = "brand"
            self.capture_tag = tag
            self.capture_text = ""

        elif testid.endswith(
            "--description-subtitle"
        ):
            self.capture = "subtitle"
            self.capture_tag = tag
            self.capture_text = ""

        elif testid.endswith(
            "--price-text"
        ):
            self.capture = "price"
            self.capture_tag = tag
            self.capture_text = ""

    def handle_endtag(
        self,
        tag,
    ):
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
                self.current[
                    "brand_title"
                ] = text

            elif (
                self.capture == "subtitle"
                and text
            ):
                self._parse_subtitle(
                    text
                )

            elif (
                self.capture == "price"
                and text
            ):
                self._parse_price(
                    text
                )

            self.capture = None
            self.capture_tag = None
            self.capture_text = ""

        if self.stack:
            self.stack.pop()

        if (
            tag == "div"
            and not self.stack
        ):
            self.items.append(
                self.current
            )

            self.current = None

    def handle_data(
        self,
        data,
    ):
        if (
            self.current is not None
            and self.capture
        ):
            self.capture_text += (
                data
            )

    def _parse_price(
        self,
        text,
    ):
        match = re.search(
            r"([0-9]+(?:[.,][0-9]+)?)",
            text,
        )

        if match:
            self.current["price"]["amount"] = (
                match.group(1)
                .replace(
                    ",",
                    ".",
                )
            )

    def _parse_subtitle(
        self,
        text,
    ):
        parts = [
            p.strip()
            for p in text.split("·")
            if p.strip()
        ]

        if len(parts) >= 1:
            self.current["size_title"] = (
                parts[0]
            )

        if len(parts) >= 2:
            self.current["status"] = (
                parts[1]
            )

        if len(parts) >= 3:
            self.current["status"] = (
                parts[2]
            )


def parse_vinted_catalogue(
    page_html: str,
) -> list:
    parser = VintedListingParser()

    parser.feed(
        page_html
    )

    items = []
    seen_ids = set()

    for item in parser.items:
        item_id = str(
            item.get(
                "id",
                "",
            )
        )

        if (
            not item_id
            or item_id in seen_ids
        ):
            continue

        seen_ids.add(
            item_id
        )

        if item.get(
            "image_url"
        ):
            item["photos"] = [
                {
                    "url": item["image_url"],
                    "full_size_url": item["image_url"],
                }
            ]

        items.append(
            item
        )

    return items


def get_vinted_session_cookie():
    try:
        SESSION.get(
            f"https://{VINTED_DOMAIN}/",
            headers=VINTED_HEADERS,
            timeout=10,
        )

    except requests.RequestException:
        pass


def print_403_diagnostics(
    response,
):
    print(
        f" [403 debug] Server: "
        f"{response.headers.get('Server', 'Not provided')}"
    )

    print(
        f" [403 debug] CF-Mitigated: "
        f"{response.headers.get('CF-Mitigated', 'Not provided')}"
    )

    print(
        f" [403 debug] CF-Ray: "
        f"{response.headers.get('CF-Ray', 'Not provided')}"
    )

    print(
        f" [403 debug] Content-Type: "
        f"{response.headers.get('Content-Type', 'Not provided')}"
    )

    print(
        f" [403 debug] Retry-After: "
        f"{response.headers.get('Retry-After', 'Not provided')}"
    )


def fetch_listings(
    search: dict,
) -> list:
    global VINTED_COOLDOWN_UNTIL
    global VINTED_403_STREAK
    global STOP_SCANNER

    now = time.time()

    if now < VINTED_COOLDOWN_UNTIL:
        remaining = max(
            1,
            int(
                VINTED_COOLDOWN_UNTIL
                - now
            ),
        )

        print(
            f" [!] Vinted cooldown active "
            f"({remaining}s remaining)."
        )

        return []

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
            "newest_first",
        ),
        "page": 1,
    }

    if search.get("max_price"):
        params["price_to"] = (
            search["max_price"]
        )

    if search.get("min_price"):
        params["price_from"] = (
            search["min_price"]
        )

    if search.get("size_ids"):
        params["size_ids[]"] = (
            search["size_ids"]
        )

    if search.get("brand_ids"):
        params["brand_ids[]"] = (
            search["brand_ids"]
        )

    if search.get("status_ids"):
        params["status_ids[]"] = (
            search["status_ids"]
        )

    try:
        r = SESSION.get(
            f"https://{VINTED_DOMAIN}/catalog",
            params=params,
            headers=VINTED_HEADERS,
            timeout=20,
        )

        if r.status_code == 403:
            print_403_diagnostics(
                r
            )

            cf_mitigated = (
                r.headers
                .get(
                    "CF-Mitigated",
                    "",
                )
                .strip()
                .lower()
            )

            if cf_mitigated == "challenge":
                STOP_SCANNER = True

                print(
                    " [!] Cloudflare challenge confirmed."
                )

                finish_runner_telemetry(
                    "cloudflare_challenge",
                    cf_ray=r.headers.get(
                        "CF-Ray"
                    ),
                )

                print(
                    " [!] Ending this scanner immediately "
                    "instead of retrying the challenged runner."
                )

                return []

            VINTED_403_STREAK += 1

            if VINTED_403_STREAK == 1:
                VINTED_COOLDOWN_UNTIL = (
                    time.time()
                    + FIRST_403_COOLDOWN
                )

                print(
                    f" [!] Vinted returned a generic "
                    f"HTTP 403. Cooling down for "
                    f"{FIRST_403_COOLDOWN}s."
                )

                return []

            if VINTED_403_STREAK == 2:
                VINTED_COOLDOWN_UNTIL = (
                    time.time()
                    + SECOND_403_COOLDOWN
                )

                print(
                    f" [!] Vinted returned another generic "
                    f"HTTP 403. Cooling down for "
                    f"{SECOND_403_COOLDOWN}s."
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
                f" [!] Vinted catalogue returned "
                f"HTTP {r.status_code}."
            )

            return []

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


def fetch_user_profile(
    user_id,
) -> dict:
    url = (
        f"https://{VINTED_DOMAIN}"
        f"/api/v2/users/{user_id}"
    )

    try:
        r = SESSION.get(
            url,
            headers=VINTED_HEADERS,
            timeout=10,
        )

        if r.status_code == 200:
            return (
                r.json()
                .get(
                    "user",
                    {},
                )
            )

    except Exception:
        pass

    return {}


def matches_exclude_words(
    item: dict,
    exclude_words: list,
) -> bool:
    if not exclude_words:
        return False

    title = (
        item.get("title")
        or ""
    ).lower()

    return any(
        word.lower().strip()
        in title
        for word in exclude_words
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

        stars = max(
            0,
            min(
                5,
                round(
                    score * 5
                ),
            ),
        )

        return (
            "⭐" * stars
            + "✩" * (
                5 - stars
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return "No ratings"


def get_item_url(
    item: dict,
) -> str:
    url = item.get(
        "url",
        "",
    )

    if (
        url
        and not url.startswith(
            "http"
        )
    ):
        url = (
            f"https://{VINTED_DOMAIN}"
            f"{url}"
        )

    return url


def build_payload(
    label: str,
    item: dict,
    colour: int,
) -> dict:
    item_url = get_item_url(
        item
    )

    user_id = (
        item.get(
            "user",
            {},
        )
        .get(
            "id"
        )
    )

    if user_id:
        user_profile = (
            fetch_user_profile(
                user_id
            )
        )

        if user_profile:
            item["user"] = {
                **item.get(
                    "user",
                    {},
                ),
                **user_profile,
            }

    user = item.get(
        "user",
        {},
    )

    seller = user.get(
        "login",
        "Unknown seller",
    )

    seller_id = user.get(
        "id"
    )

    seller_url = (
        f"https://{VINTED_DOMAIN}"
        f"/member/{seller_id}"
        if seller_id
        else item_url
    )

    amount = (
        item.get(
            "price",
            {},
        )
        .get(
            "amount",
            "?",
        )
    )

    price_str = (
        f"{CURRENCY_SYMBOL}{amount}"
    )

    brand = (
        item.get(
            "brand_title"
        )
        or "—"
    )

    size = (
        item.get(
            "size_title"
        )
        or "—"
    )

    raw_status = (
        item.get(
            "status"
        )
        or ""
    )

    status_id = item.get(
        "status_id"
    )

    condition = (
        CONDITION_LABELS.get(
            status_id,
            raw_status,
        )
        if status_id
        else raw_status
        or "—"
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
                s = str(
                    created_at
                )[:19]

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
        unix_ts = int(
            time.time()
        )

    published = (
        f"<t:{unix_ts}:R>"
    )

    feedback_score = (
        user.get(
            "feedback_reputation"
        )
        or user.get(
            "feedback_score"
        )
        or item.get(
            "user",
            {},
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

    feedback_str = (
        f"{star_rating(feedback_score)} "
        f"({feedback_count})"
    )

    photos = item.get(
        "photos",
        [],
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
                "value": brand,
                "inline": True,
            },
            {
                "name": "📐 Size",
                "value": size,
                "inline": True,
            },
            {
                "name": "⭐ Feedbacks",
                "value": feedback_str,
                "inline": True,
            },
            {
                "name": "💎 Status",
                "value": condition,
                "inline": True,
            },
            {
                "name": "💰 Price",
                "value": price_str,
                "inline": True,
            },
        ],

        "footer": {
            "text": f"🔍 Search: {label}"
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


def send_discord(
    label: str,
    item: dict,
    colour: int,
    channel_id: str = None,
):
    target_channel = (
        channel_id
        or DISCORD_CHANNEL_ID
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
        f"https://discord.com"
        f"/api/v10/channels/"
        f"{target_channel}/messages"
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
        print(
            f" [!] Discord error "
            f"{e.response.status_code}: "
            f"{e.response.text}"
        )

    except requests.RequestException as e:
        print(
            f" [!] Discord error: {e}"
        )


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

        for error in errors:
            print(
                f" ERROR: {error}"
            )

        print(
            "=" * 55
        )

        raise SystemExit(
            1
        )


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

    for search in searches:
        exclude_words = (
            search.get(
                "exclude_words",
                [],
            )
        )

        excl_str = (
            f" | exclude: "
            f"{', '.join(exclude_words)}"
            if exclude_words
            else ""
        )

        ch_str = (
            f" | channel: "
            f"{search['channel_id']}"
            if search.get(
                "channel_id"
            )
            else (
                f" | channel: default "
                f"({DISCORD_CHANNEL_ID})"
            )
        )

        print(
            f" * {search['label']}"
            f"{excl_str}"
            f"{ch_str}"
        )

    print()

    start_runner_telemetry()

    get_vinted_session_cookie()

    seen = load_seen()

    if not bool(seen):
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
                [],
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
        search["label"]:
        COLOURS[
            i % len(COLOURS)
        ]

        for i, search
        in enumerate(
            searches
        )
    }

    start_time = time.time()
    checks = 0

    while (
        time.time()
        - start_time
        < RUN_DURATION
    ):
        checks += 1

        CURRENT_CHECK_COUNT = (
            checks
        )

        ts = (
            datetime.now()
            .strftime(
                "%H:%M:%S"
            )
        )

        print(
            f"[{ts}] Check #{checks}...",
            end=" ",
            flush=True,
        )

        found_new = 0

        for search in searches:
            key = search["label"]

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

            if STOP_SCANNER:
                break

        save_seen(
            seen
        )

        if STOP_SCANNER:
            print(
                "Scanner stopped because "
                "Cloudflare challenged this runner."
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