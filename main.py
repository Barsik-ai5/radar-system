import os
import copy
import asyncio
import requests
import time
import re
import json
import threading
from datetime import datetime, timezone
from html import escape
from pyrogram import Client, idle

from google import genai
from google.genai import types
from google.genai.errors import APIError
from dotenv import load_dotenv

load_dotenv()

# === 1. ЗАЩИТА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ОТ НЕВИДИМЫХ СИМВОЛОВ (\u200b) ===
CLEAN_ENV = {
    k.replace("\u200b", "").strip(): (v.replace("\u200b", "").strip() if v else "")
    for k, v in os.environ.items()
}

def get_env(key: str, default: str = "") -> str:
    return CLEAN_ENV.get(key, default)

# === 2. ПРОФЕССИОНАЛЬНОЕ ЛОГИРОВАНИЕ ===
STATS = {"processed": 0, "sent": 0, "errors": 0}
stats_lock = threading.Lock()

def log_msg(level, tag, msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} [{level}] [{tag}] {msg}"
    print(line)
    if level == "ERROR":
        with stats_lock:
            STATS["errors"] += 1
        _notify_admin_async(line)

# === 3. ПРОВЕРКА КЛЮЧЕЙ И НАСТРОЙКА ИИ ===
API_ID_RAW = get_env("API_ID")
API_HASH = get_env("API_HASH")
SESSION_STRING = get_env("SESSION_STRING")

if not API_ID_RAW or not API_HASH or not SESSION_STRING:
    raise RuntimeError("[-] ОШИБКА СТАРТА: Не заданы ключи Телеграма (API_ID, API_HASH, SESSION_STRING)!")

API_ID = int(API_ID_RAW)
GEMINI_API_KEY = get_env("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("[-] ОШИБКА СТАРТА: Не задан GEMINI_API_KEY!")

HEARTBEAT_INTERVAL_SEC = int(get_env("HEARTBEAT_INTERVAL_HOURS", "6") or "6") * 3600
ALERT_TTL_SEC = 43200  # Единый срок жизни активной тревоги для реплая (12 часов)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
AI_CONFIG = types.GenerateContentConfig(
    safety_settings=[
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ],
    max_output_tokens=2048,
    temperature=0.1
)

# === 4. КОНФИГУРАЦИЯ КАНАЛОВ И МАРШРУТИЗАЦИИ ===
BOTS_CONFIG = {
    "SPB": {
        "token": get_env("BOT_SPB"),
        "channel": "@RadarLO_SPB",
        "name": "Питер и Ленинградская область",
        "short_region": "Ленинградская область",
        "prompt_desc": "Санкт-Петербург и Ленинградская область -> [SPB]"
    },
    "MSK": {
        "token": get_env("BOT_MSK"),
        "channel": "@Radar_MSK_OBL",
        "name": "Москва и Московская область",
        "short_region": "Московская область",
        "prompt_desc": "Москва и Московская область -> [MSK]"
    },
    "BELGOROD": {
        "token": get_env("BOT_BELGOROD"),
        "channel": "@Radar_Belgorod_Obl",
        "name": "Белгород и Белгородская область",
        "short_region": "Белгородская область",
        "prompt_desc": "Белгород и Белгородская область -> [BELGOROD]"
    },
    "KURSK": {
        "token": get_env("BOT_KURSK"),
        "channel": "@Radar_Kursk",
        "name": "Курск и Курская область",
        "short_region": "Курская область",
        "prompt_desc": "Курск и Курская область -> [KURSK]"
    }
}

missing_bot_tokens = [reg for reg, cfg in BOTS_CONFIG.items() if not cfg.get("token")]
if missing_bot_tokens:
    raise RuntimeError(f"[-] ОШИБКА СТАРТА: Не заданы токены ботов для регионов: {', '.join(missing_bot_tokens)}!")

SOURCE_ROUTING = {
    "locatorru": ["KURSK"],
    "vrv_radar": ["SPB", "MSK"],
    "radar_ru_belgorod": ["BELGOROD"]
}

# Каналы, посвященные строго одному региону (где допустимы короткие посты "Опасность по БПЛА" без названия области).
# Общероссийский "locatorru" сюда КАТЕГОРИЧЕСКИ НЕ входит!
MONO_REGION_SOURCES = {
    "radar_ru_belgorod": "BELGOROD"
}

OWN_REGION_PREFIX_MAP = {
    "KURSK": "курск",
    "BELGOROD": "белгород",
    "MSK": "москов",
    "SPB": "ленинград"
}

ADMIN_CHAT_ID = get_env("ADMIN_CHAT_ID")
ADMIN_BOT_TOKEN = get_env("ADMIN_BOT_TOKEN") or next((v["token"] for v in BOTS_CONFIG.values() if v["token"]), None)

MAIN_LOOP = None
ADMIN_QUEUE = None
SOURCE_LOCKS = {}  # Поканальные блокировки для сохранения строгой хронологии постов

def _notify_admin_async(line):
    if not ADMIN_CHAT_ID or MAIN_LOOP is None:
        return
    try:
        MAIN_LOOP.call_soon_threadsafe(_enqueue_admin_line, line)
    except RuntimeError:
        pass

def _enqueue_admin_line(line):
    if ADMIN_QUEUE is None:
        return
    try:
        ADMIN_QUEUE.put_nowait(line)
    except asyncio.QueueFull:
        pass

def sync_send_admin_message(text):
    if not ADMIN_CHAT_ID or not ADMIN_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for _ in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            elif resp.status_code == 429:
                retry_sec = int(resp.json().get("parameters", {}).get("retry_after", 3)) + 1
                time.sleep(min(retry_sec, 60))
                continue
            return None
        except Exception:
            return None
    return None

async def admin_notifier_worker():
    if not ADMIN_CHAT_ID:
        return
    while True:
        try:
            first_line = await ADMIN_QUEUE.get()
            ADMIN_QUEUE.task_done()
            batch = [first_line]
            deadline = time.monotonic() + 5.0
            while len(batch) < 20:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(ADMIN_QUEUE.get(), timeout=remaining)
                    ADMIN_QUEUE.task_done()
                    batch.append(item)
                except asyncio.TimeoutError:
                    break
            raw_text = "\n\n".join(batch)
            text = f"🚨 <b>Радар: {len(batch)} ошиб(ок)</b>\n\n<pre>{escape(raw_text[:600])}</pre>"
            await asyncio.get_running_loop().run_in_executor(None, sync_send_admin_message, text)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[ERROR] [ADMIN] Сбой воркера уведомлений: {e}")

async def heartbeat_worker():
    global _pending_snapshot
    if not ADMIN_CHAT_ID:
        return
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        try:
            with stats_lock:
                proc, sent, errs = STATS["processed"], STATS["sent"], STATS["errors"]
                STATS["processed"] = STATS["sent"] = STATS["errors"] = 0
            now_hb = time.time()
            expired_cleaned = False
            async with state_lock:
                active_count = 0
                for r_val in ACTIVE_ALERTS.values():
                    if not isinstance(r_val, dict):
                        continue
                    for k in ("uav", "missile"):
                        entry = r_val.get(k)
                        if isinstance(entry, dict) and isinstance(entry.get("msg_id"), int) and entry.get("msg_id", 0) > 0:
                            if now_hb - entry.get("ts", 0) < ALERT_TTL_SEC:
                                active_count += 1
                            else:
                                r_val[k] = None
                                expired_cleaned = True
                if expired_cleaned:
                    _pending_snapshot = copy.deepcopy(ACTIVE_ALERTS)
                    _state_dirty.set()
            text = (
                f"✅ <b>Радар жив (V18.7).</b>\n"
                f"Обработано: {proc}\nОтправлено: {sent}\n"
                f"Ошибок: {errs}\nАктивных тревог: {active_count}"
            )
            await asyncio.get_running_loop().run_in_executor(None, sync_send_admin_message, text)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_msg("ERROR", "ADMIN", f"Сбой heartbeat: {type(e).__name__}")

# === 5. ХРАНЕНИЕ СОСТОЯНИЯ ТРЕВОГ (РАЗДЕЛЬНО ДЛЯ БПЛА И РАКЕТ) ===
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts_state.json")
state_lock = None
file_write_lock = threading.Lock()

_pending_snapshot = None
_state_dirty = None

def _sanitize_slot_entry(entry):
    """Проверяет целостность структуры слота при загрузке из JSON."""
    if not isinstance(entry, dict):
        return None
    msg_id = entry.get("msg_id")
    ts = entry.get("ts", 0)
    if not isinstance(msg_id, int) or isinstance(msg_id, bool) or msg_id <= 0:
        return None
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        ts = 0
    is_gen = bool(entry.get("is_general", False))
    src_id = entry.get("src_msg_id", 0)
    if not isinstance(src_id, int) or isinstance(src_id, bool):
        src_id = 0
    return {"msg_id": msg_id, "ts": float(ts), "is_general": is_gen, "src_msg_id": src_id}

def _load_alerts_sync():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    upgraded = {}
                    for k, v in data.items():
                        if k not in BOTS_CONFIG:
                            continue
                        if isinstance(v, dict) and ("uav" in v or "missile" in v):
                            upgraded[k] = {
                                "uav": _sanitize_slot_entry(v.get("uav")),
                                "missile": _sanitize_slot_entry(v.get("missile"))
                            }
                        elif isinstance(v, dict) and isinstance(v.get("msg_id"), int) and v["msg_id"] > 0:
                            upgraded[k] = {
                                "uav": _sanitize_slot_entry({
                                    "msg_id": v["msg_id"],
                                    "ts": v.get("ts", 0),
                                    "is_general": True,
                                    "src_msg_id": 0
                                }),
                                "missile": None
                            }
                        elif isinstance(v, int) and not isinstance(v, bool) and v > 0:
                            upgraded[k] = {
                                "uav": {"msg_id": v, "ts": 0.0, "is_general": True, "src_msg_id": 0},
                                "missile": None
                            }
                        else:
                            print(f"[WARN] [STATE] Пропущена устаревшая запись состояния для {k}: {v}")
                    return upgraded
        except Exception as e:
            print(f"[WARN] [STATE] Не удалось прочитать alerts_state.json ({e}). Старт с чистого состояния.")
    return {}

def _save_alerts_sync(data) -> bool:
    with file_write_lock:
        try:
            tmp_file = STATE_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_file, STATE_FILE)
            return True
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Ошибка записи JSON: {e}")
            return False

