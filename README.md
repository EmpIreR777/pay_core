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
| Loki 3 (logs)       | `grafana/loki:3.5.12`             | http://localhost:3100        | `loki:3100/otlp` (OTLP/HTTP) |
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
                               └─ logs ────► otlp_http/loki ──► loki:3100/otlp ──► Loki UI :3100
                                    (вторая ветка: debug → stdout, страховка на dev)
```

| Слой | Компонент | Зачем |
|------|-----------|-------|
| receiver | `otlp` (grpc `:4317` + http `:4318`) | Единственный вход для телеметрии приложений. `max_recv_msg_size_mib: 16` — чтобы большие батчи не отбрасывались с `RESOURCE_EXHAUSTED` |
| processor | `memory_limiter` | **Первым** в каждом пайплайне: backpressure-клапан, не даёт Collector'у упасть по OOM. Лимиты — через ENV `OTEL_COLLECTOR_MEMORY_LIMIT_MIB` (384) / `..._SPIKE_LIMIT_MIB` (96) |
| processor | `resource` | `action: upsert` добавляет `service.namespace=pay-core` и `deployment.environment` (совпадает с `external_labels` Prometheus), не затирая `service.version` от приложения |
| processor | `batch` | **Последним**: склеивает мелкие батчи (5s / 1024) перед отправкой |
| exporter | `otlp_grpc/jaeger` | Трейсы в Jaeger с `sending_queue` + `retry_on_failure` — Jaeger перезапускается, трейсы досылаются |
| exporter | `prometheus` | Публикует метрики на `:8889`; `enable_open_metrics` даёт exemplars (переход «из графика в трейс» в Grafana) |
| exporter | `debug` | Дублирует логи в stdout Collector'а. `verbosity: detailed` — страховка на случай, если Loki недоступен. В проде ветку убирают |
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

## 📝 Loki — логи

Конфиг: [`infra/loki/loki-config.yaml`](infra/loki/loki-config.yaml). Loki закрывает
третий сигнал телеметрии и **единственный, который переживает рестарт контейнеров**:
трейсы живут в RAM Jaeger и умирают вместе с ним, логи лежат в томе `loki-data`.

```text
app ──OTLP──► otel-collector ──logs──► otlp_http/loki ──HTTP POST──► loki:3100/otlp/v1/logs
                                                                  └──► Loki UI :3100
                                                                       └──► Grafana (datasource Loki)
```

| Слой | Решение | Зачем |
|------|---------|-------|
| exporter | `otlp_http/loki` | Экспортёра `loki` в contrib 0.161.0 **больше нет** (проверено `otelcol-contrib components`). Современный путь — нативный OTLP-приёмник Loki 3.x, шлюзом служит обычный `otlp_http` |
| endpoint | `http://loki:3100/otlp` | Экспортёр сам дописывает `/v1/logs`. Если вписать `/v1/logs` вручную, получится `/otlp/v1/logs/v1/logs` → 404 |
| exporter | `debug` (вторая ветка) | Страховка: при недоступном Loki логи не теряются. `verbosity: detailed` — при `basic` печатается только сводка «log records: 1» без содержимого |
| storage | том `loki-data`, `retention_period: 168h` | Намеренно столько же, сколько `PROMETHEUS_RETENTION`. Разные окна у метрик и логов — источник путаницы «в Grafana метрика есть, а логов нет» |
| разметка | `otlp_config.resource_attributes` | `service.namespace` и `deployment.environment` идут в **индекс** потока; `trace_id`/`span_id` — в structured metadata (дёшево, ищется, не раздувает индекс) |

> ⚠️ **Кардинальность.** `index_label` попадает в индекс и перемножает число
> потоков на диске. Поэтому `service.instance.id` и `trace_id` в индекс не
> идут — иначе каждый перезапуск процесса плодил бы потоки.

### Как выглядят наши логи в Loki

Формат хранения в Loki — **потоки (streams)**, а не документы. Каждый поток = набор
лейблов + текстовые строки. Наши логи лежат так:

```
line   : 'paycore otel smoke probe log record'     ← ТОЛЬКО тело сообщения
ts     : 1790592758225851000                       ← наносекунды epoch
stream : { ...лейблы и structured metadata... }
```

