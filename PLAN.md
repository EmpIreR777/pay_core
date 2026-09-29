# 🗺️ Master Plan реализации: Payment Gateway

> **⚠️ ПРАВИЛО ПРОЦЕССА И ПЕРЕХОДА МЕЖДУ ЭТАПАМИ:**
> 1. Мы двигаемся строго последовательно: **Эпик за Эпиком, задача за задачей (T-X.Y)** в отдельных сессиях/контекстных окнах.
> 2. Пометка статуса (`[x]` или `DONE`) выставляется **ТОЛЬКО ПОСЛЕ явного подтверждения и согласования пользователем** в чате.
> 3. До подтверждения статус задачи/эпика остается `[ ] IN_PROGRESS` или `[ ] TODO`.

---

## 🎯 Целевая архитектура (одиночные инстансы, без репликаций)

```text
Клиент ──HTTP──► [FastAPI]               ← BFF, тонкий шлюз (1 инстанс)
                       │
                       │ gRPC
                       ▼
                 [Core Service]          ← Clean Architecture, бизнес-логика (1 инстанс)
                       │
        ┌──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   [Postgres]      [Redis]        [Kafka]      [Taskiq broker]
                                                    │
                                                    ▼
                                            [Taskiq worker]
                                                    │
                                          (задачи: reconciliation,
                                           cleanup, retry payments,
                                           scheduled settlements)
                       │
                       │ PaymentProvider (порт)
                       ▼
              [FakePaymentProvider]      ← default
              [YooKassaPaymentProvider]  ← ENV-flag

Observability:
  App ──OTLP──► [OTel Collector] ──┬──► [Jaeger]     (traces)
                                   ├──► [Prometheus] (metrics scrape)
                                   └──► [Loki]       (logs, optional)
```

**Ключевые архитектурные решения:**
- **OTel Collector** — единая точка приёма телеметрии. Приложение шлёт всё OTLP на Collector, а он fan-out'ит в Jaeger (traces) и Prometheus (metrics). Приложение разгружено, backends можно менять без передеплоя.
- **Taskiq** — async-native очередь задач, работает поверх Kafka (или отдельного RabbitMQ). `taskiq-scheduler` — для периодических задач (cleanup, reconciliation).
- **PaymentProvider** — доменный порт; реализация выбирается через ENV. Разработка и тесты не зависят от внешнего API ЮKassa.
- **Одиночные инстансы (single instance)**: Все сервисы и воркеры пока работают в одиночном экземпляре без репликаций.

---

## 📊 Порядок работы и зависимости

```text
ЭПИК 0 (bootstrap + OTel Collector)
   │
   ├── ЭПИК 1 (domain) ──► ЭПИК 2 (application + PaymentProvider порт)
   │                            │
   │                            ├── ЭПИК 3 (postgres)
   │                            ├── ЭПИК 4 (idempotency)
   │                            ├── ЭПИК 5 (locks)
   │                            │
   │                            ▼
   │                       ЭПИК 6 (gRPC server + OTel)
   │                            │
   │                            ▼
   │                       ЭПИК 7 (FastAPI BFF + webhook)
   │                            │
   │                            ▼
   │                       ЭПИК 8 (outbox + kafka)
   │                            │
   │                            ├── ЭПИК 9 (workers)
   │                            └── ЭПИК 10 (Taskiq + scheduler)
   │                            │
   │                            ▼
   │                       ЭПИК 11 (resilience)
   │                            │
   │                            ▼
   │                       ЭПИК 12 (observability ⭐ traceparent)
   │                            │
   │                            ▼
   │                       ЭПИК 13 (e2e/load/chaos)
   │                            │
   └────────────────────► ЭПИК 14 (финализация)
```

---

## 🗂 Эпики и детальные планы

Каждый эпик подробно расписан в файле своей папки `docs/plan/epic-XX-*/plan.md`:

