"""Тесты сценария чтения платежа (T-2.5).

DoD задачи — «тест сценария». Проверяется не только выдача состояния, но и вся
логика актуализации, потому что именно в ней money-critical решение: у «зависшего»
платежа сценарий идёт к провайдеру и **может вернуть деньги на счёт** при отказе.

Тесты сгруппированы по вопросам, на которые сценарий обязан давать честный ответ:

* кого и когда спрашиваем (терминальные не трогаем, PENDING негде спросить);
* что делаем с ответом (фиксируем переход / игнорируем / падаем на гонке);
* что происходит с деньгами (холд остаётся при успехе, возвращается при отказе);
* где проходят транзакции относительно сетевого вызова.
"""

from decimal import Decimal
from typing import Any

import pytest

from src.core_service.application.dto import GetPaymentInput
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.application.use_cases.payment_sync import PROVIDER_REJECTION_REASON
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import EntityNotFoundError, PaymentProviderError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from tests.fakes import SagaEnvironment

BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')


def _account() -> Account:
    return Account(account_id=AccountId.new(), balance=Money.from_number(BALANCE, Currency.RUB))


@pytest.fixture
def account() -> Account:
    return _account()


async def _processing_payment(environment: SagaEnvironment, account: Account) -> Payment:
    """Готовит окружение с платежом в PROCESSING — типичное «зависшее» состояние.

    Платёж создаётся настоящей сагой T-2.4, а не вручную: так тест проверяет
    реальный флоу данных, а не подложенную вручную фикстуру.
    """
    from src.core_service.application.dto import CreatePaymentInput

    environment.add_account(account)
    data = CreatePaymentInput(
        from_account_id=account.id,
        amount=Money.from_number(AMOUNT, Currency.RUB),
        idempotency_key='setup-key',
    )
    await environment.build_use_case().execute(data)
    return environment.only_payment()


def _input(payment: Payment) -> GetPaymentInput:
    return GetPaymentInput(payment_id=payment.id)


# --- Базовое чтение ----------------------------------------------------------


async def test_returns_payment_state(account: Account) -> None:
    """Сценарий отдаёт состояние созданного платежа (DoD)."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.payment_id == payment.id
    assert result.status is PaymentStatus.PROCESSING
    assert result.amount == Money.from_number(AMOUNT, Currency.RUB)
    assert result.from_account_id == account.id
    assert result.provider_payment_id == payment.provider_payment_id


async def test_returns_both_timestamps(account: Account) -> None:
    """Клиенту нужны обе метки: когда создан и когда последний раз менялся."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.created_at == payment.created_at
    assert result.updated_at == payment.updated_at


async def test_missing_payment_raises_not_found(account: Account) -> None:
    """Нет платежа — доменная ошибка, а не пустой ответ «всё хорошо»."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    data = GetPaymentInput(payment_id=PaymentId.new())

    with pytest.raises(EntityNotFoundError):
        await environment.build_get_use_case().execute(data)


# --- Кого спрашиваем ---------------------------------------------------------


async def test_does_not_query_provider_for_settled_payment(account: Account) -> None:
    """Терминальный платёж не опрашивается: исход уже известен."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.PENDING)
    payment = await _processing_payment(environment, account)
    payment.settle()
    environment.database.payments[payment.id] = payment

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.SETTLED
    assert environment.provider.status_queries == []


async def test_does_not_query_provider_for_pending_payment(account: Account) -> None:
    """У PENDING нет идентификатора операции — спрашивать некого."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    payment = Payment.create(
        payment_id=PaymentId.new(),
        from_account_id=account.id,
        amount=Money.from_number(AMOUNT, Currency.RUB),
    )
    environment.database.payments[payment.id] = payment

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.PENDING
    assert environment.provider.status_queries == []


async def test_queries_provider_with_operation_identifier(account: Account) -> None:
    """Актуализация спрашивает именно операцию провайдера, а не наш PaymentId."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)

    await environment.build_get_use_case().execute(_input(payment))

    assert environment.provider.status_queries == [payment.provider_payment_id]


# --- Инвариант: сеть вне транзакции -----------------------------------------


async def test_provider_queried_outside_transaction(account: Account) -> None:
    """Ключевой инвариант: сетевой вызов не внутри транзакции БД (AGENT.md §4.2)."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)

    await environment.build_get_use_case().execute(_input(payment))

    assert environment.provider.open_uow_during_queries == [False]


async def test_reading_and_actualization_use_separate_transactions(account: Account) -> None:
    """Чтение и запись — разные транзакции, между ними и живёт сетевой вызов."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)
    before = len(environment.uow_factory.units)

    await environment.build_get_use_case().execute(_input(payment))

    entries = list(environment.journal)
    open_index = entries.index('uow.open', before)
    provider_index = entries.index('provider.get_status', before)
    assert provider_index > open_index, 'вызов провайдера должен идти после чтения'
    # Всего две транзакции: чтение и запись результата.
    assert len(environment.uow_factory.units) - before == 2


