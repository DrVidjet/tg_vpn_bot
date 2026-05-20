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
from zoneinfo import ZoneInfo

# ====================== КОНФИГУРАЦИЯ ======================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'API.conf')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)
PAYMENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pay.json')

API_TOKEN = config.get('DEFAULT', 'API').strip('"')
ADMIN_ID = config.getint('DEFAULT', 'ADMIN_ID')
SUPPORT = config.get('DEFAULT', 'SUPPORT_LINK').strip('"')
PRICE_PER_MONTH = config.getint('DEFAULT', 'PRICE_PER_MONTH')

# Настройки X-UI
XUI_URL = config.get('DEFAULT', 'XUI_URL').strip('"')
XUI_API_TOKEN = config.get('DEFAULT', 'XUI_API_TOKEN').strip('"')

XUI_INBOUND_IDS = [int(x.strip()) for x in config.get('DEFAULT', 'XUI_INBOUND_IDS').split(',')]
XUI_SUB_LINK = config.get('DEFAULT', 'XUI_SUB_LINK').strip('"')
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
renewal_data = {}
user_ids = {}
blocked_users = set()
approved_users = set()
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.lock')
uid_counter = 1




# =======================================================
# ====================== ФУНКЦИИ =======================
# =======================================================



# ====================== Работа с 3x-ui =======================

# Создание клиента
def create_vpn_client(tg_id: int, username: str = None):
    """Создаёт клиента во всех inbound'ах"""
    base_name = f"{username}_{tg_id}" if username and username != "no_username" else str(tg_id)
    expiry_date = datetime.datetime.now() + datetime.timedelta(days=XUI_EXPIRY_DAYS)
    expiry_ms = int(expiry_date.timestamp() * 1000)

    sub_id = str(uuid.uuid4())
    success_count = 0

    for inbound_id in XUI_INBOUND_IDS:
        email = f"{base_name}@inbound{inbound_id}"

        client = {
            "id": str(uuid.uuid4()),
            "email": email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": str(tg_id),
            "subId": sub_id,
            "flow": "xtls-rprx-vision"
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
        return True, "", base_name, expiry_ms, sub_id
    else:
        return False, f"Успешно {success_count}/{len(XUI_INBOUND_IDS)} inbound'ов", base_name, expiry_ms, sub_id

# Продление клиента
def renew_vpn_client(tg_id: int, username: str = None, months: int = 1):
    """Продлевает подписку во всех inbound'ах"""
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)
        extra_days = XUI_EXPIRY_DAYS * months
        user_data = users.get(str(tg_id))
        if not user_data or not user_data.get("email"):
            return False, "Email пользователя не найден в users.json", None, None

        base_email = user_data["email"]          # берём как есть из файла
        success_count = 0
        new_expiry = None

        for inbound_id in XUI_INBOUND_IDS:
            search_email = f"{base_email}@inbound{inbound_id}"

            # Получаем inbound
            r = requests.get(
                f"{XUI_URL}/panel/api/inbounds/get/{inbound_id}",
                headers=headers,
                timeout=15
            )
            if r.status_code != 200 or not r.json().get("success"):
                print(f"⚠️ Не удалось получить inbound {inbound_id}")
                continue

            inbound = r.json().get("obj")
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])

            for client in clients:
                if client.get("email") == search_email:
                    now_ms = int(datetime.datetime.now().timestamp() * 1000)
                    current_expiry = client.get("expiryTime", 0)

                    base_time = current_expiry if current_expiry > now_ms else now_ms
                    new_expiry = base_time + (extra_days * 24 * 60 * 60 * 1000)

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
                        print(f"✅ Inbound {inbound_id} → продлено до {datetime.datetime.fromtimestamp(new_expiry/1000).date()}")
                    break

        if new_expiry and success_count > 0:
            user_data["expiry_time"] = new_expiry
            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)

            return True, "", base_email, new_expiry

        return False, f"Клиент не найден ни в одном inbound (проверено {len(XUI_INBOUND_IDS)})", None, None

    except Exception as e:
        print(f"❌ Ошибка продления: {e}")
        return False, str(e), None, None

