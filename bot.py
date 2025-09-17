import os, re, json, asyncio, logging, httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DEFAULT_STOREFRONT = "us"
INLINE_TIMEOUT = 8

def strip_inv(s:str)->str:
    import re
    return re.sub(r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\u00A0]", " ", s or "")

def clean_title(raw:str)->str:
    import re
    s = strip_inv((raw or "").strip())
    s = re.sub(r"^(?:песня|трек|сингл|song|track|single|title|titel|название)\s*[:\-–—]?\s*[«\"“]?", "", s, flags=re.I)
    s = re.sub(r"[»”\"]", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def clean_artist(a:str)->str:
    import re
    s = strip_inv(a or "").strip()
    s = re.sub(r"\b(?:on\s+)?Apple\s*Music\b", "", s, flags=re.I)
    s = re.sub(r"\s*(?:—|-|\|)\s*$", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def parse_spotify_title(title:str):
    import re
    s = re.sub(r"\s*\|\s*Spotify\s*$", title or "", flags=re.I)
    s = re.sub(r"^\s*(?:title|titel|название)\s*[:\-–—]\s*", "", s, flags=re.I)
    m = re.match(r"^(.*?)\s*[•·]\s*(.+)$", s)          # Track • Artist
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))
    m = re.match(r"^(.*?)\s+[–—-]\s+(.+)$", s)         # Track — Artist
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))
    m = re.match(r"^(.*?)\s+by\s+(.+)$", s, flags=re.I)# Track by Artist
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))
    return None, None

async def get_text(c: httpx.AsyncClient, url:str)->str:
    r = await c.get(url, timeout=15); r.raise_for_status(); return r.text

async def get_json(c: httpx.AsyncClient, url:str):
    r = await c.get(url, timeout=15); r.raise_for_status(); return r.json()

async def extract_from_spotify(c: httpx.AsyncClient, url:str):
    # oEmbed
    try:
        d = await get_json(c, f"https://open.spotify.com/oembed?url={url}")
        t,a = parse_spotify_title(d.get("title") or "")
        if t and a: return a,t
        if d.get("author_name") and d.get("title"):
            return clean_artist(d["author_name"]), clean_title(d["title"])
    except: pass
    # embed
    import re
    m = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not m: raise RuntimeError("Не удалось извлечь из Spotify")
    tid = m.group(1)
    try:
        html = await get_text(c, f"https://open.spotify.com/embed/track/{tid}?utm_source=oembed")
        tt = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
        if tt:
            t,a = parse_spotify_title(tt.group(1))
            if t and a: return a,t
    except: pass
    raise RuntimeError("Не удалось извлечь из Spotify")

async def extract_from_apple(c: httpx.AsyncClient, url:str):
    html = await get_text(c, url)
    # JSON-LD
    for m in re.finditer(r'<script type="application/ld\+json">([\s\S]*?)</script>', html, flags=re.I):
        try:
            obj = json.loads(m.group(1))
            arr = obj if isinstance(obj, list) else [obj]
            for o in arr:
                name = o.get("name",""); by = o.get("byArtist",{})
                artist = by.get("name") if isinstance(by,dict) else by
                if name and artist: return clean_artist(str(artist)), clean_title(str(name))
        except: pass
    # og:title
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, flags=re.I)
    if og and " — " in og.group(1):
        left,right = og.group(1).split(" — ",1)
        return clean_artist(right), clean_title(left)
    # title
    tt = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
    if tt and "—" in tt.group(1):
        left,right = tt.group(1).split("—",1)
        return clean_artist(right), clean_title(left)
    raise RuntimeError("Не удалось извлечь из Apple")

async def search_apple(c:httpx.AsyncClient, storefront:str, artist:str, track:str)->str|None:
    q = f"{artist} {track}".strip()
    try:
        term = httpx.QueryParams({'term': q})['term']
        d = await get_json(c, f"https://itunes.apple.com/search?media=music&entity=song&limit=10&country={storefront}&term={term}")
        if d.get("results"):
            url = d["results"][0].get("trackViewUrl") or d["results"][0].get("collectionViewUrl")
            if url: return url
    except: pass
    try:  # web-поиск
        term = httpx.QueryParams({'term': q})['term']
        h = await get_text(c, f"https://music.apple.com/{storefront}/search?term={term}")
        m = re.search(r'https://music\.apple\.com/[a-z]{2}/[^"]*/song/[^"]+/\d+', h, flags=re.I)
        if m: return m.group(0)
    except: pass
    return None

