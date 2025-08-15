# bot.py
import os
import html
import logging
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- Логи ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Конфиг из окружения ---
TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # например https://myapp.onrender.com/secret123
PORT = int(os.environ.get("PORT", 8443))

if not TOKEN:
    logger.error("Не задана переменная окружения BOT_TOKEN. Прекращаю запуск.")
    raise SystemExit("BOT_TOKEN is required")

if not WEBHOOK_URL:
    logger.warning("WEBHOOK_URL не задан. Убедись, что переменная окружения установлена (Render).")

# --- Хранилище событий в памяти ---
# events: {event_id: {"text": str, "lists": {"Я буду", "Я не иду", "Думаю"}, "plus_counts": {user: int}, "closed": bool}}
events = {}

# --- Клавиатура ---
def get_keyboard(event_id):
    """Возвращает InlineKeyboardMarkup или None (если нужно убрать клавиатуру)."""
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


# --- Форматирование сообщения (HTML) ---
def format_event(event_id: str) -> str:
    """Создаёт HTML-текст события (безопасно экранирует имена / заголовок)."""
    event = events[event_id]
    title = html.escape(event["text"])  # экранируем пользовательский текст
    parts = [f"<b>{title}</b>\n"]

    lists = event["lists"]
    plus_counts = event["plus_counts"]

    # Списки по порядку
    # Я буду (с +N при наличии)
    if lists["Я буду"]:
        lines = []
        for u in sorted(lists["Я буду"]):
            name = html.escape(u)
            cnt = plus_counts.get(u, 0)
            lines.append(name + (f" +{cnt}" if cnt > 0 else ""))
        parts.append("<b>✅ Я буду:</b>\n" + "\n".join(lines) + "\n")

    if lists["Я не иду"]:
        lines = [html.escape(u) for u in sorted(lists["Я не иду"])]
        parts.append("<b>❌ Я не иду:</b>\n" + "\n".join(lines) + "\n")

    if lists["Думаю"]:
        lines = [html.escape(u) for u in sorted(lists["Думаю"])]
        parts.append("<b>🤔 Думаю:</b>\n" + "\n".join(lines) + "\n")

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
    # Берём сырой текст сообщения без команды, чтобы сохранить переносы строк
    raw = update.message.text or ""
    text = raw
    # удалить префикс команды если есть
    if raw.startswith("/new_event"):
        text = raw[len("/new_event"):].strip()
    if not text:
        text = "Событие (без названия)"

    event_id = uuid4().hex
    events[event_id] = {
        "text": text,
        "lists": {"Я буду": set(), "Я не иду": set(), "Думаю": set()},
        "plus_counts": {},  # {user_name: count}
        "closed": False
    }

    logger.info("Создано событие id=%s by %s: %s", event_id, update.effective_user.full_name, text)

    await update.message.reply_text(
        format_event(event_id),
        parse_mode="HTML",
        reply_markup=get_keyboard(event_id)
    )


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # подтверждаем callback, чтобы не показывалось "загрузка"

    # логируем нажатие
    logger.info("Callback from user=%s data=%s", query.from_user.full_name, query.data)

    # Разбор данных
    try:
        event_id, btn = query.data.split("|", 1)
    except ValueError:
        logger.warning("Неправильный callback_data: %s", query.data)
        return

    event = events.get(event_id)
    if not event:
        logger.warning("Event not found: %s", event_id)
        await query.answer("Событие не найдено.", show_alert=True)
        return

    user_name = query.from_user.full_name

    # Сохраняем старый текст и состояние клавиатуры
    old_text = format_event(event_id)
    old_markup_present = bool(query.message.reply_markup and getattr(query.message.reply_markup, "inline_keyboard", None))

    # --- Обработка кнопок ---
    if btn == "Закрыть сбор":
        if event["closed"]:
            logger.info("Попытка закрыть уже закрытый сбор: %s", event_id)
            await query.answer("Сбор уже закрыт.", show_alert=True)
            return
        event["closed"] = True
        logger.info("Сбор закрыт: %s by %s", event_id, user_name)
    else:
        if event["closed"]:
            logger.info("Нажатие при закрытом сборе: %s by %s", event_id, user_name)
            await query.answer("Сбор уже закрыт!", show_alert=True)
            return

        if btn in ["Я буду", "Я не иду", "Думаю"]:
            # Удаляем пользователя из двух других статусов
            for k in ["Я буду", "Я не иду", "Думаю"]:
                if k != btn:
                    event["lists"][k].discard(user_name)
            # Добавляем в выбранный
            event["lists"][btn].add(user_name)
            # Если это не "Я буду", удаляем плюсы
            if btn != "Я буду":
                if user_name in event["plus_counts"]:
                    logger.debug("Удаляем плюс-голоса у %s, т.к. он выбрал %s", user_name, btn)
                event["plus_counts"].pop(user_name, None)
            logger.info("Пользователь %s -> %s (event %s)", user_name, btn, event_id)

        elif btn == "Плюс":
            # переносим в "Я буду" и добавляем плюс
            event["lists"]["Думаю"].discard(user_name)
            event["lists"]["Я не иду"].discard(user_name)
            event["lists"]["Я буду"].add(user_name)
            event["plus_counts"][user_name] = event["plus_counts"].get(user_name, 0) + 1
            logger.info("Плюс: %s теперь +%d (event %s)", user_name, event["plus_counts"][user_name], event_id)

        elif btn == "Минус":
            if user_name in event["plus_counts"]:
                event["plus_counts"][user_name] -= 1
                if event["plus_counts"][user_name] <= 0:
                    event["plus_counts"].pop(user_name, None)
                    event["lists"]["Я буду"].discard(user_name)
                    logger.info("Минус обнулил плюсы — %s удалён из 'Я буду' (event %s)", user_name, event_id)
                else:
                    logger.info("Минус: %s теперь +%d (event %s)", user_name, event["plus_counts"][user_name], event_id)
            else:
                logger.debug("Минус от %s, но у него не было плюсов (event %s)", user_name, event_id)

    # Формируем новый текст
    new_text = format_event(event_id)
    # Нужно ли править сообщение?
    need_edit = False
    # Если текст поменялся — редактируем
    if new_text != old_text:
        need_edit = True
    # Или если текст тот же, но мы закрываем и нужно убрать клавиатуру
    elif old_markup_present and event["closed"]:
        need_edit = True

    if not need_edit:
        logger.debug("Содержание и клавиатура не изменились, правка не требуется (event %s)", event_id)
        return

    # Подготовка reply_markup: если закрыт — None (убираем клавиатуру), иначе стандартная клавиатура
    reply_markup = None if event["closed"] else get_keyboard(event_id)

    # Пытаемся отредактировать. Игнорируем Message is not modified.
    try:
        await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=reply_markup)
        logger.info("Сообщение event %s обновлено", event_id)
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            logger.info("Edit skipped: сообщение не изменилось (BadRequest: Message is not modified).")
            return
        # иначе логируем ошибку
        logger.exception("BadRequest при редактировании сообщения: %s", e)
        raise


# --- Запуск приложения (webhook) ---
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new_event", new_event))
    app.add_handler(CallbackQueryHandler(button_click))

    logger.info("Запуск webhook, URL=%s, PORT=%s", WEBHOOK_URL, PORT)
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)


if __name__ == "__main__":
    main()
