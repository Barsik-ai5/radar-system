import os
import asyncio
import requests
import time
import re
import json
from datetime import datetime
from html import escape

from pyrogram import Client, idle
from google import genai
from google.genai import types
from google.genai.errors import APIError
from dotenv import load_dotenv


load_dotenv()


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def log_msg(level, tag, msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [{level}] [{tag}] {msg}")


# ============================================================
# ENV
# ============================================================

API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if (
    not API_ID_RAW
    or not API_HASH
    or not SESSION_STRING
    or API_HASH == "ТВОЙ_API_HASH_ИЗ_ENV"
):
    raise RuntimeError(
        "[-] ОШИБКА СТАРТА: Не заданы API_ID, API_HASH или SESSION_STRING в .env!"
    )

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise RuntimeError(
        "[-] ОШИБКА СТАРТА: API_ID должен быть числом!"
    )


# ============================================================
# GEMINI
# ============================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "[-] ОШИБКА СТАРТА: Не задан GEMINI_API_KEY в .env!"
    )

MODEL_NAME = "gemini-3.5-flash-lite"

# Timeout SDK в миллисекундах.
# Дополнительно ниже используется asyncio.wait_for().
AI_HTTP_TIMEOUT_MS = 45000

ai_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=AI_HTTP_TIMEOUT_MS),
)

AI_CONFIG = types.GenerateContentConfig(
    safety_settings=[
        types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT",
            threshold="BLOCK_NONE",
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH",
            threshold="BLOCK_NONE",
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
            threshold="BLOCK_NONE",
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT",
            threshold="BLOCK_NONE",
        ),
    ],
    max_output_tokens=250,
)


# ============================================================
# КОНФИГУРАЦИЯ БОТОВ
# ============================================================

BOTS_CONFIG = {
    "SPB": {
        "token": os.getenv("BOT_SPB"),
        "channel": "@RadarLO_SPB",
        "name": "Питер и Ленинградская область",
    },
    "MSK": {
        "token": os.getenv("BOT_MSK"),
        "channel": "@Radar_MSK_OBL",
        "name": "Москва и Московская область",
    },
    "BELGOROD": {
        "token": os.getenv("BOT_BELGOROD"),
        "channel": "@Radar_Belgorod_Obl",
        "name": "Белгород и Белгородская область",
    },
    "KURSK": {
        "token": os.getenv("BOT_KURSK"),
        "channel": "@Radar_Kursk",
        "name": "Курск и Курская область",
    },
}


SOURCE_ROUTING = {
    "locatorru": ["KURSK"],
    "vrv_radar": ["SPB", "MSK"],
    "radar_ru_belgorod": ["BELGOROD"],
}


ALLOWED_REGIONS = {"SPB", "MSK", "BELGOROD", "KURSK"}

TRAFFIC_EMOJIS = {
    "🔴",
    "🟡",
    "🟢",
    "⚪",
}


# ============================================================
# КЛЮЧЕВЫЕ СЛОВА ПРЕДФИЛЬТРА
# ============================================================

TARGET_KEYWORDS = [
    "белгород",
    "шебекин",
    "валуй",
    "грайворон",
    "оскол",
    "губкин",
    "волоконов",
    "борисов",
    "ивня",
    "ракитн",
    "краснояруж",
    "алексеев",
    "короч",
    "вейделев",
    "ровен",
    "чернян",
    "прохоров",
    "строител",
    "октябрьск",
    "томаров",
    "тавров",
    "дубов",
    "майск",
    "разумн",
    "беломестн",
    "стрелецк",
    "головчин",
    "бессонов",
    "красн",

    "курск",
    "курчат",
    "судж",
    "рыльск",
    "обоян",
    "льгов",
    "коренев",
    "глушков",
    "беловск",
    "железногорск",
    "фатеж",
    "щигр",
    "дмитриев",
    "тим",
    "горшечн",
    "поныр",
    "медвен",
    "золотухин",
    "мантуров",
    "солнцев",
    "черемисин",
    "касторн",
    "пристен",
    "прямицын",
    "теткин",

    "москв",
    "мск",
    "подмосков",
    "подольск",
    "люберц",
    "королев",
    "химк",
    "балаших",
    "мытищ",
    "красногорск",
    "одинцов",
    "домодедов",
    "зеленоград",
    "раменск",
    "ступин",
    "кашир",
    "коломн",
    "чехов",
    "серпухов",

    "петербург",
    "питер",
    "спб",
    "ленинград",
    "ленобласт",
    "выборг",
    "гатчин",
    "кронштадт",
    "луг",
    "кингисепп",
    "волхов",
    "тихвин",
    "всеволожск",
    "мурин",
    "кудров",
    "тосн",

    "днепро",
    "днепропетровск",
    "харьков",
    "сумы",
    "авиаторск",
    "волчанск",
    "полтав",

    "отбой",
]


# ============================================================
# СОСТОЯНИЕ
# ============================================================

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "alerts_state.json",
)

state_lock = asyncio.Lock()


