import asyncio
import json
import logging
import secrets
import sqlite3
import string
import uuid
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, BotCommand, InputMediaDocument, InputMediaPhoto
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = '9131866972:AAFdJ2It0RzILN4F79N_elYkTt-WZeuLZAo'
ADMIN_ID = 7842781365  # Telegram ID владельца

CARD_NUMBER = '2202205058178572'
CARD_OWNER = 'Никита Ф.'

SKIN_STYLES = {
    "classic": ("Classic", 229),
    "classic_plus": ("Classic +", 329),
    "future": ("Future", 389),
    "vip": ("VIP", 589),
    "vip_plus": ("VIP +", 789),
}
TOTEM_STYLES = {
    "2d": ("2D Тотем", 69),
    "3d": ("3D Тотем", 89),
    "2d_anim": ("2D Анимированный Тотем", 129),
}
FORMATS = {"64": "64x64", "128": "128x128"}
HAND_TYPES = {"thin": "Тонкие", "thick": "Толстые"}

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# =========================================================
# DATABASE + SAFE MIGRATIONS
# =========================================================
db = sqlite3.connect("polus_skins.db")
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")

db.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT,
    product TEXT NOT NULL,
    style TEXT NOT NULL,
    format TEXT,
    hands TEXT,
    price INTEGER NOT NULL,
    status TEXT NOT NULL,
    receipt_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

def ensure_columns(table, definitions):
    existing = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in definitions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    db.commit()

ensure_columns("orders", {
    "description": "TEXT NOT NULL DEFAULT ''",
    "base_price": "INTEGER NOT NULL DEFAULT 0",
    "promo_code": "TEXT",
    "discount": "INTEGER NOT NULL DEFAULT 0",
    "assigned_artist_id": "INTEGER",
    "delivery_files": "TEXT NOT NULL DEFAULT '[]'",
    "revision_note": "TEXT NOT NULL DEFAULT ''",
    "paid_at": "TEXT",
    "confirmed_at": "TEXT",
    "reference_files": "TEXT NOT NULL DEFAULT '[]'",
})
db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
ensure_columns("users", {"language": "TEXT NOT NULL DEFAULT 'ru'"})
db.execute("""
CREATE TABLE IF NOT EXISTS artists (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    balance INTEGER NOT NULL DEFAULT 0,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    discount_percent INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1,
    used_by INTEGER,
    used_order_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    text TEXT,
    file_id TEXT,
    file_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS balance_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    order_id TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    granted_by INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS payout_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    admin_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS balance_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    old_balance INTEGER NOT NULL,
    new_balance INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()

# Legacy rows: price is the original price if base_price was not migrated/populated.
db.execute("UPDATE orders SET base_price = price WHERE base_price = 0")
db.commit()

user_data = {}  # per-user conversational states
delivery_buffers = {}  # artist_id -> {order_id, files[]}
support_reply_ticket = {}  # admin_id -> ticket_id

def save_order(order_id, user_id, username, product, style, format_value,
               hands, price, description=""):
    db.execute("""
        INSERT INTO orders
        (id,user_id,username,product,style,format,hands,price,status,
         description,base_price)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (order_id,user_id,username,product,style,format_value,hands,
          price,"waiting_payment",description,price))
    db.commit()

def get_order(order_id):
    return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

def set_status(order_id, status, receipt_id=None):
    db.execute("UPDATE orders SET status=?, receipt_id=COALESCE(?,receipt_id) WHERE id=?",
               (status, receipt_id, order_id))
    db.commit()

def is_owner(user_id):
    return int(user_id) == int(ADMIN_ID)

def is_admin(user_id):
    return is_owner(user_id) or bool(
        db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    )

def is_artist(user_id):
    row = db.execute("SELECT active FROM artists WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["active"])

def is_staff(user_id):
    return is_admin(user_id) or is_artist(user_id)

def register_user(user):
    db.execute("""
        INSERT INTO users(user_id,username,full_name) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
        full_name=excluded.full_name
    """, (user.id, user.username or "", user.full_name or ""))
    db.commit()

def user_language(user_id):
    row = db.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
    return "en" if row and row["language"] == "en" else "ru"

TEXTS = {
    "ru": {
        "welcome": "Добро пожаловать в POLUS SKINS!\n\nВыбери нужную услугу:",
        "skin": "Заказать скин", "totem": "Заказать тотем", "language": "🌐 Язык / Language",
        "admin": "Админ-панель", "artist": "Кабинет художника",
        "choose": "Выбери стиль скина:", "format": "Выбери формат скина:",
        "hands": "Выбери тип рук:", "description": "Опиши заказ вкратце. Можно прикрепить фото/референсы к тексту или отправить их отдельно. Когда закончишь, нажми «Готово».",
        "totem_type": "Выбери тип тотема:", "language_title": "Выбери язык:",
        "saved": "Язык сохранён.", "order": "🧾 <b>ВАШ ЗАКАЗ POLUS SKINS</b>",
        "service": "Услуга", "format_label": "Формат", "hands_label": "Тип рук",
        "description_label": "Описание", "price": "Цена", "discount": "Скидка", "total": "К оплате",
        "promo": "Ввести промокод", "pay": "Перейти к оплате", "main": "В главное меню",
        "receipt": "Пришли фотографию или файл чека.", "receipt_sent": "Чек отправлен администратору на проверку.",
    },
    "en": {
        "welcome": "Welcome to POLUS SKINS!\n\nChoose a service:",
        "skin": "Order a skin", "totem": "Order a totem", "language": "🌐 Language / Язык",
        "admin": "Admin panel", "artist": "Artist dashboard",
        "choose": "Choose a skin style:", "format": "Choose a skin format:",
        "hands": "Choose hand type:", "description": "Briefly describe your order. Attach photos/references to the text or send them separately, then press “Done”.",
        "totem_type": "Choose a totem type:", "language_title": "Choose your language:",
        "saved": "Language saved.", "order": "🧾 <b>YOUR POLUS SKINS ORDER</b>",
        "service": "Service", "format_label": "Format", "hands_label": "Hand type",
        "description_label": "Description", "price": "Price", "discount": "Discount", "total": "Total",
        "promo": "Enter promo code", "pay": "Proceed to payment", "main": "Main menu",
        "receipt": "Send a photo or file of the receipt.", "receipt_sent": "Receipt sent to an administrator for review.",
    },
}

def tr(user_id, key):
    lang = user_language(user_id)
    return TEXTS.get(lang, TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))

async def notify_admins(bot, text):
    ids = {int(ADMIN_ID)}
    ids.update(r["user_id"] for r in db.execute("SELECT user_id FROM admins").fetchall())
    for admin_id in ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logging.exception("Admin notification failed for %s", admin_id)

