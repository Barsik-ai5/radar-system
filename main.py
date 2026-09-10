import os
import asyncio
import requests
import time
import re
from pyrogram import Client
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

API_ID = 36567125
API_HASH = "74f27c0240ce52057f170f7b119d74f3"

SESSION_STRING = os.getenv("SESSION_STRING")

# === 🔑 МУЛЬТИ-КЛЮЧИ (РОТАЦИЯ) ===
# Впиши сюда свои дополнительные ключи. Если оставишь только один, 
# скрипт будет просто спать 65 секунд, как и раньше.
GEMINI_KEYS = [
    os.getenv("GEMINI_API_KEY"),
    "ТВОЙ_ВТОРОЙ_КЛЮЧ_СЮДА",
    "ТВОЙ_ТРЕТИЙ_КЛЮЧ_СЮДА"
]
# Скрипт сам уберет пустые заглушки
GEMINI_KEYS = [k for k in GEMINI_KEYS if k and not k.startswith("ТВОЙ")]

current_key_idx = 0
genai.configure(api_key=GEMINI_KEYS[current_key_idx])

BOTS = {
    "SPB": {"token": os.getenv("BOT_SPB"), "channel": "@RadarLO_SPB", "name": "Питер и Ленинградская область"},
    "MSK": {"token": os.getenv("BOT_MSK"), "channel": "@Radar_MSK_OBL", "name": "Москва и область"},
    "BELGOROD": {"token": os.getenv("BOT_BELGOROD"), "channel": "@Radar_Belgorod_Obl", "name": "Белгород и область"},
    "KURSK": {"token": os.getenv("BOT_KURSK"), "channel": "@Radar_Kursk", "name": "Курск и область"}
}

TARGET_SOURCES = ["vrv_radar", "radar_ru_belgorod", "locatorru"]

# === КЛЮЧЕВЫЕ СЛОВА ДЛЯ ПРЕДФИЛЬТРА ПИТОНА ===
TARGET_KEYWORDS = [
    # 🔴 БЕЛГОРОДСКАЯ ОБЛАСТЬ
    "белгород", "шебекин", "валуй", "грайворон", "оскол", "губкин",
    "волоконов", "борисов", "ивня", "ракитн", "краснояруж", "алексеев",
    "короч", "вейделев", "ровен", "чернян", "прохоров", "строител",
    "октябрьск", "томаров", "тавров", "дубов", "майск", "разумн", "беломестн",
    "стрелецк", "головчин", "бессонов", "красн",

    # 🟡 КУРСКАЯ ОБЛАСТЬ
    "курск", "курчат", "судж", "рыльск", "обоян", "льгов", "коренев",
    "глушков", "беловск", "железногорск", "фатеж", "щигр", "дмитриев",
    "тим", "горшечн", "поныр", "медвен", "золотухин", "мантуров", "солнцев",
    "черемисин", "касторн", "пристен", "прямицын", "теткин",

    # 🟢 МОСКВА И МО
    "москв", "мск", "подмосков", "подольск", "люберц", "королев",
    "химк", "балаших", "мытищ", "красногорск", "одинцов", "домодедов",
    "зеленоград", "раменск", "ступин", "кашир", "коломн", "чехов", "серпухов",

    # 🔵 ПИТЕР И ЛЕНОБЛАСТЬ
    "петербург", "питер", "спб", "ленинград", "ленобласт",
    "выборг", "гатчин", "кронштадт", "луг", "кингисепп", "волхов", "тихвин",
    "всеволожск", "мурин", "кудров", "тосн",

    # ⚠️ ТОЧКИ ЗАПУСКОВ И УГРОЗЫ (УКРАИНА И ГЛОБАЛЬНЫЕ ТРЕВОГИ)
    "днепро", "днепропетровск", "харьков", "сумы", "авиаторск", "волчанск", 
    "полтав", "запуск", "взлет", "вылет", "пуск"
]

# Примечание: Если 1.5-flash когда-нибудь снова выдаст ошибку 404, просто поменяй на 'gemini-pro'
model = genai.GenerativeModel('gemini-3.8-flash')

ai_lock = asyncio.Lock()

SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
]