def normalize_state(data):
    """
    Приводит state к единому формату:

    {
        "chat_id_message_id": {
            "ts": 1234567890,
            "regions": {
                "KURSK": 123
            }
        }
    }

    Также мигрирует старый формат:
    {
        "chat_id_message_id": {
            "KURSK": 123
        }
    }
    """

    if not isinstance(data, dict):
        return {}

    normalized = {}
    now = time.time()

    for key, value in data.items():
        if not isinstance(key, str):
            continue

        # Новый формат
        if isinstance(value, dict) and isinstance(value.get("regions"), dict):
            ts = value.get("ts", now)

            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = now

            regions_raw = value.get("regions", {})
            regions = {}

            for region, message_id in regions_raw.items():
                if region not in ALLOWED_REGIONS:
                    continue

                try:
                    regions[region] = int(message_id)
                except (TypeError, ValueError):
                    continue

            if regions:
                normalized[key] = {
                    "ts": ts,
                    "regions": regions,
                }

            continue

        # Старый формат
        if isinstance(value, dict):
            regions = {}

            for region, message_id in value.items():
                if region not in ALLOWED_REGIONS:
                    continue

                try:
                    regions[region] = int(message_id)
                except (TypeError, ValueError):
                    continue

            if regions:
                normalized[key] = {
                    "ts": now,
                    "regions": regions,
                }

    return normalized


def load_alerts_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        normalized = normalize_state(raw_data)

        if raw_data != normalized:
            try:
                backup_name = (
                    STATE_FILE
                    + ".migrated."
                    + datetime.now().strftime("%Y%m%d_%H%M%S")
                )
                with open(
                    backup_name,
                    "w",
                    encoding="utf-8",
                ) as backup:
                    json.dump(
                        raw_data,
                        backup,
                        ensure_ascii=False,
                        indent=2,
                    )

                log_msg(
                    "INFO",
                    "STATE",
                    f"Старый state мигрирован. Backup: {backup_name}",
                )
            except Exception as e:
                log_msg(
                    "WARN",
                    "STATE",
                    f"Не удалось создать backup старого state: {e}",
                )

        return normalized

    except Exception as e:
        log_msg(
            "ERROR",
            "STATE",
            f"Поврежден alerts_state.json: {e}",
        )

        try:
            corrupted_name = (
                STATE_FILE
                + ".corrupted."
                + datetime.now().strftime("%Y%m%d_%H%M%S")
            )
            os.replace(STATE_FILE, corrupted_name)

            log_msg(
                "WARN",
                "STATE",
                f"Поврежденный state сохранен как {corrupted_name}",
            )

        except Exception as backup_error:
            log_msg(
                "ERROR",
                "STATE",
                f"Не удалось сохранить поврежденный state: {backup_error}",
            )

        return {}


LAST_ALERT_IDS = load_alerts_state()


async def _save_state_unlocked():
    """
    Вызывать только когда state_lock уже захвачен.
    """
    try:
        now = time.time()

        # Удаляем записи старше 24 часов.
        old_keys = []

        for key, value in LAST_ALERT_IDS.items():
            if not isinstance(value, dict):
                old_keys.append(key)
                continue

            ts = value.get("ts", 0)

            try:
                ts = float(ts)
            except (TypeError, ValueError):
                old_keys.append(key)
                continue

            if now - ts > 86400:
                old_keys.append(key)

        for key in old_keys:
            LAST_ALERT_IDS.pop(key, None)

        tmp_file = STATE_FILE + ".tmp"

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(
                LAST_ALERT_IDS,
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_file, STATE_FILE)

    except Exception as e:
        log_msg(
            "ERROR",
            "STATE",
            f"Ошибка атомарного сохранения состояния: {e}",
        )


async def save_alerts_state_async():
    async with state_lock:
        await _save_state_unlocked()


# ============================================================
# DEDUPLICATION
# ============================================================

PROCESSED_MESSAGES = {}
PROCESSED_TTL = 21600  # 6 часов


def is_duplicate_message(source_msg_key):
    now = time.time()

    # Удаляем старые записи.
    old_keys = [
        key
        for key, ts in PROCESSED_MESSAGES.items()
        if now - ts > PROCESSED_TTL
    ]

    for key in old_keys:
        PROCESSED_MESSAGES.pop(key, None)

    if source_msg_key in PROCESSED_MESSAGES:
        return True

    PROCESSED_MESSAGES[source_msg_key] = now

    # Защита от чрезмерного роста.
    if len(PROCESSED_MESSAGES) > 1000:
        sorted_keys = sorted(
            PROCESSED_MESSAGES.items(),
            key=lambda item: item[1],
        )

        for key, _ in sorted_keys[:200]:
            PROCESSED_MESSAGES.pop(key, None)

    return False


# ============================================================
# PYROGRAM
# ============================================================

app = Client(
    "radar_bot",
    session_string=SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH,
)


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api_get(method, token, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"

    try:
        response = requests.get(
            url,
            params=params or {},
            timeout=10,
        )

        try:
            data = response.json()
        except ValueError:
            data = {}

        return response, data

    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Сетевая ошибка Telegram API: {e}"
        )


