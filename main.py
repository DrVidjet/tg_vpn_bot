import telebot
from telebot import types
from telebot.types import InlineKeyboardButton
import qrcode
import io
import os
import configparser
import sys
import fcntl
import json

# ====================== КОНФИГУРАЦИЯ ======================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'API.conf')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)

API_TOKEN = config.get('DEFAULT', 'API').strip('"')
ADMIN_ID = config.getint('DEFAULT', 'ADMIN_ID')          # ← Добавлено
SUPPORT = "https://t.me/VidjetVPN"

# Инициализируем бота
bot = telebot.TeleBot(API_TOKEN)

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
pending_requests = {}   # tg_id -> {id, username, flow}
user_ids = {}           # tg_id -> internal_uid
blocked_users = set()
approved_users = set()
payment_state = {}

LOCK_FILE = "/tmp/tg_vpn_bot.lock"
uid_counter = 1

# ====================== ФУНКЦИИ ======================

def generate_qr_image(url: str):
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    bio.name = "qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio


def acquire_lock():
    lock_file = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Bot already running!")
        sys.exit(1)
    return lock_file


def load_users():
    global user_ids, blocked_users, approved_users, uid_counter
    if not os.path.exists("users.json"):
        uid_counter = 1
        return

    with open("users.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    max_uid = 0
    for tg_id_str, info in data.items():
        tg_id = int(tg_id_str)
        uid = info.get("uid")
        username = info.get("username")
        status = info.get("status")

        if uid is not None:
            user_ids[tg_id] = uid
            if uid > max_uid:
                max_uid = uid

        if status == "block":
            blocked_users.add(tg_id)
        elif status == "approved":
            approved_users.add(tg_id)

    uid_counter = max_uid + 1 if max_uid > 0 else 1


def get_or_create_uid(tg_id):
    global uid_counter
    if tg_id in user_ids:
        return user_ids[tg_id]
    uid = uid_counter
    uid_counter += 1
    user_ids[tg_id] = uid
    return uid


def save_user(tg_id, uid, username, status):
    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    data[str(tg_id)] = {
        "uid": uid,
        "username": username,
        "status": status
    }

    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def get_user_link(tg_id, username):
    """Возвращает корректную ссылку на пользователя"""
    if username and username != "no_username":
        return f"https://t.me/{username}"
    else:
        return f"tg://user?id={tg_id}"


def support_contact():
    return f"📩 Поддержка\n👤 Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее."


def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📦 Моя подписка")
    markup.add("🔄 Продлить подписку")
    markup.add("📩 Поддержка")
    return markup


def show_payment_methods(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("💳 Номер карты")
    markup.add("📱 СБП")
    bot.send_message(chat_id, "💰 Выберите способ оплаты:", reply_markup=markup)


def send_admin_request_by_tg_id(tg_id, internal_id):
    pending = pending_requests.get(tg_id, {})
    username = pending.get("username") or "no_username"
    user_link = get_user_link(tg_id, username)
    flow = pending.get("flow", "new")

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{tg_id}"),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f"deny:{tg_id}")
    )

    bot.send_message(
        ADMIN_ID,
        f"💰 Новая оплата\n\n"
        f"Пользователь: {user_link}\n"
        f"ID: {internal_id}\n"
        f"Тип: {'Продление' if flow == 'renew' else 'Новая подписка'}",
        reply_markup=markup
    )


# ====================== ХЭНДЛЕРЫ ======================

@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id
    bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())

    if tg_id in blocked_users:
        return
    if tg_id in approved_users:
        bot.send_message(message.chat.id, "Добро пожаловать 👇", reply_markup=main_menu())
        return
    if tg_id in pending_requests:
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

    ask_vpn_offer(message)


