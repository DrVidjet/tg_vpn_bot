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
import requests
import uuid
import datetime

# ====================== КОНФИГУРАЦИЯ ======================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'API.conf')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)

API_TOKEN = config.get('DEFAULT', 'API').strip('"')
ADMIN_ID = config.getint('DEFAULT', 'ADMIN_ID')
SUPPORT = "https://t.me/VidjetVPN"

# Настройки X-UI
XUI_URL = config.get('DEFAULT', 'XUI_URL').strip('"')
XUI_API_TOKEN = config.get('DEFAULT', 'XUI_API_TOKEN').strip('"')

XUI_INBOUND_IDS = [int(x.strip()) for x in config.get('DEFAULT', 'XUI_INBOUND_IDS').split(',')]
XUI_EXPIRY_DAYS = config.getint('DEFAULT', 'XUI_EXPIRY_DAYS', fallback=31)

headers = {
    "Authorization": f"Bearer {XUI_API_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# Инициализируем бота
bot = telebot.TeleBot(API_TOKEN)

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
pending_requests = {}
user_ids = {}
blocked_users = set()
approved_users = set()
LOCK_FILE = "/tmp/tg_vpn_bot.lock"
uid_counter = 1

# ====================== ФУНКЦИИ ======================

# Создание клиента
def create_vpn_client(tg_id: int, username: str = None):
    """Создаёт клиента во всех inbound'ах"""
    base_email = f"{username}_{tg_id}" if username and username != "no_username" else str(tg_id)
    expiry_date = datetime.datetime.now() + datetime.timedelta(days=XUI_EXPIRY_DAYS)
    expiry_ms = int(expiry_date.timestamp() * 1000)

    sub_id = str(uuid.uuid4())  # Один subId на всех inbound'ах

    success_count = 0

    for inbound_id in XUI_INBOUND_IDS:
        email = f"{base_email}@inbound{inbound_id}"

        client = {
            "id": str(uuid.uuid4()),
            "email": email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": str(tg_id),
            "subId": sub_id
        }

        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]})
        }

        try:
            r = requests.post(
                f"{XUI_URL}/panel/api/inbounds/addClient",
                headers=headers,
                json=payload,
                timeout=15
            )
            if r.status_code == 200 and r.json().get("success"):
                success_count += 1
                print(f"✅ Inbound {inbound_id} → {email}")
            else:
                print(f"❌ Inbound {inbound_id} ошибка: {r.text}")
        except Exception as e:
            print(f"❌ Inbound {inbound_id} exception: {e}")

    if success_count == len(XUI_INBOUND_IDS):
        return True, "", f"{base_email}@...", expiry_ms
    else:
        return False, f"Успешно {success_count}/{len(XUI_INBOUND_IDS)} inbound'ов", f"{base_email}@...", expiry_ms

# Продление клиента
def renew_vpn_client(tg_id: int, username: str = None):
    """Продлевает подписку с правильной логикой дат"""
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        user_data = users.get(str(tg_id))
        if not user_data or not user_data.get("email"):
            return False, "Пользователь или email не найден", None, None

        email = user_data["email"]
        success_count = 0
        new_expiry = None

        for inbound_id in XUI_INBOUND_IDS:
            # Получаем inbound
            r = requests.get(
                f"{XUI_URL}/panel/api/inbounds/get/{inbound_id}",
                headers=headers,
                timeout=15
            )
            if r.status_code != 200 or not r.json().get("success"):
                continue

            inbound = r.json().get("obj")
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])

            for client in clients:
                if client.get("email") == email:
                    now_ms = int(datetime.datetime.now().timestamp() * 1000)
                    current_expiry = client.get("expiryTime", 0)

                    # === Основная логика продления ===
                    if current_expiry > now_ms:
                        # Подписка ещё активна — продлеваем от даты окончания
                        base_time = current_expiry
                    else:
                        # Подписка просрочена — продлеваем от сегодня
                        base_time = now_ms

                    new_expiry = base_time + (XUI_EXPIRY_DAYS * 24 * 60 * 60 * 1000)
                    client["expiryTime"] = new_expiry

                    # Обновляем клиента
                    payload = {
                        "id": inbound_id,
                        "settings": json.dumps({"clients": [client]})
                    }

                    update = requests.post(
                        f"{XUI_URL}/panel/api/inbounds/updateClient/{client['id']}",
                        headers=headers,
                        json=payload,
                        timeout=15
                    )

                    if update.status_code == 200 and update.json().get("success"):
                        success_count += 1
                    break

        if new_expiry and success_count > 0:
            user_data["expiry_time"] = new_expiry
            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)

            expiry_date = datetime.datetime.fromtimestamp(new_expiry / 1000)
            print(f"✅ Подписка {email} продлена до {expiry_date.date()}")
            return True, "", email, new_expiry

        return False, "Не удалось продлить ни в одном inbound", None, None

    except Exception as e:
        print(f"❌ Ошибка продления: {e}")
        return False, str(e), None, None

