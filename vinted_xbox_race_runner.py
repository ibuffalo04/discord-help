"""
Temporary Xbox Vinted source-race runner.

Purpose
-------
Run the existing vinted_discord_alert.py scanner unchanged while measuring
which public Vinted catalogue source sees genuine Xbox-controller listings
first:

1. Existing server-rendered https://www.vinted.co.uk/catalog HTML
2. Newer https://api.vinted.co.uk/svc-catalogue/items JSON service

Only the EXISTING HTML scanner can send Discord alerts. The JSON API is
shadow/test-only. API failures never stop or modify the normal scanner.

Race results are stored in vinted_source_race.json and printed in the GitHub
Actions log. Remove this wrapper and point the workflow back at
vinted_discord_alert.py to end the experiment.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests

import vinted_discord_alert as bot


RACE_FILE = "vinted_source_race.json"
API_URL = "https://api.vinted.co.uk/svc-catalogue/items"
API_POLL_INTERVAL = 5
API_ERROR_COOLDOWN = 60
CONTINUITY_WINDOW = 120
MAX_KNOWN_IDS_PER_SOURCE = 2000
MAX_RACE_RESULTS = 250

RACE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def utc_iso(epoch=None):
    if epoch is None:
        epoch = time.time()

    return datetime.fromtimestamp(
        epoch,
        timezone.utc,
    ).isoformat()


def load_race_state():
    default = {
        "version": 1,
        "created_at": utc_iso(),
        "last_saved_epoch": 0,
        "sources": {
            "html": {
                "seeded": False,
                "known_ids": [],
            },
            "api": {
                "seeded": False,
                "known_ids": [],
            },
        },
        "items": {},
    }

    if not os.path.exists(RACE_FILE):
        return default

    try:
        with open(
            RACE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default

        state.setdefault(
            "version",
            1,
        )
        state.setdefault(
            "created_at",
            utc_iso(),
        )
        state.setdefault(
            "last_saved_epoch",
            0,
        )
        state.setdefault(
            "sources",
            {},
        )
        state.setdefault(
            "items",
            {},
        )

        for source in (
            "html",
            "api",
        ):
            source_state = (
                state["sources"]
                .setdefault(
                    source,
                    {},
                )
            )

            source_state.setdefault(
                "seeded",
                False,
            )
            source_state.setdefault(
                "known_ids",
                [],
            )

        return state

    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
    ):
        return default


def save_race_state_locked():
    RACE_STATE[
        "last_saved_epoch"
    ] = time.time()

    for source in (
        "html",
        "api",
    ):
        known = (
            RACE_STATE[
                "sources"
            ][
                source
            ].get(
                "known_ids",
                [],
            )
        )

        if (
            len(known)
            > MAX_KNOWN_IDS_PER_SOURCE
        ):
            RACE_STATE[
                "sources"
            ][
                source
            ][
                "known_ids"
            ] = known[
                -MAX_KNOWN_IDS_PER_SOURCE:
            ]

    items = RACE_STATE.get(
        "items",
        {},
    )

    if (
        len(items)
        > MAX_RACE_RESULTS
    ):
        ordered = sorted(
            items.items(),
            key=lambda pair: max(
                pair[1].get(
                    "html_first_seen_epoch",
                    0,
                )
                or 0,
                pair[1].get(
                    "api_first_seen_epoch",
                    0,
                )
                or 0,
            ),
        )

        for item_id, _ in ordered[
            :-MAX_RACE_RESULTS
        ]:
            items.pop(
                item_id,
                None,
            )

    temp_file = (
        f"{RACE_FILE}.tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            RACE_STATE,
            f,
            indent=2,
            ensure_ascii=False,
        )

    os.replace(
        temp_file,
        RACE_FILE,
    )


def prepare_race_state_for_this_run():
    """
    Keep continuity across back-to-back queued runners, but re-seed after a
    longer gap so listings that appeared while the scanner was offline are not
    incorrectly treated as a source-race result.
    """
    with RACE_LOCK:
        last_saved = float(
            RACE_STATE.get(
                "last_saved_epoch",
                0,
            )
            or 0
        )

        gap = (
            time.time()
            - last_saved
            if last_saved
            else None
        )

        if (
            gap is None
            or gap > CONTINUITY_WINDOW
        ):
            for source in (
                "html",
                "api",
            ):
                RACE_STATE[
                    "sources"
                ][
                    source
                ][
                    "seeded"
                ] = False

                RACE_STATE[
                    "sources"
                ][
                    source
                ][
                    "known_ids"
                ] = []

            if gap is None:
                print(
                    " [Race] New comparison file — "
                    "both sources will seed once."
                )

            else:
                print(
                    f" [Race] Previous comparison is "
                    f"{int(gap)}s old; re-seeding "
                    f"current listings before timing "
                    f"new ones."
                )

        else:
            print(
                f" [Race] Continuing previous "
                f"comparison after a "
                f"{gap:.1f}s gap."
            )

        save_race_state_locked()


def effective_exclusions(
    search,
):
    exclusions = list(
        search.get(
            "exclude_words",
            [],
        )
        or []
    )

    if bot.is_xbox_controller_search(
        search
    ):
        exclusions.extend(
            list(
                getattr(
                    bot,
                    "XBOX_ALWAYS_EXCLUDE_WORDS",
                    [],
                )
                or []
            )
        )

    output = []
    seen = set()

    for word in exclusions:
        clean = str(
            word
        ).strip()

        key = clean.lower()

        if (
            clean
            and key not in seen
        ):
            output.append(
                clean
            )
            seen.add(
                key
            )

    return output


def valid_xbox_items(
    items,
    search,
):
    exclusions = (
        effective_exclusions(
            search
        )
    )

    valid = []

    for item in (
        items
        or []
    ):
        item_id = str(
            item.get(
                "id",
                "",
            )
        ).strip()

        if not item_id:
            continue

        if bot.matches_exclude_words(
            item,
            exclusions,
        ):
            continue

        if (
            bot.is_xbox_controller_search(
                search
            )
            and not bot.is_xbox_controller_candidate(
                item
            )
        ):
            continue

        valid.append(
            item
        )

    return valid


def record_source_snapshot(
    source,
    items,
    search,
):
    """
    Record the first poll in which each source exposes an item.

    The first successful snapshot per source is baseline-only. This prevents
    the current catalogue backlog from being mistaken for newly surfaced
    listings when the test starts.
    """
    items = valid_xbox_items(
        items,
        search,
    )

    now = time.time()

    with RACE_LOCK:
        source_state = (
            RACE_STATE[
                "sources"
            ][
                source
            ]
        )

        known_list = (
            source_state
            .setdefault(
                "known_ids",
                [],
            )
        )

        known = set(
            str(x)
            for x in known_list
        )

        current_ids = [
            str(
                item.get(
                    "id"
                )
            )
            for item in items
        ]

        if not source_state.get(
            "seeded",
            False,
        ):
            for item_id in current_ids:
                if item_id not in known:
                    known_list.append(
                        item_id
                    )
                    known.add(
                        item_id
                    )

            source_state[
                "seeded"
            ] = True

            source_state[
                "seeded_at"
            ] = utc_iso(
                now
            )

            source_state[
                "last_success_at"
            ] = utc_iso(
                now
            )

            print(
                f" [Race {source.upper()}] "
                f"Baseline seeded with "
                f"{len(current_ids)} valid "
                f"Xbox listing(s)."
            )

            save_race_state_locked()
            return

        new_items = []

        for item in items:
            item_id = str(
                item.get(
                    "id",
                    "",
                )
            )

            if (
                not item_id
                or item_id in known
            ):
                continue

            known_list.append(
                item_id
            )
            known.add(
                item_id
            )
            new_items.append(
                item
            )

            entry = (
                RACE_STATE[
                    "items"
                ].setdefault(
                    item_id,
                    {
                        "id": item_id,
                        "title": (
                            item.get(
                                "title"
                            )
                            or ""
                        ),
                    },
                )
            )

            if (
                item.get(
                    "title"
                )
                and not entry.get(
                    "title"
                )
            ):
                entry[
                    "title"
                ] = item.get(
                    "title"
                )

            field_epoch = (
                f"{source}_first_seen_epoch"
            )

            field_iso = (
                f"{source}_first_seen"
            )

            if not entry.get(
                field_epoch
            ):
                entry[
                    field_epoch
                ] = now

                entry[
                    field_iso
                ] = utc_iso(
                    now
                )

                print(
                    f" [Race "
                    f"{source.upper()}] "
                    f"FIRST -> "
                    f"{item_id} | "
                    f"{entry.get('title') or 'Unknown title'}"
                )

            maybe_finish_race_locked(
                entry
            )

        source_state[
            "last_success_at"
        ] = utc_iso(
            now
        )

        save_race_state_locked()


def maybe_finish_race_locked(
    entry,
):
    html_time = entry.get(
        "html_first_seen_epoch"
    )

    api_time = entry.get(
        "api_first_seen_epoch"
    )

    if (
        not html_time
        or not api_time
    ):
        return

    if entry.get(
        "result"
    ):
        return

    delta = (
        float(
            html_time
        )
        - float(
            api_time
        )
    )

    absolute = abs(
        delta
    )

    if absolute < 0.75:
        winner = "tie"
        result = (
            "Tie (within 0.75s)"
        )

    elif delta > 0:
        winner = "api"
        result = (
            f"API {absolute:.2f}s "
            f"faster than HTML"
        )

    else:
        winner = "html"
        result = (
            f"HTML {absolute:.2f}s "
            f"faster than API"
        )

    entry[
        "winner"
    ] = winner

    entry[
        "difference_seconds"
    ] = round(
        absolute,
        3,
    )

    entry[
        "result"
    ] = result

    entry[
        "completed_at"
    ] = utc_iso()

    print(
        f" [Race RESULT] "
        f"{entry.get('id')} | "
        f"{result} | "
        f"{entry.get('title') or 'Unknown title'}"
    )


def cookie_value(
    session,
    name,
):
    """
    Avoid RequestsCookieJar.get() CookieConflictError if Vinted happens to
    set the same cookie name for more than one path/domain.
    """
    for cookie in (
        session.cookies
    ):
        if (
            cookie.name
            == name
        ):
            return (
                cookie.value
            )

    return None


def initialise_api_session(
    session,
):
    try:
        response = session.get(
            f"https://{bot.VINTED_DOMAIN}/",
            headers=bot.VINTED_HEADERS,
            timeout=10,
        )

        print(
            f" [Race API] "
            f"Anonymous session "
            f"bootstrap -> HTTP "
            f"{response.status_code}"
        )

    except requests.RequestException as exc:
        print(
            f" [Race API] "
            f"Session bootstrap failed: "
            f"{exc}"
        )


def api_headers(
    session,
):
    headers = {
        "User-Agent": (
            bot.VINTED_HEADERS
            .get(
                "User-Agent",
                "",
            )
        ),
        "Accept": (
            "application/json"
        ),
        "Accept-Language": (
            "en-GB,en;q=0.9"
        ),
        "Locale": "en-GB",
        "Platform": "web",
        "X-Next-App": (
            "marketplace-web"
        ),
        "Origin": (
            f"https://"
            f"{bot.VINTED_DOMAIN}"
        ),
        "Referer": (
            f"https://"
            f"{bot.VINTED_DOMAIN}/"
        ),
    }

    anon_id = cookie_value(
        session,
        "anon_id",
    )

    access_token = cookie_value(
        session,
        "access_token_web",
    )

    if anon_id:
        headers[
            "X-Anon-Id"
        ] = anon_id

    if access_token:
        headers[
            "Authorization"
        ] = (
            f"Bearer "
            f"{access_token}"
        )

    return headers


def api_params(
    search,
):
    params = {
        "page": 1,
        "per_page": 24,
        "search_text": (
            search.get(
                "search_text",
                "",
            )
        ),
        "order": (
            search.get(
                "order",
                "newest_first",
            )
        ),
    }

    if (
        search.get(
            "max_price"
        )
        not in (
            None,
            "",
        )
    ):
        params[
            "price_to"
        ] = search.get(
            "max_price"
        )

    if (
        search.get(
            "min_price"
        )
        not in (
            None,
            "",
        )
    ):
        params[
            "price_from"
        ] = search.get(
            "min_price"
        )

    filter_mapping = {
        "size_ids": "size",
        "brand_ids": "brand",
        "status_ids": "status",
        "catalog_ids": "catalog",
        "color_ids": "color",
        "material_ids": "material",
    }

    for (
        old_key,
        attribute_name,
    ) in (
        filter_mapping.items()
    ):
        values = (
            search.get(
                old_key
            )
            or []
        )

        if values:
            params[
                f"attribute_ids"
                f"[{attribute_name}]"
            ] = ",".join(
                str(
                    value
                )
                for value
                in values
            )

    return params


def text_from_item_box(
    value,
):
    if isinstance(
        value,
        str,
    ):
        return (
            value.strip()
        )

    if isinstance(
        value,
        dict,
    ):
        for key in (
            "title",
            "text",
            "value",
        ):
            candidate = (
                value.get(
                    key
                )
            )

            if (
                isinstance(
                    candidate,
                    str,
                )
                and candidate.strip()
            ):
                return (
                    candidate.strip()
                )

    return ""


def normalise_api_item(
    raw,
):
    item_id = str(
        raw.get(
            "id",
            "",
        )
    ).strip()

    title = str(
        raw.get(
            "title"
        )
        or ""
    ).strip()

    item_box = (
        raw.get(
            "item_box"
        )
        or {}
    )

    brand_title = ""

    if isinstance(
        item_box,
        dict,
    ):
        first_line = (
            text_from_item_box(
                item_box.get(
                    "first_line"
                )
            )
        )

        if (
            first_line
            and title
            and (
                first_line
                .lower()
                .strip()
                != title
                .lower()
                .strip()
            )
        ):
            brand_title = (
                first_line
            )

    return {
        "id": item_id,
        "title": title,
        "brand_title": (
            brand_title
        ),
    }


def fetch_api_items(
    session,
    search,
):
    try:
        response = session.get(
            API_URL,
            params=api_params(
                search
            ),
            headers=api_headers(
                session
            ),
            timeout=20,
        )

    except requests.RequestException as exc:
        print(
            f" [Race API] "
            f"Request failed: "
            f"{exc}"
        )

        return (
            None,
            "network_error",
        )

    if response.status_code != 200:
        print(
            f" [Race API] "
            f"Shadow endpoint returned "
            f"HTTP {response.status_code}; "
            f"normal HTML scanner is "
            f"unaffected."
        )

        if response.status_code in (
            401,
            403,
            429,
        ):
            return (
                None,
                "backoff",
            )

        return (
            None,
            "error",
        )

    try:
        data = response.json()

    except ValueError:
        print(
            " [Race API] HTTP 200 "
            "but response was not JSON; "
            "ignoring this shadow poll."
        )

        return (
            None,
            "error",
        )

    raw_items = data.get(
        "items",
        [],
    )

    if not isinstance(
        raw_items,
        list,
    ):
        print(
            " [Race API] JSON did not "
            "contain an items[] list."
        )

        return (
            None,
            "error",
        )

    items = []

    for raw in raw_items:
        if not isinstance(
            raw,
            dict,
        ):
            continue

        item = normalise_api_item(
            raw
        )

        if item.get(
            "id"
        ):
            items.append(
                item
            )

    return (
        items,
        "ok",
    )


def api_shadow_loop(
    searches,
):
    session = (
        requests.Session()
    )

    initialise_api_session(
        session
    )

    cooldown_until = 0.0

    while not STOP_EVENT.is_set():
        cycle_started = (
            time.time()
        )

        if (
            time.time()
            < cooldown_until
        ):
            wait_for = min(
                1.0,
                max(
                    0.1,
                    cooldown_until
                    - time.time(),
                ),
            )

            STOP_EVENT.wait(
                wait_for
            )
            continue

        for search in searches:
            if STOP_EVENT.is_set():
                break

            (
                items,
                status,
            ) = fetch_api_items(
                session,
                search,
            )

            if status == "ok":
                record_source_snapshot(
                    "api",
                    items,
                    search,
                )
                continue

            if status == "backoff":
                print(
                    f" [Race API] "
                    f"Backing the SHADOW "
                    f"test off for "
                    f"{API_ERROR_COOLDOWN}s. "
                    f"Discord/HTML scanning "
                    f"continues."
                )

                cooldown_until = (
                    time.time()
                    + API_ERROR_COOLDOWN
                )

                session = (
                    requests.Session()
                )

                break

        elapsed = (
            time.time()
            - cycle_started
        )

        sleep_for = max(
            0.1,
            API_POLL_INTERVAL
            - elapsed,
        )

        if STOP_EVENT.wait(
            sleep_for
        ):
            break

        if (
            cooldown_until
            and time.time()
            >= cooldown_until
        ):
            initialise_api_session(
                session
            )

            cooldown_until = 0.0


RACE_STATE = (
    load_race_state()
)

prepare_race_state_for_this_run()


# Read the same search configuration as the real scanner, but shadow only the
# normal Xbox-controller search. PS5 and Elite wrappers remain untouched.
ALL_SEARCHES = (
    bot.load_searches()
)

XBOX_SEARCHES = [
    search
    for search
    in ALL_SEARCHES
    if bot.is_xbox_controller_search(
        search
    )
]

if not XBOX_SEARCHES:
    XBOX_SEARCHES = [
        search
        for search
        in bot.FALLBACK_SEARCHES
        if bot.is_xbox_controller_search(
            search
        )
    ]


# Wrap only the catalogue fetch function. The returned listings and every other
# part of the existing bot are unchanged. The wrapper merely timestamps the
# HTML snapshot immediately after it has been received and parsed.
ORIGINAL_FETCH_LISTINGS = (
    bot.fetch_listings
)


def timed_fetch_listings(
    search,
):
    items = (
        ORIGINAL_FETCH_LISTINGS(
            search
        )
    )

    if (
        bot.is_xbox_controller_search(
            search
        )
        and items
    ):
        record_source_snapshot(
            "html",
            items,
            search,
        )

    return items


bot.fetch_listings = (
    timed_fetch_listings
)


def main():
    if not XBOX_SEARCHES:
        print(
            " [Race] No Xbox Controller "
            "search found; running normal "
            "bot only."
        )

        bot.run()
        return

    print(
        " [Race] HTML vs svc-catalogue "
        "A/B test ENABLED. Only the "
        "existing HTML scanner can send "
        "Discord alerts."
    )

    print(
        f" [Race] Shadow API polling "
        f"every {API_POLL_INTERVAL}s; "
        f"results -> {RACE_FILE}"
    )

    api_thread = (
        threading.Thread(
            target=api_shadow_loop,
            args=(
                XBOX_SEARCHES,
            ),
            name=(
                "vinted-api-shadow"
            ),
            daemon=True,
        )
    )

    api_thread.start()

    try:
        bot.run()

    finally:
        STOP_EVENT.set()

        api_thread.join(
            timeout=5
        )

        with RACE_LOCK:
            save_race_state_locked()

        print(
            " [Race] Comparison "
            "state saved."
        )


if __name__ == "__main__":
    main()