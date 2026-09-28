"""Тесты сценария обработки вебхука провайдера (T-2.7).

DoD задачи — «повторный webhook даёт тот же результат без дублирования».
Именно вокруг повторов и построены тесты: уведомление от шлюза приходит
повторно (провайдер ретраит, пока не получит 200), и вторая доставка не имеет
права ни второй раз перевести платёж, ни второй раз вернуть деньги, ни второй
раз написать событие в outbox.

Группы тестов отвечают на разные вопросы:

* **Успешное уведомление** — платёж переходит в ``SETTLED``, событие в outbox;
* **Отказ провайдера** — платёж ``FAILED``, холд возвращается, событие в outbox;
* **Идемпотентность (DoD)** — повтор и параллельная доставка;
* **Границы** — платёж не найден, чужое содержимое под тем же событием,
  промежуточный и запоздалый статусы;
* **Вход сценария** — валидация DTO уведомления.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.core_service.application import HandleProviderWebhookUseCase
from src.core_service.application.dto import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    CreatePaymentInput,
    HandleProviderWebhookInput,
)
from src.core_service.application.ports.idempotency_store import IdempotencyRecord
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.application.use_cases.handle_provider_webhook import (
    WEBHOOK_RECORD_TTL_SECONDS,
    _event_fingerprint,
)
from src.core_service.application.use_cases.payment_sync import PROVIDER_REJECTION_REASON
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import (
    DuplicateOperation,
    EntityNotFoundError,
    InvalidValueError,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from tests.unit.application.fakes import FROZEN_NOW, SagaEnvironment

BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')
EVENT_ID = 'yookassa-event-1'

#: Статусы, которые на шкале провайдера означают «операция завершена» (T-2.2).
TERMINAL_ACROSS_SCALES = pytest.mark.parametrize(
    ('provider_status', 'expected'),
    [
        pytest.param(ProviderStatus.SUCCEEDED, PaymentStatus.SETTLED, id='SUCCEEDED->SETTLED'),
        pytest.param(ProviderStatus.FAILED, PaymentStatus.FAILED, id='FAILED->FAILED'),
        pytest.param(ProviderStatus.REFUNDED, PaymentStatus.SETTLED, id='REFUNDED->SETTLED'),
    ],
)


def _money(value: Decimal) -> Money:
    return Money.from_number(value, Currency.RUB)


@pytest.fixture
def account() -> Account:
    return Account(account_id=AccountId.new(), balance=_money(BALANCE))


async def _processing_payment(
    environment: SagaEnvironment,
    account: Account,
    *,
    idempotency_key: str = 'setup-key',
) -> Payment:
    """Готовит окружение с платежом в PROCESSING — типичный адресат уведомления.

    Платёж и его идентификатор операции у провайдера создаёт настоящая сага
    T-2.4, а не собранная вручную фикстура: вебхук ищет платёж именно по
    ``provider_payment_id``, и подложить его «рядом» означало бы тестировать не
    тот путь данных, который работает в бою.
    """
    environment.add_account(account)
    await environment.build_use_case().execute(
        CreatePaymentInput(
            from_account_id=account.id,
            amount=_money(AMOUNT),
            idempotency_key=idempotency_key,
        )
    )
    return environment.only_payment()


def _notification(
    payment: Payment,
    status: ProviderStatus,
    *,
    event_id: str = EVENT_ID,
) -> HandleProviderWebhookInput:
    """Собирает уведомление о платеже так, как его пришлёт провайдер."""
    assert payment.provider_payment_id is not None, 'платёж вне PROCESSING не имеет операции у провайдера'
    return HandleProviderWebhookInput(
        provider_event_id=event_id,
        provider_payment_id=payment.provider_payment_id,
        provider_status=status,
    )


# --- Применение уведомления ---------------------------------------------------


@TERMINAL_ACROSS_SCALES
async def test_terminal_notification_is_applied_to_payment(
    account: Account,
    provider_status: ProviderStatus,
    expected: PaymentStatus,
) -> None:
    """Уведомление о завершении операции доводит платёж до статуса по шкале T-2.2."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    result = await environment.build_webhook_use_case().execute(_notification(payment, provider_status))

    assert result.payment_id == payment.id
    assert result.amount == _money(AMOUNT)
    assert result.status is expected
    assert environment.only_payment().status is expected