async def state_saver_worker():
    global _pending_snapshot
    while True:
        try:
            await _state_dirty.wait()
            _state_dirty.clear()
            snap = _pending_snapshot
            if snap is None:
                continue
            ok = await asyncio.get_running_loop().run_in_executor(None, _save_alerts_sync, snap)
            if not ok:
                await asyncio.sleep(2.0)
                _state_dirty.set()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Ошибка state_saver: {e}")

PROCESSED_MESSAGES = {}
SENT_REGIONS = {}  # Порегиональная защита от дублей при частичном сбое мульти-отправки

# === 6. СЛОВАРИ ГЕОГРАФИИ И ФИЛЬТРАЦИИ ===
REGION_KEYWORDS = {
    "BELGOROD": [
        "белгород", "белгородск", "шебекин", "валуй", "грайворон", "оскол", "губкин",
        "волоконов", "борисов", "ивнянск", "ракитн", "ракитянск", "краснояруж",
        "красненск", "алексеев", "короч", "вейделев", "ровен", "чернян", "прохоров",
        "строител", "октябрьск", "томаров", "тавров", "дубов", "майск", "разумн",
        "беломестн", "стрелецк", "головчин", "бессонов", "маслов", "уразов", "бирюч",
        "вознесеновк", "таволжанк", "нежегол", "яковлевск"
    ],
    "KURSK": [
        "курск", "курчат", "судж", "рыльск", "обоян", "льгов", "коренев", "глушков",
        "беловск", "железногорск", "фатеж", "щигр", "дмитриев", "тимск",
        "горшечн", "поныр", "медвен", "золотухин", "мантуров", "солнцев", "черемисин",
        "касторн", "пристен", "прямицын", "теткин", "тёткин", "халино", "конышев",
        "большесолдат", "хомутов", "крупец"
    ],
    "MSK": [
        "москв", "московск", "moscow", "подмосков", "тинао", "зеленоград",
        "подольск", "люберц", "королев", "химк", "балаших", "мытищ", "красногорск",
        "одинцов", "домодедов", "раменск", "ступин", "кашир", "коломн", "чехов",
        "серпухов", "истринск", "можайск", "наро-фоминск", "нарофоминск", "воскресенск",
        "щелков", "щёлков", "пушкин", "сергиев", "дмитров", "клинск", "солнечногорск",
        "ногинск", "электростал", "орехово-зуев", "павловский посад", "егорьевск",
        "зарайск", "луховиц", "протвин", "пущин", "рузск", "шатур", "талдом", "дубненск",
        "фрязин", "жуковск", "бронниц", "видное", "котельник", "реутов", "долгопрудн",
        "лобненск", "кубинк", "алабин", "внуков", "шереметьев"
    ],
    "SPB": [
        "петербург", "санкт", "питер", "ленинград", "ленинградск",
        "ленобласт", "лен област", "лен. област", "выборг", "гатчин", "кронштадт",
        "лужск", "кингисепп", "волхов", "тихвин", "всеволожск", "мурин",
        "кудров", "тосн", "кириш", "сосновый бор", "приозерск", "подпорож", "лодейн",
        "бокситогорск", "сланц", "волосов", "ломоносов", "колпин", "сестрорецк",
        "петергоф", "усть-луг", "сертолов", "отрадн", "шлиссельбург"
    ]
}

REGION_EXACT_WORDS = {
    "SPB": ["спб", "с-пб", "луга", "луге", "лугу", "лугой"],
    "MSK": [
        "мск", "мкад", "истра", "истре", "истру", "истрой",
        "клин", "клина", "клину", "клином", "клине",
        "руза", "рузе", "рузу", "рузой",
        "дубна", "дубне", "дубну", "дубной",
        "лобня", "лобне", "лобню", "лобней"
    ],
    "BELGOROD": ["ивня", "ивне", "ивню", "ивней"],
    "KURSK": ["тим", "тима", "тиме", "тимом", "тиму"]
}

FOREIGN_REGION_KEYWORDS = [
    "краснодар", "анап", "темрюк", "таман", "новороссийск", "геленджик", "туапсе",
    "сочи", "адлер", "ейск", "славянск-на-кубани", "черноморск", "крым", "севастопол",
    "керч", "феодоси", "евпатори", "симферопол", "джанко", "саки", "ялт", "ростов",
    "таганрог", "батайск", "миллеров", "морозовск", "каменск-шахтинск", "воронеж", "борисоглебск",
    "лиски", "острогожск", "россош", "брянск", "клинц", "унеч", "карачев", "стародуб",
    "орловск", "орел", "орёл", "мценск", "ливн", "липецк", "елец", "тамбов", "мичуринск",
    "саратов", "энгельс", "волгоград", "астрахан", "самар", "сызран", "тольятт",
    "пенз", "ульянов", "барышск", "перм", "удмурт", "ижевск", "воткинск", "чуваш",
    "чебоксар", "мордов", "саранск", "марий", "йошкар", "киров", "екатеринбург", "свердловск",
    "челябинск", "тюмен", "курган", "нижегород", "дзержинск", "казан", "татарстан", "елабуг",
    "нижнекамск", "башкортостан", "уфа", "уфе", "уфу", "салават", "оренбург", "орск",
    "тульск", "тула", "туле", "тулу", "алексин", "узлов", "калуж", "калуг", "людинов",
    "шайковк", "рязан", "смоленск", "тверск", "тверь", "твери", "торопец", "ярослав",
    "ивановск", "владимирск", "костромск", "псков", "новгородск", "мурманск", "архангельск",
    "карели", "ставропол", "дагестан", "каспийск", "чечн", "грозн", "осети", "моздок",
    "ингушет", "кабардин", "лнр", "луганск", "белокуракин", "алчевск", "днр", "донецк",
    "макеевк", "горловк", "мариупол", "запорож", "мелитопол", "бердянск", "энергодар",
    "херсон", "геническ", "скадовск"
]

