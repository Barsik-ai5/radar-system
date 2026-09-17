import os
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

# === ПРОФЕССИОНАЛЬНОЕ ЛОГИРОВАНИЕ ===
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

# === ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ===
API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not API_ID_RAW or not API_HASH or not SESSION_STRING:
    raise RuntimeError("[-] ОШИБКА СТАРТА: Не заданы ключи Телеграма!")

API_ID = int(API_ID_RAW)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("[-] ОШИБКА СТАРТА: Не задан GEMINI_API_KEY!")

HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_HOURS", "6")) * 3600

ai_client = genai.Client(api_key=GEMINI_API_KEY)
AI_CONFIG = types.GenerateContentConfig(
    safety_settings=[
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ],
    max_output_tokens=800, 
    temperature=0.3        
)

BOTS_CONFIG = {
    "SPB": {"token": os.getenv("BOT_SPB"), "channel": "@RadarLO_SPB", "name": "Питер и Ленинградская область"},
    "MSK": {"token": os.getenv("BOT_MSK"), "channel": "@Radar_MSK_OBL", "name": "Москва и Московская область"},
    "BELGOROD": {"token": os.getenv("BOT_BELGOROD"), "channel": "@Radar_Belgorod_Obl", "name": "Белгород и Белгородская область"},
    "KURSK": {"token": os.getenv("BOT_KURSK"), "channel": "@Radar_Kursk", "name": "Курск и Курская область"}
}

SOURCE_ROUTING = {
    "locatorru": ["KURSK", "BELGOROD"], 
    "vrv_radar": ["SPB", "MSK"],
    "radar_ru_belgorod": ["BELGOROD"]
}

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN") or next((v["token"] for v in BOTS_CONFIG.values() if v["token"]), None)

MAIN_LOOP = None      
ADMIN_QUEUE = None    

def _notify_admin_async(line):
    if not ADMIN_CHAT_ID or MAIN_LOOP is None: return
    try: MAIN_LOOP.call_soon_threadsafe(_enqueue_admin_line, line)
    except RuntimeError: pass  

def _enqueue_admin_line(line):
    if ADMIN_QUEUE is None: return
    try: ADMIN_QUEUE.put_nowait(line)
    except asyncio.QueueFull: pass

def sync_send_admin_message(text):
    if not ADMIN_CHAT_ID or not ADMIN_BOT_TOKEN: return None
    url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for _ in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200: return True
            elif resp.status_code == 429:
                time.sleep(min(resp.json().get("parameters", {}).get("retry_after", 2), 15))
                continue
            return None
        except Exception: return None
    return None

async def admin_notifier_worker():
    if not ADMIN_CHAT_ID: return
    while True:
        try:
            first_line = await ADMIN_QUEUE.get()
            batch = [first_line]
            deadline = time.monotonic() + 5.0
            while len(batch) < 20:
                remaining = deadline - time.monotonic()
                if remaining <= 0: break
                try: batch.append(await asyncio.wait_for(ADMIN_QUEUE.get(), timeout=remaining))
                except asyncio.TimeoutError: break
            raw_text = "\n\n".join(batch)
            text = f"🚨 <b>Радар: {len(batch)} ошиб(ок)</b>\n\n<pre>{escape(raw_text[:600])}</pre>"
            await asyncio.get_running_loop().run_in_executor(None, sync_send_admin_message, text)
        except asyncio.CancelledError: break
        except Exception as e:
            # 🔥 ФИКС 2: Ловим тихие падения
            print(f"[ERROR] [ADMIN] Сбой воркера уведомлений: {e}")

async def heartbeat_worker():
    if not ADMIN_CHAT_ID: return
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        try:
            with stats_lock:
                proc, sent, errs = STATS['processed'], STATS['sent'], STATS['errors']
                STATS['processed'] = STATS['sent'] = STATS['errors'] = 0
            async with state_lock:
                active_count = len(ACTIVE_ALERTS)
            text = f"✅ <b>Радар жив.</b>\nОбработано: {proc}\nОтправлено: {sent}\nОшибок: {errs}\nАктивных тревог: {active_count}"
            await asyncio.get_running_loop().run_in_executor(None, sync_send_admin_message, text)
        except asyncio.CancelledError: break
        except Exception as e:
            # 🔥 ФИКС 2: Ловим тихие падения
            print(f"[ERROR] [ADMIN] Сбой heartbeat: {e}")

