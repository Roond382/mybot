import os
import re
import sqlite3
import hashlib
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict, List
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
)
from telegram.ext import (
    Application, CallbackQueryHandler, ContextTypes, CommandHandler,
    MessageHandler, filters, ConversationHandler
)
from telegram.error import Conflict, RetryAfter, TimedOut, NetworkError

# --- 1. НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_final.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- 2. КОНФИГУРАЦИЯ ---
class Config:
    def __init__(self):
        BASE_DIR = Path(__file__).resolve().parent
        load_dotenv(BASE_DIR / ".env")
        self.BASE_DIR = BASE_DIR
        self.DB_FILE = BASE_DIR / "db.sqlite"
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))
        self.CHANNEL_ID = os.getenv("CHANNEL_ID")
        self.BAD_WORDS_FILE = BASE_DIR / "bad_words.txt"
        self.DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
        self.WORKING_HOURS = (0, 23)
        self.WORK_ON_WEEKENDS = True
        self.MAX_TEXT_LENGTH = 4000

        if not all([self.TOKEN, self.ADMIN_CHAT_ID, self.CHANNEL_ID]):
            raise ValueError("Ключевые переменные Telegram не установлены!")

try:
    config = Config()
except ValueError as e:
    logger.critical(f"Критическая ошибка конфигурации: {e}")
    exit(1)

# --- 3. УПРАВЛЕНИЕ БАЗОЙ ДАННЫХ ---
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                type TEXT NOT NULL,
                subtype TEXT,
                from_name TEXT,
                to_name TEXT,
                text TEXT,
                photo_id TEXT,
                phone_number TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMP,
                publish_date DATE
            )
        """)
        # Создаем индекс для быстрого поиска заявок на модерации
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending 
            ON applications (status) WHERE status = 'pending'
        """)
        # Добавляем поле phone_number, если его нет
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN phone_number TEXT")
            conn.commit()
            logger.info("Добавлено поле phone_number в таблицу applications")
        except sqlite3.OperationalError:
            pass  # Поле уже существует
        logger.info("База данных инициализирована.")

# --- 4. КОНСТАНТЫ И ТИПЫ ЗАЯВОК ---
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

REQUEST_TYPES = {
    "congrat": {"name": "🎉 Поздравление", "icon": "🎉"},
    "announcement": {"name": "📢 Объявление", "icon": "📢"},
    "news": {"name": "🗞️ Новость от жителя", "icon": "🗞️"},
    "carpool": {"name": "🚗 Попутка", "icon": "🚗"},
}

# Подтипы для "Попутка" (без эмодзи внутри)
CARPOOL_SUBTYPES = {
    "has_seats": "Есть места",
    "needs_seats": "Нужны места"
}

EXAMPLE_TEXTS = {
    "carpool": {
        "has_seats": "10.08 еду в Хабаровск. 2 места. Выезд в 9:00",
        "needs_seats": "Ищу попутку в Николаевск на 12.08"
    }
}

# --- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def validate_phone(phone: str) -> bool:
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