TAG_ALIASES_LIST = {
    "SPB": ["SPB", "СПБ", "PITER", "ПИТЕР", "ПЕТЕРБУРГ", "ЛЕНИНГРАДСК"],
    "MSK": ["MSK", "МСК", "MOSCOW", "МОСКВА", "МОСКОВСК", "ПОДМОСКОВ"],
    "BELGOROD": ["BELGOROD", "БЕЛГОРОД", "БЕЛГОРОДСК"],
    "KURSK": ["KURSK", "КУРСК", "КУРСКАЯ"]
}

TAG_ALIASES = {
    k: "|".join(rf"{re.escape(a)}(?=\b|[^а-яёa-z0-9])" for a in v)
    for k, v in TAG_ALIASES_LIST.items()
}
ALL_TAGS_REGEX = "|".join(
    rf"{re.escape(tag)}(?=\b|[^а-яёa-z0-9])"
    for tags in TAG_ALIASES_LIST.values() for tag in tags
)

SIGNATURE_LINE_PATTERNS = [
    re.compile(r"^[^\w]*@\w+[^\w]*$"),
    re.compile(r"^[^\w]*(?:https?://|t\.me/)\S+[^\w]*$", re.IGNORECASE),
    re.compile(
        r"^[^\w]*(?:радар\.ру\s*\|\s*белгород|локатор\s+россии|подписаться|источник)[^\w]*(?:@\w+)?[^\w]*$",
        re.IGNORECASE
    )
]

INLINE_SIGNATURES_RE = re.compile(
    r"(?:🎯|📡|📢)?\s*(?:радар\.ру\s*\|\s*белгород|локатор\s+россии)\s*(?:[-—|]\s*)?(?:@\w+)?|"
    r"@radar_ru_belgorod|@locatorru|@vrv_radar|@vrv_support|"
    r"https?://\S+|t\.me/\S+",
    re.IGNORECASE
)

TRANSIT_PHRASE_RE = re.compile(
    r"(?:в\s+сторону|со\s+стороны|в\s+направлении|курсом\s+на|курс\s+на)\s+"
    r"(?:(?:г\.|города|город|пос[её]лка|села|пгт|деревни|республики|респ\.)\s+)?"
    r"[а-яёa-z\-]+(?:\s+(?:област\w+|обл\.?|кра\w+|республик\w+|респ\.?|район\w+|р-н\w*|округ\w*|ао))?",
    re.IGNORECASE
)

# Универсальный шаблон любого региона РФ со словом область/обл./край/республика/АО
ANY_REGION_UNIT_PAT = (
    r"(?:[А-ЯЁа-яё\-]+(?:\s+[А-ЯЁа-яё\-]+)?\s+(?:област\w+|\bобл\.|\bобл\b|кра\w+|республик\w+|\bресп\.|\bресп\b|автономн\w+\s+округ\w*|\bао\b)|"
    r"(?:республик\w+|\bресп\.|\bресп\b)\s+[А-ЯЁа-яё\-]+(?:\s+[А-ЯЁа-яё\-]+)?|"
    r"\bЛНР\b|\bДНР\b)"
)

IS_IGNORE_RESPONSE_RE = re.compile(
    r"^\s*\[?\s*(?:ИГНОР|IGNORE)\b|"
    r"\b(?:верни\s+только\s+слово\s+игнор|не\s+относится\s+к\s+целев|"
    r"нет\s+информации\s+о\s+целев|информации\s+недостаточно|"
    r"не\s+упоминается\s+целев|целевой\s+регион\s+отсутствует)\b",
    re.IGNORECASE
)

def is_ignore_text(text: str) -> bool:
    if not text or not text.strip():
        return True
    stripped = text.strip()
    if IS_IGNORE_RESPONSE_RE.search(stripped):
        return True
    stripped_no_punct = re.sub(r"[\W_]+", "", stripped).upper()
    return stripped_no_punct in ("ИГНОР", "IGNORE")

def clean_source_text(text: str) -> str:
    """
    Удаляет строки, состоящие только из подписей источников, и мягко вырезает
    встроенные подписи/ссылки из строк с текстом тревоги.
    """
    if not text:
        return ""
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if any(p.match(stripped) for p in SIGNATURE_LINE_PATTERNS):
            continue
        line_clean = INLINE_SIGNATURES_RE.sub("", line).strip()
        line_clean = re.sub(r"[\s\-—|🎯📡]+$", "", line_clean).strip()
        if line_clean:
            cleaned_lines.append(line_clean)
    return "\n".join(cleaned_lines).strip()

def strip_moscow_time(text_low: str) -> str:
    """Убирает любые обозначения московского часового пояса ('10:00 мск', '10:00 (мск)', 'по мск')."""
    text_low = re.sub(
        r"\b\d{1,2}(?:[:.]\d{2})?\s*(?:ч(?:ас(?:а|ов)?)?\.?\s*)?\(?\s*мск\s*\)?(?=\b|[^а-яёa-z0-9])",
        "",
        text_low
    )
    text_low = re.sub(r"\b(?:по|время)\s+\(?\s*мск\s*\)?(?=\b|[^а-яёa-z0-9])", "", text_low)
    text_low = re.sub(r"\(\s*мск\s*\)", "", text_low)
    return text_low

def strip_transit_phrases(text_low: str) -> str:
    """Убирает транзитные направления ('в сторону города Курска'), чтобы они не считались чужим регионом."""
    return TRANSIT_PHRASE_RE.sub("", text_low)

def has_region_keywords(text: str, reg: str) -> bool:
    """Проверяет наличие географии целевого региона в тексте."""
    text_low = strip_moscow_time(text.lower())
    for exact_kw in REGION_EXACT_WORDS.get(reg, []):
        if re.search(rf"\b{re.escape(exact_kw)}\b", text_low, re.IGNORECASE):
            return True
    for kw in REGION_KEYWORDS.get(reg, []):
        if re.search(rf"\b{re.escape(kw)}", text_low, re.IGNORECASE):
            return True
    return False

def has_foreign_keywords(text: str, target_reg: str) -> bool:
    """
    Проверяет, упоминаются ли в тексте чужие регионы как основное место события
    (автоматически ловит любые '<X> область/край/республика/АО' + список городов, игнорируя транзитные направления).
    """
    text_low = strip_transit_phrases(strip_moscow_time(text.lower()))
    own_prefix = OWN_REGION_PREFIX_MAP[target_reg]

    # 1. Любая область, край, республика или АО РФ, не совпадающая с целевым регионом — сразу чужой регион!
    for m in re.finditer(ANY_REGION_UNIT_PAT, text_low, re.IGNORECASE):
        if own_prefix not in m.group(0).lower():
            return True

    # 2. Проверка по словарю чужих городов и регионов
    for kw in FOREIGN_REGION_KEYWORDS:
        if len(kw) <= 4:
            if re.search(rf"\b{re.escape(kw)}\b", text_low, re.IGNORECASE):
                return True
        else:
            if re.search(rf"\b{re.escape(kw)}", text_low, re.IGNORECASE):
                return True
    for other_reg, exact_kws in REGION_EXACT_WORDS.items():
        if other_reg == target_reg:
            continue
        for exact_kw in exact_kws:
            if re.search(rf"\b{re.escape(exact_kw)}\b", text_low, re.IGNORECASE):
                return True
    for other_reg, kws in REGION_KEYWORDS.items():
        if other_reg == target_reg:
            continue
        for kw in kws:
            if re.search(rf"\b{re.escape(kw)}", text_low, re.IGNORECASE):
                return True
    return False