def check_bot_health(region, info):
    token = info.get("token")
    channel = info.get("channel")

    if not token:
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Не задан BOT-токен для {region}!"
        )

    # 1. Проверяем токен.
    response, data = telegram_api_get(
        "getMe",
        token,
    )

    if response.status_code != 200 or not data.get("ok"):
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Неверный BOT-токен для {region}. "
            f"Telegram: {response.text}"
        )

    bot_user = data.get("result", {})
    bot_id = bot_user.get("id")

    if not bot_id:
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Telegram не вернул ID бота {region}."
        )

    # 2. Проверяем канал.
    response, data = telegram_api_get(
        "getChat",
        token,
        {"chat_id": channel},
    )

    if response.status_code != 200 or not data.get("ok"):
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Бот {region} не имеет доступа к "
            f"{channel}. Telegram: {response.text}"
        )

    chat = data.get("result", {})

    actual_username = (chat.get("username") or "").lower()
    expected_username = channel.lstrip("@").lower()

    if actual_username != expected_username:
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Для {region} ожидался канал "
            f"{channel}, но Telegram вернул username @{actual_username}."
        )

    # 3. Проверяем права бота.
    response, data = telegram_api_get(
        "getChatMember",
        token,
        {
            "chat_id": channel,
            "user_id": bot_id,
        },
    )

    if response.status_code != 200 or not data.get("ok"):
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Не удалось проверить права бота "
            f"{region} в {channel}. Telegram: {response.text}"
        )

    member = data.get("result", {})
    status = member.get("status")

    if status not in {"administrator", "creator"}:
        raise RuntimeError(
            f"[-] ОШИБКА СТАРТА: Бот {region} не является администратором "
            f"{channel}. Статус: {status}"
        )

    # У creator поле может отсутствовать.
    if status == "administrator":
        if member.get("can_post_messages") is False:
            raise RuntimeError(
                f"[-] ОШИБКА СТАРТА: Боту {region} запрещено "
                f"публиковать сообщения в {channel}."
            )

    log_msg(
        "INFO",
        "HEALTH",
        f"{region}: бот и канал проверены успешно.",
    )


for region, info in BOTS_CONFIG.items():
    check_bot_health(region, info)


# ============================================================
# TELEGRAM SEND
# ============================================================

def build_final_telegram_text(region, text):
    bot_info = BOTS_CONFIG[region]

    safe_name = escape(bot_info["name"], quote=True)

    channel_link = (
        "https://t.me/"
        + bot_info["channel"].lstrip("@")
    )

    cta = (
        f"📡 Дозор.ру | {safe_name} - "
        f'<a href="{channel_link}">Подписаться</a>'
    )

    # Telegram: максимум 4096 символов после обработки entities.
    # Ищем максимальный кусок исходного текста, который безопасно
    # помещается вместе с CTA.
    available = 4096 - len(cta) - 2

    if available < 100:
        available = 100

    source_text = text or ""

    escaped_full = escape(source_text, quote=True)

    if len(escaped_full) <= available:
        safe_text = escaped_full
    else:
        # Бинарный поиск по исходной строке, чтобы не порезать
        # HTML entity вроде &quot; пополам.
        left = 0
        right = len(source_text)

        best = ""

        while left <= right:
            mid = (left + right) // 2

            candidate = escape(
                source_text[:mid],
                quote=True,
            )

            if len(candidate) <= available - 1:
                best = candidate
                left = mid + 1
            else:
                right = mid - 1

        safe_text = best.rstrip() + "…"

    return (
        f"{safe_text}\n\n"
        f"{cta}"
    )


