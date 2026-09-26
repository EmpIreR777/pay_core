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
├── backend/                 # Единый бэкенд проект на uv
│   ├── src/
│   │   ├── api_gateway/     # FastAPI BFF (тонкий шлюз, 1 инстанс)
│   │   ├── core_service/    # Core Service (DDD, gRPC сервер, 1 инстанс)
│   │   ├── workers/         # FastStream фоновые воркеры (Kafka consumers, 1 инстанс)
│   │   └── tasks/           # Taskiq worker & scheduler (периодические и отложенные задачи)
│   ├── alembic/             # Миграции базы данных PostgreSQL
│   ├── tests/               # Пирамида тестов (unit, integration, e2e)
│   │   ├── unit/
│   │   ├── integration/
│   │   └── e2e/
│   ├── pyproject.toml       # Зависимости и конфигурация uv, ruff, mypy, pytest
│   └── uv.lock              # Lock-файл зависимостей uv
├── proto/payments/v1/       # Protobuf контракты gRPC
├── otel_collector/          # Конфигурация OpenTelemetry Collector
├── infra/                   # Инфраструктурные сервисы (Prometheus, Grafana, Jaeger)
│   ├── prometheus/
│   ├── grafana/
│   └── jaeger/
└── docs/                    # Документация проекта, планы эпиков и ADR
    ├── plan/
    └── adr/
```

## 🐳 Локальная инфраструктура (T-0.6)

Вся инфраструктура поднимается одним compose-файлом в **одиночных инстансах**
(без репликаций), в сети `pay-core-net`:

```bash
cp .env.example .env   # опционально: свои порты/креды
make up                # docker compose up -d (файл в корне, -f не нужен)
make ps                # статус сервисов
make down              # остановить (данные в volume'ах сохраняются)
make clean             # полный сброс вместе с томами
```

Compose-файл лежит в корне (`docker-compose.yml`) намеренно: Docker Compose ищет
`.env` в каталоге compose-файла, поэтому только из корня `.env` подхватывается
автоматически. Конфиги самих сервисов остались в `infra/` (prometheus, grafana),
конфиг Collector'а — в `otel_collector/`.

| Сервис              | Образ                              | UI / Endpoint (хост)         | Внутри сети                |
|---------------------|------------------------------------|------------------------------|----------------------------|
| PostgreSQL 16       | `postgres:16-alpine`               | `localhost:5432`             | `postgres:5432`            |
| Redis 7             | `redis:7-alpine`                   | `localhost:6379`             | `redis:6379`               |
| Kafka 4 (KRaft)     | `apache/kafka:4.1.2`               | `localhost:9092`             | `kafka:29092` (INTERNAL)   |
| Kafka UI            | `provectuslabs/kafka-ui:v0.7.2`    | http://localhost:8080        | —                          |
| OTel Collector      | `otel/opentelemetry-collector-contrib:0.161.0` | OTLP: `localhost:4317` / `:4318` | `otel-collector:4317` |
| Jaeger (traces)     | `jaegertracing/all-in-one:1.76.0`  | http://localhost:16686       | `jaeger:4317` (OTLP)       |
| Prometheus (metrics)| `prom/prometheus:v3.15.0`          | http://localhost:9090        | `prometheus:9090`          |
| Grafana             | `grafana/grafana:13.0.9`           | http://localhost:3000        | `grafana:3000`             |

**Важные детали реализации:**

- **Kafka: три listener'а.** `EXTERNAL://localhost:9092` — для приложений на хосте,
  `INTERNAL://kafka:29092` — для контейнеров compose, `CONTROLLER` — для KRaft.
  Разделение обязательно: с одним `advertised listener` клиент внутри сети
  получает `localhost:9092` в метаданных и не может подключиться.
- **Jaeger не публикует 4317/4318 на хосте** — эти порты принадлежат Collector'у.
  Collector отдаёт трейсы в Jaeger по OTLP внутри docker-сети.
- **Volume Kafka монтируется в `/var/lib/kafka/data`** (а не в `/var/lib/kafka`):
  образ объявляет `VOLUME /var/lib/kafka/data`, и том в родительском каталоге
  перекрывается анонимным томом образа — данные теряются при `down`.
- **Healthcheck'и.** У каждого сервиса есть проверка готовности, а не только
  «процесс жив», и `depends_on: condition: service_healthy` для зависимостей.
  У образа OTel Collector (distroless) нет `/bin/sh`, `wget` и `curl`, поэтому
  его healthcheck — `otelcol-contrib validate`.
- **Данные переживают `down`/`up`.** Наполнение томов проверено: топики Kafka,
  данные Postgres и AOF Redis остаются на месте.

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
