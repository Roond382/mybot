import os
import sys
import logging
from datetime import datetime, timedelta
import socket
import atexit
import signal
import re
import traceback
from typing import Dict, Any, Optional, Tuple
import asyncio
import aiofiles
import sqlite3
import pytz
from dotenv import load_dotenv
from telegram import __version__ as ptb_ver, Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Глобальная переменная для отслеживания состояния
BOT_STATE = {
    'running': False,
    'start_time': None,
    'last_activity': None
}

# Константы
DB_FILE = 'db.sqlite'
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50
MAX_TEXT_LENGTH = 4000
MAX_CONGRAT_TEXT_LENGTH = 500
MAX_ANNOUNCE_NEWS_TEXT_LENGTH = 300
CHANNEL_NAME = "Небольшой Мир: Николаевск"

# Настройки из .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID") else None
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None
TIMEZONE = pytz.timezone('Europe/Moscow')
WORKING_HOURS = (0, 23)  # 00:00-23:59 (круглосуточно)
WORK_ON_WEEKENDS = True

# Проверка обязательных переменных окружения
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения")
if not CHANNEL_ID:
    logging.warning("CHANNEL_ID не задан в переменных окружения. Публикация в канал невозможна")
if not ADMIN_CHAT_ID:
    logging.warning("ADMIN_CHAT_ID не задан в переменных окружения. Уведомления администратора не будут отправляться")

# Типы запросов
REQUEST_TYPES = {
    "congrat": {"name": "Поздравление", "icon": "🎉"},
    "announcement": {"name": "Объявление", "icon": "📢"},
    "news": {"name": "Новость от жителя", "icon": "🗞️"}
}

# Подтипы объявлений
ANNOUNCE_SUBTYPES = {
    "ride": "🚗 Попутка",
    "offer": "💡 Предложение",
    "lost": "🔍 Потеряли/Нашли"
}

# Праздники
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

# Состояния диалога
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

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def cleanup():
    """Функция очистки при завершении работы"""
    if BOT_STATE['running']:
        logger.info("Завершение работы бота...")
        if ADMIN_CHAT_ID:
            try:
                bot = Bot(token=TOKEN)
                asyncio.run(send_bot_status(bot, "Завершение работы"))
            except Exception as e:
                logger.error(f"Ошибка при отправке статуса завершения: {e}")
    global lock_socket
    if 'lock_socket' in globals():
        lock_socket.close()

def handle_signal(signum, frame):
    """Обработчик сигналов"""
    logger.info(f"Получен сигнал {signum}, завершаем работу...")
    cleanup()
    sys.exit(0)

# Регистрируем обработчики сигналов
signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

try:
    # Проверка на уже запущенный экземпляр
    lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    lock_socket.bind('\0' + 'bot_lock')
except socket.error:
    print("Бот уже запущен")
    exit(0)

def is_working_hours() -> bool:
    """Проверяет, находится ли текущее время в рабочих часах (8:00-23:00 по Москве)"""
    now = datetime.now(TIMEZONE)

    # Проверка рабочего времени
    if not (WORKING_HOURS[0] <= now.hour < WORKING_HOURS[1]):
        return False

    # Проверка выходных, если WORK_ON_WEEKENDS = False
    if not WORK_ON_WEEKENDS and now.weekday() >= 5:  # 5 и 6 - суббота и воскресенье
        return False

    return True
    # --- Функции работы с БД ---
