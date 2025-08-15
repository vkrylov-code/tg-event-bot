import os
from uuid import uuid4
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # например https://mybot.onrender.com/<secret_path>

# Хранилище событий
events = {}

# Формирование клавиатуры
def get_keyboard(event_id, closed=False):
    if closed:
        return None
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Я буду", callback_data=f"{event_id}|Я буду"),
            InlineKeyboardButton("❌ Я не иду", callback_data=f"{event_id}|Я не иду"),
            InlineKeyboardButton("🤔 Думаю", callback_data=f"{event_id}|Думаю")
        ],
        [
            InlineKeyboardButton("➕ Плюс", callback_data=f"{event_id}|Плюс"),
            InlineKeyboardButton("➖ Минус", callback_data=f"{event_id}|Минус"),
            InlineKeyboardButton("🚫 Закрыть сбор", callback_data=f"{event_id}|Закрыть сбор")
        ]
    ])

# Форматирование текста события
def format_event(event_id):
    event = events[event_id]
    text = event["text"]

    # Основные списки
    lists = event["lists"]
    plus_counts = event["plus_counts"]

    def format_users(users):
        result = []
        for user in users:
            if user in plus_counts:
                result.append(f"{user} +{plus_counts[user]}")
            else:
                result.append(user)
        return result

    parts = []
    if lists["Я буду"]:
        parts.append("\n<b>✅ Я буду:</b>\n" + "\n".join(format_users(lists["Я буду"])))
    if lists["Я не иду"]:
        parts.append("\n<b>❌ Я не иду:</b>\n" + "\n".join(lists["Я не иду"]))
    if lists["Думаю"]:
        parts.append("\n<b>🤔 Думаю:</b>\n" + "\n".join(lists["Думаю"]))

    # Итоговый блок
    total_count = len(lists["Я буду"]) + sum(plus_counts.values())
    summary = [
        "-----------------",
        f"Всего идут: {total_count}",
        f"✅ {len(lists['Я буду']) + sum(plus_counts.values())}",
        f"❌ {len(lists['Я не иду'])}",
        f"🤔 {len(lists['Думаю'])}"
    ]

    return text + "\n" + "\n".join(parts) + "\n" + "\n".join(summary)

# Команда /new_event
async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event_id = str(uuid4())
    text = " ".join(context.args) if context.args else "Событие без названия"
    text = update.message.text.replace("/new_event", "").strip()

    events[event_id] = {
        "text": text,
        "lists": {"Я буду": set(), "Я не иду": set(), "Думаю": set()},
        "plus_counts": {},
        "closed": False
    }

    await update.message.reply_text(
        format_event(event_id),
        reply_markup=get_keyboard(event_id),
        parse_mode="HTML"
    )

# Обработка кнопок
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    event_id, btn = query.data.split("|")
    event = events.get(event_id)
    if not event:
        return

    user_name = query.from_user.full_name
    old_text = format_event(event_id)

    if btn == "Закрыть сбор":
        event["closed"] = True
    elif event["closed"]:
        await query.answer("Сбор уже закрыт!", show_alert=True)
        return
    elif btn in ["Я буду", "Я не иду", "Думаю"]:
        for key in ["Я буду", "Я не иду", "Думаю"]:
            if key != btn:
                event["lists"][key].discard(user_name)
        event["lists"][btn].add(user_name)
        if btn != "Я буду":
            event["plus_counts"].pop(user_name, None)
    elif btn == "Плюс":
        event["lists"]["Думаю"].discard(user_name)
        event["lists"]["Я не иду"].discard(user_name)
        event["lists"]["Я буду"].add(user_name)
        event["plus_counts"][user_name] = event["plus_counts"].get(user_name, 0) + 1
    elif btn == "Минус":
        if user_name in event["plus_counts"]:
            event["plus_counts"][user_name] -= 1
            if event["plus_counts"][user_name] <= 0:
                event["plus_counts"].pop(user_name)
                event["lists"]["Я буду"].discard(user_name)

    new_text = format_event(event_id)

    if new_text == old_text:
        return

    await query.edit_message_text(
        text=new_text,
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id, closed=event["closed"])
    )

# Старт
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Используй /new_event <текст> для создания события.")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new_event", new_event))
    app.add_handler(CallbackQueryHandler(button_click))

    # Запуск вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        webhook_url=WEBHOOK_URL
    )

if __name__ == "__main__":
    main()
