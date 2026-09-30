"""Общие фикстуры для интеграционных тестов.

Postgres тесты получают через ``postgres_stack``: сначала живой стенд
(``make up`` в корне репозитория), а при его отсутствии — временный контейнер
testcontainer (T-3.7), поэтому ``pytest tests/integration/`` работает
автономно.
Каждый набор проверок завязан на свой стенд, и фикстуры готовности **явные**:
раньше одна autouse-фикстура observability молча пропускала весь каталог, и
интеграционный тест репозитория счетов (T-3.3) не запускался бы на машине, где
поднят только Postgres. Теперь ``observability_stack`` и ``postgres_stack``
запрашиваются явно — тестом или его модулем, — и подменяют друг друга только
там, где это правда. Observability-стенд testcontainer не поднимает: эти тесты
по-прежнему ждут ``make up``.

Здесь же живёт общая обвязка работы с Postgres: сессия одной откатываемой
транзакции, отдельная транзакция на своём соединении, перехват SQL и чтение
каталога БД. Репозитории платежей (T-3.4) и счетов (T-3.3) проверяются одинаково —
изоляция транзакции, видимость из чужого соединения, «сколько запросов ушло на
сервер», — и копия этой обвязки в каждом модуле разошлась бы с первой же правки.
То же касается каталога: его читают и проверки миграций (T-3.2), и проверки схемы
конкретных таблиц (T-4.1), поэтому запросы и разбор результата живут здесь.

Возврат схемы к ``head`` — тоже общее дело (``restore_head``): модуль, который
прогоняет откат, обязан вернуть стенд в рабочее состояние даже после падения,
иначе следующий запуск падал бы по несвязанной причине. Фикстура не ``autouse``:
её запрашивают явно те модули, которые двигают ревизии, — иначе откаты переезжали
бы в тесты, которые о них ничего не знают.

Нет ни стенда, ни Docker — тесты пропускаются, а не падают: ``make test``
обязан оставаться зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
import os
import socket
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import docker
import pytest
from alembic import command
from sqlalchemy import Row, delete, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from src.core.config import settings
from src.db.models.account import AccountModel
from src.db.models.payment import PaymentModel
from src.run_migrations import build_alembic_config, verify_schema, wait_for_database

#: Порты стенда из docker-compose.yml. ENV-переопределения не поддерживаем:
#: тесты идут против дефолтного compose-конфига репозитория, иначе пришлось бы
#: дублировать парсинг .env в тестах.
COLLECTOR_OTLP_PORT = 4317
COLLECTOR_HEALTH_PORT = 13133
COLLECTOR_METRICS_PORT = 8889
COLLECTOR_SELF_METRICS_PORT = 8888
JAEGER_PORT = 16686
PROMETHEUS_PORT = 9090

JAEGER_QUERY_URL = f'http://localhost:{JAEGER_PORT}'
PROMETHEUS_URL = f'http://localhost:{PROMETHEUS_PORT}'
#: health_check extension Collector'а отвечает в корне.
COLLECTOR_HEALTH_URL = f'http://localhost:{COLLECTOR_HEALTH_PORT}/'
#: prometheus-экспортёр Collector'а отдаёт телеметрию приложений на /metrics
#: (корень отдаёт 404), self-метрики Collector'а — на соседнем порту.
COLLECTOR_METRICS_URL = f'http://localhost:{COLLECTOR_METRICS_PORT}/metrics'
COLLECTOR_SELF_METRICS_URL = f'http://localhost:{COLLECTOR_SELF_METRICS_PORT}/metrics'

#: Образ и реквизиты временного Postgres testcontainer (T-3.7). Образ — тот же,
#: что в docker-compose.yml: тесты обязаны ходить в ту же версию БД, что и
#: стенд, иначе проверяется не продовая инфраструктура. Реквизиты задаются
#: явно: PostgresContainer читает POSTGRES_* из окружения, и настройка машины
#: незаметно подменила бы DSN тестового контейнера.
POSTGRES_TEST_IMAGE = 'postgres:16-alpine'
POSTGRES_TEST_USER = 'postgres'
POSTGRES_TEST_PASSWORD = 'postgres'
POSTGRES_TEST_DB = 'pay_core'


def is_tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Проверяет доступность порта без установки HTTP-зависимостей."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope='session')
def observability_stack() -> None:
    """Пропустить тест, если стенд observability не поднят.

    Порты берутся из тех же констант, что и URL проверок, — иначе легко получить
    «тест падает вместо skip» (схема доступности разъезжается с адресами).
    """
    required_ports = {
        'OTel Collector (OTLP :4317)': COLLECTOR_OTLP_PORT,
        'OTel Collector (health :13133)': COLLECTOR_HEALTH_PORT,
        'Jaeger (:16686)': JAEGER_PORT,
        'Prometheus (:9090)': PROMETHEUS_PORT,
    }
    unavailable = [f'{name}:{port}' for name, port in required_ports.items() if not is_tcp_port_open('localhost', port)]
    if unavailable:
        pytest.skip(
            'Стенд observability не поднят (' + ', '.join(unavailable) + '). Запустите `make up` в корне репозитория.',
        )


def postgres_is_reachable() -> bool:
    """Отвечает ли Postgres по адресу из ``DATABASE_URL``.

    Проверка идёт «сырым» сокетом по хосту и порту из DSN: так она не тянет за
    собой соединение и завершается быстро, когда стенда нет.
    """
    parsed = urlparse(settings.DATABASE_URL)
    try:
        with socket.create_connection((parsed.hostname or 'localhost', parsed.port or 5432), timeout=2.0):
            return True
    except OSError:
        return False


def docker_daemon_is_reachable() -> bool:
    """Отвечает ли Docker-демон — он нужен, чтобы поднять временный Postgres.

    Проверка — ``ping`` по API Docker: она не создаёт контейнеров и
    завершается быстро, когда демона нет. Без неё падение ``start`` выглядело
    бы как поломка тестов вместо привычного skip (AGENT.md, §5).
    """
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        return False
    return True


@pytest.fixture(scope='session')
def postgres_stack() -> Iterator[None]:
    """Обеспечить интеграционным тестам Postgres: стенд, а при его отсутствии — testcontainer.

    Первым проверяется живой стенд (``make up`` в корне репозитория): на нём
    тесты видят compose-инфраструктуру репозитория. Стенда нет — поднимается
    временный контейнер ``POSTGRES_TEST_IMAGE`` (DoD T-3.7: ``pytest
    tests/integration/`` работает автономно), и к нему здесь же применяются
    миграции: контейнер приходит с пустой базой, а схему ждут и репозитории,
    и тесты миграций. DSN контейнера подставляется в ``settings.DATABASE_URL``
    на время сессии — вся обвязка читает настройки в момент работы, поэтому
    правка её сигнатур не потребовалась.

    Ни стенда, ни Docker — skip, а не падение: ``make test`` обязан оставаться
    зелёным на машине без Docker (AGENT.md, §5).
    """
    if postgres_is_reachable():
        yield
        return

    if not docker_daemon_is_reachable():
        pytest.skip(
            'Postgres недоступен по DATABASE_URL, а Docker для testcontainer не отвечает: '
            'поднимите стенд (`make up` в корне репозитория) или запустите Docker.',
        )

    # Ryuk — репер testcontainers — монтирует docker-сокет хоста в свой контейнер.
    # На Colima сокет из docker context (`~/.colima/default/docker.sock`) не виден
    # внутри VM, и старт контейнера падает с 500 («operation not supported» при
    # mkdir сокета). Репер здесь не нужен: контейнер останавливается в finally
    # ниже, а без него худший исход — осиротевший контейнер после жёсткого
    # убийства pytest, видимый в `docker ps` по лейблу testcontainers.
    # setdefault уважает явно заданное окружение.
    os.environ.setdefault('TESTCONTAINERS_RYUK_DISABLED', 'true')

    container = PostgresContainer(
        POSTGRES_TEST_IMAGE,
        username=POSTGRES_TEST_USER,
        password=POSTGRES_TEST_PASSWORD,
        dbname=POSTGRES_TEST_DB,
        driver='asyncpg',
    )
    container.start()
    original_url = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = container.get_connection_url()
        # Миграции берут DSN из настроек, поэтому подмена выше видна им без
        # отдельной передачи; wait/verify — существующие хелперы run_migrations.
        asyncio.run(wait_for_database())
        command.upgrade(build_alembic_config(), 'head')
        asyncio.run(verify_schema())
        yield
    finally:
        settings.DATABASE_URL = original_url
        container.stop()


#: Сколько ждать чужую блокировку строки, прежде чем признать её отсутствующей.
#: Проверка блокировки обязана иметь потолок: без него тест, в котором `FOR UPDATE`
#: не сработал, провисел бы до бесконечного ожидания вместо честного падения.
LOCK_WAIT_SECONDS = 5


def build_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий с настройками приложения.

    ``autoflush=False`` обязателен: репозитории пишут явным ``flush``, иначе
    ``INSERT`` уехал бы в момент коммита, а ошибку ограничения ждал бы уже
    транзакционный код. ``expire_on_commit=False`` — чтобы считанные сущности не
    становились непригодными после фиксации. Параметры живут здесь, а не в
    каждом модуле: три одинаковые копии разъедутся с первой же правкой.
    """
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