app = Client("radar_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

def send_to_channel(region, text):
    bot_info = BOTS.get(region)
    if not bot_info or not bot_info["token"]:
        return
    
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    
    # Обновил подпись на бренд "Дозор.ру"
    final_text = f"{text}\n\n📍 Дозор.ру | {bot_info['name']} - <a href='{channel_link}'>Подписаться</a>"
    
    payload = {
        "chat_id": bot_info['channel'],
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Ошибка отправки в {region}: {e}")

def process_with_ai(text, source):
    global current_key_idx, model # Нужно для переключения ключа и модели
    
    prompt = f"""
Ты — строгий военный редактор радара "Дозор.ру". Твоя задача — проанализировать текст и сделать жесткий РЕРАЙТ по системе "СВЕТОФОР".
Источник: {source}
Текст: {text}

🚨 ПРАВИЛО СВЕТОФОРА (ОБЯЗАТЕЛЬНО начни переписанный текст с одного из этих эмодзи):
🔴 [КРАСНЫЙ] - Прямая угроза или атака! Ракетная опасность, летят БПЛА/FPV, прилеты, взрывы, работает ПВО, запуски ракет со стороны Украины.
🟡 [ЖЕЛТЫЙ] - Предупреждение! Взлет вражеской авиации, активность разведчиков, фиксация движения в сторону региона, объявление опасности (до прилетов).
🟢 [ЗЕЛЕНЫЙ] - Отбой! Отбой ракетной опасности, отбой БПЛА, небо чистое, угроза миновала.
⚪️ [БЕЛЫЙ] - Инфо/Сводка! Последствия атак (как отчеты Оперштаба), повреждения зданий/авто, раненые. То есть факты, когда ракеты на голову прямо сейчас не падают.

⚙️ ПРАВИЛА РАБОТЫ И ФИЛЬТРАЦИИ:
1. МУСОР = ИГНОР. Если это реклама, сборы денег, "доброе утро/спокойной ночи", новости политики — верни только одно слово: ИГНОР.
2. ОПРЕДЕЛИ РЕГИОН. Мы работаем только по этим: [SPB], [MSK], [BELGOROD], [KURSK]. Если новость про Брянск, Воронеж, Ростов и т.д. — строго верни ИГНОР (исключение: взлеты/запуски с Украины публикуем всегда с 🔴).
3. ⚡️ СТИЛЬ РЕРАЙТА. Перепиши исходный текст сухо, коротко и по-военному. Убери воду, эмоции и кучу лишних эмодзи источника. Оставь только суть: где, что летит, какие последствия.
4. ФОРМАТ ОТВЕТА. Верни ТОЛЬКО тег региона и твой переписанный текст. Никаких твоих комментариев!

Пример идеального ответа:
[BELGOROD]
⚪️ Шебекино и Грайворон: последствия атак БПЛА. Повреждены автомобили и частные дома, двое пострадавших переданы медикам.
"""
    for attempt in range(3):
        try:
            response = model.generate_content(prompt, safety_settings=SAFETY_SETTINGS)
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            print(f"❗️ ДЕТАЛЬНАЯ ОШИБКА ГУГЛА: {repr(e)}") # Выводим чистую ошибку в консоль
            
            if "429" in error_msg or "quota" in error_msg.lower():
                if len(GEMINI_KEYS) > 1:
                    current_key_idx = (current_key_idx + 1) % len(GEMINI_KEYS)
                    print(f"[*] Переключаюсь на резервный API-ключ №{current_key_idx + 1}...")
                    genai.configure(api_key=GEMINI_KEYS[current_key_idx])
                    model = genai.GenerativeModel('gemini-3.8-flash') # Перезапускаем модель с новым ключом
                    time.sleep(1)
                else:
                    print(f"[-] Гугл просит подождать (лимит 429). Сплю 65 секунд... (Попытка {attempt + 1}/3)")
                    time.sleep(65)
            elif "safety" in error_msg.lower():
                print(f"[-] БЛОКИРОВКА ЦЕНЗУРЫ ГУГЛА! Ошибка: {e}")
                return "ИГНОР"
            else:
                print(f"[-] Ошибка ИИ: {e}")
                return "ИГНОР"
    return "ИГНОР"

@app.on_message()
async def radar_handler(client, message):
    if not message.chat:
        return

    username = (message.chat.username or "").lower()
    title = (message.chat.title or "").lower()
    source_lower = username + " " + title

    is_our_target = False
    for target in TARGET_SOURCES:
        if target in source_lower:
            is_our_target = True
            break
            
    if not is_our_target:
        return 

    source_name = message.chat.username or message.chat.title or "Unknown"
    print(f"[*] Засёк активность в радаре: {source_name}. Проверяю...")

    text = message.text or message.caption or ""
    if not text:
        return 

    if message.date:
        age = time.time() - message.date.timestamp()
        if age > 7200:
            print(f"[-] Сообщение слишком старое (возраст: {int(age)} сек). Молча удаляю.")
            return

    # === ПРЕДФИЛЬТР ПИТОНА ===
    if "locator" in source_lower or "vrv" in source_lower:
        text_lower = text.lower()
        has_our_region = False
        for kw in TARGET_KEYWORDS:
            if kw in text_lower:
                has_our_region = True
                break
                
        if not has_our_region:
            print(f"[-] Предфильтр: В посте из {source_name} чужой регион. Скип, бережем лимит!")
            return 

    print(f"[*] Сообщение прошло предфильтр. Встало в очередь к ИИ...")
    
    async with ai_lock:
        await asyncio.sleep(4.5)
        print(f"[*] Отправляю в нейросеть: {source_name}...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, process_with_ai, text, source_name)
    
    if "ИГНОР" in result.upper():
        print(f"[-] Мусор отфильтрован из {source_name} (ИГНОР).")
        return
        
    force_send = False
    if "locator" in source_lower:
        allowed_regions = ["KURSK"]
    elif "vrv" in source_lower:
        allowed_regions = ["SPB", "MSK"]
    else:
        allowed_regions = ["BELGOROD"]
        force_send = True 

    clean_text = re.sub(r'\[.*?\]\s*', '', result).strip()

    for region in allowed_regions:
        if force_send or f"[{region}]" in result:
            await loop.run_in_executor(None, send_to_channel, region, clean_text)
            print(f"[+] Успешно отправлено в {region}!")

async def main():
    print("=======================================")
    print("[*] Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН")
    print(f"[*] Доступно API ключей: {len(GEMINI_KEYS)}")
    print("=======================================")
    await app.start()
    
    try:
        async for dialog in app.get_dialogs():
            pass 
        print("[+] Синхронизация завершена. Жду сообщений...")
    except Exception:
        pass 

    from pyrogram import idle
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
