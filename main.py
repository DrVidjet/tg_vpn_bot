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
from zoneinfo import ZoneInfo
from yookassa import Configuration, Payment
from datetime import datetime, timedelta
import time
import threading

# ====================== КОНФИГУРАЦИЯ ======================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'API.conf')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)
PAYMENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pay.json')

API_TOKEN = config.get('DEFAULT', 'API').strip('"')
ADMIN_ID = config.getint('DEFAULT', 'ADMIN_ID')
SUPPORT = config.get('DEFAULT', 'SUPPORT_LINK').strip('"')
GRUPP = config.get('DEFAULT', 'GRUPP_LINK').strip('"')
PRICE_PER_MONTH = config.getint('DEFAULT', 'PRICE_PER_MONTH')

# === ЮKassa Telegram Payments ===
PROVIDER_TOKEN = config.get('UKASSA', 'PROVIDER_TOKEN').strip('"')
YOOKASSA_SECRET_KEY = config.get('UKASSA', 'SECRET_KEY').strip('"')
YOOKASSA_SHOP_ID = config.get('UKASSA', 'SHOP_ID').strip('"')

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

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
def create_vpn_client(tg_id: int, username: str = None, months: int = 1):
    base_name = f"{username}_{tg_id}" if username and username != "no_username" else str(tg_id)
    expiry_date = datetime.now() + timedelta(days=XUI_EXPIRY_DAYS * months)
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
                    now_ms = int(datetime.now().timestamp() * 1000)
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
                        print(f"✅ Inbound {inbound_id} → продлено до {datetime.fromtimestamp(new_expiry/1000).date()}")
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

        allowed_inbounds = set(XUI_INBOUND_IDS[3:])

        for inbound in inbounds:
            inbound_id = int(inbound.get("id"))

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
    global uid_counter
    if tg_id is not None and tg_id in user_ids:
        return user_ids[tg_id]

    uid = uid_counter
    uid_counter += 1

    if tg_id is not None:
        user_ids[tg_id] = uid
    return uid

def create_unlimited_by_email(email: str):
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
            expiry_time = int((datetime.now() + timedelta(days=XUI_EXPIRY_DAYS)).timestamp() * 1000)

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

            expiry_date = datetime.fromtimestamp(
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
        if months < 1 or months > 12:
            msg = bot.send_message(tg_id, "❌ Введите число от 1 до 12")
            bot.register_next_step_handler(msg, process_months_input, tg_id)
            return
        send_invoice(tg_id, months)

    except ValueError:
        # Если ввели не число
        msg = bot.send_message(
            tg_id,
            "❌ Пожалуйста, введите **число** (например: 3)",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_months_input, tg_id)

    except Exception as e:
        bot.send_message(tg_id, "❌ Введите корректное число")

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

# Отправляет уведомление админу об успешной оплате
def admin_notify(tg_id: int, username: str, email: str, months: int, amount: int, payment_type: str):
    try:
        bot.send_message(
            ADMIN_ID,
            f"💰 <b>Новая оплата</b>\n\n"
            f"Пользователь: @{username} ({tg_id})\n"
            f"Email: <code>{email}</code>\n"
            f"Тип: {payment_type}\n"
            f"Месяцев: {months}\n"
            f"Сумма: {amount // 100} ₽\n\n"
            f"Время: {datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Не удалось отправить уведомление админу: {e}")

# Уведомления пользователям об истекающих подписках
def check_expiring_subscriptions():
    while True:
        try:
            now = datetime.now(ZoneInfo("Europe/Moscow"))

            # Запускаем проверку только в 12:00 ± 1 минута
            if now.hour == 12 and now.minute < 2:
                if os.path.exists("users.json"):
                    with open("users.json", "r", encoding="utf-8") as f:
                        users = json.load(f)

                    current_time = now.timestamp() * 1000

                    for tg_id_str, data in users.items():
                        if not tg_id_str.isdigit():
                            continue
                        tg_id = int(tg_id_str)
                        expiry = data.get("expiry_time")
                        if not expiry or expiry == 0:  # бессрочные
                            continue

                        days_left = (expiry - current_time) / (86400 * 1000)

                        if 2.8 < days_left < 3.2:   # ~за 3 дня
                            try:
                                bot.send_message(
                                    tg_id,
                                    "⚠️ <b>Ваша подписка заканчивается через 3 дня!</b>\n\n"
                                    "Не забудьте продлить, чтобы не потерять доступ.",
                                    parse_mode="HTML"
                                )
                            except:
                                pass

                        elif -0.2 < days_left < 0.8:   # в день окончания
                            try:
                                bot.send_message(
                                    tg_id,
                                    "❗️ <b>Ваша подписка сегодня заканчивается!</b>\n\n"
                                    "Продлите подписку, чтобы продолжить пользоваться VPN.",
                                    parse_mode="HTML"
                                )
                            except:
                                pass

            time.sleep(60)  # проверяем каждую минуту

        except Exception as e:
            print(f"Ошибка проверки истекающих подписок: {e}")
            time.sleep(300)

def start_expiry_checker():
    thread = threading.Thread(target=check_expiring_subscriptions, daemon=True)
    thread.start()



# ====================== Кнопки/меню =======================

# Главное меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📦 Моя подписка")
    markup.add("🔄 Продлить подписку")
    markup.add("📩 Поддержка")
    return markup

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
    markup.add("🔄 Продлить пользователя")
    markup.add("🗑 Удалить пользователя")
    markup.add("📊 Отчет по оплатам")
    markup.add("🖥 Статус серверов")

    return markup



# ====================== Деньги =======================

# Отправка платежа
def send_invoice(tg_id: int, months: int = 1):
    amount = PRICE_PER_MONTH * months * 100
    payload = f"vpn_{tg_id}_{months}_{int(datetime.now().timestamp())}"

    current_flow = pending_requests.get(tg_id, {}).get("flow", "new")

    pending_requests[tg_id] = {
        "flow": current_flow,
        "months": months,
        "payload": payload
    }

    bot.send_invoice(
        chat_id=tg_id,
        title=f"VidjetVPN — {months} месяц(-ев)",
        description=f"Подписка на VPN-серверы ({months} {'месяц' if months == 1 else 'месяцев'})",
        invoice_payload=payload,
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[types.LabeledPrice(f"Подписка на {months} мес.", amount)],
        need_email=False,
        start_parameter=f"pay_{months}"
    )

# Проверка активной подписки у пользователя
def get_user_status(tg_id):
    if not os.path.exists("users.json"):
        return None
    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)
    return users.get(str(tg_id), None)

