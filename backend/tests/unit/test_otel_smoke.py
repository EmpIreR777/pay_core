"""Юнит-тесты чистой логики smoke-зонда (T-0.8).

Сам end-to-end прогон (Collector -> Jaeger/Prometheus) живёт в
``tests/integration/test_otel_smoke.py`` и требует поднятого стенда, а эти
тесты проверяют бесплатную часть — разбор endpoint'а — без сети и Docker.
"""

import pytest
from scripts.otel_smoke import SmokeProbeConfig, parse_otlp_endpoint


@pytest.mark.parametrize(
    ('endpoint', 'expected'),
    [
        ('http://localhost:4317', ('localhost:4317', True)),
        ('http://otel-collector:4317', ('otel-collector:4317', True)),
        ('https://collector.example.com:443', ('collector.example.com:443', False)),
        ('localhost:4317', ('localhost:4317', True)),
        ('http://collector:4318', ('collector:4318', True)),
    ],
)
def test_parse_otlp_endpoint(endpoint: str, expected: tuple[str, bool]) -> None:
    """Экспортёрам нужен голый host:port и флаг insecure (T-0.8)."""
    assert parse_otlp_endpoint(endpoint) == expected


def test_parse_otlp_endpoint_defaults_to_4317() -> None:
    """Без порта подставляется стандартный OTLP/gRPC 4317."""
    assert parse_otlp_endpoint('http://localhost') == ('localhost:4317', True)


def test_parse_otlp_endpoint_rejects_grpc_scheme() -> None:
    """Схема grpc:// не поддерживается: экспортёры сами добавляют транспорт."""
    with pytest.raises(ValueError, match='Неподдерживаемая схема'):
        parse_otlp_endpoint('grpc://localhost:4317')


def test_probe_config_defaults() -> None:
    """Дефолты зонда: без экспорта он шлёт телеметрию на локальный Collector."""
    config = SmokeProbeConfig(otlp_endpoint='http://localhost:4317')

    assert config.service_name == 'paycore-otel-smoke'
    assert config.export_timeout_seconds == 10.0
    assert config.metric_export_interval_millis == 5_000
