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
bot = telebot.TeleBot(API_TOKEN,
                      parse_mode=None,
                      disable_web_page_preview=False)

# Увеличиваем таймауты
bot.enable_save_next_step_handlers(delay=2)

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
pending_requests = {}
user_ids = {}
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.lock')
uid_counter = 1
admin_given_email = None
admin_given_username = None
admin_renew_uid = 0



# =======================================================
# ====================== ФУНКЦИИ =======================
# =======================================================



# ====================== Работа с 3x-ui =======================

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

# Создание клиента в 3x-ui
def create_vpn_client(uid: int, tg_id: str = None, username: str = None, months: int = 1):
    if not tg_id:
        tg_id = "by_admin"
    base_name = f"{uid}_{username}_{tg_id}"
    if months != 0:
        expiry_date = datetime.now() + timedelta(days=XUI_EXPIRY_DAYS * months)
        expiry_ms = int(expiry_date.timestamp() * 1000)
    else:
        expiry_ms = 0
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
            "tgId": tg_id,
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
        return False, f"Успешно {success_count}/{len(XUI_INBOUND_IDS)} inbound'ов", email, expiry_ms, sub_id

# Продление клиента в 3x-ui
def renew_vpn_client(uid: int, tg_id: str = None, username: str = None, months: int = 1):
    if not tg_id:
        tg_id = "by_admin"
        user_data = users.get(str(uid))
    else:
        uid, user_data = get_user_by_tg_id(tg_id)

    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        extra_days = XUI_EXPIRY_DAYS * months

        if tg_id and tg_id != "by_admin":
            _, user_data = get_user_by_tg_id(tg_id)
        else:
            user_data = users.get(str(uid))

        if not user_data or not user_data.get("email"):
            return False, "Email пользователя не найден в users.json", None, None

        base_email = user_data["email"]
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
                continue

            inbound = r.json().get("obj")
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])

            for client in clients:
                if client.get("email") == search_email:
                    if months == 0:
                        new_expiry = 0
                    else:
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
                        print(f"✅ Inbound {inbound_id} → продлено до {datetime.fromtimestamp(new_expiry/1000).date() if months > 0 else 'БЕССРОЧНО'}")
                    break

        if success_count > 0 and new_expiry is not None:
            user_data["expiry_time"] = new_expiry
            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)

            return True, "", base_email, new_expiry

        return False, f"Клиент не найден ни в одном inbound (проверено {len(XUI_INBOUND_IDS)})", None, None

    except Exception as e:
        print(f"❌ Ошибка продления: {e}")
        return False, str(e), None, None



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

# Сохранение нового пользователя в файл
def save_user(uid, tg_id, email=None, username=None, status="approved", expiry_time=None, sub_id=None):
    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    key = str(uid)

    if key in data:
        current = data[key]
        if tg_id:
            current["tg_id"] = tg_id
        if email:
            current["email"] = email
        if username and username != "no_username":
            current["username"] = username
        current["status"] = status
        if expiry_time:
            current["expiry_time"] = expiry_time
        if sub_id:
            current["sub_id"] = sub_id

    else:
        # Новый пользователь
        if expiry_time is None:
            expiry_time = int((datetime.now() + timedelta(days=XUI_EXPIRY_DAYS)).timestamp() * 1000)
        data[key] = {
            "tg_id": tg_id or "by_admin",
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

# Отправка инструкций
def instruction_send(tg_id):
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


# Контакт поддержки
def support_contact():
    return f"📩 Поддержка\n👤 Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее."

# Надёжная отправка сообщений с повторными попытками
def safe_send_message(chat_id, text, parse_mode="HTML", reply_markup=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            return bot.send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Попытка {attempt+1}/{max_retries} отправки сообщения не удалась: {e}")
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))  # увеличиваем задержку
            else:
                print(f"Не удалось отправить сообщение пользователю {chat_id} после {max_retries} попыток")
                return None

