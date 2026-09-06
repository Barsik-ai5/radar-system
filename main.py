import asyncio
import html
import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any

import aiohttp
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pyrogram import Client, filters, idle
from pyrogram.types import Message

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

API_ID = 36567125
API_HASH = "74f27c0240ce52057f170f7b119d74f3"

SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

DATA_DIR = os.getenv("DATA_DIR", "/app/data").strip() or "/app/data"
STATE_FILE = os.path.join(DATA_DIR, "radar_state.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
WORKERS = max(2, int(os.getenv("RADAR_WORKERS", "3")))
GEMINI_TIMEOUT = max(5, int(os.getenv("GEMINI_TIMEOUT", "12")))
GEMINI_RETRIES = max(1, int(os.getenv("GEMINI_RETRIES", "2")))
QUEUE_LIMIT = max(100, int(os.getenv("RADAR_QUEUE_LIMIT", "1000")))

# Every region has ONE primary source. This is deliberate:
# it prevents the same event from arriving through multiple sources.
REGION_SOURCE = {
    "KURSK": "@locatorru",
    "MSK": "@vrv_radar",
    "SPB": "@vrv_radar",
    "BELGOROD": "@radar_ru_belgorod",
}

SOURCE_REGIONS = {
    "@locatorru": {"KURSK"},
    "@vrv_radar": {"MSK", "SPB"},
    "@radar_ru_belgorod": {"BELGOROD"},
}

BOTS = {
    "SPB": {
        "token": os.getenv("BOT_SPB", "").strip(),
        "channel": "@RadarLO_SPB",
        "button": "📡 Радар Питер и Ленинградская область | Подписаться",
    },
    "MSK": {
        "token": os.getenv("BOT_MSK", "").strip(),
        "channel": "@Radar_MSK_OBL",
        "button": "📡 Радар Москва и Московская область | Подписаться",
    },
    "BELGOROD": {
        "token": os.getenv("BOT_BELGOROD", "").strip(),
        "channel": "@Radar_Belgorod_Obl",
        "button": "📡 Радар Белгород и Белгородская область | Подписаться",
    },
    "KURSK": {
        "token": os.getenv("BOT_KURSK", "").strip(),
        "channel": "@Radar_Kursk",
        "button": "📡 Радар Курск и Курская область | Подписаться",
    },
}

REGION_DISPLAY = {
    "KURSK": "Курская область",
    "MSK": "Москва и Московская область",
    "SPB": "Санкт-Петербург и Ленинградская область",
    "BELGOROD": "Белгородская область",
}

# ============================================================
# DIRECTORIES / LOGGING
# ============================================================

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("radar")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

activity_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "radar.log"),
    maxBytes=2_000_000,
    backupCount=5,
    encoding="utf-8",
)
activity_handler.setFormatter(formatter)
logger.addHandler(activity_handler)

error_logger = logging.getLogger("radar_errors")
error_logger.setLevel(logging.ERROR)
error_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "errors.log"),
    maxBytes=2_000_000,
    backupCount=5,
    encoding="utf-8",
)
error_handler.setFormatter(formatter)
error_logger.addHandler(error_handler)

# ============================================================
# STATE
# ============================================================

state_lock = asyncio.Lock()

DEFAULT_STATE = {
    "version": 1,
    "published": {},
    "source_links": {},
    "active": {},
}

state: dict[str, Any] = DEFAULT_STATE.copy()


def deep_copy_default() -> dict[str, Any]:
    return {
        "version": 1,
        "published": {},
        "source_links": {},
        "active": {},
    }


def load_state() -> None:
    global state

    if not os.path.exists(STATE_FILE):
        state = deep_copy_default()
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        if not isinstance(loaded, dict):
            raise ValueError("state file is not a JSON object")

        base = deep_copy_default()
        for key in base:
            if key in loaded and isinstance(loaded[key], type(base[key])):
                base[key] = loaded[key]

        state = base
        logger.info(
            "STATE loaded: published=%d source_links=%d active=%d",
            len(state["published"]),
            len(state["source_links"]),
            len(state["active"]),
        )
    except Exception as exc:
        error_logger.exception("STATE LOAD ERROR: %s", exc)
        # Fail safe: start with empty state rather than crash.
        state = deep_copy_default()