# Удаление клиента
def delete_vpn_user_by_uid(uid: int):
    if not os.path.exists("users.json"):
        return False, "users.json not found"

    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)

    # Ищем пользователя по UID
    target_key = None
    for key, info in users.items():
        if info.get("uid") == uid:
            target_key = key
            break

    if not target_key:
        return False, f"Пользователь с UID {uid} не найден"

    base_email = users[target_key].get("email")
    if not base_email:
        return False, "Email не найден"

    deleted = 0
    for inbound_id in XUI_INBOUND_IDS:
        email = f"{base_email}@inbound{inbound_id}"
        try:
            r = requests.post(
                f"{XUI_URL}/panel/api/inbounds/{inbound_id}/delClientByEmail/{email}",
                headers=headers,
                timeout=15
            )
            if r.status_code == 200 and r.json().get("success"):
                deleted += 1
        except Exception as e:
            print(f"Delete error: {e}")

    # Удаляем из файла
    del users[target_key]
    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=4)

    return True, f"Удалено {deleted}/{len(XUI_INBOUND_IDS)} inbound'ов (UID: {uid})"

# Получение статуса серверов
def get_servers_status():
    text = "🖥 Статус серверов\n\n"

    # ==================== CENTRAL ====================
    try:
        r = requests.get(f"{XUI_URL}/panel/api/server/status", headers=headers, timeout=15)
        if r.status_code == 200 and r.json().get("success"):
            s = r.json()["obj"]
            text += (
                "🌐 CENTRAL\n"
                f"CPU: {s.get('cpu', 'N/A')}%\n"
                f"RAM: {round(s['mem']['current']/1024**3, 2)} / "
                f"{round(s['mem']['total']/1024**3, 2)} GB\n"
                f"TCP: {s.get('tcpCount', 'N/A')}\n"
                f"XRAY: {s['xray'].get('state', 'N/A')}\n\n"
            )
    except Exception as e:
        text += f"CENTRAL ERROR: {e}\n\n"

    # ==================== NODES ====================
    try:
        r = requests.get(f"{XUI_URL}/panel/api/nodes/list", headers=headers, timeout=15)
        if r.status_code != 200 or not r.json().get("success"):
            text += "❌ Не удалось получить список нод\n"
            return text

        nodes = r.json()["obj"]
        text += "🖥 NODES\n\n"

        for node in nodes:
            name = node.get("name", "Unknown")
            status = node.get("status", "unknown")

            cpu_pct = node.get("cpuPct")
            mem_pct = node.get("memPct")

            cpu_str = f"{round(cpu_pct, 1)}%" if isinstance(cpu_pct, (int, float)) else "N/A"
            mem_str = f"{round(mem_pct, 1)}%" if isinstance(mem_pct, (int, float)) else "N/A"

            text += (
                f"🖥 {name}\n"
                f"STATUS: {'🟢' if status == 'online' else '🔴'}\n"
                f"CPU: {cpu_str}\n"
                f"MEM: {mem_str}\n"
                f"Latency: {node.get('latencyMs', 'N/A')} ms\n"
                f"Uptime: {node.get('uptimeSecs', 0) // 86400} дней\n\n"
            )
    except Exception as e:
        text += f"NODES ERROR: {e}\n"

    return text

# Запрос онлайн клиентов у сервера
def get_online_clients():

    try:
        r = requests.post(
            f"{XUI_URL}/panel/api/inbounds/onlines",
            headers=headers,
            timeout=15
        )

        if r.status_code == 200 and r.json().get("success"):
            return set(r.json().get("obj", []))

    except Exception as e:
        print(f"Online error: {e}")

    return set()

# Запрос трафика по клиентам
def get_user_traffic(base_email):

    total_up = 0
    total_down = 0

    for inbound_id in XUI_INBOUND_IDS:

        email = f"{base_email}@inbound{inbound_id}"

        try:
            r = requests.get(
                f"{XUI_URL}/panel/api/inbounds/getClientTraffics/{email}",
                headers=headers,
                timeout=15
            )

            if r.status_code == 200 and r.json().get("success"):

                obj = r.json().get("obj", {})

                total_up += obj.get("up", 0)
                total_down += obj.get("down", 0)

        except Exception as e:
            print(f"Traffic error: {e}")

    return total_up, total_down

# Получение названий inbound'ов (remark)
def get_inbound_remarks():
    try:
        r = requests.get(
            f"{XUI_URL}/panel/api/inbounds/list",
            headers=headers,
            timeout=15
        )

        if r.status_code != 200 or not r.json().get("success"):
            return []

        inbounds = r.json().get("obj", [])

        remarks = []

        allowed_inbounds = XUI_INBOUND_IDS[3:]

        for inbound in inbounds:
            inbound_id = inbound.get("id")

            if inbound_id in allowed_inbounds:
                remark = inbound.get("remark", f"Inbound-{inbound_id}")
                remarks.append(remark)


        return remarks

    except Exception as e:
        print(f"Ошибка получения inbound remarks: {e}")
        return []