# Функция подгрузки tg пользователей
def load_users():
    global user_ids, uid_counter
    if not os.path.exists("users.json"):
        uid_counter = 1
        return

    with open("users.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    max_uid = 0
    for key, info in data.items():
        status = info.get("status")
        tg_id = info.get("tg_id")

        # Обычный пользователь (ключ = uid)
        if key.isdigit():
            uid = int(key)
            if tg_id is not None:
                user_ids[tg_id] = uid
            if uid > max_uid:
                max_uid = uid

    uid_counter = max_uid + 1 if max_uid > 0 else 1

# Получение tg ссылки на пользователя
def get_user_link(tg_id, username):
    if username and username != "no_username":
        return f"https://t.me/{username}"
    else:
        return f"tg://user?id={tg_id}"

# Поиск пользователя по tg_id в файле users.json
def get_user_by_tg_id(tg_id):
    if not os.path.exists("users.json"):
        return None, None

    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)

    for uid_key, user_data in users.items():
        if user_data.get("tg_id") == tg_id:
            return uid_key, user_data

    return None, None

# Поиск пользователя по username
def get_user_by_username(username):
    if not os.path.exists("users.json"):
        return None, None

    username = username.lower().replace("@", "").strip()

    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)

    for uid_key, user_data in users.items():
        saved_username = user_data.get("username", "").lower().replace("@", "").strip()
        if saved_username == username:
            return uid_key, user_data

    return None, None

# Обновление tgId в 3x-ui после привязки пользователя
def update_tg_id_in_xui(uid: str, tg_id: int):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        user_data = users.get(str(uid))
        if not user_data or not user_data.get("email"):
            return False

        base_email = user_data["email"]
        success_count = 0

        for inbound_id in XUI_INBOUND_IDS:
            search_email = f"{base_email}@inbound{inbound_id}"

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

            updated = False
            for client in clients:
                if client.get("email") == search_email:
                    client["tgId"] = str(tg_id)
                    updated = True
                    break

            if updated:
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
                    print(f"✅ Обновлён tgId в inbound {inbound_id}")

        return success_count > 0

    except Exception as e:
        print(f"Ошибка обновления tgId в 3x-ui: {e}")
        return False

