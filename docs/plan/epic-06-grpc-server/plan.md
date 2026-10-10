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

### [x] T-6.2. Генерация кода через Makefile
- **Что сделать:** Таргет `make gen-proto` компилирует protobuf файлы в `core_service/interfaces/grpc/generated`.
- **DoD:** `make gen-proto` работает быстро и идемпотентно.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - Таргет `backend/Makefile:gen-proto` теперь одна строка — `uv run python -m
    scripts.gen_proto`. Вся механика живёт в `scripts/gen_proto.py`, который стал
    **единственным владельцем путей** (`PROTO_ROOT`, `GENERATED_ROOT`,
    `PROJECT_PACKAGE`): Makefile, тесты и будущий Dockerfile их не дублируют
    (AGENT.md §4.4). Прежний вариант писал в `src/generated` — пакетом Core
    Service не был и плану эпика не соответствовал.
  - **Порядок шагов генерации:** собрать `.proto` (сортировка → воспроизводимость)
    → `rm -rf` каталога генерации → один запуск `protoc` на все контракты
    (`--python_out`, `--pyi_out`, `--grpc_python_out`) → починка импортов →
    `__init__.py` по каталогам.
  - **Починка импортов (самое неочевидное место).** `protoc` умеет писать в
    поддерево каталога, но не умеет задать *Python-путь* этого поддерева, поэтому
    grpc-стаб приходит с импортом `from payments.v1 import payments_pb2`, который
    находит только `proto/`, лежащий в `sys.path`. Штатного флага нет: проверено,
    что `--grpc_python_opt` отсутствует, а `M`-маппинг в `--grpc_python_out` даёт
    `Invalid grpc version`. Поэтому строки, чей верхний пакет совпадает с каталогом
    в `proto/`, получают префикс `src.core_service.interfaces.grpc.generated.`;
    сторонние импорты (`grpc`, `google.protobuf`) не трогаются. Число исправлений
    печатается в лог — «0 строк» читается как регрессия формата вывода, а не как
    успех.
  - **Идемпотентность** достигается полной очисткой, а не перезаписью по именам:
    `protoc` перезаписывает только файлы тех `.proto`, что видел, и после удаления
    контракта его старый `*_pb2.py` жил бы дальше. Чистка до компиляции даёт ещё
    и то, что при падении `protoc` не остаётся частично освежённого дерева.
  - **`__init__.py` в каждом каталоге** — иначе каталоги стали бы namespace-пакетами:
    импорт прошёл бы, но mypy перестал бы видеть типы сообщений.
  - **Исключения из линтеров:** `ruff` и `mypy` больше не заходят в каталог
    машинного вывода (правки туда стёрлись бы при следующей генерации). В mypy
    добавлен и комментарий про точечный override на будущее — он понадобится в
    T-6.3, когда сервисер импортирует grpc-стаб (у него нет `.pyi`).
  - **Архитектурные стражи не обходят каталог генерации** (`test_architecture.py`,
    `GENERATED_ROOTS` + `_is_generated`). Без этого `make test` падал бы на коде
    protobuf: проверка дублей увидела бы в нём «свои» одинаковые тела функций и
    тексты. Исключение точечное, обходит только `generated/`.
  - **`.gitignore`:** `backend/src/core_service/interfaces/grpc/generated/`.
    Закоммиченный сгенерированный код стал бы вторым, устаревшим источником правды
    о контракте, который разошёлся бы с `proto/`.
  - **Пакет-скелет:** `src/core_service/interfaces/__init__.py` и
    `.../grpc/__init__.py` — новый транспортный слой Core Service (домен и
    application о протоколе не знают; зависимость только `interfaces -> application`).
- **Проверки:**
  - `make gen-proto` — **0.15 с**; два прогона подряд дают одинаковые **sha256 всех
    шести файлов** (побайтовая идемпотентность).
  - `tests/unit/test_gen_proto.py` — **8 passed**: контракт собран (5 методов,
    `WatchPayment` — server-streaming), импорт через `src.core_service...`,
    топ-левел `payments` недоступен, `git ls-files` пуст и `check-ignore` срабатывает,
    два прогона байт-в-байт, осиротевший файл удалён, `__init__.py` у всех
    каталогов, бюджет времени.
  - Сценарий чистого клона: `rm -rf generated && pytest` → 8 passed (тесты
    генерируют код сами, сгенерированного в git нет).
  - `make -C backend lint` — зелёный (`All checks passed!`, 71 файл отформатирован,
    mypy: 69 source files без ошибок); `make -C backend test-unit` — **820 passed**
    (было 812).
- **Отложено (см. `docs/plan/deferred.md`, D-3):** замена построчной починки
  импортов на M-маппинг или `buf` — после апгрейда `grpcio-tools`. Текущее решение
  рабочее и защищено тестом; отклонённые идейные альтернативы (diff в CI — неприменим,
  т.к. код не коммитится; проверка наличия `grpcio-tools` — `uv run` синхронизирует
  окружение сам; поиск `pyproject.toml` вместо `parents[1]` — противоречит
  конвенции проекта и не добавляет устойчивости, т.к. ошибка и так падает сразу).

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
