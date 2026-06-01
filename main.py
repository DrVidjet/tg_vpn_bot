import telebot
from telebot import types
from telebot.types import InlineKeyboardButton
import io
import os
import configparser
import sys
import fcntl
import json
import requests
import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from yookassa import Payment, Configuration
import time
import threading
import base64
from flask import Flask, request, jsonify
import traceback
import random
import string
import re

# ====================== КОНФИГУРАЦИЯ ======================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'API.conf')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)

API_TOKEN = config.get('TG', 'API').strip('"')
ADMIN_ID = config.getint('TG', 'ADMIN_ID')
SUPPORT = config.get('TG', 'SUPPORT_LINK').strip('"')
GRUPP = config.get('TG', 'GRUPP_LINK').strip('"')
PRICE_PER_MONTH = config.getint('CELL', 'PRICE_PER_MONTH')
FIRST_DISCOUNT_COUNT_MONTH= config.getint('CELL', 'FIRST_DISCOUNT_COUNT_MONTH')
PRICE_PER_ONE_FIRST_DISCOUNT_MONTH = config.getint('CELL', 'PRICE_PER_ONE_FIRST_DISCOUNT_MONTH')
SECOND_DISCOUNT_COUNT_MONTH= config.getint('CELL', 'SECOND_DISCOUNT_COUNT_MONTH')
PRICE_PER_ONE_SECOND_DISCOUNT_MONTH = config.getint('CELL', 'PRICE_PER_ONE_SECOND_DISCOUNT_MONTH')

PAY_DOMEN = config.get('WEB', 'PAY_DOMEN').strip('"')
PAY_WEBHOOK = config.get('WEB', 'PAY_WEBHOOK').strip('"')
FLASK_PORT = config.getint('WEB', 'FLASK_PORT')

# === ЮKassa Telegram Payments ===
YOOKASSA_SECRET_KEY = config.get('UKASSA', 'SECRET_KEY').strip('"')
YOOKASSA_SHOP_ID = config.get('UKASSA', 'SHOP_ID').strip('"')

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

def get_yookassa_headers():
    credentials = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Idempotence-Key": str(uuid.uuid4())
    }

# Настройки X-UI
XUI_URL = config.get('3XUI', 'XUI_URL').strip('"')
XUI_API_TOKEN = config.get('3XUI', 'XUI_API_TOKEN').strip('"')

XUI_INBOUND_IDS = [int(x.strip()) for x in config.get('3XUI', 'XUI_INBOUND_IDS').split(',')]
XUI_SUB_LINK = config.get('3XUI', 'XUI_SUB_LINK').strip('"')
XUI_EXPIRY_DAYS = config.getint('3XUI', 'XUI_EXPIRY_DAYS', fallback=31)
XUI_CLIENT_LIMIT_IP = config.getint('3XUI', 'XUI_CLIENT_LIMIT_IP', fallback=3)

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

# Создание клиента в 3x-ui
def create_vpn_client(uid: int, tg_id: str = None, username: str = None, months: int = 1):
    if not tg_id:
        tg_id = "by_admin"

    base_name = f"{uid}_{username}_{tg_id}"
    sub_id = str(uuid.uuid4())

    if months != 0:
        expiry_date = datetime.now() + timedelta(days=XUI_EXPIRY_DAYS * months)
        expiry_ms = int(expiry_date.timestamp() * 1000)
    else:
        expiry_ms = 0

    client_payload = {
        "email": base_name,
        "subId": sub_id,
        "limitIp": XUI_CLIENT_LIMIT_IP,
        "totalGB": 0,
        "expiryTime": expiry_ms,
        "enable": True,
        "tgId": int(tg_id) if tg_id != "by_admin" else 0,
        "flow": "xtls-rprx-vision",
    }

    payload = {
        "client": client_payload,
        "inboundIds": XUI_INBOUND_IDS
    }

    try:
        r = requests.post(
            f"{XUI_URL}/panel/api/clients/add",
            headers=headers,
            json=payload,
            timeout=20
        )

        if r.status_code == 200 and r.json().get("success"):
            print(f"✅ Клиент создан: {base_name} | Прикреплён к {len(XUI_INBOUND_IDS)} inbound'ам")
            return True, "", base_name, expiry_ms, sub_id
        else:
            error = r.json().get("msg", r.text)
            print(f"❌ Ошибка создания клиента: {error}")
            return False, error, base_name, expiry_ms, sub_id

    except Exception as e:
        print(f"❌ Exception при создании клиента: {e}")
        return False, str(e), base_name, expiry_ms, sub_id