# Информация о подписке
def sub(tg_id, message):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        try:
            uid, user_data = get_user_by_tg_id(tg_id)

            expiry_ms = user_data.get("expiry_time")
            sub_id = user_data.get("sub_id")

            if  not sub_id:
                bot.send_message(message.chat.id, "❌ Данные подписки неполные.", reply_markup=main_menu())
                return

            moscow_tz = ZoneInfo("Europe/Moscow")

            expiry_date = "БЕССРОЧНО" if expiry_ms == 0 else datetime.fromtimestamp(
                expiry_ms / 1000, tz=moscow_tz
            ).strftime("%d.%m.%Y %H:%M (МСК)")

            sub_link = f"{XUI_SUB_LINK}/{sub_id}"

            bot.send_message(
                message.chat.id,
                "📦 <b>Ваша подписка</b>\n\n"
                f"🔗 <b>Ссылка:</b>\n"
                f"<code>{sub_link}</code>\n\n"
                f"📅 <b>Действует до:</b> {expiry_date}\n\n"
                "❤️ Спасибо, что вы с нами!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        except Exception as e:
            print("Ошибка парсинга списка пользователей по tg_id")
            bot.send_message(
                message.chat.id,
                "❌ Не удалось загрузить информацию о подписке.",
                reply_markup=main_menu()
            )

    except Exception as e:
        print(f"Ошибка доступа к файлу users.json: {e}")
        bot.send_message(
            message.chat.id,
            "❌ Не удалось загрузить информацию о подписке."
        )

# Обработка продления на несколько месяцев
def process_months_input(message, tg_id):
    try:
        months = int(message.text.strip())
        if months < 1 or months > 12:
            msg = bot.send_message(tg_id, "❌ Простите, мы пока не оформляем подписки дольше чем на год. Введите другое число месяцев.")
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
            current_time = now.timestamp() * 1000

            # Запускаем проверку только в 12:00 ± 2 минуты (чтобы не пропустить)
            if now.hour == 12 and now.minute < 3:
                if os.path.exists("users.json"):
                    with open("users.json", "r", encoding="utf-8") as f:
                        users = json.load(f)

                    sent_count = 0
                    print(f"[{now.strftime('%d.%m.%Y %H:%M')}] Запуск ежедневной проверки подписок...")

                    for uid_key, data in users.items():
                        tg_id_raw = data.get("tg_id")
                        if not tg_id_raw:
                            continue
                        try:
                            tg_id = int(tg_id_raw)
                        except (ValueError, TypeError):
                            continue

                        expiry = data.get("expiry_time")
                        if not expiry or expiry == 0:  # бессрочные
                            continue

                        days_left = (expiry - current_time) / (86400 * 1000)
                        username = data.get("username", "пользователь")

                        if 2.5 < days_left < 3.5:   # Через ~3 дня
                            try:
                                bot.send_message(
                                    tg_id,
                                    "⚠️ <b>Ваша подписка заканчивается через 3 дня!</b>\n\n"
                                    "Не забудьте продлить, чтобы не потерять доступ.",
                                    parse_mode="HTML"
                                )
                                print(f"✅ Уведомление отправлено (3 дня): {username} (TG: {tg_id})")
                                sent_count += 1
                            except Exception as e:
                                print(f"Не удалось отправить (3 дня) {tg_id}: {e}")

                        elif -0.5 < days_left < 1.0:   # Сегодня или завтра
                            try:
                                bot.send_message(
                                    tg_id,
                                    "❗️ <b>Ваша подписка сегодня заканчивается!</b>\n\n"
                                    "Продлите подписку, чтобы продолжить пользоваться VPN.",
                                    parse_mode="HTML"
                                )
                                print(f"✅ Уведомление отправлено (сегодня): {username} (TG: {tg_id})")
                                sent_count += 1
                            except Exception as e:
                                print(f"Не удалось отправить (сегодня) {tg_id}: {e}")

                    print(f"Проверка завершена. Отправлено: {sent_count} уведомлений.")

            time.sleep(60)  # проверяем каждую минуту

        except Exception as e:
            print(f"Ошибка проверки истекающих подписок: {e}")
            time.sleep(300)

# Великий русский язык
def months_word(months: int) -> str:
    if months % 10 == 1 and months % 100 != 11:
        return "месяц"
    elif months % 10 in [2, 3, 4] and months % 100 not in [12, 13, 14]:
        return "месяца"
    else:
        return "месяцев"



# ====================== Кнопки/меню/вопросы =======================

# Главное меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📦 Моя подписка")
    markup.add("🔄 Продлить подписку")
    markup.add("📑 Инструкция")
    markup.add("📩 Поддержка")
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
    price_per_month = get_price_per_month(months)
    amount = price_per_month * months * 100
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

# Скидки
def get_price_per_month(months: int) -> int:
    if months >= 8:
        return 100
    elif months >= 3:
        return 125
    else:
        return PRICE_PER_MONTH  # базовая цена из конфига

# =======================================================================
# ====================== ФУНКЦИОНАЛ ПОЛЬЗОВАТЕЛЯ ======================
# =======================================================================

# ====================== Основной оффер =======================

@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id
    username = (message.from_user.username or "no_username").lower().replace("@", "")
    loading_msg = bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())

    try:
        # 1. Ищем по tg_id
        uid, user_data = get_user_by_tg_id(tg_id)

        if user_data and user_data.get("status") == "approved":
            try:
                bot.delete_message(message.chat.id, loading_msg.message_id)
            except:
                pass
            bot.send_message(message.chat.id, "Добро пожаловать 👇", reply_markup=main_menu())
            return

        # 2. Ищем по username (пользователь добавлен админом)
        if username and username != "no_username":
            uid_by_name, user_by_name = get_user_by_username(username)
            if user_by_name and user_by_name.get("status") == "approved":
                # Привязываем tg_id
                save_user(uid_by_name, tg_id, None, username, "approved")
                print(f"✅ Привязан tg_id {tg_id} к пользователю @{username} (UID: {uid_by_name})")

                # Обновляем tgId в 3x-ui
                update_tg_id_in_xui(uid_by_name, tg_id)

                try:
                    bot.delete_message(message.chat.id, loading_msg.message_id)
                except:
                    pass
                bot.send_message(message.chat.id, "Добро пожаловать 👇", reply_markup=main_menu())
                return

    except Exception as e:
        print(f"Ошибка в start_handler: {e}")

    # Пользователь в процессе оплаты
    if tg_id in pending_requests:
        try:
            bot.delete_message(message.chat.id, loading_msg.message_id)
        except:
            pass
        bot.send_message(message.chat.id, "🕚 Жду подтверждения")
        return

    # Новый пользователь
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
        "• 🇫🇮 Хельсинки ×2\n"
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
        "• Пошаговую инструкцию\n"
        "• И использование одной подписки на 3 устройствах!\n\n"
        "💰 <b>Цена:</b>\n"
        f"{PRICE_PER_MONTH}₽ / месяц\n"
        f"{get_price_per_month(3)}₽ / от трёх месяцев\n"
        f"{get_price_per_month(8)}₽ / от восьми месяцев\n\n"
        "❓ <b>Оформляем?</b>",
        parse_mode="HTML",
        reply_markup=markup
    )