def escape_and_truncate(text: str, max_escaped_len: int = 3800) -> str:
    """
    Обрезает plain-текст строго по границе предложения или слова ДО экранирования HTML,
    гарантируя отсутствие разорванных слов и сохранность всех HTML-энтити (&amp;, &lt;, &gt;).
    """
    safe = escape(text)
    if len(safe) <= max_escaped_len:
        return safe

    limit = min(len(text), max_escaped_len)
    while limit > 100:
        cut = text[:limit]
        last_dot = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("\n"))
        if last_dot > limit * 0.7:
            candidate = cut[:last_dot + 1].strip()
        else:
            last_space = cut.rfind(" ")
            candidate = (cut[:last_space].strip() + "...") if last_space > 0 else (cut.strip() + "...")

        safe_candidate = escape(candidate)
        if len(safe_candidate) <= max_escaped_len:
            return safe_candidate
        limit = min(limit - 100, int(limit * 0.92))

    return escape(text[:100].strip())[:max_escaped_len - 3] + "..."

# === 7. ПОСТ-ОБРАБОТКА, ЗАЩИТА СЁЛ И ДЕТЕРМИНИРОВАННЫЙ СВЕТОФОР ===

RED_COMBAT_RE = re.compile(
    r"\bсби(?:т[аыо]?|л[аио]?|ти[ея])\b|"
    r"\b(?:уничтожен[аыо]?|подавлен[аыо]?|перехвачен[аыо]?)\s+(?:[^\n,.!?;]{0,30}\s+)?(?:бпла|дрон\w*|беспилот\w*|фпв|fpv|цел\w*)|"
    r"работает\s+пво|работа\s+пво|тревога\s+по\s+бпла|опасность\s+атаки\s+бпла|атака\s+бпла|"
    r"фиксация\s+бпла|прол[её]т[ы]?\s+бпла|движение\s+бпла|групп[аы]\s+бпла|волна\s+бпла|"
    r"атака\s+(?:фпв|fpv)|опасность\s+по\s+(?:фпв|fpv)|"
    r"ракетная\s+опасность|авиационная\s+опасность|баллистика|обстрел|осколк|обломк",
    re.IGNORECASE
)

LIVE_URGENT_RE = re.compile(
    r"работает\s+пво|работа\s+пво|тревога\s+по\s+бпла|опасность\s+атаки\s+бпла|"
    r"фиксация\s+бпла|прол[её]т[ы]?\s+бпла|движение\s+бпла|групп[аы]\s+бпла|волна\s+бпла|"
    r"ракетная\s+опасность|авиационная\s+опасность|баллистика|опасность\s+по\s+(?:фпв|fpv)|"
    r"(?:опасност\w*|внимание|угроз\w*)\s+(?:по\s+)?(?:осколк\w*|обломк\w*)|"
    r"в\s+укрыти|не\s+подходите\s+к\s+окнам",
    re.IGNORECASE
)

DIRECTION_RE = re.compile(r"в\s+сторону|курсом\s+на|курс\s+на|в\s+направлении", re.IGNORECASE)
MILITARY_CONTEXT_RE = re.compile(
    r"бпла|дрон|фпв|fpv|беспилот|ракет|авиац|баллист|пво|цел|воздушн|снаряд|обстрел|прол[её]т|фиксац|групп|волна",
    re.IGNORECASE
)

YELLOW_TRIGGER_RE = re.compile(
    r"опасност\w*\s+(?:по\s+)?бпла|внимание\s+по\s+бпла|угроз\w*\s+(?:по\s+)?бпла",
    re.IGNORECASE
)

NEGATED_CLEAR_RE = re.compile(
    r"отбой\s+не\s+объяв|не\s+объявлял\w*\s+отбой|нет\s+отбоя|без\s+отбоя|отбоя\s+не\s+было|отбой\s+ложн",
    re.IGNORECASE
)

POSITIVE_CLEAR_RE = re.compile(
    r"\bотбой\b|(?:опасност\w*|тревог\w*|угроз\w*)\s+(?:(?:по\s+|атаки\s+)?(?:бпла|ракет\w*|авиац\w*)\s+)?(?:отменен[аыо]?|снят[аыо]?)",
    re.IGNORECASE
)

STRIP_CLEAR_CLAUSE_RE = re.compile(
    r"\bотбой\s+(?:(?:ракетной|авиационной)\s+опасности|опасности\s+(?:атаки\s+|по\s+)?(?:бпла|фпв|fpv)|"
    r"тревоги\s+по\s+бпла|опасности|тревоги|угрозы)|"
    r"(?:ракетная\s+опасность|авиационная\s+опасность|опасност\w*\s+(?:атаки\s+|по\s+)?бпла|тревог\w*\s+по\s+бпла|угроз\w*\s+(?:по\s+)?бпла)\s+"
    r"(?:отменен[аыо]?|снят[аыо]?)",
    re.IGNORECASE
)

NEGATED_THREAT_RE = re.compile(
    r"\b(?:нет|без|отсутств\w+|не\s+зафиксирован\w*|не\s+наблюда\w*)\s+"
    r"(?:(?:активн\w+|прям\w+|реальн\w+)\s+)?"
    r"(?:опасност\w*|угроз\w*|тревог\w*)(?:\s+(?:по\s+|атаки\s+|применения\s+)?(?:бпла|дрон\w*|беспилот\w*|ракет\w*))?|"
    r"(?:опасност\w*|угроз\w*|тревог\w*)\s+"
    r"(?:(?:по\s+|атаки\s+|применения\s+)?(?:бпла|дрон\w*|беспилот\w*|ракет\w*)\s+)?"
    r"(?:[^\n,.!?;]{0,20}\s+)?"
    r"(?:нет|не\s+объявлял\w*|отсутств\w+|не\s+зафиксирован\w*)",
    re.IGNORECASE
)

REFUTED_EVENT_RE = re.compile(
    r"не\s+подтвердил\w*|не\s+нашл\w*\s+подтвержден\w*|опроверг\w*|"
    r"оказал\w*\s+(?:ложн\w*|фейк\w*)|информация\s+(?:ложн\w*|не\s+соответствует)|"
    r"\bфейк\w*\b",
    re.IGNORECASE
)

ACTIVE_REGIME_RE = re.compile(
    r"\b(?:действует|сохраняется|объявлен[аыо]?|введен[аыо]?|продлен[аыо]?)\s+(?:режим\s+)?(?:опасност|тревог|угроз)|"
    r"(?:опасност\w*|тревог\w*|угроз\w*)\s+(?:(?:по\s+|атаки\s+)?(?:бпла|ракет\w*)\s+)?(?:действует|сохраняется)",
    re.IGNORECASE
)

RETROSPECTIVE_SUMMARY_RE = re.compile(
    r"за\s+ночь|прошедшей\s+ночи|в\s+течение\s+ночи|ночью\s+силами\s+пво|"
    r"дежурными\s+средствами\s+пво|за\s+сутки|за\s+прошедшие\s+сутки|"
    r"сводка\s+обстрелов|информация\s+о\s+нанесенных|"
    r"\b\d{1,2}:\d{2}\s*[–—-]\s*\d{1,2}:\d{2}\b",
    re.IGNORECASE
)

