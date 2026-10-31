"""Middleware безопасности: бот отвечает только одному пользователю."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пропускает только апдейты от ALLOWED_USER_ID.

    Регистрируется как outer-middleware на уровне Update, поэтому чужие
    сообщения, колбэки и любые другие события отбрасываются ДО фильтров и
    хендлеров — без ответа и без каких-либо побочных эффектов.
    """

    def __init__(self, allowed_user_id: int) -> None:
        self._allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        # event_from_user кладёт в data встроенный UserContextMiddleware
        user = data.get("event_from_user")
        if user is None or user.id != self._allowed_user_id:
            logger.debug("Апдейт %s от постороннего пользователя проигнорирован", event.update_id)
            return None
        return await handler(event, data)
