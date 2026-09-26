# ЭПИК 1: Domain Layer

> **Статус эпика:** `[ ] TODO`  
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Чистый Python (никаких sqlalchemy, grpc, fastapi).
DDD: Value Objects, Entities, Domain Events, Domain Exceptions, инварианты финансовой логики.

---

## Задачи эпика

### [ ] T-1.1. Value Objects: Money, Currency, AccountId, PaymentId
- **Что сделать:** Иммутабельные dataclass (frozen=True). Money с валидацией валют, округлением и запретом сложения разных валют.
- **DoD:** Unit-тесты на арифметику, равенство и ошибки.
- **Подтверждение пользователя:** `[ ]`

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
