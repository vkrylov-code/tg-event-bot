import os
import html
import logging
from uuid import uuid4
from datetime import datetime
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from flask import Flask, request

# --- Настройки ---
load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://yourdomain.com

if not TOKEN or not DATABASE_URL or not WEBHOOK_URL:
    raise SystemExit("BOT_TOKEN, DATABASE_URL, WEBHOOK_URL required")

# --- Логирование ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Хранилище событий ---
events = {}

# --- Функции для отображения и клавиатур ---
def get_keyboard(event_id, show_delete=False):
    event = events.get(event_id)
    if not event:
        return None
    buttons = []
    if not event.get("closed"):
        buttons = [
            [
                InlineKeyboardButton("✅ Я буду", callback_data=f"{event_id}|Я буду"),
                InlineKeyboardButton("❌ Я не иду", callback_data=f"{event_id}|Я не иду"),
                InlineKeyboardButton("🤔 Думаю", callback_data=f"{event_id}|Думаю"),
            ],
            [
                InlineKeyboardButton("➕ Плюс", callback_data=f"{event_id}|Плюс"),
                InlineKeyboardButton("➖ Минус", callback_data=f"{event_id}|Минус"),
                InlineKeyboardButton("🚫 Закрыть сбор", callback_data=f"{event_id}|Закрыть сбор"),
            ]
        ]
    if show_delete or event.get("closed"):
        buttons.append([InlineKeyboardButton("🗑 Удалить событие", callback_data=f"{event_id}|Удалить")])
    return InlineKeyboardMarkup(buttons) if buttons else None

def format_user_link(user_id: int, name: str) -> str:
    safe = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

def format_event(event_id: str) -> str:
    event = events[event_id]
    title = html.escape(event["text"])
    lists = event["lists"]
    plus_counts = event["plus_counts"]
    user_names = event["user_names"]

    lines_yes = [format_user_link(uid, user_names.get(uid, "User")) + 
                 (f" +{plus_counts.get(uid,0)}" if plus_counts.get(uid,0) > 0 else "")
                 for uid in sorted(lists["Я буду"], key=lambda x: user_names.get(x,""))]
    anon_count = plus_counts.get("anon",0)
    if anon_count > 0:
        lines_yes.append(f"— +{anon_count}")

    lines_no = [format_user_link(uid, user_names.get(uid,"User")) for uid in sorted(lists["Я не иду"], key=lambda x: user_names.get(x,""))]
    lines_think = [format_user_link(uid, user_names.get(uid,"User")) for uid in sorted(lists["Думаю"], key=lambda x: user_names.get(x,""))]

    parts = [
        f"<b>{title}</b>\n",
        "<b>✅ Я буду:</b>\n" + ("\n".join(lines_yes) if lines_yes else "—"),
        "<b>❌ Я не иду:</b>\n" + ("\n".join(lines_no) if lines_no else "—"),
        "<b>🤔 Думаю:</b>\n" + ("\n".join(lines_think) if lines_think else "—"),
        "-----------------",
        f"Всего идут: {len(lists['Я буду']) + anon_count}",
        f"✅ {len(lists['Я буду']) + anon_count}",
        f"❌ {len(lists['Я не иду'])}",
        f"🤔 {len(lists['Думаю'])}"
    ]
    if event.get("closed"):
        parts.append("\n⚠️ Сбор закрыт.")
    return "\n".join(parts)

# --- Работа с БД ---
def save_event(event_id, event):
    event_copy = {**event, "lists": {k: list(v) for k,v in event["lists"].items()}}
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, data JSONB)")
        cur.execute("INSERT INTO events(event_id,data) VALUES(%s,%s) ON CONFLICT(event_id) DO UPDATE SET data=EXCLUDED.data",
                    (event_id, Json(event_copy)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Ошибка сохранения события %s: %s", event_id, e)

def load_events():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, data JSONB)")
        cur.execute("SELECT * FROM events")
        for row in cur.fetchall():
            data = row["data"]
            data["lists"] = {k:set(v) for k,v in data["lists"].items()}
            events[row["event_id"]] = data
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Ошибка загрузки событий: %s", e)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Создать событие: /new_event Текст события")

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) or "Событие (без названия)"
    event_id = uuid4().hex
    events[event_id] = {"text": text, "lists":{"Я буду":set(),"Я не иду":set(),"Думаю":set()}, "plus_counts":{}, "user_names":{}, "closed":False, "created_at": datetime.utcnow().isoformat()}
    save_event(event_id, events[event_id])
    await update.message.reply_text(format_event(event_id), parse_mode="HTML", reply_markup=get_keyboard(event_id))

async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not events:
        await update.message.reply_text("Событий пока нет.")
        return
    messages = [format_event(event_id) for event_id in events]
    await update.message.reply_text("\n\n".join(messages), parse_mode="HTML")

# --- Flask сервер ---
app = Flask(__name__)
load_events()

telegram_app = Application.builder().token(TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("new_event", new_event))
telegram_app.add_handler(CommandHandler("list_events", list_events))
# сюда можно добавить CallbackQueryHandler, если нужно

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "ok"

# --- Асинхронная задача для запуска Application ---
import asyncio
import threading

def run_telegram_app():
    async def main():
        await telegram_app.initialize()
        await telegram_app.start()
        logger.info("✅ Telegram app started")
        # Остается работать в фоне
    asyncio.run(main())

threading.Thread(target=run_telegram_app, daemon=True).start()

# --- Запуск Flask ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, threaded=True)