async def load_bad_words() -> List[str]:
    try:
        async with open(config.BAD_WORDS_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
        return [word.strip() for line in content.splitlines() for word in line.split(',') if word.strip()]
    except FileNotFoundError:
        logger.warning("Файл bad_words.txt не найден. Используются значения по умолчанию.")
        return config.DEFAULT_BAD_WORDS
    except Exception as e:
        logger.error(f"Ошибка загрузки bad_words.txt: {e}")
        return config.DEFAULT_BAD_WORDS

async def censor_text(text: str) -> tuple[str, bool]:
    bad_words = await load_bad_words()
    censored = text
    has_bad = False
    for word in bad_words:
        try:
            if re.search(re.escape(word), censored, re.IGNORECASE):
                has_bad = True
                censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
        except re.error:
            continue
    return censored, has_bad

# --- 6. ОБРАБОТЧИКИ ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)]
        for key, info in REQUEST_TYPES.items()
    ]
    await update.message.reply_text(
        "👋 Здравствуйте!\nВыберите, что хотите отправить:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return "TYPE_SELECTION"

async def handle_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    request_type = query.data
    context.user_data["type"] = request_type

    if request_type == "carpool":
        # Показываем подтипы попутки (без эмодзи)
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"carpool_{key}")]
            for key, name in CARPOOL_SUBTYPES.items()
        ] + BACK_BUTTON
        await query.edit_message_text(
            "Выберите тип попутки:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return "CARPOOL_SUBTYPE"
    elif request_type == "news":
        await query.edit_message_text(
            "Введите ваш контактный телефон (формат: +7... или 8...):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return "NEWS_PHONE_INPUT"
    else:
        await query.edit_message_text("Временно недоступно. Попробуйте позже.", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return ConversationHandler.END

async def handle_carpool_subtype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    subtype_key = query.data.replace("carpool_", "")
    context.user_data["subtype"] = subtype_key

    example = EXAMPLE_TEXTS["carpool"].get(subtype_key, "")
    await query.edit_message_text(
        f"Введите текст заявки (до {config.MAX_TEXT_LENGTH} символов).\nПример: {example}",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return "CARPOOL_TEXT_INPUT"

async def handle_carpool_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if len(text) > config.MAX_TEXT_LENGTH:
        await update.message.reply_text(f"Слишком длинный текст. Максимум: {config.MAX_TEXT_LENGTH} символов.")
        return "CARPOOL_TEXT_INPUT"

    censored_text, has_bad = await censor_text(text)
    if has_bad:
        await update.message.reply_text("❌ Ваше сообщение содержит запрещённые слова. Заявка не отправлена.")
        return ConversationHandler.END

    phone = context.user_data.get("phone")
    if not phone:
        await update.message.reply_text("Введите телефон:")
        return "CARPOOL_PHONE_INPUT"

    # Автопубликация
    try:
        message = f"🚗 <b>Попутка</b>\n{censored_text}\n📞 <b>Телефон:</b> {phone}\n#ПопуткаНиколаевск"
        await context.bot.send_message(
            chat_id=config.CHANNEL_ID,
            text=message,
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Ваша заявка опубликована!")
    except Exception as e:
        logger.error(f"Ошибка публикации попутки: {e}")
        await update.message.reply_text("❌ Не удалось опубликовать. Попробуйте позже.")

    context.user_data.clear()
    return ConversationHandler.END

# --- 7. КОМАНДА /pending (восстановление заявок) ---
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != config.ADMIN_CHAT_ID:
        return

    with get_db_connection() as conn:
        cur = conn.execute("""
            SELECT id, type, text, phone_number 
            FROM applications 
            WHERE status = 'pending' 
            ORDER BY created_at DESC
        """)
        apps = cur.fetchall()

    if not apps:
        await update.message.reply_text("📭 Нет заявок в очереди.")
        return

    for app in apps:
        app_text = f"#{app['id']} ({app['type']})\n{app['text']}\n📞 {app['phone_number']}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app['id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app['id']}")
        ]])
        try:
            await context.bot.send_message(
                chat_id=config.ADMIN_CHAT_ID,
                text=app_text,
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Не удалось отправить заявку #{app['id']}: {e}")

# --- 8. ЗАПУСК БОТА ---
def main():
    init_db()

    app = (
        Application.builder()
        .token(config.TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            "TYPE_SELECTION": [CallbackQueryHandler(handle_type_selection)],
            "CARPOOL_SUBTYPE": [CallbackQueryHandler(handle_carpool_subtype)],
            "CARPOOL_TEXT_INPUT": [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_text)],
            "CARPOOL_PHONE_INPUT": [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: handle_carpool_text(u, c))],
            "NEWS_PHONE_INPUT": [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None)],
            "back_to_start": [CallbackQueryHandler(start_command, pattern="^back_to_start$")]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: None)],
        allow_reentry=True
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("pending", pending_command))

    # Повторная отправка заявок каждые 5 минут
    job_queue = app.job_queue
    job_queue.run_repeating(lambda c: pending_command(Update(0, None), c), interval=300, first=10)

    logger.info("🚀 Бот запущен. Используйте /pending для восстановления заявок.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except Conflict:
        logger.error("❌ Бот уже запущен.")
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка: {e}", exc_info=True)
