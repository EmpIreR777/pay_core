import asyncio
import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings

logger = logging.getLogger(__name__)

#: Сколько ждать готовности базы и с каким интервалом повторов. Healthcheck
#: compose даёт Postgres 20 с на старт, поэтому запас берём заметно больше:
CONNECT_ATTEMPTS: int = 10
CONNECT_DELAY_SECONDS: int = 3

#: Таблицы, которые обязаны появиться после ``upgrade head``. Сверяется факт, а
#: не код возврата Alembic: пустая миграция (модели разошлись с версиями)
#: отрабатывает «успешно», не создав ничего.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        'accounts',
        'payments',
        'outbox',
        'idempotency_keys',
        'processed_events',
        'provider_webhook_events',
    }
)


def build_alembic_config() -> Config:
    """Собрать конфигурацию Alembic.

    ``script_location`` задаётся явно, а не берётся из ``alembic.ini``: скрипт
    запускается из произвольного каталога (в контейнере это ``/``), и полагаться
    на относительный путь значит запустить миграции не оттуда.
    """
    backend_root = Path(__file__).resolve().parent.parent
    config = Config()
    config.set_main_option('script_location', str(backend_root / 'alembic'))
    # DSN не передаём: env.py берёт DATABASE_URL из настроек сам. Дублировать
    # источник правды здесь — значит завести второе место, где живёт адрес БД.
    return config


async def wait_for_database() -> None:
    """Дождаться, пока Postgres примет соединения.

    :raises OperationalError: если база не поднялась за все попытки.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                async with engine.connect() as connection:
                    await connection.execute(text('SELECT 1'))
            except OperationalError as error:
                if attempt == CONNECT_ATTEMPTS:
                    raise
                logger.warning(
                    'БД недоступна (попытка %d/%d): %s. Повтор через %d с',
                    attempt,
                    CONNECT_ATTEMPTS,
                    error,
                    CONNECT_DELAY_SECONDS,
                )
                await asyncio.sleep(CONNECT_DELAY_SECONDS)
            else:
                logger.info('Соединение с Postgres установлено')
                return
    finally:
        await engine.dispose()


async def verify_schema() -> None:
    """Убедиться, что после миграции на месте все ожидаемые таблицы.

    :raises RuntimeError: если каких-то таблиц нет.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"),
            )
            existing = {str(row[0]) for row in rows}
    finally:
        await engine.dispose()

    missing = EXPECTED_TABLES - existing
    if missing:
        raise RuntimeError(f'После миграции отсутствуют таблицы: {sorted(missing)}')
    logger.info('Схема на месте: %d таблиц', len(existing))


def main() -> int:
    """Применить миграции до ``head`` и проверить результат.

    :returns: код возврата для ``sys.exit``.
    """
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(name)s - %(message)s')
    logger.info('Применяем миграции к %s', settings.DATABASE_URL.rsplit('@', maxsplit=1)[-1])
    asyncio.run(wait_for_database())
    command.upgrade(build_alembic_config(), 'head')
    asyncio.run(verify_schema())
    logger.info('Миграции применены успешно')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Точка входа процесса: логируем с трассировкой и гасим стек — код
        logger.error('Миграции не применены: %s', error, exc_info=True)
        sys.exit(1)
