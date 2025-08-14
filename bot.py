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

if not all([TOKEN, CHANNEL_ID, ADMIN_CHAT_ID]):
    raise ValueError("Ключевые переменные Telegram не установлены!")

# ========== Константы ==========
BACK_BUTTON = [[InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]]
DB_FILE = 'db.sqlite'
BAD_WORDS_FILE = 'bad_words.txt'
DEFAULT_BAD_WORDS = ["хуй", "пизда", "блять", "блядь", "ебать", "сука"]
MAX_NAME_LENGTH = 50

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
 NEWS_TEXT_INPUT) = range(12)

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

async def check_text_for_bad_words(text: str) -> tuple[str, bool]:
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
            WHERE user_id = ? AND created_at > datetime('now', '-24 hours')
        """, (user_id,))
        count = cur.fetchone()[0]
        return count < 5

# ========== Обработчики ==========
# ========== ФУНКЦИЯ ПРОВЕРКИ И ПУБЛИКАЦИИ ЗАЯВОК ==========
async def check_pending_applications(application: Application):
    """
    Проверяет базу данных на наличие заявок, ожидающих публикации.
    Вызывается планировщиком каждую минуту.
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Находим заявки, которые одобрены, но еще не опубликованы
            cur.execute("""
                SELECT id, type, from_name, to_name, text, photo_id, publish_date, congrat_type
                FROM applications
                WHERE status = 'approved' AND published_at IS NULL
            """)
            pending_apps = cur.fetchall()

        if not pending_apps:
            logger.info("Нет заявок, ожидающих публикации.")
            return

        logger.info(f"Найдено {len(pending_apps)} заявок для публикации.")
        bot = application.bot

        for app in pending_apps:
            app_id = app['id']
            try:
                # Формируем сообщение
                if app['type'] == 'congrat':
                    # Для поздравлений
                    message = (
                        f"🎉 <b>Поздравление</b>\n\n"
                        f"{app['text']}\n\n"
                        f"От {app['from_name']}"
                    )
                else:
                    # Для объявлений и новостей
                    message = (
                        f"📢 <b>Объявление</b>\n\n"
                        f"{app['text']}"
                    )

                if app['publish_date']:
                    message += f"\n\n📅 Опубликовано для даты: {app['publish_date']}"

                message += f"\n\n#НебольшойМир:Николаевск"

                # Публикуем
                if app['photo_id']:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=app['photo_id'],
                        caption=message,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=message,
                        parse_mode="HTML"
                    )

                # Отмечаем как опубликованную
                mark_application_as_published(app_id)
                logger.info(f"Заявка #{app_id} успешно опубликована.")

            except Exception as e:
                logger.error(f"Ошибка публикации заявки #{app_id}: {e}", exc_info=True)
                # Не отмечаем как опубликованную — попробуем снова

    except Exception as e:
        logger.error(f"Критическая ошибка при проверке заявок: {e}", exc_info=True)

def mark_application_as_published(app_id: int):
    """Отмечает заявку как опубликованную."""
    try:
        with get_db_connection() as conn:
            conn.execute("""
                UPDATE applications
                SET published_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (app_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при обновлении статуса заявки #{app_id}: {e}")
# ========== ОТПРАВКА НОВОСТИ АДМИНУ ==========
async def notify_admin_new_application(bot: Bot, app_id: int, app_data: dict):
    """Отправляет новость админу на модерацию."""
    message = (
        f"🗞️ <b>Новость от жителя</b>\n\n"
        f"{app_data['text']}\n\n"
        f"📞 Телефон: {app_data['phone_number']}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}")]
    ])
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить новость #{app_id} админу: {e}")

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

async def get_sender_name(update: Update, context: CallbackContext) -> int:
    sender_name = update.message.text.strip()
    if not validate_name(sender_name):
        await safe_reply_text(update, f"Пожалуйста, введите корректное имя (от 2 до {MAX_NAME_LENGTH} символов).")
        return SENDER_NAME_INPUT
    context.user_data["from_name"] = sender_name
    await safe_reply_text(update, f"Кого поздравляете? Например: *{EXAMPLE_TEXTS['recipient_name']}*", parse_mode="Markdown")
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
    await safe_reply_text(update, "Выберите праздник из списка или укажите свой:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_HOLIDAY_CHOICE

async def handle_congrat_holiday_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "custom_congrat":
        context.user_data["congrat_type"] = "custom"
        await safe_edit_message_text(query, f"Напишите своё поздравление (до {MAX_CONGRAT_TEXT_LENGTH} символов):", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return CUSTOM_CONGRAT_MESSAGE_INPUT

    holiday = query.data.replace("holiday_", "")
    if holiday not in HOLIDAYS:
        await safe_edit_message_text(query, "❌ Неизвестный праздник. Пожалуйста, выберите из списка.")
        return ConversationHandler.END

    template = HOLIDAY_TEMPLATES.get(holiday, "С праздником!")
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name} с {holiday}!{template}"
    context.user_data["congrat_type"] = "standard"

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="publish_today")],
        [InlineKeyboardButton("📆 Указать дату", callback_data="publish_custom_date")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_edit_message_text(query, "Когда опубликовать поздравление?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_DATE_CHOICE

async def handle_congrat_date_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "publish_today":
        context.user_data["publish_date"] = datetime.now().strftime("%Y-%m-%d")
        return await complete_request(update, context)
    elif query.data == "publish_custom_date":
        await safe_edit_message_text(query, "Введите дату публикации в формате ДД-ММ-ГГГГ:", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
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
    if len(text) > MAX_CONGRAT_TEXT_LENGTH:
        await safe_reply_text(update, f"Текст слишком длинный (максимум {MAX_CONGRAT_TEXT_LENGTH} символов).")
        return CUSTOM_CONGRAT_MESSAGE_INPUT
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")
    context.user_data["text"] = f"{from_name} поздравляет {to_name}!{text}"
    context.user_data["congrat_type"] = "custom"

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="publish_today")],
        [InlineKeyboardButton("📆 Указать дату", callback_data="publish_custom_date")],
        [InlineKeyboardButton("🔙 Вернуться в начало", callback_data="back_to_start")]
    ]
    await safe_reply_text(update, "Когда опубликовать поздравление?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONGRAT_DATE_CHOICE

async def complete_request(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    if not can_submit_request(user.id):
        await safe_reply_text(update, "Вы отправили слишком много заявок. Попробуйте позже.")
        return ConversationHandler.END

    type_ = context.user_data.get("type")
    text = context.user_data.get("text")
    publish_date = context.user_data.get("publish_date")
    from_name = context.user_data.get("from_name", "")
    to_name = context.user_data.get("to_name", "")

    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO applications (user_id, username, type, from_name, to_name, text, status, publish_date)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (user.id, user.username, type_, from_name, to_name, text, publish_date))
        app_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    await safe_reply_text(update, f"✅ Заявка #{app_id} отправлена на модерацию.")
    context.user_data.clear()
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
    scheduler.add_job(check_pending_applications, 'interval', minutes=1, args=[application])
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
    uvicorn.run(app, host="0.0.0.0", port=PORT)


