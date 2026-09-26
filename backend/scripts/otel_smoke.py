"""Сквозная проверка OTel-конвейера: приложение -> Collector -> Jaeger/Prometheus.

Задача T-0.8 (DoD): «Минимальное Python-приложение с OTel SDK шлёт тестовый span
и metric на Collector (:4317). В Jaeger виден span, в Prometheus видна метрика».

Скрипт намеренно НЕ часть runtime-кода сервисов: это диагностический зонд,
который доказывает, что связка из T-0.6/T-0.7 живая:

    приложение --OTLP/gRPC--> otel-collector (:4317)
                                  |-- traces  --> jaeger:4317  --> Jaeger UI (:16686)
                                  |-- metrics --> :8889        --> Prometheus (:9090)
                                  `-- logs    --> debug        --> stdout Collector'а

Реальные сервисы начнут пользоваться этой телеметрией в Эпике 12, здесь важна
только проверка транспорта и fan-out.

Запуск (из каталога ``backend/``)::

    uv run python -m scripts.otel_smoke            # или make otel-smoke

После запуска печатаются ``trace_id`` и ссылки, по которым видно, что трейс и
метрика доехали до Jaeger и Prometheus.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry._logs import LogRecord as APILogRecord
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from src.core.config import settings as global_settings

#: Имя сервиса, под которым зонд светится в Jaeger. Отдельное от
#: ``OTEL_SERVICE_NAME``, чтобы smoke-трейсы не смешивались с будущими
#: реальными трейсами приложений при поиске в UI.
SMOKE_SERVICE_NAME: Final[str] = 'paycore-otel-smoke'
SMOKE_SPAN_NAME: Final[str] = 'paycore.otel_smoke.probe'
SMOKE_CHILD_SPAN_NAME: Final[str] = 'paycore.otel_smoke.probe.child'
SMOKE_COUNTER_NAME: Final[str] = 'paycore_smoke_probe_count'
SMOKE_HISTOGRAM_NAME: Final[str] = 'paycore_smoke_probe_duration'
SMOKE_LOG_MESSAGE: Final[str] = 'paycore otel smoke probe log record'
SMOKE_ATTRIBUTE_KEY: Final[str] = 'paycore.smoke.probe'


@dataclass(frozen=True, slots=True)
class SmokeProbeConfig:
    """Параметры одного прогона зонда."""

    otlp_endpoint: str
    service_name: str = SMOKE_SERVICE_NAME
    export_timeout_seconds: float = 10.0
    metric_export_interval_millis: int = 5_000


@dataclass(frozen=True, slots=True)
class SmokeProbeResult:
    """Идентификаторы прогона, по которым проверяют доставку телеметрии."""

    service_name: str
    trace_id: str
    span_id: str
    otlp_endpoint: str
    counter_name: str
    histogram_name: str
    span_duration_ms: float


def parse_otlp_endpoint(endpoint: str) -> tuple[str, bool]:
    """Разбирает OTLP-endpoint в пару ``(host:port, insecure)`` для gRPC.

    Экспортёрам нужен «голый» адрес сокета и флаг TLS. Локальный
    compose-Collector слушает plaintext, значит ``http://localhost:4317``
    превращается в ``('localhost:4317', True)``.
    """
    parsed = urlparse(endpoint if '://' in endpoint else f'http://{endpoint}')
    if parsed.scheme not in ('http', 'https'):
        msg = f'Неподдерживаемая схема OTLP endpoint: {endpoint!r}'
        raise ValueError(msg)

    host = parsed.hostname or 'localhost'
    port = parsed.port or (443 if parsed.scheme == 'https' else 4317)
    return f'{host}:{port}', parsed.scheme != 'https'


def build_resource(service_name: str) -> Resource:
    """Resource приложения: минимум, который Jaeger показывает как сервис."""
    return Resource.create(
        {
            'service.name': service_name,
            'service.version': '0.1.0',
        },
    )


def create_tracer_provider(config: SmokeProbeConfig) -> TracerProvider:
    """TracerProvider с OTLP/gRPC-экспортёром, включённым в глобальный API."""
    endpoint, insecure = parse_otlp_endpoint(config.otlp_endpoint)
    provider = TracerProvider(resource=build_resource(config.service_name))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                insecure=insecure,
                timeout=config.export_timeout_seconds,
            ),
        ),
    )
    trace.set_tracer_provider(provider)
    return provider


def create_meter_provider(config: SmokeProbeConfig) -> MeterProvider:
    """MeterProvider с OTLP/gRPC-экспортёром и периодическим reader'ом."""
    endpoint, insecure = parse_otlp_endpoint(config.otlp_endpoint)
    return MeterProvider(
        resource=build_resource(config.service_name),
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=endpoint,
                    insecure=insecure,
                    timeout=config.export_timeout_seconds,
                ),
                export_interval_millis=config.metric_export_interval_millis,
            ),
        ],
    )


def create_logger_provider(config: SmokeProbeConfig) -> LoggerProvider:
    """LoggerProvider с OTLP/gRPC-экспортёром (logs-пайплайн -> debug)."""
    endpoint, insecure = parse_otlp_endpoint(config.otlp_endpoint)
    provider = LoggerProvider(resource=build_resource(config.service_name))
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(
                endpoint=endpoint,
                insecure=insecure,
                timeout=config.export_timeout_seconds,
            ),
        ),
    )
    return provider