# === СОСТОЯНИЕ ТРЕВОГ ===
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts_state.json")
state_lock = None 
file_write_lock = threading.Lock()

_pending_snapshot = None
_state_dirty = None 

def _load_alerts_sync():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in list(data.items()):
                        if not isinstance(v, dict):
                            data[k] = {"msg_id": v if isinstance(v, int) else None, "ts": 0}
                    return data
        except Exception: pass
    return {}

def _save_alerts_sync(data):
    with file_write_lock:
        try:
            tmp_file = STATE_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_file, STATE_FILE)
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Ошибка записи JSON: {e}")

async def state_saver_worker():
    global _pending_snapshot
    while True:
        try:
            await _state_dirty.wait()
            _state_dirty.clear()
            snap = _pending_snapshot
            if snap is None: continue
            await asyncio.get_running_loop().run_in_executor(None, _save_alerts_sync, snap)
        except asyncio.CancelledError: break
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Ошибка state_saver: {e}")

PROCESSED_MESSAGES = {} 

REGION_KEYWORDS = {
    "BELGOROD": ["белгород", "белгородск", "шебекин", "валуй", "грайворон", "оскол", "губкин", "волоконов", "борисов", "ивня", "ракитн", "краснояруж", "алексеев", "короч", "вейделев", "ровен", "чернян", "прохоров", "строител", "октябрьск", "томаров", "тавров", "дубов", "майск", "разумн", "беломестн", "стрелецк", "головчин", "бессонов", "красн"],
    "KURSK": ["курск", "курчат", "судж", "рыльск", "обоян", "льгов", "коренев", "глушков", "беловск", "железногорск", "фатеж", "щигр", "дмитриев", "тим", "горшечн", "поныр", "медвен", "золотухин", "мантуров", "солнцев", "черемисин", "касторн", "пристен", "прямицын", "теткин"],
    "MSK": ["москв", "московск", "мск", "moscow", "подмосков", "мкад", "подольск", "люберц", "королев", "химк", "балаших", "мытищ", "красногорск", "одинцов", "домодедов", "зеленоград", "раменск", "ступин", "кашир", "коломн", "чехов", "серпухов"],
    "SPB": ["петербург", "санкт", "питер", "спб", "с-пб", "ленинград", "ленинградск", "ленобласт", "лен област", "лен. област", "выборг", "гатчин", "кронштадт", "луг", "кингисепп", "волхов", "тихвин", "всеволожск", "мурин", "кудров", "тосн"]
}
KW_COMMON = ["отбой", "днепро", "харьков", "сумы", "полтав", "авиаторск", "волчанск"]
KW_EXACT = ["тихо", "чисто"]

TAG_ALIASES_LIST = {
    "SPB": ["SPB", "СПБ", "PITER", "ПИТЕР", "ПЕТЕРБУРГ", "ЛЕНИНГРАДСК"],
    "MSK": ["MSK", "МСК", "MOSCOW", "МОСКВА", "МОСКОВСК", "ПОДМОСКОВ"],
    "BELGOROD": ["BELGOROD", "БЕЛГОРОД", "БЕЛГОРОДСК"],
    "KURSK": ["KURSK", "КУРСК", "КУРСКАЯ"]
}

TAG_ALIASES = {k: "|".join(re.escape(a) for a in v) for k, v in TAG_ALIASES_LIST.items()}
ALL_TAGS_REGEX = "|".join([re.escape(tag) for tags in TAG_ALIASES_LIST.values() for tag in tags])