# Запрос кол-ва месяцев для новой подписки
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

# Обработчик успешной оплаты
@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    tg_id = message.chat.id
    payment = message.successful_payment
    data = pending_requests.get(tg_id, {})
    uid = get_or_create_uid(tg_id)

    months = data.get("months", 1)
    flow = data.get("flow", "new")   # "new" или "renew"
    username = message.from_user.username or "no_username"

    # Надёжная отправка
    safe_send_message(tg_id, "✅ Оплата прошла успешно! Активируем подписку...")

    if flow == "new":
        # === НОВАЯ ПОДПИСКА ===
        success, error_msg, base_name, expiry_ms, sub_id = create_vpn_client(uid, tg_id, username, months)

        if success:
            save_user(uid, tg_id, base_name, username, "approved", expiry_ms, sub_id)

            sub_link = f"{XUI_SUB_LINK}/{sub_id}"

            # Уведомление админу
            admin_notify(tg_id, username, base_name, months, payment.total_amount, "Новая подписка")

            # Отправка инструкций
            instruction_send(tg_id)

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
            bot.send_message(tg_id, f"❌ Ошибка активации подписки. Если вы уверены, что оплата прошла,\n 👤 Напишите сюда: {SUPPORT}\n⏱ Мы ответим вам как можно скорее.")
            bot.send_message(
                ADMIN_ID,
                f"⚠️ Ошибка создания пользователя!\n"
                f"TG: @{username} ({tg_id})\n"
                f"Ошибка: {error_msg}"
            )

    else:
        # === ПРОДЛЕНИЕ ===
        success, error_msg, email, expiry_ms = renew_vpn_client(uid, tg_id, username, months)

        if success:
            save_user(get_or_create_uid(tg_id), tg_id, email, username, "approved", expiry_ms)

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
            bot.send_message(tg_id, f"❌ Ошибка продления подписки. 👤 Если вы уверены, что оплата прошла, напишите сюда: {SUPPORT}\n⏱ Мы ответим вам как можно скорее.", parse_mode="HTML")
            bot.send_message(ADMIN_ID, f"⚠️ Ошибка продления!\nTG: @{username} ({tg_id})\n{error_msg}")

    pending_requests.pop(tg_id, None)

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

# Обработчик отмены оплаты
@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel:"))
def handle_cancel(call):
    tg_id = int(call.data.split(":")[1])

    pending_requests.pop(tg_id, None)

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



# ====================== Реакции на кнопки меню пользователя  =======================


# ====================== Реакция на кнопку "Моя подписка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📦 Моя подписка")
def subscribe_handler(message):
    tg_id = message.from_user.id
    sub(tg_id, message)



