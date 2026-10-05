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
import html
import logging
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

# ====================== ЛОГИРОВАНИЕ ======================
#
# [FIX] Под systemd stdout — это pipe, а не терминал, и Python буферизует его блоками:
# print() копился в памяти и в `systemctl status` / journalctl не появлялся.
# Теперь всё пишется через logging:
#   • в файл logs/bot.log в папке бота (ротация: 5 файлов по 5 МБ);
#   • в консоль — модуль logging сбрасывает её после каждой записи, поэтому journal видит всё сразу.
# print() и traceback.print_exc() (в том числе в нетронутых блоках ЮKassa) перехватываются
# и тоже попадают в лог, построчно.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

log = logging.getLogger("vidjet")


class _StreamToLogger(io.TextIOBase):
    """
    Текстовый поток: всё, что в него пишут, уходит в logger построчно.

    Ведёт себя как настоящий sys.stdout — это важно для click (через него Flask печатает
    баннер «Serving Flask app»). click определяет тип потока пробной записью write(b""):
    у текстового потока она обязана упасть с TypeError, иначе click решает, что поток
    бинарный, и начинает писать в него bytes. Также click требует атрибуты encoding/errors.
    """

    def __init__(self, logger, level, original):
        super().__init__()
        self._logger = logger
        self._level = level
        self._original = original
        self._local = threading.local()

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def writable(self):
        return True

    def readable(self):
        return False

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        if not text:
            return 0
        # Защита от рекурсии: если ошибка случилась внутри самого logging,
        # он пишет в sys.stderr — отдаём такой текст в настоящий поток.
        if getattr(self._local, "busy", False):
            self._original.write(text)
            return len(text)
        self._local.busy = True
        try:
            buf = getattr(self._local, "buf", "") + text
            *lines, rest = buf.split("\n")
            self._local.buf = rest
            for line in lines:
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        finally:
            self._local.busy = False
        return len(text)

    def flush(self):
        rest = getattr(self._local, "buf", "")
        if rest.strip() and not getattr(self._local, "busy", False):
            self._local.buf = ""
            self._local.busy = True
            try:
                self._logger.log(self._level, rest.rstrip())
            finally:
                self._local.busy = False

    def isatty(self):
        return False

    def fileno(self):
        return self._original.fileno()


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.__stdout__)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # У telebot свой обработчик в stderr — убираем его, иначе каждая строка в journal
    # будет дублироваться. Записи telebot по-прежнему попадают в наш файл и консоль.
    tb_logger = logging.getLogger("TeleBot")
    for handler in list(tb_logger.handlers):
        tb_logger.removeHandler(handler)
    tb_logger.propagate = True

    sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO, sys.__stdout__)
    sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.ERROR, sys.__stderr__)

    def _thread_excepthook(args):
        name = args.thread.name if args.thread else "?"
        log.error("Необработанное исключение в потоке %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def _sys_excepthook(exc_type, exc_value, exc_tb):
        log.critical("Необработанное исключение", exc_info=(exc_type, exc_value, exc_tb))

    threading.excepthook = _thread_excepthook
    sys.excepthook = _sys_excepthook


setup_logging()

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

# [FIX] Flow для VLESS-клиентов. Раньше был зашит только в create_vpn_client,
# а при апдейтах подставлялось то, что вернул GET (а он может вернуть "").
# Параметр НЕОБЯЗАТЕЛЬНЫЙ: без него в API.conf используется xtls-rprx-vision.
# Пустое значение (XUI_CLIENT_FLOW = "") — бот вообще не трогает flow.
# Панель сама вырезает flow на inbound'ах, где он не поддерживается
# (clientWithInboundFlow в исходниках 3x-ui), поэтому слать его на все inbound'ы безопасно.
XUI_CLIENT_FLOW = config.get('3XUI', 'XUI_CLIENT_FLOW', fallback='xtls-rprx-vision').strip().strip('"')

headers = {
    "Authorization": f"Bearer {XUI_API_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# Инициализируем бота
bot = telebot.TeleBot(API_TOKEN,
                      parse_mode=None,
                      disable_web_page_preview=False)

# [FIX] Убран bot.enable_save_next_step_handlers(delay=2).
# Это НЕ таймауты: метод включает FileHandlerBackend, который каждые 2 секунды
# пиклит next-step хендлеры в ./.handler-saves/step.save. Без load_next_step_handlers()
# при старте эти сохранения никогда не читались — была только лишняя запись на диск.

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
pending_requests = {}
user_ids = {}
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.lock')
uid_counter = 1
admin_given_email = None
admin_given_username = None
admin_renew_uid = 0

# [FIX] users.json читают/пишут одновременно: пул потоков telebot (num_threads=2 по умолчанию),
# поток Flask с вебхуком и поток рассылки. Без блокировки возможны потерянные записи
# (один поток прочитал файл, второй записал, первый затёр его своей старой копией).
USERS_FILE = "users.json"
USERS_LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()



# =======================================================
# ====================== ФУНКЦИИ =======================
# =======================================================



# ====================== users.json (потокобезопасно) =======================

def load_users_db() -> dict:
    with USERS_LOCK:
        if not os.path.exists(USERS_FILE):
            return {}
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_users_db(data: dict):
    # Атомарная запись: сначала во временный файл, потом rename.
    # Если процесс упадёт посреди записи, users.json не окажется обрезанным.
    with USERS_LOCK:
        tmp_path = USERS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, USERS_FILE)


def update_user_record(uid, **fields) -> bool:
    """Перечитывает users.json под блокировкой и меняет только переданные поля."""
    with USERS_LOCK:
        users = load_users_db()
        record = users.get(str(uid))
        if record is None:
            return False
        record.update(fields)
        save_users_db(users)
        return True



# ====================== Работа с 3x-ui =======================
#
# Всё ниже сверено с исходниками 3x-ui v3 (internal/web/controller/client.go,
# internal/web/service/client_crud.go, client_lookup.go):
#
# * GET  /panel/api/clients/get/{email} возвращает {"client": ClientRecord, "inboundIds": [...]}.
#   В ClientRecord поле "id" — это ПЕРВИЧНЫЙ КЛЮЧ записи в БД (int), а VLESS-UUID лежит в "uuid".
#   Поле "flow" — это EffectiveFlow: flow первого inbound'а, где он не пустой; если клиент ни к
#   одному flow-совместимому inbound'у не привязан — придёт "".
# * POST /panel/api/clients/update/{email} принимает model.Client, где "id" — это VLESS-UUID.
#   Это полная замена полей (не частичный патч): что не передано — обнуляется
#   (кроме id/password/auth/secret — их панель сохраняет, если они пустые).
# * POST /panel/api/clients/{email}/attach берёт flow не из тела запроса, а из EffectiveFlow.
#   Уже привязанные inbound'ы пропускаются без ошибки.

def _xui_email_path(email) -> str:
    return quote(str(email), safe="")


SLOW_REQUEST_SEC = 5


def _xui_call(method: str, path: str, **kwargs):
    """
    Запрос к 3x-ui с замером времени. Медленные ответы (≥ SLOW_REQUEST_SEC) пишутся
    в лог предупреждением — по ним видно, где тормозит панель или ноды.
    """
    t0 = time.monotonic()
    try:
        return getattr(requests, method.lower())(f"{XUI_URL}{path}", headers=headers, **kwargs)
    finally:
        elapsed = time.monotonic() - t0
        if elapsed >= SLOW_REQUEST_SEC:
            log.warning("3x-ui %s %s — медленный ответ: %.1f с", method.upper(), path, elapsed)
        else:
            log.debug("3x-ui %s %s — %.2f с", method.upper(), path, elapsed)


def _xui_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"success": False, "msg": str(data)[:300]}
    except ValueError:
        return {"success": False, "msg": f"HTTP {resp.status_code}: {resp.text[:300]}"}


def xui_get_client(email):
    """(True, {"client": {...}, "inboundIds": [...]}) или (False, текст ошибки)."""
    try:
        r = _xui_call("GET", f"/panel/api/clients/get/{_xui_email_path(email)}", timeout=15)
        data = _xui_json(r)
        if r.status_code == 200 and data.get("success") and isinstance(data.get("obj"), dict):
            return True, data["obj"]
        return False, data.get("msg") or f"HTTP {r.status_code}"
    except Exception as e:
        return False, repr(e)


def xui_attach(email, inbound_ids):
    try:
        r = _xui_call(
            "POST", f"/panel/api/clients/{_xui_email_path(email)}/attach",
            json={"inboundIds": list(inbound_ids)},
            timeout=30
        )
        data = _xui_json(r)
        if r.status_code == 200 and data.get("success"):
            return True, ""
        return False, data.get("msg") or f"HTTP {r.status_code}"
    except Exception as e:
        return False, repr(e)


def xui_update_client(email, payload: dict):
    try:
        r = _xui_call(
            "POST", f"/panel/api/clients/update/{_xui_email_path(email)}",
            json=payload,
            timeout=30
        )
        data = _xui_json(r)
        if r.status_code == 200 and data.get("success"):
            return True, ""
        return False, data.get("msg") or f"HTTP {r.status_code}"
    except Exception as e:
        return False, repr(e)


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")


