import os
import sys
import logging
import sqlite3
import re
import asyncio
import secrets
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
PORT = int(os.environ.get('PORT', 10000))  # Render использует порт 10000
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
        "demand_offer": "Ищу работу водителя. Опыт 5 лет. Тел: +7-900-123-45-67",
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
    CONGRAT_DATE_INPUT,
    ANNOUNCE_SUBTYPE_SELECTION,
    ANNOUNCE_TEXT_INPUT,
    PHONE_INPUT,
    WAIT_CENSOR_APPROVAL
) = range(10)

# ========== ТИПЫ ЗАПРОСОВ ==========
REQUEST_TYPES = {
    "congrat": {"name": "Поздравление", "icon": "🎉"},
    "announcement": {"name": "Спрос и предложения", "icon": "📢"},
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

HOLIDAY_TEMPLATES = {
    "🎄 Новый год": "С Новым годом!\nПусть исполняются все ваши желания!",
    "🪖 23 Февраля": "С Днём защитника Отечества!\nМужества, отваги и мирного неба над головой!",
    "💐 8 Марта": "С 8 Марта!\nКрасоты, счастья и весеннего настроения!",
    "🏅 9 Мая": "С Днём Победы!\nВечная память героям!",
    "🇷🇺 12 Июня": "С Днём России!\nМира, благополучия и процветания нашей стране!",
    "🤝 4 Ноября": "С Днём народного единства!\nСогласия, мира и добра!"
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

application = None  # Глобальная переменная для хранения экземпляра Application

# ========== ГЛОБАЛЬНОЕ СОСТОЯНИЕ БОТА ==========
BOT_STATE = {
    'running': False,
    'start_time': None,
    'last_activity': None
}

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
    conn.execute("PRAGMA journal_mode=WAL")  # Включаем режим WAL для лучшей производительности
    conn.execute("PRAGMA foreign_keys=ON")   # Включаем поддержку внешних ключей
    return conn
    
def init_db():
    """Инициализирует таблицы базы данных."""
    try:
        with get_db_connection() as conn:
            # Создаем таблицу applications
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
            
            # Создаем индекс для оптимизации запросов
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_approved_unpublished
                ON applications(status, published_at)
                WHERE status = 'approved' AND published_at IS NULL
            """)
            
            # Создаем таблицу для хранения пользовательских ограничений
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_limits (
                    user_id INTEGER PRIMARY KEY,
                    last_request_time TIMESTAMP,
                    request_count INTEGER DEFAULT 0
                )
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
    except Exception as e:
        logger.error(f"Неожиданная ошибка при добавлении заявки: {e}", exc_info=True)
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
            SELECT id, user_id, username, type, subtype, from_name, to_name, text, publish_date, congrat_type
            FROM applications
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
            logger.error(f"Ошибка цензуры слова '{word}': {e}\nТекст: {text[:100]}...", exc_info=True)

    try:
        censored = re.sub(
            r'(звоните|пишите|телефон|номер|тел\.?|т\.)[:;\s]*([\+\d\(\).\s-]{7,})',
            'Контактная информация скрыта (пишите в ЛС)',
            censored,
            flags=re.IGNORECASE
        )
    except re.error as e:
        logger.error(f"Ошибка цензуры контактов: {e}", exc_info=True)

    return censored, has_bad

async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправляет сообщение с обработкой ошибок."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except TelegramError as e:
        logger.warning(f"Ошибка отправки сообщения в {chat_id}: {e}\nТекст: {text[:100]}...")
    except Exception as e:
        logger.error(f"Неожиданная ошибка отправки в {chat_id}: {e}\nТекст: {text[:100]}...", exc_info=True)
    return False

async def safe_edit_message_text(query: Update.callback_query, text: str, **kwargs):
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

async def send_bot_status(bot: Bot, status: str, force_send: bool = False) -> bool:
    """Отправляет статус бота администратору."""
    if not ADMIN_CHAT_ID or not bot:
        return False

    try:
        current_time = datetime.now(TIMEZONE)
        message = (
            f"🤖 Статус бота: {status}\n"
            f"• Время: {current_time.strftime('%H:%M %d.%m.%Y')}\n"
            f"• Рабочее время: {'Да' if is_working_hours() else 'Нет'}\n"
            f"• Uptime: {get_uptime()}"
        )
        sent = await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            disable_notification=True
        )
        return sent is not None
    except Exception as e:
        logger.error(f"Не удалось отправить статус админу: {e}")
        return False

async def publish_to_channel(app_id: int, bot: Bot) -> bool:
    """Публикует заявку в канал с фото или без"""
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан. Публикация невозможна.")
        return False

    app_details = get_application_details(app_id)
    if not app_details:
        logger.error(f"Заявка #{app_id} не найдена.")
        return False

    current_time = datetime.now(TIMEZONE).strftime("%H:%M")
    
    # Используйте доступ к полям через индексацию
    text = app_details['text']
    photo_id = app_details['photo_id'] if 'photo_id' in app_details else None
    
    message_text = (
        f"{text}\n\n"
        f"#НебольшойМирНиколаевск\n"
        f"🕒 {current_time}"
    )

    try:
        if photo_id:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo_id,
                caption=message_text,
                disable_notification=True
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message_text,
                disable_web_page_preview=True
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
                if app['type'] == 'congrat' and app.get('congrat_type') == 'custom':
                    if app.get('publish_date'):
                        publish_date_obj = datetime.strptime(app['publish_date'], "%Y-%m-%d").date()
                        today = datetime.now().date()
                        if publish_date_obj <= today:
                            logger.info(f"Плановая публикация пользовательского поздравления #{app['id']} (дата подошла).")
                            await publish_to_channel(app['id'], context.bot)
                            await asyncio.sleep(1)
                else:
                    logger.info(f"Публикация заявки #{app['id']} типа '{app['type']}'")
                    await publish_to_channel(app['id'], context.bot)
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ошибка публикации заявки #{app.get('id', 'N/A')}: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка проверки заявок: {str(e)}")

def is_working_hours() -> bool:
    """Проверяет, находится ли текущее время в рабочих часах."""
    now = datetime.now(TIMEZONE)

    if not (WORKING_HOURS[0] <= now.hour < WORKING_HOURS[1]):
        return False

    if not WORK_ON_WEEKENDS and now.weekday() >= 5:
        return False

    return True

def check_environment() -> bool:
    """Проверяет наличие всех необходимых файлов и переменных."""
    checks = {
        "База данных": os.path.exists(DB_FILE),
        "Файл запрещенных слов": os.path.exists(BAD_WORDS_FILE),
        "Токен бота": bool(TOKEN),
        "ID канала": bool(CHANNEL_ID),
        "Часовой пояс": str(datetime.now(TIMEZONE))
    }

    for name, status in checks.items():
        logger.info(f"{name}: {'OK' if status else 'ERROR'}")
        if not status and name != "Файл запрещенных слов":
            return False
    return True

def get_uptime() -> str:
    """Возвращает время работы бота."""
    if not BOT_STATE.get('start_time'):
        return "N/A"
    
    uptime = datetime.now(TIMEZONE) - BOT_STATE['start_time']
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{uptime.days}d {hours}h {minutes}m {seconds}s"

def validate_name(name: str) -> bool:
    """Проверяет валидность имени."""
    if len(name) < 2 or len(name) > MAX_NAME_LENGTH:
        return False
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-]+$', name))

def validate_phone(phone: str) -> bool:
    """Проверяет валидность номера телефона."""
    # Убираем все символы кроме цифр и плюса
    clean_phone = re.sub(r'[^\d+]', '', phone)
    # Проверяем формат: +7 или 8 в начале, затем 10 цифр
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

def is_holiday_active(holiday_date_str: str) -> bool:
    """Проверяет, актуален ли праздник в текущий период."""
    current_year = datetime.now().year
    holiday_date = datetime.strptime(f"{current_year}-{holiday_date_str}", "%Y-%m-%d").date()
    today = datetime.now().date()
    return (holiday_date - timedelta(days=5)) <= today <= (holiday_date + timedelta(days=5))

async def notify_admin_new_application(bot: Bot, app_id: int, app_details: dict) -> None:
    """Уведомляет администратора о новой заявке."""
    if not ADMIN_CHAT_ID:
        return

    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', app_details['type'])
    has_photo = "✅" if 'photo_id' in app_details and app_details['photo_id'] else "❌"
    
    text = (
        f"📨 Новая заявка #{app_id}\n"
        f"• Тип: {app_type}\n"
        f"• Фото: {has_photo}\n"
        f"• От: @{app_details['username'] or 'N/A'} (ID: {app_details['user_id']})\n"
        f"• Текст: {app_details['text'][:200]}...\n\n"
        "Выберите действие:"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")
        ],
        [InlineKeyboardButton("👀 Посмотреть полностью", callback_data=f"view_{app_id}")]
    ]

    await safe_send_message(
        bot,
        ADMIN_CHAT_ID,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
async def notify_user_about_decision(bot: Bot, app_details: dict, approved: bool) -> None:
    """Уведомляет пользователя о решении по его заявке."""
    user_id = app_details['user_id']
    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', app_details['type'])
    
    if approved:
        text = (
            f"🎉 Ваша заявка на {app_type} одобрена!\n"
            f"Она будет опубликована в канале {CHANNEL_NAME}."
        )
    else:
        text = (
            f"😕 Ваша заявка на {app_type} отклонена модератором.\n"
            f"Причина: нарушение правил канала."
        )

    await safe_send_message(bot, user_id, text)

def can_submit_request(user_id: int) -> bool:
    """Проверяет, может ли пользователь отправить новую заявку."""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM applications
                WHERE user_id = ? AND created_at > datetime('now', '-1 hour')
            """, (user_id,))
            count = cur.fetchone()[0]
            return count < 5
    except sqlite3.Error as e:
        logger.error(f"Ошибка проверки лимита заявок: {e}")
        return True

async def start_command(update: Update, context: CallbackContext) -> int:
    """Обработчик команды /start."""
    user = update.effective_user
    logger.info(f"Пользователь @{user.username if user else 'N/A'} запустил бота")
    context.user_data.clear()

    text = "Выберите тип заявки:"
    keyboard = []
    for key, info in REQUEST_TYPES.items():
        keyboard.append([InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)])
    keyboard += BACK_BUTTON

    await safe_reply_text(update, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор типа заявки."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
        
    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    context.user_data["type"] = query.data
    request_type = query.data

    keyboard = [
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    if request_type == "news":
        await safe_edit_message_text(query,
            f"Введите вашу новость (до {MAX_TEXT_LENGTH} символов):\n"
            f"Пример: *{EXAMPLE_TEXTS['news']}*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ANNOUNCE_TEXT_INPUT
        
    elif request_type == "congrat":
        await safe_edit_message_text(query,
            "Введите ваше имя (например: *Иванов Виталий*):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SENDER_NAME_INPUT
        
    elif request_type == "announcement":
        keyboard = [
            [InlineKeyboardButton(subtype, callback_data=f"subtype_{subtype_key}")]
            for subtype_key, subtype in ANNOUNCE_SUBTYPES.items()
        ] + [
            [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
        ]
        
        await safe_edit_message_text(query,
            "Выберите тип объявления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ANNOUNCE_SUBTYPE_SELECTION
        
    else:
        await safe_edit_message_text(query, "❌ Неизвестный тип заявки.")
        return ConversationHandler.END

async def get_sender_name(update: Update, context: CallbackContext) -> int:
    """Получает имя отправителя с примером"""
    keyboard = BACK_BUTTON  # Сохраняем стандартную кнопку возврата
    
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.\n"
            f"Пример: *{EXAMPLE_TEXTS['sender_name']}*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SENDER_NAME_INPUT

    sender_name = update.message.text.strip()
    if not validate_name(sender_name):
        await safe_reply_text(update,
            f"Пожалуйста, введите корректное имя (только буквы, пробелы и дефисы, от 2 до {MAX_NAME_LENGTH} символов).\n"
            f"Пример: *{EXAMPLE_TEXTS['sender_name']}*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SENDER_NAME_INPUT

    context.user_data["from_name"] = sender_name
    await safe_reply_text(update,
        f"Кого поздравляете? Например: *{EXAMPLE_TEXTS['recipient_name']}*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return RECIPIENT_NAME_INPUT

async def get_recipient_name(update: Update, context: CallbackContext) -> int:
    """Получает имя получателя."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.\n"
            f"Пример: *{EXAMPLE_TEXTS['recipient_name']}*",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
            parse_mode="Markdown"
        )
        return RECIPIENT_NAME_INPUT

    recipient_name = update.message.text.strip()
    if not validate_name(recipient_name):
        await safe_reply_text(update,
            f"Пожалуйста, введите корректное имя (только буквы, пробелы и дефисы, от 2 до {MAX_NAME_LENGTH} символов).\n"
            f"Пример: *сестру Викторию* или *коллектив детсада 'Солнышко'*",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
            parse_mode="Markdown"
        )
        return RECIPIENT_NAME_INPUT

    context.user_data["to_name"] = recipient_name

    keyboard = [
        [InlineKeyboardButton(holiday, callback_data=f"holiday_{holiday}")]
        for holiday in HOLIDAYS
    ] + [
        [InlineKeyboardButton("🎂 Свой праздник", callback_data="custom_congrat")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    await safe_reply_text(update,
        "Выберите праздник для поздравления:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_HOLIDAY_CHOICE

async def back_to_start(update: Update, context: CallbackContext) -> int:
    """Возврат в главное меню."""
    context.user_data.clear()
    user = update.effective_user
    logger.info(f"Пользователь @{user.username if user else 'N/A'} вернулся в начало")
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except TelegramError as e:
            logger.warning(f"Ошибка при ответе на callback в back_to_start: {e}")
    return await start_command(update, context)

async def cancel_command(update: Update, context: CallbackContext) -> int:
    """Обработчик команды /cancel."""
    user = update.effective_user
    logger.info(f"Пользователь @{user.username if user else 'N/A'} отменил действие")
    
    await safe_reply_text(
        update,
        "❌ Текущее действие отменено. Возвращаемся в главное меню.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
        ])
    )
    
    context.user_data.clear()
    return await back_to_start(update, context)

async def handle_congrat_holiday_choice(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор праздника."""
    query = update.callback_query
    if not query or not query.data:
        logger.warning("Пустой callback_query в handle_congrat_holiday_choice")
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Ошибка ответа на callback: {e}")

    if query.data == "publish_today":
        context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
        return await submit_application(update, context)

    elif query.data == "custom_congrat":
        context.user_data["congrat_type"] = "custom"
        await safe_edit_message_text(query,
            f"Введите ваше поздравление (до {MAX_CONGRAT_TEXT_LENGTH} символов):\n"
            f"Пример: *{EXAMPLE_TEXTS['congrat']['custom']}*",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
            parse_mode="Markdown"
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    elif query.data.startswith("holiday_"):
        holiday_name = query.data[8:]  # Убираем префикс "holiday_"
        if holiday_name in HOLIDAYS:
            context.user_data["congrat_type"] = "holiday"
            context.user_data["holiday_name"] = holiday_name
            
            template = HOLIDAY_TEMPLATES.get(holiday_name, "")
            from_name = context.user_data.get("from_name", "")
            to_name = context.user_data.get("to_name", "")
            
            context.user_data["text"] = f"{template}\n\nОт: {from_name}\nДля: {to_name}"
            
            keyboard = [
                [InlineKeyboardButton("📅 Опубликовать сегодня", callback_data="publish_today")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            
            await safe_edit_message_text(query,
                f"Поздравление готово:\n\n{context.user_data['text']}\n\nВыберите действие:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return CONGRAT_HOLIDAY_CHOICE
        else:
            await safe_edit_message_text(query, "❌ Неизвестный праздник.")
            return ConversationHandler.END
    else:
        await safe_edit_message_text(query, "❌ Неизвестная команда.")
        return ConversationHandler.END

async def get_custom_congrat_message(update: Update, context: CallbackContext) -> int:
    """Получает пользовательское поздравление."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст поздравления.\n"
            f"Пример: *{EXAMPLE_TEXTS['congrat']['custom']}*",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
            parse_mode="Markdown"
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    congrat_text = update.message.text.strip()
    if len(congrat_text) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(update,
            f"Поздравление слишком длинное. Максимум {MAX_CONGRAT_TEXT_LENGTH} символов.\n"
            f"Текущая длина: {len(congrat_text)} символов.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    
    context.user_data["text"] = f"{congrat_text}\n\nОт: {from_name}\nДля: {to_name}"
    
    await safe_reply_text(update,
        "Введите дату публикации поздравления в формате ДД.ММ.ГГГГ\n"
        "Например: 15.03.2024",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    return CONGRAT_DATE_INPUT

async def get_congrat_date(update: Update, context: CallbackContext) -> int:
    """Получает дату публикации поздравления."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите дату.\n"
            "Формат: ДД.ММ.ГГГГ (например: 15.03.2024)",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return CONGRAT_DATE_INPUT

    date_text = update.message.text.strip()
    try:
        publish_date = datetime.strptime(date_text, "%d.%m.%Y").date()
        today = datetime.now().date()
        
        if publish_date < today:
            await safe_reply_text(update,
                "Дата не может быть в прошлом. Введите корректную дату:",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            return CONGRAT_DATE_INPUT
            
        context.user_data["publish_date"] = publish_date.strftime("%Y-%m-%d")
        return await submit_application(update, context)
        
    except ValueError:
        await safe_reply_text(update,
            "Неверный формат даты. Используйте ДД.ММ.ГГГГ\n"
            "Например: 15.03.2024",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return CONGRAT_DATE_INPUT

async def handle_announce_subtype_selection(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор подтипа объявления."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
        
    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query: {e}")

    if query.data.startswith("subtype_"):
        subtype = query.data[8:]  # Убираем префикс "subtype_"
        context.user_data["subtype"] = subtype
        
        # Для категории "Спрос и предложения" запрашиваем телефон
        if subtype == "demand_offer":
            await safe_edit_message_text(query,
                "Введите ваш контактный телефон:\n"
                "Формат: +7-XXX-XXX-XX-XX или 8-XXX-XXX-XX-XX",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            return PHONE_INPUT
        else:
            # Для других категорий сразу переходим к вводу текста
            example_key = subtype if subtype in EXAMPLE_TEXTS["announcement"] else "ride"
            await safe_edit_message_text(query,
                f"Введите текст объявления (до {MAX_TEXT_LENGTH} символов):\n"
                f"Пример: *{EXAMPLE_TEXTS['announcement'][example_key]}*",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
                parse_mode="Markdown"
            )
            return ANNOUNCE_TEXT_INPUT
    else:
        await safe_edit_message_text(query, "❌ Неизвестный подтип объявления.")
        return ConversationHandler.END

async def get_phone_number(update: Update, context: CallbackContext) -> int:
    """Получает номер телефона для категории 'Спрос и предложения'."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите номер телефона.\n"
            "Формат: +7-XXX-XXX-XX-XX или 8-XXX-XXX-XX-XX",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return PHONE_INPUT

    phone = update.message.text.strip()
    if not validate_phone(phone):
        await safe_reply_text(update,
            "Неверный формат номера телефона.\n"
            "Используйте формат: +7-XXX-XXX-XX-XX или 8-XXX-XXX-XX-XX\n"
            "Например: +7-900-123-45-67",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return PHONE_INPUT

    context.user_data["phone_number"] = phone
    
    await safe_reply_text(update,
        f"Введите текст объявления (до {MAX_TEXT_LENGTH} символов):\n"
        f"Пример: *{EXAMPLE_TEXTS['announcement']['demand_offer']}*",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON),
        parse_mode="Markdown"
    )
    return ANNOUNCE_TEXT_INPUT

async def get_announce_text(update: Update, context: CallbackContext) -> int:
    """Получает текст объявления или новости."""
    # Проверяем наличие фото
    photo_id = None
    if update.message and update.message.photo:
        photo_id = update.message.photo[-1].file_id
        await safe_reply_text(update, "⏳ Фото получено, обрабатываем...")
        context.user_data["photo_id"] = photo_id

    # Проверяем наличие текста
    text_content = None
    if update.message and update.message.text:
        text_content = update.message.text.strip()
    elif update.message and update.message.caption:
        text_content = update.message.caption.strip()

    if not text_content:
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return ANNOUNCE_TEXT_INPUT

    # Проверяем длину текста
    max_length = MAX_ANNOUNCE_NEWS_TEXT_LENGTH if context.user_data.get("type") == "news" else MAX_TEXT_LENGTH
    if len(text_content) > max_length:
        await safe_reply_text(update,
            f"Текст слишком длинный. Максимум {max_length} символов.\n"
            f"Текущая длина: {len(text_content)} символов.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return ANNOUNCE_TEXT_INPUT

    context.user_data["text"] = text_content
    return await submit_application(update, context)

async def submit_application(update: Update, context: CallbackContext) -> int:
    """Отправляет заявку на модерацию."""
    user = update.effective_user
    
    if not can_submit_request(user.id):
        await safe_reply_text(update,
            "❌ Вы отправили слишком много заявок за последний час. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return ConversationHandler.END

    # Собираем данные заявки
    application_data = {
        "user_id": user.id,
        "username": user.username,
        "type": context.user_data.get("type"),
        "subtype": context.user_data.get("subtype"),
        "from_name": context.user_data.get("from_name"),
        "to_name": context.user_data.get("to_name"),
        "text": context.user_data.get("text"),
        "photo_id": context.user_data.get("photo_id"),
        "phone_number": context.user_data.get("phone_number"),
        "publish_date": context.user_data.get("publish_date"),
        "congrat_type": context.user_data.get("congrat_type")
    }

    # Цензурируем текст
    censored_text, has_bad_words = await censor_text(application_data["text"])
    application_data["text"] = censored_text

    # Добавляем заявку в базу данных
    app_id = add_application(application_data)
    
    if app_id:
        # Уведомляем администратора
        await notify_admin_new_application(context.bot, app_id, application_data)
        
        # Уведомляем пользователя
        request_type_name = REQUEST_TYPES.get(application_data["type"], {}).get("name", "заявка")
        await safe_reply_text(update,
            f"✅ Ваша заявка на {request_type_name} отправлена на модерацию!\n"
            f"Номер заявки: #{app_id}\n\n"
            f"Мы рассмотрим её в ближайшее время.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        
        logger.info(f"Заявка #{app_id} от пользователя @{user.username} ({user.id}) добавлена")
    else:
        await safe_reply_text(update,
            "❌ Произошла ошибка при отправке заявки. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        logger.error(f"Не удалось добавить заявку от пользователя @{user.username} ({user.id})")

    context.user_data.clear()
    return ConversationHandler.END

async def handle_admin_callback(update: Update, context: CallbackContext) -> None:
    """Обрабатывает callback-запросы от администратора."""
    query = update.callback_query
    if not query or not query.data:
        return

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Ошибка ответа на admin callback: {e}")

    if query.from_user.id != ADMIN_CHAT_ID:
        return

    data = query.data
    
    if data.startswith("approve_"):
        app_id = int(data[8:])
        if update_application_status(app_id, "approved"):
            app_details = get_application_details(app_id)
            if app_details:
                await notify_user_about_decision(context.bot, app_details, True)
                await safe_edit_message_text(query, f"✅ Заявка #{app_id} одобрена.")
            else:
                await safe_edit_message_text(query, f"❌ Заявка #{app_id} не найдена.")
        else:
            await safe_edit_message_text(query, f"❌ Ошибка при одобрении заявки #{app_id}.")
            
    elif data.startswith("reject_"):
        app_id = int(data[7:])
        if update_application_status(app_id, "rejected"):
            app_details = get_application_details(app_id)
            if app_details:
                await notify_user_about_decision(context.bot, app_details, False)
                await safe_edit_message_text(query, f"❌ Заявка #{app_id} отклонена.")
            else:
                await safe_edit_message_text(query, f"❌ Заявка #{app_id} не найдена.")
        else:
            await safe_edit_message_text(query, f"❌ Ошибка при отклонении заявки #{app_id}.")
            
    elif data.startswith("view_"):
        app_id = int(data[5:])
        app_details = get_application_details(app_id)
        if app_details:
            app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', app_details['type'])
            text = (
                f"📋 Заявка #{app_id}\n"
                f"• Тип: {app_type}\n"
                f"• От: @{app_details['username'] or 'N/A'} (ID: {app_details['user_id']})\n"
                f"• Дата: {app_details['created_at']}\n\n"
                f"Текст:\n{app_details['text']}"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")
                ]
            ]
            
            if app_details['photo_id']:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=app_details['photo_id'],
                    caption=text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await safe_edit_message_text(query, f"❌ Заявка #{app_id} не найдена.")

async def handle_message(update: Update, context: CallbackContext) -> None:
    """Обрабатывает обычные сообщения вне диалога."""
    user = update.effective_user
    logger.info(f"Получено сообщение от @{user.username if user else 'N/A'}: {update.message.text[:50] if update.message and update.message.text else 'Медиа'}")
    
    await safe_reply_text(update,
        "Для начала работы с ботом используйте команду /start",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Начать", callback_data="back_to_start")]
        ])
    )

def create_conversation_handler() -> ConversationHandler:
    """Создает обработчик диалога."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
        ],
        states={
            TYPE_SELECTION: [
                CallbackQueryHandler(handle_type_selection, pattern="^(congrat|announcement|news)$"),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            SENDER_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_sender_name),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            RECIPIENT_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_recipient_name),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            CONGRAT_HOLIDAY_CHOICE: [
                CallbackQueryHandler(handle_congrat_holiday_choice),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_congrat_message),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            CONGRAT_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_congrat_date),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            ANNOUNCE_SUBTYPE_SELECTION: [
                CallbackQueryHandler(handle_announce_subtype_selection, pattern="^subtype_"),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            PHONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ],
            ANNOUNCE_TEXT_INPUT: [
                MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, get_announce_text),
                CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
        ],
        allow_reentry=True
    )

async def setup_bot() -> Application:
    """Настраивает и возвращает экземпляр бота."""
    global application
    
    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения")
    
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(create_conversation_handler())
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^(approve_|reject_|view_)"))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    return application

async def setup_scheduler(bot_app: Application) -> AsyncIOScheduler:
    """Настраивает планировщик задач."""
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    
    # Добавляем задачу проверки заявок каждые 5 минут
    scheduler.add_job(
        check_pending_applications,
        'interval',
        minutes=5,
        args=[bot_app.create_context()],
        id='check_applications'
    )
    
    # Добавляем задачу очистки старых заявок раз в день
    scheduler.add_job(
        cleanup_old_applications,
        'cron',
        hour=2,
        minute=0,
        id='cleanup_old_applications'
    )
    
    return scheduler

@app.on_event("startup")
async def startup_event():
    """Инициализация при запуске."""
    global application
    
    logger.info("Запуск бота...")
    BOT_STATE['start_time'] = datetime.now(TIMEZONE)
    BOT_STATE['running'] = True
    
    # Проверяем окружение
    if not check_environment():
        logger.error("Проверка окружения не пройдена")
        sys.exit(1)
    
    # Инициализируем базу данных
    init_db()
    
    # Настраиваем бота
    application = await setup_bot()
    
    # Настраиваем планировщик
    scheduler = await setup_scheduler(application)
    scheduler.start()
    
    # Настраиваем webhook
    if WEBHOOK_URL:
        await application.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            secret_token=WEBHOOK_SECRET
        )
        logger.info(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    
    # Отправляем статус админу
    await send_bot_status(application.bot, "Запущен")
    
    logger.info("Бот успешно запущен")

@app.on_event("shutdown")
async def shutdown_event():
    """Очистка при остановке."""
    global application
    
    logger.info("Остановка бота...")
    BOT_STATE['running'] = False
    
    if application:
        await send_bot_status(application.bot, "Остановлен")
        await application.shutdown()
    
    logger.info("Бот остановлен")

@app.post("/webhook")
async def webhook_handler(request: Request):
    """Обработчик webhook."""
    try:
        # Проверяем секретный токен
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if token != WEBHOOK_SECRET:
                raise HTTPException(status_code=403, detail="Неверный секретный токен")
        
        # Получаем данные
        data = await request.json()
        update = Update.de_json(data, application.bot)
        
        # Обрабатываем обновление
        await application.process_update(update)
        
        return JSONResponse({"status": "ok"})
        
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса."""
    return {
        "status": "ok",
        "uptime": get_uptime(),
        "running": BOT_STATE['running'],
        "timestamp": datetime.now(TIMEZONE).isoformat()
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