async def clear_payment_data() -> None:
    """Очистить ``payments`` и ``accounts`` перед модулем тестов и после него.

    Отдельное соединение с ``autocommit``-подобным поведением: у вызывающего
    транзакции может быть не видно, и ``DELETE`` внутри её откатился бы вместе с
    ней. Порядок обязателен — ``payments`` ссылается на ``accounts`` с
    ``ON DELETE RESTRICT``.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(delete(PaymentModel))
            await connection.execute(delete(AccountModel))
    finally:
        await engine.dispose()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Сессия одной транзакции, которая по выходу откатывается.

    Отдельный движок на каждый тест не нужен: у каждой сессии своё соединение, а
    ``rollback`` выполняется даже после падения теста — иначе следующий тест
    унаследовал бы незакрытую транзакцию и завис на блокировке.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with build_session_maker(engine)() as open_session:
            yield open_session
            await open_session.rollback()
    finally:
        await engine.dispose()


@asynccontextmanager
async def open_transaction(*, lock_wait_seconds: int | None = None) -> AsyncIterator[AsyncSession]:
    """Отдельная транзакция на своём соединении, откатываемая на выходе.

    Нужна там, где участвуют две одновременные транзакции: у сессии фикстуры своё
    соединение, и второй транзакции из неё не получить. ``lock_timeout`` задаётся
    в миллисекундах через ``SET LOCAL`` — он действует до конца транзакции и не
    требует прав на уровне базы, поэтому проверка блокировки укладывается в
    конечное время вместо бесконечного ожидания.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with build_session_maker(engine)() as transaction_session:
            await transaction_session.begin()
            try:
                if lock_wait_seconds is not None:
                    # SET LOCAL действует до конца транзакции, поэтому порядок
                    # обязателен: транзакция должна быть уже открыта.
                    await transaction_session.execute(
                        text(f'SET LOCAL lock_timeout = {lock_wait_seconds * 1000}'),
                    )
                yield transaction_session
            finally:
                await transaction_session.rollback()
    finally:
        await engine.dispose()