def sync_send_to_channel(region, text, reply_to=None):
    bot_info = BOTS_CONFIG.get(region)

    if not bot_info or not bot_info.get("token"):
        log_msg(
            "ERROR",
            "TG",
            f"Нет конфигурации бота для {region}.",
        )
        return None

    url = (
        f"https://api.telegram.org/bot"
        f"{bot_info['token']}/sendMessage"
    )

    final_text = build_final_telegram_text(
        region,
        text,
    )

    payload = {
        "chat_id": bot_info["channel"],
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if reply_to:
        payload["reply_parameters"] = {
            "message_id": int(reply_to),
            # Не превращаем reply в обычное сообщение.
            "allow_sending_without_reply": False,
        }

    for attempt in range(3):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=15,
            )

            try:
                data = response.json()
            except ValueError:
                data = {}

            if response.status_code == 200 and data.get("ok"):
                return data["result"]["message_id"]

            if response.status_code == 429:
                retry_after = (
                    data.get("parameters", {})
                    .get("retry_after", 2)
                )

                try:
                    retry_after = max(
                        1,
                        int(retry_after),
                    )
                except (TypeError, ValueError):
                    retry_after = 2

                if attempt >= 2:
                    log_msg(
                        "ERROR",
                        "TG",
                        f"Последняя попытка Telegram 429 "
                        f"для {region}. Больше не повторяем.",
                    )
                    break

                log_msg(
                    "WARN",
                    "TG",
                    f"429 в {region}. Ждем "
                    f"{retry_after} сек. "
                    f"Попытка {attempt + 1}/3.",
                )

                time.sleep(retry_after)
                continue

            if response.status_code in {
                400,
                401,
                403,
                404,
            }:
                log_msg(
                    "ERROR",
                    "TG",
                    f"Фатальная ошибка Telegram "
                    f"{response.status_code} для {region}: "
                    f"{response.text}",
                )
                break

            if 500 <= response.status_code <= 599:
                if attempt >= 2:
                    log_msg(
                        "ERROR",
                        "TG",
                        f"Telegram {response.status_code} "
                        f"после 3 попыток в {region}.",
                    )
                    break

                log_msg(
                    "WARN",
                    "TG",
                    f"Временная ошибка {response.status_code} "
                    f"в {region}. Попытка {attempt + 1}/3.",
                )

                time.sleep(2)
                continue

            log_msg(
                "ERROR",
                "TG",
                f"Неожиданный HTTP {response.status_code} "
                f"в {region}: {response.text}",
            )
            break

        except requests.exceptions.ReadTimeout:
            # Важно:
            # Telegram мог получить сообщение, но ответ не успел вернуться.
            # Повторять sendMessage здесь опасно — возможен дубль.
            log_msg(
                "ERROR",
                "TG",
                f"ReadTimeout при отправке в {region}. "
                f"Сообщение могло быть принято Telegram. "
                f"Retry отключен во избежание дубля.",
            )
            break

        except requests.exceptions.RequestException as e:
            if attempt >= 2:
                log_msg(
                    "ERROR",
                    "TG",
                    f"Сетевая ошибка Telegram после 3 попыток "
                    f"в {region}: {e}",
                )
                break

            log_msg(
                "WARN",
                "TG",
                f"Временная сетевая ошибка в {region}: {e}. "
                f"Попытка {attempt + 1}/3.",
            )

            time.sleep(2)

        except Exception as e:
            log_msg(
                "ERROR",
                "TG",
                f"Неожиданная ошибка отправки в {region}: {e}",
            )
            break

    return None


# ============================================================
# TEXT CLEANING
# ============================================================

