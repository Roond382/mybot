import os
import sys
import logging
import sqlite3
import re
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

# FastAPI импорты
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Сторонние библиотеки
import pytz
from dotenv import load_dotenv
import aiofiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Telegram Bot импорты
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackContext,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Загрузка переменных окружения
load_dotenv()

# Создание экземпляра FastAPI
app = FastAPI()

# ========== КОНФИГУРАЦИЯ ==========
PORT = int(os.environ.get('PORT', 10000))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
TOKEN = os.getenv('TELEGRAM_TOKEN')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
CHANNEL_ID = int(os.getenv('CHANNEL_ID')) if os.getenv('CHANNEL_ID') else None
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID')) if os.getenv('ADMIN_CHAT_ID') else None
TIMEZONE = pytz.timezone('Europe/Moscow')
WORKING_HOURS = (0, 23)
WORK_ON_WEEKENDS = True

# ========== КОНСТАНТЫ ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
DB_FILE = 'db.sqlite'
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50
MAX_TEXT_LENGTH = 4000
MAX_CONGRAT_TEXT_LENGTH = 500
MAX_ANNOUNCE_NEWS_TEXT_LENGTH = 300
CHANNEL_NAME = "Небольшой Мир: Николаевск"

# ========== ПРИМЕРЫ ТЕКСТОВ ==========
EXAMPLE_TEXTS = {
    "sender_name": "Иванов Виталий",
    "recipient_name": "коллектив детсада 'Солнышко'",
    "congrat": {
        "custom": "Дорогая мама! Поздравляю с Днем рождения! Желаю здоровья и счастья!"
    },
    "announcement": {
        "ride": "10.02 еду в Волгоград. 2 места. Выезд в 8:00",
        "demand_offer": "Ищу работу водителя. Опыт 5 лет.",
        "lost": "Найден ключ у магазина 'Продукты'. Опознать по брелку."
    },
    "news": "15.01 в нашем городе открыли новую детскую площадку!"
}

# ========== СОСТОЯНИЯ ДИАЛОГА ==========
(
    TYPE_SELECTION,
    SENDER_NAME_INPUT,
    RECIPIENT_NAME_INPUT,
    CONGRAT_HOLIDAY_CHOICE,
    CUSTOM_CONGRAT_MESSAGE_INPUT,
    CONGRAT_DATE_CHOICE,
    CONGRAT_DATE_INPUT,
    ANNOUNCE_SUBTYPE_SELECTION,
    ANNOUNCE_TEXT_INPUT,
    PHONE_INPUT,
    WAIT_CENSOR_APPROVAL,
    NEWS_PHONE_INPUT,
    NEWS_TEXT_INPUT,
    RIDE_TYPE_SELECTION,
    RIDE_FROM_INPUT,
    RIDE_TO_INPUT,
    RIDE_DATE_INPUT,
    RIDE_SEATS_INPUT,
    RIDE_PHONE_INPUT
) = range(19)

# ========== ТИПЫ ЗАПРОСОВ ==========
REQUEST_TYPES = {
    "congrat": {"name": "Поздравление", "icon": "🎉"},
    "announcement": {"name": "Объявление", "icon": "📢"},
    "news": {"name": "Новость от жителя", "icon": "🗞️"}
}

