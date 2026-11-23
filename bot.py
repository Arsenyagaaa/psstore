import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
# Адрес Mini App (https). Пока пусто — бот работает только с каталогом в чате.
WEBAPP_BASE = (os.getenv("WEBAPP_URL") or "").strip()
WEBAPP_FILE = (os.getenv("WEBAPP_FILE") or "").strip()  # например lumo.html; пусто = старый дизайн
WEBAPP_URL = (WEBAPP_BASE.rstrip("/") + "/" + WEBAPP_FILE) if (WEBAPP_BASE and WEBAPP_FILE) else WEBAPP_BASE
# Ссылка на Telegram-канал, например https://t.me/mychannel (пусто — кнопки не будет)
CHANNEL_URL = (os.getenv("CHANNEL_URL") or "").strip()
# Username менеджера без @, например manager_name (пусто — кнопки не будет)
MANAGER_USERNAME = (os.getenv("MANAGER_USERNAME") or "").strip().lstrip("@")

BASE_DIR = Path(__file__).parent
# Один общий каталог для бота и для Mini App
CATALOG_PATH = BASE_DIR / "webapp" / "catalog.json"

DEFAULT_WELCOME = (
    "<b>Добро пожаловать в {shop_name}!</b>\n\n"
    "Подписки PlayStation в одном месте:\n\n"
    "• Выбирайте PS Plus Essential, Extra, Deluxe и EA Play\n"
    "• Сроки: 1, 3 и 12 месяцев, цены в рублях\n"
    "• Оформите заказ, и менеджер поможет с оплатой и активацией\n"
    "• Остались вопросы? Нажмите «Поддержка»"
)

dp = Dispatcher()


# ---------- Каталог ----------

def load_catalog() -> dict:
    """Читаем каталог при каждом запросе: правки цен работают без перезапуска.

    Приводим новый формат (tiers/periods) к виду «разделы → товары → варианты»,
    с которым работают экраны чата.
    """
    with CATALOG_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    # Игры и цена нового аккаунта лежат в games.json рядом с catalog.json
    extra: dict = {}
    games_path = CATALOG_PATH.with_name("games.json")
    if games_path.exists():
        try:
            with games_path.open(encoding="utf-8") as f:
                extra = json.load(f)
        except (OSError, ValueError):
            logging.exception("Не удалось прочитать games.json")
    period_titles = {p["id"]: p["title"] for p in raw["periods"]}
    products = []
    for t in raw["tiers"]:
        options = [
            {"id": pid, "title": period_titles[pid], "price": price}
            for pid, price in t["prices"].items()
            if pid in period_titles
        ]
        products.append(
            {
                "id": t["id"],
                "title": t["title"],
                "description": t.get("short", ""),
                "options": options,
            }
        )
    # Игры: отдельный список, в чатовые экраны не попадает, нужен для заказов из корзины
    games = [
        {
            "id": g["id"],
            "title": g["title"],
            "description": g.get("desc", ""),
            "options": [{"id": "g", "title": "Игра", "price": int(g["price"])}],
        }
        for g in extra.get("games", raw.get("games", []))
    ]
    return {
        "shop_name": extra.get("shop_name", raw["shop_name"]),
        "wallet": extra.get("wallet") or raw.get("wallet"),
        "games": games,
        "welcome_text": raw.get("welcome_text") or DEFAULT_WELCOME,
        "currency": raw["currency"],
        "ua_account_fee": int(extra.get("ua_account_fee", raw.get("ua_account_fee", 80))),
        "categories": [{"id": "psplus", "title": "🎮 PlayStation Plus", "products": products}],
    }


def find_category(catalog: dict, cat_id: str) -> dict | None:
    return next((c for c in catalog["categories"] if c["id"] == cat_id), None)


def find_product(category: dict, prod_id: str) -> dict | None:
    return next((p for p in category["products"] if p["id"] == prod_id), None)


def find_option(product: dict, opt_id: str) -> dict | None:
    return next((o for o in product["options"] if o["id"] == opt_id), None)


def money(catalog: dict, value) -> str:
    return f"{value:,}".replace(",", " ") + f" {catalog['currency']}"


