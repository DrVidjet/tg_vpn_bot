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
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💳 Номер карты", callback_data=f"choose_method:card:{chat_id}"))
    markup.add(types.InlineKeyboardButton("📱 СБП", callback_data=f"choose_method:sbp:{chat_id}"))
    markup.add(types.InlineKeyboardButton("↩️ Назад", callback_data=f"choose_method:back:{chat_id}"))

    bot.send_message(chat_id, "💰 Выберите способ оплаты:", reply_markup=markup)


def send_admin_request_by_tg_id(tg_id, internal_id):
    pending = pending_requests.get(tg_id, {})
    username = pending.get("username") or "no_username"
    user_link = get_user_link(tg_id, username)
    flow = pending.get("flow", "new")

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{tg_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отказать", callback_data=f"reject:{tg_id}"))
    markup.add(types.InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block:{tg_id}"))

    bot.send_message(
        ADMIN_ID,
        f"💰 Новая оплата\n\n"
        f"Пользователь: {user_link}\n"
        f"ID: {internal_id}\n"
        f"Тип: {'Продление' if flow == 'renew' else 'Новая подписка'}",
        reply_markup=markup,
    )


# ====================== ХЭНДЛЕРЫ ======================

@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id
    loading_msg = bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())

    if tg_id in blocked_users:
        return
    if tg_id in approved_users:
        bot.send_message(message.chat.id, "Добро пожаловать 👇", reply_markup=main_menu())
        return
    if tg_id in pending_requests:
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

    ask_vpn_offer(message.chat.id, loading_msg.message_id)


def ask_vpn_offer(chat_id, loading_message_id=None):
    if loading_message_id:
        try:
            bot.delete_message(chat_id, loading_message_id)
        except:
            pass
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Да, оформляем", callback_data=f"offer:yes:{chat_id}"),
        types.InlineKeyboardButton("❌ Нет", callback_data=f"offer:no:{chat_id}")
    )
    bot.send_message(
        chat_id,
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("offer:"))
def handle_offer_response(call):
    _, answer, tg_id_str = call.data.split(":")
    tg_id = int(tg_id_str)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if answer == "no":
        bot.send_message(tg_id, "👍 Ок, если передумаете — нажмите /start")
        bot.answer_callback_query(call.id)
        return

    uid = get_or_create_uid(tg_id)
    pending_requests[tg_id] = {
        "id": uid,
        "username": call.from_user.username,
        "flow": "new"
    }
    payment_state[tg_id] = {"flow": "new"}

    bot.answer_callback_query(call.id)
    show_payment_methods(tg_id)

@bot.message_handler(func=lambda m: m.text and m.text.strip() in [
    "📦 Моя подписка", "🔄 Продлить подписку", "📩 Поддержка", "↩️ Назад"
])
def menu_handler(message):
    tg_id = message.from_user.id
    text = message.text.strip()

    if tg_id in blocked_users:
        return

    if text == "↩️ Назад":
        if tg_id in pending_requests:
            ask_vpn_offer(message.chat.id)   # ← исправить здесь
            pending_requests.pop(tg_id, None)
            payment_state.pop(tg_id, None)
        return

    if tg_id in pending_requests and text != "📩 Поддержка":
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

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

@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_method:"))
def handle_choose_method(call):
    parts = call.data.split(":")
    action = parts[1]
    tg_id = int(parts[2])

    if tg_id not in payment_state:
        payment_state[tg_id] = {"flow": "new"}

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action in ["back", "back_to_methods"]:
            # === РАЗЛИЧАЕМ, откуда нажали Назад ===
            if action == "back":
                # Назад с экрана выбора способа оплаты → в самое начало
                payment_state.pop(tg_id, None)
                pending_requests.pop(tg_id, None)
                ask_vpn_offer(tg_id)
            else:
                # Назад с экрана оплаты (карта/сбп) → обратно на выбор способа
                show_payment_methods(tg_id)

            bot.answer_callback_query(call.id, "Возвращаемся")
            return

    # Выбор способа оплаты (card или sbp)
    payment_state[tg_id]["method"] = action

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid:{tg_id}"))
    markup.add(types.InlineKeyboardButton("↩️ Назад", callback_data=f"choose_method:back_to_methods:{tg_id}"))

    if action == "card":
        tcard = config.get("DEFAULT", "TCARD_NUMBER").strip('"')
        scard = config.get("DEFAULT", "SCARD_NUMBER").strip('"')
        acard = config.get("DEFAULT", "ACARD_NUMBER").strip('"')
        bot.send_message(
            tg_id,
            f"💳 Перевод на карту:\n\n"
            f"Т-Банк <code>{tcard}</code>\n\n"
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
            tg_id,
            qr_img,
            caption=f"📱 СБП:\n{sbp_url}\n\nПосле оплаты нажмите кнопку ниже.",
            reply_markup=markup
        )

    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_methods:"))
def back_to_payment_methods(call):
    tg_id = int(call.data.split(":")[1])

    payment_state.pop(tg_id, None)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    show_payment_methods(call.message.chat.id, remove_previous=True)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(("approve:", "reject:", "block:")))
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

        bot.send_message(call.message.chat.id, "✅ Оплата одобрена")

    elif action == "reject":
        bot.send_message(tg_id, f"❌ Оплата отклонена.\n\nВы можете попробовать оплатить ещё раз. Если вы уверены, что оплата прошла, просьба связаться с поддержкой: {SUPPORT}")

        payment_state[tg_id] = {"flow": flow}

        show_payment_methods(tg_id)

        bot.send_message(call.message.chat.id, "❌ Оплата отклонена")

    elif action == "block":
        blocked_users.add(tg_id)
        save_user(tg_id, uid, username, "block")
        bot.send_message(tg_id, f"🚫 Вы заблокированы за спам. Для разблокировки свяжитесь с поддержкой: {SUPPORT}")

        bot.send_message(call.message.chat.id, "🚫 Пользователь заблокирован")

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
