"""
Single-source Xbox Vinted timing wrapper.

The normal Xbox scanner remains responsible for discovery and Discord alerts.
This wrapper does NOT poll the newer catalogue API in parallel.

Behaviour:
- One normal /catalog check every 5 seconds (the existing scanner behaviour).
- When a genuinely new listing is about to be sent to Discord, the core bot
  already opens that item's public page once for seller/rating enrichment.
- This wrapper reuses that SAME item-page request to also read Vinted's visible
  "Uploaded / ... ago" value.
- The wrapper records the exact bot detection time plus Vinted's displayed age
  in vinted_source_race.json.

The current public Vinted payload does not expose a trustworthy absolute upload
timestamp, so any calculated listed-at time is explicitly approximate.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

import vinted_discord_alert as bot


TIMING_FILE = "vinted_source_race.json"
MAX_TIMING_RESULTS = 500

# Make the intended polling interval explicit.
bot.CHECK_INTERVAL = 5


# ──────────────────────────────────────────────
# TIME HELPERS
# ──────────────────────────────────────────────
def utc_iso(epoch=None):
    if epoch is None:
        epoch = time.time()

    return datetime.fromtimestamp(
        epoch,
        timezone.utc,
    ).isoformat()


def utc_clock(epoch):
    return datetime.fromtimestamp(
        epoch,
        timezone.utc,
    ).strftime("%H:%M:%S.%f")[:-3] + " UTC"


# ──────────────────────────────────────────────
# PERSISTED TIMING LOG
# ──────────────────────────────────────────────
def load_timing_state():
    default = {
        "version": 2,
        "mode": "single_html_timing",
        "created_at": utc_iso(),
        "items": {},
    }

    if not os.path.exists(TIMING_FILE):
        return default

    try:
        with open(TIMING_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Older file = the previous HTML-vs-API race format.
        # Start a clean timing log rather than trying to mix two schemas.
        if (
            not isinstance(state, dict)
            or state.get("mode") != "single_html_timing"
        ):
            return default

        state.setdefault("version", 2)
        state.setdefault("mode", "single_html_timing")
        state.setdefault("created_at", utc_iso())
        state.setdefault("items", {})
        return state

    except (OSError, json.JSONDecodeError, TypeError):
        return default


def save_timing_state():
    items = TIMING_STATE.setdefault("items", {})

    if len(items) > MAX_TIMING_RESULTS:
        ordered = sorted(
            items.items(),
            key=lambda pair: pair[1].get("detected_epoch", 0) or 0,
        )

        for item_id, _ in ordered[:-MAX_TIMING_RESULTS]:
            items.pop(item_id, None)

    temp_file = f"{TIMING_FILE}.tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            TIMING_STATE,
            f,
            indent=2,
            ensure_ascii=False,
        )

    os.replace(temp_file, TIMING_FILE)


TIMING_STATE = load_timing_state()


# ──────────────────────────────────────────────
# VINTED "UPLOADED" TEXT PARSER
# ──────────────────────────────────────────────
class UploadDateParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.capturing = False
        self.depth = 0
        self.parts = []
        self.finished = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if (
            not self.capturing
            and attrs.get("data-testid") == "item-attributes-upload_date"
        ):
            self.capturing = True
            self.depth = 1
            return

        if self.capturing:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        # Nothing special is needed for self-closing descendants.
        return

    def handle_data(self, data):
        if self.capturing and data.strip():
            self.parts.append(data.strip())

    def handle_endtag(self, tag):
        if not self.capturing:
            return

        self.depth -= 1

        if self.depth <= 0:
            self.capturing = False
            self.finished = True


def clean_uploaded_text(text):
    text = " ".join(str(text or "").split())

    text = re.sub(
        r"^Uploaded\s*(?:/|:|\||·|-)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return text.strip()


def extract_uploaded_age(page_html):
    parser = UploadDateParser()

    try:
        parser.feed(page_html or "")
    except Exception:
        pass

    visible = clean_uploaded_text(" ".join(parser.parts))

    if visible:
        return visible

    # Fallback for a future small markup change: inspect a short window around
    # the stable data-testid and strip tags from it.
    source = page_html or ""
    marker = 'data-testid="item-attributes-upload_date"'
    pos = source.find(marker)

    if pos >= 0:
        window = source[pos:pos + 2500]
        window = re.sub(
            r"<script\b[^>]*>.*?</script>",
            " ",
            window,
            flags=re.I | re.S,
        )
        window = re.sub(r"<[^>]+>", " ", window)
        window = " ".join(window.split())

        match = re.search(
            r"Uploaded\s*(?:/|:|\||·|-)?\s*"
            r"((?:just now|less than (?:a|1) minute ago|"
            r"\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago))",
            window,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

    return ""


def approximate_age_seconds(age_text):
    """
    Convert Vinted's human display to a rough age only.

    This is deliberately NOT treated as an exact creation timestamp because
    Vinted's public item page exposes relative text rather than an absolute
    server timestamp.
    """
    text = " ".join(str(age_text or "").lower().split())

    if not text:
        return None

    if "just now" in text:
        return 0

    if "less than a minute" in text:
        return 30

    match = re.search(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
        text,
    )

    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    seconds_per_unit = {
        "second": 1,
        "minute": 60,
        "hour": 3600,
        "day": 86400,
        "week": 604800,
        # Approximate only; these are not used as exact calendar arithmetic.
        "month": 2629800,
        "year": 31557600,
    }

    return amount * seconds_per_unit[unit]


def record_timing(item, detected_epoch, uploaded_age):
    item_id = str(item.get("id", "")).strip()

    if not item_id:
        return

    age_seconds = approximate_age_seconds(uploaded_age)

    approx_listed_epoch = (
        detected_epoch - age_seconds
        if age_seconds is not None
        else None
    )

    entry = {
        "id": item_id,
        "title": item.get("title") or "",
        "detected_epoch": detected_epoch,
        "detected_at": utc_iso(detected_epoch),
        "vinted_uploaded_display": uploaded_age or None,
        "approx_age_seconds": age_seconds,
        "approx_listed_at": (
            utc_iso(approx_listed_epoch)
            if approx_listed_epoch is not None
            else None
        ),
    }

    TIMING_STATE.setdefault("items", {})[item_id] = entry
    save_timing_state()

    if uploaded_age:
        if approx_listed_epoch is not None:
            print(
                f" [Timing] {item_id} | "
                f"detected {utc_clock(detected_epoch)} | "
                f"Vinted says '{uploaded_age}' | "
                f"approx listed ~{utc_clock(approx_listed_epoch)} | "
                f"{entry['title'] or 'Unknown title'}"
            )
        else:
            print(
                f" [Timing] {item_id} | "
                f"detected {utc_clock(detected_epoch)} | "
                f"Vinted says '{uploaded_age}' | "
                f"{entry['title'] or 'Unknown title'}"
            )
    else:
        print(
            f" [Timing] {item_id} | "
            f"detected {utc_clock(detected_epoch)} | "
            f"Vinted upload age unavailable | "
            f"{entry['title'] or 'Unknown title'}"
        )


# ──────────────────────────────────────────────
# REUSE THE CORE BOT'S EXISTING ITEM-PAGE REQUEST
# ──────────────────────────────────────────────
def fetch_public_item_seller_with_timing(item):
    """
    This replaces the core helper with the same behaviour, plus extraction of
    the visible Uploaded age from the very same HTTP response.

    No extra item-page request is added.
    """
    item_url = bot.get_item_url(item)

    if not item_url:
        return {}

    try:
        r = bot.SESSION.get(
            item_url,
            headers=bot.VINTED_HEADERS,
            timeout=15,
        )

        if r.status_code != 200:
            print(
                f" [Seller] Item page returned HTTP "
                f"{r.status_code}; sending alert without seller details."
            )
            return {}

        seller = bot.parse_public_item_seller(
            r.text,
            item.get("id"),
        )

        uploaded_age = extract_uploaded_age(r.text)
        detected_epoch = item.get("_timing_detected_epoch")

        if detected_epoch:
            record_timing(
                item,
                float(detected_epoch),
                uploaded_age,
            )

            item["_timing_logged"] = True

        if seller.get("login"):
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


bot.fetch_public_item_seller = fetch_public_item_seller_with_timing


# Capture the exact moment a genuine new listing reaches the Discord-send path.
ORIGINAL_SEND_DISCORD = bot.send_discord


def send_discord_with_timing(
    label,
    item,
    colour,
    channel_id=None,
):
    detected_epoch = time.time()

    item["_timing_detected_epoch"] = detected_epoch
    item["_timing_logged"] = False

    result = ORIGINAL_SEND_DISCORD(
        label,
        item,
        colour,
        channel_id,
    )

    # In the unlikely event seller enrichment did not run (for example Vinted
    # starts putting seller data directly in catalogue HTML), still record the
    # exact detection time rather than silently losing the diagnostic.
    if not item.get("_timing_logged"):
        record_timing(
            item,
            detected_epoch,
            "",
        )

    item.pop("_timing_detected_epoch", None)
    item.pop("_timing_logged", None)

    return result


bot.send_discord = send_discord_with_timing


# ──────────────────────────────────────────────
# START THE UNCHANGED CORE SCANNER
# ──────────────────────────────────────────────
def main():
    print(
        " [Timing] Single-source mode enabled: "
        "HTML catalogue only, every 5 seconds."
    )

    print(
        " [Timing] No background svc-catalogue race is running."
    )

    print(
        f" [Timing] New-listing diagnostics -> {TIMING_FILE}"
    )

    bot.run()


if __name__ == "__main__":
    main()