class AsyncRateLimiter:
    def __init__(self, rate, per):
        self.rate = rate
        self.per = per
        self.allowance = min(rate, 3) 
        self.last_check = time.monotonic()
        self.lock = None 
    async def acquire(self):
        if self.lock is None: self.lock = asyncio.Lock()
        async with self.lock:
            current = time.monotonic()
            time_passed = current - self.last_check
            self.allowance += time_passed * (self.rate / self.per)
            if self.allowance > self.rate: self.allowance = self.rate
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
    if not bot_info: return None
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    safe_name = escape(bot_info['name'])
    final_text = f"{escape(text[:600])}\n\n📡 Дозор.ру | {safe_name} - <a href='{channel_link}'>Подписаться</a>"
    payload = {"chat_id": bot_info['channel'], "text": final_text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to and reply_to != -1:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=15)
            if response.status_code == 200: return response.json().get("result", {}).get("message_id")
            elif response.status_code == 429:
                time.sleep(min(response.json().get("parameters", {}).get("retry_after", 2), 15))
                continue
            elif response.status_code in [400, 401, 403, 404]: break 
            else: time.sleep(2)
        except requests.exceptions.ReadTimeout: return -1 
        except Exception: time.sleep(2)
    return None

async def process_with_ai(text, source):
    prompt = f"""
Ты — строгий военный радар-ассистент. Твоя задача — проанализировать текст, ВЫДЕЛИТЬ САМОЕ ВАЖНОЕ и отформатировать для Telegram-канала.
Источник: {source}
Текст: {text}

ПРАВИЛА И ФОРМАТИРОВАНИЕ (ВЫПОЛНЯТЬ СТРОГО):

1. ГЕОГРАФИЯ НЕРУШИМА (КРИТИЧЕСКИ ВАЖНО): Убирай «воду» и лишние слова, НО КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО удалять названия населенных пунктов, районов, сел и областей нашего целевого региона! Каждое упоминание конкретного места (например, "Железногорск", "Курская область", "Питер", "Москва") ОБЯЗАНО остаться в тексте.

2. СВЕТОФОРЫ (КРИТИЧЕСКИ ВАЖНО): Каждый твой ответ ОБЯЗАН начинаться с одного из эмодзи-светофоров:
   🔴 — Прямая угроза: ракетная/авиационная опасность, баллистика, обстрел, атака FPV, работа ПВО, Тревога по БПЛА, движение БПЛА. опасность по ФПВ, внимание по обломкам/осколкам, фиксация бпла.
   🟢 — Отбой: отбой любой опасности.
   🟡 — Предупреждение: опасность по БПЛА, внимание по бпла (предупреждение без конкретной атаки).
   ⚪️ — Инфо (постфактум): сводка, разрушения на земле, статистика.

3. ПРАВИЛО ОТБОЯ: Если сообщение является reply и содержит "Текущее обновление", цвет определяй ИСКЛЮЧИТЕЛЬНО по "Текущему обновлению". Если это отбой, ставь 🟢.

4. МУСОР = ИГНОР. Если в тексте сборы, реклама, "опасность сохраняется" (без указания района) — верни ТОЛЬКО слово ИГНОР.

5. РЕГИОНЫ: Мы отслеживаем ТОЛЬКО: Питер/Ленобласть -> [SPB], Москва/МО -> [MSK], Белгородская обл -> [BELGOROD], Курск/Курская обл -> [KURSK].
   Вырежи из текста все ЧУЖИЕ регионы, но СВОИ города и районы оставь на 100%!

ФОРМАТ ОТВЕТА СТРОГО ТАКОЙ:
[ТЕГ_РЕГИОНА] 🔴/🟢/🟡/⚪️ Твой переработанный текст

КАЖДЫЙ БЛОК РЕГИОНА НАЧИНАЙ С НОВОЙ СТРОКИ. Если постов несколько — разделяй их пустой строкой.

КРИТИЧЕСКОЕ ТРЕБОВАНИЕ: ТЕГИ ПИСАТЬ СТРОГО НА АНГЛИЙСКОМ! ИСПОЛЬЗУЙ ТОЛЬКО [SPB], [MSK], [BELGOROD] ИЛИ [KURSK]. Никаких русских букв в квадратных скобках! Никаких [СПБ] или [МСК]!
"""
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model='gemini-3.5-flash-lite', contents=prompt, config=AI_CONFIG
                ), timeout=50.0)
            if not response.text:
                await asyncio.sleep(2)
                continue
            return response.text.strip()
        except asyncio.TimeoutError: await asyncio.sleep(2)
        except APIError as e:
            if "429" in str(e) or "quota" in str(e): await asyncio.sleep(30)
            elif "safety" in str(e): return "ИГНОР"
            else: await asyncio.sleep(3)
        except Exception: await asyncio.sleep(3)
    return None

