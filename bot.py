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
import sqlite3

# Сторонние библиотеки
import pytz
from dotenv import load_dotenv
import aiofiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Библиотека python-telegram-bot v21+
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# --- Глобальные переменные и константы ---
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

# Глобальная переменная для отслеживания состояния бота
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
    level=logging.DEBUG,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Кэширование запрещенных слов ---
_cached_bad_words = []

async def load_bad_words_cached() -> list:
    """Асинхронно загружает список запрещенных слов и кэширует его."""
    global _cached_bad_words
    if not _cached_bad_words:
        try:
            async with aiofiles.open(BAD_WORDS_FILE, 'r', encoding='utf-8') as f:
                content = await f.read()
                _cached_bad_words = [word.strip() for line in content.splitlines() for word in line.split(',') if word.strip()]
        except FileNotFoundError:
            logger.warning("Файл с запрещенными словами не найден. Используются значения по умолчанию.")
            _cached_bad_words = DEFAULT_BAD_WORDS
        except Exception as e:
            logger.error(f"Ошибка загрузки запрещенных слов: {e}", exc_info=True)
            _cached_bad_words = DEFAULT_BAD_WORDS
    return _cached_bad_words

async def cleanup():
    """Асинхронная функция очистки при завершении работы"""
    if not BOT_STATE.get('running'):
        return

    logger.info("Завершение работы бота...")
    BOT_STATE['running'] = False

    try:
        # Отправка статуса администратору
        if ADMIN_CHAT_ID:
            bot = Bot(token=TOKEN)
            try:
                await send_bot_status(bot, "Бот завершает работу")
            except Exception as e:
                logger.error(f"Ошибка отправки статуса завершения: {e}")
            finally:
                await bot.close()
    except Exception as e:
        logger.error(f"Ошибка при создании бота для отправки статуса: {e}")

    # Остановка планировщика
    if 'scheduler' in globals():
        try:
            scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")
        except Exception as e:
            logger.error(f"Ошибка остановки планировщика: {e}")

    # Закрытие сокета
    if 'lock_socket' in globals():
        try:
            lock_socket.close()
            logger.info("Сокет закрыт")
        except Exception as e:
            logger.error(f"Ошибка закрытия сокета: {e}")

def handle_signal(signum, frame):
    """Обработчик сигналов завершения работы"""
    logger.info(f"Получен сигнал {signum}, инициируем завершение работы...")
    BOT_STATE['running'] = False
    
    # Используем существующий event loop для корректного завершения
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup())
    # Не вызываем sys.exit(0) здесь, позволяем asyncio завершить задачи

def is_working_hours() -> bool:
    """Проверяет, находится ли текущее время в рабочих часах (00:00-23:59 по Москве)"""
    now = datetime.now(TIMEZONE)

    # Проверка рабочего времени
    if not (WORKING_HOURS[0] <= now.hour < WORKING_HOURS[1]):
        return False

    # Проверка выходных, если WORK_ON_WEEKENDS = False
    if not WORK_ON_WEEKENDS and now.weekday() >= 5:  # 5 и 6 - суббота и воскресенье
        return False

    return True

# --- Функции работы с БД ---
_db_connection = None

def get_db_connection() -> sqlite3.Connection:
    """Устанавливает соединение с базой данных (синглтон)."""
    global _db_connection
    if _db_connection is None:
        _db_connection = sqlite3.connect(DB_FILE, check_same_thread=False)
        _db_connection.row_factory = sqlite3.Row
    return _db_connection

def init_db():
    """Инициализирует таблицы базы данных с индексами."""
    try:
        conn = get_db_connection()
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
        conn.commit()

    except sqlite3.Error as e:
        logger.error(f"Ошибка инициализации БД: {e}", exc_info=True)
        raise

def add_application(data: Dict[str, Any]) -> Optional[int]:
    """Добавляет новую заявку в базу данных."""
    try:
        conn = get_db_connection()
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
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
        return cur.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Ошибка получения заявки #{app_id}: {e}", exc_info=True)
        return None