# Продление клиента в 3x-ui
def renew_vpn_client(uid: int, tg_id: str = None, username: str = None, months: int = 1):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        if tg_id and str(tg_id) != "by_admin":
            _, user_data = get_user_by_tg_id(tg_id)
        elif uid:
            user_data = users.get(str(uid))

        if not user_data or not user_data.get("email"):
            error = f"Email пользователя не найден (uid={uid}, tg_id={tg_id})"
            print(f"❌ {error}")
            return False, error, None, None

        base_email = user_data["email"]

        r = requests.get(
            f"{XUI_URL}/panel/api/clients/get/{base_email}",
            headers=headers,
            timeout=15
        )

        data = r.json()

        if not data.get("success"):
            error = f"Client not found: {data.get('msg', '')}"
            print(f"❌ {error}")
            return False, error, None, None

        client = data["obj"]["client"]

        if "id" in client and isinstance(client["id"], (int, float)):
            client["id"] = str(client["id"])

        extra_days = XUI_EXPIRY_DAYS * months

        if months == 0:
            new_expiry = 0
        else:
            now_ms = int(datetime.now().timestamp() * 1000)
            current_expiry = client.get("expiryTime", 0)

            base_time = current_expiry if current_expiry > now_ms else now_ms
            new_expiry = base_time + extra_days * 24 * 60 * 60 * 1000

        client["expiryTime"] = new_expiry

        r = requests.post(
            f"{XUI_URL}/panel/api/clients/update/{base_email}",
            headers=headers,
            json=client,
            timeout=15
        )

        result = r.json()

        if not result.get("success"):
            return False, result.get("msg"), base_email, None

        if str(uid) in users:
            users[str(uid)]["expiry_time"] = new_expiry
            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)

        print(f"✅ Подписка продлена: {base_email} → {new_expiry}")

        return True, "", base_email, new_expiry

    except Exception as e:
        print(f"❌ Ошибка продления: {e}")
        return False, str(e), None, None



# Обновление tg_id после привязки пользователя
def update_tg_id(uid: str, tg_id: int, username: str = "no_username"):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        user_data = users.get(str(uid))
        if not user_data:
            return False, "User not found"

        base_email = user_data.get("email")
        if not base_email:
            return False, "Email not found"

        # Обновляем в 3x-ui
        r = requests.get(
            f"{XUI_URL}/panel/api/clients/get/{base_email}",
            headers=headers,
            timeout=15
        )

        if r.json().get("success"):
            client = r.json()["obj"]["client"]
            if "id" in client and isinstance(client.get("id"), (int, float)):
                client["id"] = str(client["id"])
            client["tgId"] = int(tg_id)

            update_resp = requests.post(
                f"{XUI_URL}/panel/api/clients/update/{base_email}",
                headers=headers,
                json=client,
                timeout=15
            )

            if not update_resp.json().get("success"):
                print(f"⚠️ Не удалось обновить tgId в 3x-ui: {update_resp.text}")

        # Обновляем в users.json
        users[str(uid)]["tg_id"] = str(tg_id)
        users[str(uid)]["username"] = username

        with open("users.json", "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=4)

        print(f"✅ tg_id успешно обновлён для UID {uid} → {tg_id}")
        return True, None

    except Exception as e:
        print(f"❌ Ошибка update_tg_id: {e}")
        return False, str(e)



# ====================== Работа с файлами =======================

# Получение uid пользователя
def get_or_create_uid(tg_id=None):
    global uid_counter

    if os.path.exists("users.json"):
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)
    else:
        users = {}

    # Ищем существующего пользователя по tg_id
    if tg_id is not None:
        for uid, data in users.items():
            if str(data.get("tg_id")) == str(tg_id):
                return int(uid)

    # Создаем нового
    uid = uid_counter
    uid_counter += 1

    return uid



# Сохранение нового пользователя в файл
def save_user(uid, tg_id, email=None, username=None, status="approved", expiry_time=None, sub_id=None, referral_code=None):
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
        if referral_code:
            current["referral_code"] = referral_code

    else:
        # Новый пользователь
        if expiry_time is None:
            expiry_time = int((datetime.now() + timedelta(days=XUI_EXPIRY_DAYS)).timestamp() * 1000)

        if not referral_code:
            referral_code = generate_referral_code()

        data[key] = {
            "tg_id": tg_id or "by_admin",
            "email": email,
            "username": username or "no_username",
            "status": status,
            "expiry_time": expiry_time,
            "sub_id": sub_id,
            "referral_code": referral_code
        }

    with open("users.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    return referral_code



# ====================== Работа с tg =======================

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
        "2. Для Android и IOS следуйте видеоинструкции ниже. На IOS пропускаем часть с маршрутизацией приложений. На ПК всё делается аналогично видеоинструкции, интерфейс на телефоне и компьютере у программы практически одинаковый, но опять же, пропуская момент с маршрутизацией приложений.\n\n",
        parse_mode="HTML"
    )

    send_instruction_video(tg_id)



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
        if key.isdigit():
            uid = int(key)
            tg_id = info.get("tg_id")
            if tg_id is not None:
                user_ids[tg_id] = uid
            if uid > max_uid:
                max_uid = uid

    uid_counter = max_uid + 1 if max_uid > 0 else 1



# Поиск пользователя по tg_id
def get_user_by_tg_id(tg_id):
    if not os.path.exists("users.json"):
        return None, None

    tg_id = str(tg_id)  # приводим к строке

    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)

    for uid_key, user_data in users.items():
        saved_tg = user_data.get("tg_id")
        if saved_tg is not None and str(saved_tg) == tg_id:
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



