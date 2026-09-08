import asyncio
import requests
import time
from pyrogram import Client
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

API_ID = 36567125
API_HASH = "74f27c0240ce52057f170f7b119d74f3"

# Получаем секреты из настроек сервера
SESSION_STRING = os.getenv("SESSION_STRING")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BOTS = {
    "SPB": {"token": os.getenv("BOT_SPB"), "channel": "@RadarLO_SPB", "name": "Питер и Ленинградская область"},
    "MSK": {"token": os.getenv("BOT_MSK"), "channel": "@Radar_MSK_OBL", "name": "Москва и область"},
    "BELGOROD": {"token": os.getenv("BOT_BELGOROD"), "channel": "@Radar_Belgorod_Obl", "name": "Белгород и область"},
    "KURSK": {"token": os.getenv("BOT_KURSK"), "channel": "@Radar_Kursk", "name": "Курск и область"}
}

TARGET_SOURCES = ["vrv_radar", "radar_ru_belgorod", "locatorru"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.8-flash')

app = Client("radar_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

BOT_START_TIME = time.time()

def send_to_channel(region, text):
    bot_info = BOTS.get(region)
    if not bot_info or not bot_info["token"]:
        return
    
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    
    final_text = f"{text}\n\n📍 Радар {bot_info['name']} | <a href='{channel_link}'>Подписаться</a>"
    
    payload = {
        "chat_id": bot_info['channel'],
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Ошибка отправки в {region}: {e}", flush=True)

def process_with_ai(text, source):
    prompt = f"""
Ты — строгий военный фильтр радара. Проанализируй текст.
Источник: {source}
Текст: {text}

Правила:
1. Если это общие слова, фразы "угроза сохраняется", сборы средств, реклама, пожелания ночи, "учения", "проверка сирен", "плановые работы", "уничтожение боеприпасов" — верни только одно слово: ИГНОР.
2. ЭМОДЗИ (ВЫБЕРИ СТРОГО ОДИН):
   🔴 — Ракетная опасность, прямая атака, фиксация/сбитие, Пуски с авиации, Авиационная атака, Обстрел, РСЗО, баллистика, работа ПВО.
   🟡 — Опасность БПЛА (желтый уровень, движение БПЛА, приготовиться, внимание), Авиационная опасность (без пусков).
   🟢 — Отбой тревоги (отбой ракетной, отбой БПЛА, чисто).
3. ТЕГ РЕГИОНА (ОБЯЗАТЕЛЬНО СТАВЬ В НАЧАЛО):
   - Питер/Ленобласть -> [SPB]
   - Москва/МО -> [MSK]
   - Белгородская обл (Валуйки, Шебекино и т.д.) -> [BELGOROD]
   - Курск/Курская обл -> [KURSK]
4. ФОРМАТ ОТВЕТА СТРОГО ТАКОЙ: [ТЕГ] [ЭМОДЗИ] Суть угрозы коротко.
"""
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "quota" in error_msg.lower():
                print(f"[-] Гугл просит подождать (лимит 429). Сплю 35 секунд... (Попытка {attempt + 1}/3)", flush=True)
                time.sleep(35)
            else:
                print(f"[-] Ошибка ИИ: {e}", flush=True)
                return "ИГНОР"
    return "ИГНОР"

@app.on_message()
async def radar_handler(client, message):
    text = message.text or message.caption or ""
    if not text:
        return 

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

    if time.time() - BOT_START_TIME < 7:
        print(f"[-] Отсекли старый кэш из {username or title} при рестарте.", flush=True)
        return

    source_name = message.chat.username or message.chat.title or "Unknown"
    print(f"[*] Получено СВЕЖЕЕ сообщение из {source_name}. Передаю нейросети...", flush=True)
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, process_with_ai, text, source_name)
    
    if "ИГНОР" in result.upper():
        print(f"[-] Мусор отфильтрован из {source_name} (ИГНОР).", flush=True)
        return
        
    # --- ЖЕСТКАЯ МАРШРУТИЗАЦИЯ ---
    if "locator" in source_lower:
        allowed_regions = ["BELGOROD", "KURSK"] # Локатор льет в Белгород и Курск
    elif "vrv" in source_lower:
        allowed_regions = ["SPB", "MSK"]
    else:
        allowed_regions = ["BELGOROD"] 

    clean_text = result
    for r in BOTS.keys():
        clean_text = clean_text.replace(f"[{r}]", "").strip()

    # СТРОГАЯ ОТПРАВКА ПО ТЕГАМ
    for region in allowed_regions:
        if f"[{region}]" in result:
            await loop.run_in_executor(None, send_to_channel, region, clean_text)
            print(f"[+] Успешно отправлено в {region}!", flush=True)

async def main():
    print("=======================================", flush=True)
    print("[*] Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН", flush=True)
    print("=======================================", flush=True)
    await app.start()
    
    print("[*] Синхронизирую подписки с сервером Телеграма...", flush=True)
    try:
        async for dialog in app.get_dialogs():
            pass 
        print("[+] Синхронизация завершена.", flush=True)
    except Exception as e:
        pass 

    from pyrogram import idle
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
    