# ====================== Реакция на кнопку "Продлить подписку"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "🔄 Продлить подписку")
def renew_handler(message):
    tg_id = message.from_user.id

    # Получаем данные пользователя
    uid, user_data = get_user_by_tg_id(tg_id)

    # Проверка на бессрочную подписку
    if user_data and user_data.get("expiry_time") == 0:
        bot.send_message(
            message.chat.id,
            "♾ <b>У вас бессрочная подписка!</b>\n\n"
            "Продлевать её не нужно 😉\n\n"
            "Приятного использования VidjetVPN! ❤️",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        return

    # Обычная логика продления (для пользователей со сроком)
    pending_requests[tg_id] = {"flow": "renew"}
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(f"📅 На 1 месяц — {PRICE_PER_MONTH}₽", callback_data="renew:1"),
        types.InlineKeyboardButton("📅 На несколько месяцев", callback_data="renew:multi")
    )
    bot.send_message(
        message.chat.id,
        "💰 <b>Цена:</b>\n"
        f"{PRICE_PER_MONTH}₽ / месяц\n"
        f"{get_price_per_month(3)}₽ / от трёх месяцев\n"
        f"{get_price_per_month(8)}₽ / от восьми месяцев\n\n"
        "🔄 Выберите срок продления:",
        parse_mode="HTML",
        reply_markup=markup
    )

# Запрос кол-ва месяцев для продления
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



# ====================== Реакция на кнопку "Инструкция"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📑 Инструкция")
def instruction_handler(message):
    instruction_send(message.chat.id)



# ====================== Реакция на кнопку "Поддержка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📩 Поддержка")
def support_handler(message):
    bot.send_message(message.chat.id, support_contact(), reply_markup=main_menu())



# =================================================================
# ====================== ФУНКЦИОНАЛ АДМИНА ======================
# =================================================================

# Обработка вызова админки
@bot.message_handler(commands=['admin'])
def admin_handler(message):
    if not is_admin(message.from_user.id):
        return

    bot.send_message(
        message.chat.id,
        "⚙️ Админ-панель",
        reply_markup=admin_panel()
    )

# Проверка на админа
def is_admin(user_id):
    return user_id == ADMIN_ID



# ====================== Реакция на кнопку "Пользователи"  =======================
@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "👥 Пользователи"
)
def show_users(message):
    if not is_admin(message.from_user.id):
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📋 Все пользователи", callback_data="users_filter:all"),
        types.InlineKeyboardButton("⏳ Платные", callback_data="users_filter:limited"),
        types.InlineKeyboardButton("♾ Бесплатные", callback_data="users_filter:unlimited")
    )

    bot.send_message(
        message.chat.id,
        "👥 Выберите фильтр пользователей:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("users_filter:"))
def users_filter_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет доступа")
        return

    filter_type = call.data.split(":")[1]

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if filter_type == "all":
        show_users_list(call.message, "all")
    elif filter_type == "limited":
        show_users_list(call.message, "limited")
    elif filter_type == "unlimited":
        show_users_list(call.message, "unlimited")

    bot.answer_callback_query(call.id)

def show_users_list(message, filter_type="all"):
    if not os.path.exists("users.json"):
        bot.send_message(message.chat.id, "users.json не найден")
        return

    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)

    all_traffic = get_all_users_traffic()
    online_clients = get_online_clients()

    result = f"👥 Пользователи — {filter_type.upper()}\n\n{'─' * 18}\n\n"
    count = 0

    for key, info in users.items():
        uid = key
        tg_id = info.get("tg_id", "-")
        email = info.get("email", "-")
        username = info.get("username", "no_username")
        status = info.get("status", "unknown")
        expiry = info.get("expiry_time", 0)
        sub_id = info.get("sub_id", "-")

        # Фильтрация
        if filter_type == "limited" and expiry == 0:
            continue
        if filter_type == "unlimited" and expiry != 0:
            continue

        moscow_tz = ZoneInfo("Europe/Moscow")
        expiry_date = "♾ БЕССРОЧНО" if expiry == 0 else datetime.fromtimestamp(
            expiry / 1000, tz=moscow_tz
        ).strftime("%d.%m.%Y %H:%M")

        online = False
        if email:
            for inbound_id in XUI_INBOUND_IDS:
                check_email = f"{email}@inbound{inbound_id}"
                if check_email in online_clients:
                    online = True
                    break

        traffic_data = all_traffic.get(email, {})
        up = round(traffic_data.get("up", 0) / (1024**3), 2)
        down = round(traffic_data.get("down", 0) / (1024**3), 2)

        result += (
            f"UID: <b>{uid}</b>\n"
            f"TG_ID: {tg_id}\n"
            f"USER: @{username}\n"
            f"EMAIL: <code>{email}</code>\n"
            f"STATUS: {status}\n"
            f"ONLINE: {'🟢' if online else '🔴'}\n"
            f"UP: {up} GB | DOWN: {down} GB\n"
            f"EXPIRE: {expiry_date}\n\n"
            f"SUB: <code>{XUI_SUB_LINK}/{sub_id}</code>\n\n"
            f"{'─' * 18}\n\n"
        )
        count += 1

    if count == 0:
        result += "Пользователей по данному фильтру не найдено."

    # Отправляем частями
    for i in range(0, len(result), 4000):
        bot.send_message(message.chat.id, result[i:i+4000], parse_mode="HTML")

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
def get_all_users_traffic():
    traffic = {}

    try:
        r = requests.get(
            f"{XUI_URL}/panel/api/inbounds/list",
            headers=headers,
            timeout=15
        )

        if r.status_code != 200 or not r.json().get("success"):
            return traffic

        inbounds = r.json().get("obj", [])

        for inbound in inbounds:
            client_stats = inbound.get("clientStats", [])

            for client in client_stats:
                email = client.get("email")

                if not email:
                    continue

                base_email = email.split("@inbound")[0]

                if base_email not in traffic:
                    traffic[base_email] = {
                        "up": 0,
                        "down": 0
                    }

                traffic[base_email]["up"] += client.get("up", 0)
                traffic[base_email]["down"] += client.get("down", 0)

    except Exception as e:
        print(f"Traffic error: {e}")

    return traffic



