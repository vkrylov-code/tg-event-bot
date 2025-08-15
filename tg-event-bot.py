# bot.py
import os
import html
import logging
from uuid import uuid4

import asyncpg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- Логи ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Конфиг из окружения ---
TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("APP_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
PORT = int(os.environ.get("PORT", 8443))
DATABASE_URL = os.environ.get("DATABASE_URL")  # например postgres://user:pass@host:port/dbname

if not TOKEN:
    logger.error("Не задан BOT_TOKEN")
    raise SystemExit("BOT_TOKEN required")
if not DATABASE_URL:
    logger.error("Не задан DATABASE_URL")
    raise SystemExit("DATABASE_URL required")

# --- Подключение к базе ---
db_pool: asyncpg.pool.Pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    # Создаем таблицы, если их нет
    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            closed BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT
        );
        CREATE TABLE IF NOT EXISTS event_statuses (
            event_id TEXT REFERENCES events(event_id),
            user_id BIGINT REFERENCES users(user_id),
            status TEXT,
            plus_count INT DEFAULT 0,
            PRIMARY KEY (event_id, user_id)
        );
        """)

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

# --- Форматирование события ---
async def format_event(event_id: str) -> str:
    async with db_pool.acquire() as conn:
        event = await conn.fetchrow("SELECT text, closed FROM events WHERE event_id=$1", event_id)
        statuses = await conn.fetch("SELECT user_id, status, plus_count FROM event_statuses WHERE event_id=$1", event_id)
        users = {row["user_id"]: (await conn.fetchrow("SELECT full_name FROM users WHERE user_id=$1", row["user_id"]))["full_name"] for row in statuses}

    lists = {"Я буду": set(), "Я не иду": set(), "Думаю": set()}
    plus_counts = {}
    for row in statuses:
        uid = row["user_id"]
        stat = row["status"]
        pc = row["plus_count"]
        if stat in lists:
            lists[stat].add(uid)
        if pc > 0:
            plus_counts[uid] = pc

    parts = [f"<b>{html.escape(event['text'])}</b>\n"]

    for key in ["Я буду", "Я не иду", "Думаю"]:
        if lists[key]:
            lines = []
            for uid in sorted(lists[key], key=lambda x: users.get(x, "")):
                link = format_user_link(uid, users.get(uid, "User"))
                cnt = plus_counts.get(uid, 0)
                lines.append(link + (f" +{cnt}" if cnt > 0 else ""))
            parts.append(f"<b>{key}:</b>\n" + "\n".join(lines))
        else:
            parts.append(f"<b>{key}:</b>\n—")

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
    await update.message.reply_text(
        "Привет! Создай событие командой:\n/new_event Текст события"
    )

async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    text = raw[len("/new_event"):].strip() if raw.startswith("/new_event") else raw
    if not text:
        text = "Событие (без названия)"
    event_id = uuid4().hex

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO events(event_id, text, closed) VALUES($1,$2,$3)", event_id, text, False)

    await update.message.reply_text(
        await format_event(event_id),
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        event_id, btn = query.data.split("|", 1)
    except ValueError:
        return
    user_id = query.from_user.id
    user_name = query.from_user.full_name

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(user_id, full_name) VALUES($1,$2) "
            "ON CONFLICT (user_id) DO UPDATE SET full_name=EXCLUDED.full_name",
            user_id, user_name
        )
        event = await conn.fetchrow("SELECT closed FROM events WHERE event_id=$1", event_id)
        if not event:
            await query.answer("Событие не найдено.", show_alert=True)
            return

        if btn == "Закрыть сбор":
            if event["closed"]:
                await query.answer("Сбор уже закрыт.", show_alert=True)
                return
            await conn.execute("UPDATE events SET closed=TRUE WHERE event_id=$1", event_id)
        else:
            if event["closed"]:
                await query.answer("Сбор уже закрыт!", show_alert=True)
                return

            if btn in ["Я буду", "Я не иду", "Думаю"]:
                # Удаляем все статусы пользователя для события
                await conn.execute("DELETE FROM event_statuses WHERE event_id=$1 AND user_id=$2", event_id, user_id)
                await conn.execute("INSERT INTO event_statuses(event_id,user_id,status,plus_count) VALUES($1,$2,$3,$4)",
                                   event_id, user_id, btn, 0)
            elif btn == "Плюс":
                row = await conn.fetchrow("SELECT plus_count FROM event_statuses WHERE event_id=$1 AND user_id=$2", event_id, user_id)
                if row:
                    await conn.execute("UPDATE event_statuses SET plus_count = plus_count+1 WHERE event_id=$1 AND user_id=$2", event_id, user_id)
                else:
                    await conn.execute("INSERT INTO event_statuses(event_id,user_id,status,plus_count) VALUES($1,$2,'Я буду',1)", event_id, user_id)
            elif btn == "Минус":
                row = await conn.fetchrow("SELECT plus_count FROM event_statuses WHERE event_id=$1 AND user_id=$2", event_id, user_id)
                if row and row["plus_count"] > 1:
                    await conn.execute("UPDATE event_statuses SET plus_count = plus_count-1 WHERE event_id=$1 AND user_id=$2", event_id, user_id)
                elif row:
                    await conn.execute("DELETE FROM event_statuses WHERE event_id=$1 AND user_id=$2", event_id, user_id)

    new_text = await format_event(event_id)
    reply_markup = None if btn == "Закрыть сбор" else get_keyboard(event_id)
    try:
        await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

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


# --- Запуск ---
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new_event", new_event))
    app.add_handler(CallbackQueryHandler(button_click))

    import asyncio
    asyncio.run(init_db())

    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