def trim_state_unlocked() -> None:
    # Keep the JSON small and practical for a long-running bot.
    max_published = 5000
    max_links = 5000

    if len(state["published"]) > max_published:
        items = list(state["published"].items())
        items.sort(key=lambda x: x[1].get("created_at", 0))
        for key, _ in items[:-max_published]:
            state["published"].pop(key, None)

    if len(state["source_links"]) > max_links:
        items = list(state["source_links"].items())
        items.sort(key=lambda x: x[1].get("created_at", 0) if isinstance(x[1], dict) else 0)
        for key, _ in items[:-max_links]:
            state["source_links"].pop(key, None)


def save_state_unlocked() -> None:
    trim_state_unlocked()

    directory = os.path.dirname(STATE_FILE) or "."
    fd, temp_path = tempfile.mkstemp(prefix="radar_state_", suffix=".json", dir=directory)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def source_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def publication_key(chat_id: int, message_id: int, region: str) -> str:
    return f"{chat_id}:{message_id}:{region}"


# ============================================================
# GEMINI
# ============================================================

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["ALERT", "CLEAR", "IGNORE", "UNCERTAIN"],
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "enum": ["BELGOROD", "KURSK", "MSK", "SPB"],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["RED", "YELLOW"],
                    },
                    "text": {"type": "string"},
                },
                "required": ["region", "severity", "text"],
            },
        },
        "event_type": {
            "type": "string",
            "enum": ["UAV", "ROCKET", "OTHER"],
        },
    },
    "required": ["decision", "items", "event_type"],
}


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def is_plain_clear(text: str) -> bool:
    value = normalise(text)
    return value in {
        "отбой",
        "отбой.",
        "отбой!",
        "опасность отмена",
        "отмена опасности",
        "опасность снята",
        "угроза снята",
    }


GENERIC_NOISE_PATTERNS = [
    r"\bугроза\s+(сохраняется|остается|осталась)\b",
    r"\bопасность\s+(сохраняется|остается|осталась)\b",
    r"\bобстановка\s+(спокойная|стабильная)\b",
    r"\bбудьте\s+внимательны\b",
    r"\bне\s+расслабляемся\b",
    r"\bсбор[а-я]*\s+средств\b",
    r"\bпожертвован",
    r"\bреклам",
    r"\bрозыгрыш",
    r"\bдоброй\s+ноч",
]

ALERT_PATTERNS = [
    r"\bопасност[ьи].{0,40}\bбпла\b",
    r"\bатак[аи]\b.{0,40}\bбпла\b",
    r"\bбпла\b.{0,40}\bопасност[ьи]\b",
    r"\bобнаружен\w*\b.{0,50}\bбпла\b",
    r"\bзафиксирован\w*\b.{0,50}\bбпла\b",
    r"\bбпла\b.{0,50}\bлетит\b",
    r"\bбеспилотник\w*\b.{0,50}\bлетит\b",
    r"\bдрон\w*\b.{0,50}\bлетит\b",
    r"\bпво\b.{0,50}\bбпла\b",
    r"\bракетн\w*\s+опасност",
    r"\bракеты?\b.{0,40}\bопасност",
]

CLEAR_PATTERNS = [
    r"\bотбой\b",
    r"\bопасност[ьи]\s+(отменена|снята)\b",
    r"\bугроза\s+(отменена|снята)\b",
    r"\bракетн\w*\s+опасност\w*\s+(отменена|снята)\b",
    r"\bатаки?\s+бпла\s+(отменена|снята)\b",
]