# Отчет по платежам
def get_payments_report(days: int = 30):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return "❌ ЮKassa не настроена (отсутствуют Shop ID / Secret Key)"

    try:
        date_from = (datetime.now() - timedelta(days=days)).isoformat() + "Z"

        response = Payment.list({
            "created_at.gte": date_from,
            "status": "succeeded",
            "limit": 100
        })

        payments = response.items if hasattr(response, 'items') else []

        if not payments:
            return f"📊 За последние {days} дней платежей не найдено."

        total_amount = 0
        text = f"📊 <b>Отчёт по платежам ЮKassa</b>\n"
        text += f"Период: последние {days} дней\n"
        text += f"Всего успешных платежей: <b>{len(payments)}</b>\n\n"

        for p in payments:
            amount = float(p.amount.value)
            total_amount += amount
            date = datetime.fromisoformat(p.created_at.replace("Z", "+00:00")).strftime("%d.%m %H:%M")

            payment_type = "💳 Карта"
            if p.payment_method and p.payment_method.type == "sbp":
                payment_type = "📱 СБП"

            text += f"• {date} | {amount} ₽ | {payment_type}\n"

        text += f"\n💰 <b>Итого за период: {round(total_amount, 2)} ₽</b>"

        return text

    except Exception as e:
        return f"❌ Ошибка получения отчёта из ЮKassa:\n{str(e)}"



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

    ask_vpn_offer(message.chat.id)