# ========== ПОДТИПЫ ОБЪЯВЛЕНИЙ ==========
ANNOUNCE_SUBTYPES = {
    "ride": "🚗 Попутка",
    "demand_offer": "🤝 Спрос и предложения",
    "lost": "🔍 Потеряли/Нашли"
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

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== ГЛОБАЛЬНОЕ СОСТОЯНИЕ БОТА ==========
BOT_STATE = {
    'running': False,
    'start_time': None,
    'last_activity': None
}

# ========== ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ ДЛЯ ПРИЛОЖЕНИЯ ==========
application_lock = asyncio.Lock()
application: Optional[Application] = None

# ========== БАЗА ДАННЫХ ==========
def cleanup_old_applications(days: int = 30) -> None:
    """Очищает старые заявки старше указанного количества дней."""
    try:
        with get_db_connection() as conn:
            conn.execute("""
                DELETE FROM applications 
                WHERE created_at < datetime('now', ?) 
                AND status IN ('published', 'rejected')
            """, (f"-{days} days",))
            conn.commit()
            logger.info(f"Очистка БД: удалены заявки старше {days} дней")
    except sqlite3.Error as e:
        logger.error(f"Ошибка очистки БД: {e}", exc_info=True)

def get_db_connection() -> sqlite3.Connection:
    """Устанавливает соединение с базой данных с настройками."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """Инициализирует таблицы базы данных."""
    try:
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
                    text TEXT NOT NULL,
                    photo_id TEXT,
                    phone_number TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    publish_date DATE,
                    published_at TIMESTAMP,
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
            conn.commit()
            logger.info("База данных успешно инициализирована")
    except sqlite3.Error as e:
        logger.error(f"Ошибка инициализации БД: {e}", exc_info=True)
        raise

def add_application(data: Dict[str, Any]) -> Optional[int]:
    """Добавляет новую заявку в базу данных."""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO applications
                (user_id, username, type, subtype, from_name, to_name, text, photo_id, phone_number, publish_date, congrat_type)
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
    except sqlite3.Error as e:
        logger.error(f"Ошибка добавления заявки: {e}\nДанные: {data}", exc_info=True)
        return None

def get_application_details(app_id: int) -> Optional[sqlite3.Row]:
    """Получает детали заявки по ID."""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
            return cur.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Ошибка получения заявки #{app_id}: {e}", exc_info=True)
        return None

def get_approved_unpublished_applications() -> list:
    """Получает одобренные, но не опубликованные заявки."""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
            SELECT * FROM applications
            WHERE status = 'approved' AND published_at IS NULL
            """)
            return cur.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Ошибка получения заявок: {e}", exc_info=True)
        return []

def update_application_status(app_id: int, status: str) -> bool:
    """Обновляет статус заявки."""
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
            conn.commit()
            return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка обновления статуса заявки #{app_id}: {e}", exc_info=True)
        return False

def mark_application_as_published(app_id: int) -> bool:
    """Помечает заявку как опубликованную."""
    try:
        with get_db_connection() as conn:
            conn.execute("""
            UPDATE applications
            SET published_at = CURRENT_TIMESTAMP, status = 'published'
            WHERE id = ?
            """, (app_id,))
            conn.commit()
            return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка публикации заявки #{app_id}: {e}", exc_info=True)
        return False

def can_submit_request(user_id: int) -> bool:
    """Проверяет, может ли пользователь отправить заявку (лимит)."""
    try:
        with get_db_connection() as conn:
            count = conn.execute("""
                SELECT COUNT(*) FROM applications 
                WHERE user_id = ? AND created_at > datetime('now', '-1 hour')
            """, (user_id,)).fetchone()[0]
            return count < 5
    except sqlite3.Error as e:
        logger.error(f"Ошибка проверки лимита заявок: {e}")
        return True

def is_working_hours() -> bool:
    """Проверяет, рабочее ли время."""
    current_time = datetime.now(TIMEZONE)
    current_hour = current_time.hour
    current_weekday = current_time.weekday()
    if not WORK_ON_WEEKENDS and current_weekday >= 5:
        return False
    return WORKING_HOURS[0] <= current_hour <= WORKING_HOURS[1]

def get_uptime() -> str:
    """Возвращает время работы бота."""
    if not BOT_STATE.get('start_time'):
        return "N/A"
    uptime = datetime.now(TIMEZONE) - BOT_STATE['start_time']
    days, remainder = divmod(uptime.total_seconds(), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{int(days)}д {int(hours)}ч {int(minutes)}м"

def validate_name(name: str) -> bool:
    """Валидирует имя."""
    if not name or len(name) < 2 or len(name) > MAX_NAME_LENGTH:
        return False
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-]+$', name))