def ask_vpn_offer(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("Да", "Нет")
    bot.send_message(
        message.chat.id,
        "🔥 <b>Добро пожаловать в VidjetVPN</b> 🔥\n\n"
        "🌍 <b>Доступные серверы:</b>\n"
        "• 🇸🇪 Стокгольм ×2\n"
        "• 🇫🇮 Хельсинки ×1\n\n"
        "🏴‍☠ Интернет в этих регионах свободный, а распределение автоматическое, выбирать сервер не нужно!\n\n"
        "⚡ <b>Преимущества:</b>\n"
        "• Без ограничений по трафику\n"
        "• Высокая скорость соединения\n\n"
        "• Прямая линия с поддержкой\n\n"
        "📦 <b>После оплаты вы получите:</b>\n"
        "• Конфиг для подключения\n"
        "• Пошаговую инструкцию\n\n"
        "💰 <b>Цена:</b> 150₽ / месяц\n\n"
        "❓ <b>Оформляем подписку?</b>",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.message_handler(func=lambda m: m.text in ["Да", "Нет"])
def handle_offer_response(message):
    tg_id = message.from_user.id
    if tg_id in blocked_users or tg_id in pending_requests:
        return

    if message.text == "Нет":
        bot.send_message(message.chat.id, "👍 Ок, если передумаете — напишите.")
        return

    uid = get_or_create_uid(tg_id)
    pending_requests[tg_id] = {
        "id": uid,
        "username": message.from_user.username,
        "flow": "new"
    }
    payment_state[tg_id] = {"flow": "new"}
    show_payment_methods(message.chat.id)


@bot.message_handler(func=lambda m: m.text and m.text.strip() in [
    "📦 Моя подписка", "🔄 Продлить подписку", "📩 Поддержка"
])
def menu_handler(message):
    tg_id = message.from_user.id
    if tg_id in blocked_users:
        return
    if tg_id in pending_requests:
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

    text = message.text.strip()

    if text == "📦 Моя подписка":
        bot.send_message(message.chat.id, "📦 Ваша подписка (в разработке)")

    elif text == "🔄 Продлить подписку":
        uid = get_or_create_uid(tg_id)
        pending_requests[tg_id] = {
            "id": uid,
            "username": message.from_user.username,
            "flow": "renew"
        }
        payment_state[tg_id] = {"flow": "renew"}
        show_payment_methods(message.chat.id)

    elif text == "📩 Поддержка":
        bot.send_message(message.chat.id, support_contact())


@bot.message_handler(func=lambda m: m.text in ["💳 Номер карты", "📱 СБП"])
def handle_payment_method(message):
    tg_id = message.from_user.id
    if tg_id not in payment_state:
        return

    method = "card" if "карты" in message.text else "sbp"
    payment_state[tg_id]["method"] = method

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Я оплатил", callback_data=f"paid:{tg_id}"))

    if method == "card":
        tcard = config.get("DEFAULT", "TCARD_NUMBER").strip('"')
        scard = config.get("DEFAULT", "SCARD_NUMBER").strip('"')
        acard = config.get("DEFAULT", "ACARD_NUMBER").strip('"')
        bot.send_message(
            message.chat.id,
            f"💳 Перевод на карту:"
            f"\n\nТ-Банк <code>{tcard}</code>\n\n"
            f"Сбер <code>{scard}</code>\n\n"
            f"Альфа <code>{acard}</code>\n\n"
            "После оплаты нажмите кнопку ниже.",
            parse_mode="HTML",
            reply_markup=markup
        )
    else:
        sbp_url = config.get("DEFAULT", "SBP_URL").strip('"')
        qr_img = generate_qr_image(sbp_url)
        bot.send_photo(
            message.chat.id,
            qr_img,
            caption=f"📱 СБП:\n{sbp_url}\n\nПосле оплаты нажмите кнопку ниже.",
            reply_markup=markup
        )

    bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())


@bot.callback_query_handler(func=lambda call: call.data.startswith("paid:"))
def handle_paid(call):
    tg_id = int(call.data.split(":")[1])
    if tg_id not in payment_state:
        return bot.answer_callback_query(call.id, "Нет активной оплаты")

    flow = payment_state[tg_id]["flow"]
    payment_state.pop(tg_id, None)

    internal_id = get_or_create_uid(tg_id)

    pending_requests[tg_id] = {
        "id": internal_id,
        "username": pending_requests.get(tg_id, {}).get("username"),
        "flow": flow
    }

    send_admin_request_by_tg_id(tg_id, internal_id)

    bot.send_message(tg_id, "⏳ Заявка отправлена на проверку оплаты.")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith(("approve:", "deny:")))
def admin_actions(call):
    action, tg_id_str = call.data.split(":")
    tg_id = int(tg_id_str)

    if tg_id not in pending_requests:
        return bot.answer_callback_query(call.id, "✅ Уже обработано")

    pending = pending_requests.pop(tg_id, {})
    flow = pending.get("flow", "new")
    username = pending.get("username") or "no_username"
    uid = get_or_create_uid(tg_id)

    if action == "approve":
        approved_users.add(tg_id)
        save_user(tg_id, uid, username, "approved")

        text = "🔄 Подписка продлена, с возвращением!" if flow == "renew" else "🎉 Подписка оплачена, добро пожаловать!"
        bot.send_message(tg_id, text, reply_markup=main_menu())

    else:  # deny
        blocked_users.add(tg_id)
        save_user(tg_id, uid, username, "block")
        bot.send_message(tg_id, "❌ Вам отказано в предоставлении доступа")
        bot.send_message(call.message.chat.id, "❌ Пользователь заблокирован")

    bot.answer_callback_query(call.id)


# ====================== ЗАПУСК ======================
if __name__ == '__main__':
    lock_file = acquire_lock()
    try:
        load_users()
        print("Bot successfully started")
        bot.infinity_polling(skip_pending=True)
    except Exception as e:
        print(f"Fatal error: {e}")
