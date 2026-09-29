"""Общие фикстуры для интеграционных тестов.

Интеграционные тесты ходят по живому стенду (``make up`` в корне репозитория).
Каждый набор проверок завязан на свой стенд, и фикстуры готовности **явные**:
раньше одна autouse-фикстура observability молча пропускала весь каталог, и
интеграционный тест репозитория счетов (T-3.3) не запускался бы на машине, где
поднят только Postgres. Теперь ``observability_stack`` и ``postgres_stack``
запрашиваются явно — тестом или его модулем, — и подменяют друг друга только
там, где это правда.

Здесь же живёт общая обвязка работы с Postgres: сессия одной откатываемой
транзакции, отдельная транзакция на своём соединении и перехват SQL. Репозитории
платежей (T-3.4) и счетов (T-3.3) проверяются одинаково — изоляция транзакции,
видимость из чужого соединения, «сколько запросов ушло на сервер», — и копия
этой обвязки в каждом модуле разошлась бы с первой же правкой.

Без стенда тесты пропускаются, а не падают: ``make test`` обязан оставаться
зелёным на машине без Docker (AGENT.md, §5).
"""

import socket
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings

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


@pytest.fixture(scope='session')
def postgres_stack() -> None:
    """Пропустить тест, если Postgres не поднят (``make up`` в корне репозитория)."""
    if not postgres_is_reachable():
        pytest.skip(
            'Postgres недоступен по DATABASE_URL. Поднимите стенд: `make up` в корне репозитория.',
        )


#: Сколько ждать чужую блокировку строки, прежде чем признать её отсутствующей.
#: Проверка блокировки обязана иметь потолок: без него тест, в котором `FOR UPDATE`
#: не сработал, провисел бы до бесконечного ожидания вместо честного падения.
LOCK_WAIT_SECONDS = 5


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Сессия одной транзакции, которая по выходу откатывается.

    Отдельный движок на каждый тест не нужен: у каждой сессии своё соединение, а
    ``rollback`` выполняется даже после падения теста — иначе следующий тест
    унаследовал бы незакрытую транзакцию и завис на блокировке.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    try:
        async with session_maker() as open_session:
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
    session_maker = async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    try:
        async with session_maker() as transaction_session:
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