def money(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"

def order_title(order):
    return f"{order['product']} — {order['style']}"

def get_artist_name(user_id):
    row = db.execute("SELECT username FROM artists WHERE user_id=?", (user_id,)).fetchone()
    return ("@" + row["username"]) if row and row["username"] else str(user_id)

def main_menu(user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text=tr(user_id, "skin"), callback_data="skin_start")
    kb.button(text=tr(user_id, "totem"), callback_data="totem_start")
    kb.button(text="👤 Личный кабинет", callback_data="customer_cabinet")
    kb.button(text=tr(user_id, "language"), callback_data="language_menu")
    if is_admin(user_id):
        kb.button(text=tr(user_id, "admin"), callback_data="admin_panel")
    if is_artist(user_id):
        kb.button(text=tr(user_id, "artist"), callback_data="artist_panel")
    kb.adjust(1)
    return kb.as_markup()


def customer_cabinet_menu(user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Все мои заказы", callback_data="customer_orders_all")
    kb.button(text="🛍 Купленные заказы", callback_data="customer_orders_paid")
    kb.button(text="🏠 В главное меню", callback_data="main")
    kb.adjust(1)
    return kb.as_markup()


def customer_order_list_keyboard(orders, back_callback):
    kb = InlineKeyboardBuilder()
    status_names = {
        "waiting_payment": "Ожидает оплаты", "check_sent": "Чек на проверке",
        "paid": "Оплачен, в работе", "delivered": "Готов", "confirmed": "Завершён",
        "revision_requested": "На правках", "rejected": "Отклонён",
    }
    for order in orders:
        status = status_names.get(order["status"], order["status"])
        kb.button(
            text=f"№{order['id']} · {order['product']} · {status}",
            callback_data=f"customer_order_{order['id']}"
        )
    kb.button(text="⬅️ В личный кабинет", callback_data=back_callback)
    kb.adjust(1)
    return kb.as_markup()

def admin_menu():
    kb = InlineKeyboardBuilder()
    for label, data in [
        ("📊 Главная статистика", "admin_stats"),
        ("🧾 Чеки на проверку", "admin_checks"),
        ("💰 Оплаченные заказы", "admin_paid"),
        ("📦 Все заказы", "admin_orders"),
        ("👥 Роли и художники", "artists_list"),
        ("💳 Балансы художников", "balances"),
        ("💸 Заявки на вывод", "payout_admin_list"),
        ("🛡 Администраторы", "admins_list"),
        ("🎟 Создать промокод −10%", "promo_create"),
        ("🎟 Управление промокодами", "promo_list"),
        ("💬 Входящие обращения", "support_list"),
        ("🔄 Обновить панель", "admin_panel"),
        ("🏠 В главное меню", "main"),
    ]:
        kb.button(text=label, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()

def artist_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Мои заказы", callback_data="artist_orders")
    kb.button(text="Мой баланс", callback_data="artist_balance")
    kb.button(text="💸 Запросить вывод", callback_data="payout_request")
    kb.button(text="В главное меню", callback_data="main")
    kb.adjust(1)
    return kb.as_markup()

def order_details(order):
    return (
        f"📦 ЗАКАЗ №{order['id']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 КЛИЕНТ\n"
        f"Имя: @{order['username'] or 'без username'}\n"
        f"Telegram ID: {order['user_id']}\n\n"
        f"🎨 ПАРАМЕТРЫ\n"
        f"Услуга: {order_title(order)}\n"
        f"Формат: {order['format'] or '—'}\n"
        f"Тип рук: {order['hands'] or '—'}\n\n"
        f"📝 ОПИСАНИЕ\n{order['description'] or 'Не указано'}\n\n"
        f"🔧 ПРАВКИ\n{order['revision_note'] or 'Нет'}\n\n"
        f"💳 ОПЛАТА\n"
        f"Цена: {money(order['base_price'] or order['price'])}\n"
        f"Скидка: {money(order['discount'])}\n"
        f"ИТОГО: {money(order['price'])}\n\n"
        f"Статус: {order['status']}\n"
        f"Художник: {get_artist_name(order['assigned_artist_id']) if order['assigned_artist_id'] else 'Не назначен'}"
    )

async def assign_artist(order_id):
    artists = db.execute("SELECT user_id FROM artists WHERE active=1 ORDER BY added_at,user_id").fetchall()
    if not artists:
        return None
    last = db.execute("""
        SELECT assigned_artist_id FROM orders
        WHERE assigned_artist_id IS NOT NULL
        ORDER BY paid_at DESC, created_at DESC LIMIT 1
    """).fetchone()
    ids = [r["user_id"] for r in artists]
    if last and last["assigned_artist_id"] in ids:
        idx = (ids.index(last["assigned_artist_id"]) + 1) % len(ids)
    else:
        idx = 0
    artist_id = ids[idx]
    db.execute("UPDATE orders SET assigned_artist_id=? WHERE id=?", (artist_id, order_id))
    db.commit()
    return artist_id

def new_promo_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "POLUS-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if not db.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone():
            return code

# =========================================================
# START / TELEGRAM COMMAND MENU
# =========================================================
@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user)
    user_data.pop(message.from_user.id, None)
    await message.answer(
        "Добро пожаловать в POLUS SKINS!\n\nВыбери нужную услугу:",
        reply_markup=main_menu(message.from_user.id)
    )

@dp.message(Command("help"))
async def help_command(message: Message):
    register_user(message.from_user)
    kb = InlineKeyboardBuilder()
    kb.button(text="Обратиться в поддержку", callback_data="support_new")
    kb.button(text="В главное меню", callback_data="main")
    kb.adjust(1)
    await message.answer("Помощь POLUS SKINS\nНажми кнопку ниже, чтобы написать в поддержку.",
                         reply_markup=kb.as_markup())

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🔐 ДОСТУП ВЛАДЕЛЬЦА ПОДТВЕРЖДЁН\n"
        "Панель POLUS SKINS. Все разделы доступны только владельцу.",
        reply_markup=admin_menu()
    )

# =========================================================
# CUSTOMER PERSONAL CABINET
# =========================================================
@dp.callback_query(F.data == "customer_cabinet")
async def customer_cabinet(c: CallbackQuery):
    register_user(c.from_user)
    total = db.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (c.from_user.id,)).fetchone()[0]
    purchased = db.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('paid','delivered','revision_requested','confirmed')",
        (c.from_user.id,)
    ).fetchone()[0]
    spent = db.execute(
        "SELECT COALESCE(SUM(price),0) FROM orders WHERE user_id=? AND status IN ('paid','delivered','revision_requested','confirmed')",
        (c.from_user.id,)
    ).fetchone()[0]
    username = f"@{c.from_user.username}" if c.from_user.username else "не указан"
    text = (
        "👤 ЛИЧНЫЙ КАБИНЕТ\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Имя: {c.from_user.full_name}\n"
        f"Username: {username}\n"
        f"Всего заказов: {total}\n"
        f"Куплено заказов: {purchased}\n"
        f"Потрачено: {money(spent)}\n\n"
        "Выбери раздел:"
    )
    await c.message.edit_text(text, reply_markup=customer_cabinet_menu(c.from_user.id))
    await c.answer()


@dp.callback_query(F.data.in_({"customer_orders_all", "customer_orders_paid"}))
async def customer_orders(c: CallbackQuery):
    if c.data == "customer_orders_paid":
        orders = db.execute(
            "SELECT * FROM orders WHERE user_id=? AND status IN ('paid','delivered','revision_requested','confirmed') ORDER BY created_at DESC LIMIT 50",
            (c.from_user.id,)
        ).fetchall()
        title = "🛍 ТВОИ КУПЛЕННЫЕ ЗАКАЗЫ\n\nЗдесь отображаются оплаченные заказы:"
    else:
        orders = db.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (c.from_user.id,)
        ).fetchall()
        title = "📦 ВСЕ ТВОИ ЗАКАЗЫ\n\nВыбери заказ, чтобы посмотреть подробности:"
    if not orders:
        await c.message.edit_text(
            "Пока здесь нет заказов.",
            reply_markup=customer_cabinet_menu(c.from_user.id)
        )
    else:
        await c.message.edit_text(
            title,
            reply_markup=customer_order_list_keyboard(orders, "customer_cabinet")
        )
    await c.answer()


@dp.callback_query(F.data.startswith("customer_order_"))
async def customer_order_detail(c: CallbackQuery, bot: Bot):
    order_id = c.data.removeprefix("customer_order_")
    order = get_order(order_id)
    if not order or int(order["user_id"]) != int(c.from_user.id):
        await c.answer("Заказ не найден", show_alert=True)
        return
    status_names = {
        "waiting_payment": "Ожидает оплаты", "check_sent": "Чек отправлен на проверку",
        "paid": "Оплачен, находится в работе", "delivered": "Готов к получению",
        "confirmed": "Завершён", "revision_requested": "На доработке",
        "rejected": "Оплата отклонена",
    }
    details = (
        f"📦 ЗАКАЗ №{order['id']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Услуга: {order_title(order)}\n"
        f"Формат: {order['format'] or '—'}\n"
        f"Тип рук: {order['hands'] or '—'}\n"
        f"Описание: {order['description'] or '—'}\n"
        f"Стоимость: {money(order['price'])}\n"
        f"Дата: {order['created_at']}\n"
        f"Статус: {status_names.get(order['status'], order['status'])}"
    )
    kb = InlineKeyboardBuilder()
    if order["status"] == "waiting_payment":
        kb.button(text="💳 Перейти к оплате", callback_data=f"payment_{order_id}")
    if order["status"] in ("delivered", "confirmed") and order["delivery_files"]:
        kb.button(text="📥 Получить файлы заказа", callback_data=f"customer_delivery_{order_id}")
    if order["status"] == "delivered":
        kb.button(text="✅ Подтвердить заказ", callback_data=f"confirm_{order_id}")
        kb.button(text="✏️ Запросить правки", callback_data=f"revision_{order_id}")
    kb.button(text="⬅️ К моим заказам", callback_data="customer_orders_all")
    kb.adjust(1)
    await c.message.edit_text(details, reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(F.data.startswith("customer_delivery_"))
async def customer_delivery(c: CallbackQuery, bot: Bot):
    order_id = c.data.removeprefix("customer_delivery_")
    order = get_order(order_id)
    if not order or int(order["user_id"]) != int(c.from_user.id):
        await c.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] not in ("delivered", "confirmed"):
        await c.answer("Файлы пока недоступны", show_alert=True)
        return
    try:
        files = json.loads(order["delivery_files"] or "[]")
    except (TypeError, json.JSONDecodeError):
        files = []
    if not files:
        await c.answer("Для этого заказа пока нет файлов", show_alert=True)
        return
    await c.answer("Отправляю файлы")
    for item in files:
        if item.get("type") == "photo":
            await bot.send_photo(c.from_user.id, item["id"], caption=f"Файлы заказа №{order_id}")
        else:
            await bot.send_document(c.from_user.id, item["id"], caption=f"Файлы заказа №{order_id}")


