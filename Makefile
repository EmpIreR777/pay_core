# ==============================================================================
# Payment Gateway — инфраструктура и качество кода.
#
# Линтер/тесты бэкенда живут в backend/Makefile (T-0.5), здесь только compose.
# docker-compose.yml лежит в корне, поэтому Docker Compose сам находит файл
# и сам подхватывает `.env` из корня репозитория — `-f` не требуется.
# ==============================================================================

COMPOSE := docker compose

.PHONY: help up down restart logs ps health clean volumes-check

help: ## Показать список доступных команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Инфраструктура (Эпик 0, T-0.6) ---

up: ## Поднять инфраструктуру (postgres, redis, kafka, otel, jaeger, prometheus, grafana)
	$(COMPOSE) up -d

down: ## Остановить инфраструктуру (данные в volume'ах сохраняются)
	$(COMPOSE) down

restart: ## Перезапустить инфраструктуру
	$(COMPOSE) restart

logs: ## Следить за логами всех сервисов
	$(COMPOSE) logs -f

ps: ## Показать статус сервисов инфраструктуры
	$(COMPOSE) ps

health: ## Показать статус healthcheck'ов всех сервисов
	@$(COMPOSE) ps --format '{{.Name}}\t{{.Status}}'

# --- Обслуживание ---

clean: ## Удалить контейнеры СЕТИ и тома с данными (полный сброс)
	$(COMPOSE) down -v --remove-orphans

volumes-check: ## Показать, где физически лежат тома с данными
	@docker volume ls --filter label=com.docker.compose.project=pay_core \
		--format '{{.Name}}	{{.Driver}}'
