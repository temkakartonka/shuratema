import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from aiogram.filters import CommandStart
import aiohttp
import hashlib
import re

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в переменных окружения")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

DEFAULT_STOREFRONT = "ru"
INLINE_TIMEOUT = 15

async def fetch_title(session, url):
    try:
        async with session.get(url, timeout=10) as resp:
            html = await resp.text()
            m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if m:
                return m.group(1).strip()
    except Exception:
        return None

async def convert_link(url: str, storefront: str = DEFAULT_STOREFRONT) -> str:
    if "music.apple.com" in url:
        return url.replace("music.apple.com", "open.spotify.com")
    elif "open.spotify.com" in url:
        return url.replace("open.spotify.com", f"music.apple.com/{storefront}")
    return None

@dp.inline_query()
async def inline_handler(inline_query: types.InlineQuery):
    url = inline_query.query.strip()
    if not url:
        return
    unique_id = hashlib.md5(url.encode()).hexdigest()

    async with aiohttp.ClientSession() as session:
        title = await fetch_title(session, url) or "Идет обработка..."

    processing_result = [
        InlineQueryResultArticle(
            id=unique_id,
            title=title,
            input_message_content=InputTextMessageContent(message_text="⏳ Идет обработка..."),
            description=url,
        )
    ]
    await inline_query.answer(processing_result, cache_time=1, is_personal=True)

    try:
        link = await asyncio.wait_for(convert_link(url), timeout=INLINE_TIMEOUT)
        if link:
            result = [
                InlineQueryResultArticle(
                    id=unique_id,
                    title="Готово!",
                    input_message_content=InputTextMessageContent(message_text=link),
                    description=link,
                )
            ]
            await inline_query.answer(result, cache_time=1, is_personal=True)
    except Exception as e:
        logging.error(f"Ошибка при обработке: {e}")

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привет! Вставь ссылку на Apple Music или Spotify прямо в строку поиска inline-бота.")

async def main():
    logging.info("Bot started. Inline only.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
