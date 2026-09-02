# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python bot (`check_notifications.py`) that monitors the Cikal school community portal for new notifications and sends them to an iPhone via ntfy (primary) and Discord (backup). It is triggered externally by Cron.org calling a GitHub Actions `workflow_dispatch` webhook on a schedule.

## Local development

```bash
cp .env.example .env
# fill in .env with real credentials

pip install -r requirements.txt

python check_notifications.py
```

The bot talks to the portal over plain HTTP (`httpx`) — there is no browser to install.

## Architecture

The entire application lives in `check_notifications.py`. The execution flow is:

1. **Schedule gate** (`is_in_schedule`): Exits immediately if called outside Mon–Fri 06:30–09:00 or 11:00–17:00 Jakarta time (GMT+7). This is the first check in `main()`.

2. **State management** (`load_state` / `save_state`): `state.json` is the persistence layer, committed back to the repo by GitHub Actions after each run. It holds:
   - `seen_ids`: MD5 hashes of already-sent notification IDs (capped at 200)
   - `cookies`: saved session cookies (`com_at`/`com_rt`) to avoid re-logging in every run
   - `first_run`: flag that silently marks all existing items as seen on the very first execution
   - `done_for_date`: ISO date string set when the end-of-day pickup signal is detected; skips all further checks that day
   - `consecutive_failures`: counter that triggers a Discord alert after 10 scraping failures in a row
   - `last_alert_at`: timestamp throttling the failure alert to at most one per 3h

3. **Scraping** (`scrape`): Talks to the portal's JSON API directly over plain HTTP (`httpx`) — no browser. **Why no browser:** on 2026-09-02 the portal began blackholing headless-Chromium TLS connections from datacenter IPs (from the same GitHub runner, `curl` got HTTP 200 in ~1s while Playwright Chromium timed out after 30s), but plain HTTP clients still work. Saved `com_at`/`com_rt` cookies from `state.json` are loaded into an `httpx` cookie jar; the `EVENT_API_URL` feed doubles as the session-validity probe. If it doesn't come back feed-shaped (`_looks_like_feed`), `_http_login` reads the JWT CSRF token from `<meta name="csrf-token">` on `/auth` and POSTs credentials to `/auth/login` (JSON body, `X-CSRF-TOKEN` header, one attempt — never re-submitted, to avoid account lockout), persisting fresh cookies. It then fetches `NOTIF_API_URL` and collects both feeds in `captured_api`.

4. **Extraction** (`extract_from_api`): Walks the captured API responses looking for list-shaped data under the keys in `FEED_LIST_KEYS` (`data`, `notifications`, `items`, `results`, `list`, `events`) — the same keys `_looks_like_feed` uses, so session-validity and extraction never disagree. Event-URL responses are sorted first. Each item is deduplicated by `source_url:id`.

5. **Notification routing** (`notify`): Sends to ntfy and Discord concurrently via `asyncio.gather(..., return_exceptions=True)` — both channels are always attempted and failures are logged but not fatal.

6. **End-of-day pickup detection** (`is_pickup_arrival`): If any unseen item contains "shuttle bus - release" or ("handed over" + "teraskota"), the bot sets `done_for_date` and pauses for the rest of the day. Both this and the school-arrival branch mark the *entire* visible feed as seen when they fire — after a backlog surfaces at once, a leftover unseen "arrived at school" item would otherwise fire a stale arrival + cooldown at the next morning's 06:30 run, every day.

7. **School-arrival detection** (`is_school_arrival`): Matches "has arrived at" + "sekolah cikal", non-adjacent. The portal's wording changes between academic years (2025/26: "has arrived at Sekolah Cikal Serpong"; 2026/27: "has arrived at Campus A TK-SD - Sekolah Cikal Serpong") — but the matcher must never fire on the afternoon "has arrived at TerasKota" message, which is not a school arrival. A false positive here starts a 3-hour cooldown that suppresses all notifications, so prefer a miss (which still sends a generic notification) over a loose match.

**Known gotcha — start of a new academic year:** the portal gates the parent's notification feed behind the new "Parents Handbook" agreement. While the agreement is pending, the notification feed returns nothing usable (only the event feed comes back), so the bot goes silent even though shuttle notifications are being generated with their real timestamps. The moment the parent accepts the agreement in the portal/app, the feed reappears and the backlog is sent (observed 2026-07-22: acceptance at 08:17, feed captured again at the 08:20 run). Accept the agreement on day one of each school year.

**Known gotcha — portal bot-protection:** the portal blocks automated *browsers* from datacenter IPs but not plain HTTP clients, which is why the scraper uses `httpx` and not Playwright (see step 3). If notifications stop and the failure alert cites connection timeouts, first check whether the `Portal reachability probe` step in the workflow still shows `curl` reaching the portal — if `curl` works but the script fails, the login flow or an API path likely changed.

## Required secrets (GitHub Actions)

| Secret | Purpose |
|---|---|
| `CIKAL_USERNAME` | Portal login email |
| `CIKAL_PASSWORD` | Portal login password |
| `DISCORD_WEBHOOK_URL` | Discord backup channel webhook |
| `NTFY_TOPIC` | ntfy topic name for iPhone push |

`NTFY_URL` defaults to `https://ntfy.sh`; override via env var for self-hosted instances.

## GitHub Actions workflow

`.github/workflows/check.yml` — triggered only via `workflow_dispatch`, called by Cron.org every 5 minutes during 06:30–09:00 and 11:00–17:00 GMT+7 (the Cron.org schedule lives in the owner's Cron.org account and must be kept in sync with `SCHEDULE_WINDOWS`). After the script runs, it commits any `state.json` changes back to `main` with `[skip ci]` to avoid loops. The `concurrency` group ensures overlapping runs queue rather than cancel.