| № | Эпик | Детальный файл плана | Статус | Подтверждено пользователем |
|---|---|---|---|---|
| **0** | **Bootstrap репозитория** | [docs/plan/epic-00-bootstrap/plan.md](docs/plan/epic-00-bootstrap/plan.md) | `[x] DONE` | `[x]` |
| **1** | **Domain Layer** | [docs/plan/epic-01-domain/plan.md](docs/plan/epic-01-domain/plan.md) | `[x] DONE` | `[x]` |
| **2** | **Application Layer (Use-cases & Ports)** | [docs/plan/epic-02-application/plan.md](docs/plan/epic-02-application/plan.md) | `[x] DONE` | `[x]` |
| **3** | **Postgres Infrastructure** | [docs/plan/epic-03-postgres/plan.md](docs/plan/epic-03-postgres/plan.md) | `[ ] TODO` | `[ ]` |
| **4** | **Idempotency** | [docs/plan/epic-04-idempotency/plan.md](docs/plan/epic-04-idempotency/plan.md) | `[ ] TODO` | `[ ]` |
| **5** | **Distributed Locks** | [docs/plan/epic-05-distributed-locks/plan.md](docs/plan/epic-05-distributed-locks/plan.md) | `[ ] TODO` | `[ ]` |
| **6** | **gRPC Server** | [docs/plan/epic-06-grpc-server/plan.md](docs/plan/epic-06-grpc-server/plan.md) | `[ ] TODO` | `[ ]` |
| **7** | **FastAPI Gateway (BFF)** | [docs/plan/epic-07-fastapi-gateway/plan.md](docs/plan/epic-07-fastapi-gateway/plan.md) | `[ ] TODO` | `[ ]` |
| **8** | **Kafka + Outbox + FastStream** | [docs/plan/epic-08-kafka-outbox/plan.md](docs/plan/epic-08-kafka-outbox/plan.md) | `[ ] TODO` | `[ ]` |
| **9** | **Workers (FastStream consumers)** | [docs/plan/epic-09-workers/plan.md](docs/plan/epic-09-workers/plan.md) | `[ ] TODO` | `[ ]` |
| **10** | **Taskiq — фоновые и периодические задачи** | [docs/plan/epic-10-taskiq/plan.md](docs/plan/epic-10-taskiq/plan.md) | `[ ] TODO` | `[ ]` |
| **11** | **Resilience** | [docs/plan/epic-11-resilience/plan.md](docs/plan/epic-11-resilience/plan.md) | `[ ] TODO` | `[ ]` |
| **12** | **Observability (OpenTelemetry)** | [docs/plan/epic-12-observability/plan.md](docs/plan/epic-12-observability/plan.md) | `[ ] TODO` | `[ ]` |
| **13** | **E2E, Race, Load, Chaos** | [docs/plan/epic-13-e2e-load-chaos/plan.md](docs/plan/epic-13-e2e-load-chaos/plan.md) | `[ ] TODO` | `[ ]` |
| **14** | **Финализация** | [docs/plan/epic-14-finalization/plan.md](docs/plan/epic-14-finalization/plan.md) | `[ ] TODO` | `[ ]` |

---

## 📋 Сводный чек-лист

- [ ] Clean Architecture с портами и адаптерами
- [ ] Domain-Driven Design (entities, VOs, events)
- [ ] Async SQLAlchemy 2.0 + Alembic
- [ ] Оптимистичные и пессимистичные локи
- [ ] Распределённые локи в Redis (SET NX + Lua + watchdog)
- [ ] Идемпотентность: HTTP, gRPC, Kafka, webhook от провайдера
- [ ] Transactional Outbox
- [ ] Kafka: партиционирование, consumer groups, DLQ, идемпотентность
- [ ] FastStream
- [ ] Taskiq + taskiq-scheduler как замена Celery/APScheduler
- [ ] Taskiq: async-native задачи + периодические задачи + retry + OTel в задачах
- [ ] gRPC: unary, server-streaming, interceptors, deadlines, reflection, health-check
- [ ] FastAPI как тонкий BFF, error mapping, SSE, webhook endpoint
- [ ] PaymentProvider как порт: Fake (default) + YooKassa (ENV)
- [ ] Reconciliation с внешним провайдером
- [ ] Retry + circuit breaker для внешнего провайдера
- [ ] Retry с backoff и jitter (gRPC)
- [ ] Circuit breaker (gRPC)
- [ ] Deadline propagation через все слои
- [ ] Graceful shutdown
- [ ] Health/readiness probes
- [ ] OpenTelemetry Collector как центральная точка приёма + fan-out (Jaeger + Prometheus)
- [ ] Пропагация traceparent через Kafka headers (inject/extract)
- [ ] Пропагация traceparent через Taskiq tasks metadata
- [ ] OTel: auto + manual spans, context propagation через HTTP/gRPC/Kafka/Taskiq
- [ ] Prometheus + Grafana + Jaeger (через Collector)
- [ ] Structured logging с trace correlation
- [ ] Race-тесты и доказывание корректности под concurrency
- [ ] Нагрузочное тестирование
- [ ] Chaos-lite (включая проверку устойчивости при сбоях)
- [ ] Контрактные тесты с внешним API через VCR-подход
- [ ] Docker multi-stage, compose с healthcheck
- [ ] ADR как инструмент команды