async def test_success_notification_publishes_settled_event(account: Account) -> None:
    """Успех попадает в outbox: событие о проведённом платеже уходит потребителю."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.SUCCEEDED))

    assert environment.event_publisher.types_published() == ['PaymentCreated', 'PaymentSettled']


async def test_failed_notification_marks_payment_failed(account: Account) -> None:
    """Отказ провайдера — известный исход: платёж переходит в FAILED с причиной."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.FAILED))

    stored = environment.only_payment()
    assert stored.status is PaymentStatus.FAILED
    assert stored.failure_reason == PROVIDER_REJECTION_REASON


async def test_failed_notification_returns_hold(account: Account) -> None:
    """Отказ, о котором мы узнали вебхуком, возвращает клиенту деньги.

    Если бы возврат жил копией в вебхуке, а не в общем правиле, он бы здесь
    легко забылся — и клиент потерял бы сумму за отклонённый платёж.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)

    await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.FAILED))

    assert environment.account_balance(account.id) == _money(BALANCE)


async def test_failed_notification_publishes_failure_and_refund(account: Account) -> None:
    """Возврат виден в outbox: и отказ, и факт возврата денег."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.FAILED))

    assert environment.event_publisher.types_published() == [
        'PaymentCreated',
        'PaymentFailed',
        'PaymentRefunded',
    ]


async def test_success_notification_keeps_hold(account: Account) -> None:
    """Проведённый платёж не возвращает денег: холд становится платежом."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.SUCCEEDED))

    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)


async def test_notification_is_applied_in_single_transaction(account: Account) -> None:
    """Статус, деньги и outbox фиксируются одним коммитом, а не тремя.

    Иначе возможен «призрак»: отказ без возврата денег или событие о платеже,
    которого в базе нет.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    commits_after_saga = environment.uow_factory.commits

    await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.FAILED))

    assert environment.uow_factory.commits == commits_after_saga + 1


# --- Идемпотентность по provider_event_id (DoD) -------------------------------


async def test_repeated_notification_returns_same_result(account: Account) -> None:
    """Повторная доставка события возвращает тот же ответ (DoD)."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()
    notification = _notification(payment, ProviderStatus.SUCCEEDED)

    first = await use_case.execute(notification)
    second = await use_case.execute(notification)

    assert second == first
    assert environment.only_payment().status is PaymentStatus.SETTLED


async def test_repeated_notification_publishes_no_second_event(account: Account) -> None:
    """Вторая доставка не пишет в outbox ещё раз (DoD: «без дублирования»).

    Дублирование события — это не косметика: потребитель обработал бы одну и ту
    же смену статуса дважды (двойное уведомление клиенту, двойная запись в
    реестр).
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()
    notification = _notification(payment, ProviderStatus.SUCCEEDED)

    await use_case.execute(notification)
    published_after_first = environment.event_publisher.types_published()

    await use_case.execute(notification)

    assert environment.event_publisher.types_published() == published_after_first


async def test_repeated_notification_does_not_touch_storage(account: Account) -> None:
    """Повтор не открывает транзакцию вовсе: ответ берётся из сохранённого.

    Это сильнее, чем «не коммитит»: между первой и второй доставкой платёж мог
    измениться (сверка, другой вебхук), и повтор обязан вернуть записанный ответ,
    а не пересчитанное состояние.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()
    notification = _notification(payment, ProviderStatus.SUCCEEDED)
    await use_case.execute(notification)
    units_after_first = len(environment.uow_factory.units)

    await use_case.execute(notification)

    assert environment.uow_factory.commits == units_after_first
    assert len(environment.uow_factory.units) == units_after_first


async def test_repeated_rejection_returns_hold_once(account: Account) -> None:
    """Повтор уведомления об отказе не возвращает деньги второй раз."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()
    notification = _notification(payment, ProviderStatus.FAILED)

    await use_case.execute(notification)
    balance_after_first = environment.account_balance(account.id)

    await use_case.execute(notification)

    assert environment.account_balance(account.id) == balance_after_first
    assert balance_after_first == _money(BALANCE)


