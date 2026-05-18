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
XUI_INBOUND_ID = config.getint('DEFAULT', 'XUI_INBOUND_ID')
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
    """Создаёт нового клиента"""
    if username and username != "no_username":
        email = f"{username}_{tg_id}@tg.bot"
    else:
        email = f"{tg_id}@tg.bot"

    expiry_date = datetime.datetime.now() + datetime.timedelta(days=XUI_EXPIRY_DAYS)
    expiry_ms = int(expiry_date.timestamp() * 1000)

    client = {
        "id": str(uuid.uuid4()),
        "email": email,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": expiry_ms,
        "enable": True,
        "tgId": str(tg_id),
        "subId": ""
    }

    payload = {
        "id": XUI_INBOUND_ID,
        "settings": json.dumps({"clients": [client]})
    }

    try:
        r = requests.post(
            f"{XUI_URL}/panel/api/inbounds/addClient",
            headers=headers,
            json=payload,
            timeout=15
        )

        if r.status_code == 200:
            result = r.json()
            if result.get("success"):
                print(f"✅ 3x-ui: Клиент {email} создан до {expiry_date.date()}")
                return True, "", email, expiry_ms
            else:
                return False, result.get('msg', 'Unknown error'), email, None
        else:
            return False, f"HTTP {r.status_code}", email, None

    except Exception as e:
        print(f"❌ Ошибка создания клиента: {e}")
        return False, str(e), email, None

# Продление клиента
def renew_vpn_client(tg_id: int, username: str = None):
    """Продлевает подписку существующему клиенту"""
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        user_data = users.get(str(tg_id))
        if not user_data or not user_data.get("email"):
            return False, "Пользователь или email не найден", None, None

        email = user_data["email"]

        # Получаем inbound
        r = requests.get(
            f"{XUI_URL}/panel/api/inbounds/get/{XUI_INBOUND_ID}",
            headers=headers,
            timeout=15
        )

        if r.status_code != 200 or not r.json().get("success"):
            return False, "Не удалось получить inbound", email, None

        inbound = r.json().get("obj")
        settings = json.loads(inbound.get("settings", "{}"))
        clients = settings.get("clients", [])

        new_expiry = None
        for client in clients:
            if client.get("email") == email:
                now_ms = int(datetime.datetime.now().timestamp() * 1000)
                current = client.get("expiryTime", 0)
                base = max(current, now_ms)
                new_expiry = base + (XUI_EXPIRY_DAYS * 24 * 60 * 60 * 1000)
                client["expiryTime"] = new_expiry
                break

        if new_expiry is None:
            return False, "Клиент не найден в inbound", email, None

        # Обновляем inbound
        payload = {"id": XUI_INBOUND_ID, "settings": json.dumps(settings)}
        update = requests.post(
            f"{XUI_URL}/panel/api/inbounds/update/{XUI_INBOUND_ID}",
            headers=headers,
            json=payload,
            timeout=15
        )

        if update.status_code != 200 or not update.json().get("success"):
            return False, "Не удалось обновить inbound", email, None

        # Обновляем users.json
        user_data["expiry_time"] = new_expiry
        with open("users.json", "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=4)

        print(f"✅ Подписка {email} продлена до {datetime.datetime.fromtimestamp(new_expiry/1000).date()}")
        return True, "", email, new_expiry

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
def save_user(tg_id, uid, email=None, username=None, status="approved",
              subscriptions=1, expiry_time=None):
    """Сохраняет/обновляет пользователя"""
    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    key = str(tg_id)

    if key in data:
        # Обновление существующего пользователя
        current = data[key]
        current["subscriptions"] = max(current.get("subscriptions", 1), subscriptions)
        if expiry_time:
            current["expiry_time"] = expiry_time
        if email:
            current["email"] = email
        if username and username != "no_username":
            current["username"] = username
        if status:
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
            "subscriptions": subscriptions,
            "expiry_time": expiry_time
        }

    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# Выбор кол-ва подписок
def ask_subscription_count(chat_id, is_renew=False, current_count=1):
    if is_renew:
        text = f"У вас сейчас {current_count} подписок.\n\nСколько хотите продлить?"
        max_count = current_count
    else:
        text = "Сколько устройств (подписок) хотите приобрести?\n\n1 — 150₽\n2 и более — 150₽ за первую + 100₽ за каждую следующую"
        max_count = 6

    markup = types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for i in range(1, max_count + 1):
        buttons.append(types.InlineKeyboardButton(str(i), callback_data=f"count:{i}:{'renew' if is_renew else 'new'}"))

    markup.add(*buttons)
    bot.send_message(chat_id, text, reply_markup=markup)

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
    markup.add("🛒 Купить ещё подписки")
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
    count = data.get("count", 1)
    amount = data.get("amount", 150)

    markup = admin_keyboard(tg_id)

    bot.send_message(
        ADMIN_ID,
        f"💰 Новая оплата\n\n"
        f"Пользователь: {user_link}\n"
        f"ID: {uid}\n"
        f"Тип: {'Продление' if flow == 'renew' else 'Новая подписка'}\n"
        f"Подписок: {count}\n"
        f"Сумма: {amount}₽",
        reply_markup=markup,
    )

