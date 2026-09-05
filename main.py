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
model = genai.GenerativeModel('gemini-3.8-flash')

app = Client("radar_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

def send_to_channel(region, text):
    bot_info = BOTS.get(region)
    if not bot_info or not bot_info["token"]:
        return
    
    url = f"https://api.telegram.org/bot{bot_info['token']}/sendMessage"
    channel_link = f"https://t.me/{bot_info['channel'].replace('@', '')}"
    
    # Твоя чистая подпись текстом, без кнопок
    final_text = f"{text}\n\nРадар {bot_info['name']} | <a href='{channel_link}'>Подписаться</a>"
    
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
2. ВАЖНО: Фразы "Все районы", "в направлении", "приготовиться" — это РЕАЛЬНАЯ ТРЕВОГА. Не игнорируй!
3. ОПРЕДЕЛИ РЕГИОН(Ы) и напиши их коды (если угроза нескольким, пиши через запятую):
   - Питер/Ленобласть -> SPB
   - Москва/МО -> MSK
   - Белгородская обл (Белгород, Шебекино, Валуйки и т.д.) -> BELGOROD
   - Курск/Курская обл (Курск, Обоянь и т.д.) -> KURSK
   (Для Орла, Воронежа, Липецка коды не пиши, просто упомяни их в тексте).
4. Напиши суть угрозы коротко и строго.
5. Начни текст СТРОГО с эмодзи (никаких других тегов в начале!):
   🔴 — Ракетная опасность, Тревога БПЛА (летит, фиксация).
   🟡 — Опасность БПЛА (желтый уровень, приготовиться), Авиационная опасность.
   🟢 — Отбой тревоги (любой).

ФОРМАТ ОТВЕТА СТРОГО ТАКОЙ (две строчки):
TARGET: [коды]
MSG: [эмодзи] [текст]
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
    
    print(f"[*] Получено сообщение из {source}. Передаю нейросети...")
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, process_with_ai, text, source)
    
    if "ИГНОР" in result.upper() or not result:
        print("[-] Мусор отфильтрован (ИГНОР).")
        return
        
    targets = []
    clean_text = result
    
    # Парсим ответ от ИИ: забираем регионы в массив, а чистый текст оставляем для канала
    if "TARGET:" in result and "MSG:" in result:
        parts = result.split("MSG:")
        target_part = parts[0].replace("TARGET:", "").strip().upper()
        clean_text = parts[1].strip()
        
        for region in BOTS.keys():
            if region in target_part:
                targets.append(region)
                
    # Если ИИ тупанул и не определил регион, отправляем по дефолту источника
    if not targets:
        if "belgorod" in source.lower(): targets.append("BELGOROD")
        elif "locator" in source.lower(): targets.append("KURSK")
        elif "vrv" in source.lower(): targets.append("SPB")
        else: targets.append("BELGOROD")

    # Рассылаем во все определенные регионы
    for region in targets:
        await loop.run_in_executor(None, send_to_channel, region, clean_text)
        print(f"[+] Успешно отправлено в {region}!")

async def main():
    print("=======================================")
    print("[*] Диспетчер ИИ-Радара УСПЕШНО ЗАПУЩЕН")
    print("=======================================")
    await app.start()
    
    # --- НАЧАЛО ФИКСА ОШИБКИ PEER ID ---
    print("[*] Синхронизирую подписки с сервером Телеграма...")
    try:
        async for dialog in app.get_dialogs():
            pass # Просто пролистываем, чтобы юзербот сохранил всё в базу
        print("[+] Синхронизация каналов прошла успешно!")
    except Exception as e:
        print(f"[-] Небольшая заминка при синхронизации: {e}")
    # --- КОНЕЦ ФИКСА ---

    from pyrogram import idle
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
