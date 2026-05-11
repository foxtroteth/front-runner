# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python bot (`check_notifications.py`) that monitors the Cikal school community portal for new notifications and sends them to an iPhone via ntfy (primary) and Discord (backup). It is triggered externally by Cron.org calling a GitHub Actions `workflow_dispatch` webhook on a schedule.

## Local development

```bash
cp .env.example .env
# fill in .env with real credentials

pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium

DEBUG=true python check_notifications.py
```

`DEBUG=true` saves screenshots (`debug_*.png`) at each browser step — these are gitignored and also uploaded as workflow artifacts in CI.

## Architecture

The entire application lives in `check_notifications.py`. The execution flow is:

1. **Schedule gate** (`is_in_schedule`): Exits immediately if called outside Mon–Fri 06:30–09:00 or 12:00–17:00 Jakarta time (GMT+7). This is the first check in `main()`.

2. **State management** (`load_state` / `save_state`): `state.json` is the persistence layer, committed back to the repo by GitHub Actions after each run. It holds:
   - `seen_ids`: MD5 hashes of already-sent notification IDs (capped at 200)
   - `cookies`: saved Playwright session cookies to avoid re-logging in every run
   - `first_run`: flag that silently marks all existing items as seen on the very first execution
   - `done_for_date`: ISO date string set when the end-of-day pickup signal is detected; skips all further checks that day
   - `consecutive_failures`: counter that triggers a Discord alert after 10 scraping failures in a row

3. **Scraping** (`scrape`): Launches a headless Chromium browser via Playwright. Session cookies from `state.json` are injected first; if the portal redirects to login, `_do_login` fills the form and saves fresh cookies back to state. The bot visits two pages: `EVENT_URL` and `NOTIF_URL`. A `response` listener intercepts all XHR/fetch calls whose URLs contain keywords from `API_URL_KEYWORDS`, parses them as JSON, and collects them in `captured_api`.

4. **Extraction** (`extract_from_api`): Walks the captured API responses looking for list-shaped data under common keys (`data`, `notifications`, `items`, `results`, `list`, `events`). Event-URL responses are sorted first. Each item is deduplicated by `source_url:id`.

5. **Notification routing** (`notify`): Sends to ntfy and Discord concurrently via `asyncio.gather(..., return_exceptions=True)` — both channels are always attempted and failures are logged but not fatal.

6. **End-of-day pickup detection** (`is_pickup_arrival`): If any unseen item contains "shuttle bus - release" or ("handed over" + "teraskota"), the bot sets `done_for_date` and pauses for the rest of the day.

## Required secrets (GitHub Actions)

| Secret | Purpose |
|---|---|
| `CIKAL_USERNAME` | Portal login email |
| `CIKAL_PASSWORD` | Portal login password |
| `DISCORD_WEBHOOK_URL` | Discord backup channel webhook |
| `NTFY_TOPIC` | ntfy topic name for iPhone push |

`NTFY_URL` defaults to `https://ntfy.sh`; override via env var for self-hosted instances.

## GitHub Actions workflow

`.github/workflows/check.yml` — triggered only via `workflow_dispatch` (called by Cron.org). After the script runs, it commits any `state.json` changes back to `main` with `[skip ci]` to avoid loops. The `concurrency` group ensures overlapping runs queue rather than cancel.
