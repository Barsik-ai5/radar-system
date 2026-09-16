import os
import asyncio
import requests
import time
import re
import json
from datetime import datetime
from html import escape
from pyrogram import Client, idle

# Актуальный SDK Google
from google import genai
from google.genai import types
from google.genai.errors import APIError

from dotenv import load_dotenv

load_dotenv()

# === ПРОФЕССИОНАЛЬНОЕ ЛОГИРОВАНИЕ ===
def log_msg(level, tag, msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [{level}] [{tag}] {msg}")

# === ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not API_ID_RAW or not API_HASH or not SESSION_STRING or API_HASH == "ТВОЙ_API_HASH_ИЗ_ENV":
    raise RuntimeError("[-] ОШИБКА СТАРТА: Не заданы API_ID, API_HASH или SESSION_STRING в файле .env!")

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise RuntimeError("[-] ОШИБКА СТАРТА: API_ID должен быть числом!")

# === 🔑 ИНИЦИАЛИЗАЦИЯ GEMINI ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("[-] ОШИБКА СТАРТА: Не задан GEMINI_API_KEY в файле .env!")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

AI_CONFIG = types.GenerateContentConfig(
    safety_settings=[
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ],
    max_output_tokens=250, 
    temperature=0.1        
)

# === ⚙️ ЕДИНАЯ КОНФИГУРАЦИЯ МАРШРУТИЗАЦИИ ===
BOTS_CONFIG = {
    "SPB": {"token": os.getenv("BOT_SPB"), "channel": "@RadarLO_SPB", "name": "Питер и Ленинградская область"},
    "MSK": {"token": os.getenv("BOT_MSK"), "channel": "@Radar_MSK_OBL", "name": "Москва и Московская область"},
    "BELGOROD": {"token": os.getenv("BOT_BELGOROD"), "channel": "@Radar_Belgorod_Obl", "name": "Белгород и Белгородская область"},
    "KURSK": {"token": os.getenv("BOT_KURSK"), "channel": "@Radar_Kursk", "name": "Курск и Курская область"}
}

SOURCE_ROUTING = {
    "locatorru": ["KURSK"],
    "vrv_radar": ["SPB", "MSK"],
    "radar_ru_belgorod": ["BELGOROD"]
}

# === ЗДОРОВЬЕ БОТОВ ПРИ СТАРТЕ ===
for region, info in BOTS_CONFIG.items():
    if not info["token"]:
        raise RuntimeError(f"[-] ОШИБКА СТАРТА: Не задан BOT-токен для региона {region}!")
    
    url = f"https://api.telegram.org/bot{info['token']}/getChat?chat_id={info['channel']}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"[-] ОШИБКА СТАРТА: Бот {region} не имеет доступа к каналу {info['channel']}. Ответ Telegram: {resp.text}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"[-] ОШИБКА СТАРТА: Сетевая ошибка при проверке бота {region}: {e}")

# === АТОМАРНОЕ СОСТОЯНИЕ АКТИВНЫХ ТРЕВОГ ===
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts_state.json")
state_lock = asyncio.Lock()

def load_alerts_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Поврежден файл состояния: {e}. Создаем бэкап.")
            try:
                os.replace(STATE_FILE, STATE_FILE + ".corrupted")
            except:
                pass
    return {} 

LAST_ALERT_IDS = load_alerts_state()
PROCESSED_MESSAGES = {} 

