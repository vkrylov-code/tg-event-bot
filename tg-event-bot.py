import os
import html
import logging
from uuid import uuid4
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
ADMIN_ID = int(os.environ.get("ADMIN_ID"))

if not TOKEN:
    logger.error("Не задана переменная окружения BOT_TOKEN. Прекращаю запуск.")
    raise SystemExit("BOT_TOKEN is required")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logger.error("Не задана переменная окружения DATABASE_URL. Прекращаю запуск.")
    raise SystemExit("DATABASE_URL is required")

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
    total_plus = 0
    for uid in sorted(lists["Я буду"], key=lambda x: user_names.get(x, "")):
        name = user_names.get(uid, "User")
        link = format_user_link(uid, name)
        cnt = plus_counts.get(uid, 0)
        total_plus += cnt
        lines.append(link + (f" +{cnt}" if cnt > 0 else ""))
    parts.append("<b>✅ Я буду:</b>\n" + ("\n".join(lines) if lines else "—"))

    # Анонимные плюсы (только плюсы, не "Я буду")
    anon_total = sum(cnt for uid, cnt in plus_counts.items() if uid not in lists["Я буду"])
    if anon_total > 0:
        parts.append(f"— Анонимные плюсы: +{anon_total}")

    # ❌ Я не иду
    lines_no = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Я не иду"], key=lambda x: user_names.get(x, ""))]
    parts.append("<b>❌ Я не иду:</b>\n" + ("\n".join(lines_no) if lines_no else "—"))

    # 🤔 Думаю
    lines_think = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Думаю"], key=lambda x: user_names.get(x, ""))]
    parts.append("<b>🤔 Думаю:</b>\n" + ("\n".join(lines_think) if lines_think else "—"))

    # Итоги
    total_yes_people = len(lists["Я буду"])
    total_go = total_yes_people + total_plus + anon_total
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
        "lists": {k: list(v) for k, v in event["lists"].items()}
    }
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                data JSONB
            )
            """
        )
        cur.execute(
            """
            INSERT INTO events (event_id, data)
            VALUES (%s, %s)
            ON CONFLICT (event_id) DO UPDATE SET data = EXCLUDED.data
            """,
            (event_id, Json(event_copy))
        )
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
        "closed": False
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
    msg = ""
    for eid, ev in events.items():
        msg += f"{eid}: {html.escape(ev['text'])}\n"
    await update.message.reply_text(msg)

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        event_id, btn = query.data.split("|", 1)
    except ValueError:
        logger.warning("Bad callback_data: %s", query.data)
        return

    event = events.get(event_id)
    if not event:
        await query.answer("Событие не найдено.", show_alert=True)
        return

    user_id = query.from_user.id
    user_name = query.from_user.full_name
    event["user_names"][user_id] = user_name
    old_text = format_event(event_id)
    old_markup_present = bool(query.message.reply_markup and getattr(query.message.reply_markup, "inline_keyboard", None))

    if btn == "Закрыть сбор":
        if event["closed"]:
            await query.answer("Сбор уже закрыт.", show_alert=True)
            return
        event["closed"] = True
    else:
        if event["closed"]:
            await query.answer("Сбор уже закрыт!", show_alert=True)
            return

        if btn in ["Я буду", "Я не иду", "Думаю"]:
            for k in ["Я буду", "Я не иду", "Думаю"]:
                if k != btn:
                    event["lists"][k].discard(user_id)
            event["lists"][btn].add(user_id)
            if btn != "Я буду":
                event["plus_counts"].pop(user_id, None)
        elif btn == "Плюс":
            if user_id in event["lists"]["Я буду"]:
                # Пользователь уже идёт — плюсы к его имени
                event["plus_counts"][user_id] = event["plus_counts"].get(user_id, 0) + 1
            else:
                # Анонимный плюс
                anon_key = f"anon_{uuid4().hex}"
                event["plus_counts"][anon_key] = 1
        elif btn == "Минус":
            # Минусы убираются как у "Я буду", так и у анонимов
            to_remove = []
            for uid, cnt in event["plus_counts"].items():
                if (uid == user_id) or (str(uid).startswith("anon_")):
                    event["plus_counts"][uid] -= 1
                    if event["plus_counts"][uid] <= 0:
                        to_remove.append(uid)
            for uid in to_remove:
                event["plus_counts"].pop(uid, None)

    save_event(event_id, event)
    new_text = format_event(event_id)
    need_edit = new_text != old_text or (old_markup_present and event["closed"])
    reply_markup = None if event["closed"] else get_keyboard(event_id)
    if need_edit:
        try:
            await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.exception("BadRequest: %s", e)
                raise

# --- Запуск приложения ---
def main():
    load_events()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_event", new_event))
    application.add_handler(CommandHandler("events", show_events))
    application.add_handler(CallbackQueryHandler(button_click))
    
    application.run_polling()

if __name__ == "__main__":
    main()