# Информация о подписке
def sub(tg_id, message=None):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        uid, user_data = get_user_by_tg_id(tg_id)

        if not user_data:
            bot.send_message(tg_id, "❌ Пользователь не найден.", reply_markup=main_menu())
            return

        expiry_ms = user_data.get("expiry_time")
        sub_id = user_data.get("sub_id")

        if not sub_id:
            bot.send_message(tg_id, "❌ Данные подписки неполные.", reply_markup=main_menu())
            return

        moscow_tz = ZoneInfo("Europe/Moscow")
        expiry_date = "БЕССРОЧНО" if expiry_ms == 0 else datetime.fromtimestamp(
            expiry_ms / 1000, tz=moscow_tz
        ).strftime("%d.%m.%Y %H:%M (МСК)")

        sub_link = f"{XUI_SUB_LINK}/{sub_id}"

        text = (
            "📦 <b>Ваша подписка</b>\n\n"
            f"🔗 <b>Ссылка:</b>\n"
            f"<code>{sub_link}</code>\n\n"
            f"📅 <b>Действует до:</b> {expiry_date}\n\n"
            "❤️ Спасибо, что вы с нами!"
        )

        if message and hasattr(message, 'chat'):
            bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_menu())
        else:
            # Вызвано из webhook или другого места
            bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=main_menu())

    except Exception as e:
        print(f"Ошибка в sub(): {e}")
        try:
            bot.send_message(tg_id, "❌ Не удалось загрузить информацию о подписке.", reply_markup=main_menu())
        except:
            pass



# Обработка продления на несколько месяцев
def process_months_input(message, tg_id, flow = "new"):
    try:
        months = int(message.text.strip())
        if months < 1 or months > 12:
            msg = bot.send_message(tg_id, "❌ Простите, мы пока не оформляем подписки дольше чем на год. Введите другое число месяцев.")
            bot.register_next_step_handler(msg, process_months_input, tg_id, flow)
            return
        username = (message.from_user.username or "no_username").lower().replace("@", "")
        if flow == "renew":
            send_invoice(tg_id, username, months, flow)
        else:
            ask_referral_before_payment(tg_id, username, months, flow)

    except ValueError:
        # Если ввели не число
        msg = bot.send_message(
            tg_id,
            "❌ Пожалуйста, введите **число** (например: 3)",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow)

    except Exception as e:
        bot.send_message(tg_id, "❌ Введите корректное число")



# Отправляет уведомление админу об успешной оплате
def admin_notify(tg_id: int, username: str, email: str, months: int, amount: int, payment_type: str, referrer_uid: str = None):
    text = (
        f"💰 <b>Новая оплата</b>\n\n"
        f"Пользователь: @{username} ({tg_id})\n"
        f"Email: <code>{email}</code>\n"
        f"Тип: {payment_type}\n"
        f"Месяцев: {months}\n"
        f"Сумма: {amount // 100} ₽\n\n"
        f"Время: {datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}"
    )

    if referrer_uid:
        try:
            with open("users.json", "r", encoding="utf-8") as f:
                users = json.load(f)
            referrer = users.get(str(referrer_uid))
            if referrer:
                ref_username = referrer.get("username", "no_username")
                ref_email = referrer.get("email", "—")
                if ref_username:
                    text += f"🔗 Привёл: @{ref_username} ({ref_email})\n"
                else:
                    text += f"🔗 Привёл: {ref_email}\n"
        except:
            text += f"🔗 Привёл: UID {referrer_uid}\n"

    text += f"\nВремя: {datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}"

    try:
        bot.send_message(ADMIN_ID, text, parse_mode="HTML")
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

def device_ru():
    if XUI_CLIENT_LIMIT_IP % 10 == 1 and XUI_CLIENT_LIMIT_IP % 100 != 11:
        return "устройство"
    elif XUI_CLIENT_LIMIT_IP % 10 in [2, 3, 4] and XUI_CLIENT_LIMIT_IP % 100 not in [12, 13, 14]:
        return "устройства"
    else:
        return "устройств"



# ====================== Кнопки/меню/вопросы =======================

# Главное меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📦 Моя подписка")
    markup.add("🎟 Рефералка")
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

