@app.on_event("startup")
async def startup_event():
    """Запуск бота при старте FastAPI."""
    global application
    try:
        if not TOKEN:
            raise ValueError("Токен бота не задан!")
        logger.info("Инициализация базы данных...")
        init_db()
        logger.info("Создание экземпляра Application...")
        application = Application.builder().token(TOKEN).build()
        logger.info("Настройка обработчиков...")
        setup_handlers(application)

        # Инициализация планировщика
        scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        scheduler.add_job(
            check_pending_applications,
            'interval',
            minutes=5,
            args=[application],
            misfire_grace_time=300
        )
        scheduler.add_job(
            cleanup_old_applications,
            'cron',
            day='*/7',
            args=[30],
            timezone=TIMEZONE
        )
        scheduler.start()

        # Настройка вебхука или polling
        if WEBHOOK_URL:
            logger.info(f"Установка вебхука на {WEBHOOK_URL}/webhook")
            await application.initialize()
            await application.start()
            await application.bot.set_webhook(
                url=f"{WEBHOOK_URL}/webhook",
                allowed_updates=Update.ALL_TYPES,
                secret_token=WEBHOOK_SECRET
            )
        else:
            logger.info("Запуск в режиме polling...")
            asyncio.create_task(application.run_polling())

        BOT_STATE['running'] = True
        BOT_STATE['start_time'] = datetime.now(TIMEZONE)

        # Отправляем админу сообщение о запуске
        if ADMIN_CHAT_ID:
            await send_bot_status(context.bot, "🟢 Бот успешно запущен!", force_send=True)
        else:
            logger.warning("ID админа не задан — уведомление не отправлено.")

        logger.info("Бот успешно запущен")
    except Exception as e:
        logger.critical(f"Ошибка запуска бота: {e}", exc_info=True)
        BOT_STATE['running'] = False
        raise
