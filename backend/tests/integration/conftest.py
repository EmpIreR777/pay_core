"""Общие фикстуры для интеграционных тестов.

Интеграционные тесты ходят по живому стенду (``make up`` в корне репозитория).
Каждый набор проверок завязан на свой стенд, и фикстуры готовности **явные**:
раньше одна autouse-фикстура observability молча пропускала весь каталог, и
интеграционный тест репозитория счетов (T-3.3) не запускался бы на машине, где
поднят только Postgres. Теперь ``observability_stack`` и ``postgres_stack``
запрашиваются явно — тестом или его модулем, — и подменяют друг друга только
там, где это правда.

Без стенда тесты пропускаются, а не падают: ``make test`` обязан оставаться
зелёным на машине без Docker (AGENT.md, §5).
"""

import socket
from urllib.parse import urlparse

import pytest

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
