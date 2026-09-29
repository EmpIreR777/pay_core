from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings

async_engine = create_async_engine(
    url=settings.DATABASE_URL,
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
