"""Хендлеры бота: приём файлов, /list, /search, скачивание и удаление."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Settings
from database import Database
from utils import (
    delete_stored_file,
    extract_incoming,
    fetch_to_disk,
    human_size,
    remove_quietly,
    reserve_unique_path,
    sanitize_filename,
)

logger = logging.getLogger(__name__)
router = Router(name="file_storage")

LIST_DEFAULT = 10
LIST_MAX = 20
SEARCH_LIMIT = 20

TYPE_LABELS = {
    "photos": "🖼 Фото",
    "videos": "🎬 Видео",
    "audio": "🎵 Аудио",
    "documents": "📎 Документ",
}

MEDIA_CONTENT_TYPES = {
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.AUDIO,
    ContentType.DOCUMENT,
    ContentType.VOICE,
    ContentType.ANIMATION,
    ContentType.VIDEO_NOTE,
}


class FileAction(CallbackData, prefix="f"):
    """Данные inline-кнопок: действие + id записи в БД."""

    action: str  # dl | del | delyes | cancel
    file_id: int


# --------------------------------------------------------------------------- #
# Карточка файла (сообщение + кнопки)
# --------------------------------------------------------------------------- #
def card_text(row: asyncpg.Record, tz: ZoneInfo) -> str:
    created = row["created_at"].replace(tzinfo=timezone.utc).astimezone(tz)
    return (
        f"<b>{html.escape(row['file_name'])}</b>\n"
        f"{TYPE_LABELS.get(row['file_type'], row['file_type'])} · "
        f"{human_size(row['file_size_bytes'])}\n"
        f"🕒 {created:%Y-%m-%d %H:%M}\n"
        f"🆔 {row['id']}"
    )


def card_keyboard(file_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬇️ Скачать", callback_data=FileAction(action="dl", file_id=file_id).pack()
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=FileAction(action="del", file_id=file_id).pack()
                ),
            ]
        ]
    )


def confirm_keyboard(file_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=FileAction(action="delyes", file_id=file_id).pack(),
                ),
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=FileAction(action="cancel", file_id=file_id).pack(),
                ),
            ]
        ]
    )


async def send_cards(message: Message, rows: list[asyncpg.Record], tz: ZoneInfo) -> None:
    """Отправляет каждый файл отдельным сообщением с кнопками «под файлом»."""
    for row in rows:
        await message.answer(card_text(row, tz), reply_markup=card_keyboard(row["id"]))
        await asyncio.sleep(0.05)  # мягко обходим лимиты Telegram на частоту отправки


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Я — личное файловое хранилище.\n\n"
        "Просто пришлите мне фото, видео, аудио или документ — я сохраню его "
        "на диск и занесу в базу.\n\n"
        "<b>Команды:</b>\n"
        "/list [N] — последние загруженные файлы (по умолчанию 10)\n"
        "/search &lt;имя&gt; — поиск файла по имени"
    )


@router.message(Command("list"))
async def cmd_list(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    limit = LIST_DEFAULT
    if command.args and command.args.strip().isdigit():
        limit = min(max(int(command.args.strip()), 1), LIST_MAX)

    rows = await db.latest(limit)
    if not rows:
        await message.answer("📭 Хранилище пока пусто.")
        return

    await message.answer(f"🗂 Последние файлы: {len(rows)}")
    await send_cards(message, rows, settings.tz)


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /search &lt;имя_файла&gt;")
        return

    rows = await db.search(query, SEARCH_LIMIT)
    if not rows:
        await message.answer(f"🔍 По запросу «{html.escape(query)}» ничего не найдено.")
        return

    await message.answer(f"🔍 Найдено: {len(rows)} (по запросу «{html.escape(query)}»)")
    await send_cards(message, rows, settings.tz)


# --------------------------------------------------------------------------- #
# Приём файлов
# --------------------------------------------------------------------------- #
@router.message(F.content_type.in_(MEDIA_CONTENT_TYPES))
async def on_media(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    incoming = extract_incoming(message)
    if incoming is None:
        return

    status = await message.reply("⏳ Сохраняю файл…")
    destination: Path | None = None

    try:
        now = message.date.astimezone(settings.tz)
        directory = settings.storage_dir / incoming.category / f"{now:%Y-%m}"

        # Бронируем уникальное имя, затем скачиваем файл прямо в него
        destination = await reserve_unique_path(directory, sanitize_filename(incoming.filename))
        await fetch_to_disk(bot, incoming.file_id, destination)
        size = destination.stat().st_size

        await db.add_file(
            telegram_user_id=message.from_user.id,
            file_name=destination.name,
            file_type=incoming.category,
            file_size_bytes=size,
            saved_path=str(destination),
        )
    except Exception as exc:  # noqa: BLE001 — пользователю нужен понятный ответ при любой ошибке
        logger.exception("Не удалось сохранить файл")
        if destination is not None:
            await remove_quietly(destination)  # не оставляем «сирот» без записи в БД
        await status.edit_text(
            "❌ Не удалось сохранить файл.\n"
            f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))[:300]}</code>"
        )
        return

    await status.edit_text(
        "✅ <b>Файл сохранён</b>\n"
        f"📄 Имя: <code>{html.escape(destination.name)}</code>\n"
        f"📦 Размер: {human_size(size)}\n"
        f"📁 Путь: <code>{html.escape(str(destination))}</code>"
    )


# --------------------------------------------------------------------------- #
# Кнопки: скачать / удалить
# --------------------------------------------------------------------------- #
@router.callback_query(FileAction.filter(F.action == "dl"))
async def cb_download(call: CallbackQuery, callback_data: FileAction, bot: Bot, db: Database) -> None:
    row = await db.get_file(callback_data.file_id)
    if row is None:
        await call.answer("Запись не найдена в базе.", show_alert=True)
        return

    path = Path(row["saved_path"])
    if not path.is_file():
        await call.answer("Файл отсутствует на диске.", show_alert=True)
        return

    await call.answer("Отправляю…")
    chat_id = call.from_user.id
    await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

    name, ftype = row["file_name"], row["file_type"]
    try:
        if ftype == "videos":
            await bot.send_video(chat_id, FSInputFile(path, filename=name), supports_streaming=True)
        elif ftype == "audio":
            await bot.send_audio(chat_id, FSInputFile(path, filename=name))
        else:
            # Фото тоже уходят документом — так сохраняется оригинальное качество
            await bot.send_document(chat_id, FSInputFile(path, filename=name))
    except TelegramBadRequest:
        # Telegram не принял файл как видео/аудио (кодек, формат) — шлём документом
        logger.warning("Отправка %s как %s не удалась, пробую send_document", name, ftype)
        try:
            await bot.send_document(chat_id, FSInputFile(path, filename=name))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось отправить файл")
            await bot.send_message(chat_id, f"❌ Не удалось отправить файл: {html.escape(str(exc))[:300]}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось отправить файл")
        await bot.send_message(chat_id, f"❌ Не удалось отправить файл: {html.escape(str(exc))[:300]}")


@router.callback_query(FileAction.filter(F.action == "del"))
async def cb_delete_ask(call: CallbackQuery, callback_data: FileAction) -> None:
    """Первый шаг удаления — подтверждение, чтобы не стереть файл случайным тапом."""
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(reply_markup=confirm_keyboard(callback_data.file_id))
    await call.answer("Удалить файл безвозвратно?")


@router.callback_query(FileAction.filter(F.action == "cancel"))
async def cb_delete_cancel(call: CallbackQuery, callback_data: FileAction) -> None:
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(reply_markup=card_keyboard(callback_data.file_id))
    await call.answer("Отменено")


@router.callback_query(FileAction.filter(F.action == "delyes"))
async def cb_delete_confirm(
    call: CallbackQuery, callback_data: FileAction, db: Database, settings: Settings
) -> None:
    row = await db.get_file(callback_data.file_id)
    if row is None:
        await _finish_card(call, "ℹ️ Запись уже удалена.")
        await call.answer()
        return

    try:
        # Сначала диск, потом БД: если удалить файл не вышло, запись остаётся
        existed = await delete_stored_file(Path(row["saved_path"]), settings.storage_dir)
    except (OSError, ValueError) as exc:
        logger.exception("Не удалось удалить файл с диска")
        await call.answer(f"Ошибка удаления: {exc}"[:200], show_alert=True)
        return

    await db.delete_file(callback_data.file_id)
    note = "" if existed else "\n(файла на диске уже не было)"
    await _finish_card(call, f"🗑 Удалён: <b>{html.escape(row['file_name'])}</b>{note}")
    await call.answer("Удалено")


async def _finish_card(call: CallbackQuery, text: str) -> None:
    """Заменяет карточку итоговым текстом и убирает кнопки."""
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(text, reply_markup=None)