# Отправка платежа через YooKassa
def send_invoice(tg_id: int, username: str, months: int = 1, flow: str = "new", referral_code: str = None):
    price_per_month = get_price_per_month(months)
    amount = price_per_month * months

    pending_requests[tg_id] = {
        "flow": flow,
        "months": months,
        "amount": amount,
        "username": username,
        "referral_code": referral_code,
        "referrer_uid": pending_requests.get(tg_id, {}).get("referrer_uid")
    }

    description = f"VidjetVPN — {months} {months_word(months)}"

    payload = {
        "amount": {
            "value": str(amount),
            "currency": "RUB"
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"https://t.me/{bot.get_me().username}?start=payment_{tg_id}"
        },
        "notification_url": f"{PAY_DOMEN}/{PAY_WEBHOOK}",
        "description": description,
        "metadata": {
            "tg_id": str(tg_id),
            "months": str(months),
            "flow": flow,
            "username": username,
            "referrer_uid": pending_requests.get(tg_id, {}).get("referrer_uid")
        }
    }

    try:
        r = requests.post(
            "https://api.yookassa.ru/v3/payments",
            headers=get_yookassa_headers(),
            json=payload,
            timeout=15
        )

        if r.status_code == 200:
            payment = r.json()
            confirmation_url = payment['confirmation']['confirmation_url']

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💳 Перейти к оплате", url=confirmation_url))
            markup.add(types.InlineKeyboardButton("❌ Отменить оплату", callback_data=f"cancel_payment"))

            bot.send_message(
                tg_id,
                f"🔗 Оплата на {months} {months_word(months)} — {amount} ₽\n\n"
                "Нажмите кнопку ниже для перехода на страницу оплаты:",
                reply_markup=markup
            )
            print(f"✅ Платеж создан. Webhook URL: {PAY_DOMEN}/{PAY_WEBHOOK}")
        else:
            bot.send_message(tg_id, "❌ Ошибка создания платежа. Попробуйте позже.")
            print(f"YooKassa error: {r.text}")

    except Exception as e:
        print(f"Ошибка YooKassa: {e}")
        bot.send_message(tg_id, "❌ Ошибка создания платежа.")



def load_processed_payments():
    if not os.path.exists("processed_payments.json"):
        return set()

    with open("processed_payments.json", "r") as f:
        return set(json.load(f))



def save_processed_payment(payment_id):
    payments = load_processed_payments()

    payments.add(payment_id)

    with open("processed_payments.json", "w") as f:
        json.dump(list(payments), f)



# Скидки
def get_price_per_month(months: int) -> int:
    if months >= SECOND_DISCOUNT_COUNT_MONTH:
        return PRICE_PER_ONE_SECOND_DISCOUNT_MONTH
    elif months >= FIRST_DISCOUNT_COUNT_MONTH:
        return PRICE_PER_ONE_FIRST_DISCOUNT_MONTH
    else:
        return PRICE_PER_MONTH



# ====================== РЕФЕРАЛЬНАЯ СИСТЕМА ======================

# Генерирует уникальный реферальный код (8 символов: цифры + заглавные буквы)
def generate_referral_code(length=8):

    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if not is_referral_code_exists(code):
            return code

def is_referral_code_exists(code: str) -> bool:
    if not os.path.exists("users.json"):
        return False
    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)
    return any(user.get("referral_code") == code for user in users.values())

# Поиск пользователя по реферальному коду
def find_user_by_referral_code(code: str):
    if not os.path.exists("users.json"):
        return None, None
    with open("users.json", "r", encoding="utf-8") as f:
        users = json.load(f)
    for uid, data in users.items():
        if data.get("referral_code") == code.upper():
            return uid, data
    return None, None

# Реферальный бонус
def give_referral_bonus(referrer_uid: str, new_user_uid: str, months: int = 1):
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = json.load(f)

        if referrer_uid and referrer_uid in users:
            referrer = users[referrer_uid]

            # Проверка: не даём бонус бессрочным пользователям
            if referrer.get("expiry_time") == 0:
                print(f"ℹ️ Реферер UID {referrer_uid} имеет бессрочную подписку — бонус не выдан")
                return

            current_expiry = referrer.get("expiry_time", 0)
            now_ms = int(datetime.now().timestamp() * 1000)
            base_time = current_expiry if current_expiry > now_ms else now_ms
            bonus_ms = XUI_EXPIRY_DAYS * months * 24 * 60 * 60 * 1000
            new_expiry = base_time + bonus_ms

            referrer["expiry_time"] = new_expiry

            # Обновляем в 3x-ui
            email = referrer.get("email")
            if email:
                try:
                    r = requests.get(f"{XUI_URL}/panel/api/clients/get/{email}", headers=headers, timeout=10)
                    if r.json().get("success"):
                        client = r.json()["obj"]["client"]
                        if "id" in client and isinstance(client["id"], (int, float)):
                            client["id"] = str(client["id"])
                        client["expiryTime"] = new_expiry
                        requests.post(f"{XUI_URL}/panel/api/clients/update/{email}", headers=headers, json=client, timeout=10)
                except:
                    pass

            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)

            # Уведомляем реферера
            tg_id = referrer.get("tg_id")
            if tg_id and tg_id != "by_admin":
                try:
                    bot.send_message(int(tg_id),
                        "🎉 <b>Реферальный бонус!</b>\n\n"
                        f"Ваш друг активировал подписку по вашей рефералке.\n"
                        f"Вам добавлен +1 месяц к подписке!",
                        parse_mode="HTML")
                except:
                    pass

    except Exception as e:
        print(f"Ошибка выдачи реферального бонуса: {e}")



# =======================================================================
# ====================== ФУНКЦИОНАЛ ПОЛЬЗОВАТЕЛЯ ======================
# =======================================================================

# ====================== Основной оффер =======================