def strip_source_attribution(text, source):
    """
    Убирает очевидные ссылки/упоминания исходного источника.
    Основная очистка также прописана в prompt.
    """

    if not text:
        return ""

    source_lower = source.lower()

    patterns = [
        rf"@{re.escape(source_lower)}",
        rf"https?://t\.me/{re.escape(source_lower)}\b",
        rf"https?://telegram\.me/{re.escape(source_lower)}\b",
        rf"t\.me/{re.escape(source_lower)}\b",
    ]

    cleaned = text

    for pattern in patterns:
        cleaned = re.sub(
            pattern,
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

    # Удаляем голые ссылки t.me, если они остались.
    cleaned = re.sub(
        r"https?://t\.me/\S+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"(?im)^\s*(источник|source)\s*:\s*.*$",
        "",
        cleaned,
    )

    cleaned = re.sub(
        r"\n{3,}",
        "\n\n",
        cleaned,
    )

    return cleaned.strip()


# ============================================================
# AI PARSER
# ============================================================

def normalize_ai_text(text):
    if not text:
        return ""

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = text.strip()

    # Иногда модель возвращает ```text ... ```
    text = re.sub(
        r"^\s*```(?:text)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s*```\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return text.strip()


def detect_traffic_emojis(text):
    return re.findall(
        r"🔴|🟡|🟢|⚪️?",
        text,
    )


def validate_ai_result(result, allowed_regions):
    """
    Возвращает:
        {
            "REGION": "🔴 текст"
        }

    либо None при нарушении формата.
    """

    result = normalize_ai_text(result)

    if not result:
        return None

    if result.upper() == "ИГНОР":
        return {}

    # Ищем все известные заголовки регионов.
    header_matches = list(
        re.finditer(
            r"(?m)^\[(SPB|MSK|BELGOROD|KURSK)\]\s*",
            result,
            flags=re.IGNORECASE,
        )
    )

    # Если есть квадратные теги, которые вообще не известны —
    # считаем ответ плохим.
    any_bracket_tags = re.findall(
        r"\[([A-Za-z_]+)\]",
        result,
    )

    for tag in any_bracket_tags:
        if tag.upper() not in ALLOWED_REGIONS:
            log_msg(
                "WARN",
                "AI",
                f"AI выдал неизвестный тег [{tag}].",
            )
            return None

    if not header_matches:
        return None

    parsed = {}

    for index, match in enumerate(header_matches):
        region = match.group(1).upper()

        if region not in allowed_regions:
            # AI явно направил сообщение не в тот регион.
            log_msg(
                "WARN",
                "AI",
                f"AI указал запрещенный для источника регион: {region}.",
            )
            return None

        if region in parsed:
            log_msg(
                "WARN",
                "AI",
                f"AI выдал регион {region} больше одного раза.",
            )
            return None

        start = match.end()

        if index + 1 < len(header_matches):
            end = header_matches[index + 1].start()
        else:
            end = len(result)

        region_text = result[start:end].strip()

        if not region_text:
            log_msg(
                "WARN",
                "AI",
                f"AI выдал пустой блок [{region}].",
            )
            return None

        emojis = detect_traffic_emojis(region_text)

        # Ровно один цвет.
        if len(emojis) != 1:
            log_msg(
                "WARN",
                "AI",
                f"В блоке [{region}] найдено цветов: {emojis}",
            )
            return None

        # Никаких неизвестных traffic emoji.
        parsed[region] = region_text

    return parsed


def extract_region_text(result, region):
    parsed = validate_ai_result(
        result,
        [region],
    )

    if not parsed:
        return None

    return parsed.get(region)


# ============================================================
# AI
# ============================================================

async def process_with_ai(text, source):
    text = text[:10000]

    prompt = f"""
Ты — строгий классификатор сообщений радара.

Источник: {source}

ТЕКСТ:
{text}

ТВОЯ ЗАДАЧА:
Определить, является ли сообщение:
1) отбоем;
2) мусором;
3) активной угрозой;
4) постфактум-информацией.

После этого определить регион и вернуть только нужный текст.

ПРИОРИТЕТЫ:
1. ОТБОЙ
2. МУСОР
3. АКТИВНАЯ УГРОЗА
4. ПОСТФАКТУМ
5. РЕГИОН

ОТБОЙ:
Если присутствует блок "Текущее обновление", приоритет имеет ТОЛЬКО текущее обновление.
Если текущее обновление сообщает "отбой", результат ОБЯЗАТЕЛЬНО:
🟢 Отбой

Фраза "угроза сохраняется" или "опасность сохраняется" в ИСХОДНОМ сообщении
НЕ должна превращать сообщение в ИГНОР, если ТЕКУЩЕЕ ОБНОВЛЕНИЕ сообщает об отбое.

Если это отбой без названия региона, вернуть:
🟢 Отбой

МУСОР:
Верни только:
ИГНОР

если сообщение является:
- рекламой;
- сбором денег;
- рекламным CTA;
- пожеланием доброй ночи/спокойной ночи;
- повтором без новой информации;
- сообщением "угроза сохраняется"/"опасность сохраняется" без нового события.

ЦВЕТА:

🔴 КРАСНЫЙ:
- ракетная/авиационная опасность;
- баллистика;
- обстрел;
- РСЗО;
- FPV;
- УАБ;
- активная тревога по БПЛА;
- обнаружение БПЛА;
- движение/полет БПЛА;
- работа ПВО;
- текущая угроза от ударных БПЛА;
- внимание по падающим осколкам/обломкам.

Если ударные БПЛА обнаружены, движутся или представляют текущую угрозу:
СТРОГО 🔴.

🟡 ЖЕЛТЫЙ:
- предупреждение по БПЛА;
- "опасность по БПЛА";
- "внимание по БПЛА",
если при этом нет признаков текущей прямой угрозы.

🟢 ЗЕЛЕНЫЙ:
- любой отбой.

⚪ БЕЛЫЙ:
- последствия уже прошедшей атаки;
- разрушения;
- последствия на земле;
- уже сбитые/уничтоженные цели,
если текущей угрозы нет.

РЕГИОНЫ:

SPB = Санкт-Петербург / Ленинградская область
MSK = Москва / Московская область
BELGOROD = Белгородская область
KURSK = Курская область

Если перечислено несколько регионов, удаляй все чужие.
Оставляй только регионы, относящиеся к разрешенному источнику.

МАРШРУТИЗАЦИЯ:
locatorru -> только KURSK
radar_ru_belgorod -> только BELGOROD
vrv_radar -> SPB и MSK

УДАЛЯЙ ИЗ ИТОГОВОГО ТЕКСТА:
- название исходного канала;
- @username исходного канала;
- t.me ссылки;
- ссылки на источник;
- "подписаться";
- рекламные CTA;
- подписи автора/источника;
- служебные подписи радара.

НИКОГДА не добавляй название источника самостоятельно.

ФОРМАТ:

Обычный случай:
[REGION] текст

Несколько регионов:
[SPB] текст
[MSK] текст

Для каждого региона допускается РОВНО ОДИН цвет.

НЕ ДОБАВЛЯЙ:
- комментарии;
- объяснения;
- Markdown;
- ``` ;
- слова "результат";
- лишние регионы.

Если сообщение не относится к отслеживаемым регионам:
ИГНОР
"""

    for attempt in range(3):
        try:
            # Ограничиваем время конкретного запроса.
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt,
                    config=AI_CONFIG,
                ),
                timeout=50,
            )

            try:
                response_text = response.text
            except Exception as e:
                log_msg(
                    "WARN",
                    "AI",
                    f"Не удалось прочитать response.text: {e}",
                )
                response_text = None

            if not response_text or not response_text.strip():
                if attempt >= 2:
                    log_msg(
                        "ERROR",
                        "AI",
                        "Gemini трижды вернул пустой ответ.",
                    )
                    return None

                log_msg(
                    "WARN",
                    "AI",
                    f"Пустой ответ Gemini. Повтор {attempt + 1}/3.",
                )

                await asyncio.sleep(2)
                continue

            return normalize_ai_text(response_text)

        except asyncio.TimeoutError:
            if attempt >= 2:
                log_msg(
                    "ERROR",
                    "AI",
                    "Gemini timeout после 3 попыток.",
                )
                return None

            log_msg(
                "WARN",
                "AI",
                f"Timeout Gemini. Повтор {attempt + 1}/3.",
            )

            await asyncio.sleep(2)

        except APIError as e:
            error_msg = str(e).lower()

            is_429 = (
                "429" in error_msg
                or "quota" in error_msg
                or "resource_exhausted" in error_msg
            )

            is_safety = (
                "safety" in error_msg
                or "blocked" in error_msg
            )

            if is_safety:
                log_msg(
                    "WARN",
                    "AI",
                    f"Gemini Safety блокировка: {e}",
                )
                return "ИГНОР"

            if is_429:
                if attempt >= 2:
                    log_msg(
                        "ERROR",
                        "AI",
                        "Последний запрос Gemini получил 429.",
                    )
                    return None

                # Важно: мы НЕ держим внешний ai_lock
                # во время этого sleep.
                log_msg(
                    "WARN",
                    "AI",
                    f"Лимит Gemini 429. "
                    f"Повтор через 30 сек. "
                    f"Попытка {attempt + 1}/3.",
                )

                await asyncio.sleep(30)
                continue

            if attempt >= 2:
                log_msg(
                    "ERROR",
                    "AI",
                    f"Gemini APIError после 3 попыток: {e}",
                )
                return None

            log_msg(
                "WARN",
                "AI",
                f"Временная ошибка Gemini: {e}. "
                f"Повтор {attempt + 1}/3.",
            )

            await asyncio.sleep(3)

        except Exception as e:
            if attempt >= 2:
                log_msg(
                    "ERROR",
                    "AI",
                    f"Gemini неизвестная ошибка после 3 попыток: {e}",
                )
                return None

            log_msg(
                "WARN",
                "AI",
                f"Сетевая/неизвестная ошибка Gemini: {e}. "
                f"Повтор {attempt + 1}/3.",
            )

            await asyncio.sleep(3)

    return None


