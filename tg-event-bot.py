import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

events = {}

BUTTONS_FIRST_ROW = [
    ("✅ Я буду", "Я буду"),
    ("❌ Я не иду", "Я не иду"),
    ("🤔 Думаю", "Думаю")
]

BUTTONS_SECOND_ROW = [
    ("➕ Плюс", "Плюс"),
    ("➖ Минус", "Минус"),
    ("🔒 Закрыть сбор", "Закрыть сбор")
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Создай событие командой /create Название события\n"
        "Можно использовать переносы строк с помощью \\n"
    )

async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи название события: /create Первая строка\\nВторая строка")
        return

    title = " ".join(context.args).replace("\\n", "\n")
    event_id = str(len(events) + 1)
    events[event_id] = {
        "title": title,
        "lists": {btn[1]: set() for btn in BUTTONS_FIRST_ROW + BUTTONS_SECOND_ROW[:-1]},
        "plus_counts": {},
        "closed": False
    }

    await update.message.reply_text(
        f"Событие создано:\n<b>{title}</b>",
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )

def get_keyboard(event_id, closed=False):
    if closed:
        return InlineKeyboardMarkup([])
    keyboard = [
        [InlineKeyboardButton(text=text, callback_data=f"{event_id}|{data}") for text, data in BUTTONS_FIRST_ROW],
        [InlineKeyboardButton(text=text, callback_data=f"{event_id}|{data}") for text, data in BUTTONS_SECOND_ROW]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_event(event_id):
    event = events[event_id]
    text = f"<b>{event['title']}</b>\n\n"
    for btn in event["lists"].keys():
        members_list = []
        for member in event["lists"][btn]:
            if btn == "Я буду":
                count = event["plus_counts"].get(member, 0)
                members_list.append(f"{member}" + (f" +{count}" if count > 0 else ""))
            else:
                members_list.append(member)
        members = "\n".join(members_list) if members_list else "—"
        text += f"<b>{btn}:</b>\n{members}\n\n"

    total_go = len(event["lists"]["Я буду"]) + sum(event["plus_counts"].values())
    total_yes = len(event["lists"]["Я буду"]) + sum(event["plus_counts"].values())
    total_no = len(event["lists"]["Я не иду"])
    total_think = len(event["lists"]["Думаю"])

    text += "-----------------\n"
    text += f"Всего идут: {total_go}\n"
    text += f"✅ {total_yes}\n"
    text += f"❌ {total_no}\n"
    text += f"🤔 {total_think}\n"

    if event["closed"]:
        text += "⚠️ Сбор закрыт."
    return text

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    event_id, btn = query.data.split("|")
    event = events.get(event_id)
    if not event:
        return

    user_name = query.from_user.full_name

    if btn == "Закрыть сбор":
        event["closed"] = True
        await query.edit_message_text(
            text=format_event(event_id),
            parse_mode="HTML",
            reply_markup=get_keyboard(event_id, closed=True)
        )
        return

    if event["closed"]:
        await query.answer("Сбор уже закрыт!", show_alert=True)
        return

    # Пользователь может быть только в одном статусе из трех
    if btn in ["Я буду", "Я не иду", "Думаю"]:
        for key in ["Я буду", "Я не иду", "Думаю"]:
            if key != btn:
                event["lists"][key].discard(user_name)
        event["lists"][btn].add(user_name)
        if btn != "Я буду":
            event["plus_counts"].pop(user_name, None)

    elif btn == "Плюс":
        # Переносит пользователя в "Я буду"
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

    await query.edit_message_text(
        text=format_event(event_id),
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )

if __name__ == "__main__":
    TOKEN = "TOKEN"
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create_event))
    app.add_handler(CallbackQueryHandler(button_click))

    logger.info("Бот запущен...")
    app.run_polling()
