# bot.py — inline-бот конвертации Apple ↔ Spotify
# Зависимости: aiogram>=3.4, httpx>=0.24

import os, re, json, asyncio, sys, logging
from urllib.parse import urlparse, parse_qs
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message

DEFAULT_STOREFRONT = "us"
INLINE_TIMEOUT = 8

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ---------- utils ----------
def strip_inv(s: str) -> str:
    return re.sub(r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\u00A0]", " ", s or "")

def clean_title(raw: str) -> str:
    s = strip_inv((raw or "").strip())
    s = re.sub(r"^(?:песня|трек|сингл|song|track|single|title|titel|название)\s*[:\-–—]?\s*[«\"“]?", "", s, flags=re.I)
    s = re.sub(r"[»”\"]", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def clean_artist(a: str) -> str:
    s = strip_inv(a or "").strip()
    s = re.sub(r"\b(?:on\s+)?Apple\s*Music\b", "", s, flags=re.I)
    s = re.sub(r"\s*(?:—|-|\|)\s*$", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def norm_for_query(s: str) -> str:
    s = strip_inv(s or "")
    s = re.sub(r"\b(?:on\s+)?Apple\s*Music\b", "", s, flags=re.I)
    s = re.sub(r"[«»“”\"]", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

async def get_text(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    return r.text

async def get_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    return r.json()

# ---------- Apple helpers (альбомная ссылка ?i=<trackId>) ----------
def is_apple_track_url(url: str) -> bool:
    """Ссылка на трек в Apple: /song/ или /album/... с ?i=<trackId>."""
    if "music.apple.com" not in url:
        return False
    if "/song/" in url:
        return True
    if "/album/" in url:
        qs = parse_qs(urlparse(url).query)
        return "i" in qs and any(x.isdigit() for x in qs.get("i", []))
    return False

async def normalize_apple_album_i(client: httpx.AsyncClient, url: str, storefront: str) -> str | None:
    """
    Преобразует /album/...?...&i=<trackId> в прямую ссылку на песню через iTunes Lookup.
    """
    if "/album/" not in url:
        return None
    qs = parse_qs(urlparse(url).query)
    ids = qs.get("i", [])
    if not ids or not ids[0].isdigit():
        return None
    track_id = ids[0]
    try:
        data = await get_json(
            client,
            f"https://itunes.apple.com/lookup?id={track_id}&country={storefront}&entity=song"
        )
        results = data.get("results") or []
        for item in results:
            if item.get("kind") == "song" and item.get("trackViewUrl"):
                return item["trackViewUrl"]
        for item in results:
            if item.get("trackViewUrl"):
                return item["trackViewUrl"]
    except Exception:
        pass
    return None

# ---------- Spotify extract ----------
def parse_spotify_title(title: str):
    s = re.sub(r"\s*\|\s*Spotify\s*$", title or "", flags=re.I)
    s = re.sub(r"^\s*(?:title|titel|название)\s*[:\-–—]\s*", "", s, flags=re.I)
    m = re.match(r"^(.*?)\s+[–—-]\s+(?:song.*?|single).*?\s+by\s+(.+)$", s, flags=re.I)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))
    m = re.match(r"^(.*?)\s+[–—-]\s+(.+)$", s)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))
    m = re.match(r"^(.*?)\s+by\s+(.+)$", s, flags=re.I)
    if m: return clean_title(m.group(1)), clean_artist(m.group(2))
    return None, None