def get_approved_unpublished_applications() -> list:
    """Получает одобренные, но не опубликованные заявки."""
    try:
        conn = get_db_connection()
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
        conn = get_db_connection()
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка обновления статуса заявки #{app_id}: {e}", exc_info=True)
        return False

def mark_application_as_published(app_id: int) -> bool:
    """Помечает заявку как опубликованную."""
    try:
        conn = get_db_connection()
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
    except ValueError as e: # Более специфичное исключение
        logger.error(f"Ошибка парсинга даты праздника: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка проверки праздника: {e}", exc_info=True)
        return False

async def censor_text(text: str) -> Tuple[str, bool]:
    """Цензурирует текст и возвращает результат."""
    bad_words = await load_bad_words_cached() # Используем кэшированную версию
    censored = text
    has_bad = False

    for word in bad_words:
        try:
            # Используем re.compile для оптимизации, если слово используется многократно
            # Но для каждого слова отдельно, это может быть излишним. Оставляем как было.
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
        # Можно добавить логику повторных попыток здесь, если это необходимо
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

async def send_bot_status(bot: Bot, status: str) -> bool:
    """Улучшенная версия с обработкой ошибок"""
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

        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            disable_notification=True
        )
        return True
    except TelegramError as e:
        logger.warning(f"Превышен лимит сообщений или другая ошибка Telegram: {e}")
        return False
    except Exception as e:
        logger.error(f"Ошибка отправки статуса: {str(e)}")
        return False

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
    except TelegramError as e: # Более специфичное исключение
        logger.error(f"Ошибка публикации заявки #{app_id} в Telegram: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка публикации заявки #{app_id}: {str(e)}")
        return False

async def check_pending_applications(context: CallbackContext) -> None:
    """Проверяет и публикует одобренные, но еще не опубликованные заявки,
    особенно те, которые должны быть опубликованы по расписанию."""
    applications = get_approved_unpublished_applications()
    for app in applications:
        # Обрабатываем только "свои поздравления" с датой публикации, которая наступила
        if app['type'] == 'congrat' and app['congrat_type'] == 'custom':
            if app['publish_date']:
                publish_date_obj = datetime.strptime(app['publish_date'], "%Y-%m-%d").date()
                today = datetime.now().date()
                if publish_date_obj <= today: # Если дата публикации сегодня или раньше
                    logger.info(f"Плановая публикация пользовательского поздравления #{app['id']} (дата подошла).")
                    await publish_to_channel(app['id'], context.bot)
                    await asyncio.sleep(1)
                # else: дата в будущем, пока ничего не делаем
            else:
                # Этот случай должен быть обработан немедленной публикацией при одобрении.
                # Если заявка все еще здесь без даты, это запасной вариант.
                logger.warning(f"Пользовательское поздравление #{app['id']} без даты публикации в check_pending_applications. Публикуем немедленно.")
                await publish_to_channel(app['id'], context.bot)
                await asyncio.sleep(1)
        # Для других типов (новости, объявления, стандартные поздравления)
        # они должны были быть опубликованы немедленно при одобрении.
        # Если они все еще здесь в статусе 'approved' и 'unpublished',
        # это означает, что немедленная публикация не удалась. Публикуем их сейчас как запасной вариант.
        elif app['type'] in ['news', 'announcement'] or (app['type'] == 'congrat' and app['congrat_type'] != 'custom'):
            logger.warning(f"Заявка #{app['id']} типа '{app['type']}' не была опубликована немедленно после одобрения. Публикуем сейчас.")
            await publish_to_channel(app['id'], context.bot)
            await asyncio.sleep(1)