# ---------- Экраны чата ----------

def main_menu(catalog: dict) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for cat in catalog["categories"]:
        kb.button(text=cat["title"], callback_data=f"cat:{cat['id']}")
    kb.adjust(1)
    text = f"<b>{html.escape(catalog['shop_name'])}</b>\n\nВыберите раздел:"
    return text, kb.as_markup()


def category_screen(catalog: dict, category: dict) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for p in category["products"]:
        kb.button(text=p["title"], callback_data=f"prod:{category['id']}:{p['id']}")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(1)
    return f"<b>{html.escape(category['title'])}</b>\n\nВыберите товар:", kb.as_markup()


def product_screen(catalog: dict, category: dict, product: dict) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for o in product["options"]:
        kb.button(
            text=f"{o['title']} — {money(catalog, o['price'])}",
            callback_data=f"buy:{category['id']}:{product['id']}:{o['id']}",
        )
    kb.button(text="⬅️ Назад", callback_data=f"cat:{category['id']}")
    kb.adjust(1)
    text = (
        f"<b>{html.escape(product['title'])}</b>\n"
        f"{html.escape(product['description'])}\n\n"
        "Выберите срок:"
    )
    return text, kb.as_markup()


def confirm_screen(
    catalog: dict, category: dict, product: dict, option: dict
) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Оформить заявку",
        callback_data=f"order:{category['id']}:{product['id']}:{option['id']}",
    )
    kb.button(text="⬅️ Назад", callback_data=f"prod:{category['id']}:{product['id']}")
    kb.adjust(1)
    text = (
        "<b>Ваш выбор</b>\n"
        f"{html.escape(product['title'])} — {html.escape(option['title'])}\n"
        f"Цена: <b>{money(catalog, option['price'])}</b>\n\n"
        "Нажмите «Оформить заявку», и мы свяжемся с вами для оплаты."
    )
    return text, kb.as_markup()


async def show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # сообщение не изменилось — игнорируем
    await callback.answer()


ORDER_COUNTER_FILE = BASE_DIR / "order_counter.txt"


def next_order_number() -> int:
    """Сквозной номер заявки: хранится в файле рядом с ботом."""
    try:
        number = int(ORDER_COUNTER_FILE.read_text().strip()) + 1
    except (FileNotFoundError, ValueError):
        number = 1
    try:
        ORDER_COUNTER_FILE.write_text(str(number))
    except OSError:
        logging.exception("Не удалось сохранить номер заявки")
    return number


async def send_admin_order(bot: Bot, user: User, source: str, lines: list[str], total: str) -> None:
    """Шлём администратору заявку (одна или несколько позиций) с номером, временем и кнопкой."""
    if not ADMIN_ID:
        return
    number = next_order_number()
    now = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
    name = html.escape(user.full_name)
    username = f"@{user.username}" if user.username else "нет username"
    text = (
        f"🛒 <b>Новая заявка №{number}</b>\n"
        f"Время: {now} (МСК)\n"
        f"Откуда: {source}\n"
        + "\n".join(lines)
        + f"\nИтого: <b>{total}</b>\n"
        f"Клиент: <a href=\"tg://user?id={user.id}\">{name}</a> ({username})\n"
        f"ID: <code>{user.id}</code>"
    )
    link = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Написать клиенту", url=link)]]
    )
    try:
        try:
            await bot.send_message(ADMIN_ID, text, reply_markup=markup)
        except TelegramBadRequest:
            # Кнопку Telegram может не принять (скрытый профиль): шлём без неё
            await bot.send_message(ADMIN_ID, text)
    except TelegramBadRequest:
        logging.exception("Не удалось отправить заявку администратору (нажмите Start у бота)")


async def notify_admin(
    bot: Bot, user: User, catalog: dict, product: dict, option: dict, source: str
) -> None:
    """Заявка на один товар. Цену берём из каталога бота, а не от клиента."""
    price = money(catalog, option["price"])
    line = f"Товар: {html.escape(product['title'])} — {html.escape(option['title'])}"
    await send_admin_order(bot, user, source, [line], price)


