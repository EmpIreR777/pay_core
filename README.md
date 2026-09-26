# 💳 Payment Gateway (Core)

Высоконадежный финансовый платежный шлюз на базе Clean Architecture, Domain-Driven Design (DDD) и асинхронного Python 3.12+.

## 🎯 Архитектурный контекст (Single Instance)

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
                                   └──► [Grafana]    (dashboards)
```

## 📁 Структура проекта

```text
.
├── proto/payments/v1/       # Protobuf контракты gRPC
├── api_gateway/             # FastAPI BFF (тонкий шлюз, 1 инстанс)
├── core_service/            # Core Service (DDD, бизнес-логика, 1 инстанс)
├── workers/                 # FastStream фоновые воркеры (Kafka consumers, 1 инстанс)
├── tasks/                   # Taskiq worker & scheduler (периодические и отложенные задачи)
├── otel_collector/          # Конфигурация OpenTelemetry Collector
├── tests/                   # Пирамида тестов (unit, integration, e2e)
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── infra/                   # Инфраструктурные сервисы (Prometheus, Grafana, Jaeger)
│   ├── prometheus/
│   ├── grafana/
│   └── jaeger/
├── alembic/                 # Миграции базы данных PostgreSQL
└── docs/                    # Документация проекта, планы эпиков и ADR
    ├── plan/
    └── adr/
```

## 🛠️ Стек технологий

- **Язык**: Python 3.12+ (строгая типизация `mypy --strict`)
- **Пакетный менеджер**: `uv`
- **Линтинг и форматирование**: `ruff`
- **Веб-фреймворк**: FastAPI (BFF)
- **RPC**: gRPC (`grpcio`, `grpcio-tools`)
- **База данных**: PostgreSQL 16 (`asyncpg`, `SQLAlchemy 2.0`) + Alembic
- **Кэш и блокировки**: Redis 7
- **Брокер сообщений**: Kafka (KRaft) + FastStream
- **Очередь задач**: Taskiq + Taskiq Scheduler
- **Observability**: OpenTelemetry Collector, Jaeger, Prometheus, Grafana