async def extract_from_spotify(client: httpx.AsyncClient, url: str):
    def meta_dict(html: str) -> dict:
        out = {}
        for m in re.finditer(r"<meta\s+[^>]*>", html, flags=re.I):
            tag = m.group(0)
            k = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
            v = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
            if k and v:
                out[k.group(1).strip().lower()] = v.group(1)
        return out

    def parse_dash(s: str):
        m = re.match(r"^(.*?)\s*[–—-]\s*(.+)$", s.strip())
        if m:
            return clean_title(m.group(1)), clean_artist(m.group(2))
        return None, None

    mid = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not mid:
        raise RuntimeError("Не удалось извлечь из Spotify")
    tid = mid.group(1)

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
            logging.info("SPOT via oEmbed.author+title")
            return clean_artist(author), clean_title(title)
    except Exception:
        pass

    # 2) HTML endpoints (meta + __NEXT_DATA__ + <title>)
    async def try_from_html(html: str):
        metas = meta_dict(html)
        ogt = metas.get("og:title") or metas.get("twitter:title")
        ogd = metas.get("og:description") or metas.get("description")
        logging.info("SPOT meta: og:title=%r og:desc=%r", ogt, ogd)

        if ogt:
            t, a = parse_spotify_title(ogt)
            if not (t and a):
                t, a = parse_dash(ogt)
            if t and a:
                logging.info("SPOT via og:title")
                return a, t

        if ogt and ogd:
            artist_guess = re.split(r"\s*[·\-\|]\s*", ogd.strip())[0]
            t2, _ = parse_dash(ogt)
            if not t2:
                t2 = clean_title(ogt)
            if artist_guess and t2:
                logging.info("SPOT via og:desc + og:title")
                return clean_artist(artist_guess), t2

        mnext = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html, flags=re.I)
        if mnext:
            try:
                data = json.loads(mnext.group(1))
                def walk(x):
                    if isinstance(x, dict):
                        if x.get("type") == "track" and x.get("name") and isinstance(x.get("artists"), list):
                            for ar in x["artists"]:
                                if isinstance(ar, dict) and ar.get("name"):
                                    return clean_artist(ar["name"]), clean_title(x["name"])
                        if x.get("uri") == f"spotify:track:{tid}" and x.get("name"):
                            artist = None
                            arts = x.get("artists") or x.get("artists.items") or []
                            if isinstance(arts, list):
                                for ar in arts:
                                    if isinstance(ar, dict) and ar.get("name"):
                                        artist = ar["name"]; break
                            if artist:
                                return clean_artist(artist), clean_title(x["name"])
                        for v in x.values():
                            r = walk(v)
                            if r: return r
                    elif isinstance(x, list):
                        for it in x:
                            r = walk(it)
                            if r: return r
                    return None
                got = walk(data)
                if got:
                    logging.info("SPOT via __NEXT_DATA__")
                    return got
            except Exception:
                pass

        tt = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
        if tt:
            t, a = parse_spotify_title(tt.group(1))
            if not (t and a):
                t, a = parse_dash(tt.group(1))
            if t and a:
                logging.info("SPOT via <title>")
                return a, t
        return None

    endpoints = [
        f"https://open.spotify.com/track/{tid}?locale=en",
        f"https://open.spotify.com/embed/track/{tid}?utm_source=oembed"
    ]
    for ep in endpoints:
        try:
            r = await client.get(ep, timeout=15)
            r.raise_for_status()
            res = await try_from_html(r.text)
            if res:
                return res
        except Exception:
            continue

    raise RuntimeError("Не удалось извлечь из Spotify")

# ---------- Apple extract ----------
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
        m = re.match(r"^(.*?)\s*[-–—]\s*(.*?)\s*[-–—]\s*Apple\s*Music$", s, flags=re.I)
        if m: return clean_artist(m.group(2)), clean_title(m.group(1))
        if " — " in s:
            left, right = s.split(" — ", 1)
            return clean_artist(right), clean_title(left)

    # <title>
    tt = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
    if tt:
        s = tt.group(1)
        m = re.match(r"^(.*?)\s*-\s*(.*?)\s*-\s*Apple\s*Music$", s, flags=re.I)
        if m: return clean_artist(m.group(2)), clean_title(m.group(1))
        if "—" in s:
            left, right = s.split("—", 1)
            return clean_artist(right), clean_title(left)

    raise RuntimeError("Не удалось извлечь из Apple")

# ---------- search ----------
async def search_apple(client: httpx.AsyncClient, storefront: str, artist: str, track: str) -> str | None:
    q = f"{artist} {track}".strip()
    countries = [storefront.lower()] + (["us"] if storefront.lower() != "us" else [])
    for country in countries:
        try:
            term = httpx.QueryParams({'term': q})['term']
            data = await get_json(client, f"https://itunes.apple.com/search?media=music&entity=song&limit=10&country={country}&term={term}")
            if data.get("results"):
                url = data["results"][0].get("trackViewUrl") or data["results"][0].get("collectionViewUrl")
                if url: return url
        except Exception:
            pass
    # запасной веб-поиск
    try:
        term = httpx.QueryParams({'term': q})['term']
        html = await get_text(client, f"https://music.apple.com/{storefront.lower()}/search?term={term}")
        m = re.search(r'https://music\.apple\.com/[a-z]{2}/[^"]*/song/[^"]+/\d+', html, flags=re.I)
        if m: return m.group(0)
    except Exception:
        pass
    return None

