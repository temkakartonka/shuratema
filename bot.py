# bot.py — упрощённый inline-бот Apple ↔ Spotify + "Идёт обработка…"
# Python 3.10+, pip install -r requirements.txt

import re, asyncio, json, sys, logging, os
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message

# === НАСТРОЙКИ ===
# Обязательная переменная окружения BOT_TOKEN (получите у @BotFather).
BOT_TOKEN = os.getenv("8275429191:AAGYQm4ISZEwl95LiigJ8lTSpm7Kj0DQd3w")
DEFAULT_STOREFRONT = "us"
INLINE_TIMEOUT = 7  # сек

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# === УТИЛИТЫ ===
def strip_inv(s: str) -> str:
    return re.sub(r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\u00A0]", " ", s or "")

def clean_title(raw: str) -> str:
    s = strip_inv((raw or "").strip())
    s = re.sub(r"^(?:песня|трек|сингл|song|track|single|title|titel|название)\s*[:\-–—]?\s*[«\"“]?", "", s, flags=re.I)
    s = re.sub(r"[»”\"]", "", s)
    s = re.sub(r"\s*[-–—]\s*(live|single|clean|explicit|radio edit|remaster(?:ed)?(?:\s*\d{2,4})?)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*\((live|single|clean|explicit|radio edit|remaster(?:ed)?(?:\s*\d{2,4})?)\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def clean_artist(a: str) -> str:
    s = strip_inv(a or "").strip()
    s = re.sub(r"\b(?:on\s+)?Apple\s*Music\b", "", s, flags=re.I)
    s = re.sub(r"\s*(?:—|-|\|)\s*$", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

async def get_text(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, timeout=15)
    r.raise_for_status()
    return r.text

async def get_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def parse_spotify_title(title: str):
    s = re.sub(r"\s*\|\s*Spotify\s*$", title or "", flags=re.I)
    s = re.sub(r"^\s*(?:title|titel|название)\s*[:\-–—]\s*", "", s, flags=re.I)

    # "Track • Artist" или "Track · Artist"
    m = re.match(r"^(.*?)\s*[•·]\s*(.+)$", s)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))

    # "Track - song and lyrics by Artist"
    m = re.match(r"^(.*?)\s+[–—-]\s+(?:song.*?|single).*?\s+by\s+(.+)$", s, flags=re.I)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))

    # "Track — Artist" / "Track - Artist"
    m = re.match(r"^(.*?)\s+[–—-]\s+(.+)$", s)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))

    # "Track by Artist"
    m = re.match(r"^(.*?)\s+by\s+(.+)$", s, flags=re.I)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))

    return None, None

# === EXTRACTORS (упрощённые) ===
async def extract_from_spotify(client: httpx.AsyncClient, url: str):
    """
    Быстро и просто:
    1) oEmbed (title + author_name)
    2) <title> на embed-странице
    """
    # 1) oEmbed
    try:
        data = await get_json(client, f"https://open.spotify.com/oembed?url={url}")
        title = data.get("title") or ""
        author = data.get("author_name") or ""
        t, a = parse_spotify_title(title)
        if t and a:
            logging.info("SPOT via oEmbed.title")
            return a, t
        if author and title:
            logging.info("СПОТ via oEmbed.author+title")
            return clean_artist(author), clean_title(title)
    except Exception:
        pass

    # 2) embed title
    mid = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not mid:
        raise RuntimeError("Не удалось извлечь из Spotify")
    tid = mid.group(1)

    try:
        html = await get_text(client, f"https://open.spotify.com/embed/track/{tid}?utm_source=oembed")
        tt = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
        if tt:
            t, a = parse_spotify_title(tt.group(1))
            if t and a:
                logging.info("SPOT via embed <title>")
                return a, t
    except Exception:
        pass

    raise RuntimeError("Не удалось извлечь из Spotify")

async def extract_from_apple(client: httpx.AsyncClient, url: str):
    html = await get_text(client, url)
    # JSON-LD
    for m in re.finditer(r'<script type="application/ld\+json">([\s\S]*?)</script>', html, flags=re.I):
        try:
            obj = json.loads(m.group(1))
            arr = obj if isinstance(obj, list) else [obj]
            for o in arr:
                name = o.get("name", "")
                by = o.get("byArtist", {})
                artist = by.get("name") if isinstance(by, dict) else by
                if name and artist:
                    return clean_artist(str(artist)), clean_title(str(name))
        except Exception:
            pass
    # og:title
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, flags=re.I)
    if og:
        s = og.group(1)
        m = re.match(r'^Песня\s+[«"]([^»”"]+)[»”"]\s+—\s+(.+)$', s, flags=re.I)
        if m: return clean_artist(m.group(2)), clean_title(m.group(1))
        if " — " in s:
            left, right = s.split(" — ", 1)
            return clean_artist(right), clean_title(left)
    # <title> запасной
    tt = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
    if tt and "—" in tt.group(1):
        left, right = tt.group(1).split("—", 1)
        return clean_artist(right), clean_title(left)
    raise RuntimeError("Не удалось извлечь из Apple")

# === SEARCH ===
async def search_apple(client: httpx.AsyncClient, storefront: str, artist: str, track: str) -> str | None:
    q = f"{artist} {track}".strip()
    try:
        term = httpx.QueryParams({'term': q})['term']
        data = await get_json(client, f"https://itunes.apple.com/search?media=music&entity=song&limit=10&country={storefront.lower()}&term={term}")
        if data.get("results"):
            url = data["results"][0].get("trackViewUrl") or data["results"][0].get("collectionViewUrl")
            if url: return url
    except Exception:
        pass
    # запасной — web-поиск
    try:
        term = httpx.QueryParams({'term': q})['term']
        html = await get_text(client, f"https://music.apple.com/{storefront.lower()}/search?term={term}")
        m = re.search(r'https://music\.apple\.com/[a-z]{2}/[^"]*/song/[^"]+/\d+', html, flags=re.I)
        if m: return m.group(0)
    except Exception:
        pass
    return None

