# ==============================================================================
# Payment Gateway — инфраструктура.

# ==============================================================================

.PHONY: help up down restart logs ps health otel-smoke clean volumes-check \
        migrate migrate-down migrate-check

help: ## Показать список доступных команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Инфраструктура ---

up: ## Поднять инфраструктуру (postgres, redis, kafka, otel, jaeger, prometheus, grafana)
	docker compose up -d

migrate: ## Применить миграции БД (одноразовый контейнер migrations)
	docker compose run --rm migrations

migrate-down: ## Откатить все миграции БД (полная очистка схемы)
	$(MAKE) -C backend migrate-down-base

migrate-check: ## Проверить, что схема в БД совпадает с моделями
	$(MAKE) -C backend migrate-check

down: ## Остановить инфраструктуру (данные в volume'ах сохраняются)
	docker compose down

restart: ## Перезапустить инфраструктуру
	docker compose restart

logs: ## Следить за логами всех сервисов
	docker compose logs -f

ps: ## Показать статус сервисов инфраструктуры
	docker compose ps

health: ## Показать статус healthcheck'ов всех сервисов
	@docker compose ps --format '{{.Name}}\t{{.Status}}'

# --- Observability ---

otel-smoke: ## Отправить тестовые span/metric/log в Collector и проверить доставку
	$(MAKE) -C backend otel-smoke

# --- Обслуживание ---

clean: ## Удалить контейнеры СЕТИ и тома с данными (полный сброс)
	docker compose down -v --remove-orphans

volumes-check: ## Показать, где физически лежат тома с данными
	@docker volume ls --filter label=com.docker.compose.project=pay_core \
		--format '{{.Name}}	{{.Driver}}'
