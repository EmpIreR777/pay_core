# ЭПИК 7: FastAPI Gateway (BFF)

> **Статус эпика:** `[ ] TODO`  
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Публичный HTTP API Gateway (тонкий BFF, одиночный инстанс).
Маппинг ошибок gRPC -> HTTP, Server-Sent Events (SSE), JWT-валидация и приём вебхуков от ЮKassa.

---

## Задачи эпика

### [ ] T-7.1. Pydantic schemas
- **Что сделать:** Схемы запросов и ответов: CreatePaymentRequest, PaymentResponse, CancelPaymentRequest, WebhookNotification.
- **DoD:** Swagger UI (/docs) открывается и валидирует схемы.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.2. gRPC client
- **Что сделать:** Async gRPC stub, управление каналом подключения, health-check к Core Service.
- **DoD:** Успешный RPC вызов из FastAPI в Core Service.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.3. Routers POST /payments, GET /payments/{id}, POST /payments/{id}/cancel
- **Что сделать:** Реализация эндпоинтов платежей с вызовом gRPC клиента.
- **DoD:** HTTP-тесты через httpx AsyncClient.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.4. Error mapping gRPC -> HTTP
- **Что сделать:** Преобразование grpc.StatusCode (NOT_FOUND -> 404, FAILED_PRECONDITION/ALREADY_EXISTS -> 409, UNAUTHENTICATED -> 401, DEADLINE_EXCEEDED -> 504).
- **DoD:** Тесты на каждый код ошибки.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.5. SSE endpoint GET /payments/{id}/watch
- **Что сделать:** Потоковый эндпоинт Server-Sent Events на базе WatchPayment gRPC stream.
- **DoD:** Тест стриминга через curl -N / AsyncClient.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.6. JWT-авторизация
- **Что сделать:** FastAPI middleware/dependency для проверки Bearer JWT токена.
- **DoD:** 401 Unauthorized без токена или с невалидным токеном.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.7. Bootstrap FastAPI с OTel
- **Что сделать:** Lifespan приложения, настройка OTel FastAPI instrumentation, экспорт трейсов на OTel Collector.
- **DoD:** При HTTP запросе создаются спаны и летят в Collector.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-7.8. Webhook endpoint POST /webhooks/yookassa
- **Что сделать:** Приём callback от ЮKassa, валидация подписи/ip, вызов HandleProviderWebhook, идемпотентность по event_id.
- **DoD:** Тест: два одинаковых webhook обрабатываются ровно один раз.
- **Подтверждение пользователя:** `[ ]`
