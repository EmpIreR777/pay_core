# ЭПИК 0: Bootstrap репозитория

> **Статус эпика:** `[x] DONE`
> **Подтверждение пользователя:** `[x] Подтверждено`

## Цель
Создать рабочий скелет проекта, базовую инфраструктуру (Postgres, Redis, Kafka, OTel Collector, Jaeger, Prometheus, Grafana), линтеры, конфиги и сквозную проверку OTel Collector.
Архитектурное решение: все сервисы и инфраструктура запускаются в одиночных инстансах (single instance, без репликаций).

---

## Задачи эпика

### [x] T-0.1. Инициализация репозитория и структура папок
- **Что сделать:**
  - Оформить структуру проекта:
    - `proto/payments/v1/`
    - `backend/` (единый бэкенд-сервис с `uv`):
      - `backend/src/api_gateway/` (1 инстанс BFF)
      - `backend/src/core_service/` (1 инстанс Core gRPC)
      - `backend/src/workers/` (1 инстанс FastStream)
      - `backend/src/tasks/` (1 инстанс Taskiq worker + 1 scheduler)
      - `backend/alembic/` (миграции PostgreSQL)
      - `backend/tests/` (`unit/`, `integration/`, `e2e/`)
    - `otel_collector/`
    - `infra/` (`prometheus/`, `grafana/`, `jaeger/`)
    - `docs/` (`plan/`, `adr/`)
  - Настроить `.gitignore` и базовый `README.md`.
- **DoD:** Структура папок оформлена, git репозиторий готов.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.2. pyproject.toml + зависимости
- **Что сделать:**
  - Настроить `uv` в `backend/pyproject.toml`.
  - Зависимости:
    - `core`: pydantic, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, redis, faststream[kafka], grpcio, grpcio-tools, grpcio-reflection, opentelemetry-*, structlog
    - `api`: fastapi, uvicorn, httpx, python-jose
    - `tasks`: taskiq, taskiq-redis, taskiq-faststream
    - `dev`: pytest, pytest-asyncio, pytest-cov, respx, testcontainers, ruff, mypy, pre-commit
- **DoD:** `uv sync` в `backend/` проходит успешно.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.3. Конфиг через pydantic-settings
- **Что сделать:**
  - Реализовать единый `Settings`. Поля: DATABASE_URL, REDIS_URL, KAFKA_BOOTSTRAP, GRPC_PORT, HTTP_PORT, OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, JWT_SECRET, PAYMENT_PROVIDER (fake | yookassa), YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY.
  - Написать тест `test_settings_loads_from_env`.
- **DoD:** Тест `test_settings_loads_from_env` проходит.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.4. Линтеры и pre-commit
- **Что сделать:**
  - Настроить ruff, mypy (--strict), pre-commit hooks.
- **DoD:** `pre-commit run --all-files` зелёный.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.5. Makefile
- **Что сделать:**
  - Таргеты: install, lint, test, up, down, migrate, gen-proto, run-api, run-core, run-workers, run-tasks, run-scheduler.
  - `up`/`down` (docker compose) реализованы в корневом `Makefile` и в `backend/Makefile` намеренно не дублируются.
- **DoD:** `make test` и `make lint` работают.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.6. docker-compose.yml с инфраструктурой
- **Что сделать:**
  - Запустить одиночные инстансы: postgres:16, redis:7, kafka (KRaft) + kafka-ui, otel-collector, jaeger:all-in-one, prometheus, grafana.
- **DoD:** `docker compose up -d` (compose-файл в корне), все сервисы healthy.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.7. OTel Collector — конфиг
- **Что сделать:**
  - `otel_collector/config.yaml`: receivers (otlp :4317/:4318), processors (batch, memory_limiter, resource), exporters (otlp/jaeger, prometheus :8889), pipelines (traces, metrics).
- **DoD:** Collector стартует, принимает OTLP и отдаёт метрики для scrape.
- **Подтверждение пользователя:** `[x]` (подтверждено)

### [x] T-0.8. Проверка Collector end-to-end
- **Что сделать:**
  - Минимальное Python-приложение с OTel SDK шлёт тестовый span и metric на Collector (:4317).
  - Проверить появление трейса в Jaeger UI и метрики в Prometheus UI.
- **DoD:** В Jaeger виден span, в Prometheus видна метрика.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Что сделано:**
  - `backend/scripts/otel_smoke.py` — зонд на OTel SDK: `TracerProvider` + `MeterProvider` +
    `LoggerProvider` с OTLP/gRPC-экспортёрами на `OTEL_EXPORTER_OTLP_ENDPOINT`.
    Шлёт root-span `paycore.otel_smoke.probe` с потомком, counter
    `paycore_smoke_probe_count`, histogram `paycore_smoke_probe_duration` и OTLP log-запись.
    Перед выходом — `force_flush()` + `shutdown()` провайдеров, иначе батчи теряются.
  - Таргеты: `make otel-smoke` в `backend/Makefile` и в корневом `Makefile`.
  - `backend/tests/integration/test_otel_smoke.py` — 5 интеграционных тестов: health Collector'а,
    трейс в Jaeger по `trace_id`, метрика на `:8889`, метрика в Prometheus после scrape,
    self-метрика `otelcol_exporter_sent_spans`. Ожидание — polling с таймаутом, без `sleep`.
    Авто-skip, если стенд не поднят (фикстура `observability_stack` в conftest).
  - `backend/tests/unit/test_otel_smoke.py` — юнит-тесты разбора OTLP endpoint (без сети).
  - `mypy --strict` теперь покрывает и `scripts/`; в `pyproject.toml` добавлен маркер
    `integration`, секция `[tool.pytest.ini_options]` и полный список кириллицы в
    `allowed-confusables` (RUF001/002/003 иначе ругаются на русские комментарии).
  - README: раздел «✅ Сквозная проверка Collector» с таблицей адресов для ручной проверки.