# === КЛЮЧЕВЫЕ СЛОВА ДЛЯ ПРЕДФИЛЬТРА И ЗАЩИТЫ ===
TARGET_KEYWORDS = [
    "белгород", "шебекин", "валуй", "грайворон", "оскол", "губкин", "волоконов", "борисов", 
    "ивня", "ракитн", "краснояруж", "алексеев", "короч", "вейделев", "ровен", "чернян", 
    "прохоров", "строител", "октябрьск", "томаров", "тавров", "дубов", "майск", "разумн", 
    "беломестн", "стрелецк", "головчин", "бессонов", "красн",
    "курск", "курчат", "судж", "рыльск", "обоян", "льгов", "коренев", "глушков", "беловск", 
    "железногорск", "фатеж", "щигр", "дмитриев", "тим", "горшечн", "поныр", "медвен", 
    "золотухин", "мантуров", "солнцев", "черемисин", "касторн", "пристен", "прямицын", "теткин",
    "москв", "мск", "подмосков", "подольск", "люберц", "королев", "химк", "балаших", "мытищ", 
    "красногорск", "одинцов", "домодедов", "зеленоград", "раменск", "ступин", "кашир", "коломн", 
    "чехов", "серпухов",
    "петербург", "питер", "спб", "ленинград", "ленобласт", "выборг", "гатчин", "кронштадт", 
    "луг", "кингисепп", "волхов", "тихвин", "всеволожск", "мурин", "кудров", "тосн",
    "днепро", "днепропетровск", "харьков", "сумы", "авиаторск", "волчанск", "полтав", "отбой"
]

# 🟡 ФИКС №5: Расширен список чужих регионов
FOREIGN_REGIONS = [
    "воронеж", "брянск", "орел", "орлов", "липецк", "тул", "калуж", 
    "смоленск", "рязан", "твер", "ростов", "краснодар", "крым",
    "тамбов", "волгоград", "астрахан", "ставрополь", "саратов",
    "донецк", "луганск", "запорож", "херсон"
]

