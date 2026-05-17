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

ADMIN_ID = 1336949347  # Telegram ID администратора
SUPPORT = "https://t.me/VidjetVPN"

pending_requests = {}  # user_id -> data
user_ids = {}          # telegram_id -> internal_id
blocked_users = set()
approved_users = set()

lock_file = open("/tmp/tg_bot.lock", "w")

# Получаем путь к конфигу
def get_config_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS  # Папка с бинарником
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))  # Папка с main.py
    return os.path.join(base_path, 'API.conf')

# Загружаем API ключи
def load_api_keys():
    config_path = get_config_path()
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file {config_path} not found.")
    
    config = configparser.ConfigParser()
    config.read(config_path)
    
    try:
        api_token = config.get('DEFAULT', 'API').strip('"')
        return api_token
    except configparser.NoOptionError:
        raise ValueError("one of the 'API' keys is missing in config file.")

# # Обработка исключений и перезапуск бота
# def run_bot():
#     while True:
#         try:
#             print("Bot is successfully started")
#             bot.polling(none_stop=True, interval=1, timeout=20)
#         except telebot.apihelper.ApiTelegramException as e:
#             print(f"Telegram API error: {e}")
#             time.sleep(5)
#         except Exception as e:
#             print(f"An error occurred: {e}")
#             time.sleep(2)

# Получаем API с конфига
API_TOKEN = load_api_keys()
# Инициализируем бота
bot = telebot.TeleBot(API_TOKEN)

# Глобальное состояние
user_data = {}

asked_users = set()

# Обработка пользователей
def load_users():
    if not os.path.exists("users.txt"):
        return

    with open("users.txt", "r") as f:
        for line in f:
            parts = line.strip().split(":")
            if len(parts) != 3:
                continue

            uid, tg_id, status = parts
            tg_id = int(tg_id)

            user_ids[tg_id] = int(uid)

            if status == "block":
                blocked_users.add(tg_id)
            elif status == "approved":
                approved_users.add(tg_id)

def save_user(uid, tg_id, status):
    with open("users.txt", "a") as f:
        f.write(f"{uid}:{tg_id}:{status}\n")

@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id

    markup = types.ReplyKeyboardRemove()
    bot.send_message(chat_id, reply_markup=markup)

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
            help_button = InlineKeyboardButton("Получить подписку", url=f"https://t.me/{bot.get_me().username}?start=help")
            markup.add(help_button)

            # Отправляем сообщение с кнопкой
            bot.send_message(message.chat.id, 
                "Спасибо, что добавили меня в группу!\n"
                "Нажмите кнопку ниже, чтобы перейти в личный чат и начать взаимодействие.",
                reply_markup=markup
            )

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
        "Интернет в этих регионах свободный 🏴‍☠️, а распределение автоматическое, выбирать сервер не нужно!\n\n"
        "⚡ <b>Преимущества:</b>\n"
        "• Без ограничений по трафику\n"
        "• Высокая скорость соединения\n"
        "📦 <b>После оплаты вы получите:</b>\n"
        "• Конфиг для подключения\n"
        "• Пошаговую инструкцию\n\n"
        "💰 <b>Цена:</b> 150₽ / месяц\n\n"
        "❓ <b>Оформляем подписку?</b>",
        parse_mode="HTML",
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
        bot.send_message(message.chat.id, "Жду подтверждения")
        return

    internal_id = max(user_ids.values(), default=0) + 1
    user_ids[tg_id] = internal_id

    pending_requests[tg_id] = {
        "id": internal_id,
        "username": message.from_user.username
    }

    send_admin_request(message, internal_id)

    bot.send_message(message.chat.id, "📨 Заявка отправлена. Ожидайте подтверждения...")

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
        bot.send_message(message.chat.id, "Жду подтверждения")
        return

    text = message.text.strip()

    if text == "📦 Моя подписка":
        bot.send_message(message.chat.id, "📦 test")

    elif text == "🔄 Продлить подписку":
        internal_id = user_ids.get(message.from_user.id, max(user_ids.values(), default=0) + 1)

        pending_requests[message.from_user.id] = {
            "id": internal_id,
            "username": message.from_user.username
        }

        send_admin_request(message, internal_id)
        bot.send_message(message.chat.id, "📨 Запрос на продление отправлен")

    elif text == "📩 Поддержка":
        bot.send_message(message.chat.id, support_contact())

# Уведомление администратору
def send_admin_request(message, internal_id):
    username = message.from_user.username
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

# Кнопки админа
@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    data = call.data
    action, tg_id = data.split(":")
    tg_id = int(tg_id)

    if tg_id not in pending_requests:
        bot.answer_callback_query(call.id, "Уже обработано")
        return

    if action == "approve":
        pending_requests.pop(tg_id, None)
        approved_users.add(tg_id)

        uid = user_ids.get(tg_id, max(user_ids.values(), default=0) + 1)
        user_ids[tg_id] = uid

        save_user(uid, tg_id, "approved")

        bot.send_message(tg_id, "✅ Доступ подтверждён")

        bot.send_message(
            tg_id,
            "Платёж подтверждён! 🎉 Добро пожаловать!",
            reply_markup=main_menu()
        )

    elif action == "deny":
        pending_requests.pop(tg_id, None)

        blocked_users.add(tg_id)

        uid = user_ids.get(tg_id, max(user_ids.values(), default=0) + 1)
        user_ids[tg_id] = uid

        save_user(uid, tg_id, "block")

        bot.send_message(tg_id, "❌ Вам отказано в предоставлении доступа")

        bot.send_message(call.message.chat.id, "Пользователь заблокирован")

# Запуск бота
if __name__ == '__main__':
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Bot already running!")
        sys.exit(1)
    try:
        load_users()
        #run_bot()  # Вызываем функцию для запуска с обработкой ошибок
        print("Bot is successfully started")
        bot.polling(none_stop=True, interval=1, timeout=10)
    except Exception as e:
        print(f"Fatal error occurred: {e}")
