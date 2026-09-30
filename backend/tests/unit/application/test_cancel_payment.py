"""Тесты сценария отмены платежа (T-2.6).

DoD задачи — «тесты на успех и отказ». Проверяется не только смена статуса, но и
денежная часть: отмена обязана вернуть холд, иначе клиент потеряет сумму за платёж,
который отменил.

Группы тестов отвечают на разные вопросы:

* **Успех** — статус, версия, баланс, событие, одна транзакция на всё;
* **Отказ по статусу** — отменять нечего: платёж уже не ``PENDING``;
* **Отказ по данным** — платёжа/счёта нет, счёт заблокирован;
* **Инварианты флоу** — блокировка счёта, возврат под ``SELECT FOR UPDATE``,
  перечитывание платежа под блокировкой (гонка с вебхуком).
"""

from decimal import Decimal

import pytest

from src.core_service.application import CancelPaymentUseCase
from src.core_service.application.dto import CancelPaymentInput, CreatePaymentInput
from src.core_service.application.ports.lock_manager import ACCOUNT_LOCK_RESOURCE_PREFIX
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import INITIAL_VERSION, Payment
from src.core_service.domain.events import PaymentCancelled
from src.core_service.domain.exceptions import (
    AccountBlocked,
    EntityNotFoundError,
    InvalidTransition,
    LockAcquisitionError,
    PaymentProviderError,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from tests.fakes import SagaEnvironment

BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')
REASON = 'Отменён клиентом'


def _money(value: Decimal) -> Money:
    return Money.from_number(value, Currency.RUB)


@pytest.fixture
def account() -> Account:
    return Account(account_id=AccountId.new(), balance=_money(BALANCE))


async def _pending_payment(environment: SagaEnvironment, account: Account) -> Payment:
    """Готовит окружение с платежом в PENDING — единственным отменяемым состоянием.

    Платёж в ``PENDING`` с уже списанным холдом получается настоящей сагой T-2.4
    при техническом сбое провайдера: деньги списаны, а статус не ушёл дальше. Ровно
    тот случай, в котором клиент и приходит отменять платёж.
    """
    environment.add_account(account)
    environment.provider.error = PaymentProviderError('провайдер недоступен')
    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(
            CreatePaymentInput(
                from_account_id=account.id,
                amount=_money(AMOUNT),
                idempotency_key='setup-key',
            )
        )
    environment.provider.error = None
    return environment.only_payment()


def _payment_in_status(
    environment: SagaEnvironment,
    account: Account,
    status: PaymentStatus,
) -> Payment:
    """Кладёт в хранилище платёж с нужным статусом, минуя сагу.

    Сага T-2.4 доводит платёж до ``PROCESSING``/``SETTLED``/``FAILED`` сама, но
    строить весь флоу ради каждого статуса долго: тест проверяет отмену, а не то,
    как платёж попал в этот статус.
    """
    environment.add_account(account)
    payment = Payment(
        payment_id=PaymentId.new(),
        from_account_id=account.id,
        amount=_money(AMOUNT),
        status=status,
        version=INITIAL_VERSION,
    )
    environment.database.payments[payment.id] = payment
    return payment


def _input(payment: Payment) -> CancelPaymentInput:
    return CancelPaymentInput(payment_id=payment.id, reason=REASON)


# --- Успех ---------------------------------------------------------------------


async def test_pending_payment_is_cancelled(account: Account) -> None:
    """Успех: платёж из PENDING уходит в CANCELLED (DoD)."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    result = await environment.build_cancel_use_case().execute(_input(payment))

    assert result.status is PaymentStatus.CANCELLED
    assert result.payment_id == payment.id


async def test_cancellation_is_persisted(account: Account) -> None:
    """Новый статус действительно записан в хранилище, а не только в ответе."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.only_payment().status is PaymentStatus.CANCELLED


async def test_cancellation_bumps_version(account: Account) -> None:
    """Версия выросла: смена статуса — реальное изменение для оптимистичной блокировки."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    version_before = payment.version

    await environment.build_cancel_use_case().execute(_input(payment))

    assert payment.version == version_before + 1


async def test_hold_is_returned_to_account(account: Account) -> None:
    """Деньги возвращаются на счёт: без этого клиент потерял бы сумму за отмену."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)

    await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.account_balance(account.id) == _money(BALANCE)


async def test_output_reports_cancelled_state(account: Account) -> None:
    """Ответ отдаёт фактическое состояние платежа после отмены."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    result = await environment.build_cancel_use_case().execute(_input(payment))

    assert result.amount == _money(AMOUNT)
    assert result.from_account_id == account.id
    assert result.created_at == payment.created_at
    assert result.updated_at == payment.updated_at


# --- Событие -------------------------------------------------------------------


async def test_cancellation_publishes_event(account: Account) -> None:
    """В outbox попадает PaymentCancelled — потребителям нужно знать об отмене."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    events_before = len(environment.event_publisher.published)

    await environment.build_cancel_use_case().execute(_input(payment))

    new_events = environment.event_publisher.published[events_before:]
    assert [type(event).__name__ for event in new_events] == ['PaymentCancelled']


async def test_published_event_carries_payment_data(account: Account) -> None:
    """Событие несёт платёж, счёт и сумму — иначе потребитель ничего не сведёт."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    await environment.build_cancel_use_case().execute(_input(payment))

    event = environment.event_publisher.published[-1]
    assert isinstance(event, PaymentCancelled)
    assert event.payment_id == payment.id
    assert event.from_account_id == account.id
    assert event.amount == _money(AMOUNT)
    assert event.reason == REASON


async def test_cancellation_without_reason_publishes_event(account: Account) -> None:
    """Причина необязательна: её отсутствие не мешает ни отмене, ни событию."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    result = await environment.build_cancel_use_case().execute(CancelPaymentInput(payment_id=payment.id))

    assert result.status is PaymentStatus.CANCELLED
    event = environment.event_publisher.published[-1]
    assert isinstance(event, PaymentCancelled)
    assert event.reason is None


# --- Атомарность и транзакции --------------------------------------------------


async def test_cancellation_commits_once(account: Account) -> None:
    """Один коммит на всё изменение: два коммита означали бы окно без денег."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    commits_before = environment.uow_factory.commits

    await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.uow_factory.commits - commits_before == 1


async def test_hold_is_returned_under_row_lock(account: Account) -> None:
    """Возврат читается под ``SELECT FOR UPDATE``: иначе параллельный платёж съест деньги.

    Возврат без блокировки строки — потерянное обновление баланса; возврат в другой
    транзакции — «платёж отменён, а денег ещё нет».
    """
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    units_before = len(environment.uow_factory.units)

    await environment.build_cancel_use_case().execute(_input(payment))

    writing_unit = environment.uow_factory.units[-1]
    assert writing_unit.accounts.get_for_update_calls == [account.id]
    # Две транзакции: короткое чтение для поиска счёта и запись с возвратом.
    assert len(environment.uow_factory.units) - units_before == 2


# --- Инварианты флоу -----------------------------------------------------------


async def test_account_is_locked(account: Account) -> None:
    """Отмена берёт ту же блокировку счёта, что и создание: иначе гонка по холду."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    environment.lock_manager.acquired_resources.clear()

    await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.lock_manager.acquired_resources == [f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{account.id}']


async def test_lock_is_released_after_cancellation(account: Account) -> None:
    """Блокировка снимается даже при успехе — иначе счёт встанет навсегда."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.lock_manager.held == set()


async def test_lock_is_released_after_failure(account: Account) -> None:
    """Падение тоже освобождает блокировку: заблокированный счёт никому не нужен."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    environment.stored_account(account.id).block()

    with pytest.raises(AccountBlocked):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.lock_manager.held == set()


async def test_lock_is_taken_before_the_write_transaction(account: Account) -> None:
    """Блокировка предшествует записи: иначе между чтением и записью вклинится вебхок."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    await environment.build_cancel_use_case().execute(_input(payment))

    entries = list(environment.journal)
    lock_entry = f'lock.acquire:account:{account.id}'
    assert lock_entry in entries
    assert entries.index(lock_entry) < entries.index('uow.commit')


async def test_creating_another_payment_works_after_cancellation(account: Account) -> None:
    """Блокировка действительно снята: следующая операция по счёту не спотыкается."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    await environment.build_cancel_use_case().execute(_input(payment))

    result = await environment.build_use_case().execute(
        CreatePaymentInput(
            from_account_id=account.id,
            amount=_money(Decimal('10.00')),
            idempotency_key='second-key',
        )
    )

    assert result.status is PaymentStatus.PROCESSING


# --- Отказ по статусу ----------------------------------------------------------


@pytest.mark.parametrize(
    'status',
    [PaymentStatus.PROCESSING, PaymentStatus.SETTLED, PaymentStatus.FAILED],
)
async def test_non_pending_payment_is_rejected(account: Account, status: PaymentStatus) -> None:
    """Отменять можно только PENDING: остальное уже не наше решение.

    ``PROCESSING`` означает, что провайдер уже получил операцию; ``SETTLED`` и
    ``FAILED`` терминальны. Во всех случаях отмена была бы враньём клиенту о
    состоянии его денег.
    """
    environment = SagaEnvironment.build()
    payment = _payment_in_status(environment, account, status)
    balance_before = environment.account_balance(account.id)

    with pytest.raises(InvalidTransition):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert payment.status is status
    assert environment.account_balance(account.id) == balance_before


async def test_already_cancelled_payment_is_rejected(account: Account) -> None:
    """Повторная отмена не возвращает деньги второй раз."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    use_case = environment.build_cancel_use_case()
    await use_case.execute(_input(payment))

    with pytest.raises(InvalidTransition):
        await use_case.execute(_input(payment))

    assert environment.account_balance(account.id) == _money(BALANCE)


async def test_rejected_cancellation_publishes_nothing(account: Account) -> None:
    """Неудачная отмена не оставляет событий: потребитель узнал бы об отмене, которой нет."""
    environment = SagaEnvironment.build()
    payment = _payment_in_status(environment, account, PaymentStatus.PROCESSING)
    events_before = len(environment.event_publisher.published)

    with pytest.raises(InvalidTransition):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert len(environment.event_publisher.published) == events_before


async def test_rejected_cancellation_does_not_move_version(account: Account) -> None:
    """Неудачный переход не трогает версию: в потоке локов не должно быть шума."""
    environment = SagaEnvironment.build()
    payment = _payment_in_status(environment, account, PaymentStatus.SETTLED)
    version_before = payment.version

    with pytest.raises(InvalidTransition):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert payment.version == version_before


# --- Отказ по данным -----------------------------------------------------------


async def test_unknown_payment_is_rejected() -> None:
    """Несуществующий платёж — «не найден», а не пустой успех."""
    environment = SagaEnvironment.build()

    with pytest.raises(EntityNotFoundError):
        await environment.build_cancel_use_case().execute(CancelPaymentInput(payment_id=PaymentId.new()))

    assert environment.event_publisher.published == []


async def test_unknown_payment_does_not_touch_accounts() -> None:
    """Чужой идентификатор не двигает баланс: мы даже не знаем, чей это счёт."""
    environment = SagaEnvironment.build()
    account = Account(account_id=AccountId.new(), balance=_money(BALANCE))
    environment.add_account(account)

    with pytest.raises(EntityNotFoundError):
        await environment.build_cancel_use_case().execute(CancelPaymentInput(payment_id=PaymentId.new()))

    assert environment.account_balance(account.id) == _money(BALANCE)


async def test_missing_account_is_rejected(account: Account) -> None:
    """Счёт исчез — вернуть холд некуда: падаем, а не списываем «в никуда»."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    del environment.database.accounts[account.id]

    with pytest.raises(EntityNotFoundError):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.only_payment().status is PaymentStatus.PENDING


async def test_blocked_account_is_rejected(account: Account) -> None:
    """Заблокированный счёт не принимает возврат: платёж остаётся PENDING.

    Возврат пропустить нельзя — клиент потеряет сумму, а починить холд потом будет
    некому. Освобождение счёта и повторный заход сценария сделают возврат позже.
    """
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    environment.stored_account(account.id).block()
    balance_before = environment.account_balance(account.id)

    with pytest.raises(AccountBlocked):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.only_payment().status is PaymentStatus.PENDING
    assert environment.account_balance(account.id) == balance_before


async def test_blocked_account_rolls_back_whole_cancellation(account: Account) -> None:
    """Падение возврата откатывает и статус: «CANCELLED без денег» не бывает."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    environment.stored_account(account.id).block()
    events_before = len(environment.event_publisher.published)

    with pytest.raises(AccountBlocked):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.only_payment().status is PaymentStatus.PENDING
    assert len(environment.event_publisher.published) == events_before


async def test_cancellation_after_unblocking_succeeds(account: Account) -> None:
    """Разблокированный счёт — тот же сценарий проходит: отказ был временным."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    use_case = environment.build_cancel_use_case()
    environment.stored_account(account.id).block()

    with pytest.raises(AccountBlocked):
        await use_case.execute(_input(payment))

    environment.stored_account(account.id).unblock()
    result = await use_case.execute(_input(payment))

    assert result.status is PaymentStatus.CANCELLED
    assert environment.account_balance(account.id) == _money(BALANCE)


# --- Гонки ---------------------------------------------------------------------


async def test_payment_closed_by_webhook_before_cancellation(account: Account) -> None:
    """Вебхок успел закрыть платёж: отмена отвергается, деньги не трогаются.

    Переход применяется к актуальному состоянию, а не к тому, что прочитали до
    блокировки: «отменить» уже ушедший провайдеру платёж — вернуть деньги дважды.
    """
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    payment.process('provider-1')
    balance_before = environment.account_balance(account.id)

    with pytest.raises(InvalidTransition):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.account_balance(account.id) == balance_before


async def test_payment_disappeared_before_cancellation(account: Account) -> None:
    """Платёж исчез между чтением и записью: отменять «по памяти» опасно.

    Первый проход (поиск счёта под блокировку) платёж находит, второй (под самой
    блокировкой) — уже нет. Ответить «отменён» и вернуть деньги платежу, которого
    не существует, нельзя.
    """
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    environment.database.payments = _VanishingPayments(environment.database.payments, payment.id)

    with pytest.raises(EntityNotFoundError):
        await environment.build_cancel_use_case().execute(_input(payment))


class _VanishingPayments(dict[PaymentId, Payment]):
    """Словарь платежей, который исчезает при втором чтении.

    Отдельный класс, а не подмена метода: у встроенного ``dict`` нельзя переопределить
    ``get`` так, чтобы это видел репозиторий, не трогая остальное хранилище.
    """

    def __init__(self, source: dict[PaymentId, Payment], vanishing_id: PaymentId) -> None:
        super().__init__(source)
        self._vanishing_id = vanishing_id
        self.reads = 0

    def get(self, key: PaymentId, default: Payment | None = None) -> Payment | None:
        self.reads += 1
        if self.reads == 2 and key == self._vanishing_id:
            return None
        return super().get(key, default)


async def test_busy_account_lock_is_reported(account: Account) -> None:
    """Занятый счёт — доменная ошибка, а не молчаливое ожидание или двойной возврат."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    resource = f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{account.id}'
    await environment.lock_manager.acquire(resource)

    with pytest.raises(LockAcquisitionError):
        await environment.build_cancel_use_case().execute(_input(payment))

    assert environment.only_payment().status is PaymentStatus.PENDING


# --- Экспорт слоя --------------------------------------------------------------


def test_use_case_is_exported_from_application_layer() -> None:
    """Сценарий виден через ``src.core_service.application`` — единую точку входа."""
    from src.core_service import application

    assert application.CancelPaymentUseCase is CancelPaymentUseCase
