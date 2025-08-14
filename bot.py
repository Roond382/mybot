import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application, CallbackContext, CallbackQueryHandler,
    CommandHandler, MessageHandler, filters, ConversationHandler
)
from dotenv import load_dotenv

# ========== Загрузка переменных окружения ==========
load_dotenv()

# ========== Конфигурация ==========
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
CHANNEL_ID = int(os.getenv('CHANNEL_ID')) if os.getenv('CHANNEL_ID') else None
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID')) if os.getenv('ADMIN_CHAT_ID') else None
TIMEZONE = pytz.timezone('Europe/Moscow')
MAX_TEXT_LENGTH = 4000

if not all([TOKEN, CHANNEL_ID, ADMIN_CHAT_ID]):
    raise ValueError("Ключевые переменные Telegram не установлены!")

# ========== Константы ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

REQUEST_TYPES = {
    "congrat": {"name": "🎉 Поздравление", "icon": "🎉"},
    "announcement": {"name": "📢 Объявление", "icon": "📢"},
    "news": {"name": "🗞️ Новость от жителя", "icon": "🗞️"},
}

ANNOUNCE_SUBTYPES = {
    "sp": "Спрос и предложения",
    "n": "Потеряли/Нашли",
    "ride": "🚗 Попутка",  # Автопубликация
}

EXAMPLE_TEXTS = {
    "sender_name": "Иванов Виталий",
    "recipient_name": "коллектив детсада 'Солнышко'",
    "congrat": {
        "birthday": "С Днём рождения, дорогие сотрудники!",
        "wedding": "Поздравляем с золотой свадьбой!"
    },
    "announcement": {
        "sp": "Сдам 2-комнатную квартиру, 15000 руб, коммуналка включена",
        "n": "Куплю старые батарейки, платим деньги"
    }
}

(TYPE_SELECTION, SENDER_NAME_INPUT, RECIPIENT_NAME_INPUT, CONGRAT_TYPE_SELECTION,
 CONGRAT_TEXT_INPUT, ANNOUNCE_SUBTYPE_SELECTION, ANNOUNCE_TEXT_INPUT,
 PHONE_INPUT, WAIT_CENSOR_APPROVAL, NEWS_PHONE_INPUT, NEWS_TEXT_INPUT) = range(11)

# ========== Логирование ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ========== Глобальные переменные ==========
BOT_STATE = {'running': False, 'start_time': None}
application_lock = asyncio.Lock()
application: Application = None