async def check_shutdown_time(context: CallbackContext) -> None:
    """Проверяет, не вышло ли время работы бота (23:00) и корректно останавливает его"""
    if not is_working_hours():
        logger.info("Рабочее время закончилось. Останавливаем бота.")
        await send_bot_status(context.bot, "Остановка (рабочее время закончилось)")
        
        try:
            # Корректное завершение приложения python-telegram-bot
            await context.application.shutdown()
            # sys.exit(0) или os._exit(0) не нужны, т.к. shutdown() позаботится о завершении
        except Exception as e:
            logger.error(f"Ошибка при остановке бота: {e}")
            # Если shutdown() не сработал, можно принудительно выйти, но это крайний случай
            sys.exit(1)

def check_environment():
    """Проверяет наличие всех необходимых файлов и переменных"""
    required_files = [DB_FILE, BAD_WORDS_FILE]
    missing_files = [f for f in required_files if not os.path.exists(f)]

    if missing_files:
        logger.error(f"Отсутствуют файлы: {missing_files}")
        if ADMIN_CHAT_ID:
            # Создаем новый объект Bot для отправки сообщения, так как application еще может быть не запущен
            temp_bot = Bot(token=TOKEN)
            asyncio.run(safe_send_message(
                temp_bot,
                ADMIN_CHAT_ID,
                f"❌ Отсутствуют файлы: {', '.join(missing_files)}"
            ))
            asyncio.run(temp_bot.close()) # Закрываем бота после использования
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
async def start_command(update: Update, context: CallbackContext) -> int:
    """
    Обработчик команды /start с очисткой состояния.
    Теперь всегда сразу показывает выбор типа заявки, независимо от deep link.
    """
    user = update.effective_user
    logger.info(f"Пользователь @{user.username if user else 'N/A'} запустил бота")
    context.user_data.clear() # Всегда очищаем состояние при старте, чтобы избежать зависаний

    # Эта часть кода теперь выполняется всегда, независимо от наличия deep link
    text = "Выберите тип заявки:"
    keyboard = []
    for key, info in REQUEST_TYPES.items():
        keyboard.append([InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)])
    keyboard += BACK_BUTTON  # Используем константу для кнопки возврата

    # Используем safe_reply_text, чтобы корректно обработать как message, так и callback_query
    await safe_reply_text(update, text, reply_markup=InlineKeyboardMarkup(keyboard))

    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор типа заявки пользователем."""
    query = update.callback_query
    if not query or not query.data:
        logger.warning("Пустой callback_query или data в handle_type_selection.")
        return ConversationHandler.END

    logger.info(f"Пользователь @{query.from_user.username if query.from_user else 'N/A'} выбрал тип заявки: {query.data}")
    logger.debug(f"Текущее user_data в handle_type_selection: {context.user_data}")

    try:
        await query.answer() # Всегда отвечаем на callback query
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
        keyboard = []
        for key, name in ANNOUNCE_SUBTYPES.items():
            keyboard.append([InlineKeyboardButton(name, callback_data=key)])
        keyboard += keyboard_nav
        await safe_edit_message_text(query,
            "Выберите подтип объявления:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return ANNOUNCE_SUBTYPE_SELECTION
    elif request_type == "news":
        await safe_edit_message_text(query,
            "Напишите текст новости (до 300 символов):",
            reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return ANNOUNCE_TEXT_INPUT
    else:
        await safe_edit_message_text(query, "❌ Неизвестный тип заявки. Возвращаемся в начало.")
        return await start_command(update, context)

async def handle_sender_name_input(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод имени отправителя для поздравления."""
    user_message = update.message.text.strip() if update.message else ""
    if not validate_name(user_message):
        await safe_reply_text(update, "Некорректное имя. Имя должно содержать от 2 до 50 символов (буквы, пробелы, дефисы), не начинаться и не заканчиваться дефисом, и не содержать двойных дефисов. Попробуйте еще раз.")
        return SENDER_NAME_INPUT

    context.user_data["from_name"] = user_message
    keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
    await safe_reply_text(update, "Кого поздравляем? (например: Бабушку Зину)", reply_markup=InlineKeyboardMarkup(keyboard_nav))
    return RECIPIENT_NAME_INPUT