**Индексные лейблы** (по ним Loki реально фильтрует — их всего 4):
| Лейбл | Значение | Откуда |
|-------|----------|--------|
| `service_name` | `paycore-otel-smoke` | от приложения |
| `service_namespace` | `pay-core` | resource-процессор Collector'а |
| `deployment_environment` | `local` | resource-процессор Collector'а |
| `service_instance_id` | `87e611ce-…` | от приложения |

Проверяется запросом:
```bash
curl -sS -G http://localhost:3100/loki/api/v1/labels | python3 -m json.tool
```

**Structured metadata** (приходит в ответе, но НЕ индексируется — по ней нельзя
фильтровать, зато она почти бесплатна): `trace_id`, `span_id`, `severity_text`,
`severity_number`, `detected_level`, `scope_name`, `service_version`, `flags`,
`observed_timestamp`, `telemetry_sdk_*`, `paycore_smoke_probe`.

> ⚠️ **Важное следствие для T-12.10.** Сейчас `trace_id` лежит в **structured
> metadata**, а не в тексте строки. Значит фильтровать логи по трейсу
> (`{...} | trace_id="..."`) **нельзя** — Loki не умеет искать по structured
> metadata в LogQL. Два рабочих варианта:
> 1. Писать `trace_id` в **тело** сообщения (structlog) — тогда заработает и
>    фильтр, и `derivedFields` «клик по логу → трейс» из `datasources/loki.yml`.
> 2. Поднять `trace_id` до **индексного лейбла** в `loki-config.yaml` — но это
>    взорвёт кардинальность (один поток на каждый трейс), делать нельзя.
>
> Правильный выбор — вариант 1.

### Удобная работа в Grafana

| Что | Где | Как |
|-----|-----|-----|
| Поиск по логам | `http://localhost:3000/explore` → Loki | LogQL: `{service_name="payment-gateway"} \|= "error"` |
| Панели логов | Дашборд `paycore-otel-overview` | Блок «📝 Логи приложений» внизу: поток логов + график активности |
| Лог → трейс | Клик по `trace_id` в строке лога | `derivedFields` в `datasources/loki.yml` открывает трейс в Jaeger |
| Сырой Loki | http://localhost:3100 | UI Loki с тем же LogQL |

> Ссылки «лог → трейс» заработают, когда приложения начнут писать `trace_id`
> в текст сообщения — это **T-12.10** (structlog с автоподмешиванием
> `trace_id`/`span_id`). Сам Loki и поиск по нему уже работают.

> ⚠️ **Grafana: uid datasource'ов заданы явно** (`jaeger`, `loki`) — на них
> ссылается `derivedFields`. Автосгенерированный uid после пересоздания тома
> Grafana сломал бы ссылки. Если меняете uid в `loki.yml` — меняйте и в
> `jaeger.yml`. Миграция существующего стенда: `docker compose down -v grafana &&
> docker compose up -d grafana` (всё восстановится из репозитория).

## ✅ Сквозная проверка Collector

Конфиг Collector'а проверен не только на валидность, но и **сквозным
прогоном**: приложение с OTel SDK отправляет тестовые `span`, `metric` и
`log` на `:4317`, и мы убеждаемся, что они доехали до Jaeger и Prometheus.

### Ручной прогон

```bash
make up                                   # поднять стенд (если ещё не поднят)
cd backend && make otel-smoke              # отправить тестовую телеметрию
```

Скрипт [`backend/scripts/otel_smoke.py`](backend/scripts/otel_smoke.py) создаёт
`TracerProvider` + `MeterProvider` + `LoggerProvider` с OTLP/gRPC-экспортёрами
на адрес из `OTEL_EXPORTER_OTLP_ENDPOINT` и шлёт:

| Что | Имя | Куда попадает |
|-----|-----|----------------|
| Корневой span | `paycore.otel_smoke.probe` | Jaeger |
| Дочерний span | `paycore.otel_smoke.probe.child` | Jaeger (проверка иерархии) |
| Counter | `paycore_smoke_probe_count` | Prometheus (`..._count_total`) |
| Histogram | `paycore_smoke_probe_duration` | Prometheus (`..._milliseconds`) |
| Log record | `paycore otel smoke probe log record` | stdout Collector'а (`debug`) |

После прогона печатается `trace_id` и готовые ссылки для ручной проверки.