def validate_phone(phone: str) -> bool:
    """Валидирует номер телефона."""
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

async def load_bad_words() -> List[str]:
    """Загружает список запрещенных слов."""
    try:
        async with aiofiles.open(BAD_WORDS_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            return [word.strip() for line in content.splitlines() for word in line.split(',') if word.strip()]
    except FileNotFoundError:
        logger.warning("Файл с запрещенными словами не найден. Используются значения по умолчанию.")
        return DEFAULT_BAD_WORDS
    except Exception as e:
        logger.error(f"Ошибка загрузки запрещенных слов: {e}", exc_info=True)
        return DEFAULT_BAD_WORDS

async def censor_text(text: str) -> Tuple[str, bool]:
    """Цензурирует текст и возвращает результат."""
    bad_words = await load_bad_words()
    censored = text
    has_bad = False
    for word in bad_words:
        try:
            if re.search(re.escape(word), censored, re.IGNORECASE):
                has_bad = True
                censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
        except re.error as e:
            logger.error(f"Ошибка цензуры слова '{word}': {e}")
    return censored, has_bad

async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправляет сообщение с обработкой ошибок."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except TelegramError as e:
        logger.warning(f"Ошибка отправки сообщения в {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка отправки в {chat_id}: {e}", exc_info=True)
    return False

async def safe_edit_message_text(query, text: str, **kwargs):
    """Редактирует сообщение с обработкой ошибок."""
    if not query or not query.message:
        return
    try:
        await query.edit_message_text(text=text, **kwargs)
    except TelegramError as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Ошибка редактирования сообщения: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка редактирования: {e}", exc_info=True)

async def safe_reply_text(update: Update, text: str, **kwargs):
    """Отвечает на сообщение с обработкой ошибок."""
    if update.callback_query:
        await safe_edit_message_text(update.callback_query, text, **kwargs)
    elif update.message:
        try:
            await update.message.reply_text(text=text, **kwargs)
        except TelegramError as e:
            logger.warning(f"Ошибка ответа на сообщение: {e}")
        except Exception as e:
            logger.error(f"Неожиданная ошибка ответа: {e}", exc_info=True)

async def notify_admin_new_application(bot: Bot, app_id: int, app_details: dict):
    """Уведомляет администратора о новой заявке."""
    if not ADMIN_CHAT_ID:
        return

    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', 'Заявка')
    subtype = ANNOUNCE_SUBTYPES.get(app_details['subtype'], '') if app_details.get('subtype') else ''
    full_type = f"{app_type}" + (f" ({subtype})" if subtype else '')
    
    if app_details['type'] == 'news':
        has_photo = "✅" if app_details.get('photo_id') else "❌"
        phone = f"\n• Телефон: {app_details['phone_number']}" if app_details.get('phone_number') else ""
        caption = (
            f"📨 Новая новость #{app_id} (без модерации)\n"
            f"• Тип: {full_type}\n• Фото: {has_photo}{phone}\n"
            f"• От: @{app_details.get('username') or 'N/A'} (ID: {app_details['user_id']})\n"
            f"• Текст: {app_details['text']}\n"
        )
        try:
            if app_details.get('photo_id'):
                await bot.send_photo(
                    chat_id=ADMIN_CHAT_ID,
                    photo=app_details['photo_id'],
                    caption=caption
                )
            else:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=caption
                )
            logger.info(f"Новость #{app_id} отправлена админу без модерации.")
        except TelegramError as e:
            logger.warning(f"Ошибка отправки новости #{app_id} админу: {e}")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при отправке новости #{app_id} админу: {e}", exc_info=True)
        return

    has_photo = "✅" if app_details.get('photo_id') else "❌"
    phone = f"\n• Телефон: {app_details['phone_number']}" if app_details.get('phone_number') else ""

    caption = (
        f"📨 Новая заявка #{app_id}\n"
        f"• Тип: {full_type}\n• Фото: {has_photo}{phone}\n"
        f"• От: @{app_details.get('username') or 'N/A'} (ID: {app_details['user_id']})\n"
        f"• Текст: {app_details['text'][:200]}...\n\nВыберите действие:"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")],
        [InlineKeyboardButton("👀 Посмотреть", callback_data=f"view_{app_id}")]
    ]

    try:
        if app_details.get('photo_id'):
            await bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=app_details['photo_id'],
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=caption,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    except TelegramError as e:
        logger.warning(f"Ошибка отправки заявки #{app_id} админу: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при уведомлении админа о заявке #{app_id}: {e}", exc_info=True)