REGION_PATTERNS = {
    "BELGOROD": [
        r"\bбелгород",
        r"\bбелгородск",
        r"\bшебекино\b",
        r"\bвалуйк",
        r"\bракитянск",
        r"\bновооскольск",
        r"\bгубкин",
        r"\bстарый\s+оскол\b",
        r"\bкорочанск",
        r"\bяковлевск",
        r"\bивнянск",
        r"\bпрохо?ровск",
    ],
    "KURSK": [
        r"\bкурск",
        r"\bкурск(ая|ой|ую)\b",
        r"\bкурчатов",
        r"\bжелезногорск\b",
        r"\bсуджанск",
        r"\bрыльск",
        r"\bобоянь\b",
        r"\bщигры\b",
        r"\bфатеж\b",
        r"\bльгов\b",
    ],
    "MSK": [
        r"\bмоскв",
        r"\bмосковск",
        r"\bподмосков",
        r"\bмо\b",
        r"\bкрасногорск",
        r"\bодинцов",
        r"\bхимк",
        r"\bдолгопруд",
        r"\bмытищ",
        r"\bбалаших",
        r"\bлюберец",
        r"\bщелков",
        r"\bклин\b",
        r"\bзеленоград\b",
        r"\bистра\b",
        r"\bруза\b",
        r"\bкубинк",
        r"\bнара[- ]?фоминск",
        r"\bчехов\b",
        r"\bподольск",
        r"\bтучков",
        r"\bдмитров",
        r"\bдубна\b",
    ],
    "SPB": [
        r"\bсанкт[- ]?петербург",
        r"\bспб\b",
        r"\bпитер\b",
        r"\bленинградск",
        r"\bленобл",
        r"\bвсеволожск",
        r"\bвыборг",
        r"\bколпино",
        r"\bтосно",
        r"\bгатчина",
        r"\bкирiш",
        r"\bкириш",
        r"\bприозерск",
        r"\bлуга\b",
        r"\bкронштадт",
    ],
}


def matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def detect_region_mentions(text: str, allowed: set[str]) -> list[str]:
    value = normalise(text)
    found = []

    for region in ["BELGOROD", "KURSK", "MSK", "SPB"]:
        if region not in allowed:
            continue
        if matches_any(value, REGION_PATTERNS[region]):
            found.append(region)

    return found


def contains_concrete_alert(text: str) -> bool:
    value = normalise(text)
    if matches_any(value, [p for p in GENERIC_NOISE_PATTERNS]):
        # A direct concrete alert still wins over generic wording.
        return matches_any(value, ALERT_PATTERNS)
    return matches_any(value, ALERT_PATTERNS)


def looks_like_clear(text: str) -> bool:
    return matches_any(normalise(text), CLEAR_PATTERNS)


def strip_source_artifacts(text: str) -> str:
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        low = clean.lower()

        if not clean:
            continue

        if "@locatorru" in low or "@vrv_radar" in low or "@radar_ru_belgorod" in low:
            continue

        if "локатор россии" in low:
            continue

        if "источник:" in low:
            continue

        lines.append(clean)

    result = "\n".join(lines).strip()

    # Never allow the model to create our button itself.
    result = re.sub(
        r"(📡\s*)?радар\b.*подписаться",
        "",
        result,
        flags=re.I,
    )

    return re.sub(r"\n{3,}", "\n\n", result).strip()


def ensure_status_prefix(text: str, severity: str) -> str:
    clean = strip_source_artifacts(text)
    clean = clean.lstrip("🔴🟡🟢⚠️ ")
    emoji = "🔴" if severity == "RED" else "🟡"
    return f"{emoji} {clean}" if clean else ""


