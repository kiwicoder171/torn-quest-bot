# TornQuestBot — Team Quest Market Helper

A 100% Torn-rule-compliant Discord bot that helps your TFC team snag **Daily Quest** items on the Torn Item Market whenever they are listed at or below **TFC's buyout price (`fc_value`)**.

## Key Features
- **Per-User Rate-Limit Protection**: Each registered API key is capped by default at **35 req/min** (configurable `5–50 req/min`, hard-capped at 50) with smooth metronome spacing and automatic HTTP 429 / Error 5 throttling — leaving 50–65+ req/min headroom under Torn's 100 req/min account limit.
- **Multi-User Key Pool & Instant Pause/Resume**:
  - Teammates register keys privately via `/setkey` (ephemeral).
  - Anyone can pause their own key anytime with `/stopkey` or re-enable it with `/startkey`.
  - When multiple keys are active, the bot load-balances requests across them.
- **Shared Team Notifications**: When any active watcher finds a listing `<= fc_value`, it pings all subscribed teammates (`/notify on|off`) in the team channel with a 1-click Torn Item Market link.

## Setup
```bash
cd TornQuestBot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add your DISCORD_BOT_TOKEN to .env
python3 bot.py
```

## Slash Commands
| Command | Description |
| :--- | :--- |
| `/setkey <api_key> [max_rpm]` | Privately register & activate your Torn API key (default 35 req/min). |
| `/stopkey` | Immediately stop the bot from using your API key. |
| `/startkey` | Re-activate your stored API key. |
| `/removekey` | Delete your API key from the bot. |
| `/quest add <item> [max_overpay_pct]` | Add a daily quest item (with TFC item autocomplete). |
| `/quest remove <item>` | Remove an item from today's quest list. |
| `/quest clear` | Clear all daily quest items. |
| `/quest list` | View active quest items & TFC target prices. |
| `/watch start` | Start watching in the current channel using active pool keys. |
| `/watch stop` | Pause the market watcher. |
| `/watch status` | Inspect watcher status, active/paused keys, and RPM usage. |
| `/notify <True\|False>` | Opt in or out of `@mention` pings on deal alerts. |
