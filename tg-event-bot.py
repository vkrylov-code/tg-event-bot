import os
import json
import html
import logging
import uuid
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# === ЛОГИ ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Хранилище событий ===
DATA_FILE = "/data/events.json"
events = {}

def save_events():
    """Сохраняем события в JSON"""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def load_events():
    """Загружаем события из JSON"""
    global events
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки: {e}")
            events = {}
    else:
        events = {}

# === Форматирование имени с кликабельным профилем ===
def format_user_link(user_id: int, name: str) -> str:
    safe_name = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

# === Форматирование события ===
def format_event(event_id: str) -> str:
    event = events[event_id]
    title = html.escape(event["text"])
    parts = [f"<b>{title}</b>\n"]

    lists = event["lists"]
    plus_counts = event["plus_counts"]
    user_names = event["user_names"]

    def list_to_str(status):
        result = []
        for uid in sorted(lists[status], key=lambda x: user_names.get(str(x), "")):
            link = format_user_link(uid, user_names[str(uid)])
            cnt = plus_counts.get(str(uid), 0)
            if cnt > 0 and status == "Я буду":
                result.append(f"{link} +{cnt}")
            else:
                result.append(link)
        return "\n".join(result) if result else ""

    if lists["Я буду"]:
        parts.append("<b>✅ Я буду:</b>\n" + list_to_str("Я буду") + "\n")
    if lists["Я не иду"]:
        parts.append("<b>❌ Я не иду:</b>\n" + list_to_str("Я не иду") + "\n")
    if lists["Думаю"]:
        parts.append("<b>🤔 Думаю:</b>\n" + list_to_str("Думаю") + "\n")

    # Считаем итоги
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

# === Генерация кнопок ===
def get_keyboard(event_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Я буду", callback_data=f"{event_id}|Я буду"),
            InlineKeyboardButton("❌ Я не иду", callback_data=f"{event_id}|Я не иду"),
            InlineKeyboardButton("🤔 Думаю", callback_data=f"{event_id}|Думаю"),
        ],
        [
            InlineKeyboardButton("+1", callback_data=f"{event_id}|Плюс"),
            InlineKeyboardButton("-1", callback_data=f"{event_id}|Минус"),
            InlineKeyboardButton("🚫 Закрыть сбор", callback_data=f"{event_id}|Закрыть"),
        ]
    ])

# === Команда /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь /event и текст события для начала сбора.")

# === Создание события ===
async def event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /event Текст события")
        return

    event_text = " ".join(context.args)
    event_id = uuid.uuid4().hex

    events[event_id] = {
        "text": event_text,
        "lists": {"Я буду": set(), "Я не иду": set(), "Думаю": set()},
        "plus_counts": {},
        "user_names": {},
        "closed": False
    }
    save_events()

    await update.message.reply_html(
        format_event(event_id),
        reply_markup=get_keyboard(event_id)
    )

# === Обработка кнопок ===
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        event_id, btn = query.data.split("|", 1)
    except ValueError:
        return

    event = events.get(event_id)
    if not event:
        logger.warning(f"Event not found: {event_id}")
        await query.edit_message_text("⚠️ Это событие устарело или удалено.")
        return

    user_id = query.from_user.id
    user_name = query.from_user.full_name
    event["user_names"][str(user_id)] = user_name  # сохраняем имя

    lists = event["lists"]
    plus_counts = event["plus_counts"]

    if btn in ["Я буду", "Я не иду", "Думаю"]:
        for k in lists:
            lists[k].discard(user_id)
        lists[btn].add(user_id)

    elif btn == "Плюс":
        if str(user_id) in plus_counts:
            plus_counts[str(user_id)] += 1
        else:
            plus_counts[str(user_id)] = 1
        # Если пользователя нет в "Я буду" — просто +N без имени
        if user_id not in lists["Я буду"]:
            plus_counts.setdefault("no_name", 0)
            plus_counts["no_name"] += 1

    elif btn == "Минус":
        if str(user_id) in plus_counts and plus_counts[str(user_id)] > 0:
            plus_counts[str(user_id)] -= 1
        if "no_name" in plus_counts and plus_counts["no_name"] > 0:
            plus_counts["no_name"] -= 1

    elif btn == "Закрыть":
        event["closed"] = True

    save_events()

    if event["closed"]:
        await query.edit_message_text(format_event(event_id), parse_mode="HTML")
    else:
        try:
            await query.edit_message_text(
                format_event(event_id),
                reply_markup=get_keyboard(event_id),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Edit message failed: {e}")

# === Запуск бота с вебхуками ===
def main():
    load_events()

    TOKEN = os.environ["BOT_TOKEN"]
    APP_URL = os.environ["RENDER_EXTERNAL_URL"]

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("event", event_command))
    app.add_handler(CallbackQueryHandler(button_click))

    # Запуск вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        url_path=TOKEN,
        webhook_url=f"{APP_URL}/{TOKEN}"
    )

if __name__ == "__main__":
    main()