async def ask_gemini(text: str, source: str, allowed: set[str]) -> dict[str, Any] | None:
    if gemini_client is None:
        logger.warning("Gemini key is missing; using fallback parser.")
        return None

    allowed_text = ", ".join(sorted(allowed))
    prompt = f"""
Ты — строгий фильтр оперативного радара. Твоя задача — классифицировать только реальные
сообщения об угрозе/атаке БПЛА или ракетной опасности и сообщения об их отбое.

Источник: {source}
Разрешённые регионы для этого источника: {allowed_text}

Текст:
{text}

ВАЖНЫЕ ПРАВИЛА:
1) Не придумывай факты и не добавляй район/объект, которого нет в тексте.
2) Сообщения типа "угроза сохраняется", "опасность сохраняется", общие предупреждения,
   реклама, сбор денег, пожелания, посты без конкретного события -> IGNORE или UNCERTAIN.
3) Явная информация "опасность атаки БПЛА", "БПЛА обнаружен/зафиксирован/летит",
   "ПВО работает по БПЛА", "ракетная опасность" -> ALERT.
4) Явный "отбой", снятие/отмена опасности -> CLEAR.
5) Учитывай только разрешённые регионы. Никогда не отправляй регион,
   которого нет среди разрешённых.
6) Если в одном сообщении есть несколько разрешённых регионов, создай отдельный item
   для каждого региона.
7) Текст каждого item должен быть коротким, нейтральным и готовым для публикации.
   Без источника, без ссылок, без призывов, без хэштегов.
8) Для ALERT:
   - RED — подтверждённая/явная опасность, фиксация, обнаружение, атака, полёт;
   - YELLOW — менее определённая, но всё ещё конкретная информация об опасности.
9) Не делай выводов о реальном происхождении или цели объекта.

Верни только JSON по заданной схеме.
"""

    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_format={
                            "text": {
                                "mime_type": "application/json",
                                "schema": GEMINI_SCHEMA,
                            }
                        },
                        temperature=0.1,
                        max_output_tokens=700,
                    ),
                ),
                timeout=GEMINI_TIMEOUT,
            )

            raw = (response.text or "").strip()
            if not raw:
                raise ValueError("Gemini returned empty response")

            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Gemini returned non-object JSON")

            return data

        except Exception as exc:
            if attempt >= GEMINI_RETRIES:
                error_logger.exception(
                    "GEMINI ERROR after %d attempts: %s",
                    attempt,
                    exc,
                )
                return None

            await asyncio.sleep(0.8 * attempt)

    return None


def validate_ai_result(
    result: dict[str, Any],
    allowed: set[str],
) -> tuple[str, list[dict[str, str]]]:
    decision = str(result.get("decision", "UNCERTAIN")).upper().strip()

    if decision not in {"ALERT", "CLEAR", "IGNORE", "UNCERTAIN"}:
        return "UNCERTAIN", []

    if decision in {"IGNORE", "UNCERTAIN"}:
        return decision, []

    items = result.get("items")
    if not isinstance(items, list):
        return "UNCERTAIN", []

    valid_items: list[dict[str, str]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        region = str(item.get("region", "")).upper().strip()
        severity = str(item.get("severity", "RED")).upper().strip()
        body = str(item.get("text", "")).strip()

        if region not in allowed:
            continue

        if severity not in {"RED", "YELLOW"}:
            severity = "RED"

        body = strip_source_artifacts(body)
        if not body:
            continue

        # Prevent accidental model hallucination of a region not named in text
        # when the source is multi-region.
        mentioned = detect_region_mentions(body, allowed)
        if len(allowed) > 1 and len(mentioned) > 0 and region not in mentioned:
            continue

        if decision == "ALERT":
            body = ensure_status_prefix(body, severity)
        else:
            body = re.sub(r"^[🔴🟡🟢⚠️]+\s*", "", body)

        valid_items.append(
            {
                "region": region,
                "severity": severity,
                "text": body,
            }
        )

    if decision in {"ALERT", "CLEAR"} and not valid_items:
        return "UNCERTAIN", []

    return decision, valid_items


def fallback_parse(
    text: str,
    source: str,
    allowed: set[str],
) -> tuple[str, list[dict[str, str]]]:
    value = normalise(text)

    # Generic "Отбой" is handled separately, because only a reply to a known
    # source alert safely identifies the target.
    if is_plain_clear(value):
        return "CLEAR_REPLY_ONLY", []

    if looks_like_clear(value):
        mentioned = detect_region_mentions(value, allowed)

        # If source has only one responsibility, it is safe to attribute a
        # clearly stated clearance to that region.
        if not mentioned and len(allowed) == 1:
            mentioned = list(allowed)

        if not mentioned:
            return "UNCERTAIN", []

        result = []
        for region in mentioned:
            event_word = "ракетной опасности" if "ракет" in value else "опасности атаки БПЛА"
            result.append(
                {
                    "region": region,
                    "severity": "RED",
                    "text": f"🟢 {REGION_DISPLAY[region]} — отбой {event_word}.",
                }
            )
        return "CLEAR", result

    if not contains_concrete_alert(value):
        return "IGNORE", []

    mentioned = detect_region_mentions(value, allowed)

    if not mentioned and len(allowed) == 1:
        mentioned = list(allowed)

    if not mentioned:
        return "UNCERTAIN", []

    result = []
    for region in mentioned:
        event_word = "ракетная опасность" if "ракет" in value else "опасность атаки БПЛА"
        result.append(
            {
                "region": region,
                "severity": "RED",
                "text": f"🔴 {REGION_DISPLAY[region]} — {event_word}.",
            }
        )

    return "ALERT", result


# ============================================================
# TELEGRAM BOT API
# ============================================================

http_session: aiohttp.ClientSession | None = None


def channel_url(region: str) -> str:
    return f"https://t.me/{BOTS[region]['channel'].lstrip('@')}"


def build_reply_markup(region: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": BOTS[region]["button"],
                    "url": channel_url(region),
                }
            ]
        ]
    }