DAMAGE_SUMMARY_MARKERS_RE = re.compile(
    r"пострадал\w*|ранен\w*|поврежден\w*|разрушен\w*|обратил\w*\s+в\s+црб",
    re.IGNORECASE
)

EVENT_WORD_RE = re.compile(
    r"бпла|дрон|фпв|fpv|беспилот|опасност|тревог|отбой|сбит|сбил|пво|"
    r"ракет|авиац|атак|фиксац|прол[её]т|обстрел|взрыв|баллист|сирен|"
    r"оповещ|пострадал|поврежден|уничтож|угроз|отменен|снят",
    re.IGNORECASE
)

FOREIGN_CAPITALS_MAP = {
    "KURSK": r"Ор[её]л|Брянск|Воронеж|Белгород|Липецк|Тул[аеу]|Калуг[аеу]|Рязан[ьи]|Смоленск|Твер[ьи]|Краснодар|Ростов|Саратов|Самар[аеу]|Ульяновск|Пенз[аеу]|Перм[ьи]|Ижевск|Воткинск|Тольятти",
    "BELGOROD": r"Ор[её]л|Брянск|Воронеж|Курск|Липецк|Тул[аеу]|Калуг[аеу]|Рязан[ьи]|Смоленск|Твер[ьи]|Краснодар|Ростов|Саратов|Самар[аеу]|Ульяновск|Пенз[аеу]|Перм[ьи]|Ижевск|Воткинск|Тольятти",
    "MSK": r"Ор[её]л|Брянск|Воронеж|Курск|Белгород|Липецк|Тул[аеу]|Калуг[аеу]|Рязан[ьи]|Смоленск|Твер[ьи]|Ярославл[ьи]|Владимир|Самар[аеу]|Ульяновск|Пенз[аеу]|Перм[ьи]|Ижевск",
    "SPB": r"Псков|Новгород|Петрозаводск|Мурманск|Твер[ьи]|Смоленск|Москва"
}

PROTECTED_GEO_PREFIX_RE = re.compile(
    r"(?:в\s+сторону|со\s+стороны|в\s+направлении|курсом\s+на|курс\s+на|от|из|над|около|вблизи|"
    r"\bс\.|\bсело|\bсела|\bселе|\bп\.|\bпос\.|\bпос[её]лок|\bпос[её]лка|\bпгт\.?|"
    r"\bд\.|\bдер\.|\bдеревн[яие]|\bх\.|\bхут\.|\bхутор[ае]?|\bст\.|\bстаниц[аые]|\bаул[ае]?|"
    r"\bг\.о\.|\bокруг\w*|\bрайон\w*|\bр-н\w*)\s*$",
    re.IGNORECASE
)

def _has_protected_prefix(full_text: str, match_start: int) -> bool:
    """Проверяет текст перед найденным топонимом на наличие маркеров села/деревни/посёлка или маршрута."""
    preceding = full_text[max(0, match_start - 35):match_start]
    return bool(PROTECTED_GEO_PREFIX_RE.search(preceding))

def strip_foreign_from_comma_lists(body: str, reg: str) -> str:
    """
    Вырезает чужие области/края/республики/обл. и чужие областные центры
    из перечислений через запятую, 100% сохраняя любые сёла, деревни, посёлки, хутора и маршруты.
    """
    own_region_prefix = OWN_REGION_PREFIX_MAP[reg]

    def _replace_trailing_region(m):
        phrase = m.group(1)
        if own_region_prefix in phrase.lower():
            return m.group(0)
        if _has_protected_prefix(m.string, m.start(1)):
            return m.group(0)
        return ""

    def _replace_leading_region(m):
        phrase = m.group(1)
        if own_region_prefix in phrase.lower():
            return m.group(0)
        if _has_protected_prefix(m.string, m.start(1)):
            return m.group(0)
        return ""

    for _ in range(2):
        body = re.sub(
            rf",\s*({ANY_REGION_UNIT_PAT})(?=\s*(?:[,—\-:.]|отбой|опасност|тревог|внимани|$))",
            _replace_trailing_region,
            body,
            flags=re.IGNORECASE
        )
        body = re.sub(
            rf"({ANY_REGION_UNIT_PAT})\s*,\s*",
            _replace_leading_region,
            body,
            flags=re.IGNORECASE
        )

    capitals_pat = FOREIGN_CAPITALS_MAP.get(reg, "")
    if capitals_pat:
        def _replace_trailing_capital(m):
            if _has_protected_prefix(m.string, m.start(1)):
                return m.group(0)
            return ""

        def _replace_leading_capital(m):
            if _has_protected_prefix(m.string, m.start(1)):
                return m.group(0)
            return ""

        body = re.sub(
            rf",\s*(\b(?:{capitals_pat})\b)(?=\s*(?:[,—\-:.]|и\s+|отбой|опасност|тревог|$))",
            _replace_trailing_capital,
            body,
            flags=re.IGNORECASE
        )
        body = re.sub(
            rf"(\b(?:{capitals_pat})\b)\s*,\s*(?=[А-ЯЁа-яё])",
            _replace_leading_capital,
            body,
            flags=re.IGNORECASE
        )

    body = re.sub(r"\s{2,}", " ", body).strip(" ,")
    return body