async def search_spotify(client: httpx.AsyncClient, artist: str, track: str) -> str | None:
    q = f"{artist} {track}".strip()
    # DuckDuckGo HTML
    try:
        qp = httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']
        html = await get_text(client, f"https://html.duckduckgo.com/html/?q={qp}")
        links = re.findall(r'href="([^"]+)"', html)
        from urllib.parse import urlparse, parse_qs, unquote
        def decode_uddg(h: str) -> str:
            try:
                qs = parse_qs(urlparse(h).query)
                return unquote(qs.get('uddg', [''])[0]) or h
            except Exception:
                return h
        for h in map(decode_uddg, links):
            if re.match(r"^https?://open\.spotify\.com/track/", h, flags=re.I):
                return h
    except Exception:
        pass
    # Bing запасной
    try:
        qp = httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']
        html = await get_text(client, f"https://www.bing.com/search?q={qp}")
        m = re.search(r'href="(https?://open\.spotify\.com/track/[A-Za-z0-9]+)"', html, flags=re.I)
        if m: return m.group(1)
    except Exception:
        pass
    return None

# === CONVERT ===
async def convert_inline(url: str, storefront: str = DEFAULT_STOREFRONT) -> str | None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
        },
        trust_env=True
    ) as client:
        if "open.spotify.com/track/" in url:
            artist, track = await extract_from_spotify(client, url)
            return await search_apple(client, storefront, artist, track)
        if "music.apple.com/" in url:
            artist, track = await extract_from_apple(client, url)
            return await search_spotify(client, artist, track)
    return None

# === BOT ===
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

@dp.message(F.text == "/id")
async def cmd_id(msg: Message):
    await msg.answer(str(msg.from_user.id))

@dp.inline_query()
async def on_inline(q: InlineQuery):
    logging.info("inline from %s: %s", q.from_user.id, q.query)

    text = (q.query or "").strip()
    m = re.search(r"(https?://\S+)", text)

    # нет ссылки — подсказка
    if not m:
        return await q.answer(
            [InlineQueryResultArticle(
                id="help",
                title="Вставьте ссылку на ПЕСНЮ",
                description="Поддерживаются: Apple /song/ и Spotify /track/",
                input_message_content=InputTextMessageContent(
                    message_text="Поддерживаются ссылки на песню: Apple /song/ и Spotify /track/."
                ),
            )],
            cache_time=1, is_personal=True
        )

    url = m.group(1)

    # не песня — подсказка
    if ("music.apple.com" in url and "/song/" not in url) or \
       ("open.spotify.com" in url and "/track/" not in url):
        return await q.answer(
            [InlineQueryResultArticle(
                id="onlysong",
                title="Нужна ссылка на ПЕСНЮ",
                description="Альбом/плейлист не поддерживается",
                input_message_content=InputTextMessageContent(
                    message_text="Используйте ссылку на ПЕСНЮ (Apple /song/, Spotify /track/)."
                ),
            )],
            cache_time=1, is_personal=True
        )

    # Плитка "Идёт обработка…"
    await q.answer(
        [InlineQueryResultArticle(
            id="processing",
            title="Идёт обработка…",
            description="ищем трек в другом сервисе",
            input_message_content=InputTextMessageContent(message_text="Идёт обработка…"),
        )],
        cache_time=0, is_personal=True
    )

    # Финальный ответ
    try:
        link = await asyncio.wait_for(convert_inline(url, DEFAULT_STOREFRONT), timeout=INLINE_TIMEOUT)
        if link:
            return await q.answer(
                [InlineQueryResultArticle(
                    id="ok",
                    title="Готово — открыть ссылку",
                    description=link,
                    input_message_content=InputTextMessageContent(message_text=link),
                )],
                cache_time=0, is_personal=True
            )
        else:
            return await q.answer(
                [InlineQueryResultArticle(
                    id="notfound",
                    title="Не найдено",
                    description="Попробуйте другой storefront или пришлите ссылку боту в ЛС",
                    input_message_content=InputTextMessageContent(
                        message_text="Не найдено. Попробуйте другой storefront или пришлите ссылку боту в личные сообщения."
                    ),
                )],
                cache_time=0, is_personal=True
            )
    except asyncio.TimeoutError:
        return await q.answer(
            [InlineQueryResultArticle(
                id="timeout",
                title="Медленно отвечает — попробуйте ещё раз",
                description="Поиск занял слишком долго",
                input_message_content=InputTextMessageContent(
                    message_text="Поиск занял слишком долго. Попробуйте ещё раз через пару секунд."
                ),
            )],
            cache_time=0, is_personal=True
        )
    except Exception as e:
        logging.exception("inline error")
        return await q.answer(
            [InlineQueryResultArticle(
                id="err",
                title="Ошибка",
                description=str(e),
                input_message_content=InputTextMessageContent(
                    message_text="Ошибка: " + str(e)
                ),
            )],
            cache_time=0, is_personal=True
        )

async def main():
    if not BOT_TOKEN or len(str(BOT_TOKEN).split(":")) != 2:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана или некорректна.")
    print("Bot started. Inline only.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