@app.on_message()
async def radar_handler(client, message):
    global _pending_snapshot
    if not message.chat: return
    username = (message.chat.username or "").lower()
    if username not in SOURCE_ROUTING: return 

    source_msg_key = f"{message.chat.id}_{message.id}"
    if source_msg_key in PROCESSED_MESSAGES: return
    PROCESSED_MESSAGES[source_msg_key] = time.time()
    
    with stats_lock: STATS["processed"] += 1
    if len(PROCESSED_MESSAGES) > 500:
        for k in list(PROCESSED_MESSAGES.keys())[:100]: PROCESSED_MESSAGES.pop(k, None)

    text = message.text or message.caption or ""
    if not text: return 

    if message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if reply_text: text = f"Исходное сообщение: {reply_text}\n\nТекущее обновление: {text}"

    if message.date:
        d = message.date
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        if time.time() - d.timestamp() > 7200 or time.time() - d.timestamp() < -300: return

    text_lower = text.lower()
    allowed_regions = SOURCE_ROUTING[username]
    target_kws = KW_COMMON.copy()
    for reg in allowed_regions:
        target_kws.extend(REGION_KEYWORDS.get(reg, []))
        
    relevant_kw_found = False
    
    for kw in KW_EXACT:
        if re.search(rf"\b{kw}\b", text_lower, re.IGNORECASE):
            relevant_kw_found = True
            break
            
    if not relevant_kw_found:
        for kw in target_kws:
            if len(kw) <= 4:
                if re.search(rf"\b{kw}", text_lower, re.IGNORECASE):
                    relevant_kw_found = True
                    break
            else:
                if kw in text_lower:
                    relevant_kw_found = True
                    break
                
    if not relevant_kw_found:
        return 

    await ai_rate_limiter.acquire()
    async with ai_semaphore:
        result = await process_with_ai(text, username)
    
    if not result or result.strip().upper() == "ИГНОР": return
    log_msg("INFO", "AI", f"Ответ ИИ:\n{result[:300]}") 

    state_changed = False
    current_ts = time.time()
    found_any_tags = False 
    parsed_regions = []
    
    for reg in allowed_regions:
        reg_aliases = TAG_ALIASES[reg]
        pattern = rf"(?:^|\n)\s*\[(?:{reg_aliases})[^\]]*\]\s*(.*?)(?=\n\s*\[(?:{ALL_TAGS_REGEX})[^\]]*\]\s*[🔴🟢🟡⚪]|\Z)"
        
        matches = list(re.finditer(pattern, result, re.IGNORECASE | re.DOTALL))
        if not matches:
            continue
            
        region_text = matches[-1].group(1).strip()
        region_text = re.sub(r"\s*\[[^\]]*$", "", region_text).strip()
        
        if not region_text: continue
        found_any_tags = True
        parsed_regions.append(reg)
            
        head = region_text.lstrip()[:100]
        m = re.search(r"(🟢|🔴|🟡)", head)
        if not m:
            m = re.search(r"(🟢|🔴|🟡)", region_text)
            
        is_green = is_red = is_yellow = False
        if m:
            light = m.group(1)
            is_green = (light == "🟢")
            is_red = (light == "🔴")
            is_yellow = (light == "🟡")
        
        reply_to = None
        async with state_lock:
            if is_green:
                active = ACTIVE_ALERTS.get(reg)
                if not active: continue 
                reply_to = active.get("msg_id")
            
        loop = asyncio.get_running_loop()
        msg_id = await loop.run_in_executor(None, sync_send_to_channel, reg, region_text, reply_to)
        
        if msg_id:
            if msg_id != -1: log_msg("INFO", "SEND", f"[{reg}] Успешно (MSG_ID: {msg_id})")
            with stats_lock: STATS["sent"] += 1
            async with state_lock:
                if is_green:
                    active = ACTIVE_ALERTS.get(reg)
                    if active and active.get("msg_id") == reply_to:
                        ACTIVE_ALERTS.pop(reg, None)
                        state_changed = True
                elif is_red or is_yellow:
                    active = ACTIVE_ALERTS.get(reg, {})
                    if current_ts >= active.get("ts", 0):
                        ACTIVE_ALERTS[reg] = {"msg_id": msg_id, "ts": current_ts}
                        state_changed = True

    # 🔥 ФИКС 1: Телеметрия. Ищет ВСЕ теги, включая склеенные в одну строку (светофор после тега)
    if state_changed or found_any_tags:
        all_legit_tags = []
        for m in re.finditer(rf"\[(?:{ALL_TAGS_REGEX})[^\]]*\]", result, re.IGNORECASE):
            pos = m.start()
            at_line_start = (pos == 0 or result[pos-1] == '\n')
            after = result[m.end():m.end()+5]
            has_light_after = bool(re.match(r"\s*[🔴🟢🟡⚪]", after))
            if at_line_start or has_light_after:
                all_legit_tags.append(m.group(0))
        
        found_regions = set()
        for tag in all_legit_tags:
            tag_upper = tag.upper()
            for reg in allowed_regions:
                aliases = TAG_ALIASES_LIST.get(reg, [])
                if any(alias.upper() in tag_upper for alias in aliases):
                    found_regions.add(reg)
                    break
        
        missed_regions = found_regions - set(parsed_regions)
        if missed_regions:
            log_msg("WARN", "PARSE", f"Пропущены регионы {missed_regions}. Теги: {all_legit_tags}")
            log_msg("WARN", "AI_FULL", f"Full response:\n{result[:1000]}") # 🔥 ФИКС 3: Полный дамп при потере

    if not found_any_tags:
        log_msg("WARN", "PARSE", f"ИИ вернул текст без валидных тегов (Скип).")
        log_msg("WARN", "AI_FULL", f"Full response:\n{result[:1000]}")

    if state_changed:
        async with state_lock: _pending_snapshot = dict(ACTIVE_ALERTS)
        _state_dirty.set()