# ====================== Реакция на кнопку "Добавить пользователя"  =================
@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "➕ Добавить пользователя")
# Спрашиваем username
def process_ask_username(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.send_message(
        message.chat.id,
        "👤 Введите @username пользователя\n\n"
        "Если username неизвестен — напишите `-`"
    )
    bot.register_next_step_handler(msg, admin_process_ask_email)

# Спрашиваем email
def admin_process_ask_email(message):
    input_text = message.text.strip()

    global admin_given_username

    if input_text == "-":
        admin_given_username = "no_username"
        bot.send_message(message.chat.id, "✅ Username пропущен (no_username)")
    else:
        admin_given_username = input_text.replace("@", "").strip().lower()
        bot.send_message(message.chat.id, f"✅ Username сохранён: @{admin_given_username}")

    # Переходим к Email
    msg = bot.send_message(message.chat.id, "📧 Введите Email (base name) для клиента:")
    bot.register_next_step_handler(msg, admin_process_ask_time_new)

# Спрашиваем время подписки
def admin_process_ask_time_new(message):

    base_name = message.text.strip().lower()

    global admin_given_email
    admin_given_email = base_name

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("♾ Бессрочно", callback_data="admin_add:unlimited"))
    markup.add(types.InlineKeyboardButton("📅 На 1 месяц", callback_data="admin_add:1"))
    markup.add(types.InlineKeyboardButton("📅 На несколько месяцев", callback_data="admin_add:multi"))

    bot.send_message(message.chat.id, "⏳ Выберите срок подписки:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_add:"))
def process_add_by_email(call):
    if call.from_user.id != ADMIN_ID:
        return

    action = call.data.split(":")[1]

    if action == "multi":
        msg = bot.send_message(call.message.chat.id, "Введите количество месяцев (1-12):")
        bot.register_next_step_handler(msg, admin_add_multi_months)
    elif action == "unlimited":
        months = 0
        admin_add_user(call.message, months)
    else:
        months = 1
        admin_add_user(call.message, months)

# Обработка ввода кол-ва месяцев
def admin_add_multi_months(message):
    try:
        months = int(message.text.strip())
        if months < 1 or months > 12:
            raise ValueError
    except:
        bot.send_message(message.chat.id, "❌ Введите корректное кол-во месяцев (1-12)")
        bot.register_next_step_handler(message, admin_add_multi_months)
        return

    admin_add_user(message, months)

