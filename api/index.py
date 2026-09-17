import os
import json
import io
import asyncio
import time
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
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
HF_TOKEN = os.getenv("HF_TOKEN", "")

EMOJI_SHAKE = '<tg-emoji emoji-id="4949806429746758578">🤝</tg-emoji>'
EMOJI_THINK = '<tg-emoji emoji-id="4942915489727775648">🤔</tg-emoji>'
EMOJI_B_HEART = '<tg-emoji emoji-id="4949561414747423396">💔</tg-emoji>'
EMOJI_SUS = '<tg-emoji emoji-id="4951814692029858673">🤨</tg-emoji>'
EMOJI_OK = '<tg-emoji emoji-id="4947363551133041555">👌</tg-emoji>'

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


def get_system_instruction() -> str:
    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    current_weekday = weekdays[now.weekday()]
    formatted_now = now.strftime(f"%d.%m.%Y, {current_weekday}, %H:%M:%S (МСК)")

    return (
        f"Текущая дата и точное время: {formatted_now}.\n"
        "Ты умный и полезный семейный ассистент в Telegram. Форматируй текст аккуратно: "
        "для выделения используй **жирный** или *курсив*, "
        "для блоков кода обязательно используй тройные кавычки ```язык с кодом внутри. "
        "Никогда не используй тройные звёздочки (***)."
    )


def clean_markdown_for_telegram(text: str) -> str:
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


async def hf_generate_flux(prompt: str) -> bytes | None:
    url = "[https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell](https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell)"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": prompt}

    async with aiohttp.ClientSession() as session:
        for _ in range(3):
            async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
                if resp.status == 200:
                    return await resp.read()
                elif resp.status == 503:
                    await asyncio.sleep(4)
                else:
                    break
    return None


async def hf_edit_image(image_bytes: bytes, instruction: str) -> bytes | None:
    url = "[https://api-inference.huggingface.co/models/timbrooks/instruct-pix2pix](https://api-inference.huggingface.co/models/timbrooks/instruct-pix2pix)"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    data = aiohttp.FormData()
    data.add_field("image", image_bytes, filename="input.jpg", content_type="image/jpeg")
    data.add_field("prompt", instruction)

    async with aiohttp.ClientSession() as session:
        for _ in range(3):
            async with session.post(url, headers=headers, data=data, timeout=60) as resp:
                if resp.status == 200:
                    return await resp.read()
                elif resp.status == 503:
                    await asyncio.sleep(4)
                else:
                    break
    return None


