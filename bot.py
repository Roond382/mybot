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
from typing import Optional, List
import aiofiles

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
MAX_CONGRAT_TEXT_LENGTH = 500
MAX_ANNOUNCE_NEWS_TEXT_LENGTH = 300
CHANNEL_NAME = "Небольшой Мир: Николаевск"
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50

if not all([TOKEN, CHANNEL_ID, ADMIN_CHAT_ID]):
    raise ValueError("Ключевые переменные Telegram не установлены!")

# ========== Константы ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
DB_FILE = 'db.sqlite'

# ========== ПРИМЕРЫ ТЕКСТОВ ==========
EXAMPLE_TEXTS = {
    "sender_name": "Иванов Виталий",
    "recipient_name": "коллектив детсада 'Солнышко'",
    "congrat": {
        "birthday": "С Днём рождения, дорогие сотрудники!",
        "wedding": "Поздравляем с золотой свадьбой!"
    },
    "announcement": {
        "ride": "10.02 еду в Волгоград. 2 места. Выезд в 8:00",
        "demand_offer": "Ищу работу водителя. Опыт 5 лет.",
        "lost": "Найден ключ у магазина 'Продукты'. Опознать по брелку."
    },
    "news": "15.01 в нашем городе открыли новую детскую площадку!"
}

# ========== ПРАЗДНИКИ ==========
HOLIDAYS = {
    "🎄 Новый год": "01-01",
    "🪖 23 Февраля": "02-23",
    "💐 8 Марта": "03-08",
    "🏅 9 Мая": "05-09",
    "🇷🇺 12 Июня": "06-12",
    "🤝 4 Ноября": "11-04"
}

# ========== ШАБЛОНЫ ПОЗДРАВЛЕНИЙ ==========
HOLIDAY_TEMPLATES = {
    "🎄 Новый год": "С Новым годом! ✨ Пусть каждый день нового года будет наполнен радостью, теплом и верой в лучшее!",
    "🪖 23 Февраля": "С Днём защитника Отечества! 💪 Крепкого здоровья, силы духа и мирного неба над головой!",
    "💐 8 Марта": "С 8 Марта! 🌸 Пусть в вашей жизни будет много тепла, красоты и счастливых мгновений!",
    "🏅 9 Мая": "С Днём Победы! 🇷🇺 Мы помним подвиг и радуемся великой Победе! Пусть в вашем доме всегда будет мир, радость и свет!",
    "🇷🇺 12 Июня": "С Днём России! ❤️ Желаем гордости за нашу страну, благополучия и уверенности в будущем!",
    "🤝 4 Ноября": "С Днём народного единства! 🤝 Пусть в вашей жизни будет согласие, доброта и взаимопонимание!"
}

# ========== ТИПЫ ЗАПРОСОВ ==========
REQUEST_TYPES = {
    "congrat": {"name": "🎉 Поздравление", "icon": "🎉"},
    "announcement": {"name": "📢 Объявление", "icon": "📢"},
    "news": {"name": "🗞️ Новость от жителя", "icon": "🗞️"}
}

# ========== ПОДТИПЫ ОБЪЯВЛЕНИЙ ==========
ANNOUNCE_SUBTYPES = {
    "ride": "🚗 Попутка",
    "demand_offer": "🤝 Спрос и предложения",
    "lost": "🔍 Потеряли/Нашли"
}

# ========== СОСТОЯНИЯ ДИАЛОГА ==========
(TYPE_SELECTION,
 SENDER_NAME_INPUT,
 RECIPIENT_NAME_INPUT,
 CONGRAT_HOLIDAY_CHOICE,
 CUSTOM_CONGRAT_MESSAGE_INPUT,
 CONGRAT_DATE_CHOICE,
 CONGRAT_DATE_INPUT,
 ANNOUNCE_SUBTYPE_SELECTION,
 ANNOUNCE_TEXT_INPUT,
 PHONE_INPUT,
 NEWS_PHONE_INPUT,
 NEWS_TEXT_INPUT,
 WAIT_CENSOR_APPROVAL,
 RIDE_FROM_INPUT,
 RIDE_TO_INPUT,
 RIDE_DATE_INPUT,
 RIDE_SEATS_INPUT,
 RIDE_PHONE_INPUT) = range(19)

