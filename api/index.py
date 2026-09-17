import os
import json
import io
from fastapi import FastAPI, Request
from upstash_redis import Redis
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile
from google import genai
from google.genai import types as genai_types
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

raw_allowed = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = {
    int(uid.strip()) 
    for uid in raw_allowed.split(",") 
    if uid.strip().isdigit()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()
ai = genai.Client(api_key=GEMINI_KEY)

redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)


def check_access(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


@dp.message(CommandStart())
async def start(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("⛔ Доступ закрыт. Этот бот предназначен исключительно для семьи.")
    
    await m.answer(
        "👋 Привет! Я семейный ИИ-ассистент.\n\n"
        "• Текстовый вопрос — ответ сразу.\n"
        "• Фото с описанием — анализ и модификация исходника.\n"
        "• /image <описание> — генерация изображения.\n"
        "• /video <описание> — генерация видео (через очередь)."
    )


# Текстовые диалоги
@dp.message(F.text & ~F.text.startswith("/"))
async def chat(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("⛔ Доступ ограничен.")
    
    await bot.send_chat_action(m.chat.id, "typing")
    try:
        res = ai.models.generate_content(model="gemini-2.5-flash", contents=m.text)
        await m.answer(res.text)
    except Exception as e:
        await m.answer(f"❌ Ошибка генерации: {e}")

@dp.message(F.photo)
async def photo_edit(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("⛔ Доступ ограничен.")
    
    prompt = m.caption or "Опиши подробно, что изображено на картинке."
    await bot.send_chat_action(m.chat.id, "typing")
    
    try:
        file_info = await bot.get_file(m.photo[-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        
        res = ai.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"), 
                prompt
            ]
        )
        await m.reply(res.text)
    except Exception as e:
        await m.reply(f"❌ Ошибка обработки фото: {e}")


# Генерация картинок (Imagen 3)
@dp.message(Command("image"))
async def gen_image(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("⛔ Доступ ограничен.")
    
    prompt = m.text.replace("/image", "").strip()
    if not prompt:
        return await m.answer("⚠️ Укажите описание картинки, например:\n`/image кот в очках на пляже`")
    
    msg = await m.answer("🎨 Генерирую изображение...")
    try:
        res = ai.models.generate_images(model="imagen-3.0-generate-002", prompt=prompt)
        photo = BufferedInputFile(res.generated_images[0].image.image_bytes, filename="art.jpg")
        await m.answer_photo(photo=photo, caption=prompt)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

@dp.message(Command("video"))
async def queue_video(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("⛔ Доступ ограничен.")
    
    prompt = m.text.replace("/video", "").strip()
    if not prompt:
        return await m.answer("⚠️ Укажите описание видео, например:\n`/video водопад в лесу на закате`")

    task = {
        "chat_id": m.chat.id,
        "user_id": m.from_user.id,
        "prompt": prompt
    }
    
    redis.rpush("video_queue", json.dumps(task))
    await m.answer("⏳ Задача поставлена в очередь генерации. Как только видео будет готово, бот пришлет его сюда!")



@app.post("/")
@app.post("/api/index")
@app.post("/api/index.py")
async def webhook(req: Request):
    upd = types.Update(**(await req.json()))
    await dp.feed_update(bot, upd)
    return {"status": "ok"}

@app.get("/")
@app.get("/api/index")
@app.get("/api/index.py")
async def health():
    return {"status": "alive"}