def emit_probe_log_record(otel_logger: object, span_context: trace.SpanContext) -> None:
    """Отправляет OTLP log-запись, привязанную к текущему трейсу."""
    emit = getattr(otel_logger, 'emit', None)
    if not callable(emit):  # pragma: no cover - защита от «сломанного» SDK
        msg = 'LoggerProvider вернул объект без вызываемого метода emit.'
        raise RuntimeError(msg)

    emit(
        APILogRecord(
            timestamp=time.time_ns(),
            severity_text='INFO',
            severity_number=SeverityNumber.INFO,
            body=SMOKE_LOG_MESSAGE,
            attributes={SMOKE_ATTRIBUTE_KEY: True},
            trace_id=span_context.trace_id,
            span_id=span_context.span_id,
            trace_flags=span_context.trace_flags,
        ),
    )


def flush_and_shutdown(
    tracer_provider: TracerProvider,
    meter_provider: MeterProvider,
    logger_provider: LoggerProvider,
) -> None:
    """Сбрасывает батчи в Collector и останавливает фоновые потоки экспортёров.

    Без явного ``force_flush`` телеметрия молча теряется: процесс успевает
    завершиться раньше, чем батчер отдаст батч. Без ``shutdown`` фоновые
    потоки экспортёров держат интерпретатор и процесс не завершается.
    """
    timeout_millis = 10_000
    tracer_provider.force_flush(timeout_millis)
    meter_provider.force_flush(timeout_millis)
    logger_provider.force_flush(timeout_millis)

    tracer_provider.shutdown()


def run_probe(config: SmokeProbeConfig) -> SmokeProbeResult:
    """Отправляет один span (с потомком), одну метрику и одну запись лога.

    Возвращает идентификаторы прогона, по которым тест (tests/integration)
    дожидается появления телеметрии в Jaeger и Prometheus.
    """
    tracer_provider = create_tracer_provider(config)
    meter_provider = create_meter_provider(config)
    logger_provider = create_logger_provider(config)

    tracer = tracer_provider.get_tracer('scripts.otel_smoke')
    meter = meter_provider.get_meter('scripts.otel_smoke')
    otel_logger = logger_provider.get_logger('scripts.otel_smoke')

    counter = meter.create_counter(
        name=SMOKE_COUNTER_NAME,
        unit='{probe}',
        description='Счётчик прогонов OTel smoke-зонда (T-0.8).',
    )
    histogram = meter.create_histogram(
        name=SMOKE_HISTOGRAM_NAME,
        unit='ms',
        description='Длительность корневого span-а smoke-зонда в миллисекундах.',
    )

    # Контекст span'а заполняется внутри ``with`` (mypy считает ветку после
    # блока всегда определённой, поэтому проверка на None была бы unreachable).
    span_context: trace.SpanContext
    started_at = time.perf_counter()
    with tracer.start_as_current_span(
        SMOKE_SPAN_NAME,
        kind=SpanKind.INTERNAL,
        attributes={SMOKE_ATTRIBUTE_KEY: True, 'paycore.smoke.probe.run': 't-0.8'},
    ) as root_span:
        span_context = root_span.get_span_context()
        # Дочерний span нужен, чтобы в Jaeger была видна иерархия (а не одиночная
        # запись): это проверка проброса контекста внутри процесса.
        child_started_at = time.perf_counter()
        with tracer.start_as_current_span(SMOKE_CHILD_SPAN_NAME, kind=SpanKind.INTERNAL):
            pass
        child_duration_ms = (time.perf_counter() - child_started_at) * 1000

        # Метрики пишутся внутри span, чтобы Collector приложил exemplar со
        # ссылкой на трейс (enable_open_metrics) — переход «график -> трейс».
        counter.add(1, attributes={SMOKE_ATTRIBUTE_KEY: True})
        histogram.record(child_duration_ms, attributes={SMOKE_ATTRIBUTE_KEY: True})
        root_span.set_status(Status(StatusCode.OK))
        emit_probe_log_record(otel_logger, span_context)

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    flush_and_shutdown(tracer_provider, meter_provider, logger_provider)

    return SmokeProbeResult(
        service_name=config.service_name,
        trace_id=format(span_context.trace_id, '032x'),
        span_id=format(span_context.span_id, '016x'),
        otlp_endpoint=config.otlp_endpoint,
        counter_name=SMOKE_COUNTER_NAME,
        histogram_name=SMOKE_HISTOGRAM_NAME,
        span_duration_ms=elapsed_ms,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Отправляет тестовые span/metric/log в OTel Collector (T-0.8).',
    )
    parser.add_argument(
        '--otlp-endpoint',
        default=global_settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        help='Адрес OTel Collector (http://host:port); по умолчанию — из Settings.',
    )
    parser.add_argument(
        '--service-name',
        default=SMOKE_SERVICE_NAME,
        help=f'Имя сервиса в Jaeger (по умолчанию {SMOKE_SERVICE_NAME}).',
    )
    return parser


def main() -> int:
    """CLI-точка входа: отправляет зонд и печатает ссылки для проверки UI."""
    args = build_arg_parser().parse_args()
    result = run_probe(
        SmokeProbeConfig(
            otlp_endpoint=args.otlp_endpoint,
            service_name=args.service_name,
        ),
    )

    print('OTel smoke-проба отправлена в Collector:')
    print(f'  OTLP endpoint : {result.otlp_endpoint}')
    print(f'  service.name  : {result.service_name}')
    print(f'  trace_id      : {result.trace_id}')
    print(f'  span_id       : {result.span_id}')
    print(f'  Метрики       : {result.counter_name}, {result.histogram_name}')
    print(f'  Длительность  : {result.span_duration_ms:.2f} ms')
    print()
    print('Проверить в UI:')
    print(f'  Jaeger     http://localhost:16686/trace/{result.trace_id}')
    print(f'             http://localhost:16686  (service: {result.service_name})')
    print(f'  Prometheus http://localhost:9090  (metric: {result.counter_name})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
