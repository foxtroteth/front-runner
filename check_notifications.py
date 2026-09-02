import os
import re
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta, time
from pathlib import Path

JAKARTA_TZ = timezone(timedelta(hours=7))

# Mon-Fri only (weekday() 0=Mon … 4=Fri)
SCHEDULE_WINDOWS = [
    (time(6, 30), time(9, 0)),    # morning:   06:30–09:00 GMT+7
    (time(11, 0), time(17, 0)),   # afternoon: 11:00–17:00 GMT+7
]


def today_jkt() -> str:
    return datetime.now(JAKARTA_TZ).date().isoformat()


def is_in_schedule() -> bool:
    now = datetime.now(JAKARTA_TZ)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    t = now.time().replace(second=0, microsecond=0)
    return any(start <= t <= end for start, end in SCHEDULE_WINDOWS)


import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CIKAL_USERNAME = os.environ["CIKAL_USERNAME"]
CIKAL_PASSWORD = os.environ["CIKAL_PASSWORD"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
STATE_FILE = Path("state.json")
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

# The portal blackholes headless-browser TLS connections from datacenter IPs
# (observed 2026-09-02: from the same GitHub runner, curl gets HTTP 200 in
# ~1s while headless Chromium times out after 30s), but plain HTTP clients
# still work — so the bot talks to the JSON API directly instead of driving
# a browser. Auth is a Laravel-style form POST to /login with an X-CSRF-TOKEN
# read from the login page's <meta name="csrf-token">.
BASE_URL = "https://community.cikal.co.id"
LOGIN_PAGE_URL = f"{BASE_URL}/auth"        # SPA shell carrying the csrf-token meta
LOGIN_POST_URL = f"{BASE_URL}/auth/login"  # JSON login endpoint
EVENT_API_URL = f"{BASE_URL}/parent/event/index?load=json"
NOTIF_API_URL = f"{BASE_URL}/parent/notification/index?load=json&p=1&l=10"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Keys under which the portal nests its list-shaped feed payloads. Used both
# to decide a session is valid (the feed came back as a real feed, not an
# auth-error envelope like {"errors":[...]}) and to extract items.
FEED_LIST_KEYS = ("data", "notifications", "items", "results", "list", "events")

EVENT_KEYWORDS = [
    "event", "acara", "kegiatan", "workshop", "seminar", "webinar",
    "festival", "lomba", "competition", "pameran", "exhibition",
    "pelatihan", "training", "gathering", "bazaar", "bazar",
]

def is_event_notification(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in EVENT_KEYWORDS)


def is_pickup_arrival(text: str) -> bool:
    """End-of-day signal: Kaia has been handed over at TerasKota."""
    t = text.lower()
    if "shuttle bus - release" in t or "shuttle bus release" in t:
        return True
    return "handed over" in t and "teraskota" in t


def is_school_arrival(text: str) -> bool:
    """Morning signal: Kaia has arrived at school.

    The wording changes between academic years — 2025/26 said
    "has arrived at Sekolah Cikal Serpong at 07:21", 2026/27 says
    "has arrived at Campus A TK-SD - Sekolah Cikal Serpong at 07:24" —
    so don't require the two phrases to be adjacent. Must NOT match the
    afternoon "has arrived at TerasKota" message.
    """
    t = text.lower()
    return "has arrived at" in t and "sekolah cikal" in t


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": [], "first_run": True}


MAX_SEEN_IDS = 200

def save_state(state: dict):
    ids = state.get("seen_ids", [])
    if len(ids) > MAX_SEEN_IDS:
        state["seen_ids"] = ids[-MAX_SEEN_IDS:]
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


async def send_discord(content: str):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content, "username": "Cikal School Bot"},
        )
        resp.raise_for_status()
    log.info("Discord message sent")


async def send_ntfy(title: str, body: str = ""):
    text = body or title
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            content=text.encode("utf-8"),
        )
        resp.raise_for_status()
    log.info("ntfy notification sent")


async def notify(title: str, body: str = ""):
    """Send via ntfy (primary) and Discord (backup). Both are always attempted."""
    discord_msg = f"**{title}**" + (f"\n{body}" if body else "")
    results = await asyncio.gather(
        send_ntfy(title, body),
        send_discord(discord_msg),
        return_exceptions=True,
    )
    for channel, exc in zip(("ntfy", "Discord"), results):
        if isinstance(exc, Exception):
            log.error("%s delivery failed: %s", channel, exc)


