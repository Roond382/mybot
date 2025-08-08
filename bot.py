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
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
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

# ========== КОНСТАНТЫ ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
DB_FILE = 'db.sqlite'
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50
MAX_TEXT_LENGTH = 4000
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
    TYPE_SELECTION, SENDER_NAME_INPUT, RECIPIENT_NAME_INPUT,
    CONGRAT_HOLIDAY_CHOICE, CUSTOM_CONGRAT_MESSAGE_INPUT, CONGRAT_DATE_INPUT,
    ANNOUNCE_SUBTYPE_SELECTION, ANNOUNCE_TEXT_INPUT, PHONE_INPUT,
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
    "🎄 Новый год": "01-01", "🪖 23 Февраля": "02-23", "💐 8 Марта": "03-08",
    "🏅 9 Мая": "05-09", "🇷🇺 12 Июня": "06-12", "🤝 4 Ноября": "11-04"
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
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

application = None

# ========== ГЛОБАЛЬНОЕ СОСТОЯНИЕ БОТА ==========
BOT_STATE = {'running': False, 'start_time': None}

# ========== БАЗА ДАННЫХ ==========
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    try:
        with get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    username TEXT, type TEXT NOT NULL, subtype TEXT, from_name TEXT,
                    to_name TEXT, text TEXT NOT NULL, photo_id TEXT, phone_number TEXT,
                    status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    publish_date DATE, published_at TIMESTAMP, congrat_type TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approved_unpublished ON applications(status, published_at) WHERE status = 'approved' AND published_at IS NULL")
            conn.commit()
            logger.info("База данных успешно инициализирована")
    except sqlite3.Error as e:
        logger.error(f"Ошибка инициализации БД: {e}", exc_info=True)
        raise

def add_application(data: Dict[str, Any]):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO applications (user_id, username, type, subtype, from_name,
                    to_name, text, photo_id, phone_number, publish_date, congrat_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['user_id'], data.get('username'), data['type'], data.get('subtype'),
                data.get('from_name'), data.get('to_name'), data['text'], data.get('photo_id'),
                data.get('phone_number'), data.get('publish_date'), data.get('congrat_type')
            ))
            app_id = cur.lastrowid
            conn.commit()
            return app_id
    except sqlite3.Error as e:
        logger.error(f"Ошибка добавления заявки: {e}\nДанные: {data}", exc_info=True)
        return None

def get_application_details(app_id: int):
    try:
        with get_db_connection() as conn:
            return conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Ошибка получения заявки #{app_id}: {e}", exc_info=True)
        return None

def get_approved_unpublished_applications():
    try:
        with get_db_connection() as conn:
            return conn.execute("SELECT * FROM applications WHERE status = 'approved' AND published_at IS NULL").fetchall()
    except sqlite3.Error as e:
        logger.error(f"Ошибка получения заявок: {e}", exc_info=True)
        return []

def update_application_status(app_id: int, status: str):
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
            conn.commit()
            return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка обновления статуса заявки #{app_id}: {e}", exc_info=True)
        return False

def mark_application_as_published(app_id: int):
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE applications SET published_at = CURRENT_TIMESTAMP, status = 'published' WHERE id = ?", (app_id,))
            conn.commit()
            return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка публикации заявки #{app_id}: {e}", exc_info=True)
        return False

def cleanup_old_applications(days: int = 30):
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM applications WHERE created_at < datetime('now', ?) AND status IN ('published', 'rejected')", (f"-{days} days",))
            conn.commit()
            logger.info(f"Очистка БД: удалены заявки старше {days} дней")
    except sqlite3.Error as e:
        logger.error(f"Ошибка очистки БД: {e}", exc_info=True)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def load_bad_words():
    try:
        async with aiofiles.open(BAD_WORDS_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            return [word.strip() for line in content.splitlines() for word in line.split(',') if word.strip()]
    except FileNotFoundError:
        return DEFAULT_BAD_WORDS
    except Exception as e:
        logger.error(f"Ошибка загрузки запрещенных слов: {e}", exc_info=True)
        return DEFAULT_BAD_WORDS

async def censor_text(text: str):
    bad_words = await load_bad_words()
    censored = text
    has_bad = any(re.search(re.escape(word), censored, re.IGNORECASE) for word in bad_words)
    if has_bad:
        for word in bad_words:
            censored = re.sub(re.escape(word), '***', censored, flags=re.IGNORECASE)
    return censored, has_bad

async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError as e:
        logger.warning(f"Ошибка отправки сообщения в {chat_id}: {e}")

async def safe_edit_message_text(query, text: str, **kwargs):
    if query and query.message:
        try:
            await query.edit_message_text(text=text, **kwargs)
        except TelegramError as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"Ошибка редактирования сообщения: {e}")

