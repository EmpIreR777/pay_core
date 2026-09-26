# ==============================================================================
# Payment Gateway — интерфейс разработчика для бэкенда.
# ==============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help install lint format pre-commit pre-commit-install \
        test test-cov \
        migrate alembic-rev alembic-down \
        gen-proto run-api run-core run-workers run-tasks run-scheduler \
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
	uv run mypy src --strict --config-file pyproject.toml

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
test: ## Запустить тесты (pytest)
	uv run pytest

test-cov: ## Запустить тесты с отчётом покрытия
	uv run pytest --cov=src --cov-report=term-missing

# ------------------------------------------------------------------------------
# Database
# ------------------------------------------------------------------------------
migrate: ## Применить миграции Alembic (upgrade head)
	uv run alembic upgrade head

alembic-down: ## Откатить последнюю миграцию
	uv run alembic downgrade -1

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
# Maintenance
# ------------------------------------------------------------------------------
clean-cache: ## Удалить кэши (__pycache__, mypy, pytest, ruff)
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
