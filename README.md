# Your Telegram Assistant Bot

Handles notes, reminders, flashcards, and custom recurring alerts you define
yourself — no code changes needed to add a new alert.

## 1. Create the bot in Telegram

1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, give it a name and a username (must end in `bot`, e.g. `khosro_assistant_bot`).
3. BotFather gives you a **token** like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`. Save it — this is your `TELEGRAM_BOT_TOKEN`.

## 2. Run it locally first (recommended, to test)

Requires Python 3.10+.

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="paste-your-token-here"   # Windows: set TELEGRAM_BOT_TOKEN=...
python bot.py
```

Then open Telegram, find your bot by its username, and send `/start`.
Try `/help` to see all commands.

If this works, you're ready to host it somewhere that stays online 24/7.

## 3. Hosting so it runs even when your computer is off

Your laptop going to sleep kills the bot (it's just a running Python
process). Two easy free-tier options:

### Option A — Railway (simplest, recommended)

1. Push this folder to a GitHub repo (create one, `git init`, `git add .`, `git commit`, push).
2. Go to https://railway.app, sign in with GitHub, click **New Project → Deploy from GitHub repo**, pick your repo.
3. In the project's **Variables** tab, add `TELEGRAM_BOT_TOKEN` with your token.
4. Railway auto-detects Python and runs `python bot.py`. If it doesn't, add a `Procfile` with:
   ```
   worker: python bot.py
   ```
5. Deploy. Check the logs tab for "Bot starting..." — that means it's live.

Free tier covers a small always-on bot like this comfortably for personal use.

### Option B — Render

1. Same GitHub repo as above.
2. On https://render.com, **New → Background Worker**, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add `TELEGRAM_BOT_TOKEN` under Environment.
6. Deploy.

(Use "Background Worker", not "Web Service" — this bot doesn't serve HTTP,
it just polls Telegram.)

## 4. Notes on how it works

- **Storage**: a local SQLite file (`bot.db`) created automatically next to
  `bot.py`. On Railway/Render this file persists between restarts as long as
  you don't redeploy from scratch on some free tiers that wipe disk — if you
  notice reminders disappearing after a redeploy, that's why, and you'd want
  to move to a small persistent volume (Railway offers this) or a hosted DB.
- **Timezone**: reminders and triggers use the server's local time. If your
  host runs in UTC and you're in Nizhny Novgorod (UTC+3), add 3 hours when
  setting times, or set the `TZ` environment variable to
  `Europe/Moscow` on your host so times match yours exactly.
- **Adding real features later**: the `custom triggers` system
  (`/addtrigger`) is deliberately generic — it just sends you a message on a
  schedule. If you want a trigger to *do* something more (e.g. fetch a price,
  check a website, call another API), tell me what specifically and I'll
  extend `fire_trigger()` to handle it.

## 5. Command reference

**Notes**
- `/note <text>` — save a note
- `/notes` — list recent notes
- `/findnote <term>` — search notes
- `/delnote <id>` — delete a note

**Reminders**
- `/remind 30m <text>` or `/remind 2h <text>` or `/remind 14:30 <text>`
- `/reminders` — list pending
- `/delremind <id>` — cancel one

**Flashcards**
- `/addcard <front> | <back>`
- `/quiz` — random question
- `/answer` — reveal answer

**Triggers (recurring alerts)**
- `/addtrigger daily 08:00 <message>`
- `/addtrigger every 45 <message>` (every 45 minutes)
- `/triggers` — list yours
- `/deltrigger <id>` — remove one
