import os
from datetime import datetime, timedelta
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import psycopg2
from psycopg2.extras import RealDictCursor, Json

# -----------------------------
# НАСТРОЙКИ
# -----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
DATABASE_URL = os.environ["DATABASE_URL"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------
# PostgreSQL
# -----------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            event_id TEXT UNIQUE NOT NULL,
            data JSONB NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """)
        conn.commit()

def save_event(event_id, data):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO events (event_id, data, created_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (event_id) DO UPDATE SET data = EXCLUDED.data
        """, (event_id, Json(data)))
        conn.commit()

def load_event(event_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM events WHERE event_id = %s", (event_id,))
        row = cur.fetchone()
        return row["data"] if row else None

def delete_old_events():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM events WHERE created_at < %s", 
                    (datetime.utcnow() - timedelta(days=90),))
        conn.commit()

# -----------------------------
# Логика бота
# -----------------------------
def build_keyboard(event):
    keyboard = [
        [
            InlineKeyboardButton("✅ Я буду", callback_data="going"),
            InlineKeyboardButton("❌ Я не иду", callback_data="not_going"),
            InlineKeyboardButton("🤔 Думаю", callback_data="thinking")
        ],
        [
            InlineKeyboardButton("➕ Плюс", callback_data="plus"),
            InlineKeyboardButton("➖ Минус", callback_data="minus"),
            InlineKeyboardButton("🛑 Закрыть сбор", callback_data="close")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def render_message(event):
    """Создаёт текст события с итогами с кликабельными именами и переносами строк"""
    text = event.get("text", "Событие").strip()
    lines = [text, "\n-----------------"]

    going_list = []
    not_going_list = []
    thinking_list = []

    for user, info in event.get("users", {}).items():
        status = info.get("status")
        plus_count = info.get("plus", 0)
        user_id = info.get("id")

        # Если есть user_id, делаем имя кликабельным
        display_name = f"[{user}](tg://user?id={user_id})" if user_id else user
        if status == "going" and plus_count:
            display_name += f" +{plus_count}"

        if status == "going":
            going_list.append(display_name)
        elif status == "not_going":
            not_going_list.append(display_name)
        elif status == "thinking":
            thinking_list.append(display_name)

    lines.append(f"Всего идут: {len(going_list)}")
    lines.append(f"✅ {len(going_list)}: {', '.join(going_list) if going_list else '-'}")
    lines.append(f"❌ {len(not_going_list)}: {', '.join(not_going_list) if not_going_list else '-'}")
    lines.append(f"🤔 {len(thinking_list)}: {', '.join(thinking_list) if thinking_list else '-'}")

    return "\n".join(lines)

# -----------------------------
# Обработчики команд
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Используй /newevent <текст> для создания события.\n"
        "Поддерживаются переносы строк в описании."
    )

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) or "Новое событие"
    text = text.replace("\\n", "\n")  # Поддержка переносов через \n
    event_id = str(update.message.message_id)
    event = {
        "text": text,
        "users": {},
        "closed": False
    }
    save_event(event_id, event)
    await update.message.reply_text(
        render_message(event),
        reply_markup=build_keyboard(event),
        parse_mode="Markdown"
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    event_id = str(query.message.message_id)
    event = load_event(event_id)
    if not event:
        await query.edit_message_text("Ошибка: событие не найдено")
        return

    if event.get("closed"):
        await query.answer("Сбор закрыт", show_alert=True)
        return

    user = query.from_user.full_name
    user_id = query.from_user.id
    info = event.setdefault("users", {}).setdefault(user, {"status": None, "plus": 0, "id": user_id})

    # Обновление статусов
    if query.data in ["going", "not_going", "thinking"]:
        info["status"] = query.data
        if query.data != "going":
            info["plus"] = 0
    elif query.data == "plus":
        info["plus"] += 1
        if info["status"] != "going":
            info["status"] = "going"
    elif query.data == "minus":
        if info["plus"] > 0:
            info["plus"] -= 1
    elif query.data == "close":
        event["closed"] = True

    save_event(event_id, event)

    try:
        await query.edit_message_text(
            render_message(event),
            reply_markup=None if event.get("closed") else build_keyboard(event),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Ошибка обновления сообщения: {e}")

# -----------------------------
# Скрытая команда /dump для администратора
# -----------------------------
async def dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещен")
        return

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT event_id, data, created_at FROM events ORDER BY created_at DESC")
        rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("Событий нет")
        return

    text_lines = []
    for row in rows:
        eid = row["event_id"]
        created = row["created_at"].strftime("%Y-%m-%d %H:%M")
        data = row["data"]
        text_lines.append(
            f"ID: {eid} | Создано: {created}\n"
            f"Текст: {data.get('text')}\n"
            f"Пользователи: {data.get('users')}\n"
            f"Closed: {data.get('closed')}\n{'-'*20}"
        )

    full_text = "\n".join(text_lines)
    for i in range(0, len(full_text), 3900):
        await update.message.reply_text(full_text[i:i+3900])

# -----------------------------
# Основной запуск
# -----------------------------
if __name__ == "__main__":
    init_db()
    delete_old_events()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newevent", new_event))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(CommandHandler("dump", dump))

    PORT = int(os.environ.get("PORT", 8443))
    URL = os.environ.get("APP_URL")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{URL}/webhook/{BOT_TOKEN}"
    )