def notif_id(item: dict) -> str:
    key = item.get("id") or item.get("text", "")
    return hashlib.md5(str(key).encode()).hexdigest()


def mark_all_seen(items: list[dict], seen_ids: set, seen_list: list):
    """Mark every currently visible item as seen (in feed order)."""
    for it in items:
        iid = notif_id(it)
        if iid not in seen_ids:
            seen_ids.add(iid)
            seen_list.append(iid)


def _cookies_to_state(client: httpx.AsyncClient) -> list:
    """Dump the client's cookie jar to the list-of-dicts shape state.json uses."""
    out = []
    for c in client.cookies.jar:
        out.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or "community.cikal.co.id",
            "path": c.path or "/",
        })
    return out


async def _http_login(client: httpx.AsyncClient) -> None:
    """Log in over plain HTTP (no browser). Raises if login fails.

    Reads the JWT CSRF token from the login page's <meta name="csrf-token">
    and POSTs credentials to /auth/login. Tries a JSON body first (the SPA
    uses axios), then falls back to a form-encoded body; success is detected
    by the portal returning its session cookie (com_at) or a 200.
    """
    log.info("Logging in over HTTP...")
    r0 = await client.get(LOGIN_PAGE_URL)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r0.text)
    if not m:
        raise RuntimeError(
            f"CSRF token not found on login page (HTTP {r0.status_code}, {len(r0.text)} bytes)"
        )
    csrf = m.group(1)

    headers = {
        "X-CSRF-TOKEN": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": LOGIN_PAGE_URL,
        "Origin": BASE_URL,
        "Accept": "application/json, text/plain, */*",
    }

    # Single JSON POST — the exact shape the SPA sends (verified 2026-09-02:
    # /auth/login accepts it and returns 401 {"errors":[...]} on bad creds).
    # One attempt only: never re-submit the same credentials, so a wrong
    # password can't hammer the account toward a lockout. Success = the portal
    # hands back its session cookie (com_at) or a 200; the caller re-probes the
    # feed afterwards, so a false 200 can't slip through as a real session.
    r = await client.post(
        LOGIN_POST_URL,
        json={"email": CIKAL_USERNAME, "password": CIKAL_PASSWORD, "remember": True},
        headers=headers,
    )
    if "com_at" in client.cookies or r.status_code == 200:
        log.info("Login accepted (HTTP %s, %d cookies)", r.status_code, len(client.cookies))
        return
    raise RuntimeError(f"Login failed (HTTP {r.status_code}): {r.text[:200]}")


def _looks_like_feed(data) -> bool:
    """True when the payload is a real feed (a list, or a dict nesting one
    under a known key) rather than an auth-error envelope like
    {"errors":[...]} / {"message":"Unauthenticated"}. Aligning the
    session-validity check with what extract_from_api can actually read stops
    an expired session that returns an error-JSON 200 from being mistaken for
    a valid one (which would reset the failure counter and go silent)."""
    if isinstance(data, list):
        return True
    if isinstance(data, dict):
        return any(isinstance(data.get(k), list) for k in FEED_LIST_KEYS)
    return False


async def _fetch_json(client: httpx.AsyncClient, url: str, label: str):
    """GET a portal JSON feed. Returns parsed JSON, or None when the response
    is empty/HTML — which is what the portal returns when the session is not
    authenticated (observed 2026-09-02: HTTP 200, 0 bytes)."""
    try:
        r = await client.get(url, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/parent",
            "Accept": "application/json, text/plain, */*",
        })
    except Exception as exc:
        log.warning("%s request failed: %s", label, exc)
        return None

    body = r.text.strip()
    if r.status_code == 200 and body[:1] in ("[", "{"):
        try:
            data = r.json()
            log.info("Fetched %s (%d bytes)", label, len(r.text))
            return data
        except Exception as exc:
            log.warning("%s returned non-JSON: %s", label, exc)
            return None

    log.info("%s empty/HTML (HTTP %s, %d bytes) — session likely invalid",
             label, r.status_code, len(r.text))
    return None