# ====================== Работа с файлами =======================

# Получение uid пользователя
def get_or_create_uid(tg_id=None):
    """Возвращает UID. Если tg_id=None — создаёт новый для бессрочного пользователя"""
    global uid_counter
    if tg_id is not None and tg_id in user_ids:
        return user_ids[tg_id]

    uid = uid_counter
    uid_counter += 1

    if tg_id is not None:
        user_ids[tg_id] = uid
    return uid

def create_unlimited_by_email(email: str):
    """Создаёт бессрочного клиента и сохраняет в users.json"""
    sub_id = str(uuid.uuid4())
    success_count = 0
    base_name = email.strip().lower()

    for inbound_id in XUI_INBOUND_IDS:
        client_email = f"{base_name}@inbound{inbound_id}"
        client = {
            "id": str(uuid.uuid4()),
            "email": client_email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": 0,          # БЕССРОЧНО
            "enable": True,
            "tgId": "",
            "subId": sub_id,
            "flow": "xtls-rprx-vision"
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
                print(f"✅ [БЕССРОЧНО] Inbound {inbound_id} → {client_email}")
            else:
                print(f"❌ Inbound {inbound_id} ошибка: {r.text}")
        except Exception as e:
            print(f"❌ Inbound {inbound_id} exception: {e}")

    if success_count == len(XUI_INBOUND_IDS):
        uid = get_or_create_uid()
        # Сохраняем в users.json
        save_user_for_unlimited(uid, base_name, sub_id)
        return True, "", base_name, uid, sub_id
    else:
        return False, f"Успешно {success_count}/{len(XUI_INBOUND_IDS)}", base_name, None, sub_id

# Сохранение пользователя через админку
def save_user_for_unlimited(uid: int, email: str, sub_id: str):
    """Сохраняет бессрочного пользователя в users.json"""
    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    # Используем UID как ключ для бессрочных пользователей
    key = f"uid_{uid}"
    data[key] = {
        "uid": uid,
        "email": email,
        "username": "unlimited",
        "status": "approved",
        "expiry_time": 0,
        "sub_id": sub_id,
        "tg_id": None  # явно указываем, что TG нет
    }

    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# Сохранение нового пользователя в файл
def save_user(tg_id, uid, email=None, username=None, status="approved", expiry_time=None, sub_id=None):
    """Сохраняет/обновляет пользователя"""
    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    key = str(tg_id)

    if key in data:
        current = data[key]
        if expiry_time:
            current["expiry_time"] = expiry_time
        if email:
            current["email"] = email
        if username and username != "no_username":
            current["username"] = username
        if sub_id:
            current["sub_id"] = sub_id
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
            "expiry_time": expiry_time,
            "sub_id": sub_id
        }

    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)



# ====================== Работа с tg =======================

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

# Получение суммы оплаты
def get_amount(tg_id):
    data = pending_requests.get(tg_id, {})
    flow = data.get("flow")

    if flow == "renew":
        months = data.get("months", 1)
        return PRICE_PER_MONTH * months

    return PRICE_PER_MONTH

# Контакт поддержки
def support_contact():
    return f"📩 Поддержка\n👤 Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее."

# Функция подгрузки tg пользователей
def load_users():
    global user_ids, blocked_users, approved_users, uid_counter
    if not os.path.exists("users.json"):
        uid_counter = 1
        return

    with open("users.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    max_uid = 0
    for key, info in data.items():
        uid = info.get("uid")
        status = info.get("status")

        # Обычный пользователь (ключ = TG_ID)
        if key.isdigit():
            tg_id = int(key)
            if uid is not None:
                user_ids[tg_id] = uid
                if uid > max_uid:
                    max_uid = uid
            if status == "block":
                blocked_users.add(tg_id)
            elif status == "approved":
                approved_users.add(tg_id)

        # Бессрочный пользователь (ключ вида uid_xxx)
        elif key.startswith("uid_"):
            if uid is not None and uid > max_uid:
                max_uid = uid

    uid_counter = max_uid + 1 if max_uid > 0 else 1

# Отправка видеоинструкции
def send_instruction_video(chat_id):
    """Отправляет видео-инструкцию из папки asset"""
    video_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'asset', 'instruction.mp4')

    if not os.path.exists(video_path):
        bot.send_message(chat_id, "📹 Видео-инструкция временно недоступна.\nПожалуйста, воспользуйтесь текстовой инструкцией по ссылке выше.")
        return

    try:
        with open(video_path, 'rb') as video:
            bot.send_video(
                chat_id,
                video,
                caption="📹 <b>Видео-инструкция по подключению</b>\n\n"
                        "Смотрите, как быстро настроить VPN за 1 минуту.",
                parse_mode="HTML",
                supports_streaming=True
            )
    except Exception as e:
        print(f"Ошибка отправки видео: {e}")
        bot.send_message(chat_id, "Не удалось отправить видео-инструкцию. Используйте текстовую инструкцию выше.")

