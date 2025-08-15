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

DB_URL = os.environ.get("DATABASE_URL")  # PostgreSQL URL

if not TOKEN:
    logger.error("Не задана переменная окружения BOT_TOKEN. Прекращаю запуск.")
    raise SystemExit("BOT_TOKEN is required")

# --- Подключение к БД ---
def get_db_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
            """)
            conn.commit()
        conn.close()
        logger.info("Таблица events готова")
    except Exception as e:
        logger.exception("Ошибка инициализации БД: %s", e)
        raise

# --- Загрузка всех событий из БД при старте ---
events = {}
def load_events():
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id, data FROM events")
            rows = cur.fetchall()
            for row in rows:
                events[row["id"]] = row["data"]
        conn.close()
        logger.info("Загружено %d событий из БД", len(events))
    except Exception as e:
        logger.exception("Ошибка загрузки событий из БД: %s", e)

# --- Сохранение события в БД ---
def save_event(event_id, event):
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO events (id, data)
                VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (event_id, Json(event)))
            conn.commit()
        conn.close()
        logger.debug("Событие %s сохранено в БД", event_id)
    except Exception as e:
        logger.exception("Ошибка сохранения события %s: %s", event_id, e)

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
    if lists["Я буду"]:
        lines = []
        for uid in sorted(lists["Я буду"], key=lambda x: user_names.get(x, "")):
            name = user_names.get(uid, "User")
            link = format_user_link(uid, name)
            cnt = plus_counts.get(uid, 0)
            lines.append(link + (f" +{cnt}" if cnt > 0 else ""))
        parts.append("<b>✅ Я буду:</b>\n" + "\n".join(lines) + "\n")
    else:
        parts.append("<b>✅ Я буду:</b>\n—\n")

    # Анонимные плюсы
    anon_lines = []
    for uid, cnt in sorted(plus_counts.items(), key=lambda x: user_names.get(x[0], "")):
        if uid not in lists["Я буду"]:
            anon_lines.append(f"— +{cnt}")
    if anon_lines:
        parts.append("\n".join(anon_lines) + "\n")

    # ❌ Я не иду
    if lists["Я не иду"]:
        lines = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Я не иду"], key=lambda x: user_names.get(x, ""))]
        parts.append("<b>❌ Я не иду:</b>\n" + "\n".join(lines) + "\n")
    else:
        parts.append("<b>❌ Я не иду:</b>\n—\n")

    # 🤔 Думаю
    if lists["Думаю"]:
        lines = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Думаю"], key=lambda x: user_names.get(x, ""))]
        parts.append("<b>🤔 Думаю:</b>\n" + "\n".join(lines) + "\n")
    else:
        parts.append("<b>🤔 Думаю:</b>\n—\n")

    # Итог
    total_yes_people = len(lists["Я буду"])
    total_plus_count = sum(plus_counts.values())
    total_go = total_yes_people + total_plus_count
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

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "Привет! Создай событие командой:\n"
        "/new_event Текст события\n\n"
        "В тексте можно использовать переносы строк. Пример:\n"
        "/new_event Первая строка\\nВторая строка"
    )

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    raw = msg.text or ""
    text = raw[len("/new_event"):].strip() if raw.startswith("/new_event") else raw
    if not text:
        text = "Событие (без названия)"

    event_id = uuid4().hex
    event = {
        "text": text,
        "lists": {"Я буду": set(), "Я не иду": set(), "Думаю": set()},
        "plus_counts": {},
        "user_names": {},
        "closed": False
    }
    events[event_id] = event
    save_event(event_id, event)

    logger.info("Создано событие id=%s by %s: %s", event_id, update.effective_user.full_name, text)

    await msg.reply_text(
        format_event(event_id),
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )
    
# --- Новый хэндлер ---
async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав на просмотр событий.")
        return

    if not events:
        await update.message.reply_text("Событий пока нет.")
        return

    lines = []
    for event_id, event in events.items():
        text = html.escape(event["text"].replace("\n", " "))
        total_yes = len(event["lists"]["Я буду"])
        total_no = len(event["lists"]["Я не иду"])
        total_think = len(event["lists"]["Думаю"])
        total_plus = sum(event["plus_counts"].values())
        closed = "⚠️ Закрыт" if event["closed"] else "🟢 Открыт"
        lines.append(f"<b>{text}</b>\nID: <code>{event_id}</code>\n✅ {total_yes} + {total_plus} | ❌ {total_no} | 🤔 {total_think} | {closed}\n---")

    message = "\n".join(lines)
    await update.message.reply_text(message, parse_mode="HTML")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
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
        event["closed"] = True
        logger.info("Сбор закрыт: %s by %s", event_id, user_name)

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
            event["plus_counts"][user_id] = event["plus_counts"].get(user_id, 0) + 1

        elif btn == "Минус":
            if user_id in event["plus_counts"]:
                event["plus_counts"][user_id] -= 1
                if event["plus_counts"][user_id] <= 0:
                    event["plus_counts"].pop(user_id, None)

    save_event(event_id, event)

    new_text = format_event(event_id)
    need_edit = new_text != old_text or (old_markup_present and event["closed"])
    if need_edit:
        reply_markup = None if event["closed"] else get_keyboard(event_id)
        try:
            await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

# --- Main ---
def main():
    init_db()
    load_events()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new_event", new_event))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(CommandHandler("list_events", list_events))

    logger.info("Starting webhook, URL=%s, PORT=%s", WEBHOOK_URL, PORT)
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
