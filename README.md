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
| OTel Collector      | `otel/opentelemetry-collector-contrib:0.161.0` | OTLP: `localhost:4317` / `:4318`, метрики: `localhost:8889` / self: `localhost:8888` | `otel-collector:4317` |
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

## 🔭 OTel Collector — конвейер телеметрии (T-0.7)

Конфиг: [`otel_collector/config.yaml`](otel_collector/config.yaml). Collector —
единая точка приёма телеметрии: приложения шлют **только** OTLP на
`:4317`/`:4318`, а он делает fan-out в Jaeger и Prometheus.

```text
app ──OTLP──► otel-collector ──┬─ traces ──► otlp_grpc/jaeger ──► jaeger:4317 ──► Jaeger UI :16686
   (:4317/:4318)                ├─ metrics ─► prometheus :8889 ──► Prometheus scrape (job otel-collector)
                               └─ logs ────► debug (stdout)      ──► Loki подключим в Эпике 12
```

| Слой | Компонент | Зачем |
|------|-----------|-------|
| receiver | `otlp` (grpc `:4317` + http `:4318`) | Единственный вход для телеметрии приложений. `max_recv_msg_size_mib: 16` — чтобы большие батчи не отбрасывались с `RESOURCE_EXHAUSTED` |
| processor | `memory_limiter` | **Первым** в каждом пайплайне: backpressure-клапан, не даёт Collector'у упасть по OOM. Лимиты — через ENV `OTEL_COLLECTOR_MEMORY_LIMIT_MIB` (384) / `..._SPIKE_LIMIT_MIB` (96) |
| processor | `resource` | `action: upsert` добавляет `service.namespace=pay-core` и `deployment.environment` (совпадает с `external_labels` Prometheus), не затирая `service.version` от приложения |
| processor | `batch` | **Последним**: склеивает мелкие батчи (5s / 1024) перед отправкой |
| exporter | `otlp_grpc/jaeger` | Трейсы в Jaeger с `sending_queue` + `retry_on_failure` — Jaeger перезапускается, трейсы досылаются |
| exporter | `prometheus` | Публикует метрики на `:8889`; `enable_open_metrics` даёт exemplars (переход «из графика в трейс» в Grafana) |
| exporter | `debug` | Логи по OTLP пока в stdout; заменяется на Loki-экспортёр в Эпике 12 |
| extension | `health_check` (`:13133`) | Готовность Collector'а |

**Два порта метрик — не путать:**

- `:8889` — телеметрия **приложений**, транслированная Collector'ом.
  Prometheus скрейпит его джобом `otel-collector`.
- `:8888` — **self-метрики** Collector'а (`otelcol_receiver_accepted_spans`,
  `otelcol_exporter_sent_spans`, `otelcol_processor_refused_*`). Джоб
  `otel-collector-self`. Нужны, чтобы отличать «сервисы не шлют телеметрию»
  от «Collector принимает и теряет».

**Resource-атрибуты → лейблы Prometheus.** Актуальная для contrib 0.161.0 опция —
`resource_constant_labels` (старая `resource_to_telemetry_conversion` помечена
`deprecated` и заменяется). `service.name` и `service.instance.id` исключены:
Prometheus уже маппит их в `job` и `instance`, дублирование только раздувает
кардинальность серий.

### Верификация конфига

```bash
# 1. Валидация конфига тем же бинарником, что и healthcheck в compose:
docker run --rm \
  -v "$PWD/otel_collector/config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
  -e OTEL_COLLECTOR_MEMORY_LIMIT_MIB=384 \
  otel/opentelemetry-collector-contrib:0.161.0 \
  validate --config=file:/etc/otelcol-contrib/config.yaml   # exit 0 = OK

# 2. Список реально доступных компонентов в образе (сверка имён):
docker run --rm otel/opentelemetry-collector-contrib:0.161.0 components

# 3. Health-endpoint Collector'а:
curl -sS http://localhost:13133/

# 4. Метрики приложений и self-метрики Collector'а:
curl -sS http://localhost:8889/metrics
curl -sS http://localhost:8888/metrics | grep otelcol_exporter_sent_spans
```

> ⚠️ **Важно про версии.** В contrib ≥ 0.12x OTLP-exporter разделён на два
> компонента: `otlp_grpc` и `otlp_http`. Имя `otlp` в 0.161.0 **не существует** —
> использование старого имени валит конфиг с ошибкой запуска. Всегда сверяйтесь
> с `otelcol-contrib components` под нужную версию образа.

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