def parse_cart(catalog: dict, raw: str) -> list[tuple[dict, dict, int]] | None:
    """Разбираем корзину из ссылки: c_ex-12m-2_de-3m-1 (тариф-срок-количество).

    Тариф ищем по началу id (ex -> extra); если подходит не ровно один, считаем ссылку ошибочной.
    Цены берём из каталога, а не от клиента.
    """
    products = list(catalog["categories"][0]["products"]) + list(catalog.get("games", []))
    items = []
    for chunk in raw.split("_"):
        bits = chunk.split("-")
        if len(bits) != 3 or not bits[2].isdigit():
            return None
        code, period_id, qty = bits[0], bits[1], int(bits[2])
        if not 1 <= qty <= 9:
            return None
        exact = [p for p in products if p["id"] == code] if code else []
        found = exact if exact else ([p for p in products if p["id"].startswith(code)] if code else [])
        if len(found) != 1:
            return None
        option = find_option(found[0], period_id)
        if not option:
            return None
        items.append((found[0], option, qty))
    return items or None


RECENT_ORDERS: dict[tuple, float] = {}
DUPLICATE_WINDOW = 90  # секунд


def is_duplicate_order(user_id: int, key: str) -> bool:
    """True, если такую же заявку этот клиент уже отправлял в последние 90 секунд."""
    now = time.monotonic()
    for k in [k for k, t in RECENT_ORDERS.items() if now - t > DUPLICATE_WINDOW]:
        del RECENT_ORDERS[k]
    if (user_id, key) in RECENT_ORDERS:
        return True
    RECENT_ORDERS[(user_id, key)] = now
    return False


def fmt_uah(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₴"


async def process_wallet_order(
    message: Message, bot: Bot, uah: int, ua: bool = False, email: str = "", comment: str = ""
) -> None:
    """Пополнение кошелька PSN Украина: сумму в рублях считаем сами по курсу из games.json."""
    catalog = load_catalog()
    w = catalog.get("wallet")
    if not w:
        await message.answer("Пополнение пока недоступно. Напишите менеджеру.")
        return
    if not (int(w["min"]) <= uah <= int(w["max"])):
        await message.answer(f"Сумма пополнения: от {fmt_uah(int(w['min']))} до {fmt_uah(int(w['max']))}.")
        return
    if is_duplicate_order(message.from_user.id, f"wallet|{uah}|{ua}|{email}|{comment}"):
        await message.answer(DUPLICATE_TEXT)
        return
    rub = round(uah * float(w["rate"]))
    fee = catalog["ua_account_fee"] if ua else 0
    total = rub + fee
    lines = [f"• Пополнение кошелька PSN Украина: {fmt_uah(uah)} × {w['rate']} ₽ = {money(catalog, rub)}"]
    if ua:
        lines.append(f"• Новый украинский аккаунт = {money(catalog, fee)}")
        lines.append("Аккаунт: нужно создать украинский")
    elif email:
        lines.append(f"Аккаунт (email): <code>{html.escape(email)}</code>")
    else:
        lines.append("Аккаунт: не указан, уточните в чате")
    if comment:
        lines.append(f"Комментарий: {html.escape(comment)}")
    await send_admin_order(bot, message.from_user, "Mini App (пополнение)", lines, money(catalog, total))
    markup = None
    if MANAGER_USERNAME:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Поддержка", url=f"https://t.me/{MANAGER_USERNAME}")]]
        )
    await message.answer(
        "✅ <b>Заявка принята!</b>\n"
        f"Пополнение кошелька PSN Украина: {fmt_uah(uah)}\n"
        f"Итого: <b>{money(catalog, total)}</b>\n"
        "Менеджер скоро свяжется с вами в Telegram.",
        reply_markup=markup,
    )


DUPLICATE_TEXT = "Эта заявка уже принята, повторно отправлять не нужно. Менеджер скоро свяжется с вами."

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,20}$")