async def scrape(state: dict) -> tuple[list[dict], list]:
    """
    Fetch events + notifications over plain HTTP (no browser).
    Returns (items, new_cookies); new_cookies is [] when the saved session
    was reused.
    """
    captured_api: list[dict] = []
    new_cookies: list = []

    jar = httpx.Cookies()
    for c in state.get("cookies", []):
        try:
            jar.set(c["name"], c["value"],
                    domain=c.get("domain", "community.cikal.co.id"),
                    path=c.get("path", "/"))
        except Exception:
            pass
    if state.get("cookies"):
        log.info("Loaded %d saved cookies — will test session validity", len(state["cookies"]))

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers={"User-Agent": USER_AGENT},
        cookies=jar,
    ) as client:
        # The event feed is the session-validity probe: it comes back as a
        # real feed whenever logged in (unlike the notification feed, which
        # can be empty while a start-of-year agreement is pending). A response
        # that isn't feed-shaped (empty, HTML, or an auth-error envelope)
        # means the session is dead — log in fresh.
        event = await _fetch_json(client, EVENT_API_URL, "events")
        if not _looks_like_feed(event):
            log.info("Session invalid — logging in fresh")
            await _http_login(client)
            # Persist fresh cookies immediately (not only on full success) so
            # a run that logs in but later fails still keeps the new session.
            new_cookies = _cookies_to_state(client)
            state["cookies"] = new_cookies
            log.info("Session saved (%d cookies)", len(new_cookies))
            event = await _fetch_json(client, EVENT_API_URL, "events")
        else:
            log.info("Session valid — login skipped")

        # The notification feed carries the arrival/pickup signals; fetch it
        # even if the event feed came back empty.
        notif = await _fetch_json(client, NOTIF_API_URL, "notifications")

        # Append only feed-shaped payloads: an error envelope must not count
        # as a captured response, or the run would look successful (counter
        # reset, no alert) while delivering nothing.
        if _looks_like_feed(event):
            captured_api.append({"url": EVENT_API_URL, "data": event})
        if _looks_like_feed(notif):
            captured_api.append({"url": NOTIF_API_URL, "data": notif})

    if not captured_api:
        # A healthy run always captures at least the event feed (even when
        # the notification feed is agreement-gated). Zero captures means a
        # failed login or an unreachable portal — count it as a failure so
        # the 10-in-a-row Discord alert can fire.
        raise RuntimeError("no API responses captured (login failed or portal unreachable)")

    items = extract_from_api(captured_api)
    return items, new_cookies


def extract_from_api(api_data: list[dict]) -> list[dict]:
    """Pull structured items from captured API responses. Event URLs come first."""
    all_items: list[dict] = []
    seen_dedup: set[str] = set()

    sorted_data = sorted(
        api_data,
        key=lambda e: (0 if "event" in e["url"].lower() else 1),
    )

    for entry in sorted_data:
        data = entry["data"]
        candidates = None
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for key in FEED_LIST_KEYS:
                v = data.get(key)
                if isinstance(v, list) and len(v) > 0:
                    candidates = v
                    break

        if not candidates:
            continue

        for i, c in enumerate(candidates):
            if not isinstance(c, dict):
                continue
            text = (
                c.get("message")
                or c.get("content")
                or c.get("description")
                or c.get("title")
                or str(c)
            )
            item_id = str(c.get("id") or c.get("_id") or i)
            dedup_key = f"{entry['url']}:{item_id}"
            if dedup_key not in seen_dedup:
                seen_dedup.add(dedup_key)
                all_items.append({
                    "id": item_id,
                    "text": str(text),
                    "source_url": entry["url"],
                })

    log.info("Extracted %d unique items from %d API responses", len(all_items), len(api_data))
    return all_items


