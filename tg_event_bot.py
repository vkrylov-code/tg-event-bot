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

if not TOKEN or not DATABASE_URL or not ADMIN_ID:
    logger.error("Отсутствуют обязательные переменные окружения BOT_TOKEN, DATABASE_URL или ADMIN_ID.")
    raise SystemExit("Запуск невозможен.")

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
        ]
    ])

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

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Создай событие командой:\n"
        "/new_event Текст события\n\n"
        "В тексте можно использовать переносы строк. Пример:\n"
        "/new_event Первая строка\\nВторая строка"
    )

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    text = raw
    if raw.startswith("/new_event"):
        text = raw[len("/new_event"):].strip()
    if not text:
        text = "Событие (без названия)"

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
    logger.info("Создано событие id=%s by %s: %s", event_id, update.effective_user.full_name, text)

    await update.message.reply_text(
        format_event(event_id),
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )

async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return
    load_events()
    if not events:
        await update.message.reply_text("Событий нет.")
        return
    msg = "<b>Список событий:</b>\n"
    for eid, ev in events.items():
        msg += f"ID: <code>{eid}</code> — {html.escape(ev['text'])}\n"
    msg += "\nЧтобы удалить событие, используй команду:\n/delete_event <ID>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def delete_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Укажи ID события для удаления: /delete_event <ID>")
        return
    event_id = args[0]
    if event_id not in events:
        await update.message.reply_text(f"Событие с ID {event_id} не найдено.")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("DELETE FROM events WHERE event_id = %s", (event_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Ошибка удаления события %s: %s", event_id, e)
        await update.message.reply_text(f"Ошибка удаления события: {e}")
        return

    events.pop(event_id, None)
    await update.message.reply_text(f"Событие {event_id} успешно удалено.")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        event_id, action = query.data.split("|", 1)
    except ValueError:
        return
    if event_id not in events:
        await query.edit_message_text("Событие больше не существует.")
        return
    event = events[event_id]
    uid = query.from_user.id
    name = query.from_user.full_name
    if action == "Закрыть сбор":
        event["closed"] = True
    elif action in ["Я буду", "Я не иду", "Думаю"]:
        for k in event["lists"]:
            event["lists"][k].discard(uid)
        event["lists"][action].add(uid)
        event["user_names"][uid] = name
    elif action in ["Плюс", "Минус"]:
        if uid in event["lists"]["Я буду"]:
            event["plus_counts"][uid] = event["plus_counts"].get(uid, 0) + (1 if action == "Плюс" else -1)
        else:
            event["plus_counts"]["anon"] = event["plus_counts"].get("anon", 0) + (1 if action == "Плюс" else -1)
    save_event(event_id, event)
    try:
        await query.edit_message_text(
            format_event(event_id),
            parse_mode="HTML",
            reply_markup=get_keyboard(event_id)
        )
    except BadRequest:
        pass

# --- Автоочистка старых и закрытых событий ---
async def cleanup_events(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    to_delete = []
    for eid, ev in events.items():
        created_at = datetime.fromisoformat(ev["created_at"])
        if ev.get("closed") or (now - created_at > timedelta(days=30)):
            to_delete.append(eid)
    if not to_delete:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        for eid in to_delete:
            cur.execute("DELETE FROM events WHERE event_id = %s", (eid,))
            events.pop(eid, None)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Автоочистка: удалено событий %d", len(to_delete))
    except Exception as e:
        logger.exception("Ошибка автоочистки: %s", e)

# --- Main ---
def main():
    load_events()
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_event", new_event))
    application.add_handler(CommandHandler("events", show_events))
    application.add_handler(CommandHandler("delete_event", delete_event))
    application.add_handler(CallbackQueryHandler(button_click))
    
    # Автоочистка каждые 6 часов
    application.job_queue.run_repeating(cleanup_events, interval=6*3600, first=10)

    application.run_polling()

if __name__ == "__main__":
    main()
