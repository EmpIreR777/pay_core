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

### [x] T-2.2. Порт PaymentProvider
- **Что сделать:** Интерфейс Protocol:
  - `create_payment(*, payment_id: UUID, amount: Money, idempotency_key: str) -> ProviderResult`
  - `get_status(provider_payment_id: str) -> ProviderStatus`
  - `refund(provider_payment_id: str, amount: Money) -> ProviderResult`
- **DoD:** Документированный контракт и строгие типы.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `ports/payment_provider.py` — контракт внешнего эквайринга финализирован (в T-2.1 типы
    `ProviderResult`/`ProviderStatus` и методы были объявлены минимально, потому что пакет портов без них
    не собирался). Три операции зафиксированы ровно в таком составе: `create_payment` (keyword-only,
    `PaymentId` + `Money` + `idempotency_key: str` → `ProviderResult`), `get_status(str) -> ProviderStatus`,
    `refund(str, Money) -> ProviderResult`.
  - Принят **контракт ошибок** — главное различие, которого не хватало: технический отказ (сеть, таймаут,
    5xx, неразбираемый ответ) реализация поднимает доменным `PaymentProviderError`, а бизнес-отказ
    (провайдер отклонил операцию) возвращается как `ProviderResult(status=FAILED)` и **не** является
    исключением. Исключение = «исход неизвестен», `FAILED` = «знаем: отклонено».
  - Зафиксирован **контракт идемпотентности**: `idempotency_key` стабилен между повторами и строится
    адаптером из `PaymentId`; `get_status` идемпотентен по природе; `refund` не допускает повторного
    возврата уже возвращённой суммы.
  - Задокументирована **карта шкал статусов**: адаптер приводит «сырые» значения шлюза к закрытому
    `ProviderStatus`, а прикладной слой переводит `ProviderStatus` → `PaymentStatus`
    (`PENDING→PENDING`, `PROCESSING→PROCESSING`, `SUCCEEDED→SETTLED`, `FAILED→FAILED`,
    `REFUNDED→SETTLED` + событие `PaymentRefunded`). Терминальные у провайдера: `SUCCEEDED`, `FAILED`,
    `REFUNDED` — после них опрос статуса не нужен.
  - В docstring модуля описано **место вызова в саге**: `create_payment` вызывается строго вне
    транзакции БД (между коммитами UoW#1 и UoW#2), чтобы не держать блокировки строк на время ответа
    внешней системы (AGENT.md §4.2). Дедлайны/ретраи/circuit breaker навешиваются снаружи (ЭПИК 11).
  - `tests/unit/application/test_payment_provider_port.py` — 19 contract-тестов: точные сигнатуры через
    `inspect.signature` (`KEYWORD_ONLY` у `create_payment`, аннотации тождественны `PaymentId`/`Money`/`str`,
    `return_annotation` тождествен `ProviderResult`/`ProviderStatus`), закрытость состава операций,
    документированность (наличие `:param`/`:returns:`/`:raises PaymentProviderError:` у методов и текста
    контракта ошибок в docstring порта), модель ошибок (`PaymentProviderError ⊂ DomainError`), закрытость
    шкалы `ProviderStatus`, строгость `ProviderResult` (`slots` + `frozen`, обязательность обоих полей,
    отказ на `''`/`'   '`/`None`/`123`) и структурное соответствие фейка порту без наследования.
- **Проверки:** `make -C backend lint` (ruff check + format check + mypy --strict) — зелёные
  (`36 source files`, `37 files already formatted`); `make -C backend test` — 389 passed.
- **Нюансы:**
  - В `create_payment` сохранён доменный `PaymentId` вместо `UUID` из формулировки задачи: решение T-2.1
    (консистентность с доменом и запрет на протаскивание сырых UUID) подтверждено тестом на аннотацию.
  - `ProviderResult`/`ProviderStatus` остались в модуле порта, как их ввёл T-2.1; DTO-пакет (T-2.3) может
    их реэкспортировать, не меняя контракт. Нового поведения в типах T-2.2 не добавляет (задел под T-2.3).
  - Тест «ровно три операции» сознательно ломается при добавлении операции в порт: это защита от
    случайного расширения контракта, правка теста — часть легального изменения порта.

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
