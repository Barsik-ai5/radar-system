import os
import asyncio
import requests
import time
import re  # <--- Добавили для жесткой вырезки любых тегов
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

ai_lock = asyncio.Lock()
BOT_START_TIME = time.time()

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
        print(f"Ошибка отправки в {region}: {e}")

def process_with_ai(text, source):
    prompt = f"""
Ты — строгий военный фильтр радара. Проанализируй текст.
Источник: {source}
Текст: {text}

Правила:
1. Если это общие слова, фразы "угроза сохраняется", сборы средств, реклама или пожелания ночи — верни только одно слово: ИГНОР.
2. ВАЖНО: Фразы "тревога", "опасность", "ФПВ", "FPV", "БПЛА", "ракетная", "атака", "ударные группы", "авиационная", "бомбовая", "фиксация", "ударный", "хорнет" — это РЕАЛЬНАЯ ТРЕВОГА. Перепиши суть коротко, используя эмодзи 🔴, 🟡, 🟢.
3. ОПРЕДЕЛИ РЕГИОН. Мы отслеживаем ТОЛЬКО эти:
   - Питер/Ленобласть -> [SPB]
   - Москва/МО -> [MSK]
   - Белгородская обл -> [BELGOROD]
   - Курск/Курская обл -> [KURSK]
   🚨 ВАЖНО: ЕСЛИ РЕГИОН ДРУГОЙ (например: Брянск, Воронеж, Ростов, Орел, Тула и т.д.) — строго верни ТОЛЬКО слово ИГНОР. Не придумывай новые теги!
4. Верни ТОЛЬКО тег(и) и готовый текст.
"""
    for attempt in range(3):
        try:
            response = model.generate_content(prompt, safety_settings=SAFETY_SETTINGS)
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "quota" in error_msg.lower():
                print(f"[-] Гугл просит подождать (лимит 429). Сплю 35 секунд... (Попытка {attempt + 1}/3)")
                time.sleep(35)
            elif "safety" in error_msg.lower():
                print(f"[-] БЛОКИРОВКА ЦЕНЗУРЫ ГУГЛА! Ошибка: {e}")
                return "ИГНОР"
            else:
                print(f"[-] Ошибка ИИ: {e}")
                return "ИГНОР"
    return "ИГНОР"

@app.on_message()
async def radar_handler(client, message):
    text = message.text or message.caption or ""
    if not text:
        return 

    if time.time() - BOT_START_TIME < 45:
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

    source_name = message.chat.username or message.chat.title or "Unknown"
    print(f"[*] ПОЙМАЛ СООБЩЕНИЕ ИЗ {source_name}. Жду очередь...")
    
    async with ai_lock:
        print(f"[*] Отправляю в нейросеть: {source_name}...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, process_with_ai, text, source_name)
    
    if "ИГНОР" in result.upper():
        print(f"[-] Мусор отфильтрован из {source_name} (ИГНОР).")
        return
        
    # --- ИСПРАВЛЕННАЯ МАРШРУТИЗАЦИЯ ---
    force_send = False
    if "locator" in source_lower:
        allowed_regions = ["KURSK"]
        # Локатор пишет про ВСЮ Россию, поэтому ТРЕБУЕМ наличие тега [KURSK]
    elif "vrv" in source_lower:
        allowed_regions = ["SPB", "MSK"]
    else:
        allowed_regions = ["BELGOROD"]
        force_send = True # Радар Белгорода пишет только про себя, ему можно без тега

    # Бронебойная зачистка ЛЮБЫХ тегов в квадратных скобках (типа [BRYANSK])
    clean_text = re.sub(r'\[.*?\]\s*', '', result).strip()

    for region in allowed_regions:
        if force_send or f"[{region}]" in result:
            await loop.run_in_executor(None, send_to_channel, region, clean_text)
            print(f"[+] Успешно отправлено в {region}!")

async def main():
    print("=======================================")
    print("[*] Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН")
    print("=======================================")
    await app.start()
    
    try:
        async for dialog in app.get_dialogs():
            pass 
        print("[+] Синхронизация завершена.")
    except Exception:
        pass 

    from pyrogram import idle
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
