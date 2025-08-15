import os
from datetime import datetime, timedelta
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # например https://myapp.onrender.com/secret123
PORT = int(os.environ.get("PORT", 8443))
ADMIN_ID = int(os.environ.get("ADMIN_ID"))  # Ваш Telegram ID
DATABASE_URL = os.environ["DATABASE_URL"]


if not TOKEN:
    logger.error("Не задана переменная окружения BOT_TOKEN. Прекращаю запуск.")
    raise SystemExit("BOT_TOKEN is required")

if not WEBHOOK_URL:
    logger.warning("WEBHOOK_URL не задан. Убедись, что переменная окружения установлена (Render).")

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


# --- Хранилище событий в памяти ---
# events: {event_id: {
#   "text": str,
#   "lists": {"Я буду": set(user_id), "Я не иду": set(user_id), "Думаю": set(user_id)},
#   "plus_counts": {user_id: int},
#   "user_names": {user_id: str},
#   "closed": bool
# }}
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
    """Кликабельная ссылка на профиль Telegram."""
    safe = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

# --- Форматирование сообщения (HTML) ---
def format_event(event_id: str) -> str:
    event = events[event_id]
    title = html.escape(event["text"])
    parts = [f"<b>{title}</b>\n"]
    lists = event["lists"]
    plus_counts = event["plus_counts"]
    user_names = event["user_names"]

    # 1) Имена пользователей, которые в "Я буду" (с +N если есть)
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

    # 2) Анонимные плюсы: пользователи, у которых есть plus_counts, но их нет в lists["Я буду"]
    anon_lines = []
    for uid, cnt in sorted(plus_counts.items(), key=lambda x: user_names.get(x[0], "")):
        if uid not in lists["Я буду"]:
            # отображаем строку без имени
            anon_lines.append(f"— +{cnt}")
    if anon_lines:
        # Вставляем анонимные строки прямо в блок "Я буду" после имен (если были)
        # Если в "Я буду" были имена, они уже вставлены выше. Добавляем анонимные строки тоже туда.
        # Для более явного разделения оставим их в отдельном блоке "Доп. места (без имени)" — но по требованию покажем просто в разделе "Я буду" как строки без имени.
        parts.append("\n".join(anon_lines) + "\n")

    # 3) "Я не иду"
    if lists["Я не иду"]:
        lines = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Я не иду"], key=lambda x: user_names.get(x, ""))]
        parts.append("<b>❌ Я не иду:</b>\n" + "\n".join(lines) + "\n")
    else:
        parts.append("<b>❌ Я не иду:</b>\n—\n")

    # 4) "Думаю"
    if lists["Думаю"]:
        lines = [format_user_link(uid, user_names.get(uid, "User")) for uid in sorted(lists["Думаю"], key=lambda x: user_names.get(x, ""))]
        parts.append("<b>🤔 Думаю:</b>\n" + "\n".join(lines) + "\n")
    else:
        parts.append("<b>🤔 Думаю:</b>\n—\n")

    # Итоговый блок
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
        "plus_counts": {},      # {user_id: int}
        "user_names": {},       # {user_id: full_name}
        "closed": False
    }
    
    save_event(event_id, event) # Сохранение события в БД
    
    logger.info("Создано событие id=%s by %s: %s", event_id, update.effective_user.full_name, text)

    await update.message.reply_text(
        format_event(event_id),
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    event_id = query.message.message_id  # используем message_id как event_id
    event = load_event(str(event_id))
    if not event:
        await query.edit_message_text("Ошибка: событие не найдено")
        return

    if event.get("closed"):
        await query.answer("Сбор закрыт", show_alert=True)
        return


    logger.info("Callback from %s data=%s", query.from_user.full_name, query.data)

    try:
        event_id, btn = query.data.split("|", 1)
    except ValueError:
        logger.warning("Bad callback_data: %s", query.data)
        return

    event = events.get(event_id)
    if not event:
        await query.answer("Событие не найдено.", show_alert=True)
        logger.warning("Event not found: %s", event_id)
        return

    user_id = query.from_user.id
    user_name = query.from_user.full_name
    event["user_names"][user_id] = user_name  # сохраняем/обновляем имя

    old_text = format_event(event_id)
    old_markup_present = bool(query.message.reply_markup and getattr(query.message.reply_markup, "inline_keyboard", None))

    # --- Обработка кнопок ---
    changed = False  # флаг изменений

    if btn == "Закрыть сбор":
        if event["closed"]:
            await query.answer("Сбор уже закрыт.", show_alert=True)
            return
        event["closed"] = True
        changed = True
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
            changed = True
            logger.info("User %s set status %s (event %s)", user_name, btn, event_id)

        elif btn == "Плюс":
            event["plus_counts"][user_id] = event["plus_counts"].get(user_id, 0) + 1
            changed = True
            logger.info("Plus: %s now +%d (event %s)", user_name, event["plus_counts"][user_id], event_id)

        elif btn == "Минус":
            if user_id in event["plus_counts"]:
                event["plus_counts"][user_id] -= 1
                if event["plus_counts"][user_id] <= 0:
                    event["plus_counts"].pop(user_id, None)
                changed = True
                logger.info("Minus: %s now +%d (event %s)", user_name, event.get("plus_counts", {}).get(user_id, 0), event_id)

    # --- Сохраняем изменения в БД ---
    if changed:
        save_event(event_id, event)

    new_text = format_event(event_id)
    need_edit = new_text != old_text or (old_markup_present and event["closed"])
    reply_markup = None if event["closed"] else get_keyboard(event_id)

    if need_edit:
        try:
            await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
            logger.info("Message updated for event %s", event_id)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                logger.info("Edit skipped: message not modified (event %s).", event_id)
                return
            logger.exception("BadRequest while editing message: %s", e)
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


# --- Запуск приложения (webhook) ---
def main():
    
    init_db()
    delete_old_events()
        
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new_event", new_event))
    app.add_handler(CallbackQueryHandler(button_click))

    logger.info("Starting webhook, URL=%s, PORT=%s", WEBHOOK_URL, PORT)
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()