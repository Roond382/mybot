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
from typing import Optional, List, Set, Dict, Tuple
import aiofiles
from concurrent.futures import ThreadPoolExecutor

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
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50
CONVERSATION_TIMEOUT_MINUTES = 15

# ========== НОВАЯ ФИЧА: Автопубликация попуток ==========
AUTO_PUBLISH_CARPOOL = os.getenv('AUTO_PUBLISH_CARPOOL', '').lower() == 'true'

# ========== Проверка обязательных переменных ==========
missing_vars = []
if not TOKEN:
    missing_vars.append("TELEGRAM_TOKEN")
if not CHANNEL_ID:
    missing_vars.append("CHANNEL_ID")
if not ADMIN_CHAT_ID:
    missing_vars.append("ADMIN_CHAT_ID")
if missing_vars:
    raise ValueError(f"Ключевые переменные окружения не установлены: {', '.join(missing_vars)}")

# ========== Константы ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
DB_FILE = 'db.sqlite'
NEWS_CHANNEL_LINK = "https://t.me/nb_mir_nikolaevsk"
NEWS_CHANNEL_TEXT = "Небольшой Мир: Николаевск"

# ========== Тексты, шаблоны и типы ==========
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

HOLIDAYS = {
    "🎄 Новый год": "01-01",
    "🪖 23 Февраля": "02-23",
    "💐 8 Марта": "03-08",
    "🏅 9 Мая": "05-09",
    "🇷🇺 12 Июня": "06-12",
    "🤝 4 Ноября": "11-04"
}

HOLIDAY_TEMPLATES = {
    "🎄 Новый год": "С Новым годом! ✨ Пусть каждый день нового года будет наполнен радостью, теплом и верой в лучшее!",
    "🪖 23 Февраля": "С Днём защитника Отечества! 💪 Крепкого здоровья, силы духа и мирного неба над головой!",
    "💐 8 Марта": "С 8 Марта! 🌸 Пусть в вашей жизни будет много тепла, красоты и счастливых мгновений!",
    "🏅 9 Мая": "С Днём Победы! 🇷🇺 Мы помним подвиг и радуемся великой Победе! Пусть в вашем доме всегда будет мир, радость и свет!",
    "🇷🇺 12 Июня": "С Днём России! ❤️ Желаем гордости за нашу страну, благополучия и уверенности в будущем!",
    "🤝 4 Ноября": "С Днём народного единства! 🤝 Пусть в вашей жизни будет согласие, доброта и взаимопонимание!"
}

REQUEST_TYPES = {
    "congrat": {"name": "🎉 Поздравление", "icon": "🎉"},
    "announcement": {"name": "📢 Объявление", "icon": "📢"},
    "news": {"name": "🗞️ Новость от жителя", "icon": "🗞️"}
}

ANNOUNCE_SUBTYPES = {
    "demand_offer": "🤝 Спрос и предложения",
    "lost": "🔍 Потеряли/Нашли"
}

# ========== Состояния диалога ==========
(TYPE_SELECTION, SENDER_NAME_INPUT, RECIPIENT_NAME_INPUT, CONGRAT_HOLIDAY_CHOICE,
 CUSTOM_CONGRAT_MESSAGE_INPUT, CONGRAT_DATE_CHOICE, CONGRAT_DATE_INPUT,
 ANNOUNCE_SUBTYPE_SELECTION, ANNOUNCE_TEXT_INPUT, PHONE_INPUT, NEWS_PHONE_INPUT,
 NEWS_TEXT_INPUT, WAIT_CENSOR_APPROVAL, RIDE_FROM_INPUT, RIDE_TO_INPUT,
 RIDE_DATE_INPUT, RIDE_SEATS_INPUT, RIDE_PHONE_INPUT, CARPOOL_SUBTYPE_SELECTION) = range(19)

# ========== Логирование ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ========== Глобальные переменные ==========
application_lock = asyncio.Lock()
application: Optional[Application] = None
bad_words_cache: Set[str] = set()
db_executor = ThreadPoolExecutor(max_workers=5)

# ========== База данных (оптимизированная версия) ==========
def run_in_executor(func, *args):
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(db_executor, func, *args)

def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db_sync():
    with _get_db_connection() as conn:
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
                congrat_type TEXT,
                ride_from TEXT,
                ride_to TEXT,
                ride_date TEXT,
                ride_seats TEXT,
                original_link TEXT
            )
        """)
        # Проверка и добавление отсутствующих колонок
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(applications)")
        columns = {row[1] for row in cursor.fetchall()}
        if 'photo_id' not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN photo_id TEXT")
        if 'ride_from' not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN ride_from TEXT")
        if 'ride_to' not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN ride_to TEXT")
        if 'ride_date' not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN ride_date TEXT")
        if 'ride_seats' not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN ride_seats TEXT")
        if 'original_link' not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN original_link TEXT")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approved_unpublished 
            ON applications(status, published_at) 
            WHERE status = 'approved' AND published_at IS NULL
        """)
        conn.commit()
        logger.info("База данных инициализирована.")

async def init_db():
    await run_in_executor(_init_db_sync)

def _db_execute_sync(query: str, params: tuple = ()):
    with _get_db_connection() as conn:
        conn.execute(query, params)
        conn.commit()

def _db_fetch_one_sync(query: str, params: tuple = ()) -> Optional[Dict]:
    with _get_db_connection() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

def _db_fetch_all_sync(query: str, params: tuple = ()) -> List[Dict]:
    with _get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

def _add_application_sync(data: dict) -> Optional[int]:
    with _get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO applications (
                user_id, username, type, subtype, from_name, to_name, 
                text, photo_id, phone_number, publish_date, congrat_type,
                ride_from, ride_to, ride_date, ride_seats, original_link
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['user_id'], data.get('username'), data['type'], data.get('subtype'),
            data.get('from_name'), data.get('to_name'), data['text'], data.get('photo_id'),
            data.get('phone_number'), data.get('publish_date'), data.get('congrat_type'),
            data.get('ride_from'), data.get('ride_to'), data.get('ride_date'), data.get('ride_seats'),
            data.get('original_link')
        ))
        app_id = cur.lastrowid
        conn.commit()
        return app_id

async def add_application(data: dict) -> Optional[int]:
    return await run_in_executor(_add_application_sync, data)

async def get_application_details(app_id: int) -> Optional[Dict]:
    return await run_in_executor(_db_fetch_one_sync, "SELECT * FROM applications WHERE id = ?", (app_id,))

async def get_approved_unpublished_applications() -> List[Dict]:
    return await run_in_executor(_db_fetch_all_sync, """
        SELECT * FROM applications 
        WHERE status = 'approved' AND published_at IS NULL
    """)

async def update_application_status(app_id: int, status: str) -> bool:
    try:
        await run_in_executor(
            _db_execute_sync,
            "UPDATE applications SET status = ? WHERE id = ?",
            (status, app_id)
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления статуса заявки {app_id}: {e}")
        return False)

async def mark_application_as_published(app_id: int):
    await run_in_executor(_db_execute_sync, """
        UPDATE applications 
        SET published_at = CURRENT_TIMESTAMP, status = 'published' 
        WHERE id = ?
    """, (app_id,))

async def can_submit_request(user_id: int) -> bool:
    row = await run_in_executor(_db_fetch_one_sync, """
        SELECT COUNT(*) as count 
        FROM applications 
        WHERE user_id = ? AND created_at > datetime('now', '-1 hour')
    """, (user_id,))
    return row['count'] < 5 if row else True

# ========== Вспомогательные функции ==========
def validate_name(name: str) -> bool:
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\'\"\-]+$', name)) and 2 <= len(name.strip()) <= MAX_NAME_LENGTH

def validate_phone(phone: str) -> bool:
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

async def load_bad_words():
    global bad_words_cache
    try:
        async with aiofiles.open(BAD_WORDS_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
        words = {word.strip().lower() for line in content.splitlines() for word in line.split(',') if word.strip()}
        bad_words_cache = words
        logger.info(f"Загружено {len(bad_words_cache)} запрещенных слов.")
    except FileNotFoundError:
        logger.warning("Файл с запрещенными словами не найден. Используются значения по умолчанию.")
        bad_words_cache = {word.lower() for word in DEFAULT_BAD_WORDS}
    except Exception as e:
        logger.error(f"Ошибка загрузки bad_words.txt: {e}")
        bad_words_cache = {word.lower() for word in DEFAULT_BAD_WORDS}

def censor_text(text: str) -> Tuple[str, bool]:
    censored = text
    has_bad = False
    for word in bad_words_cache:
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, censored, re.IGNORECASE):
            has_bad = True
            censored = re.sub(pattern, '***', censored, flags=re.IGNORECASE)
    return censored, has_bad

# ========== Безопасные обертки для отправки сообщений ==========
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

# ========== Основные обработчики команд ==========

async def start_command(update: Update, context: CallbackContext) -> int:
    """Обработчик команды /start — улучшенное приветственное сообщение."""
    context.user_data.clear()

    welcome_text = (
        "👋 *Добро пожаловать в бот канала «Что почем? Николаевск»!* 🛒\n\n"
        "Здесь вы можете:\n"
        "🎉 *Поздравить* кого-то с праздником\n"
        "🚗 *Оставить попутку*\n"
        "📢 *Разместить объявление* (работа, потеря, услуга)\n\n"
        "Выберите, что хотите сделать:"
    )

    keyboard = [
        [InlineKeyboardButton("🚗 Попутка", callback_data="carpool")],
        [InlineKeyboardButton(f"🎉 Поздравление", callback_data="congrat")],
        [InlineKeyboardButton(f"📢 Объявление", callback_data="announcement")],
        [InlineKeyboardButton(f"🗞️ Новость от жителя", callback_data="news")],
        [InlineKeyboardButton("ℹ️ Как это работает?", callback_data="help_inline")]
    ]

    await safe_reply_text(
        update,
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return TYPE_SELECTION

async def help_inline_handler(update: Update, context: CallbackContext) -> int:
    """Показывает краткую справку по использованию бота."""
    query = update.callback_query
    await query.answer()

    help_text = (
        "ℹ️ *Как отправить заявку?*\n\n"
        "1️⃣ Выберите тип: *Поздравление*, *Объявление* или *Новость*\n"
        "2️⃣ Следуйте подсказкам бота\n"
        "3️⃣ Введите текст и/или прикрепите фото\n"
        "4️⃣ Подтвердите отправку\n\n"
        "✅ После модерации ваша заявка появится в канале.\n"
        "📞 Для связи можно указать телефон.\n\n"
        "👉 Нажмите *«Вернуться»*, чтобы выбрать действие."
    )

    keyboard = [[InlineKeyboardButton("🔙 Вернуться", callback_data="back_to_start")]]
    
    await safe_edit_message_text(
        query,
        help_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return TYPE_SELECTION

async def cancel_command(update: Update, context: CallbackContext) -> int:
    await safe_reply_text(update, "Действие отменено. Чтобы начать заново, введите /start")
    context.user_data.clear()
    return ConversationHandler.END

async def back_to_start(update: Update, context: CallbackContext) -> int:
    await context.bot.answer_callback_query(update.callback_query.id)
    return await start_command(update, context)

async def handle_any_photo(update: Update, context: CallbackContext) -> None:
    if update.message and update.message.photo:
        context.user_data["photo_id"] = update.message.photo[-1].file_id
        await update.message.reply_text("📷 Фото получено. Продолжайте ввод данных.")

# ========== Обработчики попуток ==========
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
    return CARPOOL_SUBTYPE_SELECTION

async def handle_carpool_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["ride_type"] = "Ищу попутчиков" if query.data == "carpool_need" else "Предлагаю поездку"
    await safe_edit_message_text(
        query,
        "Откуда планируете поездку? (Например: Николаевск):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_FROM_INPUT

async def handle_carpool_from(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_from"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Куда направляетесь? (Например: Хабаровск):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_TO_INPUT

async def handle_carpool_to(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_to"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Когда планируете выезд? (Например: 15.08 в 10:00):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_DATE_INPUT

async def handle_carpool_date(update: Update, context: CallbackContext) -> int:
    context.user_data["ride_date"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Сколько мест доступно? (Введите число):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return RIDE_SEATS_INPUT

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
    if not await can_submit_request(update.effective_user.id):
        await safe_reply_text(update, "❌ Слишком много запросов. Попробуйте позже.")
        return ConversationHandler.END
    context.user_data["phone_number"] = phone
    ride_data = context.user_data
    text = (
        f"🚗 <b>{ride_data['ride_type']}</b>\n"
        f"📍 <b>Откуда:</b> {ride_data['ride_from']}\n"
        f"📍 <b>Куда:</b> {ride_data['ride_to']}\n"
        f"⏰ <b>Время:</b> {ride_data['ride_date']}\n"
        f"🪑 <b>Мест:</b> {ride_data['ride_seats']}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"#ЧтоПочёмНиколаевск"
    )
    censored_text, has_bad = censor_text(text)
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
    try:
        app_data = {
            'user_id': update.effective_user.id,
            'username': update.effective_user.username,
            'type': 'announcement',
            'subtype': 'ride',
            'text': censored_text,
            'phone_number': phone,
            'ride_from': ride_data['ride_from'],
            'ride_to': ride_data['ride_to'],
            'ride_date': ride_data['ride_date'],
            'ride_seats': ride_data['ride_seats'],
            'photo_id': context.user_data.get('photo_id'),
            'original_link': context.user_data.get('original_link')
        }
        if AUTO_PUBLISH_CARPOOL:
            app_id = await add_application(app_data)
            if app_id:
                success = await publish_to_channel(app_id, context.bot)
                if success:
                    await safe_reply_text(update, f"✅ Попутка сразу опубликована в канал!")
                    logger.info(f"Попутка #{app_id} опубликована без модерации.")
                else:
                    await safe_reply_text(update, "❌ Ошибка публикации. Заявка отправлена на модерацию.")
                    await notify_admin_new_application(context.bot, app_id)
            else:
                await safe_reply_text(update, "❌ Ошибка при создании заявки.")
        else:
            app_id = await add_application(app_data)
            if app_id:
                await notify_admin_new_application(context.bot, app_id)
                await safe_reply_text(update, f"✅ Заявка #{app_id} отправлена на модерацию.")
            else:
                await safe_reply_text(update, "❌ Ошибка при создании заявки.")
    except Exception as e:
        logger.error(f"Ошибка публикации попутки: {e}")
        await safe_reply_text(update, "❌ Не удалось опубликовать. Попробуйте позже.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== Обработчики поздравлений ==========
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

async def get_sender_name(update: Update, context: CallbackContext) -> int:
    sender_name = update.message.text.strip()
    if not validate_name(sender_name):
        await safe_reply_text(update, f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} символов).")
        return SENDER_NAME_INPUT
    context.user_data["from_name"] = sender_name
    await safe_reply_text(
        update, 
        f"Кого поздравляете? Например: *{EXAMPLE_TEXTS['recipient_name']}*", 
        parse_mode="Markdown"
    )
    return RECIPIENT_NAME_INPUT

async def get_recipient_name(update: Update, context: CallbackContext) -> int:
    recipient_name = update.message.text.strip()
    if not validate_name(recipient_name):
        await safe_reply_text(update, f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} символов).")
        return RECIPIENT_NAME_INPUT
    context.user_data["to_name"] = recipient_name
    keyboard = [
        [InlineKeyboardButton(holiday, callback_data=f"holiday_{holiday}")]
        for holiday in HOLIDAYS
    ] + [
        [InlineKeyboardButton("🎉 Другой праздник", callback_data="custom_congrat")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_reply_text(
        update, 
        "Выберите праздник из списка или укажите свой:", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONGRAT_HOLIDAY_CHOICE

async def handle_congrat_holiday_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "custom_congrat":
        context.user_data["congrat_type"] = "custom"
        await safe_edit_message_text(
            query, 
            f"Напишите своё поздравление (до {MAX_CONGRAT_TEXT_LENGTH} символов):", 
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    holiday = query.data.replace("holiday_", "")
    if holiday not in HOLIDAYS:
        await safe_edit_message_text(query, "❌ Неизвестный праздник. Пожалуйста, выберите из списка.")
        return ConversationHandler.END
    template = HOLIDAY_TEMPLATES.get(holiday, "С праздником!")
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name} с {holiday}! {template}"
    context.user_data["congrat_type"] = "standard"
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="publish_today")],
        [InlineKeyboardButton("📆 Указать дату", callback_data="publish_custom_date")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_edit_message_text(
        query, 
        "Когда опубликовать поздравление?", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONGRAT_DATE_CHOICE

async def handle_congrat_date_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "publish_today":
        context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
        return await complete_request(update, context)
    elif query.data == "publish_custom_date":
        await safe_edit_message_text(
            query, 
            "Введите дату публикации в формате ДД-ММ-ГГГГ:", 
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return CONGRAT_DATE_INPUT
    return ConversationHandler.END

async def get_congrat_date(update: Update, context: CallbackContext) -> int:
    date_str = update.message.text.strip()
    try:
        publish_date = datetime.strptime(date_str, "%d-%m-%Y").date()
        if publish_date < datetime.now().date():
            await safe_reply_text(update, "Нельзя указать прошедшую дату.")
            return CONGRAT_DATE_INPUT
        context.user_data["publish_date"] = publish_date.strftime("%Y-%m-%d")
        return await complete_request(update, context)
    except ValueError:
        await safe_reply_text(update, "Неверный формат даты. Используйте ДД-ММ-ГГГГ.")
        return CONGRAT_DATE_INPUT

async def get_custom_congrat_message(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if not text:
        await safe_reply_text(update, "Пожалуйста, введите текст поздравления.")
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    if len(text) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст слишком длинный (максимум {MAX_CONGRAT_TEXT_LENGTH} символов).")
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    if update.message.photo:
        context.user_data["photo_id"] = update.message.photo[-1].file_id
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name}! {text}"
    context.user_data["congrat_type"] = "custom"
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="publish_today")],
        [InlineKeyboardButton("📆 Указать дату", callback_data="publish_custom_date")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_reply_text(
        update, 
        "Когда опубликовать поздравление?", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONGRAT_DATE_CHOICE

# ========== Обработчики объявлений ==========
async def handle_announce_subtype_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    subtype_key = query.data.replace("subtype_", "")
    context.user_data["subtype"] = subtype_key
    example = EXAMPLE_TEXTS["announcement"].get(subtype_key, "")
    await safe_edit_message_text(
        query,
        f"Введите текст объявления (до {MAX_TEXT_LENGTH} символов).\nПример: {example}",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return ANNOUNCE_TEXT_INPUT

async def handle_announce_text_input(update: Update, context: CallbackContext) -> int:
    text = update.message.text or update.message.caption
    if not text:
        await safe_reply_text(update, "Пожалуйста, введите текст к вашему сообщению.")
        return ANNOUNCE_TEXT_INPUT
    text = text.strip()
    if update.message.photo:
        context.user_data["photo_id"] = update.message.photo[-1].file_id
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст слишком длинный (максимум {MAX_TEXT_LENGTH} символов).")
        return ANNOUNCE_TEXT_INPUT
    censored_text, has_bad = censor_text(text)
    if has_bad:
        context.user_data["censored_text"] = censored_text
        keyboard = [
            [InlineKeyboardButton("✅ Принять и отправить", callback_data="accept_censor")],
            [InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_censor")]
        ]
        await safe_reply_text(
            update,
            f"⚠️ В тексте найдены запрещённые слова (заменены на ***):\n{censored_text}\nПодтвердите или измените текст:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL
    context.user_data["text"] = censored_text
    await safe_reply_text(update, "Введите телефон для связи:")
    return PHONE_INPUT

# ========== Обработчики новостей ==========
async def get_news_phone_number(update: Update, context: CallbackContext) -> int:
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await safe_reply_text(update, "Неверный формат номера. Используйте +7... или 8...")
        return NEWS_PHONE_INPUT
    context.user_data["phone_number"] = phone
    await safe_reply_text(
        update, 
        f"Введите текст новости (до {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов):"
    )
    return NEWS_TEXT_INPUT

async def get_news_text(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if not text:
        await safe_reply_text(update, "Пожалуйста, введите текст новости.")
        return NEWS_TEXT_INPUT
    if len(text) > MAX_ANNOUNCE_NEWS_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст слишком длинный (максимум {MAX_ANNOUNCE_NEWS_TEXT_LENGTH} символов).")
        return NEWS_TEXT_INPUT
    if update.message.photo:
        context.user_data["photo_id"] = update.message.photo[-1].file_id
    censored_text, has_bad = censor_text(text)
    if has_bad:
        context.user_data["censored_text"] = censored_text
        keyboard = [
            [InlineKeyboardButton("✅ Отправить", callback_data="accept_censor")],
            [InlineKeyboardButton("✏️ Изменить", callback_data="edit_censor")]
        ]
        await safe_reply_text(
            update,
            f"⚠️ В тексте найдены запрещённые слова:\n{censored_text}\nПодтвердите отправку:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CENSOR_APPROVAL
    context.user_data["text"] = censored_text

    user = update.effective_user
    app_data = {
        'user_id': user.id,
        'username': user.username,
        'type': 'news',
        'text': censored_text,
        'photo_id': context.user_data.get('photo_id'),
        'phone_number': context.user_data.get('phone_number'),
        'subtype': 'news',
        'status': 'pending'
    }

    app_id = await add_application(app_data)
    if app_id:
        await notify_admin_new_application(context.bot, app_id)
        confirmation_text = (
            "✅ Новость принята.\n\n"
            "После проверки она будет опубликована в канале:\n"
            f"<a href='{NEWS_CHANNEL_LINK}'>{NEWS_CHANNEL_TEXT}</a>\n\n"
            "📰 Здесь вы можете ознакомиться с новостями нашего городка."
        )
        await safe_reply_text(
            update,
            confirmation_text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    else:
        await safe_reply_text(update, "❌ Ошибка при создании заявки.")

    context.user_data.clear()
    return ConversationHandler.END

# ========== Обработчики телефона ==========
async def get_phone_number(update: Update, context: CallbackContext) -> int:
    phone_raw = (update.message.text or update.message.caption or "").strip()
    if not validate_phone(phone_raw):
        await safe_reply_text(
            update,
            "Введите корректный номер телефона (например: +79610904569 или 8XXXXXXXXXX)."
        )
        return PHONE_INPUT
    context.user_data["phone_number"] = phone_raw
    return await complete_request(update, context)

# ========== Обработка цензуры ==========
async def handle_censor_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "accept_censor":
        context.user_data["text"] = context.user_data["censored_text"]
        return await complete_request(update, context)
    elif query.data == "edit_censor":
        await safe_edit_message_text(
            query, 
            "Введите исправленный текст:", 
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        request_type = context.user_data.get("type")
        if request_type == "congrat":
            return CUSTOM_CONGRAT_MESSAGE_INPUT
        elif request_type == "announcement":
            return ANNOUNCE_TEXT_INPUT
        elif request_type == "news":
            return NEWS_TEXT_INPUT
    return ConversationHandler.END

# ========== Завершение заявки ==========
async def complete_request(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    if not await can_submit_request(user.id):
        await safe_reply_text(update, "Вы отправили слишком много заявок. Попробуйте позже.")
        return ConversationHandler.END
    user_data = context.user_data
    app_data = {
        'user_id': user.id,
        'username': user.username,
        'type': user_data['type'],
        'subtype': user_data.get('subtype'),
        'from_name': user_data.get('from_name'),
        'to_name': user_data.get('to_name'),
        'text': user_data['text'],
        'photo_id': user_data.get('photo_id'),
        'phone_number': user_data.get('phone_number'),
        'publish_date': user_data.get('publish_date'),
        'congrat_type': user_data.get('congrat_type'),
        'ride_from': user_data.get('ride_from'),
        'ride_to': user_data.get('ride_to'),
        'ride_date': user_data.get('ride_date'),
        'ride_seats': user_data.get('ride_seats'),
        'original_link': user_data.get('original_link')
    }
    app_id = await add_application(app_data)
    if app_id:
        if ADMIN_CHAT_ID:
            await notify_admin_new_application(context.bot, app_id)
        else:
            logger.warning("ADMIN_CHAT_ID не задан. Уведомление администратору не отправлено.")
        await safe_reply_text(update, f"✅ Заявка #{app_id} отправлена на модерацию.")
    else:
        await safe_reply_text(update, "❌ Ошибка при создании заявки.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== Уведомление администратора ==========
async def notify_admin_new_application(bot: Bot, app_id: int):
    app_data = await get_application_details(app_id)
    if not app_data:
        logger.error(f"Не удалось получить данные для заявки #{app_id} для отправки админу.")
        return
    try:
        app_type = REQUEST_TYPES.get(app_data['type'], {}).get('name', 'Заявка')
        subtype = ANNOUNCE_SUBTYPES.get(app_data['subtype'], '') if app_data.get('subtype') else ''
        full_type = f"{app_type}" + (f" ({subtype})" if subtype else '')
        phone = f"• Телефон: {app_data['phone_number']}" if app_data.get('phone_number') else ""
        has_photo = "✅" if app_data.get('photo_id') else "❌"
        caption = f"""📨 Новая заявка #{app_id}
• Тип: {full_type}
• Фото: {has_photo}
{phone}
• От: @{app_data.get('username') or 'N/A'} (ID: {app_data['user_id']})
• Текст: {app_data['text']}"""
        keyboard = None
        if app_data['type'] != "news":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")]
            ])
        if app_data.get('photo_id'):
            await bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=app_data['photo_id'],
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        logger.info(f"Заявка #{app_id} отправлена администратору.")
    except Exception as e:
        logger.error(f"Ошибка отправки заявки #{app_id} администратору: {e}", exc_info=True)

# ========== Публикация в канал ==========
async def publish_to_channel(app_id: int, bot: Bot):
    app_data = await get_application_details(app_id)
    if not app_data:
        logger.error(f"Не удалось получить данные для публикации заявки #{app_id}.")
        return False
    try:
        current_time = datetime.now(TIMEZONE).strftime("%H:%M")
        text = app_data['text']
        phone = app_data.get('phone_number')
        if phone:
            text += f"\n📞 Телефон: {phone}"

        # Добавляем ссылку на оригинал, если есть
        if app_data.get('original_link'):
            text += f"\n🔗 Перейти к объявлению ({app_data['original_link']})"

        # Добавляем ссылку на новостной канал под каждым сообщением
        message_text = (
            f"{text}\n\n"
            f"📰 <a href='{NEWS_CHANNEL_LINK}'>Новости нашего городка — {NEWS_CHANNEL_TEXT}</a>\n"
            f"#ЧтоПочёмНиколаевск\n"
            f"🕒 {current_time}"
        )

        if app_data.get('photo_id'):
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=app_data['photo_id'],
                caption=message_text,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message_text,
                parse_mode="HTML"
            )
        await mark_application_as_published(app_id)
        logger.info(f"Заявка #{app_id} опубликована в канале.")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации заявки #{app_id}: {e}")
        return False

# ========== Админские функции ==========
async def admin_approve_application(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    app_id = int(query.data.split('_')[1])
    if await update_application_status(app_id, 'approved'):
        await safe_edit_message_text(
            query,
            f"✅ Заявка #{app_id} одобрена!",
            reply_markup=None
        )
    else:
        await safe_edit_message_text(
            query,
            f"❌ Не удалось одобрить заявку #{app_id}",
            reply_markup=None
        )

async def admin_reject_application(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    app_id = int(query.data.split('_')[1])
    if await update_application_status(app_id, 'rejected'):
        await safe_edit_message_text(
            query,
            f"❌ Заявка #{app_id} отклонена!",
            reply_markup=None
        )
    else:
        await safe_edit_message_text(
            query,
            f"⚠️ Не удалось отклонить заявку #{app_id}",
            reply_markup=None
        )

# ========== Проверка и публикация отложенных заявок ==========
async def check_pending_applications():
    try:
        applications = await get_approved_unpublished_applications()
        if not applications:
            return
        bot = Bot(token=TOKEN)
        for app in applications:
            try:
                if app['publish_date'] and datetime.strptime(app['publish_date'], "%Y-%m-%d").date() > datetime.now().date():
                    continue
                await publish_to_channel(app['id'], bot)
                logger.info(f"Опубликовано сообщение #{app['id']}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ошибка обработки заявки #{app['id']}: {e}")
    except Exception as e:
        logger.error(f"Ошибка проверки заявок: {e}")

# ========== Инициализация бота ==========
async def initialize_bot():
    global application
    async with application_lock:
        if application is not None:
            return
        application = Application.builder().token(TOKEN).build()
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start_command),
                CallbackQueryHandler(handle_carpool_start, pattern="^carpool$")
            ],
            states={
                TYPE_SELECTION: [CallbackQueryHandler(handle_type_selection)],
                CARPOOL_SUBTYPE_SELECTION: [CallbackQueryHandler(handle_carpool_type)],
                RIDE_FROM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_from)],
                RIDE_TO_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_to)],
                RIDE_DATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_date)],
                RIDE_SEATS_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_seats)],
                RIDE_PHONE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carpool_phone)],
                SENDER_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sender_name)],
                RECIPIENT_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_recipient_name)],
                CONGRAT_HOLIDAY_CHOICE: [CallbackQueryHandler(handle_congrat_holiday_choice)],
                CUSTOM_CONGRAT_MESSAGE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_congrat_message)],
                CONGRAT_DATE_CHOICE: [CallbackQueryHandler(handle_congrat_date_choice)],
                CONGRAT_DATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_congrat_date)],
                ANNOUNCE_SUBTYPE_SELECTION: [CallbackQueryHandler(handle_announce_subtype_selection)],
                ANNOUNCE_TEXT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_announce_text_input)],
                PHONE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
                NEWS_PHONE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_news_phone_number)],
                NEWS_TEXT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_news_text)],
                WAIT_CENSOR_APPROVAL: [CallbackQueryHandler(handle_censor_choice)]
            },
            fallbacks=[
                CommandHandler('cancel', cancel_command),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            allow_reentry=True,
            conversation_timeout=timedelta(minutes=CONVERSATION_TIMEOUT_MINUTES).total_seconds()
        )
        application.add_handler(conv_handler)
        application.add_handler(MessageHandler(filters.PHOTO, handle_any_photo), group=1)
        application.add_handler(CallbackQueryHandler(admin_approve_application, pattern="^approve_\\d+$"))
        application.add_handler(CallbackQueryHandler(admin_reject_application, pattern="^reject_\\d+$"))
        application.add_handler(CallbackQueryHandler(help_inline_handler, pattern="^help_inline$"))
        await application.initialize()
        if WEBHOOK_URL and WEBHOOK_SECRET:
            webhook_url = f"{WEBHOOK_URL}/telegram-webhook/{WEBHOOK_SECRET}"
            await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
            logger.info(f"Вебхук установлен: {webhook_url}")
        else:
            logger.warning("WEBHOOK_URL или WEBHOOK_SECRET не заданы. Вебхук не будет установлен.")
        logger.info("Бот инициализирован.")

# ========== FastAPI приложение ==========
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    await init_db()
    await load_bad_words()
    await initialize_bot()
    scheduler = AsyncIOScheduler(
        jobstores={'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')},
        executors={'default': AsyncIOExecutor()},
        timezone=TIMEZONE
    )
    scheduler.add_job(check_pending_applications, 'interval', minutes=1)
    scheduler.start()
    logger.info("FastAPI приложение запущено.")

@app.on_event("shutdown")
async def shutdown_event():
    db_executor.shutdown(wait=True)
    logger.info("Пул потоков для БД остановлен.")

@app.post("/telegram-webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    if application is None:
        await initialize_bot()
        if application is None:
            logger.error("Критическая ошибка: не удалось инициализировать приложение Telegram.")
            raise HTTPException(status_code=500, detail="Bot not initialized")
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Telegram Bot Webhook Listener is running"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
