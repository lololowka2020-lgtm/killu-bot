import asyncio
import html
import logging
import os
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re
import uuid

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

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🎵 {track_name[:40]}",
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


def _search_full_version_sync(title: str, artist: str):
    """Ищет полноценные версии короткого источника на YouTube."""
    title = str(title or "").strip()
    artist = str(artist or "").strip()
    query = " ".join(x for x in (artist, title) if x)
    if not query:
        return []

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "playlistend": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"ytsearch10:{query}",
            download=False,
        )

    results = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        try:
            duration = int(entry.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        # Отбрасываем Shorts/тизеры/обрывки. Обычная песня обычно > 2 минут.
        if duration and duration < 120:
            continue

        item_url = entry.get("webpage_url") or entry.get("original_url")
        if not item_url and entry.get("id"):
            item_url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not item_url:
            continue

        results.append({
            "url": item_url,
            "title": entry.get("title") or title,
            "artist": (
                entry.get("artist")
                or entry.get("uploader")
                or entry.get("channel")
                or artist
                or "Неизвестный исполнитель"
            ),
            "duration": duration,
        })

    # Убираем дубли по URL.
    unique = []
    seen = set()
    for item in results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique


def extract_url_choices_sync(url: str):
    """Получает именно ссылку и при необходимости ищет её полную версию."""
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

        try:
            source_duration = int(exact_item.get("duration") or 0)
        except (TypeError, ValueError):
            source_duration = 0

        # Короткие ссылки (TikTok/Shorts/тизеры) автоматически заменяем
        # на полноценный трек. Остальное скачиваем напрямую.
        if source_duration and source_duration < 120:
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
    return {"source": None, "full_versions": items}


def variants_keyboard(items: list):
    buttons = []
    for index, item in enumerate(items[:8], 1):
        title = str(item.get("title") or "Без названия")
        duration = format_duration(item.get("duration"))
        label = f"🎵 {index}. {title[:45]}"
        if duration != "—":
            label += f" · {duration}"
        key = save_callback({
            "type": "url_download",
            "url": item.get("url"),
            "title": title,
        })
        buttons.append([InlineKeyboardButton(
            text=label[:64], callback_data=f"urlpick:{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_url_choices(status_message: Message, url: str):
    """Без превью: сразу скачивает лучший полный вариант."""
    try:
        result = await asyncio.to_thread(extract_url_choices_sync, url)
        source = result.get("source")
        versions = result.get("full_versions") or []

        if source:
            # Для короткой ссылки берём лучший полноценный результат сразу.
            download_url = versions[0]["url"] if versions else source["url"]
            await download_and_send(
                status_message,
                download_url,
                status_message=status_message,
                variants=(versions if versions else []),
            )
            return

        # Плейлист: не показываем предпросмотр, а сразу скачиваем первый
        # найденный элемент и сохраняем остальные как варианты.
        if versions:
            await download_and_send(
                status_message,
                versions[0]["url"],
                status_message=status_message,
                variants=versions,
            )
            return

        raise RuntimeError("По ссылке ничего не найдено.")

    except Exception as e:
        logger.exception("Ошибка обработки ссылки: %s", e)
        try:
            await status_message.edit_text(
                "❌ Не удалось получить полноценный трек по этой ссылке.\n\n"
                "Попробуй другую ссылку на тот же трек."
            )
        except Exception:
            pass


# ============================================================
# СКАЧИВАНИЕ БЕЗ FFMPEG
# ============================================================

def download_audio_sync(
    query: str,
):

    unique_id = uuid.uuid4().hex

    output_template = os.path.join(
        DOWNLOAD_DIR,
        f"{unique_id}.%(ext)s",
    )

    ydl_opts = {
        "format": "bestaudio/best",

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },

        "max_filesize": 50 * 1024 * 1024,
    }

    if is_url(query):

        download_query = query

    else:

        download_query = (
            f"ytsearch1:{query}"
        )

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                download_query,
                download=True,
            )

            if not info:

                raise RuntimeError(
                    "Ничего не найдено."
                )

            if "entries" in info:

                entries = info.get(
                    "entries"
                )

                if not entries:

                    raise RuntimeError(
                        "Ничего не найдено."
                    )

                info = entries[0]

            title = info.get(
                "title",
                "Неизвестный трек",
            )

            file_path = ydl.prepare_filename(
                info
            )

            # Иногда расширение файла отличается.
            if not os.path.exists(
                file_path
            ):

                base = os.path.splitext(
                    file_path
                )[0]

                for filename in os.listdir(
                    DOWNLOAD_DIR
                ):

                    full_path = os.path.join(
                        DOWNLOAD_DIR,
                        filename,
                    )

                    if filename.startswith(
                        os.path.basename(base)
                    ):

                        file_path = full_path

                        break

            if not os.path.exists(
                file_path
            ):

                raise FileNotFoundError(
                    "Скачанный файл не найден."
                )

            return (
                file_path,
                title,
                info,
            )

    except Exception:

        for filename in os.listdir(
            DOWNLOAD_DIR
        ):

            if filename.startswith(
                unique_id
            ):

                try:

                    os.remove(
                        os.path.join(
                            DOWNLOAD_DIR,
                            filename,
                        )
                    )

                except OSError:
                    pass

        raise


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

    try:

        if status_message:

            await status_message.edit_text(
                "⏳ Загружаю трек..."
            )

        (
            file_path,
            title,
            info,
        ) = await asyncio.to_thread(
            download_audio_sync,
            query,
        )

        clean_name = clean_title(
            title
        )

        performer = (
            info.get("artist")
            or info.get("uploader")
            or "Неизвестный исполнитель"
        )

        caption = (
            f"🎵 <b>{escape(clean_name)}</b>\n"
            f"👤 {escape(performer)}"
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
            reply_markup=song_info_keyboard(
                str(performer),
                clean_name,
                source_url=(info.get("webpage_url") if is_url(query) else None),
                variants=variants,
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

        # Удаляем скачанный файл.
        try:

            os.remove(
                file_path
            )

        except OSError:
            pass

        file_path = None

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

                os.remove(
                    file_path
                )

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

    # Удаляем старые сообщения бота,
    # но не музыку.

    await delete_old_bot_messages(
        message.chat.id
    )

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
            "⏳ Загружаю полноценную версию..."
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
    # ИЩЕМ ИСПОЛНИТЕЛЯ
    # ========================================================

    artist_results = (
        await fetch_itunes_info(
            text,
            entity="musicArtist",
            limit=1,
        )
    )

    if artist_results:

        artist = artist_results[0]

        artist_name = artist.get(
            "artistName",
            text,
        )

        tracks = (
            await fetch_itunes_info(
                artist_name,
                entity="song",
                limit=8,
            )
        )

        if tracks:

            keyboard = artist_keyboard(
                artist_name,
                tracks,
            )

            await status.edit_text(
                f"👤 <b>{escape(artist_name)}</b>\n\n"
                "Выбери трек:",
                parse_mode="HTML",
                reply_markup=keyboard,
            )

            return

    # ========================================================
    # ОБЫЧНЫЙ ПОИСК ТРЕКА
    # ========================================================

    await download_and_send(
        status,
        text,
        status_message=status,
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
