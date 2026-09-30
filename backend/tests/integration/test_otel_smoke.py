"""T-0.8: сквозная проверка OTel-конвейера Collector -> Jaeger/Prometheus.

Тест отправляет тестовые span/metric/log через OTel SDK (скрипт
``scripts/otel_smoke.py``) в Collector и дожидается их появления в хранилищах
бэкенда: трейс — в Jaeger, метрика — в prometheus-экспортёре Collector'а и в
Prometheus после scrape.

Ожидание реализовано polling'ом с явным таймаутом (без «sleep и надеемся»):
батчинг в Collector — 5s, ``scrape_interval`` Prometheus — 15s, поэтому
ожидание берётся с запасом. Если стенд не поднят, тест пропускается.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from scripts.otel_smoke import (
    SMOKE_CHILD_SPAN_NAME,
    SMOKE_SERVICE_NAME,
    SMOKE_SPAN_NAME,
    SmokeProbeConfig,
    SmokeProbeResult,
    run_probe,
)

from src.core.config import settings
from tests.integration.conftest import (
    COLLECTOR_HEALTH_URL,
    COLLECTOR_METRICS_URL,
    COLLECTOR_SELF_METRICS_URL,
    JAEGER_QUERY_URL,
    PROMETHEUS_URL,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('observability_stack')]

#: Таймауты ожидания с запасом относительно батчинга (5s) и scrape (15s).
TRACE_WAIT_TIMEOUT_SECONDS = 30.0
METRIC_WAIT_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0


def wait_until[T](check: Callable[[], T | None], timeout_seconds: float, description: str) -> T:
    """Опрашивает ``check`` до truthy-результата или истечения таймаута.

    Исключения внутри проверки считаются «условие ещё не наступило»: транспорт
    мог ещё переподключаться, и это не повод ронять тест раньше времени.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            result = check()
        except Exception as error:
            last_error = error
        else:
            if result:
                return result
        time.sleep(POLL_INTERVAL_SECONDS)

    error_hint = f'; последняя ошибка: {last_error!r}' if last_error else ''
    pytest.fail(f'Не дождались: {description} за {timeout_seconds:.0f}s{error_hint}')


@pytest.fixture(scope='module')
def probe_result() -> SmokeProbeResult:
    """Отправляет одну порцию телеметрии в Collector для всех проверок модуля.

    Стенд проверяет фикстура ``observability_stack`` из conftest, подключённая
    ко всему модулю через ``pytestmark``.
    """
    return run_probe(
        SmokeProbeConfig(
            otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            service_name=SMOKE_SERVICE_NAME,
        ),
    )


def test_collector_is_healthy() -> None:
    """Health-extension Collector'а отвечает и подтверждает готовность пайплайнов."""
    response = httpx.get(COLLECTOR_HEALTH_URL, timeout=5.0)
    response.raise_for_status()

    assert response.json()['status'] == 'Server available'


def test_trace_is_visible_in_jaeger(probe_result: SmokeProbeResult) -> None:
    """DoD T-0.8: в Jaeger виден отправленный span вместе с дочерним."""
    url = f'{JAEGER_QUERY_URL}/api/traces/{probe_result.trace_id}'

    def trace_exists() -> list[dict[str, Any]] | None:
        traces: list[dict[str, Any]] = httpx.get(url, timeout=5.0).json().get('data') or []
        return traces or None

    traces = wait_until(
        trace_exists,
        TRACE_WAIT_TIMEOUT_SECONDS,
        f'трейс {probe_result.trace_id} в Jaeger',
    )
    operation_names = {span['operationName'] for span in traces[0]['spans']}
    # Jaeger отдаёт processes как dict {processID: {...}}, а не список.
    service_names = {process['serviceName'] for process in traces[0]['processes'].values()}

    assert SMOKE_SPAN_NAME in operation_names
    assert SMOKE_CHILD_SPAN_NAME in operation_names
    assert service_names == {SMOKE_SERVICE_NAME}


def test_metric_is_exported_by_collector(probe_result: SmokeProbeResult) -> None:
    """Метрика приложения появляется в prometheus-экспортёре Collector'а (:8889)."""

    def metric_exported() -> bool:
        return probe_result.counter_name in httpx.get(COLLECTOR_METRICS_URL, timeout=5.0).text

    wait_until(
        metric_exported,
        METRIC_WAIT_TIMEOUT_SECONDS,
        f'метрика {probe_result.counter_name} на :8889',
    )


def test_metric_is_scrapeable_in_prometheus(probe_result: SmokeProbeResult) -> None:
    """DoD T-0.8: Prometheus видит метрику, отданную Collector'ом (полный fan-out)."""

    def prometheus_has_series() -> list[dict[str, Any]] | None:
        # Collector дописывает монотонным суммам суффикс `_total`, поэтому селектор
        # строим по regex, а не по точному имени метрики.
        query = f'{{__name__=~"{probe_result.counter_name}.*"}}'
        payload = httpx.get(f'{PROMETHEUS_URL}/api/v1/query', params={'query': query}, timeout=5.0).json()
        series: list[dict[str, Any]] = payload.get('data', {}).get('result') or []
        return series or None

    series = wait_until(
        prometheus_has_series,
        METRIC_WAIT_TIMEOUT_SECONDS,
        f'метрика {probe_result.counter_name} в Prometheus',
    )

    assert any(float(item['value'][1]) >= 1 for item in series)


def test_collector_exported_spans_to_jaeger() -> None:
    """Self-метрики Collector'а подтверждают, что трейсы ушли в Jaeger."""

    def spans_sent() -> bool:
        body = httpx.get(COLLECTOR_SELF_METRICS_URL, timeout=5.0).text
        return 'otelcol_exporter_sent_spans{exporter="otlp_grpc/jaeger"' in body

    wait_until(spans_sent, TRACE_WAIT_TIMEOUT_SECONDS, 'self-метрика otelcol_exporter_sent_spans (jaeger)')
