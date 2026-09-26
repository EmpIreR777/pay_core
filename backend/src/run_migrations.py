import logging
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import DisconnectionError
from sqlalchemy_utils import create_database, database_exists  # type: ignore [import-untyped]
from src.core.config import settings

logger = logging.getLogger(__name__)


def init_database() -> None:
    """Инициализация базы данных."""
    sync_url = settings.SQLALCHEMY_SYNC_DB_URL
    schema_name = 'files'

    engine = create_engine(sync_url)

    if not database_exists(sync_url):
        create_database(sync_url)
        logger.info('✅ PostgreSQL база данных создана успешно.')
    else:
        logger.info('✅ База данных уже существует.')

    try:
        with engine.connect() as conn:
            logger.info(f'Создаём схему {schema_name}, если она не существует')
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
            conn.commit()
            logger.info(f'✅ Схема {schema_name} готова')
    except Exception as e:
        logger.error(f'❌ Ошибка при создании схемы: {e}')
        raise
    finally:
        engine.dispose()


def run_migrations_with_retry(max_attempts: int = 3, delay: int = 4) -> None:
    """Запускает миграции с повторными попытками при ошибках подключения."""
    attempt = 0
    while attempt < max_attempts:
        try:
            run_migrations()
            return
        except DisconnectionError as e:
            attempt += 1
            if attempt == max_attempts:
                logger.error(f'❌ Потерпел неудачу после {max_attempts} попыток: {e}')
                raise
            logger.warning(f'⚠️  Ошибка соединения (попытка {attempt}/{max_attempts}): {e}')
            time.sleep(delay)


def run_migrations() -> None:
    """Запуск Alembic миграций."""
    sync_url = settings.SQLALCHEMY_SYNC_DB_URL
    engine = create_engine(sync_url)

    try:
        # Инициализируем БД если её нет
        init_database()
        # Проверяем подключение
        check_connection(engine)

        # Запускаем миграции
        migrations_path = Path(__file__).parent.parent / 'migrations'
        alembic_cfg = Config()
        alembic_cfg.set_main_option('script_location', str(migrations_path))
        alembic_cfg.set_main_option('sqlalchemy.url', sync_url)

        logger.info('🔄 Применяем миграции alembic...')
        command.upgrade(alembic_cfg, 'head')

        # Проверяем таблицы
        inspector = inspect(engine)
        required_tables = [
            'stored_files',
            'alerts',
        ]
        existing_tables = inspector.get_table_names(schema='files')

        missing_tables = set(required_tables) - set(existing_tables)
        if missing_tables:
            raise Exception(f'❌ Отсутствующие таблицы после переноса: {missing_tables}')

        logger.info('✅ Миграция завершена успешно')

    except Exception as e:
        logger.error(f'❌ Ошибка во время миграции: {e}')
        raise
    finally:
        engine.dispose()


def check_connection(engine: Engine) -> bool:
    """Проверяет соединение с базой данных."""
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
            logger.info('✅ Проверка подключения к базе данных: успешно')
            return True
    except Exception as e:
        logger.error(f'❌ Не удалось проверить подключение к базе данных: {e}')
        return False


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(name)s - %(message)s')
    logger.info('🚀 Начинаю процесс миграции БД...')
    try:
        run_migrations_with_retry()
        logger.info('Vse migratsii uspeshno primeneny!')
        sys.exit(0)
    except Exception as e:
        logger.error(f'❌ Критическая ошибка при выполнении миграций: {e}')
        sys.exit(1)
