import asyncio
import difflib
import html
import logging
import os
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re
import uuid
from urllib.request import Request, urlopen

from PIL import Image
import aiohttp
import yt_dlp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден.\n\n"
        "В PowerShell выполни:\n"
        '$env:BOT_TOKEN="ТВОЙ_НОВЫЙ_ТОКЕН"'
    )

DOWNLOAD_DIR = "downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ============================================================
# BOT
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# ХРАНИЛИЩЕ КНОПОК
# ============================================================

callback_data_store = {}


# ============================================================
# ХРАНИЛИЩЕ СТАРЫХ СООБЩЕНИЙ БОТА
#
# Здесь храним только сообщения, которые можно удалить.
# Музыкальные сообщения сюда НЕ добавляем.
# ============================================================

last_bot_messages = {}

# Режим следующего запроса пользователя.
user_modes = {}


async def remember_bot_message(
    message: Message,
):
    chat_id = message.chat.id

    last_bot_messages.setdefault(
        chat_id,
        []
    )

    last_bot_messages[chat_id].append(
        message.message_id
    )


async def delete_old_bot_messages(
    chat_id: int,
):
    message_ids = last_bot_messages.get(
        chat_id,
        []
    )

    for message_id in message_ids:

        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

        except Exception:
            pass

    last_bot_messages[chat_id] = []


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def escape(text: str) -> str:
    return html.escape(
        str(text or "")
    )


def clean_title(title: str) -> str:
    """
    Чистит только технический мусор в названии, но НЕ удаляет
    признаки версии трека: Remix, Slowed, Reverb, Sped Up, Live,
    Extended и т.п. Они важны для точного выбора песни.
    """
    title = str(title or "").strip()

    title = re.sub(
        r"(?i)\b(official\s+music\s+video|official\s+video|official\s+audio|\blyrics?\b|\bvisualizer\b|\bhd\b|\b4k\b)",
        " ",
        title,
    )

    # Убираем хэштеги и служебные обрезки (#shorts, #tiktok и т.п.)
    title = re.sub(r"#\w+", " ", title)

    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"\s+([)\]])", r"\1", title)
    title = re.sub(r"([(\[])[ ]+", r"\1", title)

    return title.strip(" -_")


def is_url(text: str) -> bool:

    return bool(
        re.match(
            r"^https?://",
            text,
            re.IGNORECASE,
        )
    )


# ============================================================
# ПОДБОР ЛУЧШЕГО СОВПАДЕНИЯ (АНТИ-ПРОМАХ)
#
# Раньше бот брал ПЕРВЫЙ результат поиска YouTube не глядя.
# Из-за этого при неоднозначных запросах скачивался чужой
# трек / обрывок / кавер. Теперь берём несколько кандидатов
# и оцениваем, какой из них реально похож на запрос.
# ============================================================

def normalize_for_match(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def score_candidate(
    query: str,
    candidate: dict,
    prefer_min_duration: int = 90,
) -> float:
    """
    Считает, насколько кандидат похож на запрос.
    Учитывает: схожесть текста, вхождение всех слов запроса,
    и штрафует слишком короткие ролики (обрывки/шортсы).
    """
    q_norm = normalize_for_match(query)

    title_norm = normalize_for_match(candidate.get("title") or "")
    artist_norm = normalize_for_match(candidate.get("artist") or "")
    combined_norm = normalize_for_match(
        f"{candidate.get('artist', '')} {candidate.get('title', '')}"
    )

    title_score = difflib.SequenceMatcher(None, q_norm, title_norm).ratio()
    combined_score = difflib.SequenceMatcher(None, q_norm, combined_norm).ratio()

    score = max(title_score, combined_score)

    # Бонус, если все слова запроса нашлись у кандидата.
    q_words = set(q_norm.split())
    cand_words = set((title_norm + " " + artist_norm).split())
    if q_words and q_words.issubset(cand_words):
        score += 0.15

    try:
        duration = int(candidate.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0

    if duration and duration < prefer_min_duration:
        # Похоже на обрывок/шортс/тизер — сильный штраф.
        score -= 0.4
    elif duration and duration > 7 * 60:
        # Слишком длинное (микс/подборка) — небольшой штраф.
        score -= 0.05

    return score


def rank_candidates(
    query: str,
    candidates: list,
    prefer_min_duration: int = 90,
) -> list:
    scored = [
        (score_candidate(query, c, prefer_min_duration), c)
        for c in candidates
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored]


def looks_like_short_clip(url: str, duration) -> bool:
    """Определяет обрывок не только по длительности, но и по типу ссылки."""
    url_l = str(url or "").lower()

    if any(
        marker in url_l
        for marker in (
            "/shorts/",
            "/clip/",
            "tiktok.com",
            "instagram.com/reel",
            "instagram.com/p/",
        )
    ):
        return True

    try:
        duration = int(duration or 0)
    except (TypeError, ValueError):
        duration = 0

    return bool(duration and duration < 120)


def search_candidates_sync(query: str, count: int = 48) -> list:
    """Быстрый (без скачивания) поиск нескольких кандидатов на YouTube."""
    query = str(query or "").strip()
    if not query:
        return []

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "playlistend": count,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)

    results = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue

        try:
            duration = int(entry.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0

        item_url = entry.get("webpage_url") or entry.get("original_url")
        if not item_url and entry.get("id"):
            item_url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not item_url:
            continue

        results.append({
            "url": item_url,
            "title": entry.get("title") or query,
            "artist": (
                entry.get("artist")
                or entry.get("uploader")
                or entry.get("channel")
                or ""
            ),
            "duration": duration,
        })

    unique = []
    seen = set()
    for item in results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)

    return unique


# ============================================================
# СОХРАНЕНИЕ CALLBACK
# ============================================================

def save_callback(
    data: dict,
) -> str:

    key = uuid.uuid4().hex[:16]

    callback_data_store[key] = data

    return key


# ============================================================
# ITUNES API
# ============================================================

async def fetch_itunes_info(
    query: str,
    entity: str = "song",
    limit: int = 1,
):

    url = "https://itunes.apple.com/search"

    params = {
        "term": query,
        "entity": entity,
        "limit": limit,
    }

    try:

        timeout = aiohttp.ClientTimeout(
            total=15
        )

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                params=params,
            ) as response:

                if response.status != 200:

                    logger.warning(
                        "iTunes HTTP %s",
                        response.status,
                    )

                    return []

                data = await response.json(
                    content_type=None
                )

                return data.get(
                    "results",
                    []
                )

    except Exception as e:

        logger.exception(
            "Ошибка iTunes: %s",
            e,
        )

        return []