async def test_parallel_delivery_of_same_event_is_rejected(account: Account) -> None:
    """Пока событие в работе, параллельная доставка его не дублирует.

    Провайдер повторил уведомление, не дождавшись ответа: пропустить второй
    вызов в обработку нельзя — он применил бы переход второй раз.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    await environment.idempotency_store.try_acquire(EVENT_ID, ttl_seconds=300)

    with pytest.raises(DuplicateOperation):
        await environment.build_webhook_use_case().execute(_notification(payment, ProviderStatus.SUCCEEDED))

    assert environment.only_payment().status is PaymentStatus.PROCESSING
    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)


async def test_conflicting_content_for_same_event_is_rejected(account: Account) -> None:
    """Тот же ``provider_event_id`` с другим статусом — конфликт, а не повтор."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()

    await use_case.execute(_notification(payment, ProviderStatus.SUCCEEDED))

    with pytest.raises(DuplicateOperation):
        await use_case.execute(_notification(payment, ProviderStatus.FAILED))

    assert environment.only_payment().status is PaymentStatus.SETTLED
    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)


async def test_failed_delivery_releases_event_for_retry(account: Account) -> None:
    """Сбой обработки не «съедает» событие: следующая доставка проходит заново.

    Уведомление может обогнать наш собственный коммит создания платежа. Тогда
    платежа ещё нет, и пометить событие обработанным значило бы потерять
    уведомление навсегда.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()
    premature = HandleProviderWebhookInput(
        provider_event_id=EVENT_ID,
        provider_payment_id='provider-unknown',
        provider_status=ProviderStatus.SUCCEEDED,
    )

    with pytest.raises(EntityNotFoundError):
        await use_case.execute(premature)

    assert EVENT_ID not in environment.idempotency_store.reserved

    result = await use_case.execute(_notification(payment, ProviderStatus.SUCCEEDED))

    assert result.status is PaymentStatus.SETTLED


async def test_inflight_record_without_response_is_rejected(account: Account) -> None:
    """Запись по событию есть, ответа нет — обработка не завершена, повтор не пускаем.

    Так выглядит обработка, упавшая между резервацией и записью ответа.
    Отвечать «уже обработано» нельзя: неизвестно, применился ли переход, а
    выдать состояние платежа «как есть» значило бы соврать о результате доставки.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    notification = _notification(payment, ProviderStatus.SUCCEEDED)
    environment.idempotency_store.records[EVENT_ID] = IdempotencyRecord(
        key=EVENT_ID,
        request_hash=_event_fingerprint(notification),
        response=None,
        created_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + timedelta(seconds=WEBHOOK_RECORD_TTL_SECONDS),
    )

    with pytest.raises(DuplicateOperation):
        await environment.build_webhook_use_case().execute(notification)

    assert environment.only_payment().status is PaymentStatus.PROCESSING


# --- Границы обработки --------------------------------------------------------


