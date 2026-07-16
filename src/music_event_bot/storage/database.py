from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from music_event_bot.storage.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> int:
        async with self.connect() as connection:
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            )
            row = await cursor.fetchone()
            current = int(row["version"])
            if current > LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {current} is newer than supported version "
                    f"{LATEST_SCHEMA_VERSION}"
                )
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    await connection.executescript(sql)
                    await connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
                    await connection.commit()
                except Exception:
                    await connection.rollback()
                    raise
            return LATEST_SCHEMA_VERSION

    async def schema_version(self) -> int:
        async with self.connect() as connection:
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            )
            row = await cursor.fetchone()
            return int(row["version"])