# ========== Логирование ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ========== Глобальные переменные ==========
BOT_STATE = {'running': False, 'start_time': None, 'last_activity': None}
application_lock = asyncio.Lock()
application: Optional[Application] = None

# ========== База данных ==========
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
def validate_name(name: str) -> bool:
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-]+$', name)) and 2 <= len(name.strip()) <= MAX_NAME_LENGTH

def validate_phone(phone: str) -> bool:
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

async def load_bad_words() -> List[str]:
    try:
        async with aiofiles.open(BAD_WORDS_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
        return [word.strip() for line in content.splitlines() for word in line.split(',') if word.strip()]
    except FileNotFoundError:
        logger.warning("Файл с запрещенными словами не найден. Используются значения по умолчанию.")
        return DEFAULT_BAD_WORDS
    except Exception as e:
        logger.error(f"Ошибка загрузки bad_words.txt: {e}")
        return DEFAULT_BAD_WORDS

async def censor_text(text: str) -> tuple[str, bool]:
    bad_words = await load_bad_words()
    censored = text
    has_bad = False
    for word in bad_words:
        if re.search(re.escape(word), censored, re.IGNORECASE):
            has_bad = True
            censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
    return censored, has_bad

def can_submit_request(user_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute("""
            SELECT COUNT(*) FROM applications 
            WHERE user_id = ? AND created_at > datetime('now', '-1 hour')
        """, (user_id,))
        count = cur.fetchone()[0]
        return count < 5

# ========== Функции для базы данных ==========
def add_application(data: dict) -> Optional[int]:
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO applications (user_id, username, type, subtype, from_name, to_name, text, photo_id, phone_number, publish_date, congrat_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['user_id'],
                data.get('username'),
                data['type'],
                data.get('subtype'),
                data.get('from_name'),
                data.get('to_name'),
                data['text'],
                data.get('photo_id'),
                data.get('phone_number'),
                data.get('publish_date'),
                data.get('congrat_type')
            ))
            app_id = cur.lastrowid
            conn.commit()
            return app_id
    except Exception as e:
        logger.error(f"Ошибка добавления заявки: {e}", exc_info=True)
        return None

def get_application_details(app_id: int) -> Optional[sqlite3.Row]:
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"Ошибка получения заявки #{app_id}: {e}", exc_info=True)
        return None

def get_approved_unpublished_applications() -> List[sqlite3.Row]:
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM applications 
                WHERE status = 'approved' AND published_at IS NULL
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Ошибка получения заявок для публикации: {e}", exc_info=True)
        return []

def update_application_status(app_id: int, status: str) -> bool:
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления статуса заявки #{app_id}: {e}", exc_info=True)
        return False

def mark_application_as_published(app_id: int) -> bool:
    try:
        with get_db_connection() as conn:
            conn.execute("""
                UPDATE applications
                SET published_at = CURRENT_TIMESTAMP, status = 'published'
                WHERE id = ?
            """, (app_id,))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка при обновлении статуса заявки #{app_id}: {e}", exc_info=True)
        return False