@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id
    username = (message.from_user.username or "no_username").lower().replace("@", "")
    loading_msg = bot.send_message(message.chat.id, "⌛", reply_markup=types.ReplyKeyboardRemove())


    # Получаем полный текст команды (включая параметр)
    full_text = message.text.strip() if message.text else ""

    # === ВОЗВРАТ ПОСЛЕ ОПЛАТЫ YOOKASSA ===
    if "payment_" in full_text:
        safe_send_message(tg_id, "✅ Платёж получен. Обрабатываем...")

        # Если webhook уже успел обработать — показываем меню
        if tg_id not in pending_requests:
            bot.send_message(tg_id, "🎉 Подписка уже активирована!", reply_markup=main_menu())
        else:
            bot.send_message(tg_id, "⏳ Ожидаем подтверждение от YooKassa...", reply_markup=main_menu())
        return

    # === ОБЫЧНЫЙ ЗАПУСК ===
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
            if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

                # Обновляем tgId
                update_tg_id(uid_by_name, tg_id, username)

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
        "⚡ <b>Преимущества:</b>\n"
        "• Без ограничений по трафику\n"
        "• Высокая скорость соединения\n"
        "• Стабильная работа\n"
        "• 3 устройства на одной подписке\n"
        "• Демократичная цена\n"
        "• Система скидок\n"
        "• Реферальная система\n"
        "(Приводите друга, вам и ему по месяцу в подарок!)\n"
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
    username = (call.from_user.username or "no_username").lower().replace("@", "")

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action == "1":
        ask_referral_before_payment(tg_id, username, months=1, flow="new")
    elif action == "multi":
        # Инициализируем заранее
        pending_requests[tg_id] = {"flow": "new", "username": username}
        msg = bot.send_message(tg_id, "📅 Введите количество месяцев (1–12):")
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow="new")

    bot.answer_callback_query(call.id)

# Запрос рефералки
def ask_referral_before_payment(tg_id, username, months, flow):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Есть рефералка", callback_data=f"has_ref:{months}:{flow}"),
        types.InlineKeyboardButton("❌ Нет", callback_data=f"no_ref:{months}:{flow}")
    )
    bot.send_message(
        tg_id,
        "🎟 У вас есть реферальный код?",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith(("has_ref:", "no_ref:")))
def handle_referral_choice(call):
    tg_id = call.from_user.id
    data = call.data.split(":")
    has_ref = data[0] == "has_ref"
    months = int(data[1])
    flow = data[2]
    username = (call.from_user.username or "no_username").lower().replace("@", "")

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if has_ref:
        msg = bot.send_message(tg_id, "Введите реферальный код:")
        bot.register_next_step_handler(msg, process_referral_input, tg_id, username, months, flow)
    else:
        send_invoice(tg_id, username, months, flow, referral_code=None)

    bot.answer_callback_query(call.id)

def process_referral_input(message, tg_id, username, months, flow):
    code = message.text.strip().upper()
    referrer_uid, referrer_data = find_user_by_referral_code(code)

    if not referrer_uid:
        bot.send_message(tg_id, "❌ Реферальный код не найден.\nПопробуйте ещё раз или нажмите «Нет».")
        bot.register_next_step_handler(message, process_referral_input, tg_id, username, months, flow)
        return

    # Инициализируем запись, если её ещё нет
    if tg_id not in pending_requests:
        pending_requests[tg_id] = {
            "flow": flow,
            "months": months,
            "username": username
        }

    pending_requests[tg_id]["referrer_uid"] = referrer_uid

    # Теперь можно отправлять счёт
    send_invoice(tg_id, username, months, flow, code)

    bot.send_message(tg_id, "✅ Реферальный код принят. Переходим к оплате...")



# Обработчик отмены оплаты
@bot.callback_query_handler(func=lambda call: call.data == "cancel_payment")
def cancel_payment(call):
    tg_id = call.from_user.id
    pending_requests.pop(tg_id, None)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    # Вызываем /start как будто пользователь нажал старт заново
    fake_message = types.Message(
        message_id=0,
        from_user=call.from_user,
        chat=call.message.chat,
        date=datetime.now(),
        content_type='text',
        options={},
        json_string='{"text": "/start"}'
    )
    fake_message.text = "/start"

    bot.send_message(tg_id, "❌ Оплата отменена.")

    start_handler(fake_message)