def _is_valid_uuid(value) -> bool:
    # Xray принимает UUID как с дефисами, так и 32 hex-символа подряд —
    # оба варианта считаем нормальными, перевыпускаем только «17», «abc» и т.п.
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value.strip()))


# Поля ClientRecord, которые нельзя (или не нужно) отправлять в update как есть
_RECORD_ONLY_KEYS = ("id", "uuid", "createdAt", "updatedAt", "allowedIPs", "reverse")


def _client_payload_from_record(rec: dict) -> dict:
    """
    [FIX] Превращает ClientRecord (ответ GET) в model.Client (тело update).

    Главное: раньше бот делал client["id"] = str(client["id"]) — то есть отправлял
    первичный ключ из БД ("17") в поле, которое панель считает VLESS-UUID.
    Панель честно записывала UUID = "17". Теперь в "id" кладётся настоящий "uuid".
    Остальные поля передаются как есть (update — полная замена, нельзя их терять:
    limitHwid, group, comment, reset* и т.д.).
    """
    payload = {k: v for k, v in rec.items() if k not in _RECORD_ONLY_KEYS}

    vless_uuid = rec.get("uuid")
    if vless_uuid:
        payload["id"] = vless_uuid

    # В ClientRecord allowedIPs — строка, в model.Client — массив строк.
    # Пустую строку не шлём (иначе ошибка разбора JSON), для WireGuard панель
    # сама подставит сохранённое значение.
    raw_ips = rec.get("allowedIPs")
    ips = []
    if isinstance(raw_ips, list):
        ips = [str(x) for x in raw_ips if x]
    elif isinstance(raw_ips, str) and raw_ips.strip():
        try:
            parsed = json.loads(raw_ips)
            ips = parsed if isinstance(parsed, list) else [raw_ips]
        except json.JSONDecodeError:
            ips = [x.strip() for x in raw_ips.split(",") if x.strip()]
    if ips:
        payload["allowedIPs"] = ips

    if isinstance(rec.get("reverse"), dict):
        payload["reverse"] = rec["reverse"]

    return payload


def xui_apply_client_state(email, *, expiry_ms=None, enable=None, tg_id=None,
                           ensure_inbounds=True, regenerate_uuid=False, prefetched=None):
    """
    [FIX] Единая точка изменения клиента в 3x-ui.
    Возвращает (ok, error_text, attach_error_text).

    Порядок важен:
      1. attach недостающих inbound'ов из XUI_INBOUND_IDS;
      2. update с явно заданными flow / enable / expiryTime.
    Update применяется только к inbound'ам, привязанным на момент вызова, а attach
    берёт flow из EffectiveFlow (у полностью отвязанного клиента он пустой).
    Поэтому сначала привязываем, потом одним update выставляем flow и enable
    сразу на всех inbound'ах, включая только что привязанные.
    """
    if prefetched is None:
        ok, obj = xui_get_client(email)
        if not ok:
            return False, f"get: {obj}", ""
    else:
        obj = prefetched

    rec = obj.get("client") or {}
    attach_err = ""

    if ensure_inbounds:
        missing = sorted(set(XUI_INBOUND_IDS) - set(obj.get("inboundIds") or []))
        if missing:
            ok, err = xui_attach(email, missing)
            if ok:
                log.info(f"✅ Привязан {email} → добавил {missing}")
            else:
                attach_err = err
                log.warning(f"⚠️ Не удалось привязать {email} к {missing}: {err}")

    payload = _client_payload_from_record(rec)
    if XUI_CLIENT_FLOW:
        payload["flow"] = XUI_CLIENT_FLOW
    if expiry_ms is not None:
        payload["expiryTime"] = int(expiry_ms)
    if enable is not None:
        payload["enable"] = bool(enable)
    if tg_id is not None:
        payload["tgId"] = int(tg_id)
    if regenerate_uuid:
        payload["id"] = str(uuid.uuid4())

    ok, err = xui_update_client(email, payload)
    if not ok:
        return False, f"update: {err}", attach_err
    return True, "", attach_err


def _calc_new_expiry(current_expiry, months: int) -> int:
    if months == 0:
        return 0
    now_ms = int(datetime.now().timestamp() * 1000)
    try:
        current = int(current_expiry or 0)
    except (TypeError, ValueError):
        current = 0
    base_time = current if current > now_ms else now_ms
    return base_time + XUI_EXPIRY_DAYS * months * 24 * 60 * 60 * 1000


def xui_extend_client(email, months: int):
    """Продлевает клиента от текущей даты в панели. (ok, err, new_expiry, attach_err)"""
    ok, obj = xui_get_client(email)
    if not ok:
        return False, f"Client not found: {obj}", None, ""

    rec = obj.get("client") or {}
    new_expiry = _calc_new_expiry(rec.get("expiryTime"), months)

    ok, err, attach_err = xui_apply_client_state(
        email, expiry_ms=new_expiry, enable=True, prefetched=obj
    )
    if not ok:
        return False, err, None, attach_err
    return True, "", new_expiry, attach_err


def notify_admin_attach_problem(email, attach_err, context="Продление"):
    try:
        bot.send_message(
            ADMIN_ID,
            f"⚠️ {html.escape(context)} прошло, но не удалось привязать inbound'ы\n"
            f"Email: <code>{html.escape(str(email))}</code>\n"
            f"Ошибка: <code>{html.escape(str(attach_err)[:300])}</code>\n\n"
            f"Запустите «🔄 Синхронизировать пользователей» или привяжите вручную.",
            parse_mode="HTML"
        )
    except Exception:
        pass


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
        "flow": XUI_CLIENT_FLOW,
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
        data = _xui_json(r)

        if r.status_code == 200 and data.get("success"):
            log.info(f"✅ Клиент создан: {base_name} | Прикреплён к {len(XUI_INBOUND_IDS)} inbound'ам")
            return True, "", base_name, expiry_ms, sub_id
        else:
            error = data.get("msg") or r.text
            log.error(f"❌ Ошибка создания клиента: {error}")
            return False, error, base_name, expiry_ms, sub_id

    except Exception as e:
        log.exception(f"❌ Exception при создании клиента: {e}")
        return False, str(e), base_name, expiry_ms, sub_id



# Продление клиента в 3x-ui
def renew_vpn_client(uid: int, tg_id: str = None, username: str = None, months: int = 1):
    try:
        users = load_users_db()

        # [FIX] Сначала ищем по uid (он однозначен), потом по tg_id
        uid_key, user_data = None, None
        if uid and str(uid) in users:
            uid_key, user_data = str(uid), users[str(uid)]
        elif tg_id and str(tg_id) != "by_admin":
            uid_key, user_data = get_user_by_tg_id(tg_id)

        if not user_data or not user_data.get("email"):
            error = f"Email пользователя не найден (uid={uid}, tg_id={tg_id})"
            log.error(f"❌ {error}")
            return False, error, None, None

        base_email = user_data["email"]

        ok, err, new_expiry, attach_err = xui_extend_client(base_email, months)
        if not ok:
            log.error(f"❌ Ошибка продления {base_email}: {err}")
            return False, err, base_email, None

        if attach_err:
            notify_admin_attach_problem(base_email, attach_err)

        update_user_record(uid_key, expiry_time=new_expiry)

        log.info(f"✅ Подписка продлена: {base_email} → {new_expiry}")
        return True, "", base_email, new_expiry

    except Exception as e:
        log.exception(f"❌ Ошибка продления: {e}")
        return False, str(e), None, None



# Обновление tg_id после привязки пользователя
def update_tg_id(uid: str, tg_id: int, username: str = "no_username"):
    try:
        users = load_users_db()
        user_data = users.get(str(uid))
        if not user_data:
            return False, "User not found"

        base_email = user_data.get("email")
        if not base_email:
            return False, "Email not found"

        # Обновляем в 3x-ui (через общий хелпер: правильный UUID и flow)
        ok, err, _ = xui_apply_client_state(base_email, tg_id=int(tg_id), ensure_inbounds=False)
        if not ok:
            log.warning(f"⚠️ Не удалось обновить tgId в 3x-ui: {err}")

        # Обновляем в users.json
        update_user_record(uid, tg_id=str(tg_id), username=username)

        log.info(f"✅ tg_id успешно обновлён для UID {uid} → {tg_id}")
        return True, None

    except Exception as e:
        log.exception(f"❌ Ошибка update_tg_id: {e}")
        return False, str(e)



# ====================== Работа с файлами =======================

# Получение uid пользователя
def get_or_create_uid(tg_id=None):
    global uid_counter

    with USERS_LOCK:
        users = load_users_db()

        # Ищем существующего пользователя по tg_id
        if tg_id is not None:
            for uid, data in users.items():
                if str(data.get("tg_id")) == str(tg_id):
                    return int(uid)

        # [FIX] Создаём нового под блокировкой: два одновременных платежа
        # (Flask многопоточный) раньше могли получить один и тот же uid.
        max_uid = max((int(k) for k in users if str(k).isdigit()), default=0)
        uid = max(uid_counter, max_uid + 1)
        uid_counter = uid + 1

        return uid