# ============================================================
# AI LOCK
# ============================================================

ai_lock = asyncio.Lock()


# ============================================================
# ПРЕДФИЛЬТР
# ============================================================

def passes_prefilter(username, text):
    if username not in {
        "locatorru",
        "vrv_radar",
    }:
        return True

    text_lower = text.lower()

    for kw in TARGET_KEYWORDS:
        # Для коротких ключей используем начало слова.
        if len(kw) <= 4:
            if re.search(
                rf"\b{re.escape(kw)}",
                text_lower,
                flags=re.IGNORECASE,
            ):
                return True

        else:
            if kw in text_lower:
                return True

    return False


# ============================================================
# STATE HELPERS
# ============================================================

async def get_saved_reply_id(reply_source_key, region):
    if not reply_source_key:
        return None

    async with state_lock:
        record = LAST_ALERT_IDS.get(reply_source_key)

        if not isinstance(record, dict):
            return None

        regions = record.get("regions")

        if not isinstance(regions, dict):
            return None

        message_id = regions.get(region)

        try:
            return int(message_id)
        except (TypeError, ValueError):
            return None


# ============================================================
# MAIN HANDLER
# ============================================================

@app.on_message()
async def radar_handler(client, message):
    if not message.chat:
        return

    username = (
        message.chat.username or ""
    ).lower()

    if username not in SOURCE_ROUTING:
        return

    source_msg_key = (
        f"{message.chat.id}_{message.id}"
    )

    if is_duplicate_message(source_msg_key):
        log_msg(
            "INFO",
            "DEDUP",
            f"Пропущен повторный message_id={message.id} "
            f"из {username}.",
        )
        return

    log_msg(
        "INFO",
        "SYSTEM",
        f"Новое сообщение: "
        f"source={username}, "
        f"chat_id={message.chat.id}, "
        f"message_id={message.id}",
    )

    text = (
        message.text
        or message.caption
        or ""
    )

    if not text:
        log_msg(
            "INFO",
            "SYSTEM",
            f"Пустое текстовое сообщение {username}/{message.id}.",
        )
        return

    # Проверяем возраст до загрузки дополнительной reply-логики.
    if message.date:
        age = time.time() - message.date.timestamp()

        if age > 7200 or age < -300:
            log_msg(
                "INFO",
                "SYSTEM",
                f"Сообщение {username}/{message.id} "
                f"слишком старое/будущее. "
                f"Возраст: {int(age)} сек.",
            )
            return

    reply_source_key = None

    if message.reply_to_message:
        reply_source_key = (
            f"{message.chat.id}_"
            f"{message.reply_to_message.id}"
        )

        reply_text = (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        )

        if reply_text:
            text = (
                f"Исходное сообщение:\n"
                f"{reply_text}\n\n"
                f"Текущее обновление:\n"
                f"{text}"
            )

    # Предфильтр.
    if not passes_prefilter(
        username,
        text,
    ):
        log_msg(
            "INFO",
            "PREFILTER",
            f"{username}/{message.id}: "
            f"нет релевантных регионов/ключей. Скип.",
        )
        return

    log_msg(
        "INFO",
        "AI",
        f"{username}/{message.id}: сообщение прошло предфильтр.",
    )

    # 4.5 секунды задержки и один AI запрос за раз.
    # ВАЖНО: retry sleep внутри process_with_ai
    # уже не удерживает этот lock.
    async with ai_lock:
        await asyncio.sleep(4.5)

        log_msg(
            "INFO",
            "AI",
            f"Отправка в Gemini: "
            f"source={username}, message_id={message.id}",
        )

        result = await process_with_ai(
            text,
            username,
        )

    if not result:
        log_msg(
            "WARN",
            "AI",
            f"{username}/{message.id}: "
            f"Gemini не вернул результат.",
        )
        return

    result = normalize_ai_text(result)

    if result.upper() == "ИГНОР":
        log_msg(
            "INFO",
            "AI",
            f"{username}/{message.id}: ИГНОР.",
        )
        return

    log_msg(
        "INFO",
        "AI",
        f"Ответ Gemini:\n{result}",
    )

    clean_result = strip_source_attribution(
        result,
        username,
    )

    allowed_regions = SOURCE_ROUTING[username]

    # --------------------------------------------------------
    # Если это Отбой в reply:
    # ТОЛЬКО по памяти.
    # Никакого standalone fallback.
    # --------------------------------------------------------

    is_green_result = "🟢" in clean_result

    if is_green_result and reply_source_key:
        async with state_lock:
            record = LAST_ALERT_IDS.get(reply_source_key)

            if isinstance(record, dict):
                regions_dict = record.get("regions", {})
            else:
                regions_dict = {}

            if isinstance(regions_dict, dict):
                saved_regions = list(
                    regions_dict.keys()
                )
            else:
                saved_regions = []

        if not saved_regions:
            log_msg(
                "WARN",
                "CLEAR",
                f"{username}/{message.id}: "
                f"Отбой без сохраненной тревоги. "
                f"Standalone отправка запрещена.",
            )
            return

        clear_sent = False

        for region in saved_regions:
            reply_to = await get_saved_reply_id(
                reply_source_key,
                region,
            )

            if not reply_to:
                log_msg(
                    "WARN",
                    "CLEAR",
                    f"{username}/{message.id}: "
                    f"нет destination message_id для {region}.",
                )
                continue

            try:
                loop = asyncio.get_running_loop()

                msg_id = await loop.run_in_executor(
                    None,
                    sync_send_to_channel,
                    region,
                    clean_result,
                    reply_to,
                )

            except Exception as e:
                log_msg(
                    "ERROR",
                    "SEND",
                    f"Ошибка отправки Отбоя в {region}: {e}",
                )
                continue

            if not msg_id:
                log_msg(
                    "ERROR",
                    "CLEAR",
                    f"Отбой не отправлен в {region}. "
                    f"State оставлен для возможного следующего отбоя.",
                )
                continue

            clear_sent = True

            log_msg(
                "INFO",
                "CLEAR",
                f"Отбой отправлен в {region}: "
                f"destination_message_id={msg_id}, "
                f"reply_to={reply_to}",
            )

            async with state_lock:
                record = LAST_ALERT_IDS.get(
                    reply_source_key
                )

                if isinstance(record, dict):
                    regions = record.get("regions")

                    if isinstance(regions, dict):
                        regions.pop(region, None)

                        if not regions:
                            LAST_ALERT_IDS.pop(
                                reply_source_key,
                                None,
                            )

                await _save_state_unlocked()

        # Независимо от успеха отдельных направлений,
        # standalone fallback для reply-отбоя запрещен.
        if clear_sent or saved_regions:
            return

    # --------------------------------------------------------
    # Парсим ответ AI.
    # --------------------------------------------------------

    parsed = validate_ai_result(
        result,
        allowed_regions,
    )

    if parsed is None:
        log_msg(
            "WARN",
            "AI",
            f"{username}/{message.id}: "
            f"ответ Gemini не прошел строгую валидацию.",
        )
        return

    if parsed == {}:
        log_msg(
            "INFO",
            "AI",
            f"{username}/{message.id}: ИГНОР.",
        )
        return

    sent_any = False

    # --------------------------------------------------------
    # Обычные сообщения по тегам AI
    # --------------------------------------------------------

    for region in allowed_regions:
        region_text = parsed.get(region)

        if not region_text:
            continue

        # Очищаем возможные остаточные подписи источника.
        region_text = strip_source_attribution(
            region_text,
            username,
        )

        traffic = detect_traffic_emojis(
            region_text
        )

        if len(traffic) != 1:
            log_msg(
                "WARN",
                "AI",
                f"{username}/{message.id}/{region}: "
                f"невалидный traffic state.",
            )
            continue

        is_red = traffic[0] == "🔴"
        is_yellow = traffic[0] == "🟡"

        try:
            loop = asyncio.get_running_loop()

            msg_id = await loop.run_in_executor(
                None,
                sync_send_to_channel,
                region,
                region_text,
                None,
            )

        except Exception as e:
            log_msg(
                "ERROR",
                "SEND",
                f"Ошибка отправки в {region}: {e}",
            )
            continue

        if not msg_id:
            log_msg(
                "ERROR",
                "SEND",
                f"Не удалось отправить сообщение "
                f"в {region}.",
            )
            continue

        sent_any = True

        log_msg(
            "INFO",
            "SEND",
            f"Успешно отправлено: "
            f"source={username}, "
            f"message_id={message.id}, "
            f"region={region}, "
            f"destination_message_id={msg_id}",
        )

        # Только красные/желтые тревоги сохраняем
        # для будущего Отбоя.
        if is_red or is_yellow:
            async with state_lock:
                LAST_ALERT_IDS[source_msg_key] = {
                    "ts": time.time(),
                    "regions": {
                        region: int(msg_id),
                    },
                }

                await _save_state_unlocked()

    if sent_any:
        return

    # --------------------------------------------------------
    # БЕЗОПАСНЫЙ FALLBACK
    # --------------------------------------------------------

    # Если AI явно указал известный тег, но он не разрешен
    # для текущего source — ничего не переопределяем.
    explicit_tags = re.findall(
        r"(?m)^([A-Za-z_]+)",
        result,
    )

    normalized_explicit_tags = {
        tag.upper()
        for tag in explicit_tags
    }

    if normalized_explicit_tags:
        log_msg(
            "WARN",
            "SYSTEM",
            f"{username}/{message.id}: "
            f"AI явно указал регион(ы), "
            f"но отправка не выполнена. "
            f"Fallback запрещен.",
        )
        return

    # VRV никогда не получает принудительный fallback:
    # источник содержит сразу два региона.
    if username == "vrv_radar":
        log_msg(
            "WARN",
            "SYSTEM",
            f"VRV: AI не дал корректный тег. "
            f"Fallback запрещен.",
        )
        return

    # Для однорегиональных источников fallback допустим,
    # но только если цвет ровно один.
    traffic = detect_traffic_emojis(
        clean_result
    )

    if len(traffic) != 1:
        log_msg(
            "WARN",
            "SYSTEM",
            f"{username}/{message.id}: "
            f"Fallback запрещен — результат AI "
            f"не содержит ровно один цвет.",
        )
        return

    # Зеленый reply уже обработан выше.
    # Здесь зеленый standalone допускается только если
    # это НЕ reply.
    if "🟢" in clean_result and reply_source_key:
        log_msg(
            "WARN",
            "SYSTEM",
            f"{username}/{message.id}: "
            f"standalone Отбой для reply запрещен.",
        )
        return

    fallback_region = allowed_regions[0]

    try:
        loop = asyncio.get_running_loop()

        msg_id = await loop.run_in_executor(
            None,
            sync_send_to_channel,
            fallback_region,
            clean_result,
            None,
        )

    except Exception as e:
        log_msg(
            "ERROR",
            "SEND",
            f"Ошибка fallback отправки: {e}",
        )
        return

    if msg_id:
        log_msg(
            "INFO",
            "SEND",
            f"Fallback успешно отправлен: "
            f"source={username}, "
            f"message_id={message.id}, "
            f"region={fallback_region}, "
            f"destination_message_id={msg_id}",
        )
    else:
        log_msg(
            "ERROR",
            "SEND",
            f"Fallback НЕ отправлен: "
            f"{username}/{message.id}",
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    log_msg(
        "INFO",
        "SYSTEM",
        "=======================================",
    )

    log_msg(
        "INFO",
        "SYSTEM",
        "Диспетчер ИИ-Радара запускается...",
    )

    log_msg(
        "INFO",
        "SYSTEM",
        "=======================================",
    )

    await app.start()

    try:
        # Синхронизируем диалоги Pyrogram.
        try:
            async for _ in app.get_dialogs():
                pass

            log_msg(
                "INFO",
                "SYSTEM",
                "Синхронизация диалогов завершена.",
            )

        except Exception as e:
            log_msg(
                "ERROR",
                "SYSTEM",
                f"Ошибка синхронизации диалогов: {e}",
            )

        # Сохраняем нормализованный state при старте.
        await save_alerts_state_async()

        log_msg(
            "INFO",
            "SYSTEM",
            "=======================================",
        )

        log_msg(
            "INFO",
            "SYSTEM",
            "Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН.",
        )

        log_msg(
            "INFO",
            "SYSTEM",
            "Источники: locatorru, vrv_radar, radar_ru_belgorod",
        )

        log_msg(
            "INFO",
            "SYSTEM",
            "Маршрутизация: KURSK / SPB+MSK / BELGOROD",
        )

        log_msg(
            "INFO",
            "SYSTEM",
            "Ожидание новых сообщений...",
        )

        log_msg(
            "INFO",
            "SYSTEM",
            "=======================================",
        )

        await idle()

    finally:
        await app.stop()

        log_msg(
            "INFO",
            "SYSTEM",
            "Бот безопасно остановлен.",
        )


if __name__ == "__main__":
    app.run(main())
