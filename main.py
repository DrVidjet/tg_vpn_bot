import telebot
from telebot import types
from telebot.types import InlineKeyboardButton
import qrcode
import io
import os
import configparser
import sys
import time
import fcntl
import json

ADMIN_ID = 1336949347  # Telegram ID администратора
SUPPORT = "https://t.me/VidjetVPN"

pending_requests = {}  # user_id -> data
user_ids = {}          # telegram_id -> internal_id
blocked_users = set()
approved_users = set()

LOCK_FILE = "/tmp/tg_vpn_bot.lock"

# Получаем API с конфига
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'API.conf'
)

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

API_TOKEN = config.get('DEFAULT', 'API').strip('"')

# Инициализируем бота
bot = telebot.TeleBot(API_TOKEN)



#------------------------------------------------------------------------------------------------------------------------------------------------#
                                                                            ## ФУНКЦИИ  ##
#------------------------------------------------------------------------------------------------------------------------------------------------#

# Обработка повторного запуска
def acquire_lock():
    lock_file = open(LOCK_FILE, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Bot already running!")
        sys.exit(1)

    return lock_file

# Обработка пользователей
def load_users():
    global user_ids, blocked_users, approved_users

    if not os.path.exists("users.json"):
        return

    with open("users.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    for tg_id_str, info in data.items():
        tg_id = int(tg_id_str)

        uid = info.get("uid")
        username = info.get("username")
        status = info.get("status")

        if uid is not None:
            user_ids[tg_id] = uid

        if status == "block":
            blocked_users.add(tg_id)
        elif status == "approved":
            approved_users.add(tg_id)

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

def support_contact():
    return (
        "📩 Поддержка\n"
        f"👤 Напишите сюда: {SUPPORT}\n\n"
        "⏱ Мы ответим вам как можно скорее."
    )

# Меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add("📦 Моя подписка")
    markup.add("🔄 Продлить подписку")
    markup.add("📩 Поддержка")

    return markup

# Обработка ожидания ответа администратора
def is_pending(tg_id):
    return tg_id in pending_requests

# Оффер при старте
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
        "📦 <b>После оплаты вы получите:</b>\n"
        "• Конфиг для подключения\n"
        "• Пошаговую инструкцию\n\n"
        "💰 <b>Цена:</b> 150₽ / месяц\n\n"
        "❓ <b>Оформляем подписку?</b>",
        parse_mode="HTML",
        reply_markup=markup
    )

# Уведомление администратору
def send_admin_request(message, internal_id):
    username = call.from_user.username or "no_username"
    user_link = f"https://t.me/{username}" if username else "no_username"

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("Подтвердить", callback_data=f"approve:{message.from_user.id}"),
        types.InlineKeyboardButton("Отказать", callback_data=f"deny:{message.from_user.id}")
    )

    bot.send_message(
        ADMIN_ID,
        f"Пользователь {user_link} с id {internal_id} запросил конфиг, жду подтверждения",
        reply_markup=markup
    )

def next_uid():
    return max([int(x) for x in user_ids.values()], default=0) + 1

#------------------------------------------------------------------------------------------------------------------------------------------------#
                                                                            ## ХЭНДЛЕРЫ  ##
#------------------------------------------------------------------------------------------------------------------------------------------------#
@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id

    bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())

    # если заблокирован — игнор
    if tg_id in blocked_users:
        return

    # если уже approved → сразу меню
    if tg_id in approved_users:
        bot.send_message(
            message.chat.id,
            "Добро пожаловать 👇",
            reply_markup=main_menu()
        )
        return

    # если уже pending → ждём
    if tg_id in pending_requests:
        bot.send_message(message.chat.id, "Жду подтверждения")
        return

    # иначе → оффер
    ask_vpn_offer(message)