# Сохранение нового пользователя в файл
def save_user(uid, tg_id, email=None, username=None, status="approved", expiry_time=None, sub_id=None, referral_code=None):
    with USERS_LOCK:
        data = load_users_db()

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
            # [FIX] возвращаем реальный код существующего пользователя, а не None
            referral_code = current.get("referral_code")

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

        save_users_db(data)

    return referral_code



# ====================== Работа с tg =======================

#Предотвращение дублирующих запусков
def acquire_lock():
    lock_file = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log.error("Bot already running!")
        sys.exit(1)
    return lock_file



def norm_username(user) -> str:
    return (getattr(user, "username", None) or "no_username").lower().replace("@", "")



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
            log.warning(f"Попытка {attempt+1}/{max_retries} отправки сообщения не удалась: {e}")
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))  # увеличиваем задержку
            else:
                log.warning(f"Не удалось отправить сообщение пользователю {chat_id} после {max_retries} попыток")
                return None



# Функция подгрузки tg пользователей
def load_users():
    global user_ids, uid_counter
    data = load_users_db()
    if not data:
        uid_counter = 1
        return

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
    tg_id = str(tg_id)  # приводим к строке
    users = load_users_db()

    for uid_key, user_data in users.items():
        saved_tg = user_data.get("tg_id")
        if saved_tg is not None and str(saved_tg) == tg_id:
            return uid_key, user_data

    return None, None



# Поиск пользователя по username
def get_user_by_username(username):
    username = (username or "").lower().replace("@", "").strip()
    if not username:
        return None, None

    users = load_users_db()

    for uid_key, user_data in users.items():
        # [FIX] username может оказаться null в users.json → .lower() падал
        saved_username = (user_data.get("username") or "").lower().replace("@", "").strip()
        if saved_username == username:
            return uid_key, user_data

    return None, None



# Привязка tg_id к пользователю, которого админ добавил по username
def link_admin_added_user(user):
    username = norm_username(user)
    if username == "no_username":
        return
    uid_by_name, user_by_name = get_user_by_username(username)
    if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":
        update_tg_id(uid_by_name, user.id, username)



# Информация о подписке
def sub(tg_id, message=None):
    try:
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
        log.exception(f"Ошибка в sub(): {e}")
        try:
            bot.send_message(tg_id, "❌ Не удалось загрузить информацию о подписке.", reply_markup=main_menu())
        except:
            pass



# Обработка продления на несколько месяцев
def process_months_input(message, tg_id, flow = "new"):
    if step_interrupted(message):
        return
    try:
        months = int((message.text or "").strip())
        if months < 1 or months > 12:
            msg = bot.send_message(tg_id, "❌ Простите, мы пока не оформляем подписки дольше чем на год. Введите другое число месяцев.")
            bot.register_next_step_handler(msg, process_months_input, tg_id, flow)
            return
        username = norm_username(message.from_user)
        if flow == "renew":
            send_invoice(tg_id, username, months, flow)
        else:
            ask_referral_before_payment(tg_id, username, months, flow)

    except ValueError:
        # Если ввели не число
        msg = bot.send_message(
            tg_id,
            "❌ Пожалуйста, введите <b>число</b> (например: 3)",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow)

    except Exception as e:
        log.exception(f"Ошибка process_months_input: {e}")
        msg = bot.send_message(tg_id, "❌ Введите корректное число")
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow)



# Отправляет уведомление админу об успешной оплате
def admin_notify(tg_id: int, username: str, email: str, months: int, amount: int, payment_type: str, referrer_uid: str = None):
    text = (
        f"💰 <b>Новая оплата</b>\n\n"
        f"Пользователь: @{html.escape(str(username))} ({tg_id})\n"
        f"Email: <code>{html.escape(str(email))}</code>\n"
        f"Тип: {payment_type}\n"
        f"Месяцев: {months}\n"
        f"Сумма: {amount // 100} ₽\n\n"
        f"Время: {datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}"
    )

    if referrer_uid:
        try:
            users = load_users_db()
            referrer = users.get(str(referrer_uid))
            if referrer:
                ref_username = referrer.get("username", "no_username")
                ref_email = html.escape(str(referrer.get("email", "—")))
                if ref_username:
                    text += f"\n🔗 Привёл: @{html.escape(str(ref_username))} ({ref_email})\n"
                else:
                    text += f"\n🔗 Привёл: {ref_email}\n"
        except:
            text += f"\n🔗 Привёл: UID {referrer_uid}\n"

    try:
        bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        log.warning(f"Не удалось отправить уведомление админу: {e}")


# Уведомления пользователям об истекающих подписках
def _seconds_until_next_noon_msk() -> float:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    target_time = now.replace(hour=12, minute=0, second=0, microsecond=0)
    # Если сегодня 12:00 уже прошло, то ближайшая цель — завтрашний день
    if now >= target_time:
        target_time += timedelta(days=1)
    return (target_time - now).total_seconds()


def notify_expiring_users():
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    current_time = now.timestamp() * 1000

    users = load_users_db()
    if not users:
        return

    sent_count = 0
    log.info(f"[{now.strftime('%d.%m.%Y %H:%M')}] Запуск ежедневной рассылки уведомлений...")

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

        # Проверяем диапазон (3 дня)
        if 2.0 < days_left <= 3.0:
            try:
                bot.send_message(
                    tg_id,
                    "⚠️ <b>Ваша подписка заканчивается через 3 дня!</b>\n\n"
                    "Не забудьте продлить, чтобы не потерять доступ.",
                    parse_mode="HTML"
                )
                log.info(f"✅ Уведомление отправлено (3 дня): {username} (TG: {tg_id})")
                sent_count += 1
            except Exception as e:
                log.warning(f"Не удалось отправить (3 дня) {tg_id}: {e}")

        # Проверяем диапазон (последний день)
        elif 0.0 < days_left <= 1.0:
            try:
                bot.send_message(
                    tg_id,
                    "❗️ <b>Ваша подписка сегодня заканчивается!</b>\n\n"
                    "Продлите подписку, чтобы продолжить пользоваться VPN.",
                    parse_mode="HTML"
                )
                log.info(f"✅ Уведомление отправлено (сегодня): {username} (TG: {tg_id})")
                sent_count += 1
            except Exception as e:
                log.warning(f"Не удалось отправить (сегодня) {tg_id}: {e}")

    log.info(f"Рассылка завершена. Отправлено: {sent_count} уведомлений.")


def check_expiring_subscriptions():
    # [FIX] Раньше после первой рассылки делался sleep(86400), и время рассылки
    # «уплывало» на длительность самой рассылки каждый день. Теперь каждый раз
    # заново считаем, сколько спать до ближайших 12:00 МСК.
    while True:
        try:
            seconds_to_wait = _seconds_until_next_noon_msk()
            log.info(f"Следующая проверка подписок через {seconds_to_wait / 3600:.2f} ч. (в 12:00 МСК)")
            time.sleep(seconds_to_wait)
            notify_expiring_users()
        except Exception as e:
            log.exception(f"Ошибка в блоке рассылки: {e}")
        # Защита от двойного срабатывания, если sleep проснулся чуть раньше 12:00:00
        time.sleep(60)



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

USER_MENU_BUTTONS = (
    "📦 Моя подписка",
    "🎟 Рефералка",
    "🔄 Продлить подписку",
    "📑 Инструкция",
    "📩 Поддержка",
)

ADMIN_MENU_BUTTONS = (
    "👥 Пользователи",
    "➕ Добавить пользователя",
    "🔄 Продлить пользователя",
    "🗑 Удалить пользователя",
    "🖥 Статус серверов",
    "🔄 Синхронизировать пользователей",
    "📊 Отчет по оплатам",
)

MENU_BUTTONS = set(USER_MENU_BUTTONS) | set(ADMIN_MENU_BUTTONS)


# Главное меню пользователя
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for button in USER_MENU_BUTTONS:
        markup.add(button)
    return markup



# Главное меню админа
def admin_panel():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for button in ADMIN_MENU_BUTTONS:
        markup.add(button)
    return markup



def step_interrupted(message) -> bool:
    """
    [FIX] telebot сначала отдаёт сообщение next-step хендлеру и забирает его из обработки
    (TeleBot._notify_next_handlers → new_messages.pop), обычные message_handler'ы его уже не видят.
    Поэтому если пользователь посреди диалога нажимал кнопку меню, кнопка «съедалась»
    вопросом «введите число» и он застревал в цикле. Здесь: если пришла кнопка меню или
    команда — выходим из диалога и прогоняем сообщение через обычные хендлеры.
    Для не-текстовых сообщений (стикер, фото) message.text = None — тоже не падаем.
    """
    text = (message.text or "").strip()
    if text.startswith("/") or text in MENU_BUTTONS:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.process_new_messages([message])
        return True
    return False



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
    users = load_users_db()
    return any(user.get("referral_code") == code for user in users.values())

