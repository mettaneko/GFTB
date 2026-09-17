import os
import json
import io
import asyncio
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from upstash_redis import Redis
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
EMOJI_SUS = '<tg-emoji emoji-id="4951814692029858673">🤨</tg-emoji>'
EMOJI_OK = '<tg-emoji emoji-id="4947363551133041555">👌</tg-emoji>'

DEFAULT_MODEL = "gemini-3.8-flash"
AVAILABLE_MODELS = {
    "gemini-3.8-flash": "⚡ Gemini 3.8 Flash (Новейшая)",
    "gemini-3.6-flash": "🚀 Gemini 3.6 Flash",
    "gemini-3.1-pro": "🧠 Gemini 3.1 Pro (Глубокое мышление)",
    "gemini-3.5-flash-lite": "🪶 Gemini 3.5 Flash-Lite (Быстрая)",
    "gemini-2.5-flash": "🛡️ Gemini 2.5 Flash (Стабильная)"
}

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


def get_user_model(user_id: int) -> str:
    if redis:
        try:
            stored = redis.get(f"user_model:{user_id}")
            if stored and stored in AVAILABLE_MODELS:
                return stored
        except Exception:
            pass
    return DEFAULT_MODEL


def set_user_model(user_id: int, model_name: str) -> None:
    if redis and model_name in AVAILABLE_MODELS:
        try:
            redis.set(f"user_model:{user_id}", model_name)
        except Exception:
            pass


def get_model_keyboard(current_model: str = "") -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for model_id, display_name in AVAILABLE_MODELS.items():
        prefix = "✅ " if model_id == current_model else ""
        builder.button(text=f"{prefix}{display_name}", callback_data=f"set_model:{model_id}")
    builder.adjust(1)
    return builder.as_markup()


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


@dp.message(CommandStart())
async def start(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ закрыт. Этот бот настроен только для семьи.")

    cur_model = get_user_model(m.from_user.id)
    text = (
        f"{EMOJI_SHAKE} Привет! Я — GFTB, семейный ИИ-ассистент.\n\n"
        f"• <b>Текущая модель:</b> <code>{cur_model}</code>\n"
        f"• <b>Сменить модель:</b> /model\n"
        f"• <b>Текстовый вопрос</b> — стриминг в реальном времени\n"
        f"• <b>Фото с описанием</b> — анализ через Gemini или задача на обработку\n"
        f"• <code>/image &lt;описание&gt;</code> — генерация картинки (через очередь)\n"
        f"• <code>/video &lt;описание&gt;</code> — генерация видео (через очередь)"
    )
    await m.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("model"))