async def search_spotify(client: httpx.AsyncClient, artist: str, track: str) -> str | None:
    artist_q = norm_for_query(artist)
    track_q  = norm_for_query(track)

    async def ddg(q: str) -> str | None:
        qp = httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']
        html = await get_text(client, f"https://html.duckduckgo.com/html/?q={qp}")
        links = re.findall(r'href="([^"]+)"', html)
        from urllib.parse import urlparse, parse_qs, unquote
        def decode(h: str) -> str:
            try:
                qs = parse_qs(urlparse(h).query)
                return unquote(qs.get('uddg', [''])[0]) or h
            except Exception:
                return h
        for h in map(decode, links):
            if re.match(r"^https?://open\.spotify\.com/track/", h, flags=re.I):
                return h
        return None

    async def brave(q: str) -> str | None:
        html = await get_text(client, f"https://search.brave.com/search?q={httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']}")
        m = re.search(r'href="(https?://open\.spotify\.com/track/[A-Za-z0-9]+)"', html, flags=re.I)
        return m.group(1) if m else None

    async def bing(q: str) -> str | None:
        html = await get_text(client, f"https://www.bing.com/search?q={httpx.QueryParams({'q': 'site:open.spotify.com/track ' + q})['q']}")
        m = re.search(r'href="(https?://open\.spotify\.com/track/[A-Za-z0-9]+)"', html, flags=re.I)
        return m.group(1) if m else None

    for q in (f"{artist_q} {track_q}", f"{track_q} {artist_q}"):
        for fn in (ddg, brave, bing):
            try:
                link = await fn(q)
                if link:
                    return link
            except Exception:
                continue
    return None

# ---------- convert ----------
async def convert_inline(url: str, storefront: str = DEFAULT_STOREFRONT) -> str | None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent":"Mozilla/5.0",
                 "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language":"en"},
        trust_env=True
    ) as client:
        if "open.spotify.com/track/" in url:
            artist, track = await extract_from_spotify(client, url)
            return await search_apple(client, storefront, artist, track)

        if "music.apple.com/" in url:
            # Нормализуем альбомные ссылки с ?i=<trackId>
            norm = await normalize_apple_album_i(client, url, storefront)
            if norm:
                url = norm
            artist, track = await extract_from_apple(client, url)
            artist, track = clean_artist(artist), clean_title(track)
            return await search_spotify(client, artist, track)

    return None

# ---------- bot ----------
dp = Dispatcher()

@dp.message(F.text == "/id")
async def cmd_id(msg: Message):
    await msg.answer(str(msg.from_user.id))

@dp.inline_query()
async def on_inline(q: InlineQuery):
    text = (q.query or "").strip()
    m = re.search(r"(https?://\S+)", text)

    if not m:
        return await q.answer([InlineQueryResultArticle(
            id="help",
            title="Вставьте ссылку на ПЕСНЮ",
            description="Apple /song/ или /album?...&i=<id>, а также Spotify /track/",
            input_message_content=InputTextMessageContent(
                message_text="Поддерживаются ссылки на песню: Apple и Spotify"
            ),
        )], cache_time=1, is_personal=True)

    url = m.group(1)
    if ("music.apple.com" in url and not is_apple_track_url(url)) or \
       ("open.spotify.com" in url and "/track/" not in url):
        return await q.answer([InlineQueryResultArticle(
            id="onlysong",
            title="Нужна ссылка на ПЕСНЮ",
            description="Альбом/плейлист без ?i= не поддерживается",
            input_message_content=InputTextMessageContent(
                message_text="Используйте ссылку на ПЕСНЮ: Apple /song/ ИЛИ /album?...&i=<trackId>, либо Spotify /track/."
            ),
        )], cache_time=1, is_personal=True)

    try:
        link = await asyncio.wait_for(convert_inline(url, DEFAULT_STOREFRONT), timeout=INLINE_TIMEOUT)
        if link:
            return await q.answer([InlineQueryResultArticle(
                id="ok",
                title="Готово — открыть ссылку",
                description=link,
                input_message_content=InputTextMessageContent(message_text=link),
            )], cache_time=0, is_personal=True)
        else:
            return await q.answer([InlineQueryResultArticle(
                id="nf",
                title="Не найдено",
                description="Попробуйте другой storefront или пришлите ссылку боту в ЛС",
                input_message_content=InputTextMessageContent(
                    message_text="Не найдено. Попробуйте другой storefront или пришлите ссылку боту в личные сообщения."
                ),
            )], cache_time=0, is_personal=True)
    except asyncio.TimeoutError:
        return await q.answer([InlineQueryResultArticle(
            id="to",
            title="Медленно отвечает — попробуйте ещё раз",
            description="Поиск занял слишком долго",
            input_message_content=InputTextMessageContent(
                message_text="Поиск занял слишком долго. Попробуйте ещё раз."
            ),
        )], cache_time=0, is_personal=True)
    except Exception as e:
        logging.exception("inline error")
        return await q.answer([InlineQueryResultArticle(
            id="err",
            title="Ошибка",
            description=str(e),
            input_message_content=InputTextMessageContent(message_text="Ошибка: " + str(e)),
        )], cache_time=0, is_personal=True)

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token or ":" not in token:
        raise RuntimeError("BOT_TOKEN не задан или некорректен")
    bot = Bot(token)
    print("Bot started. Inline only.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