> Метрики пишутся **внутри** активного span'а, поэтому Collector приклеивает
> к ним `exemplar` со ссылкой на трейс — это и есть переход «из графика в трейс».

### Что и где смотреть

| Проверка | Адрес | Что искать |
|----------|-------|------------|
| **Трейсы** | http://localhost:16686 | Service `paycore-otel-smoke` → 2 span'а |
| **Трейс по ID** | http://localhost:16686/trace/`<trace_id>` | Детали конкретного прогона |
| **Метрики (до scrape)** | http://localhost:8889/metrics | `paycore_smoke_probe_count_total` |
| **Метрики в Prometheus** | http://localhost:9090 | `paycore_smoke_probe_count_total` |
| **Дашборд Grafana** | http://localhost:3000/d/paycore-otel-overview | Графики без ручного ввода запросов |
| **Explore → Jaeger** | http://localhost:3000/explore | Поиск трейсов прямо в Grafana |
| **Self-метрики Collector'а** | http://localhost:8888/metrics | `otelcol_exporter_sent_spans{exporter="otlp_grpc/jaeger"}` |
| **Health Collector'а** | http://localhost:13133 | `{"status": "Server available"}` |
| **Логи Collector'а** | `docker logs -f pay-otel-collector` | Запись `paycore otel smoke probe log record` |
| **Targets Prometheus** | http://localhost:9090/targets | Job `otel-collector` → `up` |

### Автоматическая проверка

```bash
cd backend
make test-integration   # 5 интеграционных тестов T-0.8
make test               # unit + integration
```

Тесты ждут появления телеметрии **polling'ом с явным таймаутом** (никаких
`sleep` «наугад»): трейс — в Jaeger по `trace_id`, метрика — в экспортёре
Collector'а и в Prometheus после `scrape_interval` (15s).

Если стенд не поднят, интеграционные тесты **пропускаются** (skip) с понятным
сообщением, а не падают — `make test` остаётся зелёным без Docker.

### Дашборд Grafana

Файл [`infra/grafana/provisioning/dashboards/otel-overview.json`](infra/grafana/provisioning/dashboards/otel-overview.json)
подхватывается при старте Grafana (провайдер описан в `dashboards.yml` рядом).
Открывать: **http://localhost:3000/d/paycore-otel-overview**

| Панель | Что показывает |
|--------|----------------|
| Прогоны зонда (counter) | Сколько раз запускали `make otel-smoke` — растёт ступеньками |
| Скорость прогонов | `rate(...)` — активен ли зонд прямо сейчас |
| Принято vs отправлено (spans) | Принял Collector на `:4317` → отправил в Jaeger. Расхождение = потери |
| Принято vs отправлено (metrics) | То же для метрик: receiver → экспортёр на `:8889` |
| Uptime / Память / Очередь | Здоровье Collector'а с цветовыми порогами |

Панели ссылаются на datasource через переменную `${datasource}`, поэтому
привязываются к Prometheus автоматически.

### Grafana как единое окно: метрики + трейсы

Grafana сама трейсы **не хранит** — это «окно» в хранилища. Поэтому в Grafana
провижинятся **два** источника:

| Datasource | Что хранит | Что даёт в Grafana |
|------------|------------|--------------------|
| `Prometheus` | метрики | все панели дашборда + self-метрики Collector'а |
| `Jaeger` | трейсы | **Explore → Jaeger**: поиск трейсов без перехода на :16686 |

Файлы: `infra/grafana/provisioning/datasources/prometheus.yml` и `.../jaeger.yml`.

### Почему метрика «исчезает» через несколько минут

У prometheus-экспортёра Collector'а есть параметр `metric_expiration` — сколько
он **держит** метрику после последнего обновления. Дефолт бинаря — `5m`: через
пять минут молчания серия удаляется с `:8889`, Prometheus помечает её stale, и
график становится пустым.

Наш зонд одноразовый (отправил — и процесс завершился), поэтому на dev это
выглядело как «всё сломалось». В конфиге выставлено `metric_expiration: 1h`
(через ENV `OTEL_COLLECTOR_METRIC_EXPIRATION`): окно достаточно большое, чтобы
спокойно открыть Grafana и посмотреть график.

**Прод-значение** оставляем дефолтным: там метрики шлёт живой сервис постоянно,
и протухание серии = сигнал алерта, а не особенность одноразового зонда.

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
