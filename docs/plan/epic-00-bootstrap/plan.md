# ЭПИК 0: Bootstrap репозитория

> **Статус эпика:** `[ ] TODO`  
> **Подтверждение пользователя:** `[ ] Подтверждено`

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

### [ ] T-0.2. pyproject.toml + зависимости
- **Что сделать:**
  - Настроить `uv` в `backend/pyproject.toml`.
  - Зависимости:
    - `core`: pydantic, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, redis, faststream[kafka], grpcio, grpcio-tools, opentelemetry-*, structlog
    - `api`: fastapi, uvicorn, httpx, python-jose
    - `tasks`: taskiq, taskiq-kafka (или taskiq-aio-pika), taskiq-scheduler
    - `payment`: yookassa (в extras: `uv sync --extra yookassa`)
    - `dev`: pytest, pytest-asyncio, testcontainers, ruff, mypy, pre-commit
- **DoD:** `uv sync` в `backend/` проходит успешно.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.3. Конфиг через pydantic-settings
- **Что сделать:**
  - Реализовать единый `Settings`. Поля: DATABASE_URL, REDIS_URL, KAFKA_BOOTSTRAP, GRPC_PORT, HTTP_PORT, OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, JWT_SECRET, PAYMENT_PROVIDER (fake | yookassa), YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY.
  - Написать тест `test_settings_loads_from_env`.
- **DoD:** Тест `test_settings_loads_from_env` проходит.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.4. Линтеры и pre-commit
- **Что сделать:**
  - Настроить ruff, mypy (--strict), pre-commit hooks.
- **DoD:** `pre-commit run --all-files` зелёный.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.5. Makefile
- **Что сделать:**
  - Таргеты: install, lint, test, up, down, migrate, gen-proto, run-api, run-core, run-workers, run-tasks, run-scheduler.
- **DoD:** `make test` и `make lint` работают.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.6. docker-compose.yml с инфраструктурой
- **Что сделать:**
  - Запустить одиночные инстансы: postgres:16, redis:7, kafka (KRaft) + kafka-ui, otel-collector, jaeger:all-in-one, prometheus, grafana.
- **DoD:** `docker compose -f infra/docker-compose.yml up -d`, все сервисы healthy.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.7. OTel Collector — конфиг
- **Что сделать:**
  - `otel_collector/config.yaml`: receivers (otlp :4317/:4318), processors (batch, memory_limiter, resource), exporters (otlp/jaeger, prometheus :8889), pipelines (traces, metrics).
- **DoD:** Collector стартует, принимает OTLP и отдаёт метрики для scrape.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.8. Проверка Collector end-to-end
- **Что сделать:**
  - Минимальное Python-приложение с OTel SDK шлёт тестовый span и metric на Collector (:4317).
  - Проверить появление трейса в Jaeger UI и метрики в Prometheus UI.
- **DoD:** В Jaeger виден span, в Prometheus видна метрика.
- **Подтверждение пользователя:** `[ ]`