async def telegram_send_message(
    region: str,
    text: str,
    reply_to_message_id: int | None = None,
) -> int | None:
    global http_session

    bot = BOTS[region]
    token = bot["token"]

    if not token:
        error_logger.error("BOT TOKEN MISSING for %s", region)
        return None

    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=15)
        http_session = aiohttp.ClientSession(timeout=timeout)

    payload: dict[str, Any] = {
        "chat_id": bot["channel"],
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": build_reply_markup(region),
    }

    if reply_to_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": int(reply_to_message_id),
            "allow_sending_without_reply": False,
        }

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(1, 4):
        try:
            async with http_session.post(url, json=payload) as response:
                data = await response.json(content_type=None)

            if data.get("ok"):
                return int(data["result"]["message_id"])

            error_code = data.get("error_code")
            description = data.get("description", "unknown Telegram API error")

            if error_code == 429:
                retry_after = int(data.get("parameters", {}).get("retry_after", 1))
                logger.warning(
                    "Telegram rate limit for %s; retrying after %ss",
                    region,
                    retry_after,
                )
                await asyncio.sleep(min(retry_after, 10))
                continue

            raise RuntimeError(f"Telegram API {error_code}: {description}")

        except Exception as exc:
            if attempt >= 3:
                error_logger.exception(
                    "SEND ERROR region=%s reply_to=%s: %s",
                    region,
                    reply_to_message_id,
                    exc,
                )
                return None

            await asyncio.sleep(0.7 * attempt)

    return None


# ============================================================
# PUBLISH / MEMORY
# ============================================================

async def remember_publication(
    source_chat_id: int,
    source_message_id: int,
    region: str,
    target_message_id: int,
    kind: str,
) -> None:
    async with state_lock:
        s_key = source_key(source_chat_id, source_message_id)
        p_key = publication_key(source_chat_id, source_message_id, region)

        state["published"][p_key] = {
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "region": region,
            "target_message_id": target_message_id,
            "kind": kind,
            "created_at": time.time(),
        }

        state["source_links"].setdefault(
            s_key,
            {
                "created_at": time.time(),
                "targets": [],
            },
        )

        targets = state["source_links"][s_key].setdefault("targets", [])
        targets.append(
            {
                "region": region,
                "target_message_id": target_message_id,
                "kind": kind,
            }
        )

        if kind == "ALERT":
            state["active"][region] = {
                "source_key": s_key,
                "source_message_id": source_message_id,
                "target_message_id": target_message_id,
                "updated_at": time.time(),
            }

        save_state_unlocked()