# Добавление пользователя через админа
def admin_add_user(message, months):
    global admin_given_email, admin_given_username
    tg_id = None
    uid = get_or_create_uid(tg_id)

    success, error_msg, email, expiry_ms, sub_id = create_vpn_client(uid, tg_id, admin_given_email, months)

    if success:
        save_user(uid, tg_id, email, admin_given_username, "approved", expiry_ms, sub_id)

        moscow_tz = ZoneInfo("Europe/Moscow")
        expiry_date = "БЕССРОЧНО" if expiry_ms == 0 else datetime.fromtimestamp(
            expiry_ms / 1000, tz=moscow_tz
        ).strftime("%d.%m.%Y %H:%M (МСК)")

        sub_link = f"{XUI_SUB_LINK}/{sub_id}"

        username_display = admin_given_username if admin_given_username != "no_username" else "нет"

        bot.send_message(
            message.chat.id,
            f"✅ Пользователь успешно создан!\n\n"
            f"🆔 UID: <b>{uid}</b>\n"
            f"👤 Username: @{username_display}\n"
            f"📧 Email: <code>{email}</code>\n"
            f"📅 <b>Действует до:</b> {expiry_date}\n\n"
            f"🔗 Ссылка:\n<code>{sub_link}</code>",
            parse_mode="HTML",
            reply_markup=admin_panel()
        )
    else:
        bot.send_message(message.chat.id, f"❌ Ошибка создания: {error_msg}")

    # Очистка
    admin_given_email = None
    admin_given_username = None



# ====================== Реакция на кнопку "Продлить пользователя"  =================
@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "🔄 Продлить пользователя")
def process_ask_uid(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.send_message(message.chat.id, "📧 Введите uid пользователя:")
    bot.register_next_step_handler(msg, admin_process_ask_time_renew)

def admin_process_ask_time_renew(message):

    uid = message.text.strip().lower()

    global admin_renew_uid
    admin_renew_uid = uid

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📅 На 1 месяц", callback_data="admin_renew:1"))
    markup.add(types.InlineKeyboardButton("📅 На несколько месяцев", callback_data="admin_renew:multi"))
    markup.add(types.InlineKeyboardButton("♾ Бессрочно", callback_data="admin_renew:unlimited"))

    bot.send_message(message.chat.id, "⏳ Выберите срок подписки:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_renew:"))
def process_renew_choice(call):
    if call.from_user.id != ADMIN_ID:
        return

    action = call.data.split(":")[1]

    if action == "multi":
        msg = bot.send_message(call.message.chat.id, "Введите количество месяцев (1-12):")
        bot.register_next_step_handler(msg, admin_renew_multi_months)
    else:
        months = 0 if action == "unlimited" else 1
        admin_renew_user(call.message, months)

    bot.answer_callback_query(call.id)

# Обработка ввода кол-ва месяцев
def admin_renew_multi_months(message):
    try:
        months = int(message.text.strip())
        if months < 1 or months > 12:
            raise ValueError
        admin_renew_user(message, months)
    except:
        msg = bot.send_message(message.chat.id, "❌ Введите корректное кол-во месяцев (1-12)")
        bot.register_next_step_handler(msg, admin_renew_multi_months)

