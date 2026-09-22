"""
Personal assistant / notification / custom-trigger Telegram bot.

Features
--------
1. Reminders   — one-off "remind me in X" or "remind me at HH:MM" messages.
2. Notes       — quick save + full-text search ("quick lookups").
3. Flashcards  — add front/back cards, quiz yourself (spaced-repetition-lite).
4. Triggers    — user-defined recurring alerts: "every day at 08:00 send me X",
                 or "every 30 minutes send me X". This is the generic
                 notification/workflow piece — you define what fires and when,
                 no code changes needed.

Storage: a single SQLite file (bot.db) next to this script. No external DB
needed, so hosting is simple.

Run:
    export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
    python bot.py
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            fire_at TEXT NOT NULL,
            fired INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            front TEXT NOT NULL,
            back TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            schedule_kind TEXT NOT NULL,   -- 'cron' or 'interval'
            schedule_value TEXT NOT NULL,  -- 'HH:MM' for cron, minutes for interval
            message TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------

async def note_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /note <text to save>")
        return
    conn = db()
    conn.execute(
        "INSERT INTO notes (chat_id, text, created_at) VALUES (?, ?, ?)",
        (update.effective_chat.id, text, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("Saved.")


async def note_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT id, text FROM notes WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No notes yet.")
        return
    lines = [f"#{r['id']}: {r['text']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def note_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /findnote <search term>")
        return
    conn = db()
    rows = conn.execute(
        "SELECT id, text FROM notes WHERE chat_id=? AND text LIKE ? ORDER BY id DESC",
        (update.effective_chat.id, f"%{query}%"),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No matches.")
        return
    lines = [f"#{r['id']}: {r['text']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def note_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delnote <id>")
        return
    conn = db()
    conn.execute(
        "DELETE FROM notes WHERE id=? AND chat_id=?",
        (context.args[0], update.effective_chat.id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("Deleted (if it existed).")


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------

TIME_RE = re.compile(r"^(\d+)(m|min|h|hr|hour|d|day)s?$", re.IGNORECASE)


def parse_relative(spec: str) -> datetime | None:
    m = TIME_RE.match(spec.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("m"):
        return datetime.now() + timedelta(minutes=n)
    if unit.startswith("h"):
        return datetime.now() + timedelta(hours=n)
    if unit.startswith("d"):
        return datetime.now() + timedelta(days=n)
    return None


async def remind_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remind 30m Check the autoclave  OR  /remind 14:30 Call the lab"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n/remind 30m <text>   (in 30 minutes)\n"
            "/remind 2h <text>    (in 2 hours)\n"
            "/remind 14:30 <text> (today/tomorrow at 14:30)"
        )
        return

    spec = context.args[0]
    text = " ".join(context.args[1:])

    fire_at = parse_relative(spec)
    if fire_at is None:
        # try HH:MM today/tomorrow
        try:
            hh, mm = map(int, spec.split(":"))
            candidate = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
            if candidate <= datetime.now():
                candidate += timedelta(days=1)
            fire_at = candidate
        except Exception:
            await update.message.reply_text("Couldn't parse the time. Use e.g. 30m, 2h, or 14:30.")
            return

    conn = db()
    cur = conn.execute(
        "INSERT INTO reminders (chat_id, text, fire_at, fired) VALUES (?, ?, ?, 0)",
        (update.effective_chat.id, text, fire_at.isoformat()),
    )
    conn.commit()
    reminder_id = cur.lastrowid
    conn.close()

    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    scheduler.add_job(
        send_reminder,
        "date",
        run_date=fire_at,
        args=[context.application, update.effective_chat.id, reminder_id, text],
        id=f"reminder-{reminder_id}",
    )

    await update.message.reply_text(f"Okay — I'll remind you at {fire_at.strftime('%Y-%m-%d %H:%M')}.")


async def send_reminder(application, chat_id, reminder_id, text):
    conn = db()
    conn.execute("UPDATE reminders SET fired=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()
    await application.bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {text}")


async def remind_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT id, text, fire_at FROM reminders WHERE chat_id=? AND fired=0 ORDER BY fire_at",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No pending reminders.")
        return
    lines = [f"#{r['id']} at {r['fire_at'][:16].replace('T',' ')}: {r['text']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def remind_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delremind <id>")
        return
    reminder_id = context.args[0]
    conn = db()
    conn.execute(
        "DELETE FROM reminders WHERE id=? AND chat_id=?",
        (reminder_id, update.effective_chat.id),
    )
    conn.commit()
    conn.close()
    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    try:
        scheduler.remove_job(f"reminder-{reminder_id}")
    except Exception:
        pass
    await update.message.reply_text("Deleted (if it existed).")


# --------------------------------------------------------------------------
# Flashcards
# --------------------------------------------------------------------------

async def card_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addcard front text | back text"""
    raw = " ".join(context.args)
    if "|" not in raw:
        await update.message.reply_text("Usage: /addcard <front> | <back>")
        return
    front, back = [p.strip() for p in raw.split("|", 1)]
    conn = db()
    conn.execute(
        "INSERT INTO flashcards (chat_id, front, back) VALUES (?, ?, ?)",
        (update.effective_chat.id, front, back),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("Card added.")


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random
    conn = db()
    rows = conn.execute(
        "SELECT front, back FROM flashcards WHERE chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No flashcards yet. Add one with /addcard front | back")
        return
    card = random.choice(rows)
    context.chat_data["quiz_answer"] = card["back"]
    await update.message.reply_text(f"❓ {card['front']}\n\n(Send /answer to reveal)")


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = context.chat_data.get("quiz_answer")
    if not ans:
        await update.message.reply_text("No active question. Send /quiz first.")
        return
    await update.message.reply_text(f"✅ {ans}")


# --------------------------------------------------------------------------
# Custom triggers (recurring, user-defined)
# --------------------------------------------------------------------------

async def trigger_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addtrigger daily 08:00 Time to review flashcards
    /addtrigger every 45 Drink water
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage:\n"
            "/addtrigger daily HH:MM <message>   (fires every day at that time)\n"
            "/addtrigger every N <message>       (fires every N minutes)"
        )
        return

    kind = context.args[0].lower()
    chat_id = update.effective_chat.id

    if kind == "daily":
        time_str = context.args[1]
        try:
            hh, mm = map(int, time_str.split(":"))
        except Exception:
            await update.message.reply_text("Time must be HH:MM, e.g. 08:00")
            return
        message = " ".join(context.args[2:])
        name = f"daily-{time_str}-{message[:20]}"
        conn = db()
        cur = conn.execute(
            "INSERT INTO triggers (chat_id, name, schedule_kind, schedule_value, message) VALUES (?,?,?,?,?)",
            (chat_id, name, "cron", time_str, message),
        )
        conn.commit()
        trig_id = cur.lastrowid
        conn.close()

        scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
        scheduler.add_job(
            fire_trigger,
            CronTrigger(hour=hh, minute=mm),
            args=[context.application, chat_id, message],
            id=f"trigger-{trig_id}",
        )
        await update.message.reply_text(f"Set. Trigger #{trig_id} fires daily at {time_str}.")

    elif kind == "every":
        try:
            minutes = int(context.args[1])
        except Exception:
            await update.message.reply_text("N must be a number of minutes, e.g. /addtrigger every 45 <msg>")
            return
        message = " ".join(context.args[2:])
        name = f"every-{minutes}m-{message[:20]}"
        conn = db()
        cur = conn.execute(
            "INSERT INTO triggers (chat_id, name, schedule_kind, schedule_value, message) VALUES (?,?,?,?,?)",
            (chat_id, name, "interval", str(minutes), message),
        )
        conn.commit()
        trig_id = cur.lastrowid
        conn.close()

        scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
        scheduler.add_job(
            fire_trigger,
            IntervalTrigger(minutes=minutes),
            args=[context.application, chat_id, message],
            id=f"trigger-{trig_id}",
        )
        await update.message.reply_text(f"Set. Trigger #{trig_id} fires every {minutes} minutes.")

    else:
        await update.message.reply_text("First word must be 'daily' or 'every'.")


