"""Вспомогательные функции: классификация медиа, имена файлов, загрузка на диск."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger(__name__)

# Таймаут скачивания файла по HTTP (секунды) — файлы бывают до 2 ГБ
DOWNLOAD_TIMEOUT = 3600

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_STEM_BYTES = 150  # лимит имени в ФС — 255 байт; оставляем запас под суффиксы


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
def human_size(size_bytes: int) -> str:
    """Человекочитаемый размер: B, KB, MB, GB."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


# --------------------------------------------------------------------------- #
# Имена файлов
# --------------------------------------------------------------------------- #
def sanitize_filename(name: str) -> str:
    """Убирает путь и опасные символы, ограничивает длину (в байтах, а не символах)."""
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE_CHARS.sub("_", name).strip().strip(".")
    if not name:
        name = "file"

    stem, suffix = os.path.splitext(name)
    if len(suffix.encode("utf-8")) > 16:  # «расширение» подозрительно длинное
        stem, suffix = name, ""

    stem = stem.encode("utf-8")[:_MAX_STEM_BYTES].decode("utf-8", errors="ignore").rstrip()
    return f"{stem or 'file'}{suffix}"


def _reserve_unique_path_sync(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = os.path.splitext(filename)
    candidate = directory / filename
    while True:
        try:
            # O_EXCL атомарно «бронирует» имя: два параллельных запроса
            # никогда не получат один и тот же путь и не перезапишут файл.
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate = directory / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


async def reserve_unique_path(directory: Path, filename: str) -> Path:
    """Создаёт пустой файл с уникальным именем в directory и возвращает путь к нему."""
    return await asyncio.to_thread(_reserve_unique_path_sync, directory, filename)


# --------------------------------------------------------------------------- #
# Определение типа входящего файла
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IncomingFile:
    file_id: str
    filename: str
    category: str  # photos | videos | documents | audio


def _with_extension(name: str, mime: str | None, default_ext: str) -> str:
    if os.path.splitext(name)[1]:
        return name
    ext = mimetypes.guess_extension(mime) if mime else None
    return name + (ext or default_ext)


def _category_from_mime(mime: str | None) -> str:
    """Несжатые файлы (отправленные как документ) тоже раскладываем по типам."""
    if mime:
        if mime.startswith("image/"):
            return "photos"
        if mime.startswith("video/"):
            return "videos"
        if mime.startswith("audio/"):
            return "audio"
    return "documents"


def extract_incoming(message: Message) -> IncomingFile | None:
    """Достаёт из сообщения file_id, желаемое имя и категорию."""
    stamp = message.date.strftime("%Y%m%d_%H%M%S")

    if message.photo:
        photo = message.photo[-1]  # самая большая версия
        return IncomingFile(photo.file_id, f"photo_{stamp}_{photo.file_unique_id}.jpg", "photos")

    if message.video:
        v = message.video
        name = v.file_name or f"video_{stamp}_{v.file_unique_id}"
        return IncomingFile(v.file_id, _with_extension(name, v.mime_type, ".mp4"), "videos")

    # animation проверяем раньше document: у GIF заполнены оба поля
    if message.animation:
        a = message.animation
        name = a.file_name or f"animation_{stamp}_{a.file_unique_id}"
        return IncomingFile(a.file_id, _with_extension(name, a.mime_type, ".mp4"), "videos")

    if message.video_note:
        vn = message.video_note
        return IncomingFile(vn.file_id, f"videonote_{stamp}_{vn.file_unique_id}.mp4", "videos")

    if message.audio:
        au = message.audio
        name = au.file_name or (
            f"{au.performer} - {au.title}" if au.title else f"audio_{stamp}_{au.file_unique_id}"
        )
        return IncomingFile(au.file_id, _with_extension(name, au.mime_type, ".mp3"), "audio")

    if message.voice:
        vo = message.voice
        return IncomingFile(vo.file_id, f"voice_{stamp}_{vo.file_unique_id}.ogg", "audio")

    if message.document:
        d = message.document
        name = d.file_name or f"document_{stamp}_{d.file_unique_id}"
        return IncomingFile(
            d.file_id,
            _with_extension(name, d.mime_type, ".bin"),
            _category_from_mime(d.mime_type),
        )

    return None


# --------------------------------------------------------------------------- #
# Работа с диском
# --------------------------------------------------------------------------- #
async def fetch_to_disk(bot: Bot, file_id: str, destination: Path) -> None:
    """Скачивает файл Telegram в destination.

    Локальный Bot API Server (запущен с --local) в getFile возвращает АБСОЛЮТНЫЙ
    путь к файлу в своей файловой системе. Если этот каталог смонтирован и в
    контейнер бота, файл просто копируется — без лишней передачи по HTTP
    (быстро и работает для файлов до 2 ГБ). Иначе — обычное скачивание по HTTP.
    """
    tg_file = await bot.get_file(file_id)
    source = tg_file.file_path
    if not source:
        raise RuntimeError("Telegram не вернул file_path")

    if os.path.isabs(source):
        if await asyncio.to_thread(os.path.exists, source):
            await asyncio.to_thread(shutil.copyfile, source, destination)
            # Кэш сервера Bot API нам больше не нужен — освобождаем место
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.remove, source)
            return
        logger.warning(
            "Файл %s не виден из контейнера бота. Смонтируйте рабочий каталог "
            "telegram-bot-api (--dir) в бота по тому же пути; пробую скачать по HTTP.",
            source,
        )

    await bot.download_file(source, destination=destination, timeout=DOWNLOAD_TIMEOUT)


def _delete_stored_file_sync(path: Path, root: Path) -> bool:
    resolved, root = path.resolve(), root.resolve()
    if root not in resolved.parents:
        # Защита: никогда не удаляем ничего за пределами хранилища
        raise ValueError(f"Путь вне хранилища: {path}")
    try:
        resolved.unlink()
        return True
    except FileNotFoundError:
        return False


async def delete_stored_file(path: Path, root: Path) -> bool:
    """Удаляет файл внутри root. True — файл был удалён, False — его уже не было."""
    return await asyncio.to_thread(_delete_stored_file_sync, path, root)


async def remove_quietly(path: Path) -> None:
    """Best-effort удаление (для откатов при ошибках)."""
    with contextlib.suppress(OSError):
        await asyncio.to_thread(path.unlink)
