# ==============================================================================
# Payment Gateway — инфраструктура.
#
# Линтер/тесты бэкенда живут в backend/Makefile, здесь только compose.
# docker-compose.yml лежит в корне, поэтому Docker Compose сам находит файл
# и сам подхватывает `.env` из корня репозитория — `-f` не требуется.
# ==============================================================================

.PHONY: help up down restart logs ps health otel-smoke clean volumes-check

help: ## Показать список доступных команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Инфраструктура ---

up: ## Поднять инфраструктуру (postgres, redis, kafka, otel, jaeger, prometheus, grafana)
	docker compose up -d

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