# Универсальная обработка успешной оплаты
def process_successful_payment(tg_id: int, months: int, flow: str = "new", referrer_uid: str = None):
    data = pending_requests.get(tg_id, {})
    username = data.get("username", "no_username")
    amount = data.get("amount", 0) * 100

    uid = get_or_create_uid(tg_id)

    print(f"Обработка платежа: flow={flow}, months={months}, tg_id={tg_id}, uid={uid}")

    if flow == "new":
        final_months = months
        if referrer_uid:
            final_months += 1

        success, error_msg, base_name, expiry_ms, sub_id = create_vpn_client(uid, tg_id, username, final_months)

        if success:
            ref_code = save_user(uid, tg_id, base_name, username, "approved", expiry_ms, sub_id)

            # Бонус за рефералку
            if referrer_uid:
                give_referral_bonus(referrer_uid, str(uid), months=1)

            sub_link = f"{XUI_SUB_LINK}/{sub_id}"

            admin_notify(tg_id, username, base_name, months, amount, "Новая подписка", referrer_uid)
            instruction_send(tg_id)

            safe_send_message(tg_id,
                "🎉 <b>Подписка успешно активирована!</b>\n\n"
                f"🔗 <b>Ваша ссылка на подписку:</b>\n\n"
                f"<code>{sub_link}</code>\n\n"
                "Переходить по ссылке не нужно, ее необходимо скопировать и вставить в приложение.\n\n"
                f"🎟 Ваш реферальный код:\n"
                f"<code>{ref_code}</code>\n"
                f"<b>Зовите друзей, получайте по месяцу с каждого!</b>\n\n"
                "🎉 Добро пожаловать в VidjetVPN!\n\n"
                "👇 Подписывайтесь на группу, чтобы быть в курсе новостей:\n"
                f"🏴‍☠{GRUPP}",
                reply_markup=main_menu()
            )
        else:
            safe_send_message(tg_id, f"❌ Ошибка активации подписки.\nЕсли вы уверены, что оплата прошла — напишите в поддержку: {SUPPORT}")
            bot.send_message(ADMIN_ID, f"⚠️ Ошибка создания пользователя!\nTG: @{username} ({tg_id})\nОшибка: {error_msg}")

    else:  # renew
        success, error_msg, email, expiry_ms = renew_vpn_client(uid, tg_id, username, months)
        if success:
            save_user(uid, tg_id, email, username, "approved", expiry_ms)
            admin_notify(tg_id, username, email, months, amount, "Продление")
            safe_send_message(tg_id, f"🔄 <b>Подписка успешно продлена на {months} месяцев!</b>", reply_markup=main_menu())
            sub(tg_id)  # покажет актуальную информацию
            print(f"✅ Успешное продление для tg_id={tg_id}")
        else:
            print(f"❌ Ошибка продления: {error_msg}")
            safe_send_message(tg_id, f"❌ Ошибка продления подписки.\nНапишите в поддержку: {SUPPORT}")
            bot.send_message(ADMIN_ID, f"⚠️ Ошибка продления!\nTG: @{username} ({tg_id})\nUID: {uid}\nОшибка: {error_msg}")

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
    username = (message.from_user.username or "no_username").lower().replace("@", "")

    # 2. Ищем по username (пользователь добавлен админом)
    if username and username != "no_username":
        uid_by_name, user_by_name = get_user_by_username(username)
        if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

            # Обновляем tgId
            update_tg_id(uid_by_name, tg_id, username)

    # Показываем информацию о подписке
    sub(tg_id, message)



# ====================== Реакция на кнопку "Рефералка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "🎟 Рефералка")
def referral_handler(message):
    tg_id = message.from_user.id
    username = (message.from_user.username or "no_username").lower().replace("@", "")

    # 2. Ищем по username (пользователь добавлен админом)
    if username and username != "no_username":
        uid_by_name, user_by_name = get_user_by_username(username)
        if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

            # Обновляем tgId
            update_tg_id(uid_by_name, tg_id, username)

    show_referral(tg_id)