async def safe_reply_text(update: Update, text: str, **kwargs):
    if update.callback_query:
        await safe_edit_message_text(update.callback_query, text, **kwargs)
    elif update.message:
        await update.message.reply_text(text=text, **kwargs)

def get_uptime():
    if not BOT_STATE.get('start_time'): 
        return "N/A"
    uptime = datetime.now(TIMEZONE) - BOT_STATE['start_time']
    days, rem = divmod(uptime.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{int(days)}д {int(hours)}ч {int(minutes)}м"

def validate_name(name: str):
    return 2 <= len(name) <= MAX_NAME_LENGTH and bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-]+$', name))

def validate_phone(phone: str):
    clean_phone = re.sub(r'[^\d+]', '', phone)
    return bool(re.match(r'^(\+7|8)\d{10}$', clean_phone))

def is_holiday_active(holiday_date_str: str):
    holiday_date = datetime.strptime(f"{datetime.now().year}-{holiday_date_str}", "%Y-%m-%d").date()
    today = datetime.now().date()
    return (holiday_date - timedelta(days=5)) <= today <= (holiday_date + timedelta(days=5))

async def notify_admin_new_application(bot: Bot, app_id: int, app_details: dict):
    if not ADMIN_CHAT_ID: 
        return
    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', 'Заявка')
    has_photo = "✅" if app_details.get('photo_id') else "❌"
    phone = f"\n• Телефон: {app_details['phone_number']}" if app_details.get('phone_number') else ""
    text = (
        f"📨 Новая заявка #{app_id}\n"
        f"• Тип: {app_type}\n• Фото: {has_photo}{phone}\n"
        f"• От: @{app_details.get('username') or 'N/A'} (ID: {app_details['user_id']})\n"
        f"• Текст: {app_details['text'][:200]}...\n\nВыберите действие:"
    )
    keyboard = [[InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")],
                [InlineKeyboardButton("👀 Посмотреть", callback_data=f"view_{app_id}")]]
    await safe_send_message(bot, ADMIN_CHAT_ID, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def notify_user_about_decision(bot: Bot, app_details: dict, approved: bool):
    user_id = app_details['user_id']
    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', 'заявка')
    status = "одобрена" if approved else "отклонена модератором"
    icon = "🎉" if approved else "😕"
    text = f"{icon} Ваша заявка на «{app_type}» была {status}."
    await safe_send_message(bot, user_id, text)

def can_submit_request(user_id: int):
    try:
        with get_db_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM applications WHERE user_id = ? AND created_at > datetime('now', '-1 hour')", (user_id,)).fetchone()[0]
            return count < 5
    except sqlite3.Error as e:
        logger.error(f"Ошибка проверки лимита заявок: {e}")
        return True

# ========== ПУБЛИКАЦИЯ И ПЛАНИРОВЩИК ==========
async def publish_to_channel(app_id: int, bot: Bot):
    if not CHANNEL_ID: 
        return False
    app_details = get_application_details(app_id)
    if not app_details: 
        return False

    text = app_details['text']
    photo_id = app_details['photo_id']
    phone = app_details['phone_number']
    
    if phone:
        text += f"\n\n📞 Телефон: {phone}"

    message_text = f"{text}\n\n#НебольшойМирНиколаевск"
    try:
        if photo_id:
            await bot.send_photo(CHANNEL_ID, photo=photo_id, caption=message_text)
        else:
            await bot.send_message(CHANNEL_ID, text=message_text, disable_web_page_preview=True)
        mark_application_as_published(app_id)
        logger.info(f"Опубликовано сообщение #{app_id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации #{app_id}: {e}")
        return False

async def check_pending_applications(context: CallbackContext):
    bot = context.bot
    try:
        applications = get_approved_unpublished_applications()
        for app in applications:
            publish_now = True
            if app['type'] == 'congrat' and app['publish_date']:
                publish_date_obj = datetime.strptime(app['publish_date'], "%Y-%m-%d").date()
                if publish_date_obj > datetime.now().date():
                    publish_now = False
            
            if publish_now:
                await publish_to_channel(app['id'], bot)
                await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Ошибка проверки заявок: {e}")

# ========== ОБРАБОТЧИКИ ДИАЛОГА ==========
async def start_command(update: Update, context: CallbackContext):
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton(f"{info['icon']} {info['name']}", callback_data=key)] for key, info in REQUEST_TYPES.items()]
    await safe_reply_text(update, "Здравствуйте! Выберите тип заявки:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TYPE_SELECTION

async def handle_type_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    request_type = query.data
    context.user_data["type"] = request_type

    if request_type == "news":
        await safe_edit_message_text(query, f"Введите вашу новость (до {MAX_TEXT_LENGTH} симв.) и/или прикрепите фото:", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return ANNOUNCE_TEXT_INPUT
    elif request_type == "congrat":
        await safe_edit_message_text(query, "Введите ваше имя (например: *Иванов Виталий*):", reply_markup=InlineKeyboardMarkup(BACK_BUTTON), parse_mode="Markdown")
        return SENDER_NAME_INPUT
    elif request_type == "announcement":
        keyboard = [[InlineKeyboardButton(subtype, callback_data=f"subtype_{key}")] for key, subtype in ANNOUNCE_SUBTYPES.items()] + BACK_BUTTON
        await safe_edit_message_text(query, "Выберите тип объявления:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ANNOUNCE_SUBTYPE_SELECTION
    return ConversationHandler.END

async def get_sender_name(update: Update, context: CallbackContext):
    sender_name = update.message.text.strip()
    if not validate_name(sender_name):
        await safe_reply_text(update, f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} симв).")
        return SENDER_NAME_INPUT
    context.user_data["from_name"] = sender_name
    await safe_reply_text(update, f"Кого поздравляете? Например: *{EXAMPLE_TEXTS['recipient_name']}*", parse_mode="Markdown")
    return RECIPIENT_NAME_INPUT

async def get_recipient_name(update: Update, context: CallbackContext):
    recipient_name = update.message.text.strip()
    if not validate_name(recipient_name):
        await safe_reply_text(update, f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} симв).")
        return RECIPIENT_NAME_INPUT
    context.user_data["to_name"] = recipient_name
    keyboard = [[InlineKeyboardButton(h, callback_data=f"holiday_{h}")] for h in HOLIDAYS if is_holiday_active(HOLIDAYS[h])]
    keyboard += [[InlineKeyboardButton("🎂 Свой праздник (указать дату)", callback_data="custom_congrat")], [InlineKeyboardButton("🔙 В начало", callback_data="back_to_start")]]
    await safe_reply_text(update, "Выберите праздник или свой вариант:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_HOLIDAY_CHOICE

async def handle_congrat_holiday_choice(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    if query.data == "custom_congrat":
        context.user_data["congrat_type"] = "custom"
        await safe_edit_message_text(query, f"Введите текст поздравления (до {MAX_TEXT_LENGTH} симв.):", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    
    holiday = query.data.replace("holiday_", "")
    template = HOLIDAY_TEMPLATES.get(holiday, "С праздником!")
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name} с {holiday}!\n\n{template}"
    context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
    return await complete_request(update, context)

async def get_custom_congrat_message(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст слишком длинный (макс. {MAX_TEXT_LENGTH} симв).")
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name}!\n\n{text}"
    await safe_reply_text(update, "Введите дату публикации в формате ДД-ММ-ГГГГ:")
    return CONGRAT_DATE_INPUT

async def get_congrat_date(update: Update, context: CallbackContext):
    date_str = update.message.text.strip()
    try:
        publish_date = datetime.strptime(date_str, "%d-%m-%Y").date()
        if publish_date < datetime.now().date():
            await safe_reply_text(update, "Нельзя указать прошедшую дату.")
            return CONGRAT_DATE_INPUT
        context.user_data["publish_date"] = publish_date.strftime("%Y-%m-%d")
        return await complete_request(update, context)
    except ValueError:
        await safe_reply_text(update, "Неверный формат даты. Введите ДД-ММ-ГГГГ:")
        return CONGRAT_DATE_INPUT

async def handle_announce_subtype_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    subtype_key = query.data.replace("subtype_", "")
    context.user_data["subtype"] = subtype_key
    
    if subtype_key == "demand_offer":
        await safe_edit_message_text(query, "Введите ваш контактный телефон (формат: +7... или 8...):", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return PHONE_INPUT
    
    example = EXAMPLE_TEXTS["announcement"].get(subtype_key, "")
    await safe_edit_message_text(query, f"Введите текст объявления (до {MAX_TEXT_LENGTH} симв.) и/или прикрепите фото.\nПример: *{example}*", reply_markup=InlineKeyboardMarkup(BACK_BUTTON), parse_mode="Markdown")
    return ANNOUNCE_TEXT_INPUT

async def get_phone_number(update: Update, context: CallbackContext):
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await safe_reply_text(update, "Неверный формат номера. Используйте +7... или 8...")
        return PHONE_INPUT
    context.user_data["phone_number"] = phone
    await safe_reply_text(update, f"Теперь введите текст объявления (до {MAX_TEXT_LENGTH} симв.) и/или прикрепите фото:")
    return ANNOUNCE_TEXT_INPUT

async def process_text_and_photo(update: Update, context: CallbackContext):
    if update.message.photo:
        context.user_data["photo_id"] = update.message.photo[-1].file_id
    
    text = update.message.text or update.message.caption
    if not text:
        await safe_reply_text(update, "Пожалуйста, введите текст к вашему сообщению.")
        return ANNOUNCE_TEXT_INPUT
        
    text = text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст слишком длинный (макс. {MAX_TEXT_LENGTH} симв).")
        return ANNOUNCE_TEXT_INPUT
    
    context.user_data["text"] = text
    return await complete_request(update, context)

async def handle_censor_choice(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    if query.data == "accept_censor":
        context.user_data["text"] = context.user_data["censored_text"]
        return await complete_request(update, context)
    elif query.data == "edit_censor":
        await safe_edit_message_text(query, "Введите исправленный текст:")
        if context.user_data.get("type") == "congrat":
            return CUSTOM_CONGRAT_MESSAGE_INPUT
        return ANNOUNCE_TEXT_INPUT
    return ConversationHandler.END

async def complete_request(update: Update, context: CallbackContext):
    user = update.effective_user
    if not can_submit_request(user.id):
        await safe_reply_text(update, "Вы отправили слишком много заявок. Попробуйте позже.")
        return ConversationHandler.END

    user_data = context.user_data
    final_text = user_data.get("text", "")
    
    censored_text, has_bad = await censor_text(final_text)
    if has_bad:
        user_data["censored_text"] = censored_text
        keyboard = [[InlineKeyboardButton("✅ Принять и отправить", callback_data="accept_censor")],
                    [InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_censor")]]
        await safe_reply_text(update, f"⚠️ В тексте найдены запрещённые слова (заменены на ***):\n\n{censored_text}\n\nПодтвердите или измените текст:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAIT_CENSOR_APPROVAL

    app_data = {
        'user_id': user.id, 'username': user.username, 'type': user_data['type'],
        'text': final_text, 'photo_id': user_data.get('photo_id'),
        'from_name': user_data.get('from_name'), 'to_name': user_data.get('to_name'),
        'congrat_type': user_data.get('congrat_type'), 'publish_date': user_data.get('publish_date'),
        'subtype': user_data.get('subtype'), 'phone_number': user_data.get('phone_number')
    }
    
    app_id = add_application(app_data)
    if app_id:
        await notify_admin_new_application(context.bot, app_id, app_data)
        request_type_name = REQUEST_TYPES.get(user_data['type'], {}).get('name', 'заявка')
        await safe_reply_text(update, f"✅ Ваша заявка на «{request_type_name}» (№{app_id}) отправлена на проверку!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    else:
        await safe_reply_text(update, "❌ Произошла ошибка. Попробуйте позже.", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    
    context.user_data.clear()
    return ConversationHandler.END

async def back_to_start(update: Update, context: CallbackContext):
    if update.callback_query:
        await update.callback_query.answer()
    return await start_command(update, context)

async def cancel_command(update: Update, context: CallbackContext):
    await safe_reply_text(update, "Действие отменено.", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    context.user_data.clear()
    return ConversationHandler.END

# ========== ОБРАБОТЧИКИ АДМИНА ==========
async def admin_view_application(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    app_id = int(query.data.split('_')[1])
    app_details = get_application_details(app_id)

    if not app_details:
        await safe_edit_message_text(query, "Заявка не найдена.")
        return

    app_type = REQUEST_TYPES.get(app_details['type'], {}).get('name', 'Заявка')
    subtype = ANNOUNCE_SUBTYPES.get(app_details['subtype'], '') if app_details['subtype'] else ''
    from_name = f"От: {app_details['from_name']}\n" if app_details['from_name'] else ''
    to_name = f"Кому: {app_details['to_name']}\n" if app_details['to_name'] else ''
    phone = f"Телефон: {app_details['phone_number']}\n" if app_details['phone_number'] else ''
    publish_date = f"Дата публикации: {app_details['publish_date']}\n" if app_details['publish_date'] else ''

    text = (
        f"📝 Детали заявки #{app_id}\n"
        f"Тип: {app_type} {subtype}\n"
        f"{from_name}{to_name}{phone}{publish_date}"
        f"Текст: {app_details['text']}\n"
        f"Пользователь: @{app_details.get('username', 'N/A')} (ID: {app_details['user_id']})\n"
        f"Статус: {app_details['status']}\n"
        f"Создано: {app_details['created_at']}"
    )

    keyboard = [[InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")]]
    
    if app_details['photo_id']:
        try:
            await context.bot.send_photo(query.message.chat.id, photo=app_details['photo_id'], caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await query.delete_message()
        except TelegramError as e:
            logger.error(f"Ошибка отправки фото админу: {e}")
            await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_approve_application(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    app_id = int(query.data.split('_')[1])
    app_details = get_application_details(app_id)

    if not app_details:
        await safe_edit_message_text(query, "Заявка не найдена.")
        return

    if update_application_status(app_id, 'approved'):
        await safe_edit_message_text(query, f"Заявка #{app_id} одобрена.")
        await notify_user_about_decision(context.bot, app_details, True)
    else:
        await safe_edit_message_text(query, f"Ошибка одобрения заявки #{app_id}.")

async def admin_reject_application(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    app_id = int(query.data.split('_')[1])
    app_details = get_application_details(app_id)

    if not app_details:
        await safe_edit_message_text(query, "Заявка не найдена.")
        return

    if update_application_status(app_id, 'rejected'):
        await safe_edit_message_text(query, f"Заявка #{app_id} отклонена.")
        await notify_user_about_decision(context.bot, app_details, False)
    else:
        await safe_edit_message_text(query, f"Ошибка отклонения заявки #{app_id}.")

# ========== НАСТРОЙКА ПРИЛОЖЕНИЯ TELEGRAM ==========
async def setup_telegram_application():
    global application
    if application is not None:
        return

    application = Application.builder().token(TOKEN).build()
    await application.initialize()

    # Инициализация БД
    init_db()

    # Диалог для создания заявки
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            TYPE_SELECTION: [CallbackQueryHandler(handle_type_selection, pattern="^(congrat|announcement|news)$")],
            SENDER_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sender_name)],
            RECIPIENT_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_recipient_name)],
            CONGRAT_HOLIDAY_CHOICE: [CallbackQueryHandler(handle_congrat_holiday_choice, pattern="^(holiday_.*|custom_congrat)$")],
            CUSTOM_CONGRAT_MESSAGE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_congrat_message)],
            CONGRAT_DATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_congrat_date)],
            ANNOUNCE_SUBTYPE_SELECTION: [CallbackQueryHandler(handle_announce_subtype_selection, pattern="^subtype_.*")],
            PHONE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_number)],
            ANNOUNCE_TEXT_INPUT: [MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, process_text_and_photo)],
            WAIT_CENSOR_APPROVAL: [
                CallbackQueryHandler(handle_censor_choice, pattern="^(accept_censor|edit_censor)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_text_and_photo)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CallbackQueryHandler(back_to_start, pattern="^back_to_start$")
        ],
        allow_reentry=True
    )

    application.add_handler(conv_handler)

    # Админские коллбэки
    application.add_handler(CallbackQueryHandler(admin_approve_application, pattern="^approve_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_reject_application, pattern="^reject_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_view_application, pattern="^view_\d+$"))

    # Установка вебхука
    if WEBHOOK_URL and TOKEN:
        webhook_url = f"{WEBHOOK_URL}/telegram-webhook/{WEBHOOK_SECRET}"
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"Вебхук установлен: {webhook_url}")
    else:
        logger.warning("WEBHOOK_URL или TELEGRAM_TOKEN не заданы. Вебхук не будет установлен.")

    # Запуск планировщика
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_pending_applications, 'interval', minutes=1, args=[application])
    scheduler.add_job(cleanup_old_applications, 'cron', hour=3, minute=0)
    scheduler.start()
    logger.info("Планировщик запущен.")

    # Уведомление администратора о запуске бота (ОТКЛЮЧЕНО)
    # Убрано сообщение "Бот запущен" как было запрошено
    if ADMIN_CHAT_ID:
        BOT_STATE['start_time'] = datetime.now(TIMEZONE)
        # await safe_send_message(application.bot, ADMIN_CHAT_ID, f"✅ Бот запущен! Время работы: {get_uptime()}", disable_notification=True)
        logger.info(f"Бот запущен. Уведомление администратору отключено по запросу.")
    BOT_STATE['running'] = True
    logger.info("Telegram Application setup complete.")

# ========== МАРШРУТЫ FASTAPI ==========
@app.on_event("startup")
async def startup_event():
    logger.info("Запуск FastAPI сервера...")
    await setup_telegram_application()
    logger.info("FastAPI сервер запущен.")

@app.post("/telegram-webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    
    if application is None:
        logger.error("Telegram Application не инициализирован.")
        raise HTTPException(status_code=500, detail="Bot not initialized")

    try:
        update = Update.de_json(await request.json(), application.bot)
        await application.process_update(update)
        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status")
async def status():
    return {"status": "running", "uptime": get_uptime(), "bot_initialized": application is not None}

@app.get("/")
async def root():
    return {"message": "Telegram Bot Webhook Listener"}

# Для локального запуска (если не через uvicorn)
if __name__ == "__main__":
    # Создаем заглушку для WEBHOOK_URL и WEBHOOK_SECRET, если они не заданы
    # Это позволит запускать бота локально без вебхука, но с polling
    if not os.getenv('WEBHOOK_URL') or not os.getenv('WEBHOOK_SECRET'):
        logger.warning("WEBHOOK_URL или WEBHOOK_SECRET не заданы. Бот будет работать в режиме polling (для локальной отладки).")
        async def run_polling():
            await setup_telegram_application()
            await application.run_polling()
        
        # Запускаем polling в отдельном потоке/задаче, чтобы не блокировать FastAPI
        # В реальном продакшене используйте только вебхуки
        import threading
        threading.Thread(target=lambda: asyncio.run(run_polling())).start()
        
    uvicorn.run(app, host="0.0.0.0", port=PORT)