# Первичный оффер
def ask_vpn_offer(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💳 Оплатить 1 месяц", callback_data="pay:1"))
    markup.add(types.InlineKeyboardButton("💳 Оплатить несколько месяцев", callback_data="pay:multi"))

    bot.send_message(
        chat_id,
        "🔥 <b>Добро пожаловать в VidjetVPN</b> 🔥\n\n"
        "🌍 <b>Доступные серверы:</b>\n"
        "• 🇸🇪 Стокгольм ×2\n"
        "• 🇫🇮 Хельсинки ×1\n"
        "• 🇩🇪 Берлин ×1\n\n"
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay:"))
def handle_pay_choice(call):
    tg_id = call.from_user.id
    action = call.data.split(":")[1]

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action == "1":
        send_invoice(tg_id, months=1)
    elif action == "multi":
        msg = bot.send_message(tg_id, "📅 Введите количество месяцев (1–12):")
        bot.register_next_step_handler(msg, process_months_input, tg_id)

    bot.answer_callback_query(call.id)

@bot.pre_checkout_query_handler(lambda query: True)
def pre_checkout_query(pre_checkout_q):
    bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    tg_id = message.chat.id
    payment = message.successful_payment
    data = pending_requests.get(tg_id, {})

    months = data.get("months", 1)
    flow = data.get("flow", "new")   # "new" или "renew"
    username = message.from_user.username or "no_username"

    bot.send_message(tg_id, "✅ Оплата прошла успешно! Активируем подписку...")

    if flow == "new":
        # === НОВАЯ ПОДПИСКА ===
        success, error_msg, base_name, expiry_ms, sub_id = create_vpn_client(tg_id, username, months)

        if success:
            uid = get_or_create_uid(tg_id)
            save_user(tg_id, uid, base_name, username, "approved", expiry_ms, sub_id)

            sub_link = f"{XUI_SUB_LINK}/{sub_id}"

            # Уведомление админу
            admin_notify(tg_id, username, base_name, months, payment.total_amount, "Новая подписка")

            # Сообщения пользователю
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
                "🎉 Добро пожаловать в VidjetVPN!\n\n"
                "👇 Подписывайтесь на группу, чтобы быть в курсе новостей:\n"
                f"🏴‍☠{GRUPP}",
                parse_mode="HTML",
                reply_markup=main_menu()
            )

        else:
            # Ошибка создания клиента
            bot.send_message(tg_id, f"❌ Ошибка активации подписки. Если вы уверены, что это ошибка,\n 👤 Напишите сюда: {SUPPORT}\n⏱ Мы ответим вам как можно скорее.")
            bot.send_message(
                ADMIN_ID,
                f"⚠️ Ошибка создания пользователя!\n"
                f"TG: @{username} ({tg_id})\n"
                f"Ошибка: {error_msg}"
            )

    else:
        # === ПРОДЛЕНИЕ ===
        success, error_msg, email, expiry_ms = renew_vpn_client(tg_id, username, months)

        if success:
            save_user(tg_id, get_or_create_uid(tg_id), email, username, "approved", expiry_ms)

            # Уведомление админу
            admin_notify(tg_id, username, email, months, payment.total_amount, "Продление")

            bot.send_message(
                tg_id,
                f"🔄 <b>Подписка успешно продлена на {months} месяцев!</b>",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            sub(tg_id, message)   # отправляем актуальную информацию о подписке

        else:
            bot.send_message(tg_id, "❌ Ошибка продления подписки. 👤 Если вы уверены, что это ошибка, напишите сюда: {SUPPORT}\n⏱ Мы ответим вам как можно скорее.")
            bot.send_message(ADMIN_ID, f"⚠️ Ошибка продления!\nTG: @{username} ({tg_id})\n{error_msg}")

    pending_requests.pop(tg_id, None)

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
        pending_requests[tg_id] = {"flow": "renew"}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"📅 На 1 месяц — {PRICE_PER_MONTH}₽", callback_data="renew:1"),
            types.InlineKeyboardButton("📅 На несколько месяцев", callback_data="renew:multi")
        )
        bot.send_message(
            message.chat.id,
            "🔄 Выберите срок продления:",
            reply_markup=markup
        )
    elif text == "📩 Поддержка":
        bot.send_message(message.chat.id, support_contact())

@bot.callback_query_handler(func=lambda call: call.data.startswith("renew:"))
def handle_renew_choice(call):
    action = call.data.split(":")[1]
    tg_id = call.from_user.id

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action == "1":
        send_invoice(tg_id, months=1)
    elif action == "multi":
        msg = bot.send_message(tg_id, "📅 Введите количество месяцев (1–12):")
        bot.register_next_step_handler(msg, process_months_input, tg_id)

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
        expiry_date = "БЕССРОЧНО" if expiry == 0 else datetime.fromtimestamp(expiry / 1000, tz=moscow_tz).strftime("%d.%m.%Y %H:%M")

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
    report = get_payments_report()
    bot.send_message(message.chat.id, report, parse_mode="HTML")

# ====================== ЗАПУСК ======================
if __name__ == '__main__':
    lock_file = acquire_lock()
    try:
        load_users()
        print("Bot successfully started")
        start_expiry_checker()
        bot.infinity_polling(skip_pending=True)
    except Exception as e:
        print(f"Fatal error: {e}")
