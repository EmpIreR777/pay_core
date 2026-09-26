# 🤖 AGENT.md — Кодекс и руководство AI-агента в Payment Gateway

> **Статус документа:** Обязательный регламент разработки.  
> **Правило применения:** Агент обязан руководствоваться данным документом при выполнении любых задач в проекте.

---

## 🎯 1. Архитектурный контекст системы

Мы разрабатываем высоконадёжный финансовый шлюз (**Payment Gateway**) на базе Clean Architecture, Domain-Driven Design (DDD) и асинхронного Python 3.12+.

### Архитектура одиночных инстансов (Single-Instance Only):
- **API Gateway (FastAPI)**: Тонкий BFF (Backend-for-Frontend) — **1 инстанс**.
- **Core Service (gRPC)**: Ядро финансовой бизнес-логики — **1 инстанс**.
- **Workers (FastStream)**: Асинхронные потребители Kafka — **1 инстанс**.
- **Tasks (Taskiq)**: 1 worker фоновых задач + 1 scheduler периодических задач.
- **Хранилища и брокеры**: Postgres 16 (1 инстанс), Redis 7 (1 инстанс), Kafka KRaft (1 брокер).
- **Observability**: OTel Collector (:4317/:4318) как единый приёмник -> экспортирует в Jaeger (:4317) и Prometheus (:8889).

---

## 🛑 2. Золотые правила процесса (Strict Rules)

1. **Никаких самовольных `[x]`**:
   - Агент **никогда не ставит** отметку `[x]` или статус `DONE` самостоятельно.
   - Пометка задачи или эпика выставляется **ТОЛЬКО ПОСЛЕ** явного текстового подтверждения пользователем («*Подтверждаю*», «*Принято*», «*Отмечай*»).
2. **Строго по одной задаче за сессию (T-X.Y)**:
   - Не забегать вперёд и не писать код будущих задач.
   - Следовать критериям приёмки (**DoD**) текущей задачи.
3. **Одиночные инстансы (без преждевременных репликаций)**:
   - Все конфиги, порты и docker-compose файлы настраиваются на одиночные инстансы сервисов.
4. **Валидация через Makefile перед сдачей задачи**:
   - Перед запросом подтверждения у пользователя код должен быть проверен через таргеты Makefile:
     - `make lint` (ruff check + format check + mypy --strict).
     - `make test` (pytest unit/integration).
   - Запрещено сдавать задачу с падающими линтерами или тестами.

---

## ⚡ 3. Best Practices FastAPI (BFF Layer)

1. **Асинхронный Lifespan**:
   - Использовать `@asynccontextmanager async def lifespan(app: FastAPI)` вместо устаревших `@app.on_event("startup")` и `shutdown`.
   - В lifespan инициализировать gRPC channel, клиенты внешних систем, OTel tracer и корректно освобождать их ресурсы при остановке.
2. **Тонкий BFF контроллер**:
   - Роутеры FastAPI **не содержат** бизнес-логики, прямых SQL-запросов или вычислений финансового баланса.
   - Обязанность роутера: принять HTTP-запрос, провалидировать входные DTO, вызвать метод gRPC-сервиса и вернуть HTTP-ответ.
3. **Pydantic V2 Conventions**:
   - Все схемы наследуются от `pydantic.BaseModel`.
   - Конфигурация через `model_config = ConfigDict(...)`.
   - Использовать строгие типы: `Annotated`, `Field(..., min_length=..., gt=0)`, `UUID`, `SecretStr`.
   - Использовать современные валидаторы `@field_validator` и `@model_validator(mode="after")`.
4. **Централизованная обработка ошибок (Exception Handlers)**:
   - Не оборачивать контроллеры в `try-except Exception`.
   - Использовать глобальные exception handlers для маппинга `grpc.RpcError` в стандартные HTTP статус-коды:
     - `StatusCode.NOT_FOUND` -> `404 Not Found`
     - `StatusCode.ALREADY_EXISTS` / `FAILED_PRECONDITION` -> `409 Conflict`
     - `StatusCode.UNAUTHENTICATED` -> `401 Unauthorized`
     - `StatusCode.PERMISSION_DENIED` -> `403 Forbidden`
     - `StatusCode.DEADLINE_EXCEEDED` -> `504 Gateway Timeout`
     - `StatusCode.UNAVAILABLE` -> `503 Service Unavailable`
