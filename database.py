"""Работа с PostgreSQL через asyncpg."""
from __future__ import annotations

import asyncio
import logging

import asyncpg

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_files (
    id               SERIAL PRIMARY KEY,
    telegram_user_id BIGINT       NOT NULL,
    file_name        VARCHAR(255) NOT NULL,
    file_type        VARCHAR(32)  NOT NULL,
    file_size_bytes  BIGINT       NOT NULL,
    saved_path       TEXT         NOT NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
);
CREATE INDEX IF NOT EXISTS idx_media_files_created_at
    ON media_files (created_at DESC);
"""


def _escape_like(value: str) -> str:
    """Экранирует спецсимволы LIKE, чтобы поиск был буквальным."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("База данных не инициализирована: вызовите connect()")
        return self._pool

    async def connect(self, retries: int = 10, delay: float = 3.0) -> None:
        """Создаёт пул соединений (с повторами, пока PostgreSQL поднимается) и схему."""
        for attempt in range(1, retries + 1):
            try:
                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
                break
            except (OSError, asyncpg.PostgresError) as exc:
                if attempt == retries:
                    raise
                logger.warning(
                    "PostgreSQL недоступен (%s), попытка %d/%d через %.0f c",
                    exc, attempt, retries, delay,
                )
                await asyncio.sleep(delay)

        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("База данных готова, таблица media_files проверена/создана")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def add_file(
        self,
        *,
        telegram_user_id: int,
        file_name: str,
        file_type: str,
        file_size_bytes: int,
        saved_path: str,
    ) -> int:
        """Добавляет запись о файле и возвращает её id."""
        return await self.pool.fetchval(
            """
            INSERT INTO media_files
                (telegram_user_id, file_name, file_type, file_size_bytes, saved_path)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            telegram_user_id, file_name, file_type, file_size_bytes, saved_path,
        )

    async def get_file(self, file_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM media_files WHERE id = $1", file_id)

    async def delete_file(self, file_id: int) -> bool:
        """Удаляет запись. Возвращает True, если запись существовала."""
        result = await self.pool.execute("DELETE FROM media_files WHERE id = $1", file_id)
        return result.endswith(" 1")

    async def latest(self, limit: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM media_files ORDER BY created_at DESC, id DESC LIMIT $1",
            limit,
        )

    async def search(self, query: str, limit: int) -> list[asyncpg.Record]:
        """Регистронезависимый поиск по подстроке в имени файла."""
        pattern = f"%{_escape_like(query)}%"
        return await self.pool.fetch(
            """
            SELECT * FROM media_files
            WHERE file_name ILIKE $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            pattern, limit,
        )