# ========== Обработчики ==========
async def safe_reply_text(update: Update, text: str, **kwargs):
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text=text, **kwargs)
        elif update.message:
            await update.message.reply_text(text=text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

async def safe_edit_message_text(query, text: str, **kwargs):
    try:
        await query.edit_message_text(text=text, **kwargs)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Ошибка редактирования сообщения: {e}")

async def start_command(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("🚗 Попутка", callback_data="carpool")],
        [InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)]
        for key, info in REQUEST_TYPES.items()
    ]
    await safe_reply_text(
        update,
        "👋 Здравствуйте!\nВыберите, что хотите отправить в канал:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    request_type = query.data
    context.user_data["type"] = request_type

    if request_type == "news":
        await safe_edit_message_text(
            query,
            "Введите ваш контактный телефон (формат: +7... или 8...):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return NEWS_PHONE_INPUT
    elif request_type == "congrat":
        await safe_edit_message_text(
            query,
            f"Введите ваше имя (например: *{EXAMPLE_TEXTS['sender_name']}*):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
            parse_mode="Markdown"
        )
        return SENDER_NAME_INPUT
    elif request_type == "announcement":
        keyboard = [
            [InlineKeyboardButton(subtype, callback_data=f"subtype_{key}")]
            for key, subtype in ANNOUNCE_SUBTYPES.items()
        ] + BACK_BUTTON
        await safe_edit_message_text(
            query,
            "Выберите тип объявления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ANNOUNCE_SUBTYPE_SELECTION
    return ConversationHandler.END

# ========== ОБРАБОТЧИКИ ПОПУТКИ ==========
async def handle_carpool_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["type"] = "announcement"
    context.user_data["subtype"] = "ride"

    keyboard = [
        [InlineKeyboardButton("Ищу попутчиков", callback_data="carpool_need")],
        [InlineKeyboardButton("Предлагаю поездку", callback_data="carpool_offer")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_edit_message_text(
        query,
        "Выберите тип поездки:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return RIDE_FROM_INPUT

async def handle_carpool_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["ride_type"] = "Ищу попутчиков" if query.data == "carpool_need" else "Предлагаю поездку"
    await safe_edit_message_text(
        query,
        "Откуда планируете поездку? (Например: Николаевск):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_TO_INPUT

async def handle_carpool_from(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_from"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Куда направляетесь? (Например: Хабаровск):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_DATE_INPUT

async def handle_carpool_to(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_to"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Когда планируете выезд? (Например: 15.08 в 10:00):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_SEATS_INPUT

async def handle_carpool_date(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_date"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Сколько мест доступно? (Введите число):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_PHONE_INPUT

async def handle_carpool_seats(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_seats"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Введите ваш контактный телефон (формат: +7... или 8...):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_PHONE_INPUT

async def handle_carpool_phone(update: Update, context: CallbackContext) -> int:
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await safe_reply_text(update, "Введите корректный номер телефона (например: +79610904569).")
        return RIDE_PHONE_INPUT

    context.user_data["phone"] = phone

    # Формируем сообщение
    ride_data = context.user_data
    text = (
        f"🚗 <b>{ride_data['ride_type']}</b>\n"
        f"📍 <b>Откуда:</b> {ride_data['ride_from']}\n"
        f"📍 <b>Куда:</b> {ride_data['ride_to']}\n"
        f"⏰ <b>Время:</b> {ride_data['ride_date']}\n"
        f"🪑 <b>Мест:</b> {ride_data['ride_seats']}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"#ПопуткаНиколаевск"
    )

    # Проверка на спам
    if not can_submit_request(update.effective_user.id):
        await safe_reply_text(update, "❌ Слишком много запросов. Попробуйте позже.")
        return ConversationHandler.END

    # Проверка на запрещённые слова
    censored_text, has_bad = await censor_text(text)
    if has_bad:
        context.user_data["censored_text"] = censored_text
        keyboard = [
            [InlineKeyboardButton("✅ Отправить", callback_data="accept_censor")],
            [InlineKeyboardButton("✏️ Изменить", callback_data="edit_censor")]
        ]
        await safe_reply_text(
            update,
            f"⚠️ Обнаружены запрещённые слова:\n{censored_text}\nПодтвердите отправку:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL

    # Автопубликация
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=censored_text,
            parse_mode="HTML"
        )
        await safe_reply_text(update, "✅ Ваша заявка опубликована!")
    except Exception as e:
        logger.error(f"Ошибка публикации попутки: {e}")
        await safe_reply_text(update, "❌ Не удалось опубликовать. Попробуйте позже.")

    context.user_data.clear()
    return ConversationHandler.END

# ========== ОБРАБОТЧИКИ ПОЗДРАВЛЕНИЙ И ОБЪЯВЛЕНИЙ ==========
# ... (остальные обработчики остаются как в оригинале)

# ========== ФУНКЦИЯ ПРОВЕРКИ И ПУБЛИКАЦИИ ЗАЯВОК ==========
async def check_pending_applications():
    global application
    if application is None:
        logger.warning("Application не инициализирован. Пропуск публикации.")
        return

    try:
        applications = get_approved_unpublished_applications()
        bot = application.bot

        for app in applications:
            try:
                if app['photo_id']:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=app['photo_id'],
                        caption=app['text'],
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=app['text'],
                        parse_mode="HTML"
                    )
                mark_application_as_published(app['id'])
                logger.info(f"Опубликовано сообщение #{app['id']}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ошибка обработки заявки #{app['id']}: {e}")
    except Exception as e:
        logger.error(f"Ошибка проверки заявок: {e}")

# ========== ОБРАБОТЧИКИ КНОПОК ==========
async def admin_approve_application(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_CHAT_ID:
        return
    try:
        app_id = int(query.data.split('_')[1])
        app_details = get_application_details(app_id)
        if not app_details:
            await safe_edit_message_text(query, "Заявка не найдена.")
            return
        if update_application_status(app_id, 'approved'):
            await safe_edit_message_text(query, f"Заявка #{app_id} одобрена.")
        else:
            await safe_edit_message_text(query, f"Ошибка одобрения заявки #{app_id}.")
    except Exception as e:
        logger.error(f"Ошибка при одобрении заявки: {e}")
        await safe_edit_message_text(query, "Произошла ошибка.")

async def admin_reject_application(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_CHAT_ID:
        return
    try:
        app_id = int(query.data.split('_')[1])
        if update_application_status(app_id, 'rejected'):
            await safe_edit_message_text(query, f"Заявка #{app_id} отклонена.")
        else:
            await safe_edit_message_text(query, f"Ошибка отклонения заявки #{app_id}.")
    except Exception as e:
        logger.error(f"Ошибка при отклонении заявки: {e}")
        await safe_edit_message_text(query, "Произошла ошибка.")

# ========== ДОПОЛНИТЕЛЬНЫЕ КОМАНДЫ ==========
async def help_command(update: Update, context: CallbackContext) -> None:
    help_text = (
        "ℹ️ *Помощь по боту*\n\n"
        "Этот бот предназначен для отправки заявок в группу *Небольшой Мир: Николаевск*.\n\n"
        "📌 *Как использовать:*\n"
        "1. Нажмите /start.\n"
        "2. Выберите тип заявки: Поздравление, Объявления или Новость от жителя.\n"
        "3. Следуйте инструкциям бота.\n"
        "4. Ваша заявка будет отправлена на модерацию (кроме попуток).\n\n"
        "Если у вас есть вопросы, обратитесь к администратору группы."
    )
    await safe_reply_text(update, help_text, parse_mode="Markdown")

async def pending_command(update: Update, context: CallbackContext) -> None:
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await safe_reply_text(update, "❌ Эта команда доступна только администратору.")
        return
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT id, type, subtype, from_name, to_name, text, photo_id, phone_number
                FROM applications
                WHERE status = 'pending'
                ORDER BY created_at DESC
                LIMIT 10
            """)
            apps = cur.fetchall()
        if not apps:
            await safe_reply_text(update, "📭 Нет заявок, ожидающих модерации.")
            return
        for app in apps:
            app_type = REQUEST_TYPES.get(app['type'], {}).get('name', 'Заявка')
            message = f"#{app['id']} ({app_type})\n{app['text']}"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app['id']}"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app['id']}")]
            ])
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=message,
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Ошибка команды /pending: {e}")
        await safe_reply_text(update, "❌ Ошибка при получении заявок.")

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
        logger.error(f"Ошибка вебхука: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status")
async def status():
    """Статус бота."""
    return {
        "status": "running",
        "uptime": "1д 2ч 30м",
        "bot_initialized": application is not None
    }

@app.get("/")
async def root():
    return {"message": "Telegram Bot Webhook Listener"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