5. **Dependency Injection**:
   - Использовать `fastapi.Depends()` для внедрения аутентификации пользователя (JWT Bearer), настроек `Settings` и синглтон-клиентов.
6. **Streaming & Server-Sent Events (SSE)**:
   - Использовать `StreamingResponse` с асинхронными генераторами `async def event_generator()` для стриминга статусов (`data: ...\n\n`).

---

## 🏛️ 4. Best Practices Clean Architecture & DDD (Core Service)

1. **Domain Layer — чистый Python**:
   - Расположен в `core_service/domain/`.
   - **Строжайший запрет на импорты внешних библиотек**: никакого SQLAlchemy, gRPC, FastAPI, Pydantic.
   - Использовать только стандартную библиотеку Python (`dataclasses`, `enum`, `typing`, `decimal.Decimal`, `uuid`, `datetime`).
   - Value Objects — иммутабельные (`@dataclass(frozen=True)`).
   - Entities — инкапсулируют бизнес-правила и проверяют инварианты при изменении состояния.
   - Исключения — наследуются от базового доменного `DomainError`.
2. **Application Layer (Use Cases & Ports)**:
   - Расположен в `core_service/application/`.
   - Порты объявляются через `typing.Protocol` (порты репозиториев, локов, UoW, провайдера).
   - **Финансовый паттерн саги**: внешние сетевые вызовы (`PaymentProvider.create_payment`) выполняются **СТРОГО ВНЕ** транзакции базы данных, чтобы удерживать строчные блокировки минимальное время.
3. **Infrastructure Layer (Адаптеры)**:
   - Реализации интерфейсов портов: асинхронный SQLAlchemy 2.0 (`AsyncSession`), Redis, Kafka, Taskiq.
   - Детали хранения и внешних API полностью изолированы от ядра.

---

## 🐍 5. Общесистемные стандарты Python, Tooling & Тестирование

- **Package Manager**: **`uv`** (быстрый, современный пакетный менеджер и виртуальное окружение: `uv sync`, `uv run`, `uv add`).
- **Makefile**: основной интерфейс разработчика:
  - `make lint` — запуск `uv run ruff check`, `uv run ruff format --check` и `uv run mypy --strict`.
  - `make test` — запуск `uv run pytest`.
  - `make test-cov` — запуск `uv run pytest --cov=... --cov-report=term-missing`.
  - `make format` — автоформатирование `uv run ruff format` и `uv run ruff check --fix`.
- **Python**: 3.12+ со строгой статической типизацией.
- **Typing**: `mypy --strict` — запрещены нетипизированные аргументы и неявные `Any`.
- **Formatting & Linting**: `ruff` с современным набором правил (E, F, W, I, B, UP, C4, SIM, TCH).
- **Логирование**: `structlog` в JSON с обязательной сквозной передачей `trace_id` и `span_id`.
- **Best Practices в Тестировании**:
  - **Пирамида тестов**:
    - **Unit-тесты (`tests/unit/`)**: проверяют чистую бизнес-логику домена и юзкейсы без I/O и внешних сервисов (с использованием in-memory fakes портов). Должны исполняться за миллисекунды.
    - **Интеграционные тесты (`tests/integration/`)**: проверяют реальные адаптеры (Postgres, Redis, Kafka) с использованием `testcontainers-python`.
    - **E2E тесты (`tests/e2e/`)**: сквозные сценарии полного цикла платежа.
  - **Изоляция и воспроизводимость**:
    - Никаких flaky-тестов со `sleep()` — использовать явное ожидание условий (`tenacity` / polling с таймаутом).
    - Каждому тесту — изолированная база данных или транзакционный rollback.
  - **Fakes вместо Mocks**: для чистой архитектуры предпочитать **in-memory fakes** (реализующие Protocol интерфейса) сложным `unittest.mock.MagicMock`, чтобы гарантировать соблюдение контрактов портов.
- **Observability**: телеметрия экспортируется **только в OTel Collector** (:4317/:4318), приложение не знает о Jaeger и Prometheus напрямую.

---

## 💬 6. Протокол работы над задачей

1. В начале работы прочитать описание текущей задачи в `docs/plan/epic-XX/plan.md`.
2. Реализовать код строго по DoD.
3. Запустить проверки через `make lint` и `make test`, показать консольный отчёт пользователю.
4. Запросить подтверждение: *«Задача T-X.Y выполнена и протестирована через make lint/test. Подтверждаете перевод в статус [x]?»*.