# --- Промежуточный ответ: ничего не меняем -----------------------------------


@pytest.mark.parametrize('provider_status', [ProviderStatus.PENDING, ProviderStatus.PROCESSING])
async def test_intermediate_provider_status_changes_nothing(
    account: Account,
    provider_status: ProviderStatus,
) -> None:
    """Провайдер ещё обрабатывает: платёж остаётся PROCESSING, записи не будет."""
    environment = SagaEnvironment.build(status_query=provider_status)
    payment = await _processing_payment(environment, account)
    version_before = payment.version
    events_before = len(environment.event_publisher.published)

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.PROCESSING
    assert environment.only_payment().version == version_before
    # Ни одного нового события: актуализация ничего не меняла.
    assert len(environment.event_publisher.published) == events_before


async def test_intermediate_status_does_not_create_second_transaction(account: Account) -> None:
    """Пока статус не изменился, лишнего коммита не делаем: версия строки должна быть честной."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.PROCESSING)
    payment = await _processing_payment(environment, account)
    before = len(environment.uow_factory.units)

    await environment.build_get_use_case().execute(_input(payment))

    assert len(environment.uow_factory.units) - before == 1


# --- Успешная актуализация: провайдер довёл операцию до конца ----------------


async def test_successful_actualization_settles_payment(account: Account) -> None:
    """Провайдер сообщил SUCCEEDED — платёж фиксируется как SETTLED."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.SETTLED
    assert environment.only_payment().status is PaymentStatus.SETTLED


async def test_successful_actualization_publishes_settled_event(account: Account) -> None:
    """Факт проведения попадает в outbox — потребители узнают о завершении."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)
    events_before = len(environment.event_publisher.published)

    await environment.build_get_use_case().execute(_input(payment))

    new_events = environment.event_publisher.published[events_before:]
    assert [type(event).__name__ for event in new_events] == ['PaymentSettled']


async def test_successful_actualization_keeps_hold(account: Account) -> None:
    """Успех означает, что деньги у провайдера: холд остаётся, баланс не трогаем."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)

    await environment.build_get_use_case().execute(_input(payment))

    assert environment.account_balance(account.id) == Money.from_number(BALANCE - AMOUNT, Currency.RUB)


async def test_refunded_at_provider_settles_payment(account: Account) -> None:
    """REFUNDED — тоже завершённая операция: статус платежа SETTLED (T-2.2)."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.REFUNDED)
    payment = await _processing_payment(environment, account)

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.SETTLED


async def test_second_read_does_not_ask_provider_again(account: Account) -> None:
    """После фиксации статус терминальный — повторное чтение уже не ходит в сеть."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)
    use_case = environment.build_get_use_case()

    await use_case.execute(_input(payment))
    await use_case.execute(_input(payment))

    assert len(environment.provider.status_queries) == 1


# --- Актуализация обнаруживает отказ: деньги возвращаются --------------------


async def test_actualized_rejection_marks_payment_failed(account: Account) -> None:
    """Провайдер отклонил уже созданную операцию — платёж становится FAILED."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.FAILED
    assert result.failure_reason == PROVIDER_REJECTION_REASON


async def test_actualized_rejection_returns_hold(account: Account) -> None:
    """Отказ, выявленный при чтении, возвращает деньги клиенту.

    Это тот же бизнес-отказ, что и в T-2.4, — просто мы узнали о нём позже, при
    чтении, а не сразу. Если бы актуализация не возвращала холд, клиент потерял бы
    сумму за платёж, который провайдер отклонил.
    """
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)

    await environment.build_get_use_case().execute(_input(payment))

    assert environment.account_balance(account.id) == Money.from_number(BALANCE, Currency.RUB)


async def test_actualized_rejection_publishes_failure_and_refund(account: Account) -> None:
    """Возврат виден в outbox: и отказ, и факт возврата денег."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)
    events_before = len(environment.event_publisher.published)

    await environment.build_get_use_case().execute(_input(payment))

    new_events = environment.event_publisher.published[events_before:]
    assert [type(event).__name__ for event in new_events] == ['PaymentFailed', 'PaymentRefunded']


async def test_refund_event_carries_returned_amount(account: Account) -> None:
    """Событие возврата несёт сумму — потребитель знает размер движения денег."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)
    events_before = len(environment.event_publisher.published)

    await environment.build_get_use_case().execute(_input(payment))

    refund = environment.event_publisher.published[events_before + 1]
    assert refund.refunded_amount == Money.from_number(AMOUNT, Currency.RUB)


async def test_actualized_rejection_is_recorded_once(account: Account) -> None:
    """Повторное чтение не возвращает деньги второй раз: статус уже терминальный."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)
    use_case = environment.build_get_use_case()

    await use_case.execute(_input(payment))
    await use_case.execute(_input(payment))

    assert environment.account_balance(account.id) == Money.from_number(BALANCE, Currency.RUB)