async def notify_user_about_decision(bot: Bot, app_details: dict, approved: bool):
    """Уведомляет пользователя о решении по заявке."""
    user_id = app_details['user_id']
    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', 'заявка')
    status = "одобрена" if approved else "отклонена модератором"
    icon = "🎉" if approved else "😕"
    text = f"{icon} Ваша заявка на «{app_type}» была {status}."
    await safe_send_message(bot, user_id, text)

async def publish_to_channel(app_id: int, bot: Bot) -> bool:
    """Публикует заявку в канал."""
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан. Публикация невозможна.")
        return False
    app_details = get_application_details(app_id)
    if not app_details:
        logger.error(f"Заявка #{app_id} не найдена.")
        return False
    current_time = datetime.now(TIMEZONE).strftime("%H:%M")
    text = app_details['text']
    photo_id = app_details['photo_id']
    phone = app_details['phone_number']
    if phone:
        text += f"\n📞 Телефон: {phone}"
    message_text = (
        f"{text}\n"
        f"#НебольшойМирНиколаевск\n"
        f"🕒 {current_time}"
    )
    try:
        if photo_id:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo_id,
                caption=message_text,
                disable_notification=True,
                disable_web_page_preview=True
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message_text,
                disable_web_page_preview=True,
                disable_notification=True
            )
        mark_application_as_published(app_id)
        logger.info(f"Опубликовано сообщение #{app_id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации: {str(e)}")
        return False

async def check_pending_applications(context: CallbackContext) -> None:
    """Проверяет и публикует одобренные заявки."""
    try:
        applications = get_approved_unpublished_applications()
        for app in applications:
            try:
                publish_now = True
                if app['type'] == 'congrat' and app.get('publish_date'):
                    publish_date_obj = datetime.strptime(app['publish_date'], "%Y-%m-%d").date()
                    today = datetime.now().date()
                    if publish_date_obj > today:
                        publish_now = False
                if publish_now:
                    await publish_to_channel(app['id'], context.bot)
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ошибка обработки заявки #{app['id']}: {e}")
    except Exception as e:
        logger.error(f"Ошибка проверки заявок: {e}")

