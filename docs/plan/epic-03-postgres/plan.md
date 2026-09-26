# ЭПИК 3: Postgres Infrastructure

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Хранилище на async SQLAlchemy 2.0 + Alembic, оптимистичные блокировки по version, пессимистичные SELECT FOR UPDATE, outbox и testcontainers.
Архитектура: одиночный инстанс Postgres 16.

---

## Задачи эпика

### [ ] T-3.1. SQLAlchemy модели
- **Что сделать:** Модели accounts, payments (provider_payment_id, provider_status), outbox, idempotency_keys, processed_events, provider_webhook_events.
- **DoD:** Схема описана, constraints и индексы на месте.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.2. Alembic setup + первая миграция
- **Что сделать:** Настроить асинхронный env.py alembic, сгенерировать начальную миграцию.
- **DoD:** `alembic upgrade head` и `alembic downgrade base` работают корректно.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.3. AccountRepository на Postgres
- **Что сделать:** Реализация AccountRepository: get_for_update (SELECT ... FOR UPDATE), add, update.
- **DoD:** Интеграционный тест репозитория.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.4. PaymentRepository на Postgres
- **Что сделать:** Сохранение, поиск по id, фильтрация по статусу.
- **DoD:** Интеграционные тесты.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.5. UnitOfWork
- **Что сделать:** Асинхронный контекстный менеджер UoW: commit, rollback при исключениях.
- **DoD:** Тест атомарности транзакции.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.6. Оптимистичный лок через version
- **Что сделать:** UPDATE accounts SET balance=..., version=version+1 WHERE id=... AND version=current_version. Если 0 строк - OptimisticLockError.
- **DoD:** Тест на ошибку при гонке версий.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.7. Testcontainers
- **Что сделать:** Pytest фикстура с Postgres testcontainer.
- **DoD:** `pytest tests/integration/` работает автономно.
- **Подтверждение пользователя:** `[ ]`