async def get_source_targets(source_chat_id: int, source_message_id: int) -> list[dict[str, Any]]:
    async with state_lock:
        entry = state["source_links"].get(source_key(source_chat_id, source_message_id))
        if not entry:
            return []
        return list(entry.get("targets", []))


async def is_already_published(
    source_chat_id: int,
    source_message_id: int,
    region: str,
) -> bool:
    async with state_lock:
        return publication_key(source_chat_id, source_message_id, region) in state["published"]


async def clear_active_for_target(
    region: str,
    source_chat_id: int,
    source_message_id: int,
    target_message_id: int,
) -> None:
    async with state_lock:
        current = state["active"].get(region)
        if not current:
            return

        if (
            current.get("source_message_id") == source_message_id
            and current.get("target_message_id") == target_message_id
        ):
            state["active"].pop(region, None)
            save_state_unlocked()


async def latest_active(region: str) -> dict[str, Any] | None:
    async with state_lock:
        current = state["active"].get(region)
        return dict(current) if current else None


# ============================================================
# SOURCE / PROCESSING
# ============================================================

def get_source_name(message: Message) -> str:
    username = getattr(message.chat, "username", None)
    if username:
        return f"@{username.lstrip('@')}"
    title = getattr(message.chat, "title", None)
    return title or str(message.chat.id)


def get_allowed_regions(source: str) -> set[str]:
    return set(SOURCE_REGIONS.get(source.lower(), set()))


def source_is_known(source: str) -> bool:
    return source.lower() in SOURCE_REGIONS


def message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def safe_clear_reply_text() -> str:
    # User explicitly wanted the simple source reply to become simply "Отбой."
    return "Отбой."


async def handle_plain_clear_reply(message: Message, source: str) -> None:
    if not message.reply_to_message_id:
        logger.info("CLEAR REPLY WITHOUT TARGET -> ignored | source=%s", source)
        return

    targets = await get_source_targets(message.chat.id, message.reply_to_message_id)

    if not targets:
        logger.warning(
            "CLEAR REPLY TARGET NOT FOUND in state | source=%s source_reply_to=%s",
            source,
            message.reply_to_message_id,
        )
        return

    text = safe_clear_reply_text()

    # One source alert can have been split into multiple region posts.
    # Reply to each corresponding target.
    for target in targets:
        region = target.get("region")
        target_message_id = target.get("target_message_id")

        if region not in BOTS or not isinstance(target_message_id, int):
            continue

        sent_id = await telegram_send_message(
            region=region,
            text=text,
            reply_to_message_id=target_message_id,
        )

        if sent_id is not None:
            await clear_active_for_target(
                region,
                message.chat.id,
                message.reply_to_message_id,
                target_message_id,
            )
            await remember_publication(
                message.chat.id,
                message.id,
                region,
                sent_id,
                "CLEAR_REPLY",
            )
            logger.info(
                "CLEAR REPLY SENT | source=%s region=%s target=%s new=%s",
                source,
                region,
                target_message_id,
                sent_id,
            )


async def publish_items(
    message: Message,
    source: str,
    items: list[dict[str, str]],
    kind: str,
    as_reply_to: dict[str, int | None] | None = None,
) -> None:
    for item in items:
        region = item["region"]
        text = item["text"]

        if region not in BOTS:
            continue

        if not source_is_known(source):
            continue

        expected_source = REGION_SOURCE.get(region)
        if expected_source != source.lower():
            logger.warning(
                "ROUTING BLOCKED | source=%s region=%s expected=%s",
                source,
                region,
                expected_source,
            )
            continue

        if await is_already_published(message.chat.id, message.id, region):
            logger.info(
                "IDEMPOTENCY SKIP | source=%s msg=%s region=%s",
                source,
                message.id,
                region,
            )
            continue

        reply_to = None
        if as_reply_to and as_reply_to.get("region") == region:
            reply_to = as_reply_to.get("message_id")

        sent_id = await telegram_send_message(
            region=region,
            text=text,
            reply_to_message_id=reply_to,
        )

        if sent_id is None:
            continue

        await remember_publication(
            message.chat.id,
            message.id,
            region,
            sent_id,
            kind,
        )

        logger.info(
            "PUBLISHED | source=%s src_msg=%s region=%s kind=%s target_msg=%s",
            source,
            message.id,
            region,
            kind,
            sent_id,
        )


