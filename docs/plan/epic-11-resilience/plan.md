# ЭПИК 11: Resilience

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Устойчивость системы при сбоях, таймаутах и перегрузках.
Retry с backoff/jitter, Circuit Breaker, deadline propagation, защита вокруг PaymentProvider, health/readiness probes и graceful shutdown.

---

## Задачи эпика

### [ ] T-11.1. Retry в gRPC-клиенте (FastAPI -> Core)
- **Что сделать:** Повторные попытки при транзиентных ошибках сети с backoff и jitter.
- **DoD:** При кратковременном сбое Core Service запрос от клиента успешно завершается.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-11.2. Circuit breaker
- **Что сделать:** Предохранитель (Circuit Breaker) на HTTP/gRPC клиентах: размыкание цепи при высоком проценте ошибок (мгновенный 503).
- **DoD:** Тест перехода состояний Closed -> Open -> Half-Open.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-11.3. Deadline propagation
- **Что сделать:** Сквозная передача таймаута: HTTP -> gRPC metadata -> SQL query cancel.
- **DoD:** Зависший запрос прерывается по DEADLINE_EXCEEDED без утечки ресурсов.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-11.4. Retry + circuit breaker вокруг PaymentProvider
- **Что сделать:** Защитная обёртка вокруг вызовов внешнего провайдера: retry, circuit breaker, strict timeout.
- **DoD:** Тест: падение провайдера размыкает circuit breaker и не блокирует сервис.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-11.5. Graceful shutdown
- **Что сделать:** Обработка SIGTERM/SIGINT: завершение активных транзакций, закрытие коннектов без потери данных.
- **DoD:** Остановка контейнера под нагрузкой не теряет запросы.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-11.6. Health и readiness
- **Что сделать:** /healthz (liveness) и /readyz (readiness с проверкой Postgres, Redis, Kafka).
- **DoD:** При отказе Postgres эндпоинт readiness отдаёт 503 Service Unavailable.
- **Подтверждение пользователя:** `[ ]`
