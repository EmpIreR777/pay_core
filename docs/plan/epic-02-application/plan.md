# ЭПИК 2: Application Layer (Use-cases & Ports)

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Оркестрация бизнес-логики через чистые порты и юзкейсы.
Порт PaymentProvider для внешнего эквайринга, сагаподобный флоу платежа с вызовом провайдера вне БД-транзакции.

---

## Задачи эпика

### [ ] T-2.1. Порты
- **Что сделать:** Создать Protocol интерфейсы: AccountRepository, PaymentRepository, UnitOfWork, LockManager, IdempotencyStore, EventPublisher, PaymentProvider, Clock.
- **DoD:** `mypy --strict` успешен.
- **Подтверждение пользователя:** `[ ]`

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