# Функция подсчета стоимости подписок
def calculate_price(count: int, current_count: int = 0):
    if count == 1:
        return 150
    else:
        return 150 + (count - 1) * 100

# Получаем подписки пользователя
def get_user_subscriptions(tg_id):
    if not os.path.exists("users.json"):
        return 0
    with open("users.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(str(tg_id), {}).get("subscriptions", 0)

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
        "150₽ / месяц за первую подписку\n"
        "100₽ / месяц за все дополнительные\n"
        "1 подписка = 1 устройство\n\n"
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
    ask_subscription_count(tg_id, is_renew=False)

@bot.callback_query_handler(func=lambda call: call.data.startswith("count:"))
def handle_count_selection(call):
    _, count_str, mode = call.data.split(":")
    count = int(count_str)
    tg_id = call.message.chat.id

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    amount = calculate_price(count, get_user_subscriptions(tg_id))

    pending_requests[tg_id] = {
        "id": get_or_create_uid(tg_id),
        "username": call.from_user.username if call.from_user else None,
        "flow": "new" if mode == "new" else "renew",
        "count": count,
        "amount": amount
    }

    show_payment_methods(tg_id)
    bot.answer_callback_query(call.id)

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
        current_count = get_user_subscriptions(tg_id)
        if current_count == 0:
            bot.send_message(tg_id, "У вас пока нет активных подписок.")
            return

        uid = get_or_create_uid(tg_id)
        pending_requests[tg_id] = {
            "id": uid,
            "username": message.from_user.username,
            "flow": "renew"
        }
        ask_subscription_count(tg_id, is_renew=True, current_count=current_count)
    elif text == "🛒 Купить ещё подписки":
        if tg_id not in approved_users:
            bot.send_message(tg_id, "Сначала нужно иметь хотя бы одну активную подписку.")
            return

        uid = get_or_create_uid(tg_id)
        pending_requests[tg_id] = {
            "id": uid,
            "username": message.from_user.username,
            "flow": "new"   # считаем как докупку = новая подписка
        }
        ask_subscription_count(tg_id, is_renew=False)
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
    count = data.get("count", 1)
    amount = data.get("amount", 150)

    uid = get_or_create_uid(tg_id)

    # Обновляем/дополняем данные перед отправкой админу
    pending_requests[tg_id] = {
        "id": uid,
        "username": data.get("username"),
        "flow": flow,
        "count": count,
        "amount": amount
    }

    send_admin_request_by_tg_id(tg_id, uid)

    bot.send_message(
        tg_id,
        f"⏳ Заявка отправлена на проверку оплаты.\n"
        f"Сумма: {amount}₽ | Подписок: {count}"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("choose_method:"))
def handle_choose_method(call):
    parts = call.data.split(":")
    action = parts[1]
    tg_id = int(parts[2])

    # Получаем данные из единого хранилища
    data = pending_requests.get(tg_id, {})
    amount = data.get("amount", 150)

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
            f"💰 К оплате: <b>{amount}₽</b>\n\n"
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
            caption=f"📱 СБП\n💰 К оплате: {amount}₽\n\n{sbp_url}\n\nПосле оплаты нажмите кнопку ниже.",
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
    count = pending.get("count", 1)
    username = pending.get("username") or "no_username"
    uid = get_or_create_uid(tg_id)

    if action == "approve":
        if flow == "new":
            success, error_msg, email, expiry_ms = create_vpn_client(tg_id, username)

            if success:
                approved_users.add(tg_id)
                save_user(tg_id, uid, email, username, "approved", count, expiry_ms)

                bot.send_message(call.message.chat.id, f"✅ Пользователь {tg_id} успешно создан в 3x-ui ({count} подписок)")
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
                save_user(tg_id, uid, email, username, "approved", count, expiry_ms)
                bot.send_message(call.message.chat.id, f"✅ Продление для {tg_id} успешно")
                bot.send_message(tg_id, "🔄 Подписка продлена, с возвращением!", reply_markup=main_menu())
                pending_requests.pop(tg_id, None)
            else:
                bot.send_message(call.message.chat.id, f"❌ Ошибка продления {tg_id}\n{error_msg}")

    elif action == "reject":
        current_data = pending_requests.get(tg_id, {})
        count = current_data.get("count", 1)
        flow = current_data.get("flow", "new")
        amount = current_data.get("amount", 150)

        bot.send_message(
            tg_id,
            f"❌ Оплата отклонена.\n\n"
            f"Вы можете попробовать оплатить ещё раз."
        )

        # Восстанавливаем полное состояние
        pending_requests[tg_id] = {
            "id": get_or_create_uid(tg_id),
            "username": current_data.get("username"),
            "flow": flow,
            "count": count,
            "amount": amount
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