async def capture_sql(session: AsyncSession, operation: Coroutine[object, object, object]) -> list[str]:
    """Выполнить операцию и вернуть SQL-запросы, которые она отправила в базу.

    Нужен там, где важно не «что вернулось», а «сколько и каких запросов ушло на
    сервер»: лишний ``SELECT``, лишний джойн или потерянный ``FOR UPDATE``
    возвращают те же данные, поэтому результат их не различает.
    """
    statements: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, _params: object, _ctx: object, _many: object) -> None:
        statements.append(' '.join(statement.split()))

    engine = session.get_bind()
    sync_engine = getattr(engine, 'sync_engine', engine)
    event.listen(sync_engine, 'before_cursor_execute', record)
    try:
        await operation
    finally:
        event.remove(sync_engine, 'before_cursor_execute', record)
    return statements


#: Запрос каталога Postgres: читается фактическое состояние БД, а не метаданные
#: моделей — иначе тест доказал бы лишь то, что SQLAlchemy умеет описывать
#: таблицы, но ничего про то, что миграции их создали.
PUBLIC_TABLES_QUERY = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"

#: Запрос индексов схемы ``public``. Возвращает пару ``(имя, таблица)``: одного
#: имени мало, ведь имя индекса уникально только внутри своей таблицы.
PUBLIC_INDEXES_QUERY = "SELECT indexname, tablename FROM pg_indexes WHERE schemaname = 'public'"


