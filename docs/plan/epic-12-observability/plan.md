# ЭПИК 12: Observability (OpenTelemetry)

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Полная сквозная наблюдаемость (Distributed Tracing, Metrics, Structured Logs).
OTel Collector как единая точка приёма OTLP -> экспорт в Jaeger (:4317) и Prometheus (:8889).
Критический тикет: пропагация W3C traceparent через Kafka headers и Taskiq metadata.

---

## Задачи эпика

### [ ] T-12.1. OTel Collector — production-ready конфиг
- **Что сделать:** otel_collector/config.yaml: receivers, memory_limiter, batch, resource, exporters (jaeger, prometheus :8889).
- **DoD:** Collector стабилен, собирает метрики и трейсы.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.2. Auto-instrumentation (FastAPI, gRPC, SQLAlchemy, Redis)
- **Что сделать:** Инструментация фреймворков и библиотек с отправкой данных в Collector.
- **DoD:** 1 HTTP запрос порождает детальное дерево спанов в Jaeger.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.3. Ручные спаны в use-cases
- **Что сделать:** Кастомные спаны в CreatePaymentUseCase с атрибутами account_id, payment_id, idempotency_key, amount, provider.
- **DoD:** Поиск трейсов по бизнес-атрибутам в Jaeger работает.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.4. Пропагация traceparent через Kafka headers
- **Что сделать:**
  - В Outbox сохранять traceparent колонку.
  - При публикации в Kafka инжектировать traceparent в Kafka headers (inject(headers)).
  - В FastStream консьюмере извлекать контекст (extract(headers)).
- **DoD:** Единый сквозной трейс покрывает HTTP -> gRPC -> outbox -> Kafka -> worker.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.5. Пропагация traceparent в Taskiq-задачах
- **Что сделать:** Inject traceparent в метаданные таски при enqueue, extract в Taskiq worker.
- **DoD:** Трейс покрывает постановку и исполнение задачи.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.6. Пропагация traceparent через gRPC metadata
- **Что сделать:** Проброс W3C TraceContext в метаданных gRPC клиент -> сервер.
- **DoD:** Связанные спаны между FastAPI gateway и Core Service.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.7. Метрики Prometheus через Collector
- **Что сделать:** Push метрик из сервисов в Collector по OTLP, Prometheus скрейпит порт :8889 коллектора.
- **DoD:** Prometheus отображает метрики сервисов без прямого обращения к ним.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.8. Кастомные метрики
- **Что сделать:** payments_created_total, payment_creation_duration_seconds, idempotency_hits_total, lock_acquisition_duration_seconds, outbox_pending_count, provider_call_duration_seconds, provider_errors_total.
- **DoD:** Метрики собираются и видны в Prometheus.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.9. Grafana dashboard
- **Что сделать:** Дашборд с графиками RPS, p95 latency, error rate, lock contention, outbox lag, provider metrics.
- **DoD:** JSON-дашборд в infra/grafana/dashboards/.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-12.10. Structured logging с trace correlation
- **Что сделать:** structlog вывод в JSON с автоматическим подмешиванием trace_id и span_id.
- **DoD:** По trace_id из Jaeger можно найти все связанные логи.
- **Подтверждение пользователя:** `[ ]`
