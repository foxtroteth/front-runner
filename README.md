# Cikal School Notification Bot

Monitors the Cikal community portal for new notifications and sends them to your iPhone via ntfy, with Discord as a backup.

Triggered by your external scheduler (Cron.org) which calls this GitHub Actions workflow.

---

## Setup (one-time, ~10 minutes)

### 1. Create a Discord Webhook

1. Open Discord → go to the channel where you want notifications
2. Click the gear icon (Edit Channel) → **Integrations** → **Webhooks** → **New Webhook**
3. Give it a name (e.g. "Cikal School") and copy the **Webhook URL**

### 2. Create a GitHub repository

1. Go to [github.com/new](https://github.com/new)
2. Create a **public** repository (public = unlimited free Actions minutes)
3. Push this project to it:

```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 3. Set up ntfy (iPhone push notifications)

1. Install the **ntfy** app on your iPhone (free, App Store)
2. Pick a private topic name — something hard to guess, e.g. `cikal-yourname-abc123`
3. In the app, tap **+** and subscribe to that topic name
4. That's it — notifications will appear like any other app

### 4. Add secrets to GitHub

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these secrets:

| Name | Value |
|------|-------|
| `CIKAL_USERNAME` | Your Cikal login email |
| `CIKAL_PASSWORD` | Your Cikal login password |
| `DISCORD_WEBHOOK_URL` | The Discord webhook URL (backup) |
| `NTFY_TOPIC` | Your ntfy topic name (e.g. `cikal-yourname-abc123`) |

### 5. Enable GitHub Actions

Go to your repo → **Actions** tab → click **"I understand my workflows, go ahead and enable them"**

### 6. Trigger setup (Cron.org + optional manual test)

- This workflow is triggered via **workflow_dispatch**.
- **Cron.org** calls this workflow on your chosen schedule (for example, every 5 minutes).
- You can still run it manually from **Actions** → **Check Cikal Notifications** → **Run workflow** to verify quickly.

You should get an ntfy push notification and a Discord message: "✅ Cikal School Bot is active!"

---

## How it works

- Cron.org triggers the GitHub Actions workflow on your configured schedule
- The script logs in over plain HTTP and reads the portal's notification/event JSON APIs directly (no browser — the portal blocks automated browsers from datacenter IPs)
- New notifications are sent to your iPhone via ntfy, with Discord as a backup
- `state.json` tracks which notifications have already been sent (committed back to the repo)

## Debugging

Each workflow run includes a **Portal reachability probe** step that `curl`s the portal and a control site — check it first if runs start failing. If `curl` reaches the portal but the script still fails, the login flow or an API path likely changed. The failure alert (see below) fires in Discord after 10 consecutive failures.

## Local testing

```bash
cp .env.example .env
# fill in .env with your real credentials

pip install -r requirements.txt

python check_notifications.py
```