async def main():
    if not is_in_schedule():
        log.info("Outside schedule window — skipping.")
        return

    state = load_state()
    is_first_run = state.get("first_run", False)
    # Keep seen_ids in insertion (chronological) order: the save_state cap
    # drops the oldest entries, and stable ordering keeps the state.json
    # diffs append-only instead of rewriting the whole list every run.
    seen_list: list[str] = list(state.get("seen_ids", []))
    seen_ids: set[str] = set(seen_list)

    # Skip if today's pickup already happened
    if state.get("done_for_date") == today_jkt() and not is_first_run:
        log.info("Already done for today (TerasKota pickup detected) — skipping.")
        return

    # Skip if within post-arrival cooldown
    cooldown_until = state.get("cooldown_until")
    if cooldown_until:
        cooldown_dt = datetime.fromisoformat(cooldown_until)
        if datetime.now(JAKARTA_TZ) < cooldown_dt:
            log.info("In post-arrival cooldown until %s — skipping.", cooldown_until)
            return

    log.info("Checking for new events (first_run=%s)...", is_first_run)

    try:
        items, new_cookies = await scrape(state)
    except Exception as exc:
        err = str(exc)
        log.error("Scraping error: %s", err)

        # Timeouts count as failures too: on 2026-07-27 the portal made
        # networkidle unreachable and the old silent-skip path hid a whole
        # morning of dead runs with green checkmarks and no alert.
        failures = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = failures
        save_state(state)
        log.info("Consecutive failures: %d", failures)

        if failures >= 10:
            # Re-alert at most every 3h: a long portal outage (2026-09-02
            # produced 5 alerts in one day) should not flood Discord. The
            # counter keeps climbing so the alert shows outage length.
            last_alert = state.get("last_alert_at")
            recently_alerted = last_alert and (
                datetime.now(JAKARTA_TZ) - datetime.fromisoformat(last_alert)
                < timedelta(hours=3)
            )
            if not recently_alerted:
                await send_discord(
                    f"⚠️ **Cikal Bot Error** ({failures} consecutive failures, "
                    f"~{failures * 5} min)\n```\n{err[:500]}\n```"
                )
                state["last_alert_at"] = datetime.now(JAKARTA_TZ).isoformat()
                save_state(state)
        raise SystemExit(1)

    # Successful scrape — reset failure counter
    state["consecutive_failures"] = 0
    if state.pop("last_alert_at", None):
        try:
            await send_discord("✅ **Cikal Bot recovered** — portal reachable again")
        except Exception as exc:
            log.error("Recovery notice failed: %s", exc)

    # Persist updated cookies if login was needed
    if new_cookies:
        state["cookies"] = new_cookies

    if is_first_run:
        all_ids = [notif_id(n) for n in items]
        state.update({
            "seen_ids": all_ids,
            "first_run": False,
            "last_check": datetime.now().isoformat(),
        })
        save_state(state)
        await send_discord(
            "✅ **Cikal School Bot is active!**\n"
            f"Monitoring for all new notifications.\n"
            f"Found {len(all_ids)} existing notification(s) on first run — not re-sent."
        )
        log.info("First run done. Marked %d items as seen.", len(all_ids))
        return

    # Check all items for the end-of-day pickup signal and school arrival first
    for item in items:
        nid = notif_id(item)
        text = item.get("text", "")
        if is_school_arrival(text) and nid not in seen_ids:
            # Mark the whole visible feed seen, not just this item: after a
            # backlog surfaces at once (portal outage, agreement gate), any
            # item left unseen here would fire a stale signal on a later run.
            mark_all_seen(items, seen_ids, seen_list)
            cooldown_dt = datetime.now(JAKARTA_TZ) + timedelta(hours=3)
            state["cooldown_until"] = cooldown_dt.isoformat()
            state["seen_ids"] = seen_list
            state["last_check"] = datetime.now().isoformat()
            save_state(state)
            await notify("🏫 Kaia has arrived at school", text[:800])
            log.info("School arrival detected — cooling down until %s", cooldown_dt.isoformat())
            return
        if is_pickup_arrival(text) and nid not in seen_ids:
            # The day is over — mark everything currently in the feed as
            # seen so this morning's backlogged "arrived at school" item
            # can't fire a bogus arrival + 3h cooldown at tomorrow's 06:30
            # run (which would eat the whole real morning window, daily).
            mark_all_seen(items, seen_ids, seen_list)
            state["done_for_date"] = today_jkt()
            state["cooldown_until"] = None
            state["seen_ids"] = seen_list
            state["last_check"] = datetime.now().isoformat()
            save_state(state)
            await notify(
                "🏠 Kaia has been handed over at TerasKota",
                text[:800],
            )
            log.info("Pickup detected — pausing for the rest of %s", today_jkt())
            return

    # Notify about all new notifications
    new_items = []
    for item in items:
        nid = notif_id(item)
        if nid not in seen_ids:
            new_items.append(item)
            seen_ids.add(nid)
            seen_list.append(nid)

    if new_items:
        log.info("%d new notification(s)!", len(new_items))
        for item in new_items:
            text = item.get("text", "(no text)")[:800]
            await notify("🔔 New Cikal Notification", text)
    else:
        log.info("No new notifications.")

    state.update({
        "seen_ids": seen_list,
        "last_check": datetime.now().isoformat(),
    })
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
