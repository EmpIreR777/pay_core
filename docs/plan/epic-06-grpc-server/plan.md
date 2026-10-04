# ЭПИК 6: gRPC Server

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Транспортный слой Core Service на gRPC (одиночный инстанс).
Unary и server-streaming методы, интерцепторы авторизации, логирования, маппинга ошибок и интеграция с OTel Collector.

---

## Задачи эпика

### [x] T-6.1. .proto контракт
- **Что сделать:** Описать PaymentsService: CreatePayment, GetPayment, CancelPayment, WatchPayment (stream). Плюс метод HandleProviderCallback для вебхуков.
- **DoD:** protoc компилирует код без ошибок.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - Создан `proto/payments/v1/payments.proto`. Версия вынесена в имя пакета
    (`payments.v1`) — правило эволюции контрактов: несовместимое изменение
    оформляется новым пакетом `payments.v2`, а `v1` продолжает обслуживать уже
    написанных клиентов. Сервис `PaymentsService` содержит пять методов:
    `CreatePayment`, `GetPayment`, `CancelPayment` (unary), `WatchPayment`
    (**server-streaming**) и `HandleProviderCallback` (unary, вебхук провайдера).
  - Сообщения повторяют DTO прикладного слоя (`application/dto/payment.py`) один в
    один, чтобы сервисер T-6.3 был тонким делегирующим слоем, а не вторым местом
    с логикой: `Payment` ← `GetPaymentOutput` (переиспользуется и в
    `GetPaymentResponse`, и в кадрах `WatchPaymentResponse`), `CreatePaymentResponse`
    ← `CreatePaymentOutput`, `CancelPaymentResponse` ← `CancelPaymentOutput`,
    `HandleProviderCallbackRequest/Response` ← `HandleProviderWebhookInput/Output`.
  - Соглашения контракта:
    - деньги — сообщение `Money`: `amount` **десятичной строкой** (не `double` —
      float теряет точность, а доменный `Money` его отвергает), `currency` — код
      ISO 4217;
    - идентификаторы — строки UUID (разбор в доменный тип — работа адаптера);
    - время — `google.protobuf.Timestamp` (UTC), как требует домен;
    - у `PaymentStatus`/`ProviderStatus` есть нулевой `*_UNSPECIFIED`, а значения
      префиксованы (`PAYMENT_STATUS_*`, `PROVIDER_STATUS_*`): в proto3 значения
      enum'ов делят область видимости пакета, и без префиксов две шкалы со
      статусами `PENDING`/`PROCESSING` не скомпилировались бы;
    - необязательные поля помечены `optional` (explicit presence).
  - Каталог `proto/payments/v1/` больше не пуст, поэтому placeholder `.gitkeep`
    удалён.
- **Проверки:**
  - `protoc` (`python -m grpc_tools.protoc`, тем же инструментом, что и `make
    gen-proto`) компилирует файл без ошибок: получены `payments_pb2.py`,
    `payments_pb2.pyi`, `payments_pb2_grpc.py`; в стубе объявлен `PaymentsService`.
  - `make -C backend lint` — зелёный (`All checks passed!`, `68 files already
    formatted`, mypy: `no issues found in 66 source files`).
  - `make -C backend test-unit` — **812 passed**. Полный `make test` падает только
    на `integration`-тестах по внешним причинам (несовместимость API установленного
    `testcontainers` для `RedisContainer`; на `:5432` поднят посторонний контейнер
    вместо стенда проекта), к `.proto` отношения не имеет.

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