# Генерация QR из ссылки
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

#Предотвращение дублирующих запусков
def acquire_lock():
    lock_file = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Bot already running!")
        sys.exit(1)
    return lock_file

# Функция подгрузки tg пользователей
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

# Выдача uid
def get_or_create_uid(tg_id):
    global uid_counter
    if tg_id in user_ids:
        return user_ids[tg_id]
    uid = uid_counter
    uid_counter += 1
    user_ids[tg_id] = uid
    return uid

# Сохранение нового пользователя в файл
def save_user(tg_id, uid, email=None, username=None, status="approved", expiry_time=None):
    """Сохраняет/обновляет пользователя (1 подписка)"""
    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    key = str(tg_id)

    if key in data:
        # Обновление
        current = data[key]
        if expiry_time:
            current["expiry_time"] = expiry_time
        if email:
            current["email"] = email
        if username and username != "no_username":
            current["username"] = username
        current["status"] = status
    else:
        # Новый пользователь
        if expiry_time is None:
            expiry_time = int((datetime.datetime.now() + datetime.timedelta(days=XUI_EXPIRY_DAYS)).timestamp() * 1000)

        data[key] = {
            "uid": uid,
            "email": email,
            "username": username or "no_username",
            "status": status,
            "expiry_time": expiry_time
        }

    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# Получение tg ссылки на пользователя
def get_user_link(tg_id, username):
    if username and username != "no_username":
        return f"https://t.me/{username}"
    else:
        return f"tg://user?id={tg_id}"

# Контакт поддержки
def support_contact():
    return f"📩 Поддержка\n👤 Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее."

# Главное меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📦 Моя подписка")
    markup.add("🔄 Продлить подписку")
    markup.add("📩 Поддержка")
    return markup