# ========== База данных ==========
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect('db.sqlite', check_same_thread=False)
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
                publish_date DATE,
                congrat_type TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN phone_number TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approved_unpublished 
            ON applications(status, published_at) 
            WHERE status = 'approved' AND published_at IS NULL
        """)
        logger.info("База данных инициализирована.")

# ========== Вспомогательные функции ==========
def validate_phone(phone: str) -> bool:
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

async def load_bad_words() -> list:
    bad_words_file = 'bad_words.txt'
    default_words = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
    if os.path.exists(bad_words_file):
        try:
            with open(bad_words_file, 'r', encoding='utf-8') as f:
                return [word.strip() for line in f for word in line.split(',') if word.strip()]
        except Exception as e:
            logger.warning(f"Ошибка загрузки bad_words.txt: {e}")
    return default_words

async def check_text_for_bad_words(text: str) -> tuple[str, bool]:
    bad_words = await load_bad_words()
    censored = text
    has_bad = False
    for word in bad_words:
        if re.search(re.escape(word), censored, re.IGNORECASE):
            has_bad = True
            censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
    return censored, has_bad

# ========== Обработчики ==========
async def safe_reply_text(update: Update, text: str, **kwargs):
    try:
        await update.message.reply_text(text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

async def safe_edit_message_text(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка редактирования: {e}")

async def start_command(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)]
        for key, info in REQUEST_TYPES.items()
    ]
    await safe_reply_text(
        update,
        "👋 Здравствуйте!\nВыберите, что хотите отправить:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    request_type = query.data
    context.user_data["type"] = request_type

    if request_type == "congrat":
        await safe_edit_message_text(
            query,
            "Введите имя отправителя (например: Иванов Виталий):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return SENDER_NAME_INPUT
    elif request_type == "announcement":
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"announce_{key}")]
            for key, name in ANNOUNCE_SUBTYPES.items()
        ] + BACK_BUTTON
        await safe_edit_message_text(
            query,
            "Выберите подкатегорию:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ANNOUNCE_SUBTYPE_SELECTION
    elif request_type == "news":
        await safe_edit_message_text(
            query,
            "Введите ваш контактный телефон (формат: +7... или 8...):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return NEWS_PHONE_INPUT
    else:
        return ConversationHandler.END

async def handle_announce_subtype_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    subtype_key = query.data.replace("announce_", "")
    context.user_data["subtype"] = subtype_key

    example = EXAMPLE_TEXTS["announcement"].get(subtype_key, "")
    await safe_edit_message_text(
        query,
        f"Введите текст объявления (до {MAX_TEXT_LENGTH} символов).\nПример: {example}",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return ANNOUNCE_TEXT_INPUT

async def handle_announce_text_input(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update, f"Слишком длинный текст. Максимум: {MAX_TEXT_LENGTH} символов.")
        return ANNOUNCE_TEXT_INPUT

    censored_text, has_bad = await check_text_for_bad_words(text)
    if has_bad:
        await safe_reply_text(update, "❌ Ваше сообщение содержит запрещённые слова. Заявка не отправлена.")
        return ConversationHandler.END

    context.user_data["text"] = censored_text

    # Автопубликация для "Попутка"
    if context.user_data.get("subtype") == "ride":
        phone = context.user_data.get("phone")
        if not phone:
            await safe_reply_text(update, "Введите телефон:")
            return PHONE_INPUT

        try:
            message = f"🚗 <b>Попутка</b>\n{censored_text}\n📞 <b>Телефон:</b> {phone}\n#ПопуткаНиколаевск"
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode="HTML"
            )
            await safe_reply_text(update, "✅ Ваша заявка опубликована!")
        except Exception as e:
            logger.error(f"Ошибка публикации попутки: {e}")
            await safe_reply_text(update, "❌ Не удалось опубликовать. Попробуйте позже.")
        return ConversationHandler.END

    # Для остальных — на модерацию
    await safe_reply_text(update, "Заявка отправлена на модерацию.")
    # ... (сохранение в БД и отправка админу)
    return ConversationHandler.END

# ========== Инициализация бота ==========
async def initialize_bot():
    global application
    async with application_lock:
        if application is not None:
            return
        application = Application.builder().token(TOKEN).build()
        # ... (добавление обработчиков)
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        logger.info("Бот инициализирован.")

# ========== FastAPI приложение ==========
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    init_db()
    await initialize_bot()
    scheduler = AsyncIOScheduler(
        jobstores={'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')},
        executors={'default': AsyncIOExecutor()},
        timezone=TIMEZONE
    )
    scheduler.add_job(check_pending_applications, 'interval', minutes=1)
    scheduler.start()
    BOT_STATE['running'] = True
    BOT_STATE['start_time'] = datetime.now(TIMEZONE)
    logger.info("FastAPI приложение запущено.")

@app.post("/telegram-webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    global application
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    if application is None:
        await initialize_bot()
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")
        raise HTTPException(status_code=500, detail="Internal error")

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)import os
import re
import sqlite3
import hashlib
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime
import pytz
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CallbackContext, CallbackQueryHandler,
    CommandHandler, MessageHandler, filters, ConversationHandler
)
from dotenv import load_dotenv

# ========== Загрузка переменных окружения ==========
load_dotenv()

# ========== Конфигурация ==========
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
CHANNEL_ID = int(os.getenv('CHANNEL_ID')) if os.getenv('CHANNEL_ID') else None
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID')) if os.getenv('ADMIN_CHAT_ID') else None
TIMEZONE = pytz.timezone('Europe/Moscow')
MAX_TEXT_LENGTH = 4000

# Проверка обязательных переменных
if not all([TOKEN, CHANNEL_ID, ADMIN_CHAT_ID]):
    raise ValueError("Ключевые переменные Telegram не установлены!")

# ========== Константы ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

# --- Типы заявок ---
REQUEST_TYPES = {
    "congrat": {"name": "🎉 Поздравление", "icon": "🎉"},
    "announcement": {"name": "📢 Объявление", "icon": "📢"},
    "news": {"name": "🗞️ Новость от жителя", "icon": "🗞️"},
    "carpool": {"name": "🚗 Попутка", "icon": "🚗"},  # Добавлено в главное меню
}

# --- Подтипы попутки ---
CARPOOL_SUBTYPES = {
    "has_seats": "Есть места",
    "needs_seats": "Нужны места"
}

# --- Примеры текстов ---
EXAMPLE_TEXTS = {
    "sender_name": "Иванов Виталий",
    "recipient_name": "коллектив детсада 'Солнышко'",
    "congrat": {
        "birthday": "С Днём рождения, дорогие сотрудники!",
        "wedding": "Поздравляем с золотой свадьбой!"
    },
    "announcement": {
        "sp": "Сдам 2-комнатную квартиру, 15000 руб, коммуналка включена",
        "n": "Куплю старые батарейки, платим деньги"
    },
    "carpool": {
        "has_seats": "10.08 еду в Хабаровск. 2 места. Выезд в 9:00",
        "needs_seats": "Ищу попутку в Николаевск на 12.08"
    }
}

# --- Состояния ---
TYPE_SELECTION = "TYPE_SELECTION"
SENDER_NAME_INPUT = "SENDER_NAME_INPUT"
RECIPIENT_NAME_INPUT = "RECIPIENT_NAME_INPUT"
CONGRAT_TEXT_INPUT = "CONGRAT_TEXT_INPUT"
ANNOUNCE_SUBTYPE = "ANNOUNCE_SUBTYPE"
ANNOUNCE_TEXT_INPUT = "ANNOUNCE_TEXT_INPUT"
NEWS_PHONE_INPUT = "NEWS_PHONE_INPUT"
NEWS_TEXT_INPUT = "NEWS_TEXT_INPUT"
CARPOOL_SUBTYPE = "CARPOOL_SUBTYPE"
CARPOOL_TEXT_INPUT = "CARPOOL_TEXT_INPUT"
CARPOOL_PHONE_INPUT = "CARPOOL_PHONE_INPUT"

# ========== Настройка логирования ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== Глобальное состояние ==========
BOT_STATE = {'running': False, 'start_time': None, 'last_activity': None}
application_lock = asyncio.Lock()
application: Optional[Application] = None

# ========== База данных ==========
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect('db.sqlite', check_same_thread=False)
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
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN phone_number TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        logger.info("База данных инициализирована.")

# ========== Вспомогательные функции ==========
def validate_phone(phone: str) -> bool:
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

async def load_bad_words() -> List[str]:
    bad_words_file = Path("bad_words.txt")
    default_words = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
    if bad_words_file.exists():
        try:
            with bad_words_file.open('r', encoding='utf-8') as f:
                return [word.strip() for line in f for word in line.split(',') if word.strip()]
        except Exception as e:
            logger.warning(f"Ошибка загрузки bad_words.txt: {e}")
    return default_words

async def censor_text(text: str) -> tuple[str, bool]:
    bad_words = await load_bad_words()
    censored = text
    has_bad = False
    for word in bad_words:
        if re.search(re.escape(word), censored, re.IGNORECASE):
            has_bad = True
            censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
    return censored, has_bad

# ========== Обработчики ==========
async def safe_reply_text(update: Update, text: str, **kwargs):
    try:
        await update.message.reply_text(text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

async def safe_edit_message_text(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения: {e}")

async def start_command(update: Update, context: CallbackContext) -> str:
    keyboard = [
        [InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)]
        for key, info in REQUEST_TYPES.items()
    ]
    await safe_reply_text(
        update,
        "👋 Здравствуйте!\nВыберите, что хотите отправить:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: CallbackContext) -> str:
    query = update.callback_query
    await query.answer()
    request_type = query.data
    context.user_data["type"] = request_type

    if request_type == "carpool":
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"carpool_{key}")]
            for key, name in CARPOOL_SUBTYPES.items()
        ] + BACK_BUTTON
        await safe_edit_message_text(
            query,
            "Выберите тип попутки:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CARPOOL_SUBTYPE
    elif request_type == "news":
        await safe_edit_message_text(
            query,
            "Введите ваш контактный телефон (формат: +7... или 8...):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return NEWS_PHONE_INPUT
    else:
        await safe_edit_message_text(
            query,
            "Временно недоступно. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return ConversationHandler.END

async def handle_carpool_subtype(update: Update, context: CallbackContext) -> str:
    query = update.callback_query
    await query.answer()
    subtype_key = query.data.replace("carpool_", "")
    context.user_data["subtype"] = subtype_key

    example = EXAMPLE_TEXTS["carpool"].get(subtype_key, "")
    await safe_edit_message_text(
        query,
        f"Введите текст заявки (до {MAX_TEXT_LENGTH} символов).\nПример: {example}",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return CARPOOL_TEXT_INPUT

async def handle_carpool_text(update: Update, context: CallbackContext) -> str:
    text = update.message.text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update, f"Слишком длинный текст. Максимум: {MAX_TEXT_LENGTH} символов.")
        return CARPOOL_TEXT_INPUT

    censored_text, has_bad = await censor_text(text)
    if has_bad:
        await safe_reply_text(update, "❌ Ваше сообщение содержит запрещённые слова. Заявка не отправлена.")
        return ConversationHandler.END

    phone = context.user_data.get("phone")
    if not phone:
        await safe_reply_text(update, "Введите телефон:")
        return CARPOOL_PHONE_INPUT

    # Автопубликация
    try:
        message = f"🚗 <b>Попутка</b>\n{censored_text}\n📞 <b>Телефон:</b> {phone}\n#ПопуткаНиколаевск"
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            parse_mode="HTML"
        )
        await safe_reply_text(update, "✅ Ваша заявка опубликована!")
    except Exception as e:
        logger.error(f"Ошибка публикации попутки: {e}")
        await safe_reply_text(update, "❌ Не удалось опубликовать. Попробуйте позже.")

    context.user_data.clear()
    return ConversationHandler.END

async def pending_command(update: Update, context: CallbackContext) -> None:
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await safe_reply_text(update, "❌ Эта команда доступна только администратору.")
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
        await safe_reply_text(update, "📭 Нет заявок в очереди.")
        return

    for app in apps:
        app_text = f"#{app['id']} ({app['type']})\n{app['text']}\n📞 {app['phone_number']}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app['id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app['id']}")
        ]])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=app_text,
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Не удалось отправить заявку #{app['id']}: {e}")

# ========== Инициализация бота ==========
async def initialize_bot():
    global application
    async with application_lock:
        if application is not None:
            return

        application = Application.builder().token(TOKEN).build()

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start_command)],
            states={
                TYPE_SELECTION: [CallbackQueryHandler(handle_type_selection)],
                CARPOOL_SUBTYPE: [CallbackQueryHandler(handle_carpool_subtype)],
                CARPOOL_TEXT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_text)],
                "back_to_start": [CallbackQueryHandler(start_command, pattern="^back_to_start$")]
            },
            fallbacks=[CommandHandler("cancel", lambda u, c: None)],
            allow_reentry=True
        )

        application.add_handler(conv_handler)
        application.add_handler(CommandHandler("pending", pending_command))

        await application.initialize()
        await application.start()
        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET
        )
        logger.info("Бот инициализирован и установлен вебхук.")

# ========== FastAPI приложение ==========
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    init_db()
    await initialize_bot()
    scheduler = AsyncIOScheduler(
        jobstores={'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')},
        executors={'default': AsyncIOExecutor()},
        timezone=TIMEZONE
    )
    scheduler.add_job(
        pending_command,
        'interval',
        minutes=5,
        args=[None, None],
        id='check_pending'
    )
    scheduler.start()
    BOT_STATE['running'] = True
    BOT_STATE['start_time'] = datetime.now(TIMEZONE)
    logger.info("FastAPI приложение запущено.")

@app.post("/webhook")
async def webhook(request: Request):
    global application
    if application is None:
        await initialize_bot()

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}")
        return JSONResponse(status_code=500, content={"status": "error"})

@app.get("/")
async def root():
    return {"status": "running", "bot": BOT_STATE}

# ========== Запуск (для локального тестирования) ==========
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