def show_referral(tg_id):
    uid, user_data = get_user_by_tg_id(tg_id)
    if not user_data or not user_data.get("referral_code"):
        bot.send_message(tg_id, "❌ Реферальный код не найден.", reply_markup=main_menu())
        return

    if user_data.get("expiry_time") == 0:   # Бессрочный
        bot.send_message(
            tg_id,
            "♾ <b>Бессрочным пользователям реферальная система недоступна.</b>\n\n"
            "Вы уже имеете максимальный статус подписки🎉🎉🎉",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        return

    code = user_data["referral_code"]

    invite_text = (
        "\n\n🔥 Присоединяйся к\n\n  🏴‍☠️VidjetVPN🏴‍☠️\n\n"

        "• Обход блокировок\n"
        "• Без ограничений по скорости и трафику\n"
        f"• {XUI_CLIENT_LIMIT_IP} {device_ru()} на одной подписке\n"
        "• Смешные цены и система скидок\n"
        "• Реферальная система\n\n"

        f"Мой реферальный код:\n\n"

        f"{code}\n\n"

        "При регистрации и оплате по коду — получишь +1 месяц в подарок! 🎁"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👥 Пригласить друга", switch_inline_query=invite_text))

    bot.send_message(
        tg_id,
        f"🎟 <b>Ваш реферальный код:</b>\n\n"
        f"<code>{code}</code>\n\n"
        "Приведите друга — получите +1 месяц бесплатно для себя и для друга в подарок!\n\n"
        "Поделитесь кодом с друзьями:",
        parse_mode="HTML",
        reply_markup=markup
    )



# ====================== Реакция на кнопку "Продлить подписку"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "🔄 Продлить подписку")
def renew_handler(message):
    tg_id = message.from_user.id
    username = (message.from_user.username or "no_username").lower().replace("@", "")

    # 2. Ищем по username (пользователь добавлен админом)
    if username and username != "no_username":
        uid_by_name, user_by_name = get_user_by_username(username)
        if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

            # Обновляем tgId
            update_tg_id(uid_by_name, tg_id, username)

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
    username = (call.from_user.username or "no_username").lower().replace("@", "")

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action == "1":
        send_invoice(tg_id, username, months=1, flow="renew")
    elif action == "multi":
        msg = bot.send_message(tg_id, "📅 Введите количество месяцев (1–12):")
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow="renew")

    bot.answer_callback_query(call.id)



# ====================== Реакция на кнопку "Инструкция"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📑 Инструкция")
def instruction_handler(message):
    tg_id = message.from_user.id
    username = (message.from_user.username or "no_username").lower().replace("@", "")

    # 2. Ищем по username (пользователь добавлен админом)
    if username and username != "no_username":
        uid_by_name, user_by_name = get_user_by_username(username)
        if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

            # Обновляем tgId
            update_tg_id(uid_by_name, tg_id, username)

    instruction_send(tg_id)



# ====================== Реакция на кнопку "Поддержка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📩 Поддержка")
def support_handler(message):
    tg_id = message.from_user.id
    username = (message.from_user.username or "no_username").lower().replace("@", "")

    # 2. Ищем по username (пользователь добавлен админом)
    if username and username != "no_username":
        uid_by_name, user_by_name = get_user_by_username(username)
        if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

            # Обновляем tgId
            update_tg_id(uid_by_name, tg_id, username)

    bot.send_message(tg_id, f"📩 Поддержка\n👤 Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее.", reply_markup=main_menu())



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

    messages = []
    messages.append(f"👥 Пользователи — {filter_type.upper()}\n\n{'─' * 18}\n\n")
    count = 0
    current = ""

    for key, info in users.items():
        uid = key
        tg_id = info.get("tg_id", "-")
        email = info.get("email", "-")
        username = info.get("username", "no_username")
        status = info.get("status", "unknown")
        expiry = info.get("expiry_time", 0)
        sub_id = info.get("sub_id", "-")
        ref_code = info.get("referral_code", "—")

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
        if email and email in online_clients:
            online = True

        traffic_data = all_traffic.get(email, {})
        up = round(traffic_data.get("up", 0) / (1024**3), 2)
        down = round(traffic_data.get("down", 0) / (1024**3), 2)

        user_block = (
            f"🆔 <b>UID:</b> <code>{uid}</code>\n"
            f"👤 <b>TG ID:</b> <code>{tg_id}</code>\n"
            f"💬 <b>User:</b> @{username}\n"
            f"📧 <b>Email:</b> <code>{email}</code>\n\n"

            f"🔑 <b>Ref код:</b> <code>{ref_code}</code>\n\n"

            f"📊 <b>Status:</b> {'🟢 Online' if online else '🔴 Offline'}\n\n"

            f"⬆️ <b>Upload:</b> {up} GB\n"
            f"⬇️ <b>Download:</b> {down} GB\n\n"

            f"⏳ <b>Expire:</b> {expiry_date}\n\n"

            f"🔗 <b>SUB LINK:</b>\n"
            f"<code>{XUI_SUB_LINK}/{sub_id}</code>\n\n"

            f"{'─' * 18}\n\n"
        )

        if len(current) + len(user_block) > 4000:
            messages.append(current)
            current = ""

        current += user_block

    if current:
        messages.append(current)

    for msg in messages:
        bot.send_message(
            message.chat.id,
            msg,
            parse_mode="HTML"
        )



# Запрос онлайн клиентов у сервера
def get_online_clients():
    try:
        r = requests.post(
            f"{XUI_URL}/panel/api/clients/onlines",
            headers=headers,
            timeout=15
        )
        if r.status_code == 200 and r.json().get("success"):
            return set(r.json().get("obj", []))
    except Exception as e:
        print(f"Online check error: {e}")
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
        ref_code = save_user(uid, tg_id, email, admin_given_username, "approved", expiry_ms, sub_id, referral_code=None)

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
            f"🎟 <b>Реф код:</b> <code>{ref_code}</code>\n\n"
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
    try:
        email = users[target_key].get("email")
    except Exception:
        return False, f"Пользователь с UID {uid} не найден"

    if not email:
        return False, "Email не найден"

    try:
        r = requests.post(
            f"{XUI_URL}/panel/api/clients/del/{email}",
            headers=headers,
            timeout=15
        )

        if r.status_code == 200 and r.json().get("success"):
            # Удаляем из users.json
            del users[target_key]
            with open("users.json", "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=4)

            return True, f"Клиент {email} успешно удалён (UID: {uid})"
        else:
            error = r.json().get("msg", r.text)
            return False, f"Ошибка удаления: {error}"

    except Exception as e:
        return False, f"Exception: {str(e)}"



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
        get_servers_status(),
        parse_mode="HTML"
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
                "🌐 <b>CENTRAL SERVER</b>\n"
                f"{'─' * 18}\n\n"
                f"🧠 CPU: <b>{s.get('cpu', 'N/A')}%</b>\n"
                f"💾 RAM: <b>{round(s['mem']['current']/1024**3, 2)}</b> / "
                f"<b>{round(s['mem']['total']/1024**3, 2)} GB</b>\n"
                f"🔌 TCP: <b>{s.get('tcpCount', 'N/A')}</b>\n"
                f"⚙️ XRAY: <b>{s['xray'].get('state', 'N/A')}</b>\n\n"
                f"{'─' * 18}\n\n"

            )
    except Exception as e:
        text += (
            "🌐 <b>CENTRAL SERVER</b>\n"
            f"{'─' * 18}\n"
            f"❌ <b>Error:</b> <code>{e}</code>\n\n"
            f"{'─' * 18}\n\n"
        )

    # ==================== NODES ====================
    try:
        r = requests.get(f"{XUI_URL}/panel/api/nodes/list", headers=headers, timeout=15)

        if r.status_code != 200 or not r.json().get("success"):
            text += "🖥 <b>NODES</b>\n❌ Не удалось получить список нод\n"
            return text

        nodes = r.json()["obj"]

        if not nodes:
            text += "🖥 <b>NODES</b>\nНет подключённых нод\n"
            return text

        text += f"🖥 <b>NODES STATUS</b> — {len(nodes)} нод\n\n"
        text += f"{'─' * 20}\n"

        for node in nodes:
            name = node.get("name", "Unknown")
            status = node.get("status", "unknown")
            cpu_pct = node.get("cpuPct")
            mem_pct = node.get("memPct")
            latency = node.get("latencyMs")
            uptime_secs = node.get("uptimeSecs", 0)
            online_count = node.get("onlineCount", 0)

            cpu_str = f"{round(cpu_pct, 1)}%" if isinstance(cpu_pct, (int, float)) else "N/A"
            mem_str = f"{round(mem_pct, 1)}%" if isinstance(mem_pct, (int, float)) else "N/A"
            uptime_days = uptime_secs // 86400

            status_emoji = "🟢" if status == "online" else "🔴"

            text += (
                f"<b>{name}</b>\n"
                f"Статус: {status_emoji} <b>{'Online' if status == 'online' else 'Offline'}</b>\n"
                f"🧠 CPU: <b>{cpu_str}</b>\n"
                f"💾 RAM: <b>{mem_str}</b>\n"
                f"📶 Latency: <b>{latency} ms</b>\n"
                f"⏱ Uptime: <b>{uptime_days} days.</b>\n"
                f"👥 Online: <b>{online_count}</b>\n\n"
                f"{'─' * 20}\n\n"
            )

    except Exception as e:
        text += f"🖥 <b>NODES ERROR:</b> <code>{str(e)[:300]}</code>\n"

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



# =========================
# WEBHOOK YooKassa
# =========================

app = Flask(__name__)

@app.route(f"/{PAY_WEBHOOK}", methods=['POST'])
@app.route(f"/{PAY_WEBHOOK}/", methods=['POST'])
def yookassa_webhook():
    print("🔥 WEBHOOK RECEIVED!")
    print("Headers:", dict(request.headers))

    if not request.is_json:
        print("❌ Request is not JSON")
        return jsonify({"status": "error"}), 400

    try:
        event = request.get_json()
        print("WEBHOOK RAW DATA:", json.dumps(event, indent=2, ensure_ascii=False))

        event_type = event.get('event')
        if event_type != 'payment.succeeded':
            print(f"ℹ️ Ignored event: {event_type}")
            return jsonify({"status": "ok"}), 200

        payment = event.get('object', {})
        payment_id = payment.get('id')
        metadata = payment.get('metadata', {})

        tg_id_str = metadata.get('tg_id')
        months = int(metadata.get('months', 1))
        flow = metadata.get('flow', 'new')
        username = metadata.get('username', 'no_username')

        print(f"✅ SUCCESSFUL PAYMENT: tg_id={tg_id_str}, months={months}, flow={flow}, username={username}")

        if not tg_id_str:
            print("❌ No tg_id in metadata")
            return jsonify({"status": "error"}), 200

        tg_id = int(tg_id_str)

        referrer_uid = metadata.get("referrer_uid")

        # Обработка платежа
        process_successful_payment(tg_id, months, flow, referrer_uid)

        # Сохраняем как обработанный
        save_processed_payment(payment_id)

        print(f"🎉 Payment processed successfully for user {tg_id}")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ WEBHOOK CRITICAL ERROR: {e}")
        traceback.print_exc()
        return jsonify({"status": "ok"}), 200  # YooKassa требует 200

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

        # Запускаем Flask webhook в отдельном потоке
        def run_flask():
            app.run(host='127.0.0.1', port=FLASK_PORT, debug=False)

        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print(f"🌐 Flask webhook сервер запущен на http://127.0.0.1:{FLASK_PORT}")

        bot.infinity_polling(skip_pending=True)
    except Exception as e:
        print(f"Fatal error: {e}")
