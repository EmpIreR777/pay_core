from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from src.core.config import settings

async_engine = create_async_engine(
    url=settings.SQLALCHEMY_DB_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Асинхронный контекстный менеджер для работы с сессией базы данных."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


sync_engine = create_engine(settings.SQLALCHEMY_SYNC_DB_URL, pool_pre_ping=True)

SyncSession = sessionmaker(bind=sync_engine)