# --- Гонки: между чтением и записью состояние могло измениться ----------------


async def test_webhook_won_race_does_not_break_read(account: Account) -> None:
    """Вебхук успел закрыть платёж, пока мы спрашивали провайдера.

    Ответ провайдера запоздал: применять его нельзя, иначе статус-машина отвергла
    бы переход из терминального статуса и клиент получил бы 500 на пустом месте.
    Сценарий обязан отдать актуальное (уже терминальное) состояние.
    """
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)
    original_get_status = environment.provider.get_status

    async def settle_then_ask(provider_payment_id: str) -> Any:
        # Вебхук закрывает платёж прямо во время нашего запроса. В базу кладётся
        # отдельный экземпляр — как это делает настоящий вебхук в своём процессе,
        # — поэтому наш локальный объект остаётся PROCESSING, а свежий read
        # увидит уже SETTLED.
        settled = _settled_copy(payment)
        environment.database.payments[settled.id] = settled
        return await original_get_status(provider_payment_id)

    environment.provider.get_status = settle_then_ask  # type: ignore[method-assign]

    result = await environment.build_get_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.SETTLED
    # Деньги не двинулись: возврат был бы уже неправдой.
    assert environment.account_balance(account.id) == Money.from_number(BALANCE - AMOUNT, Currency.RUB)


def _settled_copy(payment: Payment) -> Payment:
    """Отдельный экземпляр того же платежа в терминальном статусе.

    Именно так выглядит вебхук: он читает платёж в своём процессе и коммитит его,
    а у читающего сценария остаётся собственный, устаревший объект. Проверка на
    «устаревшем» экземпляре ловит баг, который проверка на том же объекте — нет:
    применение перехода к уже-SETTLED платежу в памяти тихо «прошло бы».
    """
    settled = Payment(
        payment_id=payment.id,
        from_account_id=payment.from_account_id,
        amount=payment.amount,
        status=PaymentStatus.PROCESSING,
        provider_payment_id=payment.provider_payment_id,
        created_at=payment.created_at,
    )
    settled.settle()
    return settled


async def test_payment_lost_during_actualization_raises_not_found(
    account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Платёж исчез между чтением и записью — отдавать устаревшее состояние нельзя."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)
    original_get_status = environment.provider.get_status

    async def ask_then_lose_payment(provider_payment_id: str) -> Any:
        result = await original_get_status(provider_payment_id)
        environment.database.payments.clear()
        return result

    monkeypatch.setattr(environment.provider, 'get_status', ask_then_lose_payment)

    with pytest.raises(EntityNotFoundError):
        await environment.build_get_use_case().execute(_input(payment))


# --- Технический сбой провайдера ---------------------------------------------


async def test_provider_unavailable_propagates_error(account: Account) -> None:
    """Шлюз недоступен — наружу честная ошибка, а не «платёж не найден»."""
    environment = SagaEnvironment.build(status_query_error=PaymentProviderError('шлюз недоступен'))
    payment = await _processing_payment(environment, account)

    with pytest.raises(PaymentProviderError):
        await environment.build_get_use_case().execute(_input(payment))


async def test_provider_failure_keeps_payment_processing(account: Account) -> None:
    """Технический сбой не меняет известное нам состояние и не двигает деньги."""
    environment = SagaEnvironment.build(status_query_error=PaymentProviderError('таймаут шлюза'))
    payment = await _processing_payment(environment, account)
    events_before = len(environment.event_publisher.published)

    with pytest.raises(PaymentProviderError):
        await environment.build_get_use_case().execute(_input(payment))

    assert environment.only_payment().status is PaymentStatus.PROCESSING
    assert environment.account_balance(account.id) == Money.from_number(BALANCE - AMOUNT, Currency.RUB)
    assert len(environment.event_publisher.published) == events_before


async def test_read_after_provider_recovers_actualizes_successfully(account: Account) -> None:
    """После восстановления шлюза повторное чтение доводит платёж до конца.

    Ключевой сценарий: клиент увидел ошибку, перечитал и получил готовый статус.
    """
    environment = SagaEnvironment.build(
        status_query=ProviderStatus.PROCESSING,
        status_query_error=PaymentProviderError('шлюз недоступен'),
    )
    payment = await _processing_payment(environment, account)
    use_case = environment.build_get_use_case()

    with pytest.raises(PaymentProviderError):
        await use_case.execute(_input(payment))

    environment.provider.status_query_error = None
    environment.provider.status_query = ProviderStatus.SUCCEEDED
    result = await use_case.execute(_input(payment))

    assert result.status is PaymentStatus.SETTLED
