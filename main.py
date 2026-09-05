import os
import asyncio
import requests
from pyrogram import Client, filters
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

API_ID = 36567125
API_HASH = "74f27c0240ce52057f170f7b119d74f3"

# Получаем секреты из настроек сервера (чтобы никто их не украл)
SESSION_STRING = os.getenv("SESSION_STRING")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BOTS = {
    "SPB": {"token": os.getenv("BOT_SPB"), "channel": "@RadarLO_SPB", "name": "Питер и Ленинградская область"},
    "MSK": {"token": os.getenv("BOT_MSK"), "channel": "@Radar_MSK_OBL", "name": "Москва и область"},
    "BELGOROD": {"token": os.getenv("BOT_BELGOROD"), "channel": "@Radar_Belgorod_Obl", "name": "Белгород и область"},
    "KURSK": {"token": os.getenv("BOT_KURSK"), "channel": "@Radar_Kursk", "name": "Курск и область"}
}

SOURCES = ["@vrv_radar", "@radar_ru_belgorod", "@locatorru"]

genai.configure(api_key=GEMINI_API_KEY)
# Используем flash для молниеносной скорости реакции
model = genai.GenerativeModel('gemini-1.5-flash-latest')

app = Client("radar_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

def send_to_channel(region, text):
    bot_info = BOTS.get(region)
    if not bot_info or not bot_info["token"]:
        return
    
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    
    payload = {
        "chat_id": bot_info['channel'],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": f"📍 Радар {bot_info['name']} | Подписаться", "url": channel_link}]]
        }
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
2. Если это реальная угроза (летит, фиксация, опасность) или отбой — перепиши суть коротко и строго, используя эмодзи 🔴, 🟡, 🟢.
3. ОПРЕДЕЛИ РЕГИОН и напиши его тег в самом начале:
   - Если Питер/Ленобласть -> [SPB]
   - Если Москва/МО -> [MSK]
   - Если Белгородская обл (Белгород, Шебекино, Валуйки, Строитель, Майский и т.д.) -> [BELGOROD]
   - Если Курск/Курская обл -> [KURSK]
4. Верни ТОЛЬКО тег и готовый текст. Никаких других слов и комментариев.
"""
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Ошибка ИИ: {e}")
        return "ИГНОР"

@app.on_message(filters.chat(SOURCES))
async def radar_handler(client, message):
    # Берем только текст, игнорируем тяжелые медиа
    text = message.text or message.caption or ""
    if not text:
        return 

    source = message.chat.username or message.chat.title or ""
    
    # Жесткий фильтр для Локатора: берем ТОЛЬКО Курск
    if "locator" in source.lower() and "курск" not in text.lower():
        return 

    print(f"[*] Получено сообщение из {source}. Передаю нейросети...")
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, process_with_ai, text, source)
    
    if "ИГНОР" in result.upper():
        print("[-] Мусор отфильтрован (ИГНОР).")
        return
        
    for region in BOTS.keys():
        if f"[{region}]" in result:
            clean_text = result.replace(f"[{region}]", "").strip()
            await loop.run_in_executor(None, send_to_channel, region, clean_text)
            print(f"[+] Успешно отправлено в {region}!")
            break

async def main():
    print("=======================================")
    print("[*] Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН")
    print("=======================================")
    await app.start()
    from pyrogram import idle
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
