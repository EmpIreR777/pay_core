# ЭПИК 1: Domain Layer

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Чистый Python (никаких sqlalchemy, grpc, fastapi).
DDD: Value Objects, Entities, Domain Events, Domain Exceptions, инварианты финансовой логики.

---

## Задачи эпика

### [x] T-1.1. Value Objects: Money, Currency, AccountId, PaymentId
- **Что сделать:** Иммутабельные dataclass (frozen=True). Money с валидацией валют, округлением и запретом сложения разных валют.
- **DoD:** Unit-тесты на арифметику, равенство и ошибки.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `backend/src/core_service/domain/exceptions.py` — базовый `DomainError` (AGENT.md §4.1) и
    VO-ошибки: `InvalidValueError`, `InvalidCurrencyError`, `CurrencyMismatchError`,
    `InvalidAmountError`, `NegativeAmountError`, `InvalidIdentifierError`. Бизнес-исключения
    (`InsufficientFunds`, `AccountBlocked` и др.) намеренно не добавлены — это T-1.5.
  - `value_objects/currency.py` — `Currency` на `StrEnum`, а не на dataclass: валюты образуют
    закрытое множество, поэтому Enum даёт валидацию кода, синглтонность members
    (`Currency(' rub ') is Currency.RUB`) и совместимость с pydantic v2 / SQLAlchemy бесплатно.
    Именно `StrEnum`, а не `Enum`: у обычного `Enum` `str()` даёт `'Currency.RUB'`, что портит
    сообщения об ошибках. Свойства `minor_unit`/`quantum` учитывают разную точность валют
    (JPY — 0 знаков, KWD — 3, RUB — 2). `_missing_` нормализует код и бросает
    `InvalidCurrencyError`, не выпуская наружу «голый» `ValueError` из stdlib.
  - `value_objects/money.py` — `Money` с инвариантами: только `Decimal` (float запрещён явно,
    bool отвергается до int, т.к. bool — подкласс int), неотрицательность, отказ от NaN/Infinity
    и абсурдной величины, квантование до точности валюты через `object.__setattr__`
    (frozen-dataclass иначе не даёт нормализовать значение при создании). Арифметика `+ - *`
    и порядковые сравнения бросают `CurrencyMismatchError`; `==` генерирует dataclass, поэтому
    разные валюты просто не равны. `NotImplemented` вместо исключения при работе с не-`Money` —
    тогда Python сам выдаёт корректный `TypeError`. Правило округления `ROUND_HALF_UP`
    зафиксировано тестом.
  - `value_objects/identifiers.py` — `AccountId` / `PaymentId` поверх общего `Identifier`.
    Enum здесь невозможен: пространство UUID бесконечно. Наследник помечен `dataclass`, чтобы
    `__eq__` генерировался от лица своего класса и `AccountId(x) != PaymentId(x)`.
    Отклоняется nil-UUID; строка с границы слоя приводится к UUID.
- **Проверки:** `make lint` (ruff + format + mypy --strict) — зелёные; `make test` — 120 passed;
  покрытие `src/core_service/domain` — 100 % (178/178 stmts); `make pre-commit` — все хуки зелёные.
- **Известный нюанс:** `Currency.RUB == 'RUB'` истинно (следствие `StrEnum`) — удобно для
  транспортного слоя, но в доменном коде на такое сравнение полагаться не следует; поведение
  зафиксировано отдельным тестом.

### [ ] T-1.2. Entity Account
- **Что сделать:** Поля: id, balance: Money, version: int, is_blocked: bool. Методы: withdraw, deposit, block, unblock. Защита инвариантов.
- **DoD:** Тесты на все методы и ошибочные сценарии (блокировка, нехватка средств).
- **Подтверждение пользователя:** `[ ]`

### [ ] T-1.3. Entity Payment + статус-машина
- **Что сделать:** Статусы: PENDING -> PROCESSING -> SETTLED / FAILED / CANCELLED. Метод `transition_to` строго валидирует переходы.
- **DoD:** Тесты на все валидные и недопустимые переходы.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-1.4. Domain Events
- **Что сделать:** PaymentCreated, PaymentSettled, PaymentFailed, PaymentRefunded. Уникальные event_id (UUID4), timestamp occurred_at (UTC).
- **DoD:** Тесты на генерацию событий и их неизменяемость.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-1.5. Domain Exceptions
- **Что сделать:** DomainError -> InsufficientFunds, AccountBlocked, InvalidTransition, DuplicateOperation, PaymentProviderError.
- **DoD:** Unit-тесты на выброс и перехват исключений.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-1.6. Покрытие domain >= 90%
- **Что сделать:** Запустить `pytest --cov=core_service/domain`.
- **DoD:** Покрытие кода domain >= 90%.
- **Подтверждение пользователя:** `[ ]`