@dp.message(CommandStart())
async def start(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ закрыт. Этот бот настроен только для семьи.")

    text = (
        f"{EMOJI_SHAKE} Привет! Я - GFTB. Семейный, <b>БЕСПЛАТНЫЙ</b> ИИ-ассистент. Построен на базе <b>Gemini</b> и генератором изображений <b>FLUX.1</b>.\n\n"
        f"• <b>Текстовый вопрос</b> — вывод в реальном времени\n"
        f"• <b>Фото с описанием</b> — анализ изображения\n"
        f"• <code>/image &lt;описание&gt;</code> — генерация картинки\n"
        f"• <b>{EMOJI_B_HEART} Временно недоступно |</b> <code>/video &lt;описание&gt;</code> — генерация видео (через очередь)"
    )
    await m.answer(text, parse_mode=ParseMode.HTML)


@dp.message(F.text & ~F.text.startswith("/"))
async def chat_stream(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    await bot.send_chat_action(m.chat.id, "typing")
    sent_message = await m.answer(f"{EMOJI_THINK} <i>Думаю...</i>", parse_mode=ParseMode.HTML)

    full_text = ""
    last_edit_time = time.time()
    edit_delay = 0.8
    system_instruction = get_system_instruction()

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response_stream = ai.models.generate_content_stream(
                model=MODEL_NAME,
                contents=m.text,
                config=dict(system_instruction=system_instruction)
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
            await sent_message.edit_text(f"{EMOJI_B_HEART} Ошибка генерации: {e}", parse_mode=ParseMode.HTML)
            break


@dp.message(F.photo)
async def photo_handler(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    caption = (m.caption or "").strip()
    if not caption:
        caption = "Опиши подробно, что изображено на картинке."

    edit_keywords = ["сделай", "добавь", "измени", "убери", "нарисуй", "поменяй", "переделай", "в стиле"]
    is_edit_request = any(kw in caption.lower() for kw in edit_keywords)

    await bot.send_chat_action(m.chat.id, "typing")

    file_info = await bot.get_file(m.photo[-1].file_id)
    buf = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=buf)
    img_bytes = buf.getvalue()

    if is_edit_request:
        if not HF_TOKEN:
            return await m.answer(f"{EMOJI_SUS} HF_TOKEN не настроен.", parse_mode=ParseMode.HTML)

        status_msg = await m.reply(f"{EMOJI_THINK} <i>Редактирую фото...</i>", parse_mode=ParseMode.HTML)
        try:
            instruction_en = caption
            if ai:
                try:
                    tr = ai.models.generate_content(
                        model=MODEL_NAME,
                        contents=f"Translate this photo edit instruction into simple English command: {caption}. Output ONLY the instruction."
                    )
                    if tr.text:
                        instruction_en = tr.text.strip().replace('"', '')
                except Exception:
                    pass

            result_bytes = await hf_edit_image(img_bytes, instruction_en)
            if result_bytes:
                photo_file = BufferedInputFile(result_bytes, filename="edited.jpg")
                await m.answer_photo(photo=photo_file, caption=f"✨ <b>Готово!</b>\n<i>Запрос:</i> {caption}", parse_mode=ParseMode.HTML)
                await status_msg.delete()
            else:
                await status_msg.edit_text(f"{EMOJI_B_HEART} Ошибка обработки через нейросеть.", parse_mode=ParseMode.HTML)
        except Exception as e:
            await status_msg.edit_text(f"{EMOJI_B_HEART} Ошибка: {e}", parse_mode=ParseMode.HTML)
        return

    status_msg = await m.reply(f"{EMOJI_THINK} <i>Анализирую фото...</i>", parse_mode=ParseMode.HTML)
    try:
        res = ai.models.generate_content(
            model=MODEL_NAME,
            contents=[
                genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                caption
            ],
            config=dict(system_instruction=get_system_instruction())
        )
        formatted_html = clean_markdown_for_telegram(res.text)
        try:
            await status_msg.edit_text(formatted_html, parse_mode=ParseMode.HTML)
        except Exception:
            await status_msg.edit_text(res.text, parse_mode=None)
    except Exception as e:
        await status_msg.edit_text(f"{EMOJI_B_HEART} Ошибка: {e}", parse_mode=ParseMode.HTML)


@dp.message(Command("image"))
async def gen_image(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    user_prompt = m.text.replace("/image", "").strip()
    if not user_prompt:
        return await m.answer(f"{EMOJI_SUS} Укажите описание картинки:\n<code>/image уютная комната в стиле киберпанк</code>", parse_mode=ParseMode.HTML)

    if not HF_TOKEN:
        return await m.answer(f"{EMOJI_SUS} HF_TOKEN не настроен.", parse_mode=ParseMode.HTML)

    msg = await m.answer(f"{EMOJI_THINK} <i>Генерирую в FLUX.1...</i>", parse_mode=ParseMode.HTML)

    final_prompt = user_prompt
    if ai:
        try:
            enhanced_res = ai.models.generate_content(
                model=MODEL_NAME,
                contents=f"Translate to English if needed and improve this prompt for FLUX.1 image generator: {user_prompt}. Keep it focused and clear. Output ONLY the improved prompt, no quotes."
            )
            if enhanced_res.text:
                final_prompt = enhanced_res.text.strip().replace('"', '')
        except Exception:
            pass

    try:
        img_bytes = await hf_generate_flux(final_prompt)
        if img_bytes:
            photo = BufferedInputFile(img_bytes, filename="art.jpg")
            caption = f"🎨 <b>Запрос:</b> {user_prompt}\n✨ <i>Модель: FLUX.1 Schnell</i>"
            await m.answer_photo(photo=photo, caption=caption, parse_mode=ParseMode.HTML)
            await msg.delete()
        else:
            await msg.edit_text(f"{EMOJI_B_HEART} Не удалось сгенерировать изображение (сервер занят).", parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.edit_text(f"{EMOJI_B_HEART} Ошибка генерации: {e}", parse_mode=ParseMode.HTML)


@dp.message(Command("video"))
async def queue_video(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    prompt = m.text.replace("/video", "").strip()
    if not prompt:
        return await m.answer(f"{EMOJI_SUS} Укажите описание видео:\n<code>/video закат в горах</code>", parse_mode=ParseMode.HTML)

    if not redis:
        return await m.answer(f"{EMOJI_SUS} Сервер очереди не настроен.", parse_mode=ParseMode.HTML)

    task = {"chat_id": m.chat.id, "user_id": m.from_user.id, "prompt": prompt}
    redis.rpush("video_queue", json.dumps(task))
    await m.answer(f"{EMOJI_OK} Задача на видео добавлена в очередь.", parse_mode=ParseMode.HTML)


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