def post_process_region_text(reg: str, raw_region_text: str, allow_region_prepend: bool = False) -> str:
    """
    Убирает Markdown (**), мусорные префиксы (ПОСТФАКТУМ:), вычищает чужие области из списков,
    детерминированно корректирует светофор с учетом ретроспективы/отрицаний и защищает от пустых/ложных тревог.
    """
    text = re.sub(r"(\*\*|__|~~|`)", "", raw_region_text)
    text = re.sub(r"(?m)^\s*#+\s*", "", text).strip()

    m = re.search(r"(🟢|🔴|🟡|⚪️|⚪)", text[:100])
    if not m:
        m = re.search(r"(🟢|🔴|🟡|⚪️|⚪)", text)

    had_explicit_light = bool(m)
    original_light = m.group(1) if m else "🟡"
    if original_light == "⚪️":
        original_light = "⚪"

    if m:
        body = (text[:m.start()] + " " + text[m.end():]).strip()
    else:
        body = text.strip()

    body = re.sub(
        r"^(?:ПОСТФАКТУМ|ИНФО\s*\(ПОСТФАКТУМ\)|ИНФО|СВОДКА|ПРЕДУПРЕЖДЕНИЕ|ПРЯМАЯ УГРОЗА)\s*[:—\-]\s*",
        "",
        body,
        flags=re.IGNORECASE
    ).strip()
    body = re.sub(r"^ОТБОЙ\s*[:—\-]\s*", "Отбой ", body, flags=re.IGNORECASE).strip()

    body = strip_foreign_from_comma_lists(body, reg)
    if not body or len(body.strip(" —-:,.!?")) < 3:
        return ""

    body_low = body.lower()

    has_event_word = bool(EVENT_WORD_RE.search(body_low))
    if not had_explicit_light and not has_event_word:
        return ""

    is_siren_check = "проверка систем оповещения" in body_low or "плановая проверка" in body_low
    has_refutation = bool(REFUTED_EVENT_RE.search(body_low))
    has_negated_clear = bool(NEGATED_CLEAR_RE.search(body_low))
    has_positive_clear = bool(POSITIVE_CLEAR_RE.search(body_low)) and not has_negated_clear and not has_refutation
    has_negated_threat = bool(NEGATED_THREAT_RE.search(body_low))
    is_active_regime = bool(ACTIVE_REGIME_RE.search(body_low)) and not has_negated_threat and not has_refutation

    body_without_clear = STRIP_CLEAR_CLAUSE_RE.sub(" ", body_low) if has_positive_clear else body_low
    has_live_urgent = bool(LIVE_URGENT_RE.search(body_without_clear)) and not has_refutation
    has_red_combat = bool(RED_COMBAT_RE.search(body_without_clear)) and not has_refutation
    has_red_direction = (
        bool(DIRECTION_RE.search(body_without_clear))
        and bool(MILITARY_CONTEXT_RE.search(body_without_clear))
        and not has_refutation
    )
    has_red = has_red_combat or has_red_direction

    has_yellow = (
        bool(YELLOW_TRIGGER_RE.search(body_without_clear))
        and not has_negated_threat
        and not has_refutation
    )

    is_retrospective = (
        (
            bool(RETROSPECTIVE_SUMMARY_RE.search(body_low))
            or (original_light == "⚪" and (bool(DAMAGE_SUMMARY_MARKERS_RE.search(body_low)) or len(body) > 180))
        )
        and not is_active_regime
        and not has_live_urgent
        and not has_positive_clear
    )

    if has_negated_clear and original_light == "🟢":
        original_light = "🟡"

    is_all_clear = (
        (
            has_positive_clear
            and not has_red
            and not has_live_urgent
            and not has_yellow
            and not has_negated_clear
            and not has_negated_threat
            and not is_active_regime
            and not is_retrospective
            and not has_refutation
        )
        or (
            original_light == "🟢"
            and not has_negated_clear
            and not has_negated_threat
            and not has_red
            and not has_live_urgent
            and not has_yellow
            and not is_active_regime
            and not is_retrospective
            and not has_refutation
        )
    )

    if is_siren_check or has_refutation:
        light = "⚪"
    elif is_retrospective:
        light = "⚪"
    elif has_red or has_live_urgent:
        light = "🔴"
    elif is_all_clear:
        light = "🟢"
    elif has_negated_clear:
        light = "🟡"
    elif has_negated_threat:
        light = "⚪"
    elif has_yellow or is_active_regime:
        light = "🟡"
    elif original_light == "🟢":
        light = "⚪"
    else:
        light = original_light

    short_region_name = BOTS_CONFIG[reg]["short_region"]
    if not has_event_word and had_explicit_light and len(body) < 60 and has_region_keywords(body, reg):
        if light == "🟢":
            body = f"{body} — отбой опасности по БПЛА."
        elif light == "🟡":
            body = f"{body} — опасность по БПЛА."
        elif light == "🔴":
            body = f"{body} — тревога по БПЛА."

    # Подставляем название области ТОЛЬКО для разрешенных моно-каналов и при полном отсутствии чужих регионов
    if allow_region_prepend and not has_region_keywords(body, reg) and not has_foreign_keywords(body, reg):
        body = f"{short_region_name} — {body}"

    return f"{light} {body}"

class AsyncRateLimiter:
    def __init__(self, rate, per):
        self.rate = rate
        self.per = per
        self.allowance = min(rate, 3)
        self.last_check = time.monotonic()
        self.lock = None

    async def acquire(self):
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            current = time.monotonic()
            time_passed = current - self.last_check
            self.allowance += time_passed * (self.rate / self.per)
            if self.allowance > self.rate:
                self.allowance = self.rate
            if self.allowance < 1.0:
                await asyncio.sleep((1.0 - self.allowance) * (self.per / self.rate))
                self.last_check = time.monotonic()
                self.allowance = 0.0
            else:
                self.last_check = current
                self.allowance -= 1.0

ai_rate_limiter = AsyncRateLimiter(15, 60)
ai_semaphore = asyncio.Semaphore(3)
app = Client("radar_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

def sync_send_to_channel(region, text, reply_to=None):
    bot_info = BOTS_CONFIG.get(region)
    if not bot_info or not bot_info.get("token"):
        log_msg("ERROR", "CONFIG", f"Не задан токен для региона {region}")
        return None
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    safe_name = escape(bot_info["name"])
    safe_body = escape_and_truncate(text)
    final_text = f"{safe_body}\n\n📡 Дозор.ру | {safe_name} - <a href='{channel_link}'>Подписаться</a>"
    payload = {
        "chat_id": bot_info["channel"],
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_to and isinstance(reply_to, int) and reply_to > 0:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=15)
            if response.status_code == 200:
                return response.json().get("result", {}).get("message_id")
            elif response.status_code == 429:
                retry_sec = int(response.json().get("parameters", {}).get("retry_after", 3)) + 1
                log_msg("WARN", "TG", f"[{region}] Лимит Telegram 429, ожидание {retry_sec} сек.")
                time.sleep(min(retry_sec, 120))
                continue
            elif response.status_code in [400, 401, 403, 404]:
                log_msg("ERROR", "TG", f"[{region}] Ошибка Telegram ({response.status_code}): {response.text[:200]}")
                return None
            else:
                time.sleep(2)
        except requests.exceptions.ReadTimeout:
            log_msg("WARN", "TG", f"[{region}] ReadTimeout при отправке (статус неизвестен, без повтора во избежание дубля).")
            return -1
        except Exception as e:
            log_msg("WARN", "TG", f"[{region}] Сетевая ошибка (попытка {attempt+1}): {type(e).__name__}")
            time.sleep(2)

    log_msg("ERROR", "TG", f"[{region}] Не удалось отправить сообщение после 3 попыток.")
    return None

async def process_with_ai(text, source, allowed_regions):
    regions_desc = "\n   ".join(BOTS_CONFIG[r]["prompt_desc"] for r in allowed_regions)
    allowed_tags_str = ", ".join(f"[{r}]" for r in allowed_regions)

    prompt = f"""
Ты — строгий военный радар-диспетчер. Твоя задача — проанализировать сообщение и подготовить точный пост для Telegram-канала.
Источник: {source}
Текст:
{text}

ПРАВИЛА (ВЫПОЛНЯТЬ СТРОГО):

1. ЦЕЛЕВЫЕ РЕГИОНЫ ДАННОГО ИСТОЧНИКА:
   {regions_desc}
   - Если в тексте НЕТ информации, относящейся к указанным выше целевым регионам (например, речь идет только про Краснодарский край, Анапу, Сочи, Крым, Воронеж, Ростов, Брянск, Орел, Пермь, Ульяновск, Самару, Пензу, Удмуртию или время "10:00 мск") — верни ТОЛЬКО одно слово: ИГНОР
   - Если в посте перечислено несколько областей (например, "Курская область, Орловская область — отбой опасности по БПЛА"), УДАЛИ все чужие области и оставь ТОЛЬКО наш целевой регион!
   - Соседний регион разрешено оставить ТОЛЬКО если он указывает направление пролёта в наш регион или из него (например: "Курская область — пролёт БПЛА в сторону Орла").
   - Внутри нашего целевого региона сохраняй ВСЕ названия городов, районов, округов и сёл на 100%!

2. ПОЛНОТА, ГРАМОТНОСТЬ И ЧИСТОТА ТЕКСТА:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown-разметку: никаких звездочек **, нижних подчеркиваний __ или решеток #. Пиши чистым текстом!
   - Запрещено писать слова "ПОСТФАКТУМ:", "ИНФО:", "СВОДКА:" перед текстом.
   - Каждый пост обязан содержать И географию (область/район/город), И полную суть события. Запрещено выдавать огрызки из 2 слов вроде "Курская область" или "Уничтожено БПЛА: Курская область" без количества сбитых целей!
   - В сводках обстрелов и последствий по районам пиши грамотным русским языком, сохраняй все районы, села и факты полностью, НЕ обрывай текст и НЕ придумывай странных склеенных слов (пиши "от удара дрона сгорел автомобиль", а не "сгорел дрон-автомобиль").

3. СВЕТОФОРЫ (выбери ровно один эмодзи в самом начале после тега):
   🔴 — Прямая угроза ПРЯМО СЕЙЧАС: сбитие БПЛА, сбили БПЛА, работа ПВО, тревога по БПЛА, фиксация БПЛА, пролёт/движение/курс БПЛА, атака БПЛА или FPV, опасность по FPV, опасность по осколкам/обломкам, ракетная/авиационная опасность, баллистика, обстрел. (Одиночное "Сбитие БПЛА" или "Работа ПВО" — это СТРОГО 🔴!).
   🟡 — Предупреждение: общее объявление режима "Опасность по БПЛА", "Внимание по БПЛА" (когда объявлена опасность по области/району, но без конкретных пролётов, работы ПВО и сбитий).
   🟢 — Отбой: отбой любой опасности или тревоги. Если сообщение содержит "Текущее обновление", определяй отбой ТОЛЬКО по "Текущему обновлению".
   ⚪ — Итоговая сводка за прошедший период: только большие отчёты за ночь/сутки ("Ночью силами ПВО уничтожено...", сводки о пострадавших и разрушениях на земле за день).

4. МУСОР = ИГНОР:
   - Реклама, сборы денег, казино, проверка сирен в других регионах — верни ТОЛЬКО слово: ИГНОР

ФОРМАТ ОТВЕТА СТРОГО ТАКОЙ:
[ТЕГ_РЕГИОНА] 🔴/🟡/🟢/⚪ Текст сообщения

КАЖДЫЙ БЛОК РЕГИОНА НАЧИНАЙ С НОВОЙ СТРОКИ.
ИСПОЛЬЗУЙ ТОЛЬКО РАЗРЕШЕННЫЕ АНГЛИЙСКИЕ ТЕГИ: {allowed_tags_str}.
"""
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model='gemini-3.5-flash-lite', contents=prompt, config=AI_CONFIG
                ),
                timeout=50.0
            )
            if not response.text:
                await asyncio.sleep(2)
                continue
            return response.text.strip()
        except asyncio.TimeoutError:
            await asyncio.sleep(2)
        except APIError as e:
            if "429" in str(e) or "quota" in str(e):
                await asyncio.sleep(30)
            elif "safety" in str(e):
                return "ИГНОР"
            else:
                await asyncio.sleep(3)
        except Exception:
            await asyncio.sleep(3)
    return None