def get_db_connection() -> sqlite3.Connection:
    """Устанавливает соединение с базой данных."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Инициализирует таблицы базы данных с индексами."""
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
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                publish_date DATE,
                published_at TIMESTAMP,
                congrat_type TEXT
            )
            """)

            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approved_unpublished
            ON applications(status, published_at)
            WHERE status = 'approved' AND published_at IS NULL
            """)

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
            (user_id, username, type, subtype, from_name, to_name, text, publish_date, congrat_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['user_id'],
                data.get('username'),
                data['type'],
                data.get('subtype'),
                data.get('from_name'),
                data.get('to_name'),
                data['text'],
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

# --- Валидация и цензура ---
def validate_name(name: str) -> bool:
    """Проверяет валидность имени."""
    if not name or not name.strip():
        return False

    name = name.strip()
    allowed_chars = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ -")
    return (2 <= len(name) <= MAX_NAME_LENGTH and
            all(c in allowed_chars for c in name) and
            not name.startswith('-') and
            not name.endswith('-') and
            '--' not in name)

def is_holiday_active(holiday_date_str: str) -> bool:
    """Проверяет, активен ли праздник (+/-5 дней от даты)."""
    try:
        current_year = datetime.now().year
        holiday_date = datetime.strptime(f"{current_year}-{holiday_date_str}", "%Y-%m-%d").date()
        today = datetime.now().date()
        start = holiday_date - timedelta(days=5)
        end = holiday_date + timedelta(days=5)
        return start <= today <= end
    except Exception as e:
        logger.error(f"Ошибка проверки праздника: {e}", exc_info=True)
        return False

async def load_bad_words() -> list:
    """Асинхронно загружает список запрещенных слов."""
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

    # Цензура контактной информации
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
    # --- Вспомогательные функции ---
async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправляет сообщение с подробной обработкой ошибок."""
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

async def send_bot_status(bot: Bot, status: str):
    """Отправляет статус бота администратору"""
    if ADMIN_CHAT_ID and bot:
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🤖 Статус бота: {status}\n"
                     f"• Время: {datetime.now(TIMEZONE).strftime('%H:%M %d.%m.%Y')}\n"
                     f"• Рабочее время: {'Да' if is_working_hours() else 'Нет'}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки статуса: {e}")

async def publish_to_channel(app_id: int, bot: Bot) -> bool:
    """Публикует заявку в канал с обработкой ошибок."""
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан. Публикация невозможна.")
        return False

    app_details = get_application_details(app_id)
    if not app_details:
        logger.error(f"Заявка #{app_id} не найдена.")
        return False

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=app_details['text']
        )
        mark_application_as_published(app_id)
        logger.info(f"Заявка #{app_id} опубликована в канале {CHANNEL_ID}")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации заявки #{app_id}: {str(e)}")
        return False

async def check_pending_applications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет и публикует одобренные заявки."""
    applications = get_approved_unpublished_applications()
    for app in applications:
        if app['type'] in ['news', 'announcement'] or (
            app['publish_date'] and
            datetime.strptime(app['publish_date'], "%Y-%m-%d").date() <= datetime.now().date()
        ):
            await publish_to_channel(app['id'], context.bot)
            await asyncio.sleep(1)

async def check_shutdown_time(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет, не вышло ли время работы бота (23:00) и корректно останавливает его"""
    if not is_working_hours():
        logger.info("Рабочее время закончилось. Останавливаем бота.")
        await send_bot_status(context, "Остановка (рабочее время закончилось)")
        
        try:
            await context.application.stop()
            os._exit(0)
        except Exception as e:
            logger.error(f"Ошибка при остановке бота: {e}")
            os._exit(1)

def check_environment():
    """Проверяет наличие всех необходимых файлов и переменных"""
    required_files = [DB_FILE, BAD_WORDS_FILE]
    missing_files = [f for f in required_files if not os.path.exists(f)]

    if missing_files:
        logger.error(f"Отсутствуют файлы: {missing_files}")
        if ADMIN_CHAT_ID:
            asyncio.run(safe_send_message(
                Bot(token=TOKEN),
                ADMIN_CHAT_ID,
                f"❌ Отсутствуют файлы: {', '.join(missing_files)}"
            ))
        return False
    return True

def check_bot_health():
    """Проверяет все критические компоненты перед запуском"""
    checks = {
        "База данных": os.path.exists(DB_FILE),
        "Файл запрещенных слов": os.path.exists(BAD_WORDS_FILE),
        "Переменные окружения": all([TOKEN, CHANNEL_ID, ADMIN_CHAT_ID]),
        "Часовой пояс": str(datetime.now(TIMEZONE))
    }

    for name, status in checks.items():
        logger.info(f"{name}: {'OK' if status else 'ERROR'}")
        if not status and name != "Файл запрещенных слов":
            raise RuntimeError(f"Проверка не пройдена: {name}")
            # --- Обработчики команд ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start с улучшенной обработкой ошибок и логированием."""
    if not update.message and not update.callback_query:
        logger.error("Пустое update.message и update.callback_query в start_command")
        return ConversationHandler.END

    user = update.effective_user
    logger.info(
        f"Команда /start от: @{user.username if user else 'N/A'} "
        f"(ID: {user.id if user else 'N/A'})"
    )

    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton(f"{v['icon']} {v['name']}", callback_data=k)
        for k, v in REQUEST_TYPES.items()]
    ]

    try:
        if update.callback_query:
            await safe_edit_message_text(
                update.callback_query,
                f"Добро пожаловать в {CHANNEL_NAME}!\nВыберите тип заявки:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                f"Добро пожаловать в {CHANNEL_NAME}!\nВыберите тип заявки:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        return TYPE_SELECTION

    except Exception as e:
        logger.error(f"Ошибка в start_command для пользователя {user.id if user else 'N/A'}: {e}", exc_info=True)
        if update.effective_chat:
            await safe_send_message(context.bot, update.effective_chat.id, "?? Произошла ошибка. Попробуйте позже.")
        return ConversationHandler.END

async def handle_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор типа заявки пользователем."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
        
    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    context.user_data["type"] = query.data
    request_type = query.data

    keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

    if request_type == "congrat":
        await safe_edit_message_text(query,
            "Как вас зовут? (кто поздравляет, например: Внук Виталий)",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return SENDER_NAME_INPUT

    elif request_type == "announcement":
        keyboard = [[InlineKeyboardButton(v, callback_data=k)] for k, v in ANNOUNCE_SUBTYPES.items()]
        keyboard.extend(keyboard_nav)
        await safe_edit_message_text(query,
            "Выберите тип объявления:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return ANNOUNCE_SUBTYPE_SELECTION

    elif request_type == "news":
        await safe_edit_message_text(query,
            f"Введите вашу новость (до {MAX_TEXT_LENGTH} символов):",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return ANNOUNCE_TEXT_INPUT
        
    else:
        await safe_edit_message_text(query, "❌ Неизвестный тип заявки.")
        return ConversationHandler.END

async def get_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает имя отправителя для поздравления."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return SENDER_NAME_INPUT

    sender_name = update.message.text.strip()
    if not validate_name(sender_name):
        await safe_reply_text(update,
            f"Пожалуйста, введите корректное имя (только буквы, пробелы и дефисы, от 2 до {MAX_NAME_LENGTH} символов):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return SENDER_NAME_INPUT

    context.user_data["from_name"] = sender_name
    await safe_reply_text(update,
        "Кого поздравляете? Например: бабушку Вику",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
    return RECIPIENT_NAME_INPUT

async def get_recipient_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает имя получателя для поздравления."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
        return RECIPIENT_NAME_INPUT

    recipient_name = update.message.text.strip()
    if not validate_name(recipient_name):
        await safe_reply_text(update,
            f"Пожалуйста, введите корректное имя (только буквы, пробелы и дефисы, от 2 до {MAX_NAME_LENGTH} символов):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
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

async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает пользователя в начало."""
    context.user_data.clear()
    return await start_command(update, context)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /cancel."""
    context.user_data.clear()
    await safe_reply_text(update,
        "Текущее действие отменено. Начните заново с /start",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
    return ConversationHandler.END
async def handle_congrat_holiday_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор праздника для поздравления."""
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

    holiday = query.data.replace("holiday_", "")
    if holiday not in HOLIDAYS:
        await safe_edit_message_text(query,
            "❌ Неизвестный праздник. Пожалуйста, выберите из списка.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Вернуться к выбору", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]
            ]))
        return ConversationHandler.END

    holiday_date_str = HOLIDAYS[holiday]
    if not is_holiday_active(holiday_date_str):
        start_date = (datetime.strptime(f"{datetime.now().year}-{holiday_date_str}", "%Y-%m-%d") - timedelta(days=5)).strftime("%d.%m")
        end_date = (datetime.strptime(f"{datetime.now().year}-{holiday_date_str}", "%Y-%m-%d") + timedelta(days=5)).strftime("%d.%m")
        
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

async def process_custom_congrat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод пользовательского поздравления."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Ошибка ответа на callback: {e}")

    context.user_data["congrat_type"] = "custom"
    await safe_edit_message_text(query,
        f"Введите текст поздравления (до {MAX_TEXT_LENGTH} символов):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]))
    return CUSTOM_CONGRAT_MESSAGE_INPUT

async def process_congrat_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает текст поздравления."""
    if not update.message or not update.message.text or not update.message.text.strip():
        await safe_reply_text(update,
            "Ошибка: пустое сообщение. Пожалуйста, введите текст.",
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
            ]))
        return WAIT_CENSOR_APPROVAL

    return await complete_request(update, context, formatted_text)

async def back_to_holiday_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
async def handle_announce_subtype_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор подтипа объявления."""
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("?? Вернуться в начало", callback_data="back_to_start")]]))
        return ConversationHandler.END

    context.user_data["subtype"] = subtype_key
    subtype_name = ANNOUNCE_SUBTYPES[subtype_key]
    context.user_data["subtype_emoji"] = subtype_name.split()[0]

    await safe_edit_message_text(query,
        f"Вы выбрали: {subtype_name}\nВведите текст объявления (до {MAX_TEXT_LENGTH} символов):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("?? Вернуться в начало", callback_data="back_to_start")]]))
    return ANNOUNCE_TEXT_INPUT

async def process_announce_news_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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

async def handle_censor_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
                [InlineKeyboardButton("🔙 Вернуться к вводу текста", callback_data="back_to_custom_message")],
                [InlineKeyboardButton("🔙 Вернуться к выбору праздника", callback_data="back_to_holiday_choice")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]
            await safe_edit_message_text(query,
                "Текст принят с изменениями фильтра. 📅 Укажите дату публикации (ДД-ММ-ГГГГ или 'сегодня'):",
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

async def process_congrat_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод даты публикации для поздравления."""
    if update.callback_query:
        callback_data = update.callback_query.data
        try:
            await update.callback_query.answer()
        except TelegramError as e:
            logger.warning(f"Ошибка при ответе на callback: {e}")

        if callback_data == "back_to_start":
            return await start_command(update, context)
        elif callback_data == "back_to_holiday_choice":
            return await back_to_holiday_choice(update, context)
        elif callback_data == "back_to_custom_message":
            return await back_to_custom_message(update, context)
        return CONGRAT_DATE_INPUT

    if not update.message or not update.message.text:
        await safe_reply_text(update,
            "❌ Не получен текст. Введите дату в формате ДД-ММ-ГГГГ или 'сегодня'.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Вернуться к тексту", callback_data="back_to_custom_message")],
                [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
            ]))
        return CONGRAT_DATE_INPUT

    date_str = update.message.text.strip().lower().replace('.', '-')
    keyboard_nav = [
        [InlineKeyboardButton("🔙 Вернуться к тексту", callback_data="back_to_custom_message")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]

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

async def back_to_custom_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
async def notify_admin_new_application(bot: Bot, app_id: int, app_details: dict) -> None:
    """Уведомляет администратора о новой заявке."""
    if not ADMIN_CHAT_ID:
        return

    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', app_details['type'])
    text = (
        f"📨 Новая заявка #{app_id}\n"
        f"• Тип: {app_type}\n"
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

async def handle_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
async def complete_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None) -> int:
    """Завершает создание заявки и отправляет на модерацию."""
    user = update.effective_user
    user_data = context.user_data
    
    if not text:
        text = user_data.get('censored_text', user_data.get('text'))
    
    app_data = {
        'user_id': user.id,
        'username': user.username,
        'type': user_data['type'],
        'text': text,
        'status': 'pending'
    }
    
    # Добавляем дополнительные поля в зависимости от типа заявки
    if user_data['type'] == 'congrat':
        app_data.update({
            'from_name': user_data['from_name'],
            'to_name': user_data['to_name'],
            'congrat_type': user_data.get('congrat_type'),
            'publish_date': user_data.get('publish_date')
        })
    elif user_data['type'] == 'announcement':
        app_data['subtype'] = user_data.get('subtype')
    
    # Сохраняем заявку в БД
    app_id = add_application(app_data)
    
    if app_id:
        await safe_reply_text(update,
            "❌ Произошла ошибка при сохранении заявки. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
        
        # Уведомляем администратора
        await notify_admin_new_application(context.bot, app_id, app_data)
    else:
        await safe_reply_text(update,
            "❌ Произошла ошибка при сохранении заявки. Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]])
        )
    
    context.user_data.clear()
    return ConversationHandler.END
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
            return count < 5  # Не более 5 заявок в час
    except sqlite3.Error as e:
        logger.error(f"Ошибка проверки лимита заявок: {e}")
        return True

async def check_spam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет пользователя на спам."""
    user = update.effective_user
    if not can_submit_request(user.id):
        await safe_reply_text(update,
            "?? Вы отправили слишком много заявок за последнее время.\n"
            "Пожалуйста, попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("?? В начало", callback_data="back_to_start")]])
        )
        return False
    return True
def setup_handlers(application: Application) -> None:
    """Настройка всех обработчиков команд и сообщений."""
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            TYPE_SELECTION: [CallbackQueryHandler(handle_type_selection)],
            SENDER_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sender_name)],
            RECIPIENT_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_recipient_name)],
            CONGRAT_HOLIDAY_CHOICE: [CallbackQueryHandler(handle_congrat_holiday_choice)],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_congrat_text)],
            CONGRAT_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_congrat_date),
                CallbackQueryHandler(process_congrat_date)
            ],
            ANNOUNCE_SUBTYPE_SELECTION: [CallbackQueryHandler(handle_announce_subtype_selection)],
            ANNOUNCE_TEXT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_announce_news_text)],
            WAIT_CENSOR_APPROVAL: [CallbackQueryHandler(handle_censor_choice)]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        per_message=True  # Этот параметр устраняет предупреждение
    )
    
    # Добавляем основной обработчик диалога
    application.add_handler(conv_handler)
    
    # Добавляем обработчик решений администратора
    application.add_handler(CallbackQueryHandler(
        handle_admin_decision,
        pattern=r"^(approve|reject|view)_\d+$"
    ))
    
    # Добавляем глобальную проверку на спам
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        check_spam
    ))
    
async def show_date_keyboard(update: Update, text: str) -> None:
    """Показывает клавиатуру с вариантами дат."""
    keyboard = [
        [
            InlineKeyboardButton("Сегодня", callback_data="publish_today"),
            InlineKeyboardButton("Завтра", callback_data="publish_tomorrow")
        ],
        [InlineKeyboardButton("Указать другую дату", callback_data="custom_date")],
        [InlineKeyboardButton("?? Назад", callback_data="back_to_text")]
    ]
    
    if update.callback_query:
        await safe_edit_message_text(
            update.callback_query,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await safe_reply_text(
            update,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard))
async def post_init(application: Application) -> None:
    """Действия после инициализации бота."""
    await send_bot_status(application.bot, "Бот запущен")
    BOT_STATE['running'] = True
    BOT_STATE['start_time'] = datetime.now(TIMEZONE)

def main() -> None:
    """Запуск бота."""
    init_db()
    check_environment()

    application = Application.builder().token(TOKEN).post_init(post_init).build()
    setup_handlers(application)

    # Планировщик для проверки заявок каждые 5 минут
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        check_pending_applications,
        'interval',
        minutes=5,
        args=[application]
    )
    scheduler.start()

    atexit.register(cleanup)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
