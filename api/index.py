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
    "gemini-3.5-flash-lite": "🪶 Gemini 3.5 Flash-Lite (Быстрая)"
}

DEFAULT_MODE = "chat"
AVAILABLE_MODES = {
    "chat": "💬 Чат / Текст",
    "analyze": "🔍 Анализ фото (Gemini)",
    "edit": "🎨 Редактирование фото (Qwen)",
    "generate": "🖼 Генерация картинок (FLUX)"
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


def get_user_mode(user_id: int) -> str:
    if redis:
        try:
            stored = redis.get(f"user_mode:{user_id}")
            if stored and stored in AVAILABLE_MODES:
                return stored
        except Exception:
            pass
    return DEFAULT_MODE


def set_user_mode(user_id: int, mode_name: str) -> None:
    if redis and mode_name in AVAILABLE_MODES:
        try:
            redis.set(f"user_mode:{user_id}", mode_name)
        except Exception:
            pass


def get_model_keyboard(current_model: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for model_id, display_name in AVAILABLE_MODELS.items():
        prefix = "✅ " if model_id == current_model else ""
        builder.button(text=f"{prefix}{display_name}", callback_data=f"set_model:{model_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_mode_keyboard(current_mode: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for mode_id, display_name in AVAILABLE_MODES.items():
        prefix = "✅ " if mode_id == current_mode else ""
        builder.button(text=f"{prefix}{display_name}", callback_data=f"set_mode:{mode_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_system_instruction() -> str:
    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    formatted_now = now.strftime(f"%d.%m.%Y, {weekdays[now.weekday()]}, %H:%M:%S (МСК)")

    return (
        f"Текущая дата и точное время: {formatted_now}.\n"
        "Ты внимательный семейный ассистент в Telegram. "
        "Отвечай вежливо, структурированно, используй жирный шрифт для ключевых мыслей. "
        "Никогда не используй тройные звёздочки (***)."
    )


def clean_markdown_for_telegram(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def code_block_sub(match):
        lang = match.group(1) or ""
        code = match.group(2)
        return f'<pre><code class="language-{lang}">{code}</code></pre>'

    text = re.sub(r'```(\w+)?\n?(.*?)