# Функция выбора способа оплаты
def show_payment_methods(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💳 Номер карты", callback_data=f"choose_method:card:{chat_id}"))
    markup.add(types.InlineKeyboardButton("📱 СБП", callback_data=f"choose_method:sbp:{chat_id}"))
    markup.add(types.InlineKeyboardButton("↩️ Назад", callback_data=f"choose_method:back:{chat_id}"))

    bot.send_message(chat_id, "💰 Выберите способ оплаты:", reply_markup=markup)

# Кнопки админа
def admin_keyboard(tg_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{tg_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отказать", callback_data=f"reject:{tg_id}"))
    markup.add(types.InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block:{tg_id}"))
    return markup

# Функция запроса доступа у админа
def send_admin_request_by_tg_id(tg_id, uid):
    data = pending_requests.get(tg_id, {})
    username = data.get("username") or "no_username"
    user_link = get_user_link(tg_id, username)
    flow = data.get("flow", "new")

    markup = admin_keyboard(tg_id)

    bot.send_message(
        ADMIN_ID,
        f"💰 Новая оплата\n\n"
        f"Пользователь: {user_link}\n"
        f"ID: {uid}\n"
        f"Тип: {'Продление' if flow == 'renew' else 'Новая подписка'}\n"
        f"Сумма: 150₽",
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
        "• 🇫🇮 Хельсинки ×1\n"
        "• 🇫🇷 Париж ×1\n\n"
        "🏴‍☠ Интернет в этих регионах свободный, без ограничений!\n\n"
        "⚡ <b>Преимущества:</b>\n"
        "• Без ограничений по трафику\n"
        "• Высокая скорость соединения\n"
        "• Прямая линия с поддержкой\n"
        "• Стабильная работа\n\n"
        "📦 <b>После оплаты вы получите:</b>\n"
        "• Конфиг для подключения\n"
        "• Пошаговую инструкцию\n\n"
        "💰 <b>Цена:</b>\n"
        "150₽ / месяц и использование на 3 устройствах\n"
        "❓ <b>Оформляем?</b>",
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
        bot.send_message(tg_id, "👍 Ок, если передумаете — напишите /start")
        bot.answer_callback_query(call.id)
        return

    uid = get_or_create_uid(tg_id)
    pending_requests[tg_id] = {
        "id": uid,
        "username": call.from_user.username,
        "flow": "new"
    }

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
            ask_vpn_offer(message.chat.id)
            pending_requests.pop(tg_id, None)
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
        show_payment_methods(tg_id)
    elif text == "📩 Поддержка":
        bot.send_message(message.chat.id, support_contact())

@bot.callback_query_handler(func=lambda call: call.data.startswith("paid:"))
def handle_paid(call):
    tg_id = int(call.data.split(":")[1])

    # Берём данные из основного хранилища
    data = pending_requests.get(tg_id)
    if not data:
        return bot.answer_callback_query(call.id, "Нет активной заявки")

    flow = data.get("flow", "new")

    uid = get_or_create_uid(tg_id)

    # Обновляем/дополняем данные перед отправкой админу
    pending_requests[tg_id] = {
        "id": uid,
        "username": data.get("username"),
        "flow": flow
    }

    send_admin_request_by_tg_id(tg_id, uid)

    bot.send_message(
        tg_id,
        f"⏳ Заявка отправлена на проверку оплаты.\n"
        f"Сумма: 150₽"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_method:"))
def handle_choose_method(call):
    parts = call.data.split(":")
    action = parts[1]
    tg_id = int(parts[2])

    # Получаем данные из единого хранилища
    data = pending_requests.get(tg_id, {})

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action in ["back", "back_to_methods"]:
        if action == "back":
            pending_requests.pop(tg_id, None)
            ask_vpn_offer(tg_id)
        else:
            show_payment_methods(tg_id)
        bot.answer_callback_query(call.id, "Возвращаемся")
        return

    # Сохраняем выбранный метод
    if tg_id in pending_requests:
        pending_requests[tg_id]["method"] = action

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid:{tg_id}"))
    markup.add(types.InlineKeyboardButton("↩️ Назад", callback_data=f"choose_method:back_to_methods:{tg_id}"))

    if action == "card":
        tcard = config.get("DEFAULT", "TCARD_NUMBER").strip('"')
        scard = config.get("DEFAULT", "SCARD_NUMBER").strip('"')
        acard = config.get("DEFAULT", "ACARD_NUMBER").strip('"')
        bot.send_message(
            tg_id,
            f"💳 Перевод на карту\n"
            f"💰 К оплате: <b>150₽</b>\n\n"
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
            caption=f"📱 СБП\n💰 К оплате: 150₽\n\n{sbp_url}\n\nПосле оплаты нажмите кнопку ниже.",
            reply_markup=markup
        )

    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_methods:"))
def back_to_payment_methods(call):
    tg_id = int(call.data.split(":")[1])

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

    # Если заявки уже нет — проверяем, возможно это повторное нажатие после ошибки
    if tg_id not in pending_requests:
        # Проверяем, не была ли это ошибка создания
        if action == "approve":
            bot.answer_callback_query(call.id, "Попробуйте нажать кнопку ещё раз")
            return
        else:
            return bot.answer_callback_query(call.id, "✅ Уже обработано")

    pending = pending_requests.get(tg_id, {})
    flow = pending.get("flow", "new")
    username = pending.get("username") or "no_username"
    uid = get_or_create_uid(tg_id)

    if action == "approve":
        if flow == "new":
            success, error_msg, email, expiry_ms = create_vpn_client(tg_id, username)

            if success:
                approved_users.add(tg_id)
                save_user(tg_id, uid, email, username, "approved", expiry_ms)

                bot.send_message(call.message.chat.id, f"✅ Пользователь {tg_id} успешно создан в 3x-ui")
                bot.send_message(tg_id, "🎉 Подписка оплачена, добро пожаловать!", reply_markup=main_menu())

                # Удаляем заявку только после успеха
                pending_requests.pop(tg_id, None)
            else:
                # При ошибке — НЕ удаляем из pending_requests, чтобы можно было повторить
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Ошибка создания пользователя {tg_id} в 3x-ui\n\n"
                    f"Ошибка: {error_msg}\n\n"
                    f"Попробуйте одобрить ещё раз.",
                    reply_markup=admin_keyboard(tg_id)
                )
                bot.send_message(
                    tg_id,
                    f"⏳ Ваша заявка обрабатывается.\n👤  При возникновении вопросов — Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее."
                )
        else:
            # Продление
            success, error_msg, email, expiry_ms = renew_vpn_client(tg_id, username)
            if success:
                approved_users.add(tg_id)
                save_user(tg_id, uid, email, username, "approved", expiry_ms)
                bot.send_message(call.message.chat.id, f"✅ Продление для {tg_id} успешно")
                bot.send_message(tg_id, "🔄 Подписка продлена, с возвращением!", reply_markup=main_menu())
                pending_requests.pop(tg_id, None)
            else:
                bot.send_message(call.message.chat.id, f"❌ Ошибка продления {tg_id}\n{error_msg}")

    elif action == "reject":
        current_data = pending_requests.get(tg_id, {})
        flow = current_data.get("flow", "new")

        bot.send_message(
            tg_id,
            f"❌ Оплата отклонена.\n\n"
            f"Вы можете попробовать оплатить ещё раз."
        )

        # Восстанавливаем полное состояние
        pending_requests[tg_id] = {
            "id": get_or_create_uid(tg_id),
            "username": current_data.get("username"),
            "flow": flow
        }

        show_payment_methods(tg_id)
        bot.send_message(call.message.chat.id, "❌ Оплата отклонена")

    elif action == "block":
        blocked_users.add(tg_id)
        save_user(tg_id, uid, "", username, "block")
        bot.send_message(tg_id, f"🚫 Вы заблокированы за спам.\nДля разблокировки: {SUPPORT}")
        bot.send_message(call.message.chat.id, "🚫 Пользователь заблокирован")
        pending_requests.pop(tg_id, None)

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
