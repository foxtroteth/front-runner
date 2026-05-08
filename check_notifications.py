import os
import json
import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path

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
STATE_FILE = Path("state.json")
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

LOGIN_URL = "https://community.cikal.co.id/auth#/login"
NOTIF_URL = "https://community.cikal.co.id/parent#/notification/index"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": [], "first_run": True}


def save_state(state: dict):
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


def notif_id(item: dict) -> str:
    key = item.get("id") or item.get("text", "")
    return hashlib.md5(str(key).encode()).hexdigest()


async def scrape() -> tuple[list[dict], list[dict]]:
    """Login and scrape notifications. Returns (dom_items, api_items)."""
    captured_api: list[dict] = []

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
        page = await context.new_page()

        # Intercept API responses that look like notifications
        async def on_response(response):
            url = response.url
            if response.status == 200 and any(
                k in url.lower() for k in ["notification", "notif", "activity", "news"]
            ):
                try:
                    data = await response.json()
                    captured_api.append({"url": url, "data": data})
                    log.info("Captured API response: %s", url)
                except Exception:
                    pass

        page.on("response", on_response)

        # ── Login ──────────────────────────────────────────────────────────────
        log.info("Loading login page...")
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

        # The login page is a Vue SPA — wait for the form to actually mount
        log.info("Waiting for login form to render...")
        try:
            await page.wait_for_selector(
                'input[type="password"]', timeout=20_000, state="visible"
            )
        except Exception:
            await page.screenshot(path="debug_01_login.png")
            log.info("Page HTML head:\n%s", (await page.content())[:5000])
            raise RuntimeError(
                "Login form never rendered — page may have changed or blocked us"
            )
        await asyncio.sleep(1)

        if DEBUG:
            await page.screenshot(path="debug_01_login.png")
            log.info("Screenshot saved: debug_01_login.png")

        # Cikal-specific selectors (Vue form uses placeholder + inputmode, no name attr)
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
            log.info("Page HTML:\n%s", (await page.content())[:5000])
            raise RuntimeError("Could not find email/username input on login page")

        for sel in [
            'input[type="password"]',
            'input[placeholder="Password"]',
            'input[name="password"]',
        ]:
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

        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(3)

        current_url = page.url
        log.info("Post-login URL: %s", current_url)

        if "login" in current_url or "auth" in current_url:
            if DEBUG:
                await page.screenshot(path="debug_02_login_fail.png")
            raise RuntimeError(
                f"Login appears to have failed — still at: {current_url}"
            )

        # ── Notifications page ─────────────────────────────────────────────────
        log.info("Loading notifications page...")
        await page.goto(NOTIF_URL, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(4)

        if DEBUG:
            await page.screenshot(path="debug_03_notifications.png")
            log.info("Screenshot saved: debug_03_notifications.png")

        # Extract items from DOM — try common SPA patterns
        dom_items: list[dict] = await page.evaluate(
            """() => {
            const selectors = [
                '.notification-item',
                '.notif-item',
                '[class*="notification-list"] > *',
                '[class*="notif-list"] li',
                '.list-group-item',
                '[class*="notif"][class*="card"]',
                '[class*="notification"][class*="row"]',
            ];
            for (const sel of selectors) {
                const nodes = document.querySelectorAll(sel);
                if (nodes.length > 0) {
                    return Array.from(nodes)
                        .map((el, i) => ({
                            id: el.getAttribute('data-id') || el.id || String(i),
                            text: el.innerText.trim().replace(/\\s+/g, ' '),
                            selector: sel,
                        }))
                        .filter(n => n.text.length > 10);
                }
            }
            // Fallback: grab all non-trivial text blocks from the page body
            return [{
                id: 'fullpage',
                text: document.body.innerText.trim().replace(/\\s+/g, ' '),
                selector: 'body',
            }];
        }"""
        )

        log.info(
            "DOM items: %d (selector: %s)",
            len(dom_items),
            dom_items[0].get("selector") if dom_items else "none",
        )
        await browser.close()

    return dom_items, captured_api


def extract_from_api(api_data: list[dict]) -> list[dict] | None:
    """Try to pull a structured notification list from captured API responses."""
    for entry in api_data:
        data = entry["data"]
        candidates = None
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for key in ("data", "notifications", "items", "results", "list"):
                v = data.get(key)
                if isinstance(v, list) and len(v) > 0:
                    candidates = v
                    break
        if candidates:
            items = []
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
                items.append({"id": item_id, "text": str(text)})
            if items:
                log.info("Using %d items from API: %s", len(items), entry["url"])
                return items
    return None


async def main():
    state = load_state()
    is_first_run = state.get("first_run", False)
    seen_ids: set[str] = set(state.get("seen_ids", []))

    log.info("Checking notifications (first_run=%s)...", is_first_run)

    try:
        dom_items, api_data = await scrape()
    except Exception as exc:
        log.error("Scraping error: %s", exc)
        await send_discord(f"⚠️ **Cikal Bot Error**\n```\n{exc}\n```")
        raise SystemExit(1)

    # Prefer API data (structured) over DOM text scraping
    items = extract_from_api(api_data) or dom_items

    if is_first_run:
        # Mark everything currently visible as already seen — don't spam on boot
        all_ids = [notif_id(n) for n in items]
        state.update(
            {
                "seen_ids": all_ids,
                "first_run": False,
                "last_check": datetime.now().isoformat(),
            }
        )
        save_state(state)
        await send_discord(
            "✅ **Cikal School Notification Bot is active!**\n"
            f"Checking every 5 minutes.\n"
            f"Found {len(items)} existing notification(s) on first run — not re-sent."
        )
        log.info("First run done. Marked %d notifications as seen.", len(all_ids))
        return

    # Find notifications we haven't seen before
    new_items = []
    for item in items:
        nid = notif_id(item)
        if nid not in seen_ids:
            new_items.append(item)
            seen_ids.add(nid)

    if new_items:
        log.info("%d new notification(s) found!", len(new_items))
        for item in new_items:
            text = item.get("text", "(no text)")[:800]
            await send_discord(f"🔔 **Cikal School Notification**\n{text}")
        state.update(
            {
                "seen_ids": list(seen_ids),
                "last_check": datetime.now().isoformat(),
            }
        )
        save_state(state)
    else:
        log.info("No new notifications.")


if __name__ == "__main__":
    asyncio.run(main())
