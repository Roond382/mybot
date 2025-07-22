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
        "offer": "Продаю диван (новый). 8000₽. Фото в ЛС.",
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
    WAIT_CENSOR_APPROVAL
) = range(9)

# ========== ТИПЫ ЗАПРОСОВ ==========
REQUEST_TYPES = {
    "congrat": {"name": "Поздравление", "icon": "🎉"},
    "announcement": {"name": "Объявление", "icon": "📢"},
    "news": {"name": "Новость от жителя", "icon": "🗞️"}
}

# ========== ПОДТИПЫ ОБЪЯВЛЕНИЙ ==========
ANNOUNCE_SUBTYPES = {
    "ride": "🚗 Попутка",
    "offer": "💡 Предложение",
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
                (user_id, username, type, subtype, from_name, to_name, text, photo_id, publish_date, congrat_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    data['user_id'],
                    data.get('username'),
                    data['type'],
                    data.get('subtype'),
                    data.get('from_name'),
                    data.get('to_name'),
                    data['text'],
                    data.get('photo_id'),
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
        return await complete_request(update, context)

    if query.data == "custom_date":
        keyboard_nav = [
            [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
            [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
        ]
        await safe_edit_message_text(query,
            "📅 Введите дату публикации в формате ДД-ММ-ГГГГ или 'сегодня':",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return CONGRAT_DATE_INPUT
    
    if query.data == "custom_congrat":
        return await process_custom_congrat_message(update, context)

    holiday = query.data.replace("holiday_", "")
    if holiday not in HOLIDAYS:
        await safe_edit_message_text(query,
            "❌ Неизвестный праздник. Пожалуйста, выберите из списка.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Выбрать другой праздник", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]
            ]))
        return ConversationHandler.END

    holiday_date_str = HOLIDAYS[holiday]
    if not is_holiday_active(holiday_date_str):
        current_year = datetime.now().year
        holiday_date_obj = datetime.strptime(f"{current_year}-{holiday_date_str}", "%Y-%m-%d")
        start_date = (holiday_date_obj - timedelta(days=5)).strftime("%d.%m")
        end_date = (holiday_date_obj + timedelta(days=5)).strftime("%d.%m")
        
        await safe_edit_message_text(query,
            f"❌ Праздник «{holiday}» актуален только с {start_date} по {end_date}.\n\n"
            "Вы можете:\n"
            "1. Выбрать другой праздник\n"
            "2. Создать своё поздравление (кнопка ниже)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎂 Своё поздравление", callback_data="custom_congrat")],
                [InlineKeyboardButton("🔙 Выбрать другой праздник", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]
            ]))
        return CONGRAT_HOLIDAY_CHOICE

    context.user_data["congrat_type"] = holiday
    template = HOLIDAY_TEMPLATES.get(holiday, "С праздником!")
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")

    formatted_text = f"{from_name} поздравляет {to_name} с {holiday}!\n\n{template}"
    context.user_data["text"] = formatted_text

    censored_text, has_bad = await censor_text(formatted_text)
    if has_bad:
        context.user_data["censored_text"] = censored_text
        await safe_edit_message_text(query,
            "⚠️ В тексте найдены запрещённые слова (заменены на ***):\n\n"
            f"{censored_text}\n\n"
            "Подтвердите или измените текст:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить", callback_data="accept")],
                [InlineKeyboardButton("✏️ Изменить текст", callback_data="edit")],
                [InlineKeyboardButton("🔙 Выбрать другой праздник", callback_data="back_to_holiday_choice")]
            ]))
        return WAIT_CENSOR_APPROVAL

    keyboard = [
        [InlineKeyboardButton("📅 Опубликовать сегодня", callback_data="publish_today")],
        [InlineKeyboardButton("🗓️ Указать другую дату", callback_data="custom_date")],
        [InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]
    ]

    await safe_edit_message_text(query,
        f"🎉 Текст поздравления готов!\n\n{formatted_text}\n\n"
        "Выберите дату публикации:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_DATE_INPUT

async def process_custom_congrat_message(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод пользовательского поздравления с примером"""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except TelegramError as e:
            logger.warning(f"Ошибка ответа на callback: {e}")

    context.user_data["congrat_type"] = "custom"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    await safe_reply_text(update,
        f"Введите текст поздравления (до {MAX_TEXT_LENGTH} символов):\n"
        f"Пример: *{EXAMPLE_TEXTS['congrat']['custom']}*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CUSTOM_CONGRAT_MESSAGE_INPUT

async def process_congrat_text(update: Update, context: CallbackContext) -> int:
    """Обрабатывает текст поздравления."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.\n"
            "ℹ️ Вы можете прикрепить фото к сообщению (отправьте его как изображение с подписью)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    text = update.message.text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update,
            f"Текст слишком длинный. Пожалуйста, сократите его до {MAX_TEXT_LENGTH} символов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    if len(text) < 10:
        await safe_reply_text(update,
            "Текст слишком короткий. Пожалуйста, введите более длинный текст (минимум 10 символов).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    formatted_text = f"{from_name} поздравляет {to_name}!\n\n{text}"
    context.user_data["text"] = formatted_text

    censored_text, has_bad = await censor_text(formatted_text)
    if has_bad:
        context.user_data["censored_text"] = censored_text
        await safe_reply_text(update,
            "⚠️ В вашем тексте обнаружены запрещенные слова. Они будут заменены на ***.\n\n"
            f"Предварительный просмотр:\n\n{censored_text}\n\n"
            "Вы согласны с цензурой?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Принять", callback_data="accept")],
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ))
        return WAIT_CENSOR_APPROVAL

    keyboard = [
        [InlineKeyboardButton("📅 Опубликовать сегодня", callback_data="publish_today")],
        [InlineKeyboardButton("🗓️ Указать другую дату", callback_data="custom_date")],
        [InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]
    ]
    await safe_reply_text(update,
        f"🎉 Текст поздравления готов!\n\n{formatted_text}\n\n"
        "Выберите дату публикации:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_DATE_INPUT
    
async def back_to_holiday_choice(update: Update, context: CallbackContext) -> int:
    """Возвращает к выбору праздника."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except TelegramError as e:
            logger.warning(f"Ошибка при ответе на callback: {e}")

    keyboard = [
        [InlineKeyboardButton(holiday, callback_data=f"holiday_{holiday}")]
        for holiday in HOLIDAYS
    ] + [
        [InlineKeyboardButton("🎂 Свой праздник", callback_data="custom_congrat")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    await safe_edit_message_text(query,
        "Выберите праздник для поздравления:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_HOLIDAY_CHOICE

async def handle_announce_subtype_selection(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор подтипа объявления с примером"""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Ошибка ответа на callback: {e}")

    subtype_key = query.data.replace("subtype_", "")
    if subtype_key not in ANNOUNCE_SUBTYPES:
        await safe_edit_message_text(query,
            "Неизвестный тип объявления. Пожалуйста, начните заново.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON))  # Добавлена закрывающая скобка
        return ConversationHandler.END

    context.user_data["subtype"] = subtype_key
    example_text = EXAMPLE_TEXTS["announcement"][subtype_key]
    
    keyboard = [
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    await safe_edit_message_text(query,
        f"Вы выбрали: {ANNOUNCE_SUBTYPES[subtype_key]}\n\n"
        f"Введите текст объявления (до {MAX_TEXT_LENGTH} символов):\n"
        f"Пример: *{example_text}*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ANNOUNCE_TEXT_INPUT

async def process_announce_news_text(update: Update, context: CallbackContext) -> int:
    """Обрабатывает текст объявления или новости."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return ANNOUNCE_TEXT_INPUT

    text = update.message.text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update,
            f"Текст слишком длинный. Пожалуйста, сократите его до {MAX_TEXT_LENGTH} символов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return ANNOUNCE_TEXT_INPUT

    if len(text) < 10:
        await safe_reply_text(update,
            "Текст слишком короткий. Пожалуйста, введите более длинный текст (минимум 10 символов).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return ANNOUNCE_TEXT_INPUT

    request_type = context.user_data.get("type")
    username = f"@{update.effective_user.username}" if update.effective_user.username else "пользователя"

    if request_type == "news":
        formatted_text = f"🗞️ Новость от {username}\n\n{text}"
    elif request_type == "announcement":
        subtype_key = context.user_data.get("subtype")
        if subtype_key in ANNOUNCE_SUBTYPES:
            subtype_emoji = ANNOUNCE_SUBTYPES[subtype_key].split()[0]
            formatted_text = f"{subtype_emoji} Объявление от {username}\n\n{text}"
        else:
            formatted_text = f"📢 Объявление от {username}\n\n{text}"
    else:
        formatted_text = text

    context.user_data["text"] = formatted_text

    censored_text, has_bad = await censor_text(formatted_text)
    if has_bad:
        context.user_data["censored_text"] = censored_text
        await safe_reply_text(update,
            "⚠️ В вашем тексте обнаружены запрещенные слова. Они будут заменены на ***.\n\n"
            f"Предварительный просмотр:\n\n{censored_text}\n\n"
            "Вы согласны с цензурой?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Принять", callback_data="accept")],
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]))
        return WAIT_CENSOR_APPROVAL

    return await complete_request(update, context, formatted_text)

async def handle_censor_choice(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор пользователя после цензуры."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    choice = query.data
    current_type = context.user_data.get("type")

    if choice == "accept":
        if current_type == "congrat" and context.user_data.get("congrat_type") == "custom":
            keyboard_nav_date = [
                [InlineKeyboardButton("📅 Опубликовать сегодня", callback_data="publish_today")],
                [InlineKeyboardButton("🗓️ Указать другую дату", callback_data="custom_date")],
                [InlineKeyboardButton("🔙 Вернуться к вводу текста", callback_data="back_to_custom_message")],
                [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            await safe_edit_message_text(query,
                "Текст принят с изменениями фильтра. Выберите дату публикации:",
                reply_markup=InlineKeyboardMarkup(keyboard_nav_date))
            return CONGRAT_DATE_INPUT
        else:
            context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
            return await complete_request(update, context)

    elif choice == "edit":
        if current_type == "congrat" and context.user_data.get("congrat_type") == "custom":
            keyboard_nav_custom = [
                [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            await safe_edit_message_text(query,
                f"Введите исправленный текст поздравления (до {MAX_TEXT_LENGTH} символов):",
                reply_markup=InlineKeyboardMarkup(keyboard_nav_custom))
            return CUSTOM_CONGRAT_MESSAGE_INPUT
        else:
            keyboard_nav = [
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            await safe_edit_message_text(query,
                f"Введите исправленный текст (до {MAX_TEXT_LENGTH} символов):",
                reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return ANNOUNCE_TEXT_INPUT
    else:
        keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
        await safe_edit_message_text(query,
            "Некорректный выбор.",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return WAIT_CENSOR_APPROVAL
        
async def complete_request(update: Update, context: CallbackContext, text: Optional[str] = None) -> int:
    """Завершает обработку заявки с транзакционной безопасностью."""
    user = update.effective_user
    if not user:
        logger.error("Не удалось идентифицировать пользователя")
        await safe_reply_text(
            update,
            "❌ Ошибка: не удалось идентифицировать пользователя.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        return ConversationHandler.END

    try:
        user_data = context.user_data
        final_text = text or user_data.get("censored_text") or user_data.get("text", "")
        
        if not final_text or 'type' not in user_data:
            logger.error("Недостаточно данных для создания заявки")
            await safe_reply_text(
                update,
                "❌ Ошибка: недостаточно данных для создания заявки.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
            )
            return ConversationHandler.END

        # Проверка лимита заявок
        if not await check_spam(update, context):
            return ConversationHandler.END

        # Подготовка данных с валидацией
        app_data = {
            'user_id': user.id,
            'username': user.username,
            'type': user_data['type'],
            'text': final_text,
            'photo_id': user_data.get('photo_id'),
            'from_name': user_data.get('from_name', ''),  # Добавлены значения по умолчанию
            'to_name': user_data.get('to_name', ''),
            'congrat_type': user_data.get('congrat_type'),
            'publish_date': user_data.get('publish_date'),
            'subtype': user_data.get('subtype')
        }

        # Транзакционная обработка БД
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("""
                INSERT INTO applications
                (user_id, username, type, subtype, from_name, to_name, text, photo_id, publish_date, congrat_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                app_data['user_id'],
                app_data.get('username'),
                app_data['type'],
                app_data.get('subtype'),
                app_data.get('from_name'),
                app_data.get('to_name'),
                app_data['text'],
                app_data.get('photo_id'),
                app_data.get('publish_date'),
                app_data.get('congrat_type')
            ))
            
            app_id = cur.lastrowid
            conn.commit()
            logger.info(f"Заявка #{app_id} успешно сохранена")
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка БД: {str(e)}", exc_info=True)
            if conn:
                conn.rollback()
            await safe_reply_text(
                update,
                "❌ Ошибка сохранения заявки. Попробуйте позже.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
            )
            return ConversationHandler.END
        finally:
            if conn:
                conn.close()

        # Уведомление администратора
        await notify_admin_new_application(context.bot, app_id, app_data)

        # Подтверждение пользователю
        request_type = REQUEST_TYPES.get(user_data['type'], {}).get('name', user_data['type'])
        await safe_reply_text(
            update,
            f"✅ Ваша заявка на {request_type} успешно создана (№{app_id})!\n"
            "Она будет опубликована после проверки модератором.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )

        return ConversationHandler.END

    except Exception as e:
        logger.critical(f"Критическая ошибка: {str(e)}", exc_info=True)
        await safe_reply_text(
            update,
            "❌ Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        return ConversationHandler.END
    finally:
        # Очистка контекста в любом случае
        context.user_data.clear()
        
async def process_congrat_date(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод даты публикации."""
    keyboard_nav = [
        [InlineKeyboardButton("🔙 Вернуться к тексту", callback_data="back_to_custom_message")],
        [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

    if update.callback_query:
        callback_data = update.callback_query.data
        try:
            await update.callback_query.answer()
        except TelegramError as e:
            logger.warning(f"Ошибка при ответе на callback: {e}")

        if callback_data == "publish_today":
            context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
            return await complete_request(update, context)
        elif callback_data == "custom_date":
            await safe_edit_message_text(update.callback_query,
                "📅 Введите дату публикации в формате ДД-ММ-ГГГГ или 'сегодня':",
                reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return CONGRAT_DATE_INPUT
        elif callback_data == "back_to_start":
            return await start_command(update, context)
        elif callback_data == "back_to_holiday_choice":
            return await back_to_holiday_choice(update, context)
        elif callback_data == "back_to_custom_message":
            return await back_to_custom_message(update, context)
        else:
            await safe_edit_message_text(update.callback_query,
                "❌ Неизвестная кнопка. Пожалуйста, введите дату или выберите из предложенных вариантов.",
                reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return CONGRAT_DATE_INPUT

    if not update.message or not update.message.text:
        await safe_reply_text(update,
            "❌ Не получен текст. Введите дату в формате ДД-ММ-ГГГГ или 'сегодня'.",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return CONGRAT_DATE_INPUT

    date_str = update.message.text.strip().lower().replace('.', '-')
    
    if date_str == "сегодня":
        context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
        return await complete_request(update, context)

    try:
        publish_date = datetime.strptime(date_str, "%d-%m-%Y")
        today = datetime.now().date()

        if publish_date.date() < today:
            await safe_reply_text(update,
                "❌ Нельзя указать прошедшую дату. Введите сегодняшнюю или будущую дату (ДД-ММ-ГГГГ):",
                reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return CONGRAT_DATE_INPUT

        context.user_data["publish_date"] = publish_date.strftime("%Y-%m-%d")
        return await complete_request(update, context)

    except ValueError:
        await safe_reply_text(update,
            "❌ Неверный формат даты. Введите дату в формате ДД-ММ-ГГГГ или 'сегодня':",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return CONGRAT_DATE_INPUT

async def back_to_custom_message(update: Update, context: CallbackContext) -> int:
    """Возвращает к вводу текста поздравления."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except TelegramError as e:
            logger.warning(f"Ошибка при ответе на callback: {e}")

    await safe_edit_message_text(query,
        f"✏️ Введите текст поздравления (до {MAX_TEXT_LENGTH} символов):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
            [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
        ]))
    return CUSTOM_CONGRAT_MESSAGE_INPUT

async def handle_admin_decision(update: Update, context: CallbackContext) -> None:
    """Обрабатывает решение администратора по заявке."""
    query = update.callback_query
    if not query or not query.data:
        return

    action, app_id = query.data.split('_', 1)
    app_id = int(app_id)
    app_details = get_application_details(app_id)

    if not app_details:
        await query.answer("Заявка не найдена!")
        return

    if action == "approve":
        update_application_status(app_id, "approved")
        await query.edit_message_text(f"✅ Заявка #{app_id} одобрена!")
        await notify_user_about_decision(context.bot, app_details, approved=True)

        should_publish_immediately = True
        publication_reason = "по умолчанию (объявление, новость или поздравление на сегодня/прошлое)"

        if app_details['type'] == 'congrat' and app_details['congrat_type'] == 'custom':
            if app_details['publish_date']:
                publish_date_obj = datetime.strptime(app_details['publish_date'], "%Y-%m-%d").date()
                today = datetime.now().date()
                if publish_date_obj > today:
                    should_publish_immediately = False
                    publication_reason = f"по расписанию (дата: {app_details['publish_date']})"
        
        if should_publish_immediately:
            logger.info(f"Попытка немедленной публикации заявки #{app_id} (Тип: {app_details['type']}). Причина: {publication_reason}.")
            await publish_to_channel(app_id, context.bot)
        else:
            logger.info(f"Заявка #{app_id} одобрена, но будет опубликована позже. Причина: {publication_reason}.")

    elif action == "reject":
        update_application_status(app_id, "rejected")
        await query.edit_message_text(f"❌ Заявка #{app_id} отклонена!")
        await notify_user_about_decision(context.bot, app_details, approved=False)
    elif action == "view":
        full_text = (
            f"📋 Полный текст заявки #{app_id}:\n\n"
            f"{app_details['text']}\n\n"
            f"Статус: {app_details['status']}"
        )
        await query.answer()
        await safe_send_message(
            context.bot,
            ADMIN_CHAT_ID,
            full_text,
            reply_to_message_id=query.message.message_id
        )

async def handle_photo_message(update: Update, context: CallbackContext) -> int:
    """
    Обрабатывает сообщения с фотографиями для новостей, объявлений и поздравлений.
    """
    if not update.message or not update.message.photo:
        logger.warning("Получено сообщение без фото")
        await safe_reply_text(
            update,
            "❌ Пожалуйста, отправьте фото с подписью.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        return ConversationHandler.END

    try:
        # Получаем фото с оптимальным качеством
        photo = update.message.photo[-2]
        file = await context.bot.get_file(photo.file_id)
        
        # Проверка размера файла
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
        if file.file_size > MAX_FILE_SIZE:
            logger.warning(f"Файл слишком большой: {file.file_size} байт")
            await safe_reply_text(
                update,
                "❌ Файл слишком большой (максимум 10MB). Пожалуйста, сожмите фото.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
            )
            return ConversationHandler.END

        # Сохраняем данные
        context.user_data["photo_id"] = photo.file_id
        context.user_data["text"] = update.message.caption or ""

        # Уведомление о получении фото
        await safe_reply_text(
            update,
            "⏳ Фото получено, обрабатываем...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )

        # Если есть подпись - обрабатываем, иначе запрашиваем текст
        if context.user_data["text"].strip():
            request_type = context.user_data.get("type")
            if request_type == "congrat":
                return await process_congrat_text(update, context)
            else:
                return await process_announce_news_text(update, context)
        
        await safe_reply_text(
            update,
            "📝 Введите текст для вашего сообщения:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        return CUSTOM_CONGRAT_MESSAGE_INPUT if context.user_data.get("type") == "congrat" else ANNOUNCE_TEXT_INPUT

    except Exception as e:
        logger.error(f"Ошибка обработки фото: {str(e)}", exc_info=True)
        await safe_reply_text(
            update,
            "⚠️ Произошла ошибка при обработке фото. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        return ConversationHandler.END
        
async def check_spam(update: Update, context: CallbackContext) -> bool:
    """Проверяет пользователя на спам."""
    user = update.effective_user
    if not can_submit_request(user.id):
        await safe_reply_text(update,
            "🔙 Вы отправили слишком много заявок за последнее время.\n"
            "Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        return False
    return True
	
async def help_command(update: Update, context: CallbackContext) -> None:
    """Обработчик команды /help"""
    help_text = (
        "ℹ️ <b>Как работает бот</b>:\n\n"
        "1. Нажмите /start — появится меню.\n"
        "2. Выберите тип:\n"
        "   • 🎉 <b>Поздравление</b> — на праздник\n"
        "   • 📢 <b>Объявление</b> — попутка, потеряли/нашли\n"
        "   • 🗞️ <b>Новость</b> — от жителя города\n\n"
        "3. Заполните форму.\n"
        "4. Модератор проверит.\n"
        "5. Если одобрено — появится в канале.\n\n"
        "❗ Можно прикреплять фото к объявлениям и новостям.\n"
        "⏰ Работаем круглосуточно."
    )
    keyboard = [
        [InlineKeyboardButton("🚀 Начать", callback_data="back_to_start")]
    ]
    await safe_reply_text(
        update,
        help_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
async def unknown_message_fallback(update: Update, context: CallbackContext) -> int:
    """Обработчик для неизвестных сообщений."""
    logger.info(f"Получено неизвестное сообщение от @{update.effective_user.username}: {update.message.text}")
    await safe_reply_text(update,
        "Извините, я не понял вашу команду. Пожалуйста, используйте кнопки или команду /start.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]])
    )
    return ConversationHandler.END

def setup_handlers(application: Application) -> None:
    """Настройка всех обработчиков команд."""
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            TYPE_SELECTION: [CallbackQueryHandler(handle_type_selection)],
            SENDER_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_sender_name),
            ],
            RECIPIENT_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_recipient_name),
            ],
            CONGRAT_HOLIDAY_CHOICE: [CallbackQueryHandler(handle_congrat_holiday_choice)],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_congrat_text),
                MessageHandler(filters.PHOTO, handle_photo_message)  # Добавлен обработчик фото
            ],
            CONGRAT_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_congrat_date),
                CallbackQueryHandler(process_congrat_date)
            ],
            ANNOUNCE_SUBTYPE_SELECTION: [CallbackQueryHandler(handle_announce_subtype_selection)],
            ANNOUNCE_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_announce_news_text),
                MessageHandler(filters.PHOTO, handle_photo_message)
            ],
            WAIT_CENSOR_APPROVAL: [CallbackQueryHandler(handle_censor_choice)]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_command),
            CallbackQueryHandler(back_to_start, pattern="^back_to_start$"),
            CallbackQueryHandler(back_to_holiday_choice, pattern="^back_to_holiday_choice$"),
            CallbackQueryHandler(back_to_custom_message, pattern="^back_to_custom_message$"),
            MessageHandler(filters.ALL, unknown_message_fallback)
        ],
        per_message=False
    )
    
    application.add_handler(conv_handler)
    application.add_handler(
        CallbackQueryHandler(handle_admin_decision, pattern=r"^(approve|reject|view)_\d+$"),
        group=2
    )
    # Обработчик команды /help
    application.add_handler(CommandHandler('help', help_command))
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
            await send_bot_status(application.bot, "🟢 Бот успешно запущен!", force_send=True)
        else:
            logger.warning("ID админа не задан — уведомление не отправлено.")

        logger.info("Бот успешно запущен")
    except Exception as e:
        logger.critical(f"Ошибка запуска бота: {e}", exc_info=True)
        BOT_STATE['running'] = False
        raise
        
@app.post("/webhook")
async def webhook_handler(request: Request):
    """Обработчик вебхука от Telegram."""
    try:
        client_ip = request.client.host if request.client else "unknown"
        logger.info(f"Входящий вебхук от IP: {client_ip}")

        # Проверка секретного токена
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if not token:
                logger.warning("Отсутствует секретный токен")
                raise HTTPException(status_code=403, detail="Secret token required")
            
            if not secrets.compare_digest(token, WEBHOOK_SECRET):
                logger.warning("Неверный секретный токен")
                raise HTTPException(status_code=403, detail="Invalid token")

        # Проверка инициализации приложения
        if not application or not hasattr(application, 'update_queue'):
            logger.error("Приложение бота не инициализировано")
            raise HTTPException(status_code=503, detail="Bot application not initialized")

        # Обработка обновления
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        await application.update_queue.put(update)
        
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
        
def main():
    """Точка входа для запуска сервера."""
    # Инициализация БД
    init_db()
    if not check_environment():
        logger.error("Проверка окружения не пройдена!")
        sys.exit(1)
    # Запуск сервера
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

@app.get("/")
async def health_check():
    return {"status": "ok", "bot_running": BOT_STATE.get('running', False)}

if __name__ == "__main__":
    main()