async def process_cart_order(
    message: Message,
    bot: Bot,
    raw: str,
    ua: bool = False,
    email: str = "",
    comment: str = "",
    source: str = "Mini App (корзина)",
) -> None:
    """Общая обработка заказа из корзины: проверка, уведомление админу, ответ клиенту."""
    if is_duplicate_order(message.from_user.id, f"cart|{raw}|{ua}|{email}|{comment}"):
        await message.answer(DUPLICATE_TEXT)
        return
    catalog = load_catalog()
    items = parse_cart(catalog, raw)
    if not items or len(items) > 8:
        await message.answer("Не удалось разобрать корзину. Откройте магазин заново.")
        return
    fee = catalog["ua_account_fee"] if ua else 0
    total = sum(option["price"] * qty for _, option, qty in items) + fee

    admin_lines = [
        f"• {html.escape(p['title'])} — {html.escape(o['title'])} × {qty} = {money(catalog, o['price'] * qty)}"
        for p, o, qty in items
    ]
    if ua:
        admin_lines.append(f"• Новый украинский аккаунт = {money(catalog, fee)}")
        account_line = "Аккаунт: нужно создать украинский"
    elif email:
        account_line = f"Аккаунт (email): <code>{html.escape(email)}</code>"
    else:
        account_line = "Аккаунт: не указан, уточните в чате"
    admin_lines.append(account_line)
    if comment:
        admin_lines.append(f"Комментарий: {html.escape(comment)}")
    await send_admin_order(bot, message.from_user, source, admin_lines, money(catalog, total))

    markup = None
    if MANAGER_USERNAME:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Поддержка", url=f"https://t.me/{MANAGER_USERNAME}")]]
        )
    client_lines = [
        f"• {html.escape(p['title'])} — {html.escape(o['title'])} × {qty}" for p, o, qty in items
    ]
    if ua:
        client_lines.append("• Новый украинский аккаунт")
    await message.answer(
        "✅ <b>Заявка принята!</b>\n"
        + "\n".join(client_lines)
        + f"\nИтого: <b>{money(catalog, total)}</b>\n"
        "Менеджер скоро свяжется с вами в Telegram.",
        reply_markup=markup,
    )


async def handle_cart_order(message: Message, bot: Bot, raw: str) -> None:
    """Заказ из корзины по ссылке /start c_ex-12m-2_de-3m-1[_u] (суффикс _u: нужен новый аккаунт)."""
    ua = raw.endswith("_u")
    if ua:
        raw = raw[:-2]
    await process_cart_order(message, bot, raw, ua=ua)


# ---------- Обработчики ----------

def find_welcome_photo() -> Path | None:
    """Фото для приветствия: положите welcome.jpg (или .png) рядом с bot.py."""
    for name in ("welcome.jpg", "welcome.jpeg", "welcome.png"):
        path = BASE_DIR / name
        if path.is_file():
            return path
    return None


def start_inline_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))])
    else:
        # Пока Mini App не подключён, кнопка открывает каталог в чате
        rows.append([InlineKeyboardButton(text="Открыть магазин", callback_data="open_catalog")])
    if MANAGER_USERNAME:
        rows.append([InlineKeyboardButton(text="Поддержка", url=f"https://t.me/{MANAGER_USERNAME}")])
    if CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="Наш Telegram-канал", url=CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart(deep_link=True))
async def cmd_start_order(message: Message, command: CommandObject, bot: Bot) -> None:
    """Заказ из Mini App приходит как /start o_<тариф>_<срок>, например o_deluxe_3m."""
    args = (command.args or "").strip()
    if args.startswith("c_"):
        await handle_cart_order(message, bot, args[2:])
        return
    if args.startswith("w_"):
        bits = args[2:].split("_")
        if bits[0].isdigit() and len(bits[0]) <= 6 and bits[1:] in ([], ["u"]):
            await process_wallet_order(message, bot, int(bits[0]), ua=bits[1:] == ["u"])
        else:
            await message.answer("Не удалось разобрать заявку. Откройте магазин заново.")
        return
    parts = args.split("_")
    if len(parts) != 3 or parts[0] != "o":
        await cmd_start(message)  # незнакомая ссылка: просто показываем приветствие
        return

    if is_duplicate_order(message.from_user.id, f"one|{args}"):
        await message.answer(DUPLICATE_TEXT)
        return
    catalog = load_catalog()
    product = find_product(catalog["categories"][0], parts[1])
    option = find_option(product, parts[2]) if product else None
    if not option:
        await message.answer("Такого варианта нет в каталоге. Откройте магазин заново.")
        return

    await notify_admin(bot, message.from_user, catalog, product, option, "Mini App")
    markup = None
    if MANAGER_USERNAME:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Поддержка", url=f"https://t.me/{MANAGER_USERNAME}")]]
        )
    await message.answer(
        "✅ <b>Заявка принята!</b>\n"
        f"{html.escape(product['title'])} — {html.escape(option['title'])}, "
        f"{money(catalog, option['price'])}\n"
        "Менеджер скоро свяжется с вами в Telegram.",
        reply_markup=markup,
    )