# Поиск пользователя по реферальному коду
def find_user_by_referral_code(code: str):
    users = load_users_db()
    for uid, data in users.items():
        if data.get("referral_code") == code.upper():
            return uid, data
    return None, None

# Реферальный бонус
def give_referral_bonus(referrer_uid: str, new_user_uid: str, months: int = 1):
    try:
        if not referrer_uid or str(referrer_uid) == str(new_user_uid):
            return

        users = load_users_db()
        referrer = users.get(str(referrer_uid))
        if not referrer:
            return

        # Проверка: не даём бонус бессрочным пользователям
        if referrer.get("expiry_time") == 0:
            log.info(f"ℹ️ Реферер UID {referrer_uid} имеет бессрочную подписку — бонус не выдан")
            return

        email = referrer.get("email")
        if not email:
            return

        # [FIX] Раньше: дата бралась из users.json, клиент в панели обновлялся «вслепую»
        # (except: pass), к inbound'ам не привязывался и UUID портился.
        # Теперь тот же путь, что и обычное продление.
        ok, err, new_expiry, attach_err = xui_extend_client(email, months)
        if not ok:
            log.error(f"❌ Не удалось начислить реферальный бонус {email}: {err}")
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Не удалось начислить реферальный бонус\n"
                    f"Реферер UID: {referrer_uid}\nEmail: <code>{html.escape(str(email))}</code>\n"
                    f"Ошибка: <code>{html.escape(str(err)[:300])}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        if attach_err:
            notify_admin_attach_problem(email, attach_err, context="Реферальный бонус")

        update_user_record(referrer_uid, expiry_time=new_expiry)

        # Уведомляем реферера
        tg_id = referrer.get("tg_id")
        if tg_id and tg_id != "by_admin":
            try:
                bot.send_message(int(tg_id),
                    "🎉 <b>Реферальный бонус!</b>\n\n"
                    f"Ваш друг активировал подписку по вашей рефералке.\n"
                    f"Вам добавлен +{months} {months_word(months)} к подписке!",
                    parse_mode="HTML")
            except:
                pass

    except Exception as e:
        log.exception(f"Ошибка выдачи реферального бонуса: {e}")



# =======================================================================
# ====================== ФУНКЦИОНАЛ ПОЛЬЗОВАТЕЛЯ ======================
# =======================================================================

# ====================== Основной оффер =======================

@bot.message_handler(commands=['start'])
def start_handler(message):
    tg_id = message.from_user.id

    # /start всегда сбрасывает незаконченный диалог
    bot.clear_step_handler_by_chat_id(message.chat.id)

    # Получаем полный текст команды (включая параметр)
    full_text = message.text.strip() if message.text else ""

    # === ВОЗВРАТ ПОСЛЕ ОПЛАТЫ YOOKASSA ===
    if "payment_" in full_text:
        # Возврат по return_url ещё не означает, что оплата прошла — честно об этом говорим
        if tg_id not in pending_requests:
            bot.send_message(tg_id, "🎉 Подписка уже активирована!", reply_markup=main_menu())
        else:
            bot.send_message(tg_id, "⏳ Ожидаем подтверждение оплаты от YooKassa...", reply_markup=main_menu())
        return

    show_start(message.chat.id, message.from_user)


def show_start(chat_id, user):
    """[FIX] Логика /start вынесена в функцию, чтобы не собирать фейковый types.Message в cancel_payment."""
    tg_id = user.id

    try:
        # 1. Ищем по tg_id
        uid, user_data = get_user_by_tg_id(tg_id)

        if user_data and user_data.get("status") == "approved":
            bot.send_message(chat_id, "Добро пожаловать 👇", reply_markup=main_menu())
            return

        # 2. Ищем по username (пользователь добавлен админом)
        username = norm_username(user)
        if username != "no_username":
            uid_by_name, user_by_name = get_user_by_username(username)
            if user_by_name and user_by_name.get("status") == "approved" and user_by_name.get("tg_id") == "by_admin":

                # Обновляем tgId
                update_tg_id(uid_by_name, tg_id, username)

                bot.send_message(chat_id, "Добро пожаловать 👇", reply_markup=main_menu())
                return

    except Exception as e:
        log.exception(f"Ошибка в show_start: {e}")

    # Пользователь в процессе оплаты.
    # [FIX] Раньше тут был тупик «🕚 Жду подтверждения» навсегда (до рестарта бота),
    # если человек открыл счёт и передумал. Теперь даём кнопку отмены.
    if tg_id in pending_requests:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ Отменить оплату", callback_data="cancel_payment"))
        bot.send_message(
            chat_id,
            "🕚 Ждём подтверждения оплаты.\n\n"
            "Если вы уже оплатили — подписка активируется автоматически в течение пары минут.\n"
            "Если передумали — отмените оплату и начните заново.",
            reply_markup=markup
        )
        return

    # Новый пользователь
    ask_vpn_offer(chat_id)



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
        f"• {XUI_CLIENT_LIMIT_IP} {device_ru()} на одной подписке\n"
        "• Демократичная цена\n"
        "• Система скидок\n"
        "• Реферальная система\n"
        "(Приводите друга, вам и ему по месяцу в подарок!)\n"
        "• Прямая линия с поддержкой\n\n"
        "📦 <b>После оплаты вы получите:</b>\n"
        "• Конфиг для подключения\n"
        "• Пошаговую инструкцию\n"
        f"• И использование одной подписки на {XUI_CLIENT_LIMIT_IP} {device_ru()}!\n\n"
        "💰 <b>Цена:</b>\n"
        f"{PRICE_PER_MONTH}₽ / месяц\n"
        f"{get_price_per_month(FIRST_DISCOUNT_COUNT_MONTH)}₽ / от {FIRST_DISCOUNT_COUNT_MONTH} {months_word(FIRST_DISCOUNT_COUNT_MONTH)}\n"
        f"{get_price_per_month(SECOND_DISCOUNT_COUNT_MONTH)}₽ / от {SECOND_DISCOUNT_COUNT_MONTH} {months_word(SECOND_DISCOUNT_COUNT_MONTH)}\n\n"
        "❓ <b>Оформляем?</b>",
        parse_mode="HTML",
        reply_markup=markup
    )

# Запрос кол-ва месяцев для новой подписки
@bot.callback_query_handler(func=lambda call: call.data.startswith("pay:"))
def handle_pay_choice(call):
    tg_id = call.from_user.id
    action = call.data.split(":")[1]
    username = norm_username(call.from_user)

    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action == "1":
        ask_referral_before_payment(tg_id, username, months=1, flow="new")
    elif action == "multi":
        # [FIX] Убрана преждевременная запись в pending_requests: если человек
        # не доходил до счёта, /start навсегда показывал «Жду подтверждения».
        # send_invoice сам создаёт запись, когда счёт реально выставлен.
        msg = bot.send_message(tg_id, "📅 Введите количество месяцев (1–12):")
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow="new")

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
    username = norm_username(call.from_user)

    bot.answer_callback_query(call.id)
    # [FIX] Если человек нажал «Без кода» из сообщения об ошибке, ожидающий ввод кода
    # хендлер должен быть снят, иначе он съест следующее сообщение пользователя.
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if has_ref:
        msg = bot.send_message(tg_id, "Введите реферальный код:")
        bot.register_next_step_handler(msg, process_referral_input, tg_id, username, months, flow)
    else:
        send_invoice(tg_id, username, months, flow, referral_code=None)

def process_referral_input(message, tg_id, username, months, flow):
    if step_interrupted(message):
        return

    code = (message.text or "").strip().upper()
    referrer_uid, referrer_data = find_user_by_referral_code(code) if code else (None, None)

    if not referrer_uid:
        # [FIX] Раньше просили «нажмите «Нет»», но кнопку к этому моменту уже удалили —
        # пользователь застревал. Даём кнопку продолжить без кода.
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Продолжить без кода", callback_data=f"no_ref:{months}:{flow}"))
        msg = bot.send_message(
            tg_id,
            "❌ Реферальный код не найден.\nВведите код ещё раз или продолжите без него.",
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_referral_input, tg_id, username, months, flow)
        return

    # Инициализируем запись, если её ещё нет
    if tg_id not in pending_requests:
        pending_requests[tg_id] = {
            "flow": flow,
            "months": months,
            "username": username
        }

    pending_requests[tg_id]["referrer_uid"] = referrer_uid

    bot.send_message(tg_id, "✅ Реферальный код принят. Переходим к оплате...")

    # Теперь можно отправлять счёт
    send_invoice(tg_id, username, months, flow, code)



# Обработчик отмены оплаты
@bot.callback_query_handler(func=lambda call: call.data == "cancel_payment")
def cancel_payment(call):
    tg_id = call.from_user.id
    pending_requests.pop(tg_id, None)

    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    bot.send_message(tg_id, "❌ Оплата отменена.")

    # [FIX] Вместо сборки фейкового types.Message и прямого вызова start_handler
    show_start(call.message.chat.id, call.from_user)