async def handle_recipient_name_input(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод имени получателя для поздравления."""
    user_message = update.message.text.strip() if update.message else ""
    if not validate_name(user_message):
        await safe_reply_text(update, "Некорректное имя. Имя должно содержать от 2 до 50 символов (буквы, пробелы, дефисы), не начинаться и не заканчиваться дефисом, и не содержать двойных дефисов. Попробуйте еще раз.")
        return RECIPIENT_NAME_INPUT

    context.user_data["to_name"] = user_message

    keyboard = []
    for holiday_name, holiday_date in HOLIDAYS.items():
        if is_holiday_active(holiday_date):
            keyboard.append([InlineKeyboardButton(holiday_name, callback_data=f"holiday_{holiday_date}")])
    keyboard.append([InlineKeyboardButton("Написать свое поздравление", callback_data="custom_congrat")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")])

    await safe_reply_text(update, "Выберите праздник или напишите свое поздравление:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_HOLIDAY_CHOICE

async def handle_congrat_holiday_choice(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор праздника или опции 'свое поздравление'."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    if query.data.startswith("holiday_"):
        holiday_date = query.data.replace("holiday_", "")
        holiday_name = next((name for name, date in HOLIDAYS.items() if date == holiday_date), "")
        context.user_data["congrat_type"] = "holiday"
        context.user_data["publish_date"] = holiday_date # Дата публикации - день праздника
        context.user_data["text"] = HOLIDAY_TEMPLATES.get(holiday_name, "")
        
        # Переходим к подтверждению
        return await confirm_application(update, context)

    elif query.data == "custom_congrat":
        context.user_data["congrat_type"] = "custom"
        keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
        await safe_edit_message_text(query, "Напишите текст вашего поздравления (до 500 символов):", reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    else:
        # Это не должно произойти при корректной работе, но на всякий случай
        keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
        await safe_edit_message_text(query, "Неизвестный выбор. Пожалуйста, попробуйте еще раз.", reply_markup=InlineKeyboardMarkup(keyboard_nav))
        return CONGRAT_HOLIDAY_CHOICE

async def handle_custom_congrat_message_input(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод текста собственного поздравления."""
    user_message = update.message.text.strip() if update.message else ""
    if not user_message or len(user_message) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст поздравления не может быть пустым и должен быть не более {MAX_CONGRAT_TEXT_LENGTH} символов. Попробуйте еще раз.")
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    context.user_data["text"] = user_message
    keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
    await safe_reply_text(update, "На какую дату опубликовать поздравление? (в формате ДД.ММ.ГГГГ, например 01.01.2025)", reply_markup=InlineKeyboardMarkup(keyboard_nav))
    return CONGRAT_DATE_INPUT

async def handle_congrat_date_input(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод даты публикации поздравления."""
    user_message = update.message.text.strip() if update.message else ""
    try:
        publish_date = datetime.strptime(user_message, "%d.%m.%Y").date()
        if publish_date < datetime.now().date():
            await safe_reply_text(update, "Дата публикации не может быть в прошлом. Пожалуйста, введите корректную дату.")
            return CONGRAT_DATE_INPUT
        context.user_data["publish_date"] = publish_date.strftime("%Y-%m-%d")
    except ValueError:
        await safe_reply_text(update, "Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.ГГГГ (например 01.01.2025).")
        return CONGRAT_DATE_INPUT

    return await confirm_application(update, context)

async def handle_announce_subtype_selection(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор подтипа объявления."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    context.user_data["subtype"] = query.data
    keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
    await safe_edit_message_text(query, "Напишите текст объявления (до 4000 символов):", reply_markup=InlineKeyboardMarkup(keyboard_nav))
    return ANNOUNCE_TEXT_INPUT

async def handle_announce_text_input(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ввод текста объявления или новости."""
    user_message = update.message.text.strip() if update.message else ""
    if not user_message or len(user_message) > MAX_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст не может быть пустым и должен быть не более {MAX_TEXT_LENGTH} символов. Попробуйте еще раз.")
        return ANNOUNCE_TEXT_INPUT

    context.user_data["text"] = user_message

    return await confirm_application(update, context)

async def confirm_application(update: Update, context: CallbackContext) -> int:
    """Показывает пользователю сводку заявки и просит подтверждения."""
    app_data = context.user_data
    app_type_info = REQUEST_TYPES.get(app_data.get("type"), {"name": "Неизвестно", "icon": ""})
    summary_text = f"{app_type_info['icon']} Тип: {app_type_info['name']}\n"

    if app_data.get("type") == "congrat":
        summary_text += f"От кого: {app_data.get('from_name', 'Не указано')}\n"
        summary_text += f"Кому: {app_data.get('to_name', 'Не указано')}\n"
        summary_text += f"Дата публикации: {app_data.get('publish_date', 'Не указано')}\n"
        summary_text += f"Текст: {app_data.get('text', 'Не указано')}\n"
    elif app_data.get("type") == "announcement":
        subtype_name = ANNOUNCE_SUBTYPES.get(app_data.get("subtype"), "Неизвестно")
        summary_text += f"Подтип: {subtype_name}\n"
        summary_text += f"Текст: {app_data.get('text', 'Не указано')}\n"
    elif app_data.get("type") == "news":
        summary_text += f"Текст: {app_data.get('text', 'Не указано')}\n"

    censored_text, has_bad_words = await censor_text(app_data.get('text', ''))
    if has_bad_words:
        summary_text += "\n⚠️ Внимание: В вашем тексте обнаружены потенциально нецензурные слова или контактная информация. Они будут заменены или скрыты при публикации.\n"
        summary_text += f"Предварительный просмотр цензурированного текста:\n{censored_text}\n"
        # Сохраняем цензурированный текст для дальнейшего использования
        context.user_data["final_text"] = censored_text
    else:
        context.user_data["final_text"] = app_data.get('text', '')

    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить и отправить на модерацию", callback_data="submit_application")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_application")],
        [InlineKeyboardButton("❌ Отменить", callback_data="cancel_application")]
    ]
    keyboard += BACK_BUTTON

    await safe_reply_text(update, f"Пожалуйста, проверьте вашу заявку:\n\n{summary_text}\n\nВсе верно?", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_CENSOR_APPROVAL

async def handle_confirmation(update: Update, context: CallbackContext) -> int:
    """Обрабатывает подтверждение, редактирование или отмену заявки."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    if query.data == "submit_application":
        app_data = context.user_data
        user = update.effective_user
        
        # Используем final_text, который уже прошел цензуру
        data_to_save = {
            "user_id": user.id,
            "username": user.username,
            "type": app_data["type"],
            "subtype": app_data.get("subtype"),
            "from_name": app_data.get("from_name"),
            "to_name": app_data.get("to_name"),
            "text": app_data["final_text"], # Используем цензурированный текст
            "publish_date": app_data.get("publish_date"),
            "congrat_type": app_data.get("congrat_type")
        }
        
        app_id = add_application(data_to_save)

        if app_id:
            await safe_edit_message_text(query, "Ваша заявка принята и отправлена на модерацию. Спасибо!", reply_markup=None)
            logger.info(f"Заявка #{app_id} от пользователя {user.id} ({user.username}) добавлена в БД.")
            
            # Уведомление администратора о новой заявке
            if ADMIN_CHAT_ID:
                admin_notification_text = (
                    f"🔔 Новая заявка #{app_id} от @{user.username or user.id}:\n"
                    f"Тип: {REQUEST_TYPES.get(app_data['type'], {}).get('name', 'Неизвестно')}\n"
                    f"Текст: {app_data['final_text'][:200]}...\n"
                    f"[Просмотреть и одобрить](https://t.me/{context.bot.get_me().username}?start=moderate_{app_id})" # Пример ссылки для модерации
                )
                await safe_send_message(context.bot, ADMIN_CHAT_ID, admin_notification_text)

            context.user_data.clear() # Очищаем данные пользователя после успешной отправки
            return ConversationHandler.END
        else:
            await safe_edit_message_text(query, "Произошла ошибка при сохранении заявки. Пожалуйста, попробуйте еще раз.", reply_markup=None)
            return ConversationHandler.END

    elif query.data == "edit_application":
        # Возвращаем пользователя к началу ввода, сохраняя тип заявки
        app_type = context.user_data.get("type")
        context.user_data.clear() # Очищаем все, кроме типа
        context.user_data["type"] = app_type

        keyboard_nav = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]

        if app_type == "congrat":
            await safe_edit_message_text(query,
                "Как вас зовут? (кто поздравляет, например: Внук Виталий)",
                reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return SENDER_NAME_INPUT
        elif app_type == "announcement":
            keyboard = []
            for key, name in ANNOUNCE_SUBTYPES.items():
                keyboard.append([InlineKeyboardButton(name, callback_data=key)])
            keyboard += keyboard_nav
            await safe_edit_message_text(query,
                "Выберите подтип объявления:",
                reply_markup=InlineKeyboardMarkup(keyboard))
            return ANNOUNCE_SUBTYPE_SELECTION
        elif app_type == "news":
            await safe_edit_message_text(query,
                "Напишите текст новости (до 300 символов):",
                reply_markup=InlineKeyboardMarkup(keyboard_nav))
            return ANNOUNCE_TEXT_INPUT
        else:
            # Если тип заявки потерян или некорректен, возвращаем в начало
            return await start_command(update, context)

    elif query.data == "cancel_application":
        await safe_edit_message_text(query, "Создание заявки отменено.", reply_markup=None)
        context.user_data.clear()
        return ConversationHandler.END

async def back_to_start(update: Update, context: CallbackContext) -> int:
    """Полный сброс состояния и возврат в главное меню."""
    user = update.effective_user
    logger.info(f"Пользователь @{user.username if user else 'N/A'} инициировал возврат в начало.")
    logger.debug(f"Состояние user_data до очистки: {context.user_data}")
    context.user_data.clear()
    logger.debug(f"Состояние user_data после очистки: {context.user_data}")

    if update.callback_query:
        try:
            await update.callback_query.answer()
            logger.debug(f"Ответ на callback_query {update.callback_query.id} в back_to_start.")
        except TelegramError as e:
            logger.warning(f"Ошибка при ответе на callback в back_to_start: {e}")

    result = await start_command(update, context)
    logger.info(f"back_to_start завершен, переход в состояние: {result}")
    return result

async def moderate_command(update: Update, context: CallbackContext) -> None:
    """Обработчик команды /moderate для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await safe_reply_text(update, "У вас нет прав для использования этой команды.")
        return

    args = context.args
    if not args or len(args) != 1:
        await safe_reply_text(update, "Использование: /moderate <app_id>")

    except ValueError:
        await safe_reply_text(update, "ID заявки должен быть числом.")
        return

    app_details = get_application_details(app_id)

    if not app_details:
        await safe_reply_text(update, f"Заявка с ID {app_id} не найдена.")


    try:
        app_id = int(args[0])
    except ValueError:
        await safe_reply_text(update, "ID заявки должен быть числом.")
        return

    app_details = get_application_details(app_id)

    if not app_details:
        await safe_reply_text(update, f"Заявка с ID {app_id} не найдена.")
        return

    summary_text = f"Заявка #{app_id}:\n"
    summary_text += f"От пользователя: @{app_details['username'] or app_details['user_id']}\n"
    summary_text += f"Тип: {REQUEST_TYPES.get(app_details['type'], {}).get('name', 'Неизвестно')}\n"
    if app_details['subtype']:
        summary_text += f"Подтип: {ANNOUNCE_SUBTYPES.get(app_details['subtype'], 'Неизвестно')}\n"
    if app_details['from_name']:
        summary_text += f"От кого: {app_details['from_name']}\n"
    if app_details['to_name']:
        summary_text += f"Кому: {app_details['to_name']}\n"
    if app_details['publish_date']:
        summary_text += f"Дата публикации: {app_details['publish_date']}\n"
    summary_text += f"Текст: {app_details['text']}\n"
    summary_text += f"Статус: {app_details['status']}\n"
    summary_text += f"Создана: {app_details['created_at']}\n"

    keyboard = [
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")]
    ]

    await safe_reply_text(update, summary_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_moderation_callback(update: Update, context: CallbackContext) -> None:
    """Обрабатывает коллбэки от кнопок модерации."""
    query = update.callback_query
    if not query or not query.data:
        return

    try:
        await query.answer()
    except TelegramError as e:
        logger.warning(f"Error answering callback query {query.id}: {e}")

    if not query.data.startswith(("approve_", "reject_")):
        return

    action, app_id_str = query.data.split("_")
    app_id = int(app_id_str)

    app_details = get_application_details(app_id)
    if not app_details:
        await safe_edit_message_text(query, f"Заявка #{app_id} не найдена или уже удалена.")
        return

    if app_details['status'] != 'pending':
        await safe_edit_message_text(query, f"Заявка #{app_id} уже имеет статус '{app_details['status']}'.")
        return

    if action == "approve":
        if update_application_status(app_id, 'approved'):
            await safe_edit_message_text(query, f"Заявка #{app_id} одобрена.")
            logger.info(f"Заявка #{app_id} одобрена администратором {query.from_user.username or query.from_user.id}")
            
            # Если это не пользовательское поздравление с датой в будущем, публикуем немедленно
            if not (app_details['type'] == 'congrat' and app_details['congrat_type'] == 'custom' and app_details['publish_date'] and datetime.strptime(app_details['publish_date'], "%Y-%m-%d").date() > datetime.now().date()):
                await publish_to_channel(app_id, context.bot)
            else:
                await safe_send_message(context.bot, app_details['user_id'], f"Ваша заявка #{app_id} одобрена и будет опубликована {app_details['publish_date']}.")

        else:
            await safe_edit_message_text(query, f"Ошибка при одобрении заявки #{app_id}.")
    elif action == "reject":
        if update_application_status(app_id, 'rejected'):
            await safe_edit_message_text(query, f"Заявка #{app_id} отклонена.")
            logger.info(f"Заявка #{app_id} отклонена администратором {query.from_user.username or query.from_user.id}")
            await safe_send_message(context.bot, app_details['user_id'], f"Ваша заявка #{app_id} была отклонена модератором. Пожалуйста, проверьте текст на наличие запрещенных слов или контактной информации.")
        else:
            await safe_edit_message_text(query, f"Ошибка при отклонении заявки #{app_id}.")

async def error_handler(update: object, context: CallbackContext) -> None:
    """Логирует ошибки, вызванные обработчиками Update."""
    logger.error("Исключение при обработке обновления:", exc_info=context.error)
    try:
        if ADMIN_CHAT_ID:
            error_message = (
                f"❌ Произошла ошибка в боте!\n"
                f"Update: {update}\n"
                f"Ошибка: {context.error}\n"
                f"Traceback: {traceback.format_exc()}"
            )
            # Отправляем сообщение об ошибке администратору, обрезая длинный traceback
            await safe_send_message(context.bot, ADMIN_CHAT_ID, error_message[:4000])
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения об ошибке администратору: {e}")

# --- Функции для отслеживания состояния бота ---
def get_uptime() -> str:
    """Возвращает время работы бота."""
    if BOT_STATE['start_time']:
        uptime = datetime.now() - BOT_STATE['start_time']
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}д {hours}ч {minutes}м {seconds}с"
    return "Неизвестно"

async def update_last_activity(update: Update, context: CallbackContext) -> None:
    """Обновляет время последней активности бота."""
    BOT_STATE['last_activity'] = datetime.now()

async def send_periodic_status(context: CallbackContext) -> None:
    """Отправляет периодический статус бота администратору."""
    await send_bot_status(context.bot, "Работает")

# --- Основная функция запуска бота ---
async def main():
    """Запускает бота."""
    # Проверка окружения перед запуском
    if not check_environment():
        logger.critical("Не пройдены проверки окружения. Бот не может быть запущен.")
        sys.exit(1)

    check_bot_health()
    init_db()
    await load_bad_words_cached() # Загружаем и кэшируем слова при старте

    # Инициализация Application
    application = Application.builder().token(TOKEN).build()

    # Добавляем обработчики
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            TYPE_SELECTION: [
                CallbackQueryHandler(handle_type_selection, pattern='^(congrat|announcement|news)$')
            ],
            SENDER_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sender_name_input)
            ],
            RECIPIENT_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_recipient_name_input)
            ],
            CONGRAT_HOLIDAY_CHOICE: [
                CallbackQueryHandler(handle_congrat_holiday_choice, pattern='^(holiday_.*|custom_congrat)$')
            ],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_congrat_message_input)
            ],
            CONGRAT_DATE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_congrat_date_input)
            ],
            ANNOUNCE_SUBTYPE_SELECTION: [
                CallbackQueryHandler(handle_announce_subtype_selection, pattern='^(ride|offer|lost)$')
            ],
            ANNOUNCE_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_announce_text_input)
            ],
            WAIT_CENSOR_APPROVAL: [
                CallbackQueryHandler(handle_confirmation, pattern='^(submit_application|edit_application|cancel_application)$')
            ],
        },
        fallbacks=[
            CallbackQueryHandler(back_to_start, pattern='^back_to_start$'),
            CommandHandler("cancel", cancel_command) # Обработка команды /cancel
        ]
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("moderate", moderate_command))

    # Обработчик ошибок
    application.add_error_handler(error_handler)

    # Запуск планировщика APScheduler
    global scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_pending_applications, 'interval', minutes=1, args=(application.bot,))
    scheduler.add_job(check_shutdown_time, 'interval', minutes=1, args=(application.bot,))
    scheduler.add_job(send_periodic_status, 'interval', hours=1, args=(application.bot,))
    scheduler.start()

    # Установка состояния бота
    BOT_STATE['running'] = True
    BOT_STATE['start_time'] = datetime.now()
    BOT_STATE['last_activity'] = datetime.now()

    # Регистрация обработчиков сигналов для корректного завершения
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    atexit.register(lambda: asyncio.run(cleanup()))

    logger.info("Бот запущен!")
    await send_bot_status(application.bot, "Запущен")

    # Запуск бота
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}", exc_info=True)
        sys.exit(1)



            CallbackQueryHandler(back_to_start, pattern=\'^back_to_start$\'
            ),
            CommandHandler("cancel", cancel_command)
        ]
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("moderate", moderate_command))

    # Обработчик ошибок
    application.add_error_handler(error_handler)

    # Запуск планировщика APScheduler
    global scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_pending_applications, \'interval\', minutes=1, args=(application.bot,))
    scheduler.add_job(check_shutdown_time, \'interval\', minutes=1, args=(application.bot,))
    scheduler.add_job(send_periodic_status, \'interval\', hours=1, args=(application.bot,))
    scheduler.start()

    # Установка состояния бота
    BOT_STATE[\'running\'] = True
    BOT_STATE[\'start_time\'] = datetime.now()
    BOT_STATE[\'last_activity\'] = datetime.now()

    # Регистрация обработчиков сигналов для корректного завершения
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    atexit.register(lambda: asyncio.run(cleanup()))

    logger.info("Бот запущен!")
    await send_bot_status(application.bot, "Запущен")

    # Запуск бота
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == \'__main__\':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}", exc_info=True)
        sys.exit(1)


