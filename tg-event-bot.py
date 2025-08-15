import os
import json
import logging
from uuid import uuid4
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# =====================
# НАСТРОЙКИ
# =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Токен из переменной окружения Render
ADMIN_ID = int(os.environ.get("ADMIN_ID"))  # Твой Telegram ID из переменной окружения Render
DATA_DIR = "/data"
DATA_FILE = os.path.join(DATA_DIR, "events.json")

# Создаём папку, если её нет
os.makedirs(DATA_DIR, exist_ok=True)

# =====================
# ЛОГИ
# =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# =====================
# ХРАНИЛИЩЕ
# =====================
events = {}

def save_events():
    """Сохраняем события в JSON"""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False)
        logging.info("События сохранены в %s", DATA_FILE)
    except Exception as e:
        logging.error("Ошибка сохранения событий: %s", e)

def load_events():
    """Загружаем события из JSON"""
    global events
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
            logging.info("События загружены (%s)", len(events))
        except Exception as e:
            logging.error("Ошибка загрузки событий: %s", e)
            events = {}
    else:
        logging.info("Файл с событиями отсутствует, начинаем с пустого списка.")
        events = {}

# =====================
# ЛОГИКА
# =====================
def get_keyboard(event_id):
    """Создаёт клавиатуру"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Я буду", callback_data=f"yes|{event_id}"),
            InlineKeyboardButton("❌ Я не иду", callback_data=f"no|{event_id}"),
            InlineKeyboardButton("🤔 Думаю", callback_data=f"maybe|{event_id}")
        ],
        [
            InlineKeyboardButton("➕ Плюс", callback_data=f"plus|{event_id}"),
            InlineKeyboardButton("➖ Минус", callback_data=f"minus|{event_id}"),
            InlineKeyboardButton("🔒 Закрыть сбор", callback_data=f"close|{event_id}")
        ]
    ])

def format_event(event):
    """Формирует красивое сообщение события"""
    text = event["text"] + "\n\n"
    text += "✅ Я буду:\n"
    for user_id, user_data in event["yes"].items():
        count = user_data.get("plus", 0)
        plus_text = f" +{count}" if count > 0 else ""
        text += f"[{user_data['name']}](tg://user?id={user_id}){plus_text}\n"

    text += "\n❌ Я не иду:\n"
    for user_id, user_data in event["no"].items():
        text += f"[{user_data['name']}](tg://user?id={user_id})\n"

    text += "\n🤔 Думаю:\n"
    for user_id, user_data in event["maybe"].items():
        text += f"[{user_data['name']}](tg://user?id={user_id})\n"

    # Подсчёты
    total_yes = len(event["yes"]) + sum(u["plus"] for u in event["yes"].values())
    total_no = len(event["no"])
    total_maybe = len(event["maybe"])

    text += "\n-----------------\n"
    text += f"Всего идут: {total_yes}\n"
    text += f"✅ {total_yes}\n"
    text += f"❌ {total_no}\n"
    text += f"🤔 {total_maybe}"

    return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь /create чтобы создать событие.")

async def create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event_id = uuid4().hex
    text = " ".join(context.args) if context.args else "Новое событие"
    events[event_id] = {
        "text": text,
        "yes": {},
        "no": {},
        "maybe": {},
        "plus_no_name": 0
    }
    save_events()
    await update.message.reply_text(text, reply_markup=get_keyboard(event_id), parse_mode="Markdown")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, event_id = query.data.split("|")
    event = events.get(event_id)

    if not event:
        logging.warning("Event not found: %s", event_id)
        await query.edit_message_text("Событие устарело или удалено.")
        return

    user_id = str(query.from_user.id)
    user_name = query.from_user.full_name

    # Убираем пользователя из других статусов
    if action in ["yes", "no", "maybe"]:
        event["yes"].pop(user_id, None)
        event["no"].pop(user_id, None)
        event["maybe"].pop(user_id, None)

    if action == "yes":
        event["yes"][user_id] = {"name": user_name, "plus": event["yes"].get(user_id, {}).get("plus", 0)}
    elif action == "no":
        event["no"][user_id] = {"name": user_name}
    elif action == "maybe":
        event["maybe"][user_id] = {"name": user_name}
    elif action == "plus":
        if user_id in event["yes"]:
            event["yes"][user_id]["plus"] = event["yes"][user_id].get("plus", 0) + 1
        else:
            event["plus_no_name"] += 1
    elif action == "minus":
        if user_id in event["yes"] and event["yes"][user_id].get("plus", 0) > 0:
            event["yes"][user_id]["plus"] -= 1
        elif event["plus_no_name"] > 0:
            event["plus_no_name"] -= 1
    elif action == "close":
        await query.edit_message_text(format_event(event), parse_mode="Markdown")
        events.pop(event_id, None)
        save_events()
        return

    save_events()
    await query.edit_message_text(format_event(event), reply_markup=get_keyboard(event_id), parse_mode="Markdown")

# =====================
# АДМИН-КОМАНДА /dump
# =====================
async def dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет прав для этой команды.")
        return

    if not os.path.exists(DATA_FILE):
        await update.message.reply_text("Файл событий отсутствует.")
        return

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = f.read()

    if len(data) > 4000:
        await update.message.reply_document(document=open(DATA_FILE, "rb"))
    else:
        await update.message.reply_text(f"```\n{data}\n```", parse_mode="Markdown")

# =====================
# ЗАПУСК
# =====================
if __name__ == "__main__":
    load_events()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(CommandHandler("dump", dump))

    logging.info("Бот запущен.")
    app.run_polling()
