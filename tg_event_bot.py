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

# --- Загружаем .env ---
load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://yourdomain.com

if not TOKEN or not DATABASE_URL or not WEBHOOK_URL:
    raise SystemExit("BOT_TOKEN, DATABASE_URL, WEBHOOK_URL required")

# --- Логирование ---
logging.basicConfig(filename="bot.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Хранилище событий ---
events = {}

# --- Клавиатуры и форматирование ---
def get_keyboard(event_id, show_delete=False):
    event = events.get(event_id)
    if not event:
        return None
    if event.get("closed"):
        buttons = []
        if show_delete:
            buttons.append([InlineKeyboardButton("🗑 Удалить событие", callback_data=f"{event_id}|Удалить")])
        return InlineKeyboardMarkup(buttons) if buttons else None

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
    if show_delete:
        buttons.append([InlineKeyboardButton("🗑 Удалить событие", callback_data=f"{event_id}|Удалить")])
    return InlineKeyboardMarkup(buttons)

def format_user_link(user_id: int, name: str) -> str:
    safe = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

def format_event(event_id: str) -> str:
    event = events[event_id]
    title = html.escape(event["text"])
    parts = [f"<b>{title}</b>\n"]
    lists = event["lists"]
    plus_counts = event["plus_counts"]
    user_names = event["user_names"]

    lines = [format_user_link(uid, user_names.get(uid, "User")) +
             (f" +{plus_counts.get(uid,0)}" if plus_counts.get(uid,0)>0 else "")
             for uid in sorted(lists["Я буду"], key=lambda x: user_names.get(x,""))]
    anon_count = plus_counts.get("anon",0)
    if anon_count>0:
        lines.append(f"— +{anon_count}")
    parts.append("<b>✅ Я буду:</b>\n" + ("\n".join(lines) if lines else "—"))

    lines_no = [format_user_link(uid, user_names.get(uid,"User")) for uid in sorted(lists["Я не иду"], key=lambda x: user_names.get(x,""))]
    parts.append("\n<b>❌ Я не иду:</b>\n" + ("\n".join(lines_no) if lines_no else "—"))

    lines_think = [format_user_link(uid, user_names.get(uid,"User")) for uid in sorted(lists["Думаю"], key=lambda x: user_names.get(x,""))]
    parts.append("\n<b>🤔 Думаю:</b>\n" + ("\n".join(lines_think) if lines_think else "—"))

    total_yes = len(lists["Я буду"]) + sum(plus_counts.get(uid,0) for uid in lists["Я буду"]) + plus_counts.get("anon",0)
    total_no = len(lists["Я не иду"])
    total_think = len(lists["Думаю"])
    parts.append("\n-----------------")
    parts.append(f"Всего идут: {total_yes}")
    parts.append(f"✅ {total_yes}")
    parts.append(f"❌ {total_no}")
    parts.append(f"🤔 {total_think}")

    if event.get("closed"):
        parts.append("\n⚠️ Сбор закрыт.")
    return "\n".join(parts)

# --- Работа с БД ---
def save_event(event_id, event):
    event_copy = {**event, "lists": {k: list(v) for k,v in event["lists"].items()}}
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                data JSONB
            )
        """)
        cur.execute("""
            INSERT INTO events(event_id,data)
            VALUES(%s,%s)
            ON CONFLICT(event_id) DO UPDATE SET data=EXCLUDED.data
        """,(event_id,Json(event_copy)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Ошибка сохранения события %s: %s", event_id, e)

def load_events():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor(cursor_factory=RealDictCursor)
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
    await update.message.reply_text("👋 Привет!\nСоздать событие: /new_event Текст события")

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) or "Событие (без названия)"
    event_id = uuid4().hex
    events[event_id] = {
        "text": text,
        "lists":{"Я буду":set(),"Я не иду":set(),"Думаю":set()},
        "plus_counts":{},
        "user_names":{},
        "closed":False,
        "created_at":datetime.utcnow().isoformat()
    }
    save_event(event_id, events[event_id])
    await update.message.reply_text(format_event(event_id), parse_mode="HTML",
                                    reply_markup=get_keyboard(event_id))

async def list_events_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not events:
        await update.message.reply_text("Событий пока нет.")
        return
    messages = [format_event(eid) for eid in events]
    await update.message.reply_text("\n\n".join(messages), parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    event_id, action = query.data.split("|",1)
    if event_id not in events:
        await query.edit_message_text("Событие больше недоступно.")
        return
    event = events[event_id]
    changed=False
    user = query.from_user
    if action in ["Я буду","Я не иду","Думаю"]:
        for lst in event["lists"].values():
            lst.discard(user.id)
        event["lists"][action].add(user.id)
        event["user_names"][user.id] = user.full_name
        if action!="Я буду" and user.id in event["plus_counts"]:
            del event["plus_counts"][user.id]
        changed=True
    elif action=="Плюс":
        if user.id in event["lists"]["Я буду"]:
            event["plus_counts"][user.id] = event["plus_counts"].get(user.id,0)+1
        else:
            event["plus_counts"]["anon"] = event["plus_counts"].get("anon",0)+1
        changed=True
    elif action=="Минус":
        if user.id in event["lists"]["Я буду"] and event["plus_counts"].get(user.id,0)>0:
            event["plus_counts"][user.id]-=1
            changed=True
        elif event["plus_counts"].get("anon",0)>0:
            event["plus_counts"]["anon"]-=1
            changed=True
    elif action=="Закрыть сбор":
        event["closed"]=True
        changed=True
    elif action=="Удалить" and user.id==ADMIN_ID:
        events.pop(event_id, None)
        await query.edit_message_text("Событие удалено администратором.")
        return
    if changed:
        save_event(event_id,event)
        try:
            await query.edit_message_text(format_event(event_id),
                                          parse_mode="HTML",
                                          reply_markup=get_keyboard(event_id))
        except BadRequest:
            pass

# --- Flask и Telegram Application ---
app = Flask(__name__)
telegram_app = Application.builder().token(TOKEN).build()

# --- Инициализация Telegram App ---
import asyncio
async def init_app():
    await telegram_app.initialize()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("new_event", new_event))
    telegram_app.add_handler(CommandHandler("list_events", list_events_handler))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    load_events()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")

asyncio.run(init_app())

# --- Webhook route ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        if request.headers.get("content-type") == "application/json":
            update = Update.de_json(request.get_json(force=True), telegram_app.bot)
            telegram_app.update_queue.put_nowait(update)
            logger.info("✅ Update forwarded to Telegram app queue")
            return "ok"
        return "Unsupported Media Type", 415
    except Exception as e:
        logger.exception("💥 Error in webhook: %s", e)
        return "Internal Server Error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, threaded=True)