- **Исправления, найденные при ручной проверке:**
  - `otel_collector/config.yaml`: задано `metric_expiration: 1h` (дефолт бинаря `5m`).
    Причина «белого экрана» на `:8889` и пустых графиков: серия удалялась через 5 минут
    после последнего прогона одноразового зонда. Значение вынесено в ENV
    `OTEL_COLLECTOR_METRIC_EXPIRATION` (прод оставляет дефолтным).
  - `infra/grafana/provisioning/dashboards/` — дашборд `otel-overview.json` (11 панелей) +
    провайдер `dashboards.yml`. Раньше дашбордов не было вовсе, поэтому Grafana
    показывала пустоту. Панели ссылаются на datasource через переменную `${datasource}`;
    в `datasources/prometheus.yml` добавлен `httpMethod: POST` для длинных PromQL.
  - `infra/grafana/provisioning/datasources/jaeger.yml` — Jaeger подключён в Grafana вторым
    datasource: Grafana сама трейсы не хранит, но с этим datasource появляется
    `Explore -> Jaeger` (поиск трейсов без перехода на :16686). Проверено:
    `POST /api/datasources/uid/<jaeger>/health` -> `{"status":"OK"}`.
    uid намеренно не задан: Grafana ищет datasource по имени, а явный uid роняет
    провижининг на существующем томе grafana.db.
  - Панель памяти в `otel-overview.json` переделана: вместо одной плитки `RSS`
    график на две линии — `heap_alloc` (реально занято, ~32 МБ) и `memory_rss`
    (удерживается процессом, ~217 МБ) + пунктир лимита `memory_limiter` (384 МиБ).
    Причина: `RSS` у Go всегда выше `heap` (блоки у ОС не возвращаются), из-за чего
    плитка выглядела как «память почти на пределе». Утечки нет: 20 прогонов зонда
    дали `heap` 25.4 -> 30.5 МБ (сработал GC) при неизменном `RSS` 210 МБ.
- **Фактический результат проверки (прогон выполнен):**
  - Jaeger `http://localhost:16686`: сервис `paycore-otel-smoke`, трейс из 2 span'ов
    (root + child, `CHILD_OF`-связь), `service.namespace=pay-core`, `deployment.environment=local`.
  - Collector `:8889/metrics`: `paycore_smoke_probe_count_total` (counter) и
    `paycore_smoke_probe_duration_milliseconds` (histogram) с лейблами
    `service_namespace`, `service_version`, `deployment_environment`.
  - Prometheus `http://localhost:9090`: серия `paycore_smoke_probe_count_total` видна в UI.
  - Collector `:8888/metrics`: `otelcol_exporter_sent_spans{exporter="otlp_grpc/jaeger"}` растёт,
    `otelcol_receiver_accepted_log_records` > 0 (logs-пайплайн тоже работает).
  - Grafana `http://localhost:3000/d/paycore-otel-overview`: 11 панелей отдают данные.
  - `make lint` и `make test` (16 тестов) — зелёные; `pre-commit run --all-files` — зелёный.
- **Известное ограничение: переход «из графика в трейс» (exemplars) НЕ РАБОТАЕТ,
  и это НЕ наша ошибка — это баг upstream.** Исследовано экспериментально в T-0.9
  (замер по шагам через изолированный Collector с `debug`-экспортёром, чтобы увидеть
  сырой OTLP):
  - OTel SDK 1.45.0 exemplar **создаёт** и **отправляет** по OTLP. Проверено на сыром
    OTLP: `Exemplar #0 -> Trace ID: a65d5024f670aecba69ff35d8fd90e8a -> Span ID: a7f654a0faa771e1`.
  - Collector exemplar **получает** (видно в отладке пайплайна).
  - Экспортёр `prometheus` в contrib 0.161.0 exemplars **не отдаёт**: в
    OpenMetrics-выводе `:8889/metrics` ноль комментариев `# {trace_id="..."}`.
    Проверено на изолированном стенде с тем же экспортёром — результат тот же,
    то есть дело НЕ в нашем конфиге.
  - Подтверждение от вендора: open-telemetry/opentelemetry-collector-contrib
    issue **#40424** «Exemplars not exposed in Prometheus /metrics despite being
    received by OpenTelemetry Collector» — закрыт как *stale / not planned*.
    Смежная задача #47159 «Exemplars support for native Prometheus histograms»
    закрыта как *completed* (апр. 2026), но в 0.161.0 поддержки ещё нет.
  - Итог: цепочка рвётся на звене **Collector → Prometheus**. Флаг
    `--enable-feature=exemplar-storage` в Prometheus **не поможет** — хранить
    нечего. Варианты: обновление contrib после появления поддержки либо переход
    на экспортёр `prometheus_remote_write`.
  - **Прежняя гипотеза в этом документе была неверной.** Утверждалось, что дело в
    одноразовости smoke-зонда («пишет метрику один раз и умирает до скрейпа»).
    Это не так: `metric_expiration: 1h` удерживает серию, и Prometheus стабильно её
    скрейпит (проверено — серия видна). Настоящая причина — баг экспортёра, см. выше.