async def process_message(message: Message) -> None:
    try:
        source = get_source_name(message)
        if not source_is_known(source):
            return

        allowed = get_allowed_regions(source)
        text = message_text(message)

        if not text:
            return

        logger.info(
            "RECEIVED | source=%s msg=%s reply_to=%s text=%s",
            source,
            message.id,
            message.reply_to_message_id,
            text[:300].replace("\n", " "),
        )

        # 1) Plain "Отбой" as a reply: resolve exact source alert -> exact target.
        if is_plain_clear(text):
            await handle_plain_clear_reply(message, source)
            return

        # 2) Fast ignore for known non-events.
        if matches_any(normalise(text), GENERIC_NOISE_PATTERNS) and not contains_concrete_alert(text):
            logger.info("NOISE IGNORED | source=%s msg=%s", source, message.id)
            return

        # 3) Explicit clear with region: publish standalone new message.
        #    No reply is used here, exactly as requested.
        fast_fallback_decision, fast_fallback_items = fallback_parse(text, source, allowed)

        if fast_fallback_decision == "CLEAR" and fast_fallback_items:
            await publish_items(
                message,
                source,
                fast_fallback_items,
                kind="CLEAR",
                as_reply_to=None,
            )
            return

        # 4) Obvious concrete alert can be routed without Gemini when the source
        #    has only one region. This is the emergency fallback path.
        if fast_fallback_decision == "ALERT" and len(allowed) == 1:
            # Still ask Gemini when available, because it gives better wording.
            # If Gemini fails, this path becomes the publication fallback.
            if gemini_client is None:
                await publish_items(
                    message,
                    source,
                    fast_fallback_items,
                    kind="ALERT",
                )
                return

        # 5) Gemini analysis.
        ai_result = await ask_gemini(text, source, allowed)
        if ai_result is None:
            # Gemini failure -> conservative deterministic fallback.
            fallback_decision, fallback_items = fallback_parse(text, source, allowed)

            if fallback_decision == "ALERT":
                logger.warning(
                    "GEMINI DOWN -> FALLBACK ALERT | source=%s msg=%s",
                    source,
                    message.id,
                )
                await publish_items(
                    message,
                    source,
                    fallback_items,
                    kind="ALERT",
                )
            elif fallback_decision == "CLEAR" and fallback_items:
                logger.warning(
                    "GEMINI DOWN -> FALLBACK CLEAR | source=%s msg=%s",
                    source,
                    message.id,
                )
                await publish_items(
                    message,
                    source,
                    fallback_items,
                    kind="CLEAR",
                )
            else:
                logger.warning(
                    "GEMINI DOWN -> FALLBACK IGNORE/UNCERTAIN | source=%s msg=%s",
                    source,
                    message.id,
                )
            return

        decision, items = validate_ai_result(ai_result, allowed)

        if decision == "IGNORE":
            logger.info("AI IGNORE | source=%s msg=%s", source, message.id)
            return

        if decision == "UNCERTAIN":
            logger.warning(
                "AI UNCERTAIN -> ignored | source=%s msg=%s",
                source,
                message.id,
            )
            return

        if decision == "ALERT":
            await publish_items(
                message,
                source,
                items,
                kind="ALERT",
            )
            return

        if decision == "CLEAR":
            await publish_items(
                message,
                source,
                items,
                kind="CLEAR",
            )
            return

    except Exception as exc:
        error_logger.exception(
            "PROCESS MESSAGE ERROR source=%s msg=%s: %s",
            get_source_name(message),
            message.id,
            exc,
        )


