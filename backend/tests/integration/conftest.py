"""Общие фикстуры для интеграционных тестов.

Интеграционные тесты T-0.8 ходят по HTTP в уже поднятый стенд (``make up``).
Если стенд не поднят, тесты пропускаются, а не падают: ``make test`` обязан
оставаться зелёным на машине без Docker.
"""

import socket

import pytest

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


@pytest.fixture(scope='session', autouse=True)
def observability_stack() -> None:
    """Пропускает интеграционные тесты, если стенд observability не поднят.

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
