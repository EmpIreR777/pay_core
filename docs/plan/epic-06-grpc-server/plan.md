# ЭПИК 6: gRPC Server

> **Статус эпика:** `[ ] TODO`  
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Транспортный слой Core Service на gRPC (одиночный инстанс).
Unary и server-streaming методы, интерцепторы авторизации, логирования, маппинга ошибок и интеграция с OTel Collector.

---

## Задачи эпика

### [ ] T-6.1. .proto контракт
- **Что сделать:** Описать PaymentsService: CreatePayment, GetPayment, CancelPayment, WatchPayment (stream). Плюс метод HandleProviderCallback для вебхуков.
- **DoD:** protoc компилирует код без ошибок.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-6.2. Генерация кода через Makefile
- **Что сделать:** Таргет `make gen-proto` компилирует protobuf файлы в `core_service/interfaces/grpc/generated`.
- **DoD:** `make gen-proto` работает быстро и идемпотентно.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-6.3. PaymentsServicer
- **Что сделать:** Реализация gRPC-сервисера с делегированием в application use-cases.
- **DoD:** Unit-тест сервисера.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-6.4. Interceptors: auth, logging, error mapping
- **Что сделать:** Интерцепторы: проверка токена/метаданных, structured logging и конвертация DomainError в grpc.StatusCode.
- **DoD:** Тест возврата UNAUTHENTICATED и корректных статус-кодов.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-6.5. Server-streaming WatchPayment
- **Что сделать:** Потоковый метод gRPC, передающий клиенту обновления статуса.
- **DoD:** Тест стриминга событий платежа.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-6.6. Health-check + reflection
- **Что сделать:** Подключить standard grpc_health.v1 и reflection.
- **DoD:** `grpcurl -plaintext localhost:50051 list` успешно отображает сервисы.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-6.7. Bootstrap gRPC-сервера с OTel
- **Что сделать:** Инициализация одиночного инстанса Core Service, OTel gRPC instrumentation, экспорт трейсов на OTel Collector (:4317).
- **DoD:** Сервер стартует, трейсы RPC отображаются в Jaeger.
- **Подтверждение пользователя:** `[ ]`
