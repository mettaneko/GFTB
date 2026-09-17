import os
import json
import io
import asyncio
import time
import re
from fastapi import FastAPI, Request
from upstash_redis import Redis
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from google import genai
from google.genai import types as genai_types

app = FastAPI()
__all__ = ["app"]

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

EMOJI_SHAKE = '<tg-emoji emoji-id="4949806429746758578">🤝</tg-emoji>'
EMOJI_THINK = '<tg-emoji emoji-id="4942915489727775648">🤔</tg-emoji>'
EMOJI_B_HEART = '<tg-emoji emoji-id="4949561414747423396">💔</tg-emoji>'

MODEL_NAME = "gemini-3.6-flash"

ALLOWED_USERS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USERS", "").split(",")
    if uid.strip().isdigit()
}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
ai = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if (UPSTASH_URL and UPSTASH_TOKEN) else None


def check_access(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


@dp.message(CommandStart())
async def start(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("⛔ Доступ закрыт. Этот бот настроен только для семьи.")
    text = (
        f"👋 {EMOJI_SHAKE} <b>Привет! Я семейный ИИ-ассистент.</b>\n\n"
        f"• <b>Текстовый вопрос</b> — вывод в реальном времени\n"
        f"• <b>Фото с описанием</b> — анализ изображения\n"
        f"• <code>/image &lt;описание&gt;</code> — генерация картинки\n"
        f"• <code>/video &lt;описание&gt;</code> — генерация видео (через очередь)"
    )
    await m.answer(text, parse_mode=ParseMode.HTML)

def clean_markdown_for_telegram(text: str) -> str:
    """Очищает и адаптирует форматирование нейросети под Telegram HTML."""

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def code_block_sub(match):
        lang = match.group(1) or ""
        code = match.group(2)
        return f'<pre><code class="language-{lang}">{code}</code></pre>'
    
    text = re.sub(r'```(\w+)?\n?(.*?)```', code_block_sub, text, flags=re.DOTALL)

    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)

    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*([^*]+)\*', r'<i>\1</i>', text)

    text = re.sub(r'^#{1,6}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    return text


@dp.message(F.text & ~F.text.startswith("/"))
async def chat_stream(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("💔 {EMOJI_B_HEART} Доступ ограничен.")

    await bot.send_chat_action(m.chat.id, "typing")
    sent_message = await m.answer(f"{EMOJI_THINK} <i>Думаю...</i>", parse_mode=ParseMode.HTML)

    full_text = ""
    last_edit_time = time.time()
    edit_delay = 0.8

    system_instruction = (
        "Ты помощник в Telegram. Форматируй текст аккуратно: "
        "для выделения используй **жирный** или *курсив*, "
        "для блоков кода обязательно используй тройные кавычки ```язык с кодом внутри. "
        "Не используй тройные звёздочки (***)."
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response_stream = ai.models.generate_content_stream(
                model=MODEL_NAME,
                contents=m.text,
                config=dict(
                    system_instruction=system_instruction
                )
            )

            for chunk in response_stream:
                if chunk.text:
                    full_text += chunk.text
                    current_time = time.time()

                    if current_time - last_edit_time > edit_delay:
                        try:
                            await sent_message.edit_text(full_text + " ▌", parse_mode=None)
                            last_edit_time = current_time
                        except Exception:
                            pass

            if full_text.strip():
                formatted_html = clean_markdown_for_telegram(full_text)
                try:
                    await sent_message.edit_text(formatted_html, parse_mode=ParseMode.HTML)
                except Exception:
                    await sent_message.edit_text(full_text, parse_mode=None)
            break

        except Exception as e:
            if "503" in str(e) and attempt < max_retries - 1:
                await asyncio.sleep(2)
                continue
            await sent_message.edit_text("💔 {EMOJI_B_HEART}",f" Ошибка генерации: {e}", parse_mode=None)
            break


# Анализ фото с описанием
@dp.message(F.photo)
async def photo_edit(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("💔 {EMOJI_B_HEART} Доступ ограничен.")

    prompt = m.caption or "Опиши подробно, что изображено на картинке."
    await bot.send_chat_action(m.chat.id, "typing")
    status_msg = await m.reply("🤔 {EMOJI_THINK} Анализирую фото...")

    try:
        file_info = await bot.get_file(m.photo[-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)

        res = ai.models.generate_content(
            model=MODEL_NAME,
            contents=[
                genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
                prompt
            ]
        )
        try:
            await status_msg.edit_text(res.text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await status_msg.edit_text(res.text, parse_mode=None)
    except Exception as e:
        await status_msg.edit_text("💔 {EMOJI_B_HEART}",f" Ошибка: {e}", parse_mode=None)


@dp.message(Command("image"))
async def gen_image(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("💔 {EMOJI_B_HEART} Доступ ограничен.")

    prompt = m.text.replace("/image", "").strip()
    if not prompt:
        return await m.answer("⚠️ Укажите описание картинки:\n`/image кот в очках`")

    msg = await m.answer("🤔 {EMOJI_THINK} Генерирую изображение...")
    try:
        res = ai.models.generate_images(model="imagen-3.0-generate-002", prompt=prompt)
        photo = BufferedInputFile(res.generated_images[0].image.image_bytes, filename="art.jpg")
        await m.answer_photo(photo=photo, caption=f"Prompt: {prompt}")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}", parse_mode=None)


@dp.message(Command("video"))
async def queue_video(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer("💔 {EMOJI_B_HEART} Доступ ограничен.")

    prompt = m.text.replace("/video", "").strip()
    if not prompt:
        return await m.answer("⚠️ Укажите описание видео:\n`/video закат в горах`")

    if not redis:
        return await m.answer("⚠️ Сервер очереди не настроен.")

    task = {"chat_id": m.chat.id, "user_id": m.from_user.id, "prompt": prompt}
    redis.rpush("video_queue", json.dumps(task))
    await m.answer("⏳ Задача на видео добавлена в очередь.")


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