def _lookup_username(tg_id) -> str:
    """Username из Telegram, если в памяти (pending_requests) его уже нет — например, после рестарта."""
    try:
        chat = bot.get_chat(tg_id)
        if chat and chat.username:
            return chat.username.lower().replace("@", "")
    except Exception as e:
        log.warning(f"Не удалось получить username для {tg_id}: {e}")
    return "no_username"


# Универсальная обработка успешной оплаты
def process_successful_payment(tg_id: int, months: int, flow: str = "new", referrer_uid: str = None):
    data = pending_requests.get(tg_id, {})
    # [FIX] pending_requests живёт только в памяти. Если бот перезапускался между
    # выставлением счёта и вебхуком, раньше получали username=no_username и сумму 0 ₽.
    username = data.get("username") or _lookup_username(tg_id)
    amount = (data.get("amount") or get_price_per_month(months) * months) * 100

    uid = get_or_create_uid(tg_id)

    # [FIX] Если у пользователя уже есть клиент (например, нажал старую кнопку оффера
    # и оплатил «новую» подписку повторно) — продлеваем, а не создаём дубликат,
    # который панель всё равно отклонит как Duplicate email.
    _, existing = get_user_by_tg_id(tg_id)
    if flow == "new" and existing and existing.get("email"):
        log.info(f"ℹ️ tg_id={tg_id} уже имеет клиента {existing.get('email')} — оформляем как продление")
        flow = "renew"
        referrer_uid = None

    log.info(f"Обработка платежа: flow={flow}, months={months}, tg_id={tg_id}, uid={uid}")

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
                "👇 Подписывайтесь на группу, чтобы быть в курсе технических работ, нововведений сервиса и новостей в мире VPN и рунета:\n"
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
            safe_send_message(tg_id, f"🔄 <b>Подписка успешно продлена на {months} {months_word(months)}!</b>", reply_markup=main_menu())
            sub(tg_id)  # покажет актуальную информацию
            log.info(f"✅ Успешное продление для tg_id={tg_id}")
        else:
            log.error(f"❌ Ошибка продления: {error_msg}")
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
        log.exception(f"Ошибка отправки видео: {e}")
        bot.send_message(chat_id, "Не удалось отправить видео-инструкцию. Используйте текстовую инструкцию выше.")



# [FIX] Удалён обработчик callback "cancel:<tg_id>": ни одна кнопка такой callback не создаёт,
# а сам хендлер брал tg_id из callback_data вместо call.from_user.id.



# ====================== Реакции на кнопки меню пользователя  =======================


# ====================== Реакция на кнопку "Моя подписка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📦 Моя подписка")
def subscribe_handler(message):
    # Пользователь мог быть добавлен админом по username — привязываем tg_id
    link_admin_added_user(message.from_user)

    # Показываем информацию о подписке
    sub(message.from_user.id, message)



# ====================== Реакция на кнопку "Рефералка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "🎟 Рефералка")
def referral_handler(message):
    link_admin_added_user(message.from_user)
    show_referral(message.from_user.id)

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

    link_admin_added_user(message.from_user)

    # Получаем данные пользователя
    uid, user_data = get_user_by_tg_id(tg_id)

    # [FIX] Незарегистрированный пользователь не может «продлить» — раньше он доходил до оплаты,
    # а после оплаты получал «Ошибка продления» (клиента-то нет).
    if not user_data:
        bot.send_message(message.chat.id, "❌ У вас ещё нет подписки. Нажмите /start, чтобы оформить её.")
        return

    # Проверка на бессрочную подписку
    if user_data.get("expiry_time") == 0:
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
    # [FIX] Убрана преждевременная запись pending_requests — её делает send_invoice.
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(f"📅 На 1 месяц — {PRICE_PER_MONTH}₽", callback_data="renew:1"),
        types.InlineKeyboardButton("📅 На несколько месяцев", callback_data="renew:multi")
    )
    bot.send_message(
        message.chat.id,
        "💰 <b>Цена:</b>\n"
        f"{PRICE_PER_MONTH}₽ / месяц\n"
        f"{get_price_per_month(FIRST_DISCOUNT_COUNT_MONTH)}₽ / от {FIRST_DISCOUNT_COUNT_MONTH} {months_word(FIRST_DISCOUNT_COUNT_MONTH)}\n"
        f"{get_price_per_month(SECOND_DISCOUNT_COUNT_MONTH)}₽ / от {SECOND_DISCOUNT_COUNT_MONTH} {months_word(SECOND_DISCOUNT_COUNT_MONTH)}\n\n"
        "🔄 Выберите срок продления:",
        parse_mode="HTML",
        reply_markup=markup
    )



# Запрос кол-ва месяцев для продления
@bot.callback_query_handler(func=lambda call: call.data.startswith("renew:"))
def handle_renew_choice(call):
    action = call.data.split(":")[1]
    tg_id = call.from_user.id
    username = norm_username(call.from_user)

    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if action == "1":
        send_invoice(tg_id, username, months=1, flow="renew")
    elif action == "multi":
        msg = bot.send_message(tg_id, "📅 Введите количество месяцев (1–12):")
        bot.register_next_step_handler(msg, process_months_input, tg_id, flow="renew")



# ====================== Реакция на кнопку "Инструкция"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📑 Инструкция")
def instruction_handler(message):
    link_admin_added_user(message.from_user)
    instruction_send(message.from_user.id)



# ====================== Реакция на кнопку "Поддержка"  =======================
@bot.message_handler(func=lambda m: m.text and m.text.strip() == "📩 Поддержка")
def support_handler(message):
    link_admin_added_user(message.from_user)
    bot.send_message(message.from_user.id, f"📩 Поддержка\n👤 Напишите сюда: {SUPPORT}\n\n⏱ Мы ответим вам как можно скорее.", reply_markup=main_menu())



# =================================================================
# ====================== ФУНКЦИОНАЛ АДМИНА ======================
# =================================================================

# Обработка вызова админки
@bot.message_handler(commands=['admin'])
def admin_handler(message):
    if not is_admin(message.from_user.id):
        return

    bot.clear_step_handler_by_chat_id(message.chat.id)
    bot.send_message(
        message.chat.id,
        "⚙️ Админ-панель",
        reply_markup=admin_panel()
    )

# Проверка на админа
def is_admin(user_id):
    return user_id == ADMIN_ID


def _remove_inline_keyboard(call):
    """[FIX] Убираем инлайн-кнопки сразу после нажатия — защита от двойного клика
    (раньше двойной тап по «На 1 месяц» продлевал/создавал клиента дважды)."""
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass



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

    # Отвечаем сразу, пока запрос не протух
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        log.warning(f"answer_callback_query: {e}")

    filter_type = call.data.split(":")[1]

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    if filter_type in ("all", "limited", "unlimited"):
        show_users_list(call.message, filter_type)

def show_users_list(message, filter_type="all"):
    users = load_users_db()
    if not users:
        bot.send_message(message.chat.id, "users.json не найден или пуст")
        return

    all_traffic = get_all_users_traffic()
    online_clients = get_online_clients()

    messages = []
    messages.append(f"👥 Пользователи — {filter_type.upper()}\n\n{'─' * 18}\n\n")
    current = ""

    for key, info in users.items():
        uid = key
        tg_id = info.get("tg_id", "-")
        email = info.get("email", "-")
        username = info.get("username", "no_username")
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

        online = bool(email and email in online_clients)

        traffic_data = all_traffic.get(email, {})
        up = round(traffic_data.get("up", 0) / (1024**3), 2)
        down = round(traffic_data.get("down", 0) / (1024**3), 2)

        # [FIX] html.escape: в email/username от админа мог попасть символ <, & —
        # Telegram отклонял всё сообщение (Bad Request: can't parse entities)
        user_block = (
            f"🆔 <b>UID:</b> <code>{html.escape(str(uid))}</code>\n"
            f"👤 <b>TG ID:</b> <code>{html.escape(str(tg_id))}</code>\n"
            f"💬 <b>User:</b> @{html.escape(str(username))}\n"
            f"📧 <b>Email:</b> <code>{html.escape(str(email))}</code>\n\n"

            f"🔑 <b>Ref код:</b> <code>{html.escape(str(ref_code))}</code>\n\n"

            f"📊 <b>Status:</b> {'🟢 Online' if online else '🔴 Offline'}\n\n"

            f"⬆️ <b>Upload:</b> {up} GB\n"
            f"⬇️ <b>Download:</b> {down} GB\n\n"

            f"⏳ <b>Expire:</b> {expiry_date}\n\n"

            f"🔗 <b>SUB LINK:</b>\n"
            f"<code>{XUI_SUB_LINK}/{html.escape(str(sub_id))}</code>\n\n"

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
        data = _xui_json(r)
        if r.status_code == 200 and data.get("success"):
            return set(data.get("obj") or [])
    except Exception as e:
        log.exception(f"Online check error: {e}")
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
        data = _xui_json(r)

        if r.status_code != 200 or not data.get("success"):
            return traffic

        inbounds = data.get("obj") or []

        for inbound in inbounds:
            client_stats = inbound.get("clientStats") or []

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
        log.exception(f"Traffic error: {e}")

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
        "Если username неизвестен — напишите -"
    )
    bot.register_next_step_handler(msg, admin_process_ask_email)