# ========== ОБРАБОТЧИКИ ДИАЛОГА ==========
async def start_command(update: Update, context: CallbackContext) -> int:
    """Обработчик команды /start."""
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)]
        for key, info in REQUEST_TYPES.items()
    ]
    await safe_reply_text(update, "👋 Здравствуйте!\nВыберите, что хотите отправить в канал:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TYPE_SELECTION

async def handle_ride_type_selection(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора типа поездки."""
    query = update.callback_query
    await query.answer()
    ride_type = query.data
    context.user_data["ride_type"] = "Ищу попутчиков" if ride_type == "ride_need" else "Предлагаю поездку"
    
    await safe_edit_message_text(
        query,
        "Откуда планируете поездку? (Например: Москва, центр):",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    return RIDE_FROM_INPUT

async def handle_type_selection(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора типа заявки."""
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

async def get_ride_from(update: Update, context: CallbackContext) -> int:
    """Получает пункт отправления для поездки."""
    context.user_data["ride_from"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Куда направляетесь? (Например: Санкт-Петербург, аэропорт):")
    return RIDE_TO_INPUT

async def get_ride_to(update: Update, context: CallbackContext) -> int:
    """Получает пункт назначения для поездки."""
    context.user_data["ride_to"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Когда планируете выезд? (Например: 15.08 в 8:00):")
    return RIDE_DATE_INPUT

async def get_ride_date(update: Update, context: CallbackContext) -> int:
    """Получает дату поездки."""
    context.user_data["ride_date"] = update.message.text.strip()
    await safe_reply_text(
        update,
        "Сколько мест доступно? (Укажите число):")
    return RIDE_SEATS_INPUT

async def get_ride_seats(update: Update, context: CallbackContext) -> int:
    """Получает количество мест в поездке."""
    seats = update.message.text.strip()
    if not seats.isdigit():
        await safe_reply_text(update, "Пожалуйста, укажите число:")
        return RIDE_SEATS_INPUT
    context.user_data["ride_seats"] = seats
    await safe_reply_text(
        update,
        "Введите контактный телефон (формат: +7... или 8...):")
    return RIDE_PHONE_INPUT

async def get_sender_name(update: Update, context: CallbackContext) -> int:
    """Получает имя отправителя поздравления."""
    sender_name = update.message.text.strip()
    if not validate_name(sender_name):
        await safe_reply_text(
            update,
            f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} символов)."
        )
        return SENDER_NAME_INPUT
    context.user_data["from_name"] = sender_name
    await safe_reply_text(
        update,
        f"Кого поздравляете? Например: *{EXAMPLE_TEXTS['recipient_name']}*",
        parse_mode="Markdown"
    )
    return RECIPIENT_NAME_INPUT

async def get_recipient_name(update: Update, context: CallbackContext) -> int:
    """Получает имя получателя поздравления."""
    recipient_name = update.message.text.strip()
    if not validate_name(recipient_name):
        await safe_reply_text(
            update,
            f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} символов)."
        )
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
    """Обработчик выбора праздника."""
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
    context.user_data["text"] = f"{from_name} поздравляет {to_name} с {holiday}!\n{template}"
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
    """Обработчик выбора даты публикации поздравления."""
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