async def fire_trigger(application, chat_id, message):
    await application.bot.send_message(chat_id=chat_id, text=f"🔔 {message}")


async def trigger_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT id, schedule_kind, schedule_value, message FROM triggers WHERE chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No triggers set.")
        return
    lines = []
    for r in rows:
        if r["schedule_kind"] == "cron":
            lines.append(f"#{r['id']} daily @ {r['schedule_value']}: {r['message']}")
        else:
            lines.append(f"#{r['id']} every {r['schedule_value']} min: {r['message']}")
    await update.message.reply_text("\n".join(lines))


async def trigger_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /deltrigger <id>")
        return
    trig_id = context.args[0]
    conn = db()
    conn.execute(
        "DELETE FROM triggers WHERE id=? AND chat_id=?",
        (trig_id, update.effective_chat.id),
    )
    conn.commit()
    conn.close()
    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    try:
        scheduler.remove_job(f"trigger-{trig_id}")
    except Exception:
        pass
    await update.message.reply_text("Deleted (if it existed).")


# --------------------------------------------------------------------------
# Startup: reload scheduled jobs from DB (survives restarts)
# --------------------------------------------------------------------------

def restore_jobs(application, scheduler: AsyncIOScheduler):
    conn = db()

    for r in conn.execute("SELECT * FROM reminders WHERE fired=0"):
        fire_at = datetime.fromisoformat(r["fire_at"])
        if fire_at <= datetime.now():
            continue  # missed while offline; skip silently
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=fire_at,
            args=[application, r["chat_id"], r["id"], r["text"]],
            id=f"reminder-{r['id']}",
        )

    for r in conn.execute("SELECT * FROM triggers"):
        if r["schedule_kind"] == "cron":
            hh, mm = map(int, r["schedule_value"].split(":"))
            scheduler.add_job(
                fire_trigger,
                CronTrigger(hour=hh, minute=mm),
                args=[application, r["chat_id"], r["message"]],
                id=f"trigger-{r['id']}",
            )
        else:
            scheduler.add_job(
                fire_trigger,
                IntervalTrigger(minutes=int(r["schedule_value"])),
                args=[application, r["chat_id"], r["message"]],
                id=f"trigger-{r['id']}",
            )

    conn.close()


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your assistant bot. Send /help to see what I can do."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "NOTES\n"
        "/note <text> — save a note\n"
        "/notes — list recent notes\n"
        "/findnote <term> — search notes\n"
        "/delnote <id> — delete a note\n\n"
        "REMINDERS\n"
        "/remind 30m <text> | /remind 2h <text> | /remind 14:30 <text>\n"
        "/reminders — list pending\n"
        "/delremind <id> — cancel one\n\n"
        "FLASHCARDS\n"
        "/addcard <front> | <back>\n"
        "/quiz — random question\n"
        "/answer — reveal answer\n\n"
        "TRIGGERS (recurring alerts you define)\n"
        "/addtrigger daily HH:MM <message>\n"
        "/addtrigger every N <message>  (N = minutes)\n"
        "/triggers — list yours\n"
        "/deltrigger <id> — remove one"
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the TELEGRAM_BOT_TOKEN environment variable first.")

    init_db()

    application = ApplicationBuilder().token(token).build()

    scheduler = AsyncIOScheduler()
    application.bot_data["scheduler"] = scheduler

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))

    application.add_handler(CommandHandler("note", note_add))
    application.add_handler(CommandHandler("notes", note_list))
    application.add_handler(CommandHandler("findnote", note_find))
    application.add_handler(CommandHandler("delnote", note_delete))

    application.add_handler(CommandHandler("remind", remind_add))
    application.add_handler(CommandHandler("reminders", remind_list))
    application.add_handler(CommandHandler("delremind", remind_delete))

    application.add_handler(CommandHandler("addcard", card_add))
    application.add_handler(CommandHandler("quiz", quiz))
    application.add_handler(CommandHandler("answer", answer))

    application.add_handler(CommandHandler("addtrigger", trigger_add))
    application.add_handler(CommandHandler("triggers", trigger_list))
    application.add_handler(CommandHandler("deltrigger", trigger_delete))

    restore_jobs(application, scheduler)
    scheduler.start()

    log.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