async def search_spotify(c:httpx.AsyncClient, artist:str, track:str)->str|None:
    q = f"{artist} {track}".strip()
    try:  # DuckDuckGo HTML
        qp = httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']
        h = await get_text(c, f"https://html.duckduckgo.com/html/?q={qp}")
        import urllib.parse as U, re
        def decode(href:str)->str:
            try:
                qs = U.parse_qs(U.urlparse(href).query)
                return U.unquote(qs.get('uddg',[''])[0]) or href
            except: return href
        for href in re.findall(r'href="([^"]+)"', h):
            u = decode(href)
            if re.match(r"^https?://open\.spotify\.com/track/", u, flags=re.I): return u
    except: pass
    try:  # Bing
        qp = httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']
        h = await get_text(c, f"https://www.bing.com/search?q={qp}")
        m = re.search(r'href="(https?://open\.spotify\.com/track/[A-Za-z0-9]+)"', h, flags=re.I)
        if m: return m.group(1)
    except: pass
    return None

async def convert_inline(url:str, storefront:str=DEFAULT_STOREFRONT)->str|None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent":"Mozilla/5.0","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en"},
        trust_env=True
    ) as c:
        if "open.spotify.com/track/" in url:
            artist, track = await extract_from_spotify(c, url)
            return await search_apple(c, storefront, artist, track)
        if "music.apple.com/" in url:
            artist, track = await extract_from_apple(c, url)
            return await search_spotify(c, artist, track)
    return None

dp = Dispatcher()

@dp.message(F.text == "/id")
async def cmd_id(m: Message):
    await m.answer(str(m.from_user.id))

@dp.inline_query()
async def on_inline(q: InlineQuery):
    text = (q.query or "").strip()
    m = re.search(r"(https?://\S+)", text)
    if not m:
        return await q.answer([
            InlineQueryResultArticle(
                id="help",
                title="Вставьте ссылку на ПЕСНЮ",
                description="Поддерживаются Apple /song/ и Spotify /track/",
                input_message_content=InputTextMessageContent(
                    message_text="Поддерживаются ссылки на песню: Apple /song/ и Spotify /track/."
                ),
            )
        ], cache_time=1, is_personal=True)

    url = m.group(1)
    if ("music.apple.com" in url and "/song/" not in url) or ("open.spotify.com" in url and "/track/" not in url):
        return await q.answer([
            InlineQueryResultArticle(
                id="onlysong",
                title="Нужна ссылка на ПЕСНЮ",
                description="Альбом/плейлист не поддерживается",
                input_message_content=InputTextMessageContent(
                    message_text="Используйте ссылку на ПЕСНЮ (Apple /song/, Spotify /track/)."
                ),
            )
        ], cache_time=1, is_personal=True)

    try:
        link = await asyncio.wait_for(convert_inline(url, DEFAULT_STOREFRONT), timeout=INLINE_TIMEOUT)
        if link:
            return await q.answer([
                InlineQueryResultArticle(
                    id="ok",
                    title="Готово — открыть ссылку",
                    description=link,
                    input_message_content=InputTextMessageContent(message_text=link),
                )
            ], cache_time=0, is_personal=True)
        else:
            return await q.answer([
                InlineQueryResultArticle(
                    id="nf",
                    title="Не найдено",
                    description="Попробуйте другой storefront или пришлите ссылку боту в ЛС",
                    input_message_content=InputTextMessageContent(
                        message_text="Не найдено. Попробуйте другой storefront или пришлите ссылку боту в личные сообщения."
                    ),
                )
            ], cache_time=0, is_personal=True)
    except asyncio.TimeoutError:
        return await q.answer([
            InlineQueryResultArticle(
                id="to",
                title="Медленно отвечает — попробуйте ещё раз",
                description="Поиск занял слишком долго",
                input_message_content=InputTextMessageContent(
                    message_text="Поиск занял слишком долго. Попробуйте ещё раз через пару секунд."
                ),
            )
        ], cache_time=0, is_personal=True)
    except Exception as e:
        logging.exception("inline error")
        return await q.answer([
            InlineQueryResultArticle(
                id="err",
                title="Ошибка",
                description=str(e),
                input_message_content=InputTextMessageContent(message_text="Ошибка: "+str(e)),
            )
        ], cache_time=0, is_personal=True)

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token or ":" not in token:
        raise RuntimeError("BOT_TOKEN не задан или некорректен")
    bot = Bot(token)
    print("Bot started. Inline only.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