@pytest.mark.parametrize('provider_status', [ProviderStatus.PENDING, ProviderStatus.PROCESSING])
async def test_intermediate_notification_changes_nothing(
    account: Account,
    provider_status: ProviderStatus,
) -> None:
    """Промежуточный статус — «ещё обрабатывается»: ни записи, ни события.

    Именно на этот сигнал опирается сценарий, когда в T-2.4 провайдер отвечает
    ``PROCESSING``: терминальный исход придёт вебхуком, выдумывать его рано.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    commits_before = environment.uow_factory.commits
    published_before = environment.event_publisher.types_published()

    result = await environment.build_webhook_use_case().execute(_notification(payment, provider_status))

    assert result.status is PaymentStatus.PROCESSING
    assert environment.only_payment().status is PaymentStatus.PROCESSING
    assert environment.event_publisher.types_published() == published_before
    assert environment.uow_factory.commits == commits_before


async def test_late_notification_after_settlement_changes_nothing(account: Account) -> None:
    """Запоздалое уведомление о закрытом платеже не двигает деньги.

    Платёж уже ``SETTLED``: отказ в этом состоянии неприменим, и вернуть холд по
    нему значило бы вернуть деньги за проведённый платёж.
    """
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    use_case = environment.build_webhook_use_case()
    await use_case.execute(_notification(payment, ProviderStatus.SUCCEEDED, event_id='event-1'))
    balance_after_settlement = environment.account_balance(account.id)
    published_before = environment.event_publisher.types_published()
    commits_before = environment.uow_factory.commits

    result = await use_case.execute(_notification(payment, ProviderStatus.FAILED, event_id='event-2'))

    assert result.status is PaymentStatus.SETTLED
    assert environment.only_payment().status is PaymentStatus.SETTLED
    assert environment.account_balance(account.id) == balance_after_settlement
    assert environment.event_publisher.types_published() == published_before
    assert environment.uow_factory.commits == commits_before


async def test_notification_for_unknown_operation_raises_not_found(account: Account) -> None:
    """Уведомление о неизвестной нам операции — доменная ошибка, а не «успех»."""
    environment = SagaEnvironment.build()
    await _processing_payment(environment, account)
    unknown = HandleProviderWebhookInput(
        provider_event_id='event-unknown',
        provider_payment_id='provider-does-not-exist',
        provider_status=ProviderStatus.SUCCEEDED,
    )

    with pytest.raises(EntityNotFoundError):
        await environment.build_webhook_use_case().execute(unknown)


async def test_notification_affects_only_matching_payment() -> None:
    """Уведомление адресно: меняется платёж своей операции, а не первый найденный."""
    environment = SagaEnvironment.build()
    first_account = Account(account_id=AccountId.new(), balance=_money(BALANCE))
    second_account = Account(account_id=AccountId.new(), balance=_money(BALANCE))
    first = await _processing_payment(environment, first_account, idempotency_key='first-key')
    environment.add_account(second_account)
    await environment.build_use_case().execute(
        CreatePaymentInput(
            from_account_id=second_account.id,
            amount=_money(AMOUNT),
            idempotency_key='second-key',
        )
    )
    # Платежей в хранилище теперь два, поэтому ищем второй напрямую: у
    # ``only_payment`` контракт «ровно один», и он тут проверял бы не то.
    second = next(payment for payment in environment.database.payments.values() if payment.id != first.id)

    await environment.build_webhook_use_case().execute(_notification(second, ProviderStatus.SUCCEEDED))

    assert environment.database.payments[second.id].status is PaymentStatus.SETTLED
    assert environment.database.payments[first.id].status is PaymentStatus.PROCESSING


# --- Вход сценария ------------------------------------------------------------


def test_input_trims_event_identifier() -> None:
    """Идентификатор события нормализуется: пробелы вокруг не делают другой ключ."""
    data = HandleProviderWebhookInput(
        provider_event_id='  event-1  ',
        provider_payment_id='provider-1',
        provider_status=ProviderStatus.SUCCEEDED,
    )

    assert data.provider_event_id == 'event-1'


@pytest.mark.parametrize(
    ('overrides', 'field'),
    [
        pytest.param({'provider_event_id': '   '}, 'provider_event_id', id='empty-event-id'),
        pytest.param({'provider_payment_id': '   '}, 'provider_payment_id', id='empty-payment-id'),
    ],
)
def test_input_rejects_empty_identifiers(overrides: dict[str, object], field: str) -> None:
    """Пустые идентификаторы отвергаются: «событие без id» дедуплицировать нечем."""
    values: dict[str, object] = {
        'provider_event_id': 'event-1',
        'provider_payment_id': 'provider-1',
        'provider_status': ProviderStatus.SUCCEEDED,
        **overrides,
    }

    with pytest.raises(InvalidValueError, match=field):
        HandleProviderWebhookInput(**values)  # type: ignore[arg-type]


def test_input_rejects_event_identifier_longer_than_storage_boundary() -> None:
    """Ключ события мерится той же границей, что и колонка хранилища ключей."""
    with pytest.raises(InvalidValueError, match='provider_event_id'):
        HandleProviderWebhookInput(
            provider_event_id='e' * (MAX_IDEMPOTENCY_KEY_LENGTH + 1),
            provider_payment_id='provider-1',
            provider_status=ProviderStatus.SUCCEEDED,
        )


def test_input_rejects_status_not_from_provider_scale() -> None:
    """Статус обязан быть из шкалы провайдера: «сырое» значение — работа адаптера."""
    with pytest.raises(InvalidValueError, match='provider_status'):
        HandleProviderWebhookInput(
            provider_event_id='event-1',
            provider_payment_id='provider-1',
            provider_status='SUCCEEDED',  # type: ignore[arg-type]
        )


# --- Экспорт слоя --------------------------------------------------------------


def test_use_case_is_exported_from_application_layer() -> None:
    """Сценарий виден через ``src.core_service.application`` — единую точку входа."""
    from src.core_service import application

    assert application.HandleProviderWebhookUseCase is HandleProviderWebhookUseCase