def shop_keyboard(file: str) -> ReplyKeyboardMarkup:
    url = f"{WEBAPP_BASE.rstrip('/')}/{file}" if file else WEBAPP_BASE
    sep = "&" if "?" in url else "?"
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=f"{url}{sep}kb=1"))]],
        resize_keyboard=True,
        is_persistent=True,
    )


@dp.message(Command("lumo"))
async def cmd_lumo(message: Message) -> None:
    """Только для админа: кнопка магазина с новым дизайном (остальные клиенты ничего не заметят)."""
    if message.from_user.id != ADMIN_ID or not WEBAPP_BASE:
        return
    await message.answer("Новый дизайн LUMO: нажмите кнопку «Открыть магазин» внизу.", reply_markup=shop_keyboard("lumo.html"))


@dp.message(Command("classic"))
async def cmd_classic(message: Message) -> None:
    """Только для админа: вернуть кнопку старого дизайна."""
    if message.from_user.id != ADMIN_ID or not WEBAPP_BASE:
        return
    await message.answer("Старый дизайн: нажмите кнопку «Открыть магазин» внизу.", reply_markup=shop_keyboard("index.html"))


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    catalog = load_catalog()
    text = catalog["welcome_text"].replace("{shop_name}", html.escape(catalog["shop_name"]))
    markup = start_inline_keyboard()
    photo = find_welcome_photo()

    if photo and len(text) <= 1024:  # лимит Telegram на подпись к фото
        await message.answer_photo(FSInputFile(photo), caption=text, reply_markup=markup)
    elif photo:
        await message.answer_photo(FSInputFile(photo))
        await message.answer(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)

    # Кнопка «Открыть магазин» под полем ввода. Только из неё Mini App может передать
    # боту email и комментарий (метку ?kb=1 витрина использует, чтобы это понять).
    if WEBAPP_URL:
        sep = "&" if "?" in WEBAPP_URL else "?"
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=f"{WEBAPP_URL}{sep}kb=1"))]],
            resize_keyboard=True,
            is_persistent=True,
        )
    else:
        keyboard = ReplyKeyboardRemove()
    cleanup = await message.answer("·", reply_markup=keyboard)
    try:
        await cleanup.delete()
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "open_catalog")
async def cb_open_catalog(callback: CallbackQuery) -> None:
    # Приветствие может быть фото, которое нельзя «переписать» в текст, поэтому шлём новое сообщение
    text, markup = main_menu(load_catalog())
    await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@dp.message(F.web_app_data)