# Обработчик приглашения в группу телеграм
@bot.message_handler(content_types=['new_chat_members'])
def handle_new_chat_member(message):
    # Проверяем, добавили ли в группу именно бота
    for new_member in message.new_chat_members:
        if new_member.id == bot.get_me().id:
            # Создаем кнопку "Помочь"
            markup = types.InlineKeyboardMarkup(row_width=1)
            help_button = InlineKeyboardButton("Получить подписку", url=f"https://t.me/{bot.get_me().username}?start=start")
            markup.add(help_button)

            # Отправляем сообщение с кнопкой
            bot.send_message(message.chat.id, 
                "Спасибо, что добавили меня в группу!\n"
                "Нажмите кнопку ниже, чтобы перейти в личный чат и получить конфиг VPN!",
                reply_markup=markup
            )

# Обработка ответа
@bot.message_handler(func=lambda m: m.text in ["Да", "Нет"])
def handle_offer_response(message):
    tg_id = message.from_user.id

    if tg_id in blocked_users:
        return

    if message.text == "Нет":
        bot.send_message(message.chat.id, "👍 Ок, если передумаете — напишите.")
        return

    if tg_id in pending_requests:
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

    internal_id = next_uid()
    user_ids[tg_id] = internal_id

    pending_requests[tg_id] = {
        "id": internal_id,
        "username": message.from_user.username
    }

    send_admin_request(message, internal_id)

    bot.send_message(message.chat.id, "🕚 Заявка отправлена. Ожидайте подтверждения...")

# Обработка меню
@bot.message_handler(func=lambda m: m.text and m.text.strip() in [
    "📦 Моя подписка",
    "🔄 Продлить подписку",
    "📩 Поддержка"
])
def menu_handler(message):
    tg_id = message.from_user.id

    # 1. blocked → молчим
    if tg_id in blocked_users:
        return

    # 2. pending → ждем
    if tg_id in pending_requests:
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

    text = message.text.strip()

    if text == "📦 Моя подписка":
        bot.send_message(message.chat.id, "📦 test")

    elif text == "🔄 Продлить подписку":
        internal_id = next_uid()

        pending_requests[message.from_user.id] = {
            "id": internal_id,
            "username": message.from_user.username
        }

        send_admin_request(message, internal_id)
        bot.send_message(message.chat.id, "📨 Запрос на продление отправлен")

    elif text == "📩 Поддержка":
        bot.send_message(message.chat.id, support_contact())

# Кнопки админа
@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    data = call.data
    action, tg_id = data.split(":")
    tg_id = int(tg_id)

    if tg_id not in pending_requests:
        bot.answer_callback_query(call.id, "✅ Уже обработано")
        return

    if action == "approve":
        pending_requests.pop(tg_id, None)
        approved_users.add(tg_id)

        uid = user_ids.get(tg_id, next_uid())
        user_ids[tg_id] = uid

        username = call.from_user.username or "no_username"
        save_user(tg_id, uid, username, "approved")

        bot.send_message(tg_id, "✅ Доступ подтверждён")

        bot.send_message(
            tg_id,
            "✅ Платёж подтверждён! 🎉 Добро пожаловать!\n\n"
            f"Задать вопросы можно в группе, там же публикуются все новости сервиса: {SUPPORT}",
            reply_markup=main_menu()
        )

    elif action == "deny":
        pending_requests.pop(tg_id, None)

        blocked_users.add(tg_id)

        uid = user_ids.get(tg_id, next_uid())
        user_ids[tg_id] = uid

        username = call.from_user.username
        save_user(tg_id, uid, username, "block")

        bot.send_message(tg_id, "❌ Вам отказано в предоставлении доступа")

        bot.send_message(call.message.chat.id, "❌ Пользователь заблокирован")



#------------------------------------------------------------------------------------------------------------------------------------------------#
                                                                            ## ЗАПУСК БОТА ##
#------------------------------------------------------------------------------------------------------------------------------------------------#

if __name__ == '__main__':
    lock_file = acquire_lock()

    try:
        load_users()

        print("Bot is successfully started")

        bot.infinity_polling(skip_pending=True)

    except Exception as e:
        print(f"Fatal error occurred: {e}")
