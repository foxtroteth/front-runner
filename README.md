# Cikal School Notification Bot

Monitors the Cikal community portal for new notifications and sends them to Discord.

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

### 3. Add secrets to GitHub

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these three secrets:

| Name | Value |
|------|-------|
| `CIKAL_USERNAME` | Your Cikal login email |
| `CIKAL_PASSWORD` | Your Cikal login password |
| `DISCORD_WEBHOOK_URL` | The webhook URL from step 1 |

### 4. Enable GitHub Actions

Go to your repo → **Actions** tab → click **"I understand my workflows, go ahead and enable them"**

### 5. Trigger setup (Cron.org + optional manual test)

- This workflow is triggered via **workflow_dispatch**.
- **Cron.org** calls this workflow on your chosen schedule (for example, every 5 minutes).
- You can still run it manually from **Actions** → **Check Cikal Notifications** → **Run workflow** to verify quickly.

You should get a Discord message: "✅ Cikal School Notification Bot is active!"

---

## How it works

- Cron.org triggers the GitHub Actions workflow on your configured schedule
- The script logs in with Playwright (headless Chrome), loads the notifications page, and checks for new items
- New notifications are sent to Discord immediately
- `state.json` tracks which notifications have already been sent (committed back to the repo)

## Debugging

Run the workflow manually with **debug = true** to get screenshots of what the browser sees at each step. Screenshots are saved as workflow artifacts.

## Local testing

```bash
cp .env.example .env
# fill in .env with your real credentials

pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium

DEBUG=true python check_notifications.py
```
