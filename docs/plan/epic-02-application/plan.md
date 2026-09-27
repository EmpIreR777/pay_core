# ЭПИК 2: Application Layer (Use-cases & Ports)

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Оркестрация бизнес-логики через чистые порты и юзкейсы.
Порт PaymentProvider для внешнего эквайринга, сагаподобный флоу платежа с вызовом провайдера вне БД-транзакции.

---

## Задачи эпика

### [x] T-2.1. Порты
- **Что сделать:** Создать Protocol интерфейсы: AccountRepository, PaymentRepository, UnitOfWork, LockManager, IdempotencyStore, EventPublisher, PaymentProvider, Clock.
- **DoD:** `mypy --strict` успешен.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `application/ports/` — контракты инфраструктуры как `typing.Protocol` + `@runtime_checkable`. Реализациям
    не нужно наследоваться от портов: достаточно совпасть по «форме» (structural typing), что и проверяют тесты.
  - `ports/clock.py` — `Clock.now()` (синхронный: чтение времени не требует `await`, в тестах время замораживается).
  - `ports/account_repository.py` — `AccountRepository`: `get`, `get_for_update` (`SELECT ... FOR UPDATE`,
    блокировка строки до конца транзакции), `add`, `update`. Коммит репозитории не делают — это граница `UnitOfWork`.
  - `ports/payment_repository.py` — `PaymentRepository`: `get`, `get_by_provider_payment_id` (для вебхуков),
    `add`, `update`, `find_by_status(status, *, limit, updated_before)` (выборка «зависших» платежей для сверки).
  - `ports/unit_of_work.py` — `UnitOfWork`: `accounts`/`payments` (property) + `__aenter__`/`__aexit__`/`commit`/`rollback`
    (`__aenter__ -> Self`). Транзакционная граница, чтобы баланс + платёж + outbox фиксировались атомарно.
  - `ports/lock_manager.py` — `LockManager` (`lock` → `AbstractAsyncContextManager[DistributedLock]`, `acquire`, `release`)
    и `DistributedLock` (`resource`, `token`, `is_held`, `release`, `__aenter__`/`__aexit__`). `lock` синхронный —
    он лишь возвращает контекстный менеджер, захват происходит в `__aenter__`.
  - `ports/idempotency_store.py` — `IdempotencyStore` (`get`, `save`, `try_acquire` = `SET NX`, `release`) и
    `IdempotencyRecord` (`frozen`-dataclass; `request_hash` ловит конфликт «тот же ключ, другое тело»).
  - `ports/event_publisher.py` — `EventPublisher.publish(DomainEvent)` — абстракция outbox.
  - `ports/payment_provider.py` — `PaymentProvider` (`create_payment(*, payment_id, amount, idempotency_key)`,
    `get_status`, `refund`), `ProviderResult`, `ProviderStatus` (отдельная шкала статусов провайдера).
  - `application/__init__.py`, `ports/__init__.py` — реэкспорт портов наружу слоя.
  - `tests/unit/application/test_ports.py` — 60 архитектурных (contract) тестов: `_is_protocol` / `_is_runtime_protocol`,
    наличие и `async`-ность методов (`inspect.iscoroutinefunction`), `property` (`inspect.getattr_static`),
    прохождение `isinstance(fake, Port)` для класса без наследования, негативная проверка, неизменяемость
    `ProviderResult`/`IdempotencyRecord` и валидация их значений.
- **Проверки:** `make -C backend lint` (ruff check + format check + mypy --strict) — зелёные; `make -C backend test` — 370 passed.
- **Нюансы:**
  - `PaymentProvider` объявлен уже в T-2.1 (он есть в списке портов задачи), поэтому его минимальный контракт и
    типы `ProviderResult`/`ProviderStatus` определены здесь — иначе пакет не импортируется и `mypy --strict` не проходит.
    Детальная финализация контракта — T-2.2, полный DTO-пакет (может реэкспортировать эти типы) — T-2.3.
  - В `create_payment` использован доменный `PaymentId` вместо `UUID` из формулировки T-2.2 — строже и консистентно с доменом.
  - `PaymentRepository.get_by_provider_payment_id` добавлен под обработку вебхуков (T-2.7).

### [ ] T-2.2. Порт PaymentProvider
- **Что сделать:** Интерфейс Protocol:
  - `create_payment(*, payment_id: UUID, amount: Money, idempotency_key: str) -> ProviderResult`
  - `get_status(provider_payment_id: str) -> ProviderStatus`
  - `refund(provider_payment_id: str, amount: Money) -> ProviderResult`
- **DoD:** Документированный контракт и строгие типы.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.3. DTO
- **Что сделать:** CreatePaymentInput/Output, GetPaymentInput, ProviderResult, ProviderStatus.
- **DoD:** Валидация и строгая типизация.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.4. UseCase CreatePayment
- **Что сделать:** Сагаподобный флоу:
  1. Idempotency check.
  2. Redis lock на from_account.
  3. UoW: списание с аккаунта + создание Payment(PENDING) + outbox PaymentCreated. Commit.
  4. Вне транзакции: вызов PaymentProvider.create_payment.
  5. Новый UoW: обновление статуса (PROCESSING / FAILED) + provider_payment_id + outbox. Commit.
  6. Сохранение Idempotency ответа.
- **DoD:** Тесты с FakePaymentProvider: успех, ошибка провайдера, дубликат ключа.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.5. UseCase GetPayment
- **Что сделать:** Получение платежа + актуализация через провайдера при зависании в PROCESSING.
- **DoD:** Тест сценария.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.6. UseCase CancelPayment
- **Что сделать:** Отмена для PENDING, возврат средств на баланс, генерация события отмены.
- **DoD:** Тесты на успех и отказ.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.7. UseCase HandleProviderWebhook
- **Что сделать:** Обработка webhook: поиск платежа, перевод в SETTLED/FAILED, запись outbox. Идемпотентность по provider_event_id.
- **DoD:** Тест: повторный webhook дает тот же результат без дублирования.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.8. UseCase WatchPayment
- **Что сделать:** Async generator потока статусов платежа.
- **DoD:** Unit-тест генератора.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.9. In-memory fakes
- **Что сделать:** Реализация фейков портов + FakePaymentProvider (настраиваемый: success/failure/delay).
- **DoD:** Запуск юзкейсов без внешней инфры.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.10. Покрытие application >= 85%
- **DoD:** `pytest --cov=core_service/application` показывает >= 85%.
- **Подтверждение пользователя:** `[ ]`