# Спрашиваем email
def admin_process_ask_email(message):
    if step_interrupted(message):
        return
    if not message.text:
        msg = bot.send_message(message.chat.id, "❌ Нужен текст. Введите @username или -")
        bot.register_next_step_handler(msg, admin_process_ask_email)
        return

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
    if step_interrupted(message):
        return
    if not message.text or not message.text.strip():
        msg = bot.send_message(message.chat.id, "❌ Email не может быть пустым. Введите Email (base name):")
        bot.register_next_step_handler(msg, admin_process_ask_time_new)
        return

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

    # [FIX] Не было answer_callback_query («часики» на кнопке) и кнопки не убирались
    bot.answer_callback_query(call.id)
    _remove_inline_keyboard(call)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    action = call.data.split(":")[1]

    if action == "multi":
        msg = bot.send_message(call.message.chat.id, "Введите количество месяцев (1-12):")
        bot.register_next_step_handler(msg, admin_add_multi_months)
    elif action == "unlimited":
        admin_add_user(call.message, 0)
    else:
        admin_add_user(call.message, 1)

# Обработка ввода кол-ва месяцев
def admin_add_multi_months(message):
    if step_interrupted(message):
        return
    try:
        months = int((message.text or "").strip())
        if months < 1 or months > 12:
            raise ValueError
    except:
        msg = bot.send_message(message.chat.id, "❌ Введите корректное кол-во месяцев (1-12)")
        bot.register_next_step_handler(msg, admin_add_multi_months)
        return

    admin_add_user(message, months)

# Добавление пользователя через админа
def admin_add_user(message, months):
    global admin_given_email, admin_given_username

    # [FIX] Если бот перезапускали между шагами, глобальные переменные пустые —
    # раньше создавался клиент с именем "<uid>_None_by_admin"
    if not admin_given_email:
        bot.send_message(message.chat.id, "❌ Данные потеряны (бот перезапускался?). Начните заново.", reply_markup=admin_panel())
        return

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
            f"👤 Username: @{html.escape(str(username_display))}\n"
            f"📧 Email: <code>{html.escape(str(email))}</code>\n"
            f"📅 <b>Действует до:</b> {expiry_date}\n\n"
            f"🎟 <b>Реф код:</b> <code>{ref_code}</code>\n\n"
            f"🔗 Ссылка:\n<code>{sub_link}</code>",
            parse_mode="HTML",
            reply_markup=admin_panel()
        )
    else:
        bot.send_message(message.chat.id, f"❌ Ошибка создания: {error_msg}", reply_markup=admin_panel())

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
    if step_interrupted(message):
        return

    uid = (message.text or "").strip().lower()

    # [FIX] Проверяем UID сразу, а не после выбора срока
    if not uid or uid not in load_users_db():
        msg = bot.send_message(message.chat.id, f"❌ Пользователь с UID {html.escape(uid) or '—'} не найден. Введите uid ещё раз:")
        bot.register_next_step_handler(msg, admin_process_ask_time_renew)
        return

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

    bot.answer_callback_query(call.id)
    _remove_inline_keyboard(call)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    action = call.data.split(":")[1]

    if action == "multi":
        msg = bot.send_message(call.message.chat.id, "Введите количество месяцев (1-12):")
        bot.register_next_step_handler(msg, admin_renew_multi_months)
    else:
        months = 0 if action == "unlimited" else 1
        admin_renew_user(call.message, months)

# Обработка ввода кол-ва месяцев
def admin_renew_multi_months(message):
    if step_interrupted(message):
        return
    try:
        months = int((message.text or "").strip())
        if months < 1 or months > 12:
            raise ValueError
    except:
        msg = bot.send_message(message.chat.id, "❌ Введите корректное кол-во месяцев (1-12)")
        bot.register_next_step_handler(msg, admin_renew_multi_months)
        return

    admin_renew_user(message, months)

# Продление пользователя через админа
def admin_renew_user(message, months):
    global admin_renew_uid
    uid = admin_renew_uid

    if not uid:
        bot.send_message(message.chat.id, "❌ Ошибка: UID не найден (бот перезапускался?). Начните заново.", reply_markup=admin_panel())
        return

    user_data = load_users_db().get(str(uid))
    if not user_data:
        bot.send_message(message.chat.id, f"❌ Пользователь с UID {uid} не найден.", reply_markup=admin_panel())
        admin_renew_uid = None
        return

    tg_id = user_data.get("tg_id")
    username = user_data.get("username", "no_username")
    email = user_data.get("email")
    sub_id = user_data.get("sub_id")

    if not email:
        bot.send_message(message.chat.id, f"❌ У пользователя с UID {uid} не найден email.", reply_markup=admin_panel())
        admin_renew_uid = None
        return

    # [FIX] renew_vpn_client сам пишет новую дату в users.json — повторная запись
    # старым словарём (как было) могла затереть чужие изменения.
    success, error_msg, email, new_expiry = renew_vpn_client(uid, tg_id, username, months)

    if success and new_expiry is not None:
        moscow_tz = ZoneInfo("Europe/Moscow")
        expiry_date = "БЕССРОЧНО" if new_expiry == 0 else datetime.fromtimestamp(
            new_expiry / 1000, tz=moscow_tz
            ).strftime("%d.%m.%Y %H:%M (МСК)")

        sub_link = f"{XUI_SUB_LINK}/{sub_id}"

        # [FIX] было **...** при parse_mode="HTML" — звёздочки печатались как есть
        bot.send_message(
            message.chat.id,
            f"✅ <b>Подписка успешно продлена!</b>\n\n"
            f"🆔 UID: <b>{uid}</b>\n"
            f"👤 @{html.escape(str(username))}\n"
            f"📧 <code>{html.escape(str(email))}</code>\n"
            f"🔄 Месяцев добавлено: {months if months > 0 else '∞'}\n"
            f"📅 Новая дата окончания: <b>{expiry_date}</b>\n\n"
            f"🔗 Ссылка:\n<code>{sub_link}</code>",
            parse_mode="HTML",
            reply_markup=admin_panel()
        )

        # Уведомляем пользователя, если есть tg_id
        # [FIX] было tg_id != "unlimited" — такого значения нет, у добавленных админом
        # tg_id == "by_admin", int("by_admin") падал и админ видел ложную ошибку.
        if tg_id and str(tg_id).isdigit():
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
            bot.send_message(message.chat.id, "ℹ️ Пользователь ещё не заходил в бота (tg_id неизвестен) — уведомление не отправлено.")
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
    msg = bot.send_message(message.chat.id, "🗑 Введите <b>UID</b> пользователя для удаления:", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_delete_by_uid)

# Проверка uid
def process_delete_by_uid(message):
    if step_interrupted(message):
        return
    try:
        uid = int((message.text or "").strip())
    except:
        return bot.send_message(message.chat.id, "❌ Неверный UID. Введите число.", reply_markup=admin_panel())

    success, msg = delete_vpn_user_by_uid(uid)
    if success:
        bot.send_message(message.chat.id, f"✅ {msg}", reply_markup=admin_panel())
    else:
        bot.send_message(message.chat.id, f"❌ {msg}", reply_markup=admin_panel())