# ============================================================
# QUEUE
# ============================================================

queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=QUEUE_LIMIT)
sequence = 0
sequence_lock = asyncio.Lock()


async def enqueue_message(message: Message, priority: int) -> None:
    global sequence

    async with sequence_lock:
        sequence += 1
        seq = sequence

    try:
        queue.put_nowait((priority, seq, message))
    except asyncio.QueueFull:
        error_logger.error(
            "QUEUE FULL -> message dropped | source=%s msg=%s",
            get_source_name(message),
            message.id,
        )


async def worker(worker_id: int) -> None:
    logger.info("WORKER %s started", worker_id)

    while True:
        priority, seq, message = await queue.get()
        try:
            await process_message(message)
        except Exception as exc:
            error_logger.exception(
                "WORKER %s ERROR: %s",
                worker_id,
                exc,
            )
        finally:
            queue.task_done()


# ============================================================
# PYROGRAM
# ============================================================

load_state()

app = Client(
    "radar_bot",
    session_string=SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH,
)

SOURCES = list(SOURCE_REGIONS.keys())


@app.on_message(filters.chat(SOURCES))
async def radar_handler(client: Client, message: Message) -> None:
    text = message_text(message)

    if not text:
        return

    # Priority 0: direct "Отбой" replies.
    priority = 0 if is_plain_clear(text) else 10
    await enqueue_message(message, priority)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def validate_configuration() -> None:
    required = {
        "SESSION_STRING": SESSION_STRING,
        "GEMINI_API_KEY": GEMINI_API_KEY,
    }

    for name, value in required.items():
        if not value:
            error_logger.error("MISSING ENV: %s", name)

    for region, info in BOTS.items():
        if not info["token"]:
            error_logger.error("MISSING ENV: BOT_%s", region)

    logger.info("SOURCE ROUTING:")
    for source, regions in SOURCE_REGIONS.items():
        logger.info("  %s -> %s", source, ", ".join(sorted(regions)))

    logger.info("GEMINI MODEL: %s", GEMINI_MODEL)
    logger.info("WORKERS: %s | TIMEOUT: %ss", WORKERS, GEMINI_TIMEOUT)


async def warm_up_telegram() -> None:
    logger.info("Synchronizing Telegram dialogs...")
    try:
        count = 0
        async for _dialog in app.get_dialogs():
            count += 1
        logger.info("Dialogs synchronized: %s", count)
    except Exception as exc:
        error_logger.exception("DIALOG SYNC ERROR: %s", exc)

    # Verify that all three sources are reachable by the Pyrogram account.
    for source in SOURCES:
        try:
            chat = await app.get_chat(source)
            logger.info(
                "SOURCE OK: %s | title=%s",
                source,
                getattr(chat, "title", None),
            )
        except Exception as exc:
            error_logger.exception("SOURCE CHECK FAILED: %s: %s", source, exc)


async def shutdown_http() -> None:
    global http_session

    if http_session is not None and not http_session.closed:
        await http_session.close()
        http_session = None


async def main() -> None:
    print("==============================================")
    print("[*] AI RADAR DISPATCHER v2 STARTING")
    print("==============================================")

    await validate_configuration()

    if not SESSION_STRING:
        raise RuntimeError("SESSION_STRING is missing")

    await app.start()
    logger.info("Pyrogram started.")

    await warm_up_telegram()

    workers = [
        asyncio.create_task(worker(i + 1))
        for i in range(WORKERS)
    ]

    try:
        logger.info("RADAR SYSTEM IS RUNNING.")
        await idle()
    finally:
        for task in workers:
            task.cancel()

        await asyncio.gather(*workers, return_exceptions=True)

        await shutdown_http()

        try:
            await app.stop()
        except Exception as exc:
            error_logger.exception("PYROGRAM STOP ERROR: %s", exc)

        logger.info("RADAR SYSTEM STOPPED.")


if __name__ == "__main__":
    asyncio.run(main())
