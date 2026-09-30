# ==============================================================================
# Payment Gateway — интерфейс разработчика для бэкенда.
# ==============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help install lint format pre-commit pre-commit-install \
        test test-cov test-cov-domain test-cov-application test-unit test-integration \
        migrate migrate-down migrate-down-base migrate-current migrate-check migrate-sql \
        alembic-rev gen-proto run-api run-core run-workers run-tasks run-scheduler otel-smoke \
        clean-cache

# ------------------------------------------------------------------------------
# Help
# ------------------------------------------------------------------------------
help: ## Показать список доступных команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------------------
# Dependencies
# ------------------------------------------------------------------------------
install: ## Установить зависимости (uv sync --all-groups)
	uv sync --all-groups

# ------------------------------------------------------------------------------
# Code quality
# ------------------------------------------------------------------------------
lint: ## ruff check + ruff format --check + mypy --strict
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src scripts --strict --config-file pyproject.toml

format: ## Автоформатирование (ruff check --fix + ruff format)
	uv run ruff check --fix .
	uv run ruff format .

pre-commit: ## Прогнать все pre-commit хуки по всем файлам
	uv run pre-commit run --all-files

pre-commit-install: ## Установить pre-commit hook в .git/hooks
	uv run pre-commit install --install-hooks

# ------------------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------------------
test: ## Запустить все тесты (unit + integration)
	uv run pytest

test-unit: ## Запустить только быстрые unit-тесты (без стенда)
	uv run pytest tests/unit

test-integration: ## Запустить интеграционные тесты (Postgres: стенд или testcontainer; observability: make up)
	uv run pytest tests/integration -m integration

test-cov: ## Запустить тесты с отчётом покрытия
	uv run pytest --cov=src --cov-report=term-missing

test-cov-domain: ## Запустить тесты с проверкой покрытия доменного слоя (>= 90%)
	uv run pytest --cov=src/core_service/domain --cov-report=term-missing --cov-fail-under=90

test-cov-application: ## Запустить тесты с проверкой покрытия прикладного слоя (>= 85%, T-2.10)
	uv run pytest --cov=src/core_service/application --cov-report=term-missing --cov-fail-under=85


# ------------------------------------------------------------------------------
# Database
# ------------------------------------------------------------------------------
migrate: ## Применить миграции Alembic до head
	uv run alembic upgrade head

migrate-down: ## Откатить последнюю миграцию
	uv run alembic downgrade -1

migrate-down-base: ## Откатить все миграции (полная очистка схемы)
	uv run alembic downgrade base

migrate-current: ## Показать применённую ревизию и заголовки ревизий
	uv run alembic current
	uv run alembic heads

migrate-check: ## Проверить, что схема в БД совпадает с моделями
	uv run alembic check

migrate-sql: ## Показать SQL миграций без применения (--sql)
	uv run alembic upgrade head --sql

alembic-rev: ## Создать ревизию: make alembic-rev m='add users'
	./alembic_rev.sh "$(m)"

# ------------------------------------------------------------------------------
# gRPC contracts
# ------------------------------------------------------------------------------
gen-proto: ## Сгенерировать Python-контракты из proto/
	mkdir -p src/generated && uv run python -m grpc_tools.protoc \
		-I ../proto \
		--python_out=./src/generated \
		--pyi_out=./src/generated \
		--grpc_python_out=./src/generated \
		$$(find ../proto -name '*.proto')

# ------------------------------------------------------------------------------
# Run services (single instance)
# ------------------------------------------------------------------------------
run-api: ## FastAPI BFF (HTTP)
	uv run uvicorn src.app:app --host 127.0.0.1 --port $${HTTP_PORT:-8000} --reload

run-core: ## Core gRPC Service
	uv run python -m src.core_service.main

run-workers: ## FastStream Kafka consumers
	uv run faststream run src.workers.main:app

run-tasks: ## Taskiq worker
	uv run taskiq worker src.tasks.broker:broker src.tasks.tasks

run-scheduler: ## Taskiq scheduler
	uv run taskiq scheduler src.tasks.scheduler:scheduler src.tasks.tasks

# ------------------------------------------------------------------------------
# Observability
# ------------------------------------------------------------------------------

otel-smoke: ## Отправить тестовые span/metric/log в OTel Collector (T-0.8)
	uv run python -m scripts.otel_smoke

# ------------------------------------------------------------------------------
# Maintenance
# ------------------------------------------------------------------------------
clean-cache: ## Удалить кэши (__pycache__, mypy, pytest, ruff)
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