ai_lock = asyncio.Lock()
app = Client("radar_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

def sync_send_to_channel(region, text, reply_to=None, is_clear=False):
    bot_info = BOTS_CONFIG.get(region)
    if not bot_info:
        return None
    
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    
    safe_text = escape(text[:3800])
    
    final_text = (
        f"{safe_text}\n\n"
        f"📡 Дозор.ру | {bot_info['name']} - <a href='{channel_link}'>Подписаться</a>"
    )
    
    payload = {
        "chat_id": bot_info['channel'],
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    if reply_to:
        payload["reply_parameters"] = {
            "message_id": reply_to,
            "allow_sending_without_reply": not is_clear 
        }

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    return data["result"]["message_id"]
                    
            elif response.status_code == 429:
                try:
                    retry_after = response.json().get("parameters", {}).get("retry_after", 2)
                except Exception:
                    retry_after = 2
                log_msg("WARN", "TG", f"Лимит отправки 429 в {region}. Ждем {retry_after} сек...")
                time.sleep(retry_after)
                continue
                
            elif response.status_code in [400, 401, 403, 404]:
                log_msg("ERROR", "TG", f"Фатальная ошибка {response.status_code} для {region}. Без повтора. Ответ: {response.text}")
                break 
                
            else:
                log_msg("WARN", "TG", f"Ошибка {response.status_code} в {region}. Ответ: {response.text}")
                time.sleep(2)
                
        except requests.exceptions.ReadTimeout:
            log_msg("ERROR", "TG", f"Timeout отправки в {region}. Сообщение могло дойти, отменяем retry во избежание дубля.")
            break 
        except Exception as e:
            log_msg("ERROR", "TG", f"Сетевая ошибка при отправке в {region}: {e}")
            time.sleep(2)
            
    return None

def extract_region_text(result, region):
    pattern = rf"\[{region}\]\s*(.*?)(?=\n\[(?:SPB|MSK|BELGOROD|KURSK)\]|\Z)"
    match = re.search(pattern, result, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()

async def process_with_ai(text, source):
    text = text[:10000] 
    
    prompt = f"""
Ты — строгий военный фильтр радара. Проанализируй текст.
Источник: {source}
Текст: {text}

ПОРЯДОК ПРИОРИТЕТОВ (ВАЖНО!):
1. ОТБОЙ ТРЕВОГИ
2. МУСОР (ИГНОР)
3. АКТИВНАЯ УГРОЗА (КРАСНЫЙ/ЖЕЛТЫЙ)
4. ПОСТФАКТУМ (БЕЛЫЙ)
5. ОПРЕДЕЛЕНИЕ РЕГИОНА

Правила:
1. 🚨 КРИТИЧЕСКИЙ ПРИОРИТЕТ (ОТБОЙ): 
   Если сообщение является reply и содержит "Текущее обновление", то цвет и смысл определяй ИСКЛЮЧИТЕЛЬНО по "Текущему обновлению".
   Если "Текущее обновление" означает отмену/отбой угрозы, результат ОБЯЗАТЕЛЬНО должен быть 🟢 Отбой.
   Исходное сообщение используется только для понимания того, какая именно тревога была отменена, но НЕ для определения текущего цвета.
   ВАЖНО: Если Текущее обновление означает Отбой, правила МУСОР к Исходному сообщению НЕ ПРИМЕНЯЮТСЯ!
   ОТБОЙ ИМЕЕТ ПРИОРИТЕТ НАД ПРАВИЛОМ ОТСУТСТВИЯ РЕГИОНА. Если это отбой без названия региона, всё равно верни 🟢 Отбой.

2. МУСОР = ИГНОР. СТРОГО: Если в тексте есть фразы "угроза сохраняется", "опасность сохраняется", сборы средств, реклама или пожелания ночи — верни ТОЛЬКО слово ИГНОР.
3. СВЕТОФОР ЭМОДЗИ (ВЫДАЙ СТРОГО ОДИН ЦВЕТ НА РЕГИОН):
   🔴 [КРАСНЫЙ] — Прямая угроза: Ракетная/авиационная опасность, баллистика, обстрел РСЗО, атака FPV, УАБ, ТРЕВОГА по БПЛА, фиксация БПЛА, ДВИЖЕНИЕ БПЛА, РАБОТА ПВО, внимание по падающим осколкам/обломкам. ВАЖНО: Если ударные БПЛА обнаружены, движутся, летят или представляют текущую угрозу — это СТРОГО 🔴.
   ⚪️ [БЕЛЫЙ] — Инфо (постфактум): Последствия атак, разрушения на земле. Если сообщается только о уже сбитых/уничтоженных целях (даже если это Ударные БПЛА) и текущей угрозы нет — это ⚪️.
   🟡 [ЖЕЛТЫЙ] — Предупреждение: ОПАСНОСТЬ ПО БПЛА, внимание по бпла (если не сказано, что они ударные).
   🟢 [ЗЕЛЕНЫЙ] — Отбой: Отбой любой опасности.
4. ОПРЕДЕЛИ РЕГИОН. Мы отслеживаем ТОЛЬКО: Питер/Ленобласть -> [SPB], Москва/МО -> [MSK], Белгородская обл -> [BELGOROD], Курск/Курская обл -> [KURSK].
5. 🚨 ПРАВИЛО МАССОВЫХ ТРЕВОГ (ВЫРЕЗАЙ ЛИШНЕЕ):
   Если в тексте перечислены регионы, ВЫРЕЖИ все чужие! Оставь ТОЛЬКО наш целевой регион.
   Если наших регионов нет вообще (И ЭТО НЕ ОТБОЙ) — верни ИГНОР.
6. ФОРМАТ ОТВЕТА:
   Верни ТОЛЬКО тег региона и текст.

   Один регион:
   [REGION] текст

   Несколько наших регионов:
   каждый регион отдельной строкой.

   Никогда не объединяй несколько регионов под одним тегом.
   Не добавляй пояснений, комментариев или лишнего текста.
"""
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=prompt,
                    config=AI_CONFIG
                ),
                timeout=50.0
            )
            
            if not response.text:
                log_msg("WARN", "AI", "Пустой текстовый ответ от Gemini")
                await asyncio.sleep(2)
                continue
                
            return response.text.strip()
            
        except asyncio.TimeoutError:
            log_msg("WARN", "AI", f"Timeout ответа от Gemini (50s). Попытка {attempt + 1}/3.")
            await asyncio.sleep(2)
            continue
        except APIError as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "quota" in error_msg:
                log_msg("WARN", "AI", f"Лимит 429. Ожидание 65 сек... (Попытка {attempt + 1}/3)")
                await asyncio.sleep(65)
                continue 
            elif "safety" in error_msg:
                log_msg("WARN", "AI", f"Блокировка Safety: {e}")
                return "ИГНОР"
            else:
                log_msg("ERROR", "AI", f"Временная ошибка APIError. Ждем 3 сек... (Попытка {attempt + 1}/3)")
                await asyncio.sleep(3)
                continue
        except Exception as e:
            log_msg("ERROR", "AI", f"Сетевая ошибка при запросе к Gemini: {e}")
            await asyncio.sleep(3)
            continue
                
    log_msg("ERROR", "AI", "Не удалось получить ответ от Gemini после 3 попыток.")
    return None

async def save_alerts_state_async():
    global LAST_ALERT_IDS
    async with state_lock:
        try:
            current_time = time.time()
            keys_to_del = [k for k, v in LAST_ALERT_IDS.items() if current_time - v.get("ts", 0) > 86400]
            for k in keys_to_del:
                LAST_ALERT_IDS.pop(k, None)
                
            tmp_file = STATE_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(LAST_ALERT_IDS, f)
            os.replace(tmp_file, STATE_FILE)
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Ошибка сохранения состояния: {e}")

@app.on_message()
async def radar_handler(client, message):
    if not message.chat:
        return

    username = (message.chat.username or "").lower()
    if username not in SOURCE_ROUTING:
        return 

    source_msg_key = f"{message.chat.id}_{message.id}"
    
    if source_msg_key in PROCESSED_MESSAGES:
        return
    PROCESSED_MESSAGES[source_msg_key] = time.time()
    
    if len(PROCESSED_MESSAGES) > 500:
        keys_to_del = list(PROCESSED_MESSAGES.keys())[:100]
        for k in keys_to_del:
            PROCESSED_MESSAGES.pop(k, None)

    log_msg("INFO", "SYSTEM", f"Новое сообщение в радаре: {username} (ID: {message.id}). Проверяю...")

    text = message.text or message.caption or ""
    if not text:
        return 

    reply_source_key = None

    if message.reply_to_message:
        reply_source_key = f"{message.chat.id}_{message.reply_to_message.id}"
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if reply_text:
            text = f"Исходное сообщение: {reply_text}\n\nТекущее обновление: {text}"

    if message.date:
        age = time.time() - message.date.timestamp()
        if age > 7200 or age < -300: 
            log_msg("INFO", "SYSTEM", f"Сообщение слишком старое/будущее (возраст: {int(age)} сек). Скип.")
            return

    if username in ["locatorru", "vrv_radar"]:
        text_lower = text.lower()
        has_our_region = False
        for kw in TARGET_KEYWORDS:
            if len(kw) <= 4:
                if re.search(rf"\b{kw}", text_lower, re.IGNORECASE):
                    has_our_region = True
                    break
            else:
                if kw in text_lower:
                    has_our_region = True
                    break
                
        if not has_our_region:
            log_msg("INFO", "PREFILTER", f"В посте из {username} нет наших регионов. Скип.")
            return 

    log_msg("INFO", "AI", "Сообщение прошло предфильтр. Встало в очередь к ИИ...")
    
    async with ai_lock:
        await asyncio.sleep(4.5)
        log_msg("INFO", "AI", f"Отправляю в нейросеть (source: {username})...")
        result = await process_with_ai(text, username)
    
    if not result:
        log_msg("WARN", "SYSTEM", "Сообщение пропущено из-за критической ошибки ИИ.")
        return

    if result.strip().upper() == "ИГНОР":
        log_msg("INFO", "AI", f"Мусор отфильтрован (ИГНОР).")
        return
        
    log_msg("INFO", "AI", f"Чистый ответ от ИИ:\n{result}") 

    # 🔴 ФИКС №2: Регистронезависимая очистка тегов из финального текста
    clean_result = re.sub(r'\[(?:SPB|MSK|BELGOROD|KURSK)\]\s*', '', result, flags=re.IGNORECASE).strip()
    
    allowed_regions = SOURCE_ROUTING[username]
    sent_any = False
    state_changed_flag = False

    async def send_and_track(reg, txt, reason):
        nonlocal state_changed_flag
        is_green = "🟢" in txt
        is_red = "🔴" in txt
        is_yellow = "🟡" in txt
        
        reply_to = None
        async with state_lock:
            if is_green and reply_source_key and reply_source_key in LAST_ALERT_IDS:
                reply_to = LAST_ALERT_IDS[reply_source_key].get("regions", {}).get(reg)
                
        # 🔴 ФИКС №1: Если это отбой, но реплая в памяти нет — рубим отправку
        if is_green and reply_to is None:
            log_msg("WARN", "SEND", f"Не найдено сообщение для реплая в {reg}. Отбой не отправляем.")
            return False
        
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
            
        msg_id = await loop.run_in_executor(None, sync_send_to_channel, reg, txt, reply_to, is_green)
        
        if msg_id:
            log_msg("INFO", "SEND", f"Успешно отправлено в {reg} {reason} (MSG_ID: {msg_id})")
            async with state_lock:
                if is_green:
                    if reply_source_key and reply_source_key in LAST_ALERT_IDS:
                        LAST_ALERT_IDS[reply_source_key]["regions"].pop(reg, None)
                        if not LAST_ALERT_IDS[reply_source_key]["regions"]:
                            LAST_ALERT_IDS.pop(reply_source_key, None)
                        state_changed_flag = True
                elif is_red or is_yellow:
                    if source_msg_key not in LAST_ALERT_IDS:
                        LAST_ALERT_IDS[source_msg_key] = {"ts": time.time(), "regions": {}}
                    LAST_ALERT_IDS[source_msg_key]["regions"][reg] = msg_id
                    state_changed_flag = True
            return True
        return False

    if "🟢" in clean_result and reply_source_key:
        async with state_lock:
            saved_regions_dict = LAST_ALERT_IDS.get(reply_source_key, {}).get("regions", {})
            saved_regions = list(saved_regions_dict.keys())
            
        ai_tags = [r for r in ["SPB", "MSK", "BELGOROD", "KURSK"] if re.search(rf"\[{r}\]", result, re.IGNORECASE)]
        
        regions_to_clear = []
        if ai_tags:
            regions_to_clear = [r for r in ai_tags if r in saved_regions]
        else:
            update_text = (message.text or message.caption or "").lower()
            if any(alien in update_text for alien in FOREIGN_REGIONS):
                log_msg("WARN", "SYSTEM", "Отбой направлен на чужой регион. Скип очистки нашей тревоги.")
            else:
                regions_to_clear = saved_regions

        for reg in regions_to_clear:
            if await send_and_track(reg, clean_result, "(умный отбой по памяти)"):
                sent_any = True
                
        if sent_any:
            if state_changed_flag:
                await save_alerts_state_async()
            return 

    for region in allowed_regions:
        region_text = extract_region_text(result, region)
        if region_text:
            if await send_and_track(region, region_text, "(по тегу ИИ)!"):
                sent_any = True

    if not sent_any:
        ai_tagged_any_known = any(re.search(rf"\[{r}\]", result, re.IGNORECASE) for r in ["SPB", "MSK", "BELGOROD", "KURSK"])
        
        if ai_tagged_any_known:
            log_msg("WARN", "SYSTEM", "ИИ указал регион, которого нет в разрешенных для этого источника. Скип Fallback.")
        else:
            if username == "vrv_radar":
                log_msg("WARN", "SYSTEM", "VRV не получил тега от ИИ. Скип Fallback для безопасности (МСК/СПБ).")
            else:
                await send_and_track(allowed_regions[0], clean_result, "(принудительно, ИИ забыл тег)")

    if state_changed_flag:
        await save_alerts_state_async()
            
async def main():
    log_msg("INFO", "SYSTEM", "=======================================")
    log_msg("INFO", "SYSTEM", "Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН")
    log_msg("INFO", "SYSTEM", "=======================================")
    
    await app.start()
    try:
        try:
            async for _ in app.get_dialogs():
                pass 
            log_msg("INFO", "SYSTEM", "Синхронизация завершена. Жду сообщений...")
        except Exception as e:
            log_msg("ERROR", "SYSTEM", f"Ошибка синхронизации диалогов: {e}")

        await idle()
    finally:
        await app.stop()
        log_msg("INFO", "SYSTEM", "Бот безопасно остановлен.")

if __name__ == "__main__":
    app.run(main())
