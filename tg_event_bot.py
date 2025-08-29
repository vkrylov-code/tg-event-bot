# --- Flask и Telegram Application ---
app = Flask(__name__)
telegram_app = Application.builder().token(TOKEN).build()

# --- Инициализация Telegram App ---
import asyncio
async def init_app():
    await telegram_app.initialize()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("new_event", new_event))
    telegram_app.add_handler(CommandHandler("list_events", list_events_handler))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    load_events()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")

asyncio.run(init_app())

# --- Webhook route с мгновенной обработкой апдейтов ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        if request.headers.get("content-type") == "application/json":
            update_data = request.get_json(force=True)
            update = Update.de_json(update_data, telegram_app.bot)
            
            # Обработка апдейта сразу
            asyncio.run(telegram_app.process_update(update))

            logger.info("✅ Update processed immediately")
            return "ok"
        return "Unsupported Media Type", 415
    except Exception as e:
        logger.exception("💥 Error in webhook: %s", e)
        return "Internal Server Error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, threaded=True)