async def cmd_model(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")
    cur_model = get_user_model(m.from_user.id)
    await m.answer(
        f"⚙️ <b>Выберите активную модель нейросети:</b>\nТекущая: <code>{cur_model}</code>",
        reply_markup=get_model_keyboard(cur_model),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("set_model:"))
async def on_model_selected(cb: types.CallbackQuery):
    if not check_access(cb.from_user.id):
        return await cb.answer("Доступ ограничен.", show_alert=True)

    new_model = cb.data.split("set_model:")[1]
    if new_model in AVAILABLE_MODELS:
        set_user_model(cb.from_user.id, new_model)
        await cb.answer(f"Модель изменена на {new_model}")
        try:
            await cb.message.edit_text(
                f"{EMOJI_OK} <b>Модель успешно изменена на:</b> <code>{new_model}</code>\n"
                f"<i>{AVAILABLE_MODELS[new_model]}</i>",
                reply_markup=get_model_keyboard(new_model),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


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
    user_model = get_user_model(m.from_user.id)

    try:
        response_stream = ai.models.generate_content_stream(
            model=user_model,
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

    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            await sent_message.edit_text(
                f"{EMOJI_B_HEART} <b>Закончилась квота для модели</b> <code>{user_model}</code>!\n\n"
                f"Выберите другую модель для продолжения работы:",
                reply_markup=get_model_keyboard(user_model),
                parse_mode=ParseMode.HTML
            )
        else:
            await sent_message.edit_text(f"{EMOJI_B_HEART} Ошибка генерации: {err_str}", parse_mode=ParseMode.HTML)


@dp.message(F.photo)
async def photo_handler(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    caption = (m.caption or "").strip()
    if not caption:
        caption = "Опиши подробно, что изображено на картинке."

    edit_keywords = ["сделай", "добавь", "измени", "убери", "нарисуй", "поменяй", "переделай", "в стиле"]
    is_edit_request = any(kw in caption.lower() for kw in edit_keywords)

    if is_edit_request:
        if not redis:
            return await m.answer(f"{EMOJI_SUS} Сервер очереди не настроен.", parse_mode=ParseMode.HTML)

        photo_id = m.photo[-1].file_id
        task = {
            "type": "edit_image",
            "chat_id": m.chat.id,
            "message_id": m.message_id,
            "file_id": photo_id,
            "prompt": caption,
            "created_at": time.time()
        }
        redis.rpush("media_queue", json.dumps(task))
        return await m.answer(f"{EMOJI_OK} Задача на редактирование добавлена в очередь.", parse_mode=ParseMode.HTML)

    await bot.send_chat_action(m.chat.id, "typing")
    status_msg = await m.reply(f"{EMOJI_THINK} <i>Анализирую фото...</i>", parse_mode=ParseMode.HTML)
    user_model = get_user_model(m.from_user.id)

    try:
        file_info = await bot.get_file(m.photo[-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)

        res = ai.models.generate_content(
            model=user_model,
            contents=[
                genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
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
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            await status_msg.edit_text(
                f"{EMOJI_B_HEART} <b>Лимит исчерпан для</b> <code>{user_model}</code>.\nПереключитесь на другую модель:",
                reply_markup=get_model_keyboard(user_model),
                parse_mode=ParseMode.HTML
            )
        else:
            await status_msg.edit_text(f"{EMOJI_B_HEART} Ошибка: {err_str}", parse_mode=ParseMode.HTML)


@dp.message(Command("image"))
async def gen_image(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    user_prompt = m.text.replace("/image", "").strip()
    if not user_prompt:
        return await m.answer(f"{EMOJI_SUS} Укажите описание картинки:\n<code>/image уютная комната в стиле киберпанк</code>", parse_mode=ParseMode.HTML)

    if not redis:
        return await m.answer(f"{EMOJI_SUS} Сервер очереди не настроен.", parse_mode=ParseMode.HTML)

    task = {
        "type": "gen_image",
        "chat_id": m.chat.id,
        "message_id": m.message_id,
        "prompt": user_prompt,
        "created_at": time.time()
    }
    redis.rpush("media_queue", json.dumps(task))
    await m.answer(f"{EMOJI_OK} Задача на генерацию картинки добавлена в очередь.", parse_mode=ParseMode.HTML)


@dp.message(Command("video"))
async def queue_video(m: types.Message):
    if not check_access(m.from_user.id):
        return await m.answer(f"{EMOJI_B_HEART} Доступ ограничен.")

    prompt = m.text.replace("/video", "").strip()
    if not prompt:
        return await m.answer(f"{EMOJI_SUS} Укажите описание видео:\n<code>/video закат в горах</code>", parse_mode=ParseMode.HTML)

    if not redis:
        return await m.answer(f"{EMOJI_SUS} Сервер очереди не настроен.", parse_mode=ParseMode.HTML)

    task = {
        "type": "gen_video",
        "chat_id": m.chat.id,
        "message_id": m.message_id,
        "prompt": prompt,
        "created_at": time.time()
    }
    redis.rpush("media_queue", json.dumps(task))
    await m.answer(f"{EMOJI_OK} Задача на видео добавлена в очередь.", parse_mode=ParseMode.HTML)


@app.post("/")
@app.post("/api/index")
@app.post("/api/index.py")
async def webhook(req: Request):
    try:
        data = await req.json()
        upd = types.Update(**data)
        await dp.feed_update(bot, upd)
    except Exception as e:
        print(f"Webhook update handled: {e}")
    return {"status": "ok"}


@app.get("/")
@app.get("/api/index")
@app.get("/api/index.py")
async def health():
    return {"status": "alive"}