# =========================================================
# CUSTOMER ORDER FLOW
# =========================================================
@dp.callback_query(F.data == "skin_start")
async def skin_start(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for key, (name, price) in SKIN_STYLES.items():
        kb.button(text=f"{name} — {price} ₽", callback_data=f"skin_style_{key}")
    kb.button(text="Назад", callback_data="main")
    kb.adjust(1)
    await c.message.edit_text(tr(c.from_user.id, "choose"), reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("skin_style_"))
async def skin_style(c: CallbackQuery):
    style = c.data.removeprefix("skin_style_")
    if style not in SKIN_STYLES:
        await c.answer("Неизвестный стиль", show_alert=True); return
    user_data[c.from_user.id] = {"type":"skin","style":style}
    kb = InlineKeyboardBuilder()
    for key, name in FORMATS.items():
        kb.button(text=name, callback_data=f"format_{key}")
    kb.button(text="Назад", callback_data="skin_start")
    kb.adjust(1)
    await c.message.edit_text(tr(c.from_user.id, "format"), reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("format_"))
async def choose_format(c: CallbackQuery):
    key = c.data.removeprefix("format_")
    data = user_data.get(c.from_user.id)
    if key not in FORMATS or not data or data.get("type") != "skin":
        await c.answer("Начни заказ заново", show_alert=True); return
    data["format"] = key
    kb = InlineKeyboardBuilder()
    kb.button(text="Тонкие", callback_data="hands_thin")
    kb.button(text="Толстые", callback_data="hands_thick")
    kb.button(text="Назад", callback_data="skin_start")
    kb.adjust(1)
    await c.message.edit_text(tr(c.from_user.id, "hands"), reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("hands_"))
async def choose_hands(c: CallbackQuery):
    hands = c.data.removeprefix("hands_")
    data = user_data.get(c.from_user.id)
    if hands not in HAND_TYPES or not data or data.get("type") != "skin":
        await c.answer("Начни заказ заново", show_alert=True); return
    data.update({"hands":hands,"awaiting_description":True,"description":"","reference_files":[]})
    await c.message.answer(tr(c.from_user.id, "description"),
                           reply_markup=_description_done_keyboard())
    await c.answer()

@dp.callback_query(F.data == "totem_start")
async def totem_start(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for key,(name,price) in TOTEM_STYLES.items():
        kb.button(text=f"{name} — {price} ₽", callback_data=f"totem_{key}")
    kb.button(text="Назад", callback_data="main")
    kb.adjust(1)
    await c.message.edit_text(tr(c.from_user.id, "totem_type"), reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("totem_"))
async def choose_totem(c: CallbackQuery):
    key = c.data.removeprefix("totem_")
    if key not in TOTEM_STYLES:
        await c.answer("Неизвестный вариант", show_alert=True); return
    name, price = TOTEM_STYLES[key]
    user_data[c.from_user.id] = {
        "type": "totem", "totem_name": name, "price": price,
        "awaiting_description": True, "description": "", "reference_files": []
    }
    await c.message.answer(tr(c.from_user.id, "description"),
                           reply_markup=_description_done_keyboard())
    await c.answer()

async def show_order_summary(message, order):
    uid = message.chat.id
    kb = InlineKeyboardBuilder()
    kb.button(text=tr(uid, "promo"), callback_data=f"promo_apply_{order['id']}")
    kb.button(text=tr(uid, "pay"), callback_data=f"payment_{order['id']}")
    kb.button(text=tr(uid, "main"), callback_data="main")
    kb.adjust(1)
    await message.answer(
        f"{tr(uid, 'order')}\n━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>🎨 {tr(uid, 'service')}</b>\n{order_title(order)}\n\n"
        f"<b>📐 {tr(uid, 'format_label')}</b>\n{order['format']}\n\n"
        f"<b>🖐 {tr(uid, 'hands_label')}</b>\n{order['hands']}\n\n"
        f"<b>📝 {tr(uid, 'description_label')}</b>\n{order['description'] or '—'}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{tr(uid, 'price')}</b>: {money(order['base_price'])}\n"
        f"<b>{tr(uid, 'discount')}</b>: {money(order['discount'])}\n"
        f"<b>{tr(uid, 'total')}</b>: {money(order['price'])}",
        reply_markup=kb.as_markup(), parse_mode="HTML"
    )

def _description_done_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="description_done")
    kb.button(text="Отмена", callback_data="main")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data == "description_done")
async def description_done(c: CallbackQuery):
    state = user_data.get(c.from_user.id, {})
    if not state.get("awaiting_description"):
        await c.answer("Сначала начни оформление заказа.", show_alert=True)
        return
    description = (state.get("description") or "").strip()
    files = state.get("reference_files", [])
    if not description and not files:
        await c.answer("Добавь описание или хотя бы одно фото.", show_alert=True)
        return

    order_id = uuid.uuid4().hex[:10]
    if state.get("type") == "skin":
        style_name, price = SKIN_STYLES[state["style"]]
        save_order(order_id, c.from_user.id, c.from_user.username or "", "Скин",
                   style_name, FORMATS[state["format"]], HAND_TYPES[state["hands"]],
                   price, description)
    elif state.get("type") == "totem":
        save_order(order_id, c.from_user.id, c.from_user.username or "", "Тотем",
                   state["totem_name"], "Не требуется", "Не требуется",
                   state["price"], description)
    else:
        await c.answer("Не удалось определить тип заказа.", show_alert=True)
        return

    db.execute("UPDATE orders SET reference_files=? WHERE id=?",
               (json.dumps(files), order_id))
    db.commit()
    state.clear()
    state["order_id"] = order_id
    await c.message.answer("Описание и референсы сохранены.")
    await show_order_summary(c.message, get_order(order_id))
    await c.answer("Заказ создан")


@dp.callback_query(F.data == "language_menu")
async def language_menu(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data="language_ru")
    kb.button(text="🇬🇧 English", callback_data="language_en")
    kb.button(text="⬅️ Назад", callback_data="main")
    kb.adjust(1)
    await c.message.edit_text(tr(c.from_user.id, "language_title"), reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.in_({"language_ru", "language_en"}))
async def language_set(c: CallbackQuery):
    lang = c.data.removeprefix("language_")
    db.execute("UPDATE users SET language=? WHERE user_id=?", (lang, c.from_user.id))
    db.commit()
    await c.message.edit_text(tr(c.from_user.id, "saved"), reply_markup=main_menu(c.from_user.id))
    await c.answer()

@dp.message(Command("language"))
async def language_command(message: Message):
    register_user(message.from_user)
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data="language_ru")
    kb.button(text="🇬🇧 English", callback_data="language_en")
    kb.adjust(1)
    await message.answer("Выбери язык / Choose language:", reply_markup=kb.as_markup())

