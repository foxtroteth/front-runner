import os
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
from playwright.async_api import async_playwright

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

LOGIN_URL = "https://community.cikal.co.id/auth#/login"
NOTIF_URL = "https://community.cikal.co.id/parent#/notification/index"
EVENT_URL = "https://community.cikal.co.id/parent#/event/index"

EVENT_KEYWORDS = [
    "event", "acara", "kegiatan", "workshop", "seminar", "webinar",
    "festival", "lomba", "competition", "pameran", "exhibition",
    "pelatihan", "training", "gathering", "bazaar", "bazar",
]

API_URL_KEYWORDS = ["notification", "notif", "activity", "news", "event"]


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


async def _do_login(page) -> None:
    """Fill and submit the login form. Raises if login fails."""
    log.info("Navigating to login page...")
    # domcontentloaded, not networkidle: the portal keeps connections open
    # (observed 2026-07-27), so networkidle never fires and goto times out
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

    try:
        await page.wait_for_selector('input[type="password"]', timeout=20_000, state="visible")
    except Exception:
        await page.screenshot(path="debug_01_login.png")
        log.info("Page HTML head:\n%s", (await page.content())[:3000])
        raise RuntimeError("Login form never rendered — page may have changed or blocked us")

    await asyncio.sleep(1)

    if DEBUG:
        await page.screenshot(path="debug_01_login.png")

    filled = False
    for sel in [
        'input[inputmode="email"]',
        'input[placeholder="User Name"]',
        'input[placeholder*="user name" i]',
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'form input.input:not([type="password"])',
    ]:
        try:
            await page.fill(sel, CIKAL_USERNAME, timeout=3_000)
            log.info("Email field: %s", sel)
            filled = True
            break
        except Exception:
            pass

    if not filled:
        await page.screenshot(path="debug_01_login.png")
        raise RuntimeError("Could not find email/username input on login page")

    for sel in ['input[type="password"]', 'input[placeholder="password"]', 'input[name="password"]']:
        try:
            await page.fill(sel, CIKAL_PASSWORD, timeout=3_000)
            log.info("Password field: %s", sel)
            break
        except Exception:
            pass

    for sel in [
        'button:has-text("Sign In")',
        'button:has-text("Sign in")',
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Masuk")',
        'input[type="submit"]',
    ]:
        try:
            await page.click(sel, timeout=3_000)
            log.info("Submit button: %s", sel)
            break
        except Exception:
            pass

    await asyncio.sleep(8)

    current_url = page.url
    log.info("Post-login URL: %s", current_url)
    if "login" in current_url or "auth" in current_url:
        if DEBUG:
            await page.screenshot(path="debug_02_login_fail.png")
        raise RuntimeError(f"Login failed — still at: {current_url}")


async def scrape(state: dict) -> tuple[list[dict], list]:
    """
    Login (or reuse saved session) then scrape events + notifications.
    Returns (items, new_cookies).
    new_cookies is [] when the existing session was still valid.
    """
    captured_api: list[dict] = []
    new_cookies: list = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        # Restore saved session cookies
        saved_cookies = state.get("cookies", [])
        if saved_cookies:
            await context.add_cookies(saved_cookies)
            log.info("Loaded %d saved cookies — will test session validity", len(saved_cookies))

        page = await context.new_page()

        async def on_response(response):
            url = response.url
            if response.status == 200 and any(k in url.lower() for k in API_URL_KEYWORDS):
                try:
                    data = await response.json()
                    captured_api.append({"url": url, "data": data})
                    log.info("Captured API: %s", url)
                except Exception:
                    pass

        page.on("response", on_response)

        page_errors: list[str] = []

        async def load(url: str, label: str) -> None:
            """goto + settle. domcontentloaded, not networkidle: the portal
            keeps connections open (observed 2026-07-27), so networkidle
            never fires. The XHRs we capture arrive during the sleep."""
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(6)
            except Exception as exc:
                page_errors.append(f"{label}: {exc}")
                log.warning("%s failed to load: %s", label, exc)

        # Navigate to events page — doubles as session validity check
        log.info("Loading events page...")
        await load(EVENT_URL, "events page")

        if "login" in page.url or "auth" in page.url:
            log.info("Session invalid — logging in fresh")
            await _do_login(page)

            # Save new cookies so future runs skip login. Written into state
            # here (not only on success) so a run that logs in but then fails
            # still persists the fresh session via the failure-path save_state.
            new_cookies = await context.cookies()
            state["cookies"] = new_cookies
            log.info("Session saved (%d cookies)", len(new_cookies))

            # Revisit events page now that we're logged in
            log.info("Reloading events page after login...")
            await load(EVENT_URL, "events page (after login)")
        else:
            log.info("Session valid — login skipped")

        if DEBUG:
            await page.screenshot(path="debug_03_events.png")

        # Also hit the notifications page for the pickup-arrival signal.
        # Attempted even if the events page failed — it carries the
        # arrival/pickup signals, so it must not die with the events page.
        log.info("Loading notifications page...")
        await load(NOTIF_URL, "notifications page")

        if DEBUG:
            await page.screenshot(path="debug_04_notifications.png")

        await browser.close()

    if not captured_api:
        # A healthy run always captures at least the event feed (even when
        # the notification feed is agreement-gated). Zero captures means a
        # dead session, a broken portal, or too-slow page loads — count it
        # as a failure so the 10-in-a-row Discord alert can fire.
        detail = "; ".join(page_errors) if page_errors else "pages loaded but no matching XHR seen"
        raise RuntimeError(f"no API responses captured: {detail}")

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
            for key in ("data", "notifications", "items", "results", "list", "events"):
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