async def on_webapp_order(message: Message, bot: Bot) -> None:
    try:
        data = json.loads(message.web_app_data.data)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        await message.answer("Не удалось прочитать заказ. Попробуйте ещё раз.")
        return

    # Пополнение кошелька (поддерживаем оба формата: wallet как dict и wallet как int)
    if "wallet" in data:
        raw_w = data["wallet"]
        if isinstance(raw_w, dict):
            uah = raw_w.get("uah")
            ua = raw_w.get("ua") is True
            email = str(raw_w.get("email") or "").strip()[:254]
            comment = str(raw_w.get("comment") or "").strip()[:300]
        else:
            uah = raw_w
            ua = data.get("ua") is True
            email = str(data.get("email") or "").strip()[:254]
            comment = str(data.get("comment") or "").strip()[:300]
        if isinstance(uah, bool) or not isinstance(uah, int):
            await message.answer("Не удалось прочитать заявку. Попробуйте ещё раз.")
            return
        if not ua and email and not EMAIL_RE.match(email):
            await message.answer("Email аккаунта указан неверно. Откройте магазин и оформите заявку заново.")
            return
        await process_wallet_order(message, bot, uah, ua=ua, email=email, comment=comment)
        return

    # Новый формат: корзина с данными аккаунта
    if "items" in data:
        items = data.get("items")
        if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items):
            await message.answer("Не удалось прочитать заказ. Попробуйте ещё раз.")
            return
        ua = data.get("ua") is True
        email = str(data.get("email") or "").strip()[:254]
        if not ua and email and not EMAIL_RE.match(email):
            await message.answer("Email аккаунта указан неверно. Откройте магазин и оформите заказ заново.")
            return
        comment = str(data.get("comment") or "").strip()[:300]
        await process_cart_order(message, bot, "_".join(items), ua=ua, email=email, comment=comment)
        return

    # Старый формат: один тариф
    try:
        tier_id, period_id = str(data["tier"]), str(data["period"])
    except (KeyError, TypeError):
        await message.answer("Не удалось прочитать заказ. Попробуйте ещё раз.")
        return
    catalog = load_catalog()
    category = catalog["categories"][0]
    product = find_product(category, tier_id)
    option = find_option(product, period_id) if product else None
    if not option:
        await message.answer("Такого варианта нет в каталоге. Откройте магазин заново.")
        return
    await notify_admin(bot, message.from_user, catalog, product, option, "Mini App")
    await message.answer(
        "✅ <b>Заявка принята!</b>\n"
        f"{html.escape(product['title'])} — {html.escape(option['title'])}, "
        f"{money(catalog, option['price'])}\n"
        "Менеджер скоро свяжется с вами в Telegram."
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery) -> None:
    text, markup = main_menu(load_catalog())
    await show(callback, text, markup)


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(callback: CallbackQuery) -> None:
    catalog = load_catalog()
    category = find_category(catalog, callback.data.split(":")[1])
    if not category:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    text, markup = category_screen(catalog, category)
    await show(callback, text, markup)


@dp.callback_query(F.data.startswith("prod:"))
async def cb_product(callback: CallbackQuery) -> None:
    _, cat_id, prod_id = callback.data.split(":")
    catalog = load_catalog()
    category = find_category(catalog, cat_id)
    product = find_product(category, prod_id) if category else None
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    text, markup = product_screen(catalog, category, product)
    await show(callback, text, markup)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery) -> None:
    _, cat_id, prod_id, opt_id = callback.data.split(":")
    catalog = load_catalog()
    category = find_category(catalog, cat_id)
    product = find_product(category, prod_id) if category else None
    option = find_option(product, opt_id) if product else None
    if not option:
        await callback.answer("Вариант не найден", show_alert=True)
        return
    text, markup = confirm_screen(catalog, category, product, option)
    await show(callback, text, markup)


@dp.callback_query(F.data.startswith("order:"))
async def cb_order(callback: CallbackQuery, bot: Bot) -> None:
    _, cat_id, prod_id, opt_id = callback.data.split(":")
    catalog = load_catalog()
    category = find_category(catalog, cat_id)
    product = find_product(category, prod_id) if category else None
    option = find_option(product, opt_id) if product else None
    if not option:
        await callback.answer("Вариант не найден", show_alert=True)
        return

    await notify_admin(bot, callback.from_user, catalog, product, option, "Каталог в чате")

    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 В меню", callback_data="menu")
    await show(
        callback,
        "✅ <b>Заявка принята!</b>\nМенеджер скоро свяжется с вами в Telegram.",
        kb.as_markup(),
    )


# ---------- Запуск ----------

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Проверьте файл .env.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if WEBAPP_URL:
        # Синяя кнопка «Открыть магазин» слева от поля ввода
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            logging.exception("Не удалось настроить кнопку меню")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