@app.on_message()
async def radar_handler(client, message):
    global _pending_snapshot
    if not message.chat:
        return
    username = (message.chat.username or "").lower()
    if username not in SOURCE_ROUTING:
        return

    now_ts = time.time()
    source_msg_key = f"{message.chat.id}_{message.id}"
    if source_msg_key in PROCESSED_MESSAGES:
        return
    PROCESSED_MESSAGES[source_msg_key] = now_ts

    with stats_lock:
        STATS["processed"] += 1
    if len(PROCESSED_MESSAGES) > 500:
        cutoff = now_ts - 3600
        old_keys = [k for k, ts_val in PROCESSED_MESSAGES.items() if ts_val < cutoff]
        for k in (old_keys or list(PROCESSED_MESSAGES.keys())[:100]):
            PROCESSED_MESSAGES.pop(k, None)
    if len(SENT_REGIONS) > 1000:
        cutoff = now_ts - 7200
        old_r_keys = [k for k, ts_val in SENT_REGIONS.items() if ts_val < cutoff]
        for k in (old_r_keys or list(SENT_REGIONS.keys())[:200]):
            SENT_REGIONS.pop(k, None)

    raw_text = message.text or message.caption or ""
    if not raw_text:
        return

    clean_current = clean_source_text(raw_text)
    if not clean_current:
        return

    if message.reply_to_message:
        reply_raw = message.reply_to_message.text or message.reply_to_message.caption or ""
        clean_reply = clean_source_text(reply_raw)
        if clean_reply:
            text = f"Исходное сообщение: {clean_reply}\n\nТекущее обновление: {clean_current}"
        else:
            text = clean_current
    else:
        text = clean_current

    if message.date:
        d = message.date
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if now_ts - d.timestamp() > 7200 or now_ts - d.timestamp() < -300:
            return

    allowed_regions = SOURCE_ROUTING[username]
    matched_regions = [reg for reg in allowed_regions if has_region_keywords(text, reg)]
    is_mono_source = (username in MONO_REGION_SOURCES)

    # ВАЖНО: Подстановка области разрешена ТОЛЬКО для чисто регионального канала (radar_ru_belgorod),
    # и НИКОГДА для общероссийского locatorru!
    if not matched_regions and is_mono_source:
        single_reg = MONO_REGION_SOURCES[username]
        text_low = text.lower()
        has_alert_word = any(w in text_low for w in ["бпла", "опасност", "тревог", "отбой", "ракет", "пво", "сбит", "сбил", "обстрел"])
        if has_alert_word and not has_foreign_keywords(text, single_reg):
            matched_regions = [single_reg]
            text = f"({BOTS_CONFIG[single_reg]['short_region']}) {text}"

    if not matched_regions:
        return

    if username not in SOURCE_LOCKS:
        SOURCE_LOCKS[username] = asyncio.Lock()
    source_lock = SOURCE_LOCKS[username]

    async with source_lock:
        await ai_rate_limiter.acquire()
        async with ai_semaphore:
            result = await process_with_ai(text, username, matched_regions)

        if result is None:
            PROCESSED_MESSAGES.pop(source_msg_key, None)
            log_msg("ERROR", "AI", f"[{username}] Отказ Gemini API после 3 попыток (MSG_ID: {message.id})")
            return

        if is_ignore_text(result):
            return

        log_msg("INFO", "AI", f"Ответ ИИ:\n{result[:300]}")

        result = re.sub(
            rf"(?<!^)(?<!\n)\s*(\[(?:{ALL_TAGS_REGEX})[^\]]*\]\s*[🔴🟢🟡⚪])",
            r"\n\1",
            result,
            flags=re.IGNORECASE
        )

        state_changed = False
        current_ts = time.time()
        found_any_tags = False
        parsed_regions = []
        any_send_failed = False

        for reg in matched_regions:
            reg_send_key = f"{source_msg_key}_{reg}"
            if reg_send_key in SENT_REGIONS:
                found_any_tags = True
                parsed_regions.append(reg)
                continue

            reg_aliases = TAG_ALIASES[reg]
            pattern = rf"(?:^|\n)\s*\[(?:{reg_aliases})[^\]]*\]\s*(.*?)(?=\n\s*\[(?:{ALL_TAGS_REGEX})[^\]]*\]\s*[🔴🟢🟡⚪]|\Z)"

            matches = list(re.finditer(pattern, result, re.IGNORECASE | re.DOTALL))
            if matches:
                region_text = matches[-1].group(1).strip()
            elif (
                len(matched_regions) == 1
                and not re.search(rf"\[(?:{ALL_TAGS_REGEX})[^\]]*\]", result, re.IGNORECASE)
                and bool(re.match(r"^\s*(?:🟢|🔴|🟡|⚪️|⚪)", result))
            ):
                region_text = result.strip()
            else:
                continue

            region_text = re.sub(r"\s*\[[^\]]*$", "", region_text).strip()
            if is_ignore_text(region_text):
                continue

            region_text = post_process_region_text(reg, region_text, allow_region_prepend=is_mono_source)
            if not region_text:
                continue

            if not has_region_keywords(region_text, reg):
                log_msg("WARN", "FILTER", f"[{reg}] Отклонен чужой пост: {region_text[:100]}")
                continue

            found_any_tags = True
            parsed_regions.append(reg)

            head = region_text.lstrip()[:20]
            is_green = "🟢" in head
            is_red = "🔴" in head
            is_yellow = "🟡" in head

            text_low = region_text.lower()
            is_missile_type = any(w in text_low for w in ["ракет", "авиац", "баллист"])
            is_uav_type = any(w in text_low for w in ["бпла", "дрон", "беспилот", "фпв", "fpv"])
            clear_both_slots = is_green and ((not is_missile_type and not is_uav_type) or (is_missile_type and is_uav_type))
            alert_slot = "missile" if is_missile_type else "uav"

            is_general_alert = is_yellow or (
                is_red and any(w in text_low for w in [
                    "ракетная опасность", "авиационная опасность", "тревога по бпла", "опасность атаки бпла"
                ])
                and not bool(re.search(r"\bсби(?:т[аыо]?|л[аио]?|ти[ея])\b|фиксация|прол[её]т", text_low))
            )

            reply_to = None
            snapshot_slot_ids = {}
            async with state_lock:
                reg_state = ACTIVE_ALERTS.get(reg) or {"uav": None, "missile": None}
                for s_key in ("uav", "missile"):
                    s_val = reg_state.get(s_key)
                    snapshot_slot_ids[s_key] = s_val.get("msg_id") if isinstance(s_val, dict) else None

                if is_green:
                    if clear_both_slots:
                        candidates = [
                            e for e in (reg_state.get("missile"), reg_state.get("uav"))
                            if isinstance(e, dict) and isinstance(e.get("msg_id"), int) and e["msg_id"] > 0
                            and (current_ts - e.get("ts", 0) < ALERT_TTL_SEC)
                        ]
                        if candidates:
                            candidates.sort(key=lambda x: x.get("ts", 0), reverse=True)
                            reply_to = candidates[0]["msg_id"]
                    else:
                        target_entry = reg_state.get(alert_slot)
                        if isinstance(target_entry, dict) and isinstance(target_entry.get("msg_id"), int) and target_entry["msg_id"] > 0:
                            if current_ts - target_entry.get("ts", 0) < ALERT_TTL_SEC:
                                reply_to = target_entry["msg_id"]

            loop = asyncio.get_running_loop()
            msg_id = await loop.run_in_executor(None, sync_send_to_channel, reg, region_text, reply_to)

            if msg_id is None:
                any_send_failed = True
            elif msg_id == -1:
                SENT_REGIONS[reg_send_key] = current_ts
            elif isinstance(msg_id, int) and msg_id > 0:
                SENT_REGIONS[reg_send_key] = current_ts
                log_msg("INFO", "SEND", f"[{reg}] Успешно (MSG_ID: {msg_id}, REPLY_TO: {reply_to})")
                with stats_lock:
                    STATS["sent"] += 1

                async with state_lock:
                    reg_state = ACTIVE_ALERTS.setdefault(reg, {"uav": None, "missile": None})
                    if is_green:
                        slots_to_clear = ("missile", "uav") if clear_both_slots else (alert_slot,)
                        for s_key in slots_to_clear:
                            cur_slot = reg_state.get(s_key)
                            if isinstance(cur_slot, dict):
                                same_anchor = (cur_slot.get("msg_id") == snapshot_slot_ids.get(s_key))
                                not_newer_src = (cur_slot.get("src_msg_id", 0) <= message.id)
                                if same_anchor and not_newer_src:
                                    reg_state[s_key] = None
                                    state_changed = True
                    elif is_red or is_yellow:
                        slots_to_set = ("missile", "uav") if (is_missile_type and is_uav_type) else (alert_slot,)
                        for s_key in slots_to_set:
                            active_entry = reg_state.get(s_key)
                            is_expired = (not active_entry) or (current_ts - active_entry.get("ts", 0) > ALERT_TTL_SEC)
                            has_valid_id = isinstance(active_entry, dict) and isinstance(active_entry.get("msg_id"), int) and active_entry["msg_id"] > 0
                            not_stale_event = (not isinstance(active_entry, dict)) or (message.id >= active_entry.get("src_msg_id", 0))

                            if (is_expired or not has_valid_id) and not_stale_event:
                                reg_state[s_key] = {
                                    "msg_id": msg_id,
                                    "ts": current_ts,
                                    "is_general": is_general_alert,
                                    "src_msg_id": message.id
                                }
                                state_changed = True
                            elif is_general_alert and not active_entry.get("is_general", False) and not_stale_event:
                                reg_state[s_key] = {
                                    "msg_id": msg_id,
                                    "ts": current_ts,
                                    "is_general": True,
                                    "src_msg_id": message.id
                                }
                                state_changed = True

        if any_send_failed:
            PROCESSED_MESSAGES.pop(source_msg_key, None)

        # Телеметрия пропущенных регионов
        if state_changed or found_any_tags:
            all_legit_tags = []
            for m in re.finditer(rf"\[(?:{ALL_TAGS_REGEX})[^\]]*\]", result, re.IGNORECASE):
                pos = m.start()
                at_line_start = (pos == 0 or result[pos - 1] == "\n")
                after = result[m.end():m.end() + 5]
                has_light_after = bool(re.match(r"\s*[🔴🟢🟡⚪]", after))
                if at_line_start or has_light_after:
                    all_legit_tags.append(m.group(0))

            found_regions = set()
            for tag in all_legit_tags:
                tag_upper = tag.upper()
                for reg in matched_regions:
                    aliases = TAG_ALIASES_LIST.get(reg, [])
                    if any(alias.upper() in tag_upper for alias in aliases):
                        found_regions.add(reg)
                        break

            missed_regions = found_regions - set(parsed_regions)
            if missed_regions:
                log_msg("WARN", "PARSE", f"Пропущены регионы {missed_regions}. Теги: {all_legit_tags}")
                log_msg("WARN", "AI_FULL", f"Full response:\n{result[:1000]}")

        if not found_any_tags:
            log_msg("WARN", "PARSE", "Пост не прошел финальную проверку или без валидных тегов.")
            log_msg("WARN", "AI_FULL", f"Full response:\n{result[:1000]}")

        if state_changed:
            async with state_lock:
                _pending_snapshot = copy.deepcopy(ACTIVE_ALERTS)
            _state_dirty.set()