# Получение tg ссылки на пользователя
def get_user_link(tg_id, username):
    if username and username != "no_username":
        return f"https://t.me/{username}"
    else:
        return f"tg://user?id={tg_id}"

# Информация о подписке
def sub(tg_id, message):
    try:
            if not os.path.exists("users.json"):
                bot.send_message(message.chat.id, "❌ Данные подписки не найдены.")
                return

            with open("users.json", "r", encoding="utf-8") as f:
                users = json.load(f)

            user_data = users.get(str(tg_id))

            if not user_data:
                bot.send_message(message.chat.id, "❌ Подписка не найдена.")
                return

            expiry_ms = user_data.get("expiry_time")
            sub_id = user_data.get("sub_id")

            if not expiry_ms or not sub_id:
                bot.send_message(message.chat.id, "❌ Данные подписки неполные.")
                return

            moscow_tz = ZoneInfo("Europe/Moscow")

            expiry_date = datetime.datetime.fromtimestamp(
                expiry_ms / 1000, tz=moscow_tz
            ).strftime("%d.%m.%Y %H:%M")

            sub_link = f"{XUI_SUB_LINK}/{sub_id}"

            bot.send_message(
                message.chat.id,
                "📦 <b>Ваша подписка</b>\n\n"
                f"🔗 <b>Ссылка:</b>\n"
                f"<code>{sub_link}</code>\n\n"
                f"📅 <b>Действует до:</b> {expiry_date} (МСК)\n\n"
                "❤️ Спасибо, что вы с нами!",
                parse_mode="HTML"
            )

    except Exception as e:
        print(f"Ошибка получения подписки: {e}")
        bot.send_message(
            message.chat.id,
            "❌ Не удалось загрузить информацию о подписке."
        )

# Обработка продления на несколько месяцев
def process_months_input(message, tg_id):
    try:
        months = int(message.text.strip())
        if months <= 0 or months > 12:
            return bot.send_message(message.chat.id, "Введите корректное число месяцев (1–12)")

        renewal_data[tg_id] = months
        pending_requests[tg_id]["months"] = months

        price = PRICE_PER_MONTH * months

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Перейти к оплате", callback_data=f"renew_pay:{tg_id}"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{tg_id}"))

        bot.send_message(
            message.chat.id,
            f"💰 Итоговая сумма: {price}₽\n📅 Месяцев: {months}",
            reply_markup=markup
        )

    except:
        bot.send_message(message.chat.id, "Введите число")

# Функция запроса доступа у админа
def send_admin_request_by_tg_id(tg_id, uid):
    data = pending_requests.get(tg_id, {})
    username = data.get("username") or "no_username"
    user_link = get_user_link(tg_id, username)
    flow = data.get("flow", "new")

    markup = admin_keyboard(tg_id)
    data = pending_requests.get(tg_id, {})
    months = data.get("months", renewal_data.get(tg_id, 1))
    price = PRICE_PER_MONTH * months
    bot.send_message(
        ADMIN_ID,
        f"💰 Новая оплата\n\n"
        f"Пользователь: {user_link}\n"
        f"ID: {uid}\n"
        f"Тип: {'Продление' if flow == 'renew' else 'Новая подписка'}\n"
        f"Сумма: {price}₽ ({months} мес)",
        reply_markup=markup,
    )

# Проверка на админа
def is_admin(user_id):
    return user_id == ADMIN_ID

# Проверка uid
def process_delete_by_uid(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.strip())
    except:
        return bot.send_message(message.chat.id, "❌ Неверный UID. Введите число.")

    success, msg = delete_vpn_user_by_uid(uid)
    if success:
        bot.send_message(message.chat.id, f"✅ {msg}")
    else:
        bot.send_message(message.chat.id, f"❌ {msg}")



# ====================== Кнопки/меню =======================

# Главное меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📦 Моя подписка")
    markup.add("🔄 Продлить подписку")
    markup.add("📩 Поддержка")
    return markup