# Удаление клиента
def delete_vpn_user_by_uid(uid: int):
    try:
        users = load_users_db()
    except Exception:
        return False, "users.json not found"

    target_key = str(uid)
    if target_key not in users:
        return False, f"Пользователь с UID {uid} не найден"

    email = users[target_key].get("email")
    if not email:
        return False, "Email не найден"

    try:
        r = requests.post(
            f"{XUI_URL}/panel/api/clients/del/{_xui_email_path(email)}",
            headers=headers,
            timeout=15
        )
        data = _xui_json(r)

        panel_ok = r.status_code == 200 and data.get("success")
        error = str(data.get("msg") or r.text)
        # [FIX] Если клиента уже удалили в панели вручную, раньше запись в users.json
        # было невозможно удалить из бота вообще.
        already_gone = "not found" in error.lower()

        if panel_ok or already_gone:
            with USERS_LOCK:
                users = load_users_db()
                users.pop(target_key, None)
                save_users_db(users)

            note = " (в панели его уже не было)" if already_gone and not panel_ok else ""
            return True, f"Клиент {email} успешно удалён (UID: {uid}){note}"
        else:
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
    text = ""

    # ==================== CENTRAL SERVER ====================
    try:
        r = requests.get(f"{XUI_URL}/panel/api/server/status", headers=headers, timeout=15)
        data = _xui_json(r)

        if r.status_code == 200 and data.get("success"):
            s = data["obj"]

            cpu = s.get('cpu')
            # [FIX] f"{'N/A':.1f}" бросал ValueError, и весь блок превращался в Error
            cpu_str = f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "N/A"
            mem_current = round(s['mem']['current'] / 1024**3, 2) if 'mem' in s else 'N/A'
            mem_total = round(s['mem']['total'] / 1024**3, 2) if 'mem' in s else 'N/A'
            disk_current = round(s.get('disk', {}).get('current', 0) / 1024**3, 1)
            disk_total = round(s.get('disk', {}).get('total', 0) / 1024**3, 1)
            xray_state = s.get('xray', {}).get('state', 'N/A')
            xray_version = s.get('xray', {}).get('version', 'N/A')
            tcp_count = s.get('tcpCount', 'N/A')

            text += (
                "🌐 <b>CENTRAL SERVER</b>\n"
                f"{'─' * 18}\n\n"
                f"🧠 CPU: <b>{cpu_str}</b>\n"
                f"💾 RAM: <b>{mem_current} / {mem_total} GB</b>\n"
                f"📦 Disk: <b>{disk_current} / {disk_total} GB</b>\n"
                f"🔌 TCP: <b>{tcp_count}</b>\n"
                f"⚙️ XRAY: <b>{html.escape(str(xray_state))}</b> (<code>{html.escape(str(xray_version))}</code>)\n\n"
                f"{'─' * 18}\n\n"
            )
        else:
            text += "🌐 <b>CENTRAL SERVER</b>\n❌ Не удалось получить статус\n\n"

    except Exception as e:
        text += (
            "🌐 <b>CENTRAL SERVER</b>\n"
            f"{'─' * 18}\n"
            f"❌ <b>Error:</b> <code>{html.escape(str(e)[:250])}</code>\n\n"
            f"{'─' * 18}\n\n"
        )

    # ==================== NODES ====================
    try:
        r = requests.get(f"{XUI_URL}/panel/api/nodes/list", headers=headers, timeout=15)
        data = _xui_json(r)

        if r.status_code != 200 or not data.get("success"):
            text += "🖥 <b>NODES</b>\n❌ Failed to get node list\n"
            return text

        nodes = data.get("obj") or []

        if not nodes:
            text += "🖥 <b>NODES</b>\nNo nodes\n"
            return text

        text += f"🖥 <b>NODES STATUS</b> — {len(nodes)} nodes\n\n"
        text += f"{'─' * 18}\n"

        for node in nodes:
            name = node.get("name", "Unknown")
            status = node.get("status", "unknown")
            cpu_pct = node.get("cpuPct")
            mem_pct = node.get("memPct")
            latency = node.get("latencyMs")
            uptime_secs = node.get("uptimeSecs") or 0

            cpu_str = f"{round(cpu_pct, 1)}%" if isinstance(cpu_pct, (int, float)) else "N/A"
            mem_str = f"{round(mem_pct, 1)}%" if isinstance(mem_pct, (int, float)) else "N/A"
            uptime_days = uptime_secs // 86400

            status_emoji = "🟢" if status == "online" else "🔴"

            text += (
                f"<b>{html.escape(str(name))}</b>\n\n"
                f"{status_emoji} <b>{'Online' if status == 'online' else 'Offline'}</b>\n"
                f"🧠 CPU: <b>{cpu_str}</b>\n"
                f"💾 RAM: <b>{mem_str}</b>\n"
                f"📶 Latency: <b>{latency} ms</b>\n"
                f"⏱ Uptime: <b>{uptime_days} days.</b>\n\n"
                f"{'─' * 18}\n\n"
            )

    except Exception as e:
        text += f"🖥 <b>NODES ERROR:</b> <code>{html.escape(str(e)[:300])}</code>\n"

    return text



# ========= Реакция на кнопку "Синхронизация пользователей" ============
SYNC_PROGRESS_EVERY_SEC = 15


@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and m.text == "🔄 Синхронизировать пользователей")
def sync_users_handler(message):
    if not is_admin(message.from_user.id):
        return

    # [FIX] Не даём запустить две синхронизации параллельно
    if not SYNC_LOCK.acquire(blocking=False):
        bot.send_message(message.chat.id, "⏳ Синхронизация уже идёт, дождитесь результата.\nПрогресс — в logs/bot.log.")
        return

    msg = bot.send_message(
        message.chat.id,
        "🔄 Запускаю синхронизацию клиентов (inbound'ы, flow, включение, UUID)...\n"
        "Получаю список клиентов из панели."
    )

    def edit_status(text):
        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=text, parse_mode="HTML")
        except Exception as e:
            # «message is not modified» и т.п. — не критично
            log.debug("Не удалось обновить сообщение о синхронизации: %s", e)

    last_edit = [0.0]

    def progress(done, planned, stats):
        # Не чаще раза в SYNC_PROGRESS_EVERY_SEC — у Telegram лимиты на редактирование
        now = time.monotonic()
        if done < planned and now - last_edit[0] < SYNC_PROGRESS_EVERY_SEC:
            return
        last_edit[0] = now
        edit_status(
            "🔄 <b>Синхронизация идёт</b>\n\n"
            f"Исправлено клиентов: <b>{done} / {planned}</b>\n"
            f"Ошибок пока: <b>{stats['errors']}</b>\n\n"
            "Подробности — в logs/bot.log"
        )

    # [FIX] Синхронизация идёт в отдельном потоке: раньше она занимала один из двух
    # рабочих потоков telebot, и бот «подвисал» для пользователей на время синка.
    def worker():
        t0 = time.monotonic()
        try:
            stats = sync_clients(progress=progress)
            text = format_sync_report(stats) + f"\n\n⏱ Заняло: {time.monotonic() - t0:.0f} с"
        except Exception as e:
            log.exception("Синхронизация прервана ошибкой")
            text = (
                f"❌ <b>Синхронизация не выполнена</b>\n<code>{html.escape(str(e)[:500])}</code>\n\n"
                "Трейсбек — в logs/bot.log"
            )
        finally:
            SYNC_LOCK.release()

        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=text, parse_mode="HTML")
        except Exception:
            bot.send_message(message.chat.id, text, parse_mode="HTML")

    threading.Thread(target=worker, name="sync", daemon=True).start()


def fetch_all_clients(page_size=200, max_pages=500):
    """
    Забирает всех клиентов постранично через /clients/list/paged.
    [FIX] Параметр размера страницы в 3x-ui называется pageSize (ClientPageParams, form:"pageSize"),
    а не size — раньше он игнорировался и панель отдавала по 25 клиентов. Максимум — 200.
    """
    clients = []
    page = 1
    while page <= max_pages:
        t0 = time.monotonic()
        r = _xui_call("GET", "/panel/api/clients/list/paged",
                      params={"page": page, "pageSize": page_size}, timeout=60)
        data = _xui_json(r)

        if r.status_code != 200 or not data.get("success"):
            raise RuntimeError(f"clients/list/paged → HTTP {r.status_code}: {str(data.get('msg') or r.text)[:300]}")

        obj = data.get("obj") or {}
        items = obj.get("items") or []
        clients.extend(items)

        total = obj.get("filtered", obj.get("total", 0))
        log.info("Синхронизация: страница %d — %d клиентов (%d / %d), %.1f с",
                 page, len(items), len(clients), total, time.monotonic() - t0)

        if not items or len(clients) >= total:
            break
        page += 1

    return clients