async def fetch_rows(query: str) -> list[Row[Any]]:
    """Выполнить запрос и вернуть все строки результата.

    Отдельное соединение на каждый вызов: каталог читают тесты, которые сами
    двигают ревизии, и переиспользовать их транзакцию нельзя — она может быть
    откатываемой, и тогда проверка увидела бы не committed-состояние базы.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(query))
            return list(result.mappings())
    finally:
        await engine.dispose()


#: Запрос «есть ли таблица» — параметризован, а не собран строкой. Имя таблицы
#: приходит из теста, и ``S608`` (ruff) правомерно считает склейку в SQL вектором
#: инъекции, даже когда значение заведомо своё: безопасность обязана обеспечиваться
#: параметром, а не тем, что «сюда никто чужое не подставит».
TABLE_PRESENCE_QUERY = "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = :table_name"


async def fetch_first(query: str, **parameters: object) -> object | None:
    """Выполнить запрос с параметрами и вернуть первое значение первой строки.

    Значение или ``None``, если строк нет. Отдельное соединение на каждый вызов:
    каталог читают тесты, которые сами двигают ревизии, и переиспользовать их
    транзакцию нельзя — она может быть откатываемой, и тогда проверка увидела бы
    не committed-состояние базы.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(query), parameters)
            row = result.mappings().first()
            return None if row is None else next(iter(row.values()))
    finally:
        await engine.dispose()


async def query_first_value(query: str) -> object | None:
    """Выполнить запрос без параметров и вернуть первое значение первой строки."""
    return await fetch_first(query)


async def list_public_tables() -> set[str]:
    """Имена таблиц, реально созданных в схеме ``public``."""
    return {str(row['tablename']) for row in await fetch_rows(PUBLIC_TABLES_QUERY)}


async def query_table_presence(table_name: str) -> bool:
    """Есть ли таблица в схеме ``public``.

    Отдельный запрос вместо поиска в :func:`list_public_tables`: проверке «создал ли
    миграцию таблицу» нужен факт поимённо, а перебор всего списка и сравнение
    множеств отвечало бы на другой вопрос — «созданы ли все таблицы разом», — и
    стало бы повторением проверки из ``test_migrations.py``.
    """
    return await fetch_first(TABLE_PRESENCE_QUERY, table_name=table_name) is not None


async def list_public_indexes() -> dict[str, str]:
    """Индексы схемы ``public`` как отображение ``имя индекса -> имя таблицы``."""
    return {str(row['indexname']): str(row['tablename']) for row in await fetch_rows(PUBLIC_INDEXES_QUERY)}


@pytest.fixture
def restore_head() -> Iterator[None]:
    """Вернуть базу к ``head`` после теста, даже если он упал.

    Иначе упавший тест на откате оставил бы стенд без схемы, и следующий запуск
    ``make test`` падал бы уже по другой, не связанной с ним причине.
    """
    yield
    command.upgrade(build_alembic_config(), 'head')