# Функция выбора способа оплаты
def show_payment_methods(tg_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💳 Номер карты", callback_data=f"choose_method:card:{tg_id}"))
    markup.add(types.InlineKeyboardButton("📱 СБП", callback_data=f"choose_method:sbp:{tg_id}"))
    markup.add(types.InlineKeyboardButton("↩️ Назад", callback_data=f"choose_method:back:{tg_id}"))

    bot.send_message(tg_id, "💰 Выберите способ оплаты:", reply_markup=markup)

# Кнопки админа
def admin_keyboard(tg_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{tg_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отказать", callback_data=f"reject:{tg_id}"))
    markup.add(types.InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block:{tg_id}"))
    return markup

# Главное меню админа
def admin_panel():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add("👥 Пользователи")
    markup.add("➕ Добавить пользователя")
    markup.add("🗑 Удалить пользователя")
    markup.add("📊 Отчет по оплатам")
    markup.add("🖥 Статус серверов")

    return markup



# ====================== Деньги =======================

# Сохранение платежа для отчета
def save_payment(tg_id: int, amount: int, months: int, payment_type: str, username: str = None, email: str = None):

    # Админа игнорим
    if tg_id == ADMIN_ID:
        return

    # Сохраняет платеж в pay.json
    if os.path.exists(PAYMENTS_FILE):
        with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
            payments = json.load(f)
    else:
        payments = []

    now = datetime.datetime.now(ZoneInfo("Europe/Moscow"))

    payment = {
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "tg_id": tg_id,
        "username": username or "unknown",
        "email": email or "—",
        "amount": amount,
        "months": months,
        "type": payment_type,   # "new" или "renew"
        "uid": get_or_create_uid(tg_id) if tg_id else None
    }

    payments.append(payment)

    with open(PAYMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(payments, f, ensure_ascii=False, indent=4)

# Отчет по платежам
def get_monthly_report():
    if not os.path.exists(PAYMENTS_FILE):
        return "Нет платежей за этот месяц."

    with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
        payments = json.load(f)

    now = datetime.datetime.now(ZoneInfo("Europe/Moscow"))
    current_month = now.strftime("%Y-%m")

    month_payments = [p for p in payments if p["date"].startswith(current_month)]

    total_income = sum(p["amount"] for p in month_payments)
    count = len(month_payments)

    text = f"📊 Отчёт за {now.strftime('%B %Y')}\n\n"
    text += f"💰 Всего платежей: {count}\n"
    text += f"💵 Доход за месяц: {total_income} ₽\n\n"
    text += "Последние платежи:\n\n"

    for p in sorted(month_payments, key=lambda x: x["date"], reverse=True)[:15]:
        text += (
            f"{'─' * 18}\n"
            f"• {p.get('email', '—')}\n"
            f"  {p['date'][:16]} | "
            f"{p['amount']}₽ | "
            f"({p['type']})\n"
        )

    return text




# =======================================================
# ====================== ХЭНДЛЕРЫ ======================
# =======================================================



@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id
    loading_msg = bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())

    if tg_id in blocked_users:
        return

    user_data = None

    if os.path.exists("users.json"):
        try:
            with open("users.json", "r", encoding="utf-8") as f:
                users = json.load(f)

            user_data = users.get(str(tg_id))
        except:
            user_data = None

    if user_data and user_data.get("status") == "approved":
        try:
            bot.delete_message(message.chat.id, loading_msg.message_id)
        except:
            pass

        bot.send_message(
            message.chat.id,
            "Добро пожаловать 👇",
            reply_markup=main_menu()
        )
        return

    if tg_id in pending_requests:
        try:
            bot.delete_message(message.chat.id, loading_msg.message_id)
        except:
            pass

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

    # Получаем сервера
    remarks = get_inbound_remarks()

    servers_text = ""
    for remark in remarks:
        servers_text += f"• {remark}\n"

    if not servers_text:
        servers_text = "• 🇸🇪 Стокгольм ×2\n• 🇫🇮 Хельсинки ×1\n• 🇫🇷 Париж ×1\n"

    bot.send_message(
        chat_id,
        "🔥 <b>Добро пожаловать в VidjetVPN</b> 🔥\n\n"
        "🌍 <b>Доступные серверы:</b>\n"
        f"{servers_text}\n"
        "🏴‍☠ Интернет в этих регионах свободный, без ограничений!\n\n"
        "⚡ <b>Преимущества:</b>\n"
        "• Без ограничений по трафику\n"
        "• Высокая скорость соединения\n"
        "• Стабильная работа\n"
        "• 3 устройства на одной подписке\n"
        "• Демократичная цена\n"
        "• Прямая линия с поддержкой\n\n"
        "📦 <b>После оплаты вы получите:</b>\n"
        "• Конфиг для подключения\n"
        "• Пошаговую инструкцию\n\n"
        "💰 <b>Цена:</b>\n"
        f"{PRICE_PER_MONTH}₽ / месяц и использование на 3 устройствах\n\n"
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
        flow = pending_requests[tg_id].get("flow")

        # блокируем только "новую оплату", но не поддержку и не подписку
        if flow == "new":
            bot.send_message(message.chat.id, "🕚 Жду подтверждения оплаты")
            return

    if text == "📦 Моя подписка":
        sub(tg_id, message)
    elif text == "🔄 Продлить подписку":
        uid = get_or_create_uid(tg_id)
        pending_requests[tg_id] = {
            "id": uid,
            "username": message.from_user.username,
            "flow": "renew"
        }

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"📅 На 1 месяц — {PRICE_PER_MONTH}₽", callback_data=f"renew:1:{tg_id}"),
            types.InlineKeyboardButton("📅 На несколько месяцев", callback_data=f"renew:multi:{tg_id}")
        )

        bot.send_message(
            message.chat.id,
            "🔄 Выберите срок продления:",
            reply_markup=markup
        )
    elif text == "📩 Поддержка":
        bot.send_message(message.chat.id, support_contact())

@bot.callback_query_handler(func=lambda call: call.data.startswith("paid:"))
def handle_paid(call):
    tg_id = int(call.data.split(":")[1])

    # Берём данные из основного хранилища
    data = pending_requests.get(tg_id)
    if not data:
        return bot.answer_callback_query(call.id, "Нет активной заявки")

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    flow = data.get("flow", "new")

    uid = get_or_create_uid(tg_id)

    # Обновляем/дополняем данные перед отправкой админу
    pending_requests[tg_id]["id"] = uid
    pending_requests[tg_id]["username"] = data.get("username")
    pending_requests[tg_id]["flow"] = flow

    send_admin_request_by_tg_id(tg_id, uid)

    bot.send_message(
        tg_id,
        f"⏳ Заявка отправлена на проверку оплаты.\n\n"
        "Обычно проверка занимает от 1 до 15 минут."
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("renew:"))
def handle_renew_choice(call):
    _, mode, tg_id_str = call.data.split(":")
    tg_id = int(tg_id_str)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if mode == "1":
        months = 1
        renewal_data[tg_id] = months

        pending_requests[tg_id]["months"] = months

        show_payment_methods(tg_id)
        bot.answer_callback_query(call.id)
        return

    if mode == "multi":
        msg = bot.send_message(call.message.chat.id, "📅 Введите количество месяцев (число):")

        bot.register_next_step_handler(msg, process_months_input, tg_id)
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
    amount = get_amount(tg_id)
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
            caption=f"📱 СБП\n💰 К оплате: <b>{amount}₽</b>\n\n{sbp_url}\n\nПосле оплаты нажмите кнопку ниже.", parse_mode="HTML",
            reply_markup=markup
        )

    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("renew_pay:"))
def handle_renew_pay(call):
    tg_id = int(call.data.split(":")[1])

    months = renewal_data.get(tg_id, 1)
    price = PRICE_PER_MONTH * months

    pending_requests[tg_id]["months"] = months
    pending_requests[tg_id]["amount"] = price

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    show_payment_methods(call.message.chat.id)

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

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel:"))
def handle_cancel(call):
    tg_id = int(call.data.split(":")[1])

    pending_requests.pop(tg_id, None)
    renewal_data.pop(tg_id, None)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    bot.send_message(
        tg_id,
        "❌ Оплата отменена",
        reply_markup=main_menu()
    )

    bot.answer_callback_query(call.id)

# Блокировка текстового спама "Я оплатил"
@bot.message_handler(func=lambda m: m.text and any(phrase in m.text.lower() for phrase in
    ["я оплатил", "я оплатилa", "оплатил", "оплатила", "перевёл", "перевела"]))
def handle_text_paid(message):
    tg_id = message.from_user.id

    if tg_id in pending_requests:
        bot.send_message(
            tg_id,
            "✅ Используйте кнопку «Я оплатил», а не пишите текстом.\n"
            f"Если кнопка исчезла — ожидайте подтверждения оплаты. В среднем она занимает до 15 минут. Если подтверждение не приходит дольше, 👤 Напишите сюда: {SUPPORT}"
        )
    else:
        bot.send_message(tg_id, "Если вы хотите оплатить — нажмите «🔄 Продлить подписку».")

@bot.callback_query_handler(func=lambda call: call.data.startswith(("approve:", "reject:", "block:")))
def admin_actions(call):
    action, tg_id_str = call.data.split(":")
    tg_id = int(tg_id_str)

    if not is_admin(call.from_user.id):
        return
    try:
        bot.delete_message(call.message.chat_id, call.message.message_id)
    except:
        pass
    # Если заявки уже нет
    if tg_id not in pending_requests:
        if action == "approve":
            bot.answer_callback_query(call.id, "Попробуйте нажать кнопку ещё раз")
        else:
            bot.answer_callback_query(call.id, "✅ Уже обработано")
        return

    data = pending_requests.get(tg_id, {})
    flow = data.get("flow", "new")
    username = data.get("username") or "no_username"
    uid = get_or_create_uid(tg_id)

    if action == "approve":
        if flow == "new":
            # === НОВАЯ ПОДПИСКА ===
            success, error_msg, base_name, expiry_ms, sub_id = create_vpn_client(tg_id, username)

            if success:
                approved_users.add(tg_id)
                save_user(tg_id, uid, base_name, username, "approved", expiry_ms, sub_id)
                # === ЗАПИСЬ ПЛАТЕЖА ===
                months = data.get("months", 1)
                amount = PRICE_PER_MONTH * months
                save_payment(tg_id, amount, months, "new", username, base_name)
                sub_link = f"{XUI_SUB_LINK}/{sub_id}"

                bot.send_message(call.message.chat.id, f"✅ Пользователь {tg_id} успешно создан в 3x-ui")

                bot.send_message(
                    tg_id,
                    "📋 <b>Инструкция по подключению:</b>\n\n"
                    "1. Скачайте приложение v2raytun\n"
                    "Android: https://play.google.com/store/apps/details?id=com.v2raytun.android\n"
                    "IOS: https://apps.apple.com/kz/app/v2raytun/id6476628951\n"
                    "Windows, MAC, Linux: https://v2raytun.com/\n"
                    "А так же другие клиенты и инструкции к ним можно посмотреть по этой ссылке:\n https://gist.github.com/kksudo/9e2072b3c60a72040f4e9d6fb9da7e9c\n\n"
                    "2. Для Android и IOS следуйте видеоинструкции ниже. На IOS пропускаем часть с маршрутизацией приложений.\n\n"
                    "3. Пользуемся подключениями:\n 🏴‍☠Устройство-1,2,3\nВнешние сервера:\n🇫🇮HELSINKI, 🇸🇪STOKGOLM и тд\nиспользуем только если основное подключение отвалилось!\n\n"
                    "4. Одно устройство - 1 подключение. Что это значит? Например хотим сидеть с ноутбука и с телефона. На ноутбуке выбираем 🏴‍☠Устройство-1, на телефоне 🏴‍☠Устройство-2. СИДЕТЬ С ДВУХ УСТРОЙСТВ НА ОДНОМ ПОДКЛЮЧЕНИИ НЕЛЬЗЯ!\n\n"
                    f"Если будут вопросы — 👤 Напишите сюда: {SUPPORT}\n⏱ Мы ответим вам как можно скорее.",
                    parse_mode="HTML"
                )

                send_instruction_video(tg_id)

                bot.send_message(tg_id,
                    "🎉 <b>Подписка успешно активирована!</b>\n\n"
                    f"🔗 <b>Ваша ссылка на подписку:</b>\n"
                    f"<code>{sub_link}</code>\n"
                    "Переходить по ссылке не нужно, ее необходимо скопировать и вставить в приложение.\n\n"
                    "🎉 Добро пожаловать в VidjetVPN!",
                    parse_mode="HTML",
                    reply_markup=main_menu()
                )

                pending_requests.pop(tg_id, None)

            else:
                # Ошибка создания
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Ошибка создания пользователя {tg_id} в 3x-ui\n\n"
                    f"Ошибка: {error_msg}\n\n"
                    f"Попробуйте одобрить ещё раз.",
                    reply_markup=admin_keyboard(tg_id)
                )
                bot.send_message(
                    tg_id,
                    f"⏳ Ваша заявка обрабатывается.\n\n"
                    f"👤 При возникновении вопросов — Напишите сюда: {SUPPORT}"
                )

        else:
            # === ПРОДЛЕНИЕ ===
            months = pending_requests.get(tg_id, {}).get("months", 1)
            success, error_msg, email, expiry_ms = renew_vpn_client(tg_id, username, months)
            if success:
                approved_users.add(tg_id)
                save_user(tg_id, uid, email, username, "approved", expiry_ms)

                # === ЗАПИСЬ ПЛАТЕЖА ===
                amount = PRICE_PER_MONTH * months
                save_payment(tg_id, amount, months, "renew", username, email)

                bot.send_message(call.message.chat.id, f"✅ Продление для {tg_id} успешно")
                sub(tg_id, call.message)
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

        pending_requests[tg_id] = {
            "id": get_or_create_uid(tg_id),
            "username": current_data.get("username"),
            "flow": flow,
            "amount": PRICE_PER_MONTH
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

@bot.message_handler(commands=['admin'])
def admin_handler(message):

    if not is_admin(message.from_user.id):
        return

    bot.send_message(
        message.chat.id,
        "⚙️ Админ-панель",
        reply_markup=admin_panel()
    )

@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and
    m.text == "👥 Пользователи"
)
def show_users(message):
    if not is_admin(message.from_user.id):
        return
    if not os.path.exists("users.json"):
        return bot.send_message(message.chat.id, "users.json not found")

    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)

    online_clients = get_online_clients()
    result = "👥 Пользователи:\n\n"

    for key, info in users.items():
        username = info.get("username", "no_username")
        uid = info.get("uid", "-")
        status = info.get("status", "unknown")
        expiry = info.get("expiry_time", 0)
        email = info.get("email", "—")

        tg_id = key if not key.startswith("uid_") else "— (бессрочно)"

        moscow_tz = ZoneInfo("Europe/Moscow")
        expiry_date = "БЕССРОЧНО" if expiry == 0 else datetime.datetime.fromtimestamp(expiry / 1000, tz=moscow_tz).strftime("%d.%m.%Y %H:%M")

        online = False
        if email:
            for inbound_id in XUI_INBOUND_IDS:
                check_email = f"{email}@inbound{inbound_id}"
                if check_email in online_clients:
                    online = True
                    break

        up, down = get_user_traffic(email)
        up_gb = round(up / (1024**3), 2)
        down_gb = round(down / (1024**3), 2)

        result += (
            f"UID: {uid}\n"
            f"TG_ID: {tg_id}\n"
            f"USER: @{username}\n"
            f"EMAIL: {email}\n"
            f"STATUS: {status}\n"
            f"ONLINE: {'🟢' if online else '🔴'}\n"
            f"UP: {up_gb} GB\n"
            f"DOWN: {down_gb} GB\n"
            f"EXPIRE: {expiry_date}\n"
            f"{'─' * 15}\n\n"
        )

    # Отправляем частями, если слишком длинное сообщение
    for i in range(0, len(result), 4000):
        bot.send_message(message.chat.id, result[i:i+4000])