async def main():
    global MAIN_LOOP, ADMIN_QUEUE, ACTIVE_ALERTS, state_lock, _state_dirty
    state_lock = asyncio.Lock()
    _state_dirty = asyncio.Event()
    MAIN_LOOP = asyncio.get_running_loop()
    ADMIN_QUEUE = asyncio.Queue(maxsize=200)
    ACTIVE_ALERTS = await MAIN_LOOP.run_in_executor(None, _load_alerts_sync)
    await app.start()
    notifier_task = asyncio.create_task(admin_notifier_worker())
    heartbeat_task = asyncio.create_task(heartbeat_worker())
    state_saver_task = asyncio.create_task(state_saver_worker())
    async for _ in app.get_dialogs(limit=50): pass 
    if ADMIN_CHAT_ID: await MAIN_LOOP.run_in_executor(None, sync_send_admin_message, "✅ <b>Радар (V17.7 Telemetry Unchained) запущен!</b>")
    try: await idle()
    finally:
        if state_saver_task and not state_saver_task.done():
            state_saver_task.cancel()
            try: await asyncio.wait_for(state_saver_task, timeout=5.0)
            except Exception: pass
        if _state_dirty.is_set() and _pending_snapshot is not None:
            _save_alerts_sync(_pending_snapshot)
        tasks = [t for t in (notifier_task, heartbeat_task) if t and not t.done()]
        for t in tasks: t.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10.0)
            for t in pending: t.cancel()
        await app.stop()

if __name__ == "__main__":
    app.run(main())
