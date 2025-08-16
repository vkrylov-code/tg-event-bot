import os
import html
import logging
from uuid import uuid4
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import psycopg2
from psycopg2.extras import RealDictCursor, Json

# --- Логи ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Конфиг из окружения ---
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Хранилище событий в памяти ---
events = {}

# --- Клавиатура ---
def get_keyboard(event_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Я буду", callback_data=f"{event_id}|Я буду"),
            InlineKeyboardButton("❌ Я не иду", callback_data=f"{event_id}|Я не иду"),
            InlineKeyboardButton("🤔 Думаю", callback_data=f"{event_id}|Думаю"),
        ],
        [
            InlineKeyboardButton("➕ Плюс", callback_data=f"{event_id}|Плюс"),
            InlineKeyboardButton("➖ Минус", callback_data=f"{event_id}|Минус"),
            InlineKeyboardButton("🚫 Закрыть сбор", callback_data=f"{event_id}|Закрыть сбор"),
            InlineKeyboardButton("🗑️ Удалить", callback_data=f"{event_id}|Удалить"),
        ]
    ])

def format_user_link(user_id: int, name: str) -> str:
    safe = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

def format_event(event_id: str) -> str:
    event = events[event_id]
    title = html.escape(event["text"])
    parts = [f"<b>{title}</b>\nID: {event_id}\n"]
    lists = event["lists"]
    plus_counts = event["plus_counts"]
    user_names = event["user_names"]

    # ✅ Я буду
    lines = []
    for uid in sorted(lists["Я буду"], key=lambda x: user_names.get(x, "")):
        name = user_names.get(uid, "User")
        link = format_user_link(uid, name)
        cnt = plus_counts.get(uid, 0)
        lines.append(link + (f" +{cnt}" if cnt > 0 else ""))
    parts.append("<b>✅ Я буду:</b>\n" + ("\n".join(lines) if lines else "—"))

    # Анонимные плюсы
    anon_count = plus_counts.get("anon", 0)
    if anon_count > 0:
        parts.append(f"— +{anon_count}")

    # ❌ Я не иду
    lines_no = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Я не иду"], key=lambda x: user_names.get(x, ""))]
    parts.append("<b>❌ Я не иду:</b>\n" + ("\n".join(lines_no) if lines_no else "—"))

    # 🤔 Думаю
    lines_think = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Думаю"], key=lambda x: user_names.get(x, ""))]
    parts.append("<b>🤔 Думаю:</b>\n" + ("\n".join(lines_think) if lines_think else "—"))

    # Итоги
    total_yes_people = len(lists["Я буду"])
    total_plus_count = sum(plus_counts.get(uid, 0) for uid in lists["Я буду"])
    total_anon_plus = plus_counts.get("anon", 0)
    total_go = total_yes_people + total_plus_count + total_anon_plus
    total_no = len(lists["Я не иду"])
    total_think = len(lists["Думаю"])

    parts.append("-----------------")
    parts.append(f"Всего идут: {total_go}")
    parts.append(f"✅ {total_go}")
    parts.append(f"❌ {total_no}")
    parts.append(f"🤔 {total_think}")

    if event["closed"]:
        parts.append("\n⚠️ Сбор закрыт.")

    return "\n".join(parts)

# --- База данных ---
def save_event(event_id, event):
    event_copy = {
        **event,
        "lists": {k: list(v) for k, v in event["lists"].items()},
        "created_at": event.get("created_at", datetime.utcnow().isoformat())
    }
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
            INSERT INTO events (event_id, data)
            VALUES (%s, %s)
            ON CONFLICT (event_id) DO UPDATE SET data = EXCLUDED.data
        """, (event_id, Json(event_copy)))
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
        rows = cur.fetchall()
        for row in rows:
            event_id = row["event_id"]
            data = row["data"]
            data["lists"] = {k: set(v) for k, v in data["lists"].items()}
            events[event_id] = data
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Ошибка загрузки событий: %s", e)

def delete_event(event_id):
    events.pop(event_id, None)
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("DELETE FROM events WHERE event_id = %s", (event_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Ошибка удаления события %s: %s", event_id, e)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! /new_event Текст события")

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else "Событие (без названия)"
    event_id = uuid4().hex
    events[event_id] = {
        "text": text,
        "lists": {"Я буду": set(), "Я не иду": set(), "Думаю": set()},
        "plus_counts": {},
        "user_names": {},
        "closed": False,
        "created_at": datetime.utcnow().isoformat()
    }
    save_event(event_id, events[event_id])
    await update.message.reply_text(format_event(event_id), parse_mode="HTML", reply_markup=get_keyboard(event_id))

async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return
    load_events()
    if not events:
        await update.message.reply_text("Событий нет.")
        return
    msg = ""
    for eid, ev in events.items():
        msg += f"{eid}: {html.escape(ev['text'])}\n"
    await update.message.reply_text(msg)

async def delete_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return
    if not context.args:
        await update.message.reply_text("Укажите ID события для удаления")
        return
    event_id = context.args[0]
    delete_event(event_id)
    await update.message.reply_text(f"Событие {event_id} удалено")

# --- Запуск ---
def main():
    load_events()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_event", new_event))
    application.add_handler(CommandHandler("events", show_events))
    application.add_handler(CommandHandler("delete_event", delete_event_command))
    application.run_polling()

if __name__ == "__main__":
    main()