# Продление пользователя через админа
def admin_renew_user(message, months):
    global admin_renew_uid
    uid = admin_renew_uid

    if not uid:
        bot.send_message(message.chat.id, "❌ Ошибка: UID не найден.")
        return

    # === Получаем данные пользователя из users.json ===
    user_key = str(uid)

    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка чтения users.json: {e}")
        return

    user_data = users.get(str(uid))
    if not user_data:
        bot.send_message(message.chat.id, f"❌ Пользователь с UID {uid} не найден.")
        return

    tg_id = user_data.get("tg_id")
    username = user_data.get("username", "no_username")
    email = user_data.get("email")
    sub_id = user_data.get("sub_id")

    if not email:
        bot.send_message(message.chat.id, f"❌ У пользователя с UID {uid} не найден email.")
        return

    success, error_msg, email, new_expiry = renew_vpn_client(uid, tg_id, username, months)

    if success and new_expiry is not None:
        # Продляем дату
        users[user_key]["expiry_time"] = new_expiry

        try:
            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)
        except Exception as e:
                    bot.send_message(message.chat.id, f"❌ Ошибка записи в users.json: {e}")

        moscow_tz = ZoneInfo("Europe/Moscow")
        expiry_date = "БЕССРОЧНО" if new_expiry == 0 else datetime.fromtimestamp(
            new_expiry / 1000, tz=moscow_tz
            ).strftime("%d.%m.%Y %H:%M (МСК)")

        sub_link = f"{XUI_SUB_LINK}/{sub_id}"

        bot.send_message(
            message.chat.id,
            f"✅ **Подписка успешно продлена!**\n\n"
            f"🆔 UID: <b>{uid}</b>\n"
            f"👤 @{username}\n"
            f"📧 <code>{email}</code>\n"
            f"🔄 Месяцев добавлено: {months if months > 0 else '∞'}\n"
            f"📅 Новая дата окончания: <b>{expiry_date}</b>\n\n"
            f"🔗 Ссылка:\n<code>{sub_link}</code>",
            parse_mode="HTML",
            reply_markup=admin_panel()
        )

        # Уведомляем пользователя, если есть tg_id
        if tg_id and tg_id != "unlimited":
            try:
                period_text = (
                    f"{months} {months_word(months)}."
                    if months > 0
                    else "неограниченный срок. Поздравляем с бессрочной подпиской!"
                )

                bot.send_message(
                    int(tg_id),
                    f"🔄 Ваша подписка была продлена администратором на {period_text}🎉🎉🎉\n\n"
                    f"📅 Новая дата окончания: {expiry_date}\n\n"
                    f"🔗 Ссылка:\n<code>{sub_link}</code>",
                    parse_mode="HTML",
                    reply_markup=main_menu()
                )

            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Ошибка отправки уведомления пользователя: {e}")
        else:
            bot.send_message(message.chat.id, f"❌ Не найден tg_id пользователя")
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка продления пользователя UID {uid}:\n{error_msg}",
            reply_markup=admin_panel()
        )
    admin_renew_uid = None


# ====================== Реакция на кнопку "Удалить пользователя"  =================
@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "🗑 Удалить пользователя")
def ask_delete_user(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.send_message(message.chat.id,"🗑 Введите **UID** пользователя для удаления:")
    bot.register_next_step_handler(msg, process_delete_by_uid)

# Проверка uid
def process_delete_by_uid(message):
    try:
        uid = int(message.text.strip())
    except:
        return bot.send_message(message.chat.id, "❌ Неверный UID. Введите число.")

    success, msg = delete_vpn_user_by_uid(uid)
    if success:
        bot.send_message(message.chat.id, f"✅ {msg}", reply_markup=admin_panel())
    else:
        bot.send_message(message.chat.id, f"❌ {msg}", reply_markup=admin_panel())

# Удаление клиента
def delete_vpn_user_by_uid(uid: int):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)
    except Exception as e:
        return False, "users.json not found"

    target_key = str(uid)

    # Ищем пользователя по uid в users.json
    try:
        email = users[target_key].get("email")
    except Exception as e:
        return False, f"Пользователь с UID {uid} не найден"

    deleted = 0
    for inbound_id in XUI_INBOUND_IDS:
        try:
            r = requests.post(
                f"{XUI_URL}/panel/api/inbounds/{inbound_id}/delClientByEmail/{email}@inbound{inbound_id}",
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



# ====================== Реакция на кнопку "Статус серверов"  =================
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



# ====================== Реакция на кнопку "Отчет по оплатам" =================
@bot.message_handler(func=lambda m:
    m.from_user.id == ADMIN_ID and m.text == "📊 Отчет по оплатам")
def show_payments_report(message):
    if not is_admin(message.from_user.id):
        return
    report = get_payments_report()
    bot.send_message(message.chat.id, report, parse_mode="HTML")

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

# ====================== ЗАПУСК ======================

# Запуск проверки истёкших подписок в фоне
def start_expiry_checker():
    thread = threading.Thread(target=check_expiring_subscriptions, daemon=True)
    thread.start()

# Основной запуск программы
if __name__ == '__main__':
    lock_file = acquire_lock()
    try:
        load_users()
        print("Bot successfully started")
        start_expiry_checker()
        bot.infinity_polling(skip_pending=True)
    except Exception as e:
        print(f"Fatal error: {e}")