# ============================================================
# КЛАВИАТУРА ТРЕКА
# ============================================================

def song_info_keyboard(
    artist: str,
    track_name: str,
    source_url: str | None = None,
    variants: list | None = None,
):
    buttons = []

    info_key = save_callback({
        "type": "info",
        "artist": artist,
        "track": track_name,
        "source_url": source_url,
    })
    buttons.append([InlineKeyboardButton(
        text="ℹ️ Информация о треке",
        callback_data=f"info:{info_key}",
    )])

    if variants and len(variants) > 1:
        variant_key = save_callback({
            "type": "variants",
            "items": variants,
        })
        buttons.append([InlineKeyboardButton(
            text="🔀 Другие варианты",
            callback_data=f"variants:{variant_key}",
        )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ============================================================
# КЛАВИАТУРА ИСПОЛНИТЕЛЯ
# ============================================================

def artist_keyboard(
    artist_name: str,
    tracks: list,
):

    buttons = []

    for track in tracks:

        track_name = track.get(
            "trackName",
            "Неизвестный трек",
        )

        artist = track.get(
            "artistName",
            artist_name,
        )

        key = save_callback(
            {
                "type": "download",
                "query": f"{artist} - {track_name}",
            }
        )

        album = str(track.get("collectionName") or "").strip()
        ms = track.get("trackTimeMillis")
        duration = format_duration(ms / 1000) if ms else "—"

        # Подробная кнопка выбора: название + исполнитель + альбом + длительность.
        first = f"▸ {track_name}"
        second = f"└ {artist}"
        if album:
            second += f" · {album}"
        if duration != "—":
            second += f" · {duration}"

        label = f"{first[:42]}\n{second[:45]}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"download:{key}",
                )
            ]
        )

    artist_key = save_callback(
        {
            "type": "artist",
            "artist": artist_name,
        }
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="👤 Информация об исполнителе",
                callback_data=f"artist:{artist_key}",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# ============================================================
# ПОЛУЧЕНИЕ ДАННЫХ ПО ССЫЛКЕ
# ============================================================

def format_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"

    if seconds <= 0:
        return "—"

    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"



def search_results_keyboard(items: list, page: int = 0):
    """Постраничный список результатов: по 8 треков на страницу."""
    per_page = 8
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    visible = items[start:start + per_page]

    buttons = []
    for local_index, item in enumerate(visible, start + 1):
        title = clean_title(item.get("title") or "Без названия")
        artist = str(item.get("artist") or "").strip()
        duration = format_duration(item.get("duration"))

        # Более аккуратный двухстрочный дизайн.
        line1 = f"{local_index:02d}  {title}"
        line2 = artist or "Неизвестный исполнитель"
        if duration != "—":
            line2 += f"  ·  {duration}"

        key = save_callback({
            "type": "download",
            "query": item.get("url"),
            "title": title,
        })
        buttons.append([InlineKeyboardButton(
            text=f"{line1[:38]}\n{line2[:38]}",
            callback_data=f"download:{key}"
        )])

    # Навигация появляется только если результатов больше одной страницы.
    if total_pages > 1:
        nav = []
        if page > 0:
            prev_key = save_callback({"type": "search_page", "items": items, "page": page - 1})
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"page:{prev_key}"))

        # Небольшой ряд номеров страниц. Показываем максимум 5 кнопок.
        page_window = list(range(total_pages))
        if total_pages > 5:
            left = max(0, min(page - 2, total_pages - 5))
            page_window = list(range(left, left + 5))
        for pno in page_window:
            pkey = save_callback({"type": "search_page", "items": items, "page": pno})
            text = f"· {pno + 1} ·" if pno == page else str(pno + 1)
            nav.append(InlineKeyboardButton(text=text, callback_data=f"page:{pkey}"))

        if page < total_pages - 1:
            next_key = save_callback({"type": "search_page", "items": items, "page": page + 1})
            nav.append(InlineKeyboardButton(text="›", callback_data=f"page:{next_key}"))

        buttons.append(nav)

    # Небольшая служебная строка вместо перегруженных кнопок.
    if total_pages > 1:
        info_key = save_callback({"type": "search_page", "items": items, "page": page})
        buttons.append([InlineKeyboardButton(
            text=f"📄 Страница {page + 1} из {total_pages} · всего {len(items)}",
            callback_data=f"page:{info_key}"
        )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def url_choice_keyboard(items: list):
    buttons = []
    for index, item in enumerate(items[:10], 1):
        title = str(item.get("title") or "Без названия")
        duration = format_duration(item.get("duration"))
        label = f"🎵 {index}. {title[:42]}"
        if duration != "—":
            label += f" · {duration}"
        key = save_callback({
            "type": "url_download",
            "url": item.get("url"),
            "title": title,
        })
        buttons.append([
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"urlpick:{key}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _search_full_version_sync(title: str, artist: str, min_duration: int = 120):
    """
    Ищет полноценные версии короткого источника (обрывка/шортса) на YouTube.
    Название сначала чистится от мусора (эмодзи, хэштеги, "official video"
    и т.п.), иначе поиск получает грязный запрос и находит что попало.
    Результаты ранжируются по схожести, а не берётся первый попавшийся.
    """
    title = clean_title(title)
    artist = str(artist or "").strip()
    query = " ".join(x for x in (artist, title) if x)
    if not query:
        return []

    candidates = search_candidates_sync(query, count=10)

    # Отбрасываем то, что явно короче обычной песни.
    candidates = [
        c for c in candidates
        if not c.get("duration") or c["duration"] >= min_duration
    ]

    return rank_candidates(query, candidates, prefer_min_duration=min_duration)



def is_tiktok_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower().split(":", 1)[0]
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def resolve_url_sync(url: str) -> str:
    """Разворачивает короткую ссылку (vt.tiktok.com и т.п.) без стороннего сервиса."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=15) as response:
            return response.geturl() or url
    except Exception:
        return url


def fetch_tiktok_oembed_sync(url: str) -> dict:
    """Берёт публичные метаданные TikTok через официальный oEmbed."""
    import json
    from urllib.parse import quote

    resolved = resolve_url_sync(url)
    api_url = "https://www.tiktok.com/oembed?url=" + quote(resolved, safe="")
    req = Request(
        api_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
    )
    with urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def tiktok_music_query_from_oembed(data: dict) -> str:
    """Пытается вытащить название звука из HTML oEmbed, затем запасной вариант — title."""
    embed_html = str(data.get("html") or "")
    match = re.search(r"♬\s*([^<]+)", embed_html)
    if match:
        sound = html.unescape(match.group(1)).strip()
        sound = re.sub(r"\s+", " ", sound)
        if sound and sound.lower() not in {"original sound", "оригинальный звук"}:
            return sound

    title = html.unescape(str(data.get("title") or "")).strip()
    author = html.unescape(str(data.get("author_name") or "")).strip()
    title = re.sub(r"#\w+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return " ".join(x for x in (title, author) if x)


def extract_url_choices_sync(url: str):
    """Получает ссылку и превращает её в до 8 вариантов для выбора."""
    def base_opts(noplaylist: bool):
        return {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": noplaylist,
            "extract_flat": True,
            "playlistend": 20,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                )
            },
        }

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    is_youtube_video = (
        "youtube.com" in parsed.netloc.lower() and "v" in query
    ) or (
        "youtu.be" in parsed.netloc.lower() and bool(parsed.path.strip("/"))
    )

    exact_url = url
    if is_youtube_video and "v" in query:
        clean_query = {k: v for k, v in query.items() if k != "list"}
        exact_url = urlunparse((
            parsed.scheme, parsed.netloc, parsed.path, parsed.params,
            urlencode(clean_query, doseq=True), parsed.fragment,
        ))

    # TikTok: сначала пытаемся получить нормальный URL и метаданные.
    # Это важно, потому что vt.tiktok.com — короткая ссылка, а yt-dlp
    # периодически ломается именно на TikTok extraction.
    if is_tiktok_url(url):
        try:
            resolved_url = resolve_url_sync(url)
            with yt_dlp.YoutubeDL(base_opts(True)) as ydl:
                exact_info = ydl.extract_info(resolved_url, download=False)

            if exact_info:
                exact_item = {
                    "url": exact_info.get("webpage_url") or resolved_url,
                    "title": exact_info.get("title") or "TikTok",
                    "artist": (
                        exact_info.get("artist")
                        or exact_info.get("creator")
                        or exact_info.get("uploader")
                        or exact_info.get("channel")
                        or ""
                    ),
                    "duration": exact_info.get("duration"),
                }
                music_query = " ".join(
                    x for x in (
                        exact_info.get("track"),
                        exact_info.get("artist"),
                        exact_info.get("title"),
                    ) if x
                )
            else:
                raise RuntimeError("TikTok metadata is empty")
        except Exception:
            # Официальный TikTok oEmbed — fallback без стороннего сервиса.
            data = fetch_tiktok_oembed_sync(url)
            resolved_url = resolve_url_sync(url)
            music_query = tiktok_music_query_from_oembed(data)
            exact_item = {
                "url": resolved_url,
                "title": data.get("title") or "TikTok",
                "artist": data.get("author_name") or "",
                "duration": None,
            }

        # Ищем полноценные музыкальные версии по данным TikTok.
        if music_query:
            versions = search_candidates_sync(music_query, count=10)
            versions = rank_candidates(
                music_query,
                versions,
                prefer_min_duration=90,
            )
            if versions:
                return {
                    "source": exact_item,
                    "full_versions": versions[:8],
                }

        return {"source": exact_item, "full_versions": []}

    # Остальные URL обрабатываем обычным yt-dlp.
    with yt_dlp.YoutubeDL(base_opts(True)) as ydl:
        exact_info = ydl.extract_info(exact_url, download=False)

    if not exact_info:
        raise RuntimeError("По ссылке ничего не найдено.")

    if not exact_info.get("entries"):
        exact_item = {
            "url": exact_info.get("webpage_url") or exact_url,
            "title": exact_info.get("title") or "Без названия",
            "artist": (
                exact_info.get("artist")
                or exact_info.get("creator")
                or exact_info.get("uploader")
                or exact_info.get("channel")
                or "Неизвестный исполнитель"
            ),
            "duration": exact_info.get("duration"),
        }

        if looks_like_short_clip(exact_url, exact_item.get("duration")):
            full_versions = _search_full_version_sync(
                exact_item["title"], exact_item["artist"]
            )
            if full_versions:
                return {
                    "source": exact_item,
                    "full_versions": full_versions[:8],
                }

        return {"source": exact_item, "full_versions": []}

    with yt_dlp.YoutubeDL(base_opts(False)) as ydl:
        info = ydl.extract_info(url, download=False)

    items = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        item_url = entry.get("webpage_url") or entry.get("original_url")
        if not item_url and entry.get("id"):
            extractor_key = str(info.get("extractor_key", "")).lower()
            if "youtube" in extractor_key:
                item_url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not item_url:
            continue
        items.append({
            "url": item_url,
            "title": entry.get("title") or "Без названия",
            "artist": (
                entry.get("artist")
                or entry.get("creator")
                or entry.get("uploader")
                or entry.get("channel")
                or "Неизвестный исполнитель"
            ),
            "duration": entry.get("duration"),
        })

    if not items:
        raise RuntimeError("В этой ссылке не найдено доступных треков.")
    return {"source": None, "full_versions": items[:8]}

def variant_display_name(title: str) -> str:
    title = str(title or "Без названия")
    low = title.lower()
    for marker, label in [
        ("slowed + reverb", "🌙 Slowed + Reverb"),
        ("slowed reverb", "🌙 Slowed + Reverb"),
        ("slowed", "🌙 Slowed"),
        ("sped up", "⚡ Sped Up"),
        ("speed up", "⚡ Sped Up"),
        ("remix", "🎚 Remix"),
        ("extended", "⏳ Extended"),
        ("live", "🎤 Live"),
        ("acoustic", "🎸 Acoustic"),
        ("instrumental", "🎹 Instrumental"),
        ("karaoke", "🎙 Karaoke"),
    ]:
        if marker in low:
            return label
    return "🎵 Original"


def variants_keyboard(items: list):
    buttons = []
    for index, item in enumerate(items[:8], 1):
        title = str(item.get("title") or "Без названия")
        duration = format_duration(item.get("duration"))
        label = f"{variant_display_name(title)} · {index}"
        if duration != "—":
            label += f" · {duration}"
        key = save_callback({"type": "url_download", "url": item.get("url"), "title": title})
        buttons.append([InlineKeyboardButton(text=label[:64], callback_data=f"urlpick:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def download_thumbnail_sync(url: str, unique_id: str):
    """Скачивает обложку и делает JPEG до 320x320 и 200 КБ."""
    if not url:
        return None
    path = os.path.join(DOWNLOAD_DIR, f"{unique_id}_cover.jpg")
    raw = os.path.join(DOWNLOAD_DIR, f"{unique_id}_cover_raw")
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as response:
            data = response.read(5 * 1024 * 1024)
        with open(raw, "wb") as f:
            f.write(data)
        with Image.open(raw) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320), Image.Resampling.LANCZOS)

            # Мрачная, но аккуратная стилизация обложки.
            # Текст/название трека не изменяем — меняем только изображение.
            from PIL import ImageEnhance, ImageFilter, ImageDraw
            import random

            image = ImageEnhance.Contrast(image).enhance(1.12)
            image = ImageEnhance.Brightness(image).enhance(0.70)
            image = ImageEnhance.Color(image).enhance(0.82)
            image = image.convert("RGBA")
            w, h = image.size

            # Чёрная дымка поверх изображения.
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 62))
            image = Image.alpha_composite(image, overlay)

            # Едва заметный холодный/красный тон по краям.
            tint = Image.new("RGBA", image.size, (90, 0, 12, 0))
            tint.putalpha(24)
            image = Image.alpha_composite(image, tint)

            # Виньетка.
            vignette = Image.new("L", image.size, 0)
            draw = ImageDraw.Draw(vignette)
            draw.ellipse((-int(w * 0.20), -int(h * 0.20), int(w * 1.20), int(h * 1.20)), fill=205)
            vignette = vignette.filter(ImageFilter.GaussianBlur(max(10, min(w, h) // 9)))
            dark = Image.new("RGBA", image.size, (0, 0, 0, 145))
            dark.putalpha(vignette.point(lambda x: 145 - x // 2))
            image = Image.alpha_composite(image, dark)

            # Лёгкое зерно для creepy-визуала, без перегруза.
            grain = Image.new("RGBA", image.size, (0, 0, 0, 0))
            pixels = grain.load()
            step = max(2, min(w, h) // 120)
            for y in range(0, h, step):
                for x in range(0, w, step):
                    if random.random() < 0.18:
                        a = random.randint(8, 24)
                        pixels[x, y] = (255, 255, 255, a)
            grain = grain.filter(ImageFilter.GaussianBlur(0.35))
            image = Image.alpha_composite(image, grain)

            # Тонкая красная рамка/акцент.
            draw = ImageDraw.Draw(image)
            draw.rectangle((2, 2, w - 3, h - 3), outline=(115, 0, 15, 125), width=2)
            draw.line((0, h - 3, w, h - 3), fill=(135, 0, 18, 165), width=2)

            image.convert("RGB").save(path, "JPEG", quality=78, optimize=True)
        try: os.remove(raw)
        except OSError: pass
        if os.path.getsize(path) > 200 * 1024:
            with Image.open(path) as image:
                for quality in (65, 55, 45, 35):
                    image.save(path, "JPEG", quality=quality, optimize=True)
                    if os.path.getsize(path) <= 200 * 1024:
                        break
        return path if os.path.exists(path) and os.path.getsize(path) <= 200 * 1024 else None
    except Exception:
        for x in (path, raw):
            try: os.remove(x)
            except OSError: pass
        return None


async def show_url_choices(status_message: Message, url: str):
    """Для ссылки сначала показывает найденные варианты, а не скачивает первый."""
    try:
        result = await asyncio.to_thread(extract_url_choices_sync, url)
        source = result.get("source")
        versions = result.get("full_versions") or []

        items = versions[:8]
        if not items and source:
            items = [source]

        if not items:
            raise RuntimeError("По ссылке ничего не найдено.")

        await status_message.edit_text(
            "╭─ 🔎 <b>НАЙДЕННЫЕ ТРЕКИ</b>\n╰─ <i>Выбери подходящий вариант</i>",
            parse_mode="HTML",
            reply_markup=search_results_keyboard(items),
        )

    except Exception as e:
        logger.exception("Ошибка обработки ссылки: %s", e)
        try:
            await status_message.edit_text(
                "❌ Не удалось распознать музыку по этой ссылке.\n\n"
                "Если это TikTok, попробуй отправить ссылку ещё раз или название трека."
            )
        except Exception:
            pass


# ============================================================
# СКАЧИВАНИЕ БЕЗ FFMPEG
# ============================================================

def download_audio_sync(query: str):
    """Надёжно скачивает выбранный трек.

    Важно: сначала пробуем именно URL выбранной кнопки. Если YouTube
    блокирует конкретный формат/клиент, пробуем несколько клиентов и
    затем резервный поиск по названию.
    """
    unique_id = uuid.uuid4().hex
    output_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        )
    }

    def make_opts(client=None):
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": headers,
            "max_filesize": 50 * 1024 * 1024,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "ignoreerrors": False,
        }
        if client:
            opts["extractor_args"] = {
                "youtube": {"player_client": [client]}
            }
        return opts

    auto_variants = []
    download_target = query

    # Для текстового запроса выбираем лучший результат поиска.
    if not is_url(query):
        candidates = search_candidates_sync(query, count=12)
        ranked = rank_candidates(query, candidates, prefer_min_duration=90)
        if not ranked:
            raise RuntimeError("Ничего не найдено.")
        download_target = ranked[0]["url"]
        auto_variants = ranked[:8]

    info = None
    file_path = None
    last_error = None

    # Несколько вариантов клиента YouTube. Это особенно важно сейчас:
    # yt-dlp документирует изменения с PO Token и доступностью форматов.
    clients = ["android_vr", "web_embedded", None]
    for client in clients:
        try:
            with yt_dlp.YoutubeDL(make_opts(client)) as ydl:
                info = ydl.extract_info(download_target, download=True)
            if info:
                break
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Не удалось скачать %s через client=%s: %s",
                download_target, client, exc
            )

    # Если выбранный URL не скачался, пытаемся найти тот же трек по метаданным.
    if not info and is_url(query):
        fallback_title = ""
        try:
            with yt_dlp.YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "http_headers": headers,
            }) as meta_ydl:
                meta = meta_ydl.extract_info(query, download=False)
                if meta:
                    fallback_title = " ".join(
                        x for x in (
                            meta.get("artist"),
                            meta.get("creator"),
                            meta.get("track"),
                            meta.get("title"),
                        ) if x
                    )
        except Exception as exc:
            logger.warning("Не удалось получить метаданные URL: %s", exc)

        if fallback_title:
            fallback_candidates = search_candidates_sync(fallback_title, count=12)
            fallback_ranked = rank_candidates(
                fallback_title, fallback_candidates, prefer_min_duration=90
            )
            for candidate in fallback_ranked[:8]:
                for client in clients:
                    try:
                        with yt_dlp.YoutubeDL(make_opts(client)) as ydl:
                            info = ydl.extract_info(candidate["url"], download=True)
                        if info:
                            download_target = candidate["url"]
                            auto_variants = fallback_ranked[:8]
                            break
                    except Exception as exc:
                        last_error = exc
                if info:
                    break

    if not info:
        raise RuntimeError(
            f"Не удалось скачать выбранный трек. Последняя ошибка: {last_error}"
        )

    if "entries" in info:
        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError("YouTube не вернул файл трека.")
        info = entries[0]

    title = info.get("title") or "Неизвестный трек"

    # Ищем фактически созданный файл, не полагаясь только на расширение.
    possible = []
    try:
        prepared = yt_dlp.YoutubeDL(make_opts()).prepare_filename(info)
        possible.append(prepared)
    except Exception:
        pass

    for candidate_path in possible:
        if os.path.exists(candidate_path):
            file_path = candidate_path
            break

    if not file_path:
        prefix = unique_id
        for filename in os.listdir(DOWNLOAD_DIR):
            if filename.startswith(prefix):
                candidate_path = os.path.join(DOWNLOAD_DIR, filename)
                if os.path.isfile(candidate_path):
                    file_path = candidate_path
                    break

    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError("yt-dlp сообщил об успехе, но файл не найден.")

    return file_path, title, info, auto_variants



# ============================================================
# СКАЧАТЬ И ОТПРАВИТЬ
# ============================================================

async def download_and_send(
    target_message: Message,
    query: str,
    status_message: Message | None = None,
    variants: list | None = None,
):

    file_path = None
    cover_path = None

    try:

        if status_message:

            await status_message.edit_text(
                "⏳ Загружаю трек..."
            )

        (
            file_path,
            title,
            info,
            auto_variants,
        ) = await asyncio.to_thread(
            download_audio_sync,
            query,
        )

        # Если варианты не передали явно (например, при скачивании по
        # точной ссылке из кнопки) — используем то, что нашли автоматически
        # при текстовом поиске.
        final_variants = variants if variants else auto_variants

        clean_name = clean_title(
            title
        )

        performer = (
            info.get("artist")
            or info.get("uploader")
            or "Неизвестный исполнитель"
        )

        album = info.get("album") or info.get("playlist")
        release_date = info.get("release_date") or info.get("upload_date") or ""
        year = str(release_date)[:4] if release_date else ""
        genre = info.get("genre") or ""

        caption_lines = [
            f"🎵 <b>{escape(clean_name)}</b>",
            f"👤 {escape(performer)}",
        ]
        if album:
            caption_lines.append(f"💿 {escape(album)}")
        if year.isdigit():
            caption_lines.append(f"📅 {escape(year)}")
        if genre:
            caption_lines.append(f"🎸 {escape(genre)}")
        caption = "\n".join(caption_lines)

        thumbnail_url = info.get("thumbnail")
        if thumbnail_url:
            cover_path = await asyncio.to_thread(
                download_thumbnail_sync, thumbnail_url, uuid.uuid4().hex
            )

        # ВАЖНО:
        # Музыкальное сообщение не записываем
        # в last_bot_messages.
        #
        # Поэтому при следующем запросе музыка
        # останется в чате.

        await target_message.answer_audio(
            audio=FSInputFile(
                file_path
            ),
            caption=caption,
            parse_mode="HTML",
            title=clean_name[:64],
            performer=str(
                performer
            )[:64],
            thumbnail=(FSInputFile(cover_path) if cover_path else None),
            reply_markup=song_info_keyboard(
                str(performer),
                clean_name,
                source_url=(info.get("webpage_url") if is_url(query) else None),
                variants=final_variants,
            ),
        )

        # Удаляем временный статус.
        if status_message:

            try:

                await bot.delete_message(
                    chat_id=status_message.chat.id,
                    message_id=status_message.message_id,
                )

            except Exception:
                pass

            # Убираем его из списка.
            chat_id = status_message.chat.id

            if chat_id in last_bot_messages:

                if (
                    status_message.message_id
                    in last_bot_messages[chat_id]
                ):

                    last_bot_messages[
                        chat_id
                    ].remove(
                        status_message.message_id
                    )

        # Удаляем временные файлы. Музыка в Telegram остаётся.
        try:
            os.remove(file_path)
        except OSError:
            pass
        file_path = None
        if cover_path:
            try:
                os.remove(cover_path)
            except OSError:
                pass
            cover_path = None
    except Exception as e:

        logger.exception(
            "Ошибка скачивания: %s",
            e,
        )

        error_text = (
            "❌ Не удалось загрузить трек.\n\n"
            "Попробуй другое название "
            "или другую ссылку."
        )

        if status_message:

            try:

                await status_message.edit_text(
                    error_text
                )

            except Exception:
                pass

        else:

            error_message = (
                await target_message.answer(
                    error_text
                )
            )

            await remember_bot_message(
                error_message
            )

    finally:

        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass
        if cover_path:
            try:
                os.remove(cover_path)
            except OSError:
                pass


# ============================================================
# START
# ============================================================

@dp.message(
    Command("start")
)
async def cmd_start(
    message: Message,
):
    await delete_old_bot_messages(message.chat.id)
    user_modes[message.from_user.id] = "song"

    user_name = (
        message.from_user.first_name
        if message.from_user
        else "Ник"
    )

    welcome = await message.answer(
        f"Привет, <b>{escape(user_name)}</b> 👋",
        parse_mode="HTML",
    )

    await remember_bot_message(
        welcome
    )


@dp.callback_query(F.data.startswith("mode:"))
async def choose_mode(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    user_modes[callback.from_user.id] = mode
    await callback.answer()
    msg = await callback.message.answer(
        "👤 Напиши имя исполнителя." if mode == "artist"
        else "🎧 Напиши название песни или исполнителя."
    )
    await remember_bot_message(msg)


# ============================================================
# ОБРАБОТКА ТЕКСТА
# ============================================================

@dp.message(
    F.text
)
async def handle_text(
    message: Message,
):

    text = message.text.strip()

    if not text:
        return

    # Удаляем предыдущие сообщения бота.
    # Музыка НЕ удаляется.

    await delete_old_bot_messages(
        message.chat.id
    )

    # Удаляем сообщение пользователя,
    # чтобы чат был чистым.

    try:

        await message.delete()

    except Exception:
        pass

    # ========================================================
    # URL
    # ========================================================

    if is_url(text):

        status = await message.answer(
            "⏳ Анализирую ссылку и ищу трек..."
        )

        await remember_bot_message(
            status
        )

        await show_url_choices(
            status,
            text,
        )

        return

    # ========================================================
    # ПОИСК
    # ========================================================

    status = await message.answer(
        "🔎 Ищу..."
    )

    await remember_bot_message(
        status
    )

    # ========================================================
    # ПОИСК РЕЗУЛЬТАТОВ
    user_id = message.from_user.id
    mode = user_modes.get(user_id, "song")

    if mode == "artist":
        artist_results = await fetch_itunes_info(text, entity="musicArtist", limit=1)
        if artist_results:
            artist_name = artist_results[0].get("artistName", text)
            tracks = await fetch_itunes_info(artist_name, entity="song", limit=8)
            if tracks:
                await status.edit_text(
                    f"👤 <b>{escape(artist_name)}</b>\\n\\nВыбери трек:",
                    parse_mode="HTML",
                    reply_markup=artist_keyboard(artist_name, tracks),
                )
                return

    # Обычный поиск: показываем до 8 вариантов и ждём выбора.
    candidates = await asyncio.to_thread(search_candidates_sync, text, 48)
    ranked = rank_candidates(text, candidates, prefer_min_duration=90)

    if not ranked:
        await status.edit_text("😔 Ничего не найдено.")
        return

    await status.edit_text(
        "🎧 <b>Результаты поиска</b>\\n<i>Выбери нужный трек:</i>",
        parse_mode="HTML",
        reply_markup=search_results_keyboard(ranked[:48]),
    )



# ============================================================
# СКАЧИВАНИЕ ТОЧНОГО ЭЛЕМЕНТА ИЗ ССЫЛКИ
# ============================================================

@dp.callback_query(
    F.data.startswith("urlpick:")
)
async def download_url_item(
    callback: CallbackQuery,
):

    key = callback.data.split(":", 1)[1]
    data = callback_data_store.get(key)

    if not data:
        await callback.answer(
            "Кнопка устарела.",
            show_alert=True,
        )
        return

    url = data.get("url")
    title = data.get("title", "трек")

    if not url:
        await callback.answer(
            "Ссылка недоступна.",
            show_alert=True,
        )
        return

    await callback.answer("Загружаю именно выбранный материал...")

    await delete_old_bot_messages(
        callback.message.chat.id
    )

    status = await callback.message.answer(
        f"⏳ Загружаю:\n<b>{escape(title)}</b>",
        parse_mode="HTML",
    )

    await remember_bot_message(status)

    # Здесь передаётся ИМЕННО URL, выбранный пользователем.
    # yt-dlp не получает название для поиска.
    await download_and_send(
        callback.message,
        url,
        status_message=status,
    )


@dp.callback_query(
    F.data.startswith("variants:")
)
async def show_variants(
    callback: CallbackQuery,
):
    key = callback.data.split(":", 1)[1]
    data = callback_data_store.get(key)
    if not data:
        await callback.answer("Варианты устарели.", show_alert=True)
        return

    items = data.get("items") or []
    if not items:
        await callback.answer("Вариантов нет.", show_alert=True)
        return

    await callback.answer("Выбери нужную версию")
    await callback.message.answer(
        "🔀 <b>Другие варианты</b>\n\nВыбери трек:",
        parse_mode="HTML",
        reply_markup=variants_keyboard(items),
    )


# ============================================================
# ПЕРЕКЛЮЧЕНИЕ СТРАНИЦ ПОИСКА
# ============================================================

@dp.callback_query(F.data.startswith("page:"))
async def search_page(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    data = callback_data_store.get(key)
    if not data or data.get("type") != "search_page":
        await callback.answer("Страница устарела.", show_alert=True)
        return

    items = data.get("items") or []
    page = int(data.get("page") or 0)
    total_pages = max(1, (len(items) + 7) // 8)
    page = max(0, min(page, total_pages - 1))

    await callback.answer(f"Страница {page + 1} из {total_pages}")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=search_results_keyboard(items, page=page)
        )
    except Exception as e:
        logger.warning("Не удалось переключить страницу: %s", e)


# ============================================================
# СКАЧИВАНИЕ ВЫБРАННОГО ТРЕКА
# ============================================================

@dp.callback_query(
    F.data.startswith("download:")
)
async def download_selected_track(
    callback: CallbackQuery,
):

    key = callback.data.split(
        ":",
        1,
    )[1]

    data = callback_data_store.get(
        key
    )

    if not data:

        await callback.answer(
            "Кнопка устарела.",
            show_alert=True,
        )

        return

    query = data.get(
        "query"
    )

    await callback.answer(
        "Загружаю..."
    )

    # Удаляем старые сообщения бота,
    # но музыка остаётся.

    await delete_old_bot_messages(
        callback.message.chat.id
    )

    status = await callback.message.answer(
        f"⏳ Загружаю:\n"
        f"<b>{escape(query)}</b>",
        parse_mode="HTML",
    )

    await remember_bot_message(
        status
    )

    await download_and_send(
        callback.message,
        query,
        status_message=status,
    )


# ============================================================
# ИНФОРМАЦИЯ ОБ ИСПОЛНИТЕЛЕ
# ============================================================

@dp.callback_query(
    F.data.startswith("artist:")
)
async def show_artist_info(
    callback: CallbackQuery,
):

    key = callback.data.split(
        ":",
        1,
    )[1]

    data = callback_data_store.get(
        key
    )

    if not data:

        await callback.answer(
            "Кнопка устарела.",
            show_alert=True,
        )

        return

    artist_name = data.get(
        "artist"
    )

    await callback.answer()

    results = (
        await fetch_itunes_info(
            artist_name,
            entity="musicArtist",
            limit=1,
        )
    )

    if not results:

        msg = await callback.message.answer(
            "❌ Информация об исполнителе не найдена."
        )

        await remember_bot_message(
            msg
        )

        return

    artist = results[0]

    name = artist.get(
        "artistName",
        artist_name,
    )

    genre = artist.get(
        "primaryGenreName",
        "Не указан",
    )

    artist_url = artist.get(
        "artistLinkUrl"
    )

    text = (
        f"👤 <b>{escape(name)}</b>\n\n"
        f"🎸 Жанр: <b>{escape(genre)}</b>"
    )

    if artist_url:

        text += (
            f'\n\n<a href="{escape(artist_url)}">'
            "Apple Music</a>"
        )

    msg = await callback.message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await remember_bot_message(
        msg
    )


# ============================================================
# ИНФОРМАЦИЯ О ТРЕКЕ
# ============================================================

def get_track_info_from_url_sync(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info and info.get("entries"):
            entries = info.get("entries") or []
            return entries[0] if entries else None
        return info


async def send_track_info_message(message: Message, info: dict):
    title = clean_title(info.get("title") or "Неизвестный трек")
    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or info.get("channel")
        or "Неизвестный исполнитель"
    )
    album = info.get("album") or info.get("collection") or "Не указан"
    genre = info.get("genre") or "Не указан"
    duration = format_duration(info.get("duration"))
    date = info.get("release_date") or info.get("upload_date") or ""
    year = str(date)[:4] if date else "Неизвестно"
    channel = info.get("channel") or info.get("uploader")

    lines = [
        f"🎵 <b>{escape(title)}</b>",
        f"👤 {escape(artist)}",
        "",
        f"💿 Альбом: <b>{escape(album)}</b>",
        f"📅 Год: <b>{escape(year)}</b>",
        f"🎸 Жанр: <b>{escape(genre)}</b>",
        f"⏱ Длительность: <b>{escape(duration)}</b>",
    ]
    if channel and str(channel) != str(artist):
        lines.append(f"📺 Канал: <b>{escape(channel)}</b>")

    source = info.get("webpage_url")
    if source:
        lines.append(f'\n<a href="{escape(source)}">Источник</a>')

    thumbnail = info.get("thumbnail")
    text = "\n".join(lines)
    if thumbnail:
        try:
            msg = await message.answer_photo(photo=thumbnail, caption=text, parse_mode="HTML")
        except Exception:
            msg = await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        msg = await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await remember_bot_message(msg)


def get_track_info_sync(artist_query: str, track_query: str):
    """
    Получает информацию напрямую через yt-dlp.
    Это резервный вариант, если iTunes отвечает 403.
    FFmpeg не нужен.
    """
    query = f"{artist_query} - {track_query}".strip(" -")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"ytsearch1:{query}",
            download=False,
        )

        if not info:
            return None

        if "entries" in info:
            entries = info.get("entries") or []
            if not entries:
                return None
            info = entries[0]

        return info


@dp.callback_query(
    F.data.startswith("info:")
)
async def show_song_info(
    callback: CallbackQuery,
):

    key = callback.data.split(
        ":",
        1,
    )[1]

    data = callback_data_store.get(
        key
    )

    if not data:
        await callback.answer(
            "Кнопка устарела.",
            show_alert=True,
        )
        return

    artist_query = data.get(
        "artist",
        "",
    )

    track_query = data.get(
        "track",
        "",
    )

    await callback.answer(
        "Загружаю информацию..."
    )

    source_url = data.get("source_url")

    # Если музыка была скачана по ссылке, сначала читаем МЕТАДАННЫЕ
    # именно этого материала. Это исключает случай, когда поиск находит
    # другой трек с похожим названием.
    if source_url:
        try:
            exact = await asyncio.to_thread(get_track_info_from_url_sync, source_url)
            if exact:
                await send_track_info_message(callback.message, exact)
                return
        except Exception as e:
            logger.warning("Не удалось получить точную информацию по URL: %s", e)

    # Сначала пробуем iTunes.
    results = await fetch_itunes_info(
        f"{artist_query} {track_query}",
        entity="song",
        limit=1,
    )

    if not results:
        results = await fetch_itunes_info(
            track_query,
            entity="song",
            limit=1,
        )

    # Если iTunes вернул 403/пустой результат —
    # используем yt-dlp.
    if results:
        track = results[0]

        artist = track.get(
            "artistName",
            artist_query or "Неизвестный исполнитель",
        )

        track_name = track.get(
            "trackName",
            track_query or "Неизвестный трек",
        )

        album = track.get(
            "collectionName",
            "Не указан",
        )

        release_date = track.get(
            "releaseDate",
            "",
        )

        year = (
            release_date[:4]
            if release_date
            else "Неизвестно"
        )

        genre = track.get(
            "primaryGenreName",
            "Неизвестно",
        )

        duration = track.get(
            "trackTimeMillis"
        )

        duration_text = ""

        if duration:
            total_seconds = int(
                duration / 1000
            )

            minutes = total_seconds // 60
            seconds = total_seconds % 60

            duration_text = (
                f"\n⏱ Длительность: "
                f"<b>{minutes}:{seconds:02d}</b>"
            )

        text = (
            f"🎵 <b>{escape(track_name)}</b>\n"
            f"👤 {escape(artist)}\n\n"
            f"💿 Альбом: <b>{escape(album)}</b>\n"
            f"📅 Год: <b>{escape(year)}</b>\n"
            f"🎸 Жанр: <b>{escape(genre)}</b>"
            f"{duration_text}"
        )

        cover = track.get(
            "artworkUrl100"
        )

        if cover:
            cover = cover.replace(
                "100x100bb",
                "600x600bb",
            )

            msg = await callback.message.answer_photo(
                photo=cover,
                caption=text,
                parse_mode="HTML",
            )
        else:
            msg = await callback.message.answer(
                text,
                parse_mode="HTML",
            )

        await remember_bot_message(msg)
        return

    # ------------------------------------------------------------
    # РЕЗЕРВ: yt-dlp
    # ------------------------------------------------------------

    try:
        info = await asyncio.to_thread(
            get_track_info_sync,
            artist_query,
            track_query,
        )

        if not info:
            raise RuntimeError(
                "Трек не найден."
            )

        title = clean_title(
            info.get(
                "title",
                track_query or "Неизвестный трек",
            )
        )

        artist = (
            info.get("artist")
            or info.get("creator")
            or info.get("uploader")
            or artist_query
            or "Неизвестный исполнитель"
        )

        album = (
            info.get("album")
            or info.get("playlist")
            or "Не указан"
        )

        upload_date = info.get(
            "upload_date",
            "",
        )

        year = (
            upload_date[:4]
            if upload_date
            else "Неизвестно"
        )

        genre = (
            info.get("genre")
            or "Не указан"
        )

        duration = info.get(
            "duration"
        )

        duration_text = ""

        if duration:
            total_seconds = int(duration)
            minutes = total_seconds // 60
            seconds = total_seconds % 60

            duration_text = (
                f"\n⏱ Длительность: "
                f"<b>{minutes}:{seconds:02d}</b>"
            )

        uploader = info.get(
            "uploader"
        )

        text = (
            f"🎵 <b>{escape(title)}</b>\n"
            f"👤 {escape(artist)}\n\n"
            f"💿 Альбом: <b>{escape(album)}</b>\n"
            f"📅 Год: <b>{escape(year)}</b>\n"
            f"🎸 Жанр: <b>{escape(genre)}</b>"
            f"{duration_text}"
        )

        if uploader and str(uploader) != str(artist):
            text += (
                f"\n📺 Канал: <b>{escape(uploader)}</b>"
            )

        webpage_url = info.get(
            "webpage_url"
        )

        if webpage_url:
            text += (
                f'\n\n<a href="{escape(webpage_url)}">'
                "Источник</a>"
            )

        thumbnail = info.get(
            "thumbnail"
        )

        if thumbnail:
            try:
                msg = await callback.message.answer_photo(
                    photo=thumbnail,
                    caption=text,
                    parse_mode="HTML",
                )
            except Exception:
                msg = await callback.message.answer(
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        else:
            msg = await callback.message.answer(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        await remember_bot_message(msg)

    except Exception as e:
        logger.exception(
            "Ошибка получения информации о треке: %s",
            e,
        )

        msg = await callback.message.answer(
            "❌ Не удалось получить информацию о треке.\n\n"
            f"🎵 {escape(track_query)}\n"
            f"👤 {escape(artist_query)}",
            parse_mode="HTML",
        )

        await remember_bot_message(msg)


# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    logger.info(
        "Music Bot запущен."
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(main())