async def get_custom_congrat_message(update: Update, context: CallbackContext) -> int:
    """Получает пользовательский текст поздравления."""
    text = update.message.text.strip()
    if len(text) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(
            update,
            f"Текст слишком длинный (максимум {MAX_CONGRAT_TEXT_LENGTH} символов)."
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name}!\n{text}"
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
async def get_ride_phone_and_finish(update: Update, context: CallbackContext) -> int:
    """Получает телефон для поездки и завершает процесс."""
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await safe_reply_text(update, "Неверный формат номера. Используйте +7... или 8...")
        return RIDE_PHONE_INPUT
    
    # Формируем текст объявления
    ride_data = context.user_data
    text = (
        f"{ride_data['ride_type']}\n"
        f"📍 Откуда: {ride_data['ride_from']}\n"
        f"📍 Куда: {ride_data['ride_to']}\n"
        f"⏰ Время: {ride_data['ride_date']}\n"
        f"🪑 Мест: {ride_data['ride_seats']}\n"
        f"📞 Телефон: {phone}"
    )
    
    # Проверка на спам
    if not can_submit_request(update.effective_user.id):
        await safe_reply_text(update, "❌ Слишком много запросов. Попробуйте позже.")
        return ConversationHandler.END
    
    # Проверка на запрещенные слова
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
            reply_markup=InlineKeyboardMarkup(keyboard))
        return WAIT_CENSOR_APPROVAL
    
    # Создаем заявку
    app_data = {
        'user_id': update.effective_user.id,
        'username': update.effective_user.username,
        'type': 'announcement',
        'subtype': 'ride',
        'text': text,
        'phone_number': phone,
        'status': 'approved'  # Публикуем без модерации
    }
    
    app_id = add_application(app_data)
    if app_id:
        if await publish_to_channel(app_id, context.bot):
            await safe_reply_text(
                update,
                "✅ Ваше объявление опубликовано в канале!",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        else:
            await safe_reply_text(
                update,
                "❌ Ошибка при публикации объявления.",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    else:
        await safe_reply_text(
            update,
            "❌ Ошибка при создании заявки.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    
    context.user_data.clear()
    return ConversationHandler.END
async def handle_announce_subtype_selection(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора подтипа объявления."""
    query = update.callback_query
    await query.answer()
    subtype_key = query.data.replace("subtype_", "")
    context.user_data["subtype"] = subtype_key
    
    if subtype_key == "ride":
        # Обработка объявлений о поездках
        keyboard = [
            [InlineKeyboardButton("🚗 Ищу попутчиков", callback_data="ride_need")],
            [InlineKeyboardButton("🚙 Предлагаю поездку", callback_data="ride_offer")],
            *BACK_BUTTON
        ]
        await safe_edit_message_text(
            query,
            "Выберите тип поездки:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return RIDE_TYPE_SELECTION
    elif subtype_key == "demand_offer":
        # Обработка спроса/предложения
        await safe_edit_message_text(
            query,
            "Введите ваш контактный телефон (формат: +7... или 8...):",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return PHONE_INPUT
    else:
        # Обработка других типов объявлений (потеряли/нашли)
        example = EXAMPLE_TEXTS["announcement"].get(subtype_key, "")
        await safe_edit_message_text(
            query,
            f"Введите текст объявления (до {MAX_TEXT_LENGTH} символов). Пример:\n\n{example}",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return ANNOUNCE_TEXT_INPUT

async def handle_censor_choice(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора действия при цензуре."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "accept_censor":
        # Пользователь подтвердил отправку с цензурой
        context.user_data["text"] = context.user_data["censored_text"]
        return await complete_request(update, context)
    elif query.data == "edit_censor":
        # Пользователь хочет изменить текст
        await safe_edit_message_text(
            query,
            "Введите исправленный текст:",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        
        # Возвращаем в соответствующее состояние в зависимости от типа запроса
        if context.user_data.get("type") == "congrat":
            return CUSTOM_CONGRAT_MESSAGE_INPUT
        elif context.user_data.get("type") == "announcement":
            if context.user_data.get("subtype") == "ride":
                return RIDE_PHONE_INPUT  # Для поездок возвращаем к вводу телефона
            return ANNOUNCE_TEXT_INPUT
        elif context.user_data.get("type") == "news":
            return NEWS_TEXT_INPUT
    
    return ConversationHandler.END

# Добавляем обработчики в ConversationHandler
def setup_conv_handler() -> ConversationHandler:
    """Настраивает обработчик диалога."""
    return ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            TYPE_SELECTION: [
                CallbackQueryHandler(handle_type_selection, pattern="^(congrat|announcement|news)$")
            ],
            RIDE_TYPE_SELECTION: [
                CallbackQueryHandler(handle_ride_type_selection, pattern="^(ride_need|ride_offer)$")
            ],
            RIDE_FROM_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_from)
            ],
            RIDE_TO_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_to)
            ],
            RIDE_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_date)
            ],
            RIDE_SEATS_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_seats)
            ],
            RIDE_PHONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_phone_and_finish)
            ],
            WAIT_CENSOR_APPROVAL: [
                CallbackQueryHandler(handle_censor_choice, pattern="^(accept_censor|edit_censor)$")
            ],
            # ... другие состояния ...
        },
        fallbacks=[
            CommandHandler('cancel', cancel_command),
            CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
        ],
        allow_reentry=True
    )

async def setup_telegram_application():
    """Настраивает Telegram приложение."""
    global application
    async with application_lock:
        if application is not None:
            return

        application = Application.builder().token(TOKEN).build()
        
        # Инициализация БД
        init_db()
        
        # Добавляем обработчик диалога
        application.add_handler(setup_conv_handler())
        
        # Добавляем другие обработчики
        application.add_handler(CallbackQueryHandler(admin_approve_application, pattern="^approve_\\d+$"))
        application.add_handler(CallbackQueryHandler(admin_reject_application, pattern="^reject_\\d+$"))
        application.add_handler(CallbackQueryHandler(admin_view_application, pattern="^view_\\d+$"))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("pending", pending_command))
        
        # Настройка вебхука и планировщика
        if WEBHOOK_URL:
            await application.bot.set_webhook(f"{WEBHOOK_URL}/telegram-webhook/{WEBHOOK_SECRET}")
        
        scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        scheduler.add_job(check_pending_applications, 'interval', minutes=1, args=[application])
        scheduler.add_job(cleanup_old_applications, 'cron', hour=3, minute=0)
        scheduler.start()
        
        BOT_STATE['start_time'] = datetime.now(TIMEZONE)
        BOT_STATE['running'] = True
async def cancel_command(update: Update, context: CallbackContext) -> int:
    """Обработчик команды /cancel для отмены текущего диалога."""
    await safe_reply_text(
        update,
        "Текущее действие отменено. Используйте /start для начала нового запроса.",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    context.user_data.clear()
    return ConversationHandler.END

async def back_to_start(update: Update, context: CallbackContext) -> int:
    """Возвращает пользователя в главное меню."""
    query = update.callback_query
    if query:
        await query.answer()
    
    # Очищаем данные пользователя
    context.user_data.clear()
    
    # Создаем клавиатуру главного меню
    keyboard = [
        [InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)]
        for key, info in REQUEST_TYPES.items()
    ]
    
    # Отправляем сообщение с меню
    await safe_reply_text(
        update,
        "Вы вернулись в главное меню. Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return TYPE_SELECTION

def main() -> None:
    """Основная функция запуска бота."""
    try:
        # Инициализация логгера
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('bot.log'),
                logging.StreamHandler()
            ]
        )
        logger.info("Запуск бота...")

        # Проверка обязательных переменных окружения
        if not TOKEN:
            logger.error("Не задан TELEGRAM_TOKEN в переменных окружения!")
            sys.exit(1)

        # Создание и настройка приложения
        application = Application.builder().token(TOKEN).build()

        # Инициализация базы данных
        init_db()

        # Настройка обработчиков
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start_command)],
            states={
                TYPE_SELECTION: [CallbackQueryHandler(handle_type_selection)],
                RIDE_TYPE_SELECTION: [CallbackQueryHandler(handle_ride_type_selection)],
                RIDE_FROM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_from)],
                RIDE_TO_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_to)],
                RIDE_DATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_date)],
                RIDE_SEATS_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_seats)],
                RIDE_PHONE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ride_phone_and_finish)],
                # Другие состояния...
            },
            fallbacks=[
                CommandHandler('cancel', cancel_command),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            allow_reentry=True
        )

        application.add_handler(conv_handler)
        application.add_handler(CommandHandler('help', help_command))
        application.add_handler(CommandHandler('pending', pending_command))

        # Запуск бота
        if WEBHOOK_URL and WEBHOOK_SECRET:
            logger.info("Бот запущен в режиме вебхука")
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                webhook_url=f"{WEBHOOK_URL}/telegram-webhook/{WEBHOOK_SECRET}"
            )
        else:
            logger.info("Бот запущен в режиме поллинга")
            application.run_polling()

    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    # Проверка режима работы (FastAPI или поллинг)
    if os.getenv('WEBHOOK_MODE', '0') == '1':
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    else:
        main()