@dp.message(F.text)
async def text_router(message: Message, bot: Bot):
    uid = message.from_user.id
    register_user(message.from_user)
    state = user_data.get(uid, {})

    if state.get("awaiting_description"):
        description = message.text.strip()
        if len(description) > 1000:
            await message.answer("Слишком длинное описание. Максимум 1000 символов."); return
        state["description"] = description
        await message.answer("Текст сохранён. Можешь добавить фото/референсы или нажать «Готово».",
                             reply_markup=_description_done_keyboard())
        return

    if state.get("awaiting_promo"):
        order_id = state["awaiting_promo"]
        order = get_order(order_id)
        code = message.text.strip().upper()
        promo = db.execute("SELECT * FROM promo_codes WHERE code=? AND active=1 AND used_by IS NULL",
                           (code,)).fetchone()
        if not order or order["status"] != "waiting_payment":
            await message.answer("Заказ уже недоступен."); return
        if not promo:
            await message.answer("Промокод не найден, отключён или уже использован. Попробуй ещё раз."); return
        discount = (order["base_price"] * promo["discount_percent"]) // 100
        final_price = max(0, order["base_price"] - discount)
        db.execute("UPDATE orders SET promo_code=?,discount=?,price=? WHERE id=?",
                   (code,discount,final_price,order_id))
        db.commit()
        state.pop("awaiting_promo",None)
        await message.answer(f"Промокод применён: скидка {promo['discount_percent']}%.\nНовая цена: {money(final_price)}")
        await show_order_summary(message,get_order(order_id))
        return

    if state.get("awaiting_support"):
        text = message.text.strip()
        if not text:
            await message.answer("Напиши текст обращения."); return
        ticket = db.execute("SELECT id FROM support_tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                            (uid,)).fetchone()
        if not ticket:
            cur = db.execute("INSERT INTO support_tickets(user_id,username) VALUES(?,?)",
                             (uid,message.from_user.username or ""))
            ticket_id = cur.lastrowid
        else:
            ticket_id = ticket["id"]
        db.execute("INSERT INTO support_messages(ticket_id,sender_id,text) VALUES(?,?,?)",
                   (ticket_id,uid,text))
        db.commit()
        state.pop("awaiting_support",None)
        await message.answer(f"Обращение №{ticket_id} отправлено. Ответ придёт сюда.")
        await notify_admins(bot, f"Новое сообщение в поддержке №{ticket_id}\nОт: @{message.from_user.username or 'без username'} (ID {uid})\n\n{text}")
        return

    if state.get("awaiting_revision"):
        order_id = state["awaiting_revision"]
        order = get_order(order_id)
        if not order or order["user_id"] != uid or order["status"] != "delivered":
            await message.answer("Этот заказ сейчас не ожидает правок."); return
        note = message.text.strip()
        if not note:
            await message.answer("Опиши, какие правки нужны."); return
        db.execute("UPDATE orders SET status='revision_requested',revision_note=? WHERE id=?",
                   (note,order_id))
        db.commit()
        state.pop("awaiting_revision",None)
        await message.answer("Правки отправлены художнику.")
        if order["assigned_artist_id"]:
            await bot.send_message(order["assigned_artist_id"],
                f"Клиент запросил правки по заказу {order_id}:\n\n{note}")
        await notify_admins(bot, f"Правки по заказу {order_id}:\n{note}")
        return

    if state.get("awaiting_artist_username") and is_admin(uid):
        username = message.text.strip().lstrip("@").lower()
        user = db.execute("SELECT * FROM users WHERE lower(username)=?",(username,)).fetchone()
        if not user:
            await message.answer("Не нашёл пользователя. Попроси художника открыть бота и нажать /start, затем повтори."); return
        db.execute("""
            INSERT INTO artists(user_id,username,active,balance) VALUES(?,?,1,0)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,active=1
        """,(user["user_id"],username))
        db.commit()
        state.pop("awaiting_artist_username",None)
        await message.answer(f"Роль художника выдана @{username} (ID {user['user_id']}).")
        await bot.send_message(user["user_id"],"Тебе выдана роль художника POLUS SKINS. Открой /start.")
        return

    if state.get("awaiting_support_reply") and is_admin(uid):
        ticket_id = support_reply_ticket.get(uid)
        if not ticket_id:
            state.pop("awaiting_support_reply",None); return
        ticket = db.execute("SELECT user_id FROM support_tickets WHERE id=?",(ticket_id,)).fetchone()
        if not ticket:
            await message.answer("Обращение не найдено."); return
        db.execute("INSERT INTO support_messages(ticket_id,sender_id,text) VALUES(?,?,?)",
                   (ticket_id,uid,message.text))
        db.commit()
        await bot.send_message(ticket["user_id"],f"Ответ поддержки по обращению №{ticket_id}:\n\n{message.text}")
        await message.answer("Ответ отправлен.")
        state.pop("awaiting_support_reply",None)
        support_reply_ticket.pop(uid,None)
        return

    if state.get("awaiting_balance_edit") and is_admin(uid):
        artist_id = state["awaiting_balance_edit"]
        try:
            new_balance = int(message.text.strip().replace(" ", ""))
            if new_balance < 0 or new_balance > 100000000:
                raise ValueError
        except ValueError:
            await message.answer("Введи целое число от 0 до 100 000 000.")
            return
        artist = db.execute("SELECT balance FROM artists WHERE user_id=?", (artist_id,)).fetchone()
        if not artist:
            state.pop("awaiting_balance_edit", None)
            await message.answer("Художник не найден.")
            return
        old_balance = int(artist["balance"])
        db.execute("UPDATE artists SET balance=? WHERE user_id=?", (new_balance, artist_id))
        db.execute("INSERT INTO balance_adjustments(artist_id,admin_id,old_balance,new_balance,note) VALUES(?,?,?,?,?)",
                   (artist_id, uid, old_balance, new_balance, "Ручное изменение администратором"))
        db.commit()
        state.pop("awaiting_balance_edit", None)
        await message.answer(f"Баланс изменён: {money(old_balance)} → {money(new_balance)}.")
        try:
            await bot.send_message(artist_id, f"Администратор обновил твой баланс. Новый баланс: {money(new_balance)}.")
        except Exception:
            pass
        return

    if state.get("awaiting_payout_amount") and is_artist(uid):
        try:
            amount = int(message.text.strip().replace(" ", ""))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Введи положительную сумму целым числом.")
            return
        artist = db.execute("SELECT balance FROM artists WHERE user_id=?", (uid,)).fetchone()
        pending = db.execute("SELECT COALESCE(SUM(amount),0) FROM payout_requests WHERE artist_id=? AND status='pending'", (uid,)).fetchone()[0]
        available = int(artist["balance"] if artist else 0) - int(pending)
        if amount > available:
            await message.answer(f"Недостаточно доступного баланса. Доступно: {money(max(0, available))}.")
            return
        cur = db.execute("INSERT INTO payout_requests(artist_id,amount) VALUES(?,?)", (uid, amount))
        db.commit()
        state.pop("awaiting_payout_amount", None)
        await message.answer(f"Заявка №{cur.lastrowid} создана на сумму {money(amount)}. Ожидай решения администратора.")
        await notify_admins(bot, f"Новая заявка на вывод №{cur.lastrowid} от {get_artist_name(uid)} (ID {uid}): {money(amount)}. Открой заявку в админ-панели, чтобы связаться с художником и согласовать реквизиты отдельно.")
        return

    if state.get("awaiting_admin_username") and is_owner(uid):
        username = message.text.strip().lstrip("@").lower()
        user = db.execute("SELECT * FROM users WHERE lower(username)=?", (username,)).fetchone()
        if not user:
            await message.answer("Пользователь не найден. Попроси его открыть бота через /start и повтори.")
            return
        if int(user["user_id"]) == int(ADMIN_ID):
            await message.answer("Владелец уже имеет полный доступ.")
            state.pop("awaiting_admin_username", None)
            return
        db.execute("INSERT INTO admins(user_id,username,granted_by) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                   (user["user_id"], username, uid))
        db.commit()
        state.pop("awaiting_admin_username", None)
        await message.answer(f"Роль администратора выдана @{username}.")
        await bot.send_message(user["user_id"], "Тебе выдана роль администратора POLUS SKINS. Открой /admin.")
        return

    if state.get("awaiting_artist_delivery_note") and is_artist(uid):
        order_id = state.get("delivery_order")
        if order_id:
            order = get_order(order_id)
            if order and order["assigned_artist_id"] == uid:
                state["delivery_note"] = message.text.strip()
                await message.answer("Комментарий сохранён. Теперь отправь файлы заказа, затем нажми «Завершить сдачу».")
                return

# =========================================================
# PROMO FLOW
# =========================================================
@dp.callback_query(F.data.startswith("promo_apply_"))
async def promo_apply(c: CallbackQuery):
    order_id = c.data.removeprefix("promo_apply_")
    order = get_order(order_id)
    if not order or order["user_id"] != c.from_user.id or order["status"] != "waiting_payment":
        await c.answer("Заказ недоступен",show_alert=True); return
    user_data.setdefault(c.from_user.id,{})["awaiting_promo"] = order_id
    await c.message.answer("Отправь промокод одним сообщением.")
    await c.answer()

@dp.callback_query(F.data.startswith("payment_"))
async def payment(c: CallbackQuery):
    order_id = c.data.removeprefix("payment_")
    order = get_order(order_id)
    if not order or order["user_id"] != c.from_user.id or order["status"] != "waiting_payment":
        await c.answer("Заказ недоступен",show_alert=True); return
    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить чек",callback_data=f"receipt_{order_id}")
    kb.button(text="Назад к заказу",callback_data=f"summary_{order_id}")
    kb.adjust(1)
    await c.message.answer(
        f"ОПЛАТА POLUS SKINS\n\nСумма: {money(order['price'])}\n\n"
        f"Получатель: {CARD_OWNER}\nРеквизиты: {CARD_NUMBER}\n\n"
        "После оплаты отправь фотографию чека.",
        reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("summary_"))
async def summary_again(c: CallbackQuery):
    order = get_order(c.data.removeprefix("summary_"))
    if not order or order["user_id"] != c.from_user.id:
        await c.answer("Заказ не найден",show_alert=True); return
    await show_order_summary(c.message,order)
    await c.answer()

@dp.callback_query(F.data.startswith("receipt_"))
async def ask_receipt(c: CallbackQuery):
    order_id = c.data.removeprefix("receipt_")
    order = get_order(order_id)
    if not order or order["user_id"] != c.from_user.id or order["status"] != "waiting_payment":
        await c.answer("Заказ недоступен",show_alert=True); return
    user_data.setdefault(c.from_user.id,{})["awaiting_receipt"] = order_id
    await c.message.answer(tr(c.from_user.id, "receipt"))
    await c.answer()

@dp.message(F.photo)
async def receive_photo(message: Message, bot: Bot):
    uid = message.from_user.id
    state = user_data.get(uid,{})
    if state.get("awaiting_description"):
        refs = state.setdefault("reference_files", [])
        if len(refs) >= 10:
            await message.answer("Можно добавить не более 10 фото. Нажми «Готово», чтобы продолжить.",
                                 reply_markup=_description_done_keyboard())
            return
        refs.append({"type": "photo", "id": message.photo[-1].file_id})
        if message.caption and not state.get("description"):
            caption = message.caption.strip()
            if len(caption) > 1000:
                refs.pop()
                await message.answer("Подпись слишком длинная. Максимум 1000 символов.")
                return
            state["description"] = caption
        await message.answer(f"Фото добавлено ({len(refs)}/10). Можно отправить ещё или нажать «Готово».",
                             reply_markup=_description_done_keyboard())
        return
    if state.get("awaiting_receipt"):
        order_id = state["awaiting_receipt"]
        order = get_order(order_id)
        if not order or order["user_id"] != uid or order["status"] != "waiting_payment":
            await message.answer("Этот заказ уже обработан."); return
        file_id = message.photo[-1].file_id
        db.execute("UPDATE orders SET status='check_sent',receipt_id=? WHERE id=?",(file_id,order_id))
        db.commit()
        kb = InlineKeyboardBuilder()
        kb.button(text="Подтвердить оплату",callback_data=f"approve_{order_id}")
        kb.button(text="Отклонить оплату",callback_data=f"reject_{order_id}")
        kb.adjust(1)
        for admin_id in {int(ADMIN_ID), *[r["user_id"] for r in db.execute("SELECT user_id FROM admins")]}:
            await bot.send_photo(admin_id, file_id,
                caption=f"Чек по заказу {order_id}\nКлиент ID: {uid}\nСумма: {money(order['price'])}",
                reply_markup=kb.as_markup())
        state.pop("awaiting_receipt",None)
        await message.answer(tr(uid, "receipt_sent"))
        return

    # Artist delivery accepts documents and photos.
    if is_artist(uid) and uid in delivery_buffers:
        delivery_buffers[uid]["files"].append({"type":"photo","id":message.photo[-1].file_id})
        await message.answer("Файл добавлен. Можешь отправить ещё или завершить сдачу.")
        return

    # Support attachments can be sent as a photo to the open ticket.
    if state.get("awaiting_support"):
        ticket = db.execute("SELECT id FROM support_tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",(uid,)).fetchone()
        if not ticket:
            cur=db.execute("INSERT INTO support_tickets(user_id,username) VALUES(?,?)",(uid,message.from_user.username or ""))
            ticket_id=cur.lastrowid
        else: ticket_id=ticket["id"]
        fid=message.photo[-1].file_id
        db.execute("INSERT INTO support_messages(ticket_id,sender_id,file_id,file_type) VALUES(?,?,?,?)",(ticket_id,uid,fid,"photo"))
        db.commit()
        state.pop("awaiting_support",None)
        await message.answer("Файл отправлен в поддержку.")
        await bot.send_message(ADMIN_ID,f"В обращении №{ticket_id} получено фото от клиента ID {uid}.")
        return

@dp.message(F.document)
async def receive_document(message: Message, bot: Bot):
    uid = message.from_user.id
    state = user_data.get(uid, {})
    if state.get("awaiting_receipt"):
        order_id = state["awaiting_receipt"]
        order = get_order(order_id)
        if not order or order["user_id"] != uid or order["status"] != "waiting_payment":
            await message.answer("Этот заказ уже обработан или не найден.")
            return
        file_id = message.document.file_id
        db.execute("UPDATE orders SET status='check_sent',receipt_id=? WHERE id=?", (file_id, order_id))
        db.commit()
        kb = InlineKeyboardBuilder()
        kb.button(text="Подтвердить оплату", callback_data=f"approve_{order_id}")
        kb.button(text="Отклонить оплату", callback_data=f"reject_{order_id}")
        kb.adjust(1)
        for admin_id in {int(ADMIN_ID), *[r["user_id"] for r in db.execute("SELECT user_id FROM admins")]}:
            await bot.send_document(
                admin_id, file_id,
                caption=f"Чек по заказу {order_id}\nКлиент ID: {uid}\nСумма: {money(order['price'])}",
                reply_markup=kb.as_markup()
            )
        state.pop("awaiting_receipt", None)
        await message.answer(tr(uid, "receipt_sent"))
        return
    uid = message.from_user.id
    if is_artist(uid) and uid in delivery_buffers:
        delivery_buffers[uid]["files"].append({"type":"document","id":message.document.file_id})
        await message.answer("Файл добавлен. Можешь отправить ещё или завершить сдачу.")
        return
    state = user_data.get(uid,{})
    if state.get("awaiting_support"):
        ticket=db.execute("SELECT id FROM support_tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",(uid,)).fetchone()
        if not ticket:
            cur=db.execute("INSERT INTO support_tickets(user_id,username) VALUES(?,?)",(uid,message.from_user.username or ""))
            ticket_id=cur.lastrowid
        else: ticket_id=ticket["id"]
        db.execute("INSERT INTO support_messages(ticket_id,sender_id,file_id,file_type) VALUES(?,?,?,?)",
                   (ticket_id,uid,message.document.file_id,"document"))
        db.commit(); state.pop("awaiting_support",None)
        await message.answer("Файл отправлен в поддержку.")
        await message.bot.send_message(ADMIN_ID,f"В обращении №{ticket_id} получен документ от клиента ID {uid}.")

# =========================================================
# OWNER: APPROVE PAYMENT / ORDERS
# =========================================================
async def process_payment_check(c: CallbackQuery, bot: Bot, status: str):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа",show_alert=True); return
    order_id = c.data.split("_",1)[1]
    order = get_order(order_id)
    if not order or order["status"] != "check_sent":
        await c.answer("Чек уже обработан или заказ не найден",show_alert=True); return
    if status == "paid":
        # A promo code is consumed only after payment is approved.
        if order["promo_code"]:
            cur=db.execute("UPDATE promo_codes SET used_by=?,used_order_id=? WHERE code=? AND used_by IS NULL",
                           (order["user_id"],order_id,order["promo_code"]))
            if cur.rowcount == 0:
                await c.answer("Промокод уже использован. Проверь заказ вручную.",show_alert=True); return
        db.execute("UPDATE orders SET status='paid',paid_at=? WHERE id=?",
                   (datetime.utcnow().isoformat(),order_id))
        db.commit()
        artist_id = await assign_artist(order_id)
        await bot.send_message(order["user_id"],
            f"Оплата подтверждена! Заказ №{order_id} принят в работу.")
        if artist_id:
            await bot.send_message(artist_id,
                f"Тебе назначен новый заказ №{order_id}.\n\n{order_details(get_order(order_id))}")
        else:
            await notify_admins(bot, f"ВНИМАНИЕ: заказ {order_id} оплачен, но активных художников нет.")
    else:
        set_status(order_id,"rejected")
        await bot.send_message(order["user_id"],
            f"Чек по заказу №{order_id} не удалось подтвердить. Свяжись с поддержкой через /help.")
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Готово")

@dp.callback_query(F.data.startswith("approve_"))
async def approve(c: CallbackQuery, bot: Bot):
    await process_payment_check(c,bot,"paid")

@dp.callback_query(F.data.startswith("reject_"))
async def reject(c: CallbackQuery, bot: Bot):
    await process_payment_check(c,bot,"rejected")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    total = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    waiting = db.execute("SELECT COUNT(*) FROM orders WHERE status='waiting_payment'").fetchone()[0]
    checks = db.execute("SELECT COUNT(*) FROM orders WHERE status='check_sent'").fetchone()[0]
    paid = db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('paid','delivered','revision_requested','confirmed')").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM orders WHERE status='confirmed'").fetchone()[0]
    artists = db.execute("SELECT COUNT(*) FROM artists WHERE active=1").fetchone()[0]
    tickets = db.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0]
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    revenue = db.execute("SELECT COALESCE(SUM(price),0) FROM orders WHERE status IN ('paid','delivered','revision_requested','confirmed')").fetchone()[0]
    text = (
        "📊 СТАТИСТИКА POLUS SKINS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Пользователей: {users}\n"
        f"📦 Всего заказов: {total}\n"
        f"⏳ Ожидают оплаты: {waiting}\n"
        f"🧾 Чеков на проверку: {checks}\n"
        f"💰 Оплаченных/в работе: {paid}\n"
        f"✅ Завершённых: {done}\n"
        f"🎨 Активных художников: {artists}\n"
        f"💬 Открытых обращений: {tickets}\n"
        f"💵 Сумма оплаченных заказов: {money(revenue)}"
    )
    await c.message.edit_text(text, reply_markup=admin_menu())
    await c.answer()