def _as_dict(value) -> dict:
    """В 3x-ui v3 settings/streamSettings приходят объектами, в старых версиях — JSON-строкой."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _inbound_flow_capable(inbound: dict) -> bool:
    """Повторяет inboundCanEnableTlsFlow + DisableFlow из исходников 3x-ui."""
    if inbound.get("protocol") != "vless" or inbound.get("disableFlow"):
        return False
    stream = _as_dict(inbound.get("streamSettings"))
    network = stream.get("network")
    if network == "tcp":
        return stream.get("security") in ("tls", "reality")
    if network == "xhttp":
        settings = _as_dict(inbound.get("settings"))
        return any(v and v != "none" for v in (settings.get("encryption"), settings.get("decryption")))
    return False


def audit_target_inbounds():
    """
    Смотрит реальные записи клиентов в settings целевых inbound'ов.
    Возвращает (flow_broken, bad_uuid) — множества email'ов.

    Почему не через GET /clients/get: он отдаёт «эффективный» flow — первый непустой.
    Если flow слетел только на одном inbound из четырёх, GET этого не покажет.
    """
    r = _xui_call("GET", "/panel/api/inbounds/list", timeout=60)
    data = _xui_json(r)
    if r.status_code != 200 or not data.get("success"):
        raise RuntimeError(f"inbounds/list → HTTP {r.status_code}: {str(data.get('msg') or r.text)[:300]}")

    target = set(XUI_INBOUND_IDS)
    flow_broken, bad_uuid = set(), set()
    seen_ids = set()

    for inbound in data.get("obj") or []:
        if inbound.get("id") not in target:
            continue
        seen_ids.add(inbound.get("id"))
        settings = _as_dict(inbound.get("settings"))
        check_flow = bool(XUI_CLIENT_FLOW) and _inbound_flow_capable(inbound)
        check_uuid = inbound.get("protocol") in ("vless", "vmess")

        ib_flow = ib_uuid = 0
        for entry in settings.get("clients") or []:
            if not isinstance(entry, dict):
                continue
            email = entry.get("email")
            if not email:
                continue
            if check_flow and (entry.get("flow") or "") != XUI_CLIENT_FLOW:
                flow_broken.add(email)
                ib_flow += 1
            if check_uuid and entry.get("id") and not _is_valid_uuid(entry.get("id")):
                bad_uuid.add(email)
                ib_uuid += 1

        log.info("Синхронизация: inbound %s (%s, flow-совместим: %s) — без flow: %d, плохой UUID: %d",
                 inbound.get("id"), inbound.get("protocol"), "да" if check_flow else "нет", ib_flow, ib_uuid)

    missing_ids = target - seen_ids
    if missing_ids:
        log.warning("Синхронизация: inbound'ов %s из XUI_INBOUND_IDS нет в панели", sorted(missing_ids))

    return flow_broken, bad_uuid


def sync_clients(progress=None) -> dict:
    """
    Проверяет всех клиентов панели и чинит:
      • недостающие inbound'ы из XUI_INBOUND_IDS;
      • слетевший flow (на любом из целевых inbound'ов);
      • UUID, испорченный старым кодом продления (не-UUID значение);
      • клиентов, которые оплачены (по панели И по users.json), но остались выключены
        — это жертвы старого бага «дата сдвинулась, клиент не включился».

    [FIX] Выключенные клиенты больше не ломают синхронизацию и не считаются ошибкой:
      • истёкшие — пропускаются;
      • выключенные, но не оплаченные по данным бота (например, отключены вручную в панели
        и не из бота) — пропускаются и показываются отдельной строкой.

    Работает в два прохода: сначала составляет план (быстро, без запросов к панели),
    потом исправляет клиентов по одному, логируя каждого.
    progress(done, planned, stats) вызывается после каждого исправленного клиента.
    """
    stats = {
        "total": 0, "ok": 0, "attached": 0, "flow_fixed": 0, "uuid_fixed": 0, "enabled": 0,
        "skipped_expired": 0, "skipped_disabled": 0, "errors": 0, "missing_in_panel": 0,
    }

    log.info("Синхронизация: старт. Целевые inbound'ы: %s, flow: %r", XUI_INBOUND_IDS, XUI_CLIENT_FLOW)

    clients = fetch_all_clients()
    flow_broken, bad_uuid = audit_target_inbounds()

    users = load_users_db()
    bot_users = {u.get("email"): u for u in users.values() if u.get("email")}

    target = set(XUI_INBOUND_IDS)
    now_ms = int(time.time() * 1000)
    panel_emails = set()

    # ---------- Проход 1: план ----------
    plan = []  # (email, missing, need_flow, need_uuid, need_enable)
    for client in clients:
        email = client.get("email")
        if not email:
            continue
        panel_emails.add(email)
        stats["total"] += 1

        # expiryTime < 0 в 3x-ui — «отсчёт с первого подключения», это активный клиент
        expiry = int(client.get("expiryTime") or 0)
        if 0 < expiry <= now_ms:
            stats["skipped_expired"] += 1
            continue

        need_enable = False
        if client.get("enable") is False:
            bot_user = bot_users.get(email)
            bot_expiry = bot_user.get("expiry_time") if bot_user else None
            paid_by_bot = (
                bot_user is not None
                and bot_user.get("status") == "approved"
                and (bot_expiry == 0 or (isinstance(bot_expiry, (int, float)) and bot_expiry > now_ms))
            )
            if not paid_by_bot:
                stats["skipped_disabled"] += 1
                continue
            need_enable = True

        missing = sorted(target - set(client.get("inboundIds") or []))
        need_flow = email in flow_broken
        need_uuid = email in bad_uuid

        if not (missing or need_flow or need_uuid or need_enable):
            stats["ok"] += 1
            continue

        plan.append((email, missing, need_flow, need_uuid, need_enable))

    stats["missing_in_panel"] = len(set(bot_users) - panel_emails)

    log.info(
        "Синхронизация: план — всего %d, в порядке %d, к исправлению %d "
        "(attach: %d, flow: %d, uuid: %d, включить: %d), пропуск: истёк %d, выключен %d",
        stats["total"], stats["ok"], len(plan),
        sum(1 for p in plan if p[1]), sum(1 for p in plan if p[2]),
        sum(1 for p in plan if p[3]), sum(1 for p in plan if p[4]),
        stats["skipped_expired"], stats["skipped_disabled"],
    )

    if progress:
        progress(0, len(plan), stats)

    # ---------- Проход 2: исправления ----------
    for index, (email, missing, need_flow, need_uuid, need_enable) in enumerate(plan, start=1):
        what = ", ".join(filter(None, [
            f"attach {missing}" if missing else "",
            "flow" if need_flow else "",
            "uuid" if need_uuid else "",
            "enable" if need_enable else "",
        ]))
        t0 = time.monotonic()
        try:
            ok, err, attach_err = xui_apply_client_state(
                email,
                enable=True if need_enable else None,
                regenerate_uuid=need_uuid,
            )
        except Exception as e:
            log.exception("Синхронизация [%d/%d] %s: исключение", index, len(plan), email)
            ok, err, attach_err = False, repr(e), ""

        elapsed = time.monotonic() - t0

        if not ok or attach_err:
            stats["errors"] += 1
            log.error("Синхронизация [%d/%d] %s: ОШИБКА (%s) за %.1f с — %s %s",
                      index, len(plan), email, what, elapsed, err or "", attach_err or "")
        else:
            if missing:
                stats["attached"] += 1
            if need_flow:
                stats["flow_fixed"] += 1
            if need_uuid:
                stats["uuid_fixed"] += 1
            if need_enable:
                stats["enabled"] += 1
            log.info("Синхронизация [%d/%d] %s: исправлено (%s) за %.1f с",
                     index, len(plan), email, what, elapsed)

        if progress:
            progress(index, len(plan), stats)

    log.info("Синхронизация завершена: %s", stats)
    return stats


def format_sync_report(stats: dict) -> str:
    text = (
        f"✅ <b>Синхронизация завершена</b>\n\n"
        f"Клиентов в панели: <b>{stats['total']}</b>\n"
        f"Уже в порядке: <b>{stats['ok']}</b>\n\n"
        f"🔗 Привязано к недостающим inbound'ам: <b>{stats['attached']}</b>\n"
        f"🌊 Исправлен flow: <b>{stats['flow_fixed']}</b>\n"
        f"🔑 Перевыпущен испорченный UUID: <b>{stats['uuid_fixed']}</b>\n"
        f"🟢 Включено оплаченных, но выключенных: <b>{stats['enabled']}</b>\n\n"
        f"⏭ Пропущено (срок истёк): <b>{stats['skipped_expired']}</b>\n"
        f"⏭ Пропущено (выключены, не оплачены по данным бота): <b>{stats['skipped_disabled']}</b>\n"
        f"❌ Ошибок: <b>{stats['errors']}</b>"
    )
    if stats["missing_in_panel"]:
        text += f"\n\n⚠️ Есть в users.json, но нет в панели: <b>{stats['missing_in_panel']}</b>"
    if stats["uuid_fixed"]:
        text += (
            "\n\nℹ️ Пользователям с перевыпущенным UUID нужно обновить подписку в приложении "
            "(в v2raytun — обновить группу подписки), ссылка на подписку не меняется."
        )
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
    thread = threading.Thread(target=check_expiring_subscriptions, name="expiry-checker", daemon=True)
    thread.start()

# Основной запуск программы
if __name__ == '__main__':
    lock_file = acquire_lock()
    try:
        load_users()
        log.info("Bot successfully started. Лог: %s", LOG_FILE)
        start_expiry_checker()

        # Запускаем Flask webhook в отдельном потоке
        # [FIX] Если сервер вебхуков падает, бот продолжает работать, но оплаты перестают
        # приниматься — и раньше этого никто не замечал. Теперь: запись в лог, сообщение
        # админу (один раз) и повторный запуск через 30 секунд.
        def run_flask():
            admin_notified = False
            while True:
                try:
                    app.run(host='127.0.0.1', port=FLASK_PORT, debug=False)
                    log.error("Flask-сервер вебхуков неожиданно остановился")
                except Exception:
                    log.exception("Flask-сервер вебхуков упал")
                if not admin_notified:
                    admin_notified = True
                    try:
                        bot.send_message(
                            ADMIN_ID,
                            "⚠️ Сервер вебхуков ЮKassa упал — оплаты сейчас не принимаются.\n"
                            "Перезапускаю каждые 30 секунд. Подробности — в logs/bot.log."
                        )
                    except Exception:
                        log.exception("Не удалось уведомить админа о падении Flask")
                time.sleep(30)

        flask_thread = threading.Thread(target=run_flask, name="flask", daemon=True)
        flask_thread.start()
        log.info(f"🌐 Flask webhook сервер запущен на http://127.0.0.1:{FLASK_PORT}")

        bot.infinity_polling(skip_pending=True)
    except Exception:
        log.exception("Fatal error")
