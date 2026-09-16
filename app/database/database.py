"""Async engine / session factory plus the small amount of schema bootstrap
the MVP needs (create_all - no migration tool on purpose)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Base

log = logging.getLogger(__name__)


def _default_literal(column: Any) -> Optional[str]:
    """SQL literal for a model default, so existing rows get a sane value."""
    default = getattr(column, "default", None)
    value = getattr(default, "arg", None) if default is not None else None
    if value is None or callable(value):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


class Database:
    def __init__(self, url: str, echo: bool = False) -> None:
        self._url = url
        self._is_sqlite = url.startswith("sqlite")
        connect_args: dict[str, Any] = {}
        if self._is_sqlite:
            # Wait up to 30 s for a lock instead of the 5 s default: a message
            # being processed (OCR, a price lookup) can hold a write open for
            # a few seconds while the price loop wants to write too.
            connect_args["timeout"] = 30
        self._engine: AsyncEngine = create_async_engine(
            url, echo=echo, pool_pre_ping=True, connect_args=connect_args
        )
        if self._is_sqlite:
            event.listen(self._engine.sync_engine, "connect", self._sqlite_pragmas)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @staticmethod
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        """WAL lets readers (stats, /positions) never block the writer."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._add_missing_columns)
        log.info("database schema ready")

    @staticmethod
    def _add_missing_columns(connection: Any) -> None:
        """Bring an older database up to date without a migration tool.

        ``create_all`` only ever creates missing *tables*, so a column added to
        a model after the first run would break every query against it.  New
        columns are added nullable with the model default, which is safe for
        existing rows and enough for this project.
        """
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} ADD COLUMN "
                    f"{column.name} {column.type.compile(connection.dialect)}"
                )
                literal = _default_literal(column)
                if literal is not None:
                    ddl += f" DEFAULT {literal}"
                connection.execute(text(ddl))
                log.warning("added missing column %s.%s", table.name, column.name)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session scope that commits on success and rolls back on error."""
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def close(self) -> None:
        await self._engine.dispose()