async def main():
    global MAIN_LOOP, ADMIN_QUEUE, ACTIVE_ALERTS, state_lock, _state_dirty, SOURCE_LOCKS
    state_lock = asyncio.Lock()
    _state_dirty = asyncio.Event()
    SOURCE_LOCKS = {src: asyncio.Lock() for src in SOURCE_ROUTING}
    MAIN_LOOP = asyncio.get_running_loop()
    ADMIN_QUEUE = asyncio.Queue(maxsize=200)
    ACTIVE_ALERTS = await MAIN_LOOP.run_in_executor(None, _load_alerts_sync)
    await app.start()
    notifier_task = asyncio.create_task(admin_notifier_worker())
    heartbeat_task = asyncio.create_task(heartbeat_worker())
    state_saver_task = asyncio.create_task(state_saver_worker())
    async for _ in app.get_dialogs(limit=50):
        pass
    if ADMIN_CHAT_ID:
        await MAIN_LOOP.run_in_executor(None, sync_send_admin_message, "✅ <b>Радар (V18.7 Argon Prod) запущен!</b>")
    try:
        await idle()
    finally:
        if state_saver_task and not state_saver_task.done():
            state_saver_task.cancel()
            try:
                await asyncio.wait_for(state_saver_task, timeout=5.0)
            except (asyncio.CancelledError, Exception):
                pass
        if _state_dirty.is_set() and _pending_snapshot is not None:
            _save_alerts_sync(_pending_snapshot)
        tasks = [t for t in (notifier_task, heartbeat_task) if t and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10.0)
            for t in pending:
                t.cancel()
        await app.stop()

if __name__ == "__main__":
    app.run(main())