@dp.callback_query(F.data == "admin_checks")
async def admin_checks(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    orders = db.execute(
        "SELECT * FROM orders WHERE status='check_sent' ORDER BY created_at ASC LIMIT 50"
    ).fetchall()
    if not orders:
        await c.message.edit_text("🧾 Чеков, ожидающих проверки, нет.", reply_markup=admin_menu())
    else:
        await c.message.edit_text(
            "🧾 ЧЕКИ НА ПРОВЕРКУ\nВыбери заказ:",
            reply_markup=render_order_list(orders, "admin_order")
        )
    await c.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа",show_alert=True); return
    await c.message.edit_text("Панель владельца POLUS SKINS",reply_markup=admin_menu())
    await c.answer()

def render_order_list(orders, prefix):
    kb=InlineKeyboardBuilder()
    for o in orders:
        kb.button(text=f"{o['id']} · {o['product']} · {o['status']}",callback_data=f"{prefix}_{o['id']}")
    kb.button(text="Назад",callback_data="admin_panel" if prefix.startswith("admin") else "artist_panel")
    kb.adjust(1)
    return kb.as_markup()

@dp.callback_query(F.data.in_({"admin_orders","admin_paid"}))
async def admin_orders(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа",show_alert=True); return
    if c.data=="admin_paid":
        orders=db.execute("SELECT * FROM orders WHERE status IN ('paid','delivered','revision_requested') ORDER BY created_at DESC LIMIT 50").fetchall()
        title="Оплаченные и выполняемые заказы:"
    else:
        orders=db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50").fetchall()
        title="Последние заказы:"
    if not orders:
        await c.message.edit_text("Заказов пока нет.",reply_markup=admin_menu())
    else:
        await c.message.edit_text(title,reply_markup=render_order_list(orders,"admin_order"))
    await c.answer()

@dp.callback_query(F.data.startswith("admin_order_"))
async def admin_order_detail(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа",show_alert=True); return
    oid=c.data.removeprefix("admin_order_")
    o=get_order(oid)
    if not o: await c.answer("Заказ не найден",show_alert=True); return
    kb=InlineKeyboardBuilder()
    if o["status"] == "check_sent":
        kb.button(text="✅ Подтвердить оплату", callback_data=f"approve_{oid}")
        kb.button(text="❌ Отклонить чек", callback_data=f"reject_{oid}")
    if o["status"] in ("paid","revision_requested"):
        kb.button(text="Сдать заказ",callback_data=f"artist_deliver_{oid}")
    if o["status"]=="delivered":
        kb.button(text="Ожидать подтверждения",callback_data=f"admin_wait_{oid}")
        kb.button(text="✅ Закрыть заказ вручную",callback_data=f"admin_close_{oid}")
    kb.button(text="Назначить художника",callback_data=f"assign_pick_{oid}")
    kb.button(text="К списку",callback_data="admin_paid")
    kb.adjust(1)
    await c.message.edit_text(order_details(o),reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("admin_wait_"))
async def admin_wait(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    oid=c.data.removeprefix("admin_wait_")
    db.execute("UPDATE orders SET status='delivered' WHERE id=? AND status='delivered'",(oid,))
    db.commit()
    await c.answer("Заказ остаётся на ожидании подтверждения.")


@dp.callback_query(F.data.startswith("admin_close_"))
async def admin_close_order(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    oid = c.data.removeprefix("admin_close_")
    db.execute("BEGIN IMMEDIATE")
    order = get_order(oid)
    if not order:
        db.rollback()
        await c.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "delivered":
        db.rollback()
        await c.answer("Закрыть вручную можно только готовый заказ, ожидающий подтверждения.", show_alert=True)
        return

    cur = db.execute(
        "UPDATE orders SET status='confirmed', confirmed_at=? WHERE id=? AND status='delivered'",
        (datetime.utcnow().isoformat(), oid)
    )
    if cur.rowcount == 0:
        db.rollback()
        await c.answer("Заказ уже обработан.", show_alert=True)
        return

    artist_id = order["assigned_artist_id"]
    credited = False
    if artist_id:
        exists = db.execute("SELECT 1 FROM balance_ledger WHERE order_id=?", (oid,)).fetchone()
        if not exists:
            amount = int(order["price"])
            db.execute("UPDATE artists SET balance=balance+? WHERE user_id=?", (amount, artist_id))
            db.execute("INSERT INTO balance_ledger(artist_id,order_id,amount) VALUES(?,?,?)",
                       (artist_id, oid, amount))
            credited = True
    db.commit()

    await c.message.edit_text(
        order_details(get_order(oid)) + "\\n\\n✅ Заказ закрыт вручную администратором.",
        reply_markup=admin_menu()
    )
    try:
        await bot.send_message(order["user_id"],
                               f"Заказ №{oid} закрыт администратором: работа отмечена как завершённая.")
    except Exception:
        logging.exception("Could not notify customer about manual order closure: %s", oid)
    if credited:
        await notify_admins(bot, f"Заказ №{oid} закрыт вручную. Художнику {artist_id} начислено {money(order['price'])}.")
    else:
        await notify_admins(bot, f"Заказ №{oid} закрыт вручную администратором.")
    await c.answer("Заказ закрыт без подтверждения клиента.")

# =========================================================
# ARTISTS / AUTOMATIC ASSIGNMENT / DELIVERY
# =========================================================
@dp.callback_query(F.data == "artists_list")
async def artists_list(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    artists=db.execute("SELECT * FROM artists ORDER BY active DESC,added_at").fetchall()
    kb=InlineKeyboardBuilder()
    for a in artists:
        label="🟢" if a["active"] else "⚪"
        kb.button(text=f"{label} @{a['username'] or a['user_id']} · {money(a['balance'])}",
                  callback_data=f"artist_manage_{a['user_id']}")
    kb.button(text="Выдать роль",callback_data="artist_add")
    kb.button(text="Назад",callback_data="admin_panel")
    kb.adjust(1)
    await c.message.edit_text("Художники:",reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data == "artist_add")
async def artist_add(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    user_data.setdefault(c.from_user.id,{})["awaiting_artist_username"]=True
    await c.message.answer("Введи username художника без @. Он должен предварительно запустить бота командой /start.")
    await c.answer()

@dp.callback_query(F.data.startswith("artist_manage_"))
async def artist_manage(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    uid=int(c.data.removeprefix("artist_manage_"))
    a=db.execute("SELECT * FROM artists WHERE user_id=?",(uid,)).fetchone()
    if not a: await c.answer("Художник не найден",show_alert=True); return
    kb=InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить баланс", callback_data=f"balance_edit_{uid}")
    kb.button(text="Удалить роль" if a["active"] else "Вернуть роль",callback_data=f"artist_toggle_{uid}")
    kb.button(text="К списку",callback_data="artists_list")
    kb.adjust(1)
    await c.message.edit_text(f"Художник @{a['username'] or uid}\nID: {uid}\nБаланс: {money(a['balance'])}\nСтатус: {'активен' if a['active'] else 'отключён'}",reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("artist_toggle_"))
async def artist_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    uid=int(c.data.removeprefix("artist_toggle_"))
    db.execute("UPDATE artists SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE user_id=?",(uid,))
    db.commit()
    artists=db.execute("SELECT * FROM artists ORDER BY active DESC,added_at").fetchall()
    kb=InlineKeyboardBuilder()
    for a in artists:
        label="🟢" if a["active"] else "⚪"
        kb.button(text=f"{label} @{a['username'] or a['user_id']} · {money(a['balance'])}",
                  callback_data=f"artist_manage_{a['user_id']}")
    kb.button(text="Выдать роль",callback_data="artist_add")
    kb.button(text="Назад",callback_data="admin_panel")
    kb.adjust(1)
    await c.message.edit_text("Художники и роли:",reply_markup=kb.as_markup())
    await c.answer("Статус изменён")

@dp.callback_query(F.data == "artist_panel")
async def artist_panel(c: CallbackQuery):
    if not is_artist(c.from_user.id):
        await c.answer("Нет доступа",show_alert=True); return
    await c.message.edit_text("Кабинет художника",reply_markup=artist_menu())
    await c.answer()

@dp.callback_query(F.data == "artist_orders")
async def artist_orders(c: CallbackQuery):
    if not is_artist(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    orders=db.execute("SELECT * FROM orders WHERE assigned_artist_id=? AND status IN ('paid','revision_requested','delivered') ORDER BY created_at DESC",(c.from_user.id,)).fetchall()
    if not orders:
        await c.message.edit_text("Назначенных заказов пока нет.",reply_markup=artist_menu())
    else:
        await c.message.edit_text("Твои заказы:",reply_markup=render_order_list(orders,"artist_order"))
    await c.answer()

@dp.callback_query(F.data.startswith("artist_order_"))
async def artist_order_detail(c: CallbackQuery):
    if not is_artist(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    oid=c.data.removeprefix("artist_order_")
    o=get_order(oid)
    if not o or o["assigned_artist_id"]!=c.from_user.id:
        await c.answer("Это не твой заказ",show_alert=True); return
    kb=InlineKeyboardBuilder()
    if o["status"] in ("paid","revision_requested"):
        kb.button(text="Сдать заказ",callback_data=f"artist_deliver_{oid}")
    kb.button(text="К списку",callback_data="artist_orders")
    kb.adjust(1)
    await c.message.edit_text(order_details(o),reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("assign_pick_"))
async def assign_pick(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    oid=c.data.removeprefix("assign_pick_")
    artists=db.execute("SELECT * FROM artists WHERE active=1").fetchall()
    kb=InlineKeyboardBuilder()
    for a in artists:
        kb.button(text=f"@{a['username'] or a['user_id']}",callback_data=f"assign_to_{oid}_{a['user_id']}")
    kb.button(text="Назад",callback_data=f"admin_order_{oid}")
    kb.adjust(1)
    await c.message.edit_text("Выбери художника:",reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("assign_to_"))
async def assign_to(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    parts=c.data.split("_")
    artist_id=int(parts[-1]); oid="_".join(parts[2:-1])
    db.execute("UPDATE orders SET assigned_artist_id=? WHERE id=?",(artist_id,oid)); db.commit()
    await bot.send_message(artist_id,f"Тебе назначен заказ №{oid}.\n\n{order_details(get_order(oid))}")
    await c.answer("Назначено")
    await admin_order_detail_from_callback(c,oid)

async def admin_order_detail_from_callback(c,oid):
    o=get_order(oid)
    kb=InlineKeyboardBuilder()
    if o["status"] in ("paid","revision_requested"): kb.button(text="Сдать заказ",callback_data=f"artist_deliver_{oid}")
    kb.button(text="Назначить художника",callback_data=f"assign_pick_{oid}")
    kb.button(text="К списку",callback_data="admin_paid")
    kb.adjust(1)
    await c.message.edit_text(order_details(o),reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("artist_deliver_"))
async def begin_delivery(c: CallbackQuery):
    oid=c.data.removeprefix("artist_deliver_")
    o=get_order(oid)
    if not o or not (is_admin(c.from_user.id) or (is_artist(c.from_user.id) and o["assigned_artist_id"]==c.from_user.id)):
        await c.answer("Нет доступа к этому заказу",show_alert=True); return
    if o["status"] not in ("paid","revision_requested"):
        await c.answer("Заказ нельзя сдать в текущем статусе",show_alert=True); return
    delivery_buffers[c.from_user.id]={"order_id":oid,"files":[]}
    kb=InlineKeyboardBuilder()
    kb.button(text="Завершить сдачу",callback_data=f"delivery_finish_{oid}")
    await c.message.answer("Отправь файлы заказа (документы или фотографии). Когда закончишь, нажми «Завершить сдачу».",reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("delivery_finish_"))
async def finish_delivery(c: CallbackQuery, bot: Bot):
    oid=c.data.removeprefix("delivery_finish_")
    buf=delivery_buffers.get(c.from_user.id)
    o=get_order(oid)
    if not buf or buf["order_id"]!=oid or not o:
        await c.answer("Сессия сдачи не найдена",show_alert=True); return
    if not (is_admin(c.from_user.id) or (is_artist(c.from_user.id) and o["assigned_artist_id"]==c.from_user.id)):
        await c.answer("Нет доступа",show_alert=True); return
    if not buf["files"]:
        await c.answer("Сначала отправь хотя бы один файл",show_alert=True); return
    db.execute("UPDATE orders SET status='delivered',delivery_files=? WHERE id=?",
               (json.dumps(buf["files"]),oid)); db.commit()
    delivery_buffers.pop(c.from_user.id,None)
    kb=InlineKeyboardBuilder()
    kb.button(text="Внести правки",callback_data=f"revision_{oid}")
    kb.button(text="Подтвердить заказ",callback_data=f"confirm_{oid}")
    kb.adjust(1)
    await bot.send_message(o["user_id"],
        f"Твой заказ №{oid} выполнен! Проверь файлы. Если нужны изменения — запроси правки, либо подтверди заказ.",
        reply_markup=kb.as_markup())
    for item in buf["files"]:
        if item["type"]=="photo":
            await bot.send_photo(o["user_id"],item["id"])
        else:
            await bot.send_document(o["user_id"],item["id"])
    await c.message.answer("Файлы отправлены клиенту. Заказ ожидает его подтверждения или правок.")
    await c.answer("Сдано")

@dp.callback_query(F.data.startswith("revision_"))
async def request_revision(c: CallbackQuery):
    oid=c.data.removeprefix("revision_")
    o=get_order(oid)
    if not o or o["user_id"]!=c.from_user.id or o["status"]!="delivered":
        await c.answer("Заказ недоступен",show_alert=True); return
    user_data.setdefault(c.from_user.id,{})["awaiting_revision"]=oid
    await c.message.answer("Опиши, какие правки нужно внести, одним сообщением.")
    await c.answer()

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order(c: CallbackQuery, bot: Bot):
    oid=c.data.removeprefix("confirm_")
    o=get_order(oid)
    if not o or o["user_id"]!=c.from_user.id or o["status"]!="delivered":
        await c.answer("Заказ недоступен",show_alert=True); return
    artist_id=o["assigned_artist_id"]
    if not artist_id:
        await c.answer("У заказа не назначен художник. Напиши в поддержку.",show_alert=True); return
    # 100% of the amount actually paid, i.e. after promo discount.
    amount=int(o["price"])
    cur=db.execute("UPDATE orders SET status='confirmed',confirmed_at=? WHERE id=? AND status='delivered'",
                   (datetime.utcnow().isoformat(),oid))
    if cur.rowcount==0:
        await c.answer("Заказ уже подтверждён",show_alert=True); return
    db.execute("UPDATE artists SET balance=balance+? WHERE user_id=?",(amount,artist_id))
    db.execute("INSERT INTO balance_ledger(artist_id,order_id,amount) VALUES(?,?,?)",(artist_id,oid,amount))
    db.commit()
    await c.message.edit_text(f"Заказ №{oid} подтверждён. Спасибо за покупку!")
    await bot.send_message(artist_id,f"Клиент подтвердил заказ №{oid}. Тебе начислено {money(amount)} (100% оплаченной суммы).")
    await notify_admins(bot, f"Заказ №{oid} подтверждён. Художнику {artist_id} начислено {money(amount)}.")
    await c.answer("Спасибо!")

@dp.callback_query(F.data == "artist_balance")
async def artist_balance(c: CallbackQuery):
    if not is_artist(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    a=db.execute("SELECT balance FROM artists WHERE user_id=?",(c.from_user.id,)).fetchone()
    rows=db.execute("SELECT order_id,amount,created_at FROM balance_ledger WHERE artist_id=? ORDER BY id DESC LIMIT 20",(c.from_user.id,)).fetchall()
    text=f"Твой баланс: {money(a['balance'] if a else 0)}\n\nНачисления:"
    for r in rows: text+=f"\nЗаказ {r['order_id']}: +{money(r['amount'])}"
    await c.message.edit_text(text,reply_markup=artist_menu())
    await c.answer()

@dp.callback_query(F.data == "balances")
async def balances(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    rows=db.execute("SELECT * FROM artists ORDER BY balance DESC").fetchall()
    text="Балансы художников:\n\n"+"\n".join(
        f"@{r['username'] or r['user_id']}: {money(r['balance'])} ({'активен' if r['active'] else 'отключён'})"
        for r in rows) if rows else "Художников пока нет."
    await c.message.edit_text(text,reply_markup=admin_menu())
    await c.answer()

# =========================================================
# BALANCE EDITING, PAYOUT REQUESTS AND ADMIN ROLES
# =========================================================
@dp.callback_query(F.data.startswith("balance_edit_"))
async def balance_edit_start(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    artist_id = int(c.data.removeprefix("balance_edit_"))
    artist = db.execute("SELECT * FROM artists WHERE user_id=?", (artist_id,)).fetchone()
    if not artist:
        await c.answer("Художник не найден", show_alert=True); return
    user_data.setdefault(c.from_user.id, {})["awaiting_balance_edit"] = artist_id
    await c.message.answer(
        f"Текущий баланс @{artist['username'] or artist_id}: {money(artist['balance'])}\n"
        "Отправь новый баланс целым числом в рублях. Для обнуления отправь 0."
    )
    await c.answer()

@dp.callback_query(F.data == "payout_request")
async def payout_request_start(c: CallbackQuery):
    if not is_artist(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    user_data.setdefault(c.from_user.id, {})["awaiting_payout_amount"] = True
    await c.message.answer(
        "Укажи сумму, которую хочешь вывести, в рублях. "
        "После создания заявки администратор свяжется с тобой, чтобы отдельно согласовать способ выплаты. "
        "Не отправляй платёжные реквизиты в этот чат."
    )
    await c.answer()

@dp.callback_query(F.data == "payout_admin_list")
async def payout_admin_list(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    rows = db.execute("SELECT * FROM payout_requests ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC LIMIT 50").fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"№{r['id']} · {get_artist_name(r['artist_id'])} · {money(r['amount'])} · {r['status']}",
                  callback_data=f"payout_view_{r['id']}")
    kb.button(text="⬅️ Назад", callback_data="admin_panel")
    kb.adjust(1)
    await c.message.edit_text("💸 Заявки на вывод:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("payout_view_"))
async def payout_view(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    rid = int(c.data.removeprefix("payout_view_"))
    r = db.execute("SELECT * FROM payout_requests WHERE id=?", (rid,)).fetchone()
    if not r:
        await c.answer("Заявка не найдена", show_alert=True); return
    artist = db.execute("SELECT balance,username FROM artists WHERE user_id=?", (r["artist_id"],)).fetchone()
    kb = InlineKeyboardBuilder()
    if r["status"] == "pending":
        kb.button(text="✉️ Открыть чат с художником", url=f"tg://user?id={r['artist_id']}")
        kb.button(text="✅ Подтвердить выплату", callback_data=f"payout_approve_{rid}")
        kb.button(text="❌ Отказать", callback_data=f"payout_reject_{rid}")
    kb.button(text="⬅️ К заявкам", callback_data="payout_admin_list")
    kb.adjust(1)
    await c.message.edit_text(
        f"Заявка №{rid}\nХудожник: @{(artist['username'] if artist else '') or r['artist_id']}\n"
        f"Telegram ID: {r['artist_id']}\nСумма: {money(r['amount'])}\n"
        f"Баланс: {money(artist['balance'] if artist else 0)}\nСтатус: {r['status']}\n\n"
        "Свяжись с художником через кнопку выше и согласуй реквизиты отдельно. "
        "Подтверждай выплату только после фактического перевода. Реквизиты в боте не сохраняются.",
        reply_markup=kb.as_markup()
    )
    await c.answer()

@dp.callback_query(F.data.startswith("payout_approve_"))
async def payout_approve(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    rid = int(c.data.removeprefix("payout_approve_"))
    r = db.execute("SELECT * FROM payout_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "pending":
        await c.answer("Заявка уже обработана", show_alert=True); return
    db.execute("BEGIN IMMEDIATE")
    artist = db.execute("SELECT balance FROM artists WHERE user_id=?", (r["artist_id"],)).fetchone()
    if not artist or artist["balance"] < r["amount"]:
        db.rollback()
        await c.answer("Недостаточно средств на балансе.", show_alert=True); return
    db.execute("UPDATE artists SET balance=balance-? WHERE user_id=?", (r["amount"], r["artist_id"]))
    db.execute("UPDATE payout_requests SET status='approved',admin_id=?,processed_at=? WHERE id=? AND status='pending'",
               (c.from_user.id, datetime.utcnow().isoformat(), rid))
    db.commit()
    await bot.send_message(r["artist_id"],
        "Администратор отметил заявку как выплаченную.\n"
        f"Сумма выплаты: {money(r['amount'])}.")
    await c.message.edit_text(f"✅ Заявка №{rid} подтверждена. Баланс уменьшен на {money(r['amount'])}.",
                              reply_markup=admin_menu())
    await c.answer("Выплата отмечена")

@dp.callback_query(F.data.startswith("payout_reject_"))
async def payout_reject(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    rid = int(c.data.removeprefix("payout_reject_"))
    r = db.execute("SELECT * FROM payout_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "pending":
        await c.answer("Заявка уже обработана", show_alert=True); return
    db.execute("UPDATE payout_requests SET status='rejected',admin_id=?,processed_at=? WHERE id=? AND status='pending'",
               (c.from_user.id, datetime.utcnow().isoformat(), rid))
    db.commit()
    await bot.send_message(r["artist_id"], f"Заявка на вывод №{rid} отклонена. Баланс не изменён.")
    await c.message.edit_text(f"❌ Заявка №{rid} отклонена. Баланс сохранён.", reply_markup=admin_menu())
    await c.answer("Заявка отклонена")

@dp.callback_query(F.data == "admins_list")
async def admins_list(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Только владелец может управлять администраторами.", show_alert=True); return
    rows = db.execute("SELECT * FROM admins ORDER BY created_at DESC").fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🛡 @{r['username'] or r['user_id']} · снять доступ",
                  callback_data=f"admin_revoke_{r['user_id']}")
    kb.button(text="➕ Выдать роль администратора", callback_data="admin_grant_start")
    kb.button(text="⬅️ Назад", callback_data="admin_panel")
    kb.adjust(1)
    await c.message.edit_text("🛡 Администраторы (управляет только владелец):", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data == "admin_grant_start")
async def admin_grant_start(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Только владелец может выдавать роль администратора.", show_alert=True); return
    user_data.setdefault(c.from_user.id, {})["awaiting_admin_username"] = True
    await c.message.answer("Отправь username без @. Пользователь должен предварительно открыть бота и нажать /start.")
    await c.answer()

@dp.callback_query(F.data.startswith("admin_revoke_"))
async def admin_revoke(c: CallbackQuery, bot: Bot):
    if not is_owner(c.from_user.id):
        await c.answer("Только владелец может снимать роль администратора.", show_alert=True); return
    target = int(c.data.removeprefix("admin_revoke_"))
    db.execute("DELETE FROM admins WHERE user_id=?", (target,))
    db.commit()
    try:
        await bot.send_message(target, "Твой доступ администратора POLUS SKINS отключён.")
    except Exception:
        pass
    await admins_list(c)
    await c.answer("Роль администратора снята")

# =========================================================
# PROMOCODES ADMIN
# =========================================================
@dp.callback_query(F.data == "promo_create")
async def promo_create(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    code=new_promo_code()
    db.execute("INSERT INTO promo_codes(code,discount_percent) VALUES(?,10)",(code,)); db.commit()
    await c.message.answer(f"Создан промокод: {code}\nСкидка: 10%\nОдно использование.")
    await c.answer("Промокод создан")

@dp.callback_query(F.data == "promo_list")
async def promo_list(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    rows=db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 30").fetchall()
    if not rows: text="Промокодов пока нет."
    else:
        text="Промокоды:\n\n"+"\n".join(
            f"{r['code']} — {r['discount_percent']}% — {'использован' if r['used_by'] else ('активен' if r['active'] else 'выключен')}"
            for r in rows)
    await c.message.edit_text(text,reply_markup=admin_menu())
    await c.answer()

# =========================================================
# SUPPORT
# =========================================================
@dp.callback_query(F.data == "support_new")
async def support_new(c: CallbackQuery):
    user_data.setdefault(c.from_user.id,{})["awaiting_support"]=True
    await c.message.answer("Опиши свою проблему сообщением. Можно также приложить фото или документ.")
    await c.answer()

@dp.callback_query(F.data == "support_list")
async def support_list(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    rows=db.execute("SELECT * FROM support_tickets ORDER BY status='open' DESC,id DESC LIMIT 40").fetchall()
    kb=InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"№{r['id']} · @{r['username'] or r['user_id']} · {r['status']}",
                  callback_data=f"support_ticket_{r['id']}")
    kb.button(text="Назад",callback_data="admin_panel")
    kb.adjust(1)
    await c.message.edit_text("Обращения поддержки:",reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("support_ticket_"))
async def support_ticket_open(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    tid=int(c.data.removeprefix("support_ticket_"))
    ticket=db.execute("SELECT * FROM support_tickets WHERE id=?",(tid,)).fetchone()
    if not ticket: await c.answer("Обращение не найдено",show_alert=True); return
    msgs=db.execute("SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id",(tid,)).fetchall()
    text=f"Обращение №{tid}\nКлиент ID: {ticket['user_id']}\nСтатус: {ticket['status']}\n"
    for m in msgs[-15:]:
        who="Администратор" if is_admin(m["sender_id"]) else "Клиент"
        text+=f"\n{who}: {m['text'] or '[вложение]'}"
    kb=InlineKeyboardBuilder()
    kb.button(text="Ответить",callback_data=f"support_reply_{tid}")
    kb.button(text="Закрыть обращение",callback_data=f"support_close_{tid}")
    kb.button(text="К обращениям",callback_data="support_list")
    kb.adjust(1)
    await c.message.edit_text(text[:3900],reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("support_reply_"))
async def support_reply(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    tid=int(c.data.removeprefix("support_reply_"))
    support_reply_ticket[c.from_user.id]=tid
    user_data.setdefault(c.from_user.id,{})["awaiting_support_reply"]=True
    await c.message.answer(f"Напиши ответ для обращения №{tid}.")
    await c.answer()

@dp.callback_query(F.data.startswith("support_close_"))
async def support_close(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Нет доступа",show_alert=True); return
    tid=int(c.data.removeprefix("support_close_"))
    db.execute("UPDATE support_tickets SET status='closed' WHERE id=?",(tid,)); db.commit()
    await c.answer("Обращение закрыто")
    await support_list(c)

# =========================================================
# MAIN MENU
# =========================================================
@dp.callback_query(F.data == "main")
async def go_main(c: CallbackQuery):
    user_data.pop(c.from_user.id,None)
    await c.message.edit_text("POLUS SKINS\n\nВыбери услугу:",reply_markup=main_menu(c.from_user.id))
    await c.answer()

# =========================================================
# STARTUP
# =========================================================
async def main():
    if BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
        raise ValueError("Укажи BOT_TOKEN в настройках.")
    if ADMIN_ID == 123456789:
        raise ValueError("Укажи Telegram ID владельца в ADMIN_ID.")
    bot=Bot(token=BOT_TOKEN)
    try:
        await bot.set_my_commands([
            BotCommand(command="start",description="Главное меню"),
            BotCommand(command="help",description="Поддержка и помощь"),
            BotCommand(command="language",description="Сменить язык / Change language"),
        ])
        logging.info("POLUS SKINS started")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