@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "➕ Добавить пользователя")
def ask_add_unlimited_user(message):
    msg = bot.send_message(
        message.chat.id,
        "➕ Введите **Email** для бессрочного пользователя:\n"
    )
    bot.register_next_step_handler(msg, process_add_unlimited_by_email)


def process_add_unlimited_by_email(message):
    if message.from_user.id != ADMIN_ID:
        return

    email = message.text.strip().lower()

    success, error_msg, base_name, uid, sub_id = create_unlimited_by_email(email)

    if success:
        sub_link = f"{XUI_SUB_LINK}/{sub_id}"
        bot.send_message(
            message.chat.id,
            f"✅ Бессрочный пользователь успешно создан!\n\n"
            f"🆔 UID: <b>{uid}</b>\n"
            f"📧 Email: <code>{base_name}</code>\n"
            f"🔗 Ссылка:\n<code>{sub_link}</code>",
            parse_mode="HTML"
        )
    else:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n{error_msg}")

@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "🗑 Удалить пользователя")
def ask_delete_user(message):
    msg = bot.send_message(
        message.chat.id,
        "🗑 Введите **UID** пользователя для удаления:"
    )
    bot.register_next_step_handler(msg, process_delete_by_uid)

@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and
    m.text == "🖥 Статус серверов"
)
def servers_status(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        get_servers_status()
    )

@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "📊 Отчет по оплатам")
def show_payments_report(message):
    if not is_admin(message.from_user.id):
        return
    report = get_monthly_report()
    bot.send_message(message.chat.id, report)

# ====================== ЗАПУСК ======================
if __name__ == '__main__':
    lock_file = acquire_lock()
    try:
        load_users()
        print("Bot successfully started")
        bot.infinity_polling(skip_pending=True)
    except Exception as e:
        print(f"Fatal error: {e}")
