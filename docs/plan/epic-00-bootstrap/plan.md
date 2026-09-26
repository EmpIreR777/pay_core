# ЭПИК 0: Bootstrap репозитория

> **Статус эпика:** `[ ] TODO`  
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Создать рабочий скелет проекта, базовую инфраструктуру (Postgres, Redis, Kafka, OTel Collector, Jaeger, Prometheus, Grafana), линтеры, конфиги и сквозную проверку OTel Collector.
Архитектурное решение: все сервисы и инфраструктура запускаются в одиночных инстансах (single instance, без репликаций).

---

## Задачи эпика

### [ ] T-0.1. Инициализация репозитория и структура папок
- **Что сделать:**
  - Оформить структуру проекта:
    - `proto/payments/v1/`
    - `api_gateway/` (1 инстанс BFF)
    - `core_service/` (1 инстанс Core gRPC)
    - `workers/` (1 инстанс FastStream)
    - `tasks/` (1 инстанс Taskiq worker + 1 scheduler)
    - `otel_collector/`
    - `tests/unit/`, `tests/integration/`, `tests/e2e/`
    - `infra/` (`prometheus/`, `grafana/`, `jaeger/`)
    - `alembic/`
    - `docs/` (`plan/`, `adr/`)
  - Настроить `.gitignore` и базовый `README.md`.
- **DoD:** `tree -L 2` показывает требуемую структуру папок, git репозиторий готов.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-0.2. pyproject.toml + зависимости
- **Что сделать:**
  - Настроить Poetry (или uv).
  - Зависимости:
    - `core`: pydantic, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, redis, faststream[kafka], grpcio, grpcio-tools, opentelemetry-*, structlog
    - `api`: fastapi, uvicorn, httpx, python-jose
    - `tasks`: taskiq, taskiq-kafka (или taskiq-aio-pika), taskiq-scheduler
    - `payment`: yookassa (в extras: `poetry install -E yookassa`)
    - `dev`: pytest, pytest-asyncio, testcontainers, ruff, mypy, pre-commit
- **DoD:** `poetry install` (или `uv sync`) проходит успешно.
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
