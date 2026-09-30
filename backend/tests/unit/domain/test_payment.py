"""Тесты сущности Payment и её статус-машины (T-1.3).

DoD задачи:
- Тесты на все валидные и недопустимые переходы статус-машины.
- Защита финансовых инвариантов (положительная сумма, неизменность атрибутов).
- Корректность версионирования для оптимистичной блокировки.
- Работа вспомогательных методов (process, settle, fail, cancel).
- Тождество по PaymentId и стабильность хеширования.
"""

import itertools
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.core_service.domain import (
    AccountId,
    Currency,
    DomainError,
    InvalidAmountError,
    InvalidIdentifierError,
    InvalidTransition,
    InvalidValueError,
    Money,
    Payment,
    PaymentId,
    PaymentStatus,
)
from src.core_service.domain.entities.payment import INITIAL_VERSION, MIN_VERSION


def rub(value: str) -> Money:
    return Money.from_number(value, Currency.RUB)


def make_payment(
    amount: str = '100.00',
    currency: Currency = Currency.RUB,
    *,
    status: PaymentStatus = PaymentStatus.PENDING,
    version: int = INITIAL_VERSION,
    provider_payment_id: str | None = None,
    failure_reason: str | None = None,
) -> Payment:
    return Payment(
        payment_id=PaymentId.new(),
        from_account_id=AccountId.new(),
        amount=Money.from_number(amount, currency),
        status=status,
        version=version,
        provider_payment_id=provider_payment_id,
        failure_reason=failure_reason,
    )


# --- 1. Статус-машина: PaymentStatus VO ---


def test_payment_status_is_str_enum() -> None:
    assert isinstance(PaymentStatus.PENDING, str)
    assert PaymentStatus.PENDING == 'PENDING'
    assert str(PaymentStatus.PENDING) == 'PENDING'


def test_payment_status_terminal_flags() -> None:
    assert PaymentStatus.PENDING.is_terminal is False
    assert PaymentStatus.PROCESSING.is_terminal is False
    assert PaymentStatus.SETTLED.is_terminal is True
    assert PaymentStatus.FAILED.is_terminal is True
    assert PaymentStatus.CANCELLED.is_terminal is True


def test_payment_status_can_transition_to_method() -> None:
    assert PaymentStatus.PENDING.can_transition_to(PaymentStatus.PROCESSING) is True
    assert PaymentStatus.PENDING.can_transition_to(PaymentStatus.CANCELLED) is True
    assert PaymentStatus.PENDING.can_transition_to(PaymentStatus.SETTLED) is False

    assert PaymentStatus.PROCESSING.can_transition_to(PaymentStatus.SETTLED) is True
    assert PaymentStatus.PROCESSING.can_transition_to(PaymentStatus.FAILED) is True
    assert PaymentStatus.PROCESSING.can_transition_to(PaymentStatus.CANCELLED) is False

    assert PaymentStatus.SETTLED.can_transition_to(PaymentStatus.PROCESSING) is False
    assert PaymentStatus.FAILED.can_transition_to(PaymentStatus.PROCESSING) is False
    assert PaymentStatus.CANCELLED.can_transition_to(PaymentStatus.PROCESSING) is False

    assert PaymentStatus.PENDING.can_transition_to('PROCESSING') is False  # type: ignore[arg-type]


# --- 2. Создание сущности Payment и валидация инвариантов ---


def test_create_factory_creates_pending_payment() -> None:
    payment_id = PaymentId.new()
    account_id = AccountId.new()
    amount = rub('500.00')

    payment = Payment.create(
        payment_id=payment_id,
        from_account_id=account_id,
        amount=amount,
    )

    assert payment.id == payment_id
    assert payment.from_account_id == account_id
    assert payment.amount == amount
    assert payment.currency == Currency.RUB
    assert payment.status == PaymentStatus.PENDING
    assert payment.version == INITIAL_VERSION
    assert payment.provider_payment_id is None
    assert payment.failure_reason is None
    assert isinstance(payment.created_at, datetime)
    assert payment.created_at.tzinfo is not None
    assert payment.updated_at == payment.created_at


def test_constructor_validates_types() -> None:
    pid = PaymentId.new()
    aid = AccountId.new()
    amt = rub('10.00')

    with pytest.raises(InvalidIdentifierError, match='payment_id: ожидается экземпляр PaymentId'):
        Payment(payment_id=uuid4(), from_account_id=aid, amount=amt)  # type: ignore[arg-type]

    with pytest.raises(InvalidIdentifierError, match='from_account_id: ожидается экземпляр AccountId'):
        Payment(payment_id=pid, from_account_id=uuid4(), amount=amt)  # type: ignore[arg-type]

    with pytest.raises(InvalidValueError, match='amount: ожидается экземпляр Money'):
        Payment(payment_id=pid, from_account_id=aid, amount='10.00')  # type: ignore[arg-type]

    with pytest.raises(InvalidAmountError, match='не может быть нулевой'):
        Payment(payment_id=pid, from_account_id=aid, amount=rub('0.00'))

    with pytest.raises(InvalidValueError, match='status: ожидается экземпляр PaymentStatus'):
        Payment(payment_id=pid, from_account_id=aid, amount=amt, status='PENDING')  # type: ignore[arg-type]

    with pytest.raises(InvalidValueError, match='version: ожидается int'):
        Payment(payment_id=pid, from_account_id=aid, amount=amt, version=True)  # type: ignore[arg-type]

    with pytest.raises(InvalidValueError, match=f'version: не может быть меньше {MIN_VERSION}'):
        Payment(payment_id=pid, from_account_id=aid, amount=amt, version=0)


@pytest.mark.parametrize('bad_id', ['', '   ', 123])
def test_constructor_validates_provider_payment_id(bad_id: object) -> None:
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        Payment(
            payment_id=PaymentId.new(),
            from_account_id=AccountId.new(),
            amount=rub('10.00'),
            provider_payment_id=bad_id,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize('bad_reason', ['', '   ', 123])
def test_constructor_validates_failure_reason(bad_reason: object) -> None:
    with pytest.raises(InvalidValueError, match='failure_reason'):
        Payment(
            payment_id=PaymentId.new(),
            from_account_id=AccountId.new(),
            amount=rub('10.00'),
            failure_reason=bad_reason,  # type: ignore[arg-type]
        )


def test_constructor_validates_datetime_timezone_aware() -> None:
    naive_dt = datetime(2025, 1, 1, 12, 0, 0)
    with pytest.raises(InvalidValueError, match='created_at: datetime должен быть timezone-aware'):
        Payment(
            payment_id=PaymentId.new(),
            from_account_id=AccountId.new(),
            amount=rub('10.00'),
            created_at=naive_dt,
        )

    with pytest.raises(InvalidValueError, match='created_at: ожидается datetime'):
        Payment(
            payment_id=PaymentId.new(),
            from_account_id=AccountId.new(),
            amount=rub('10.00'),
            created_at='2025-01-01',  # type: ignore[arg-type]
        )
    # non-UTC aware datetime
    tz_plus_3 = timezone(timedelta(hours=3))
    dt_with_offset = datetime(2025, 1, 1, 12, 0, 0, tzinfo=tz_plus_3)
    with pytest.raises(InvalidValueError, match='временная зона должна быть строго UTC'):
        Payment(
            payment_id=PaymentId.new(),
            from_account_id=AccountId.new(),
            amount=rub('10.00'),
            created_at=dt_with_offset,
        )

    aware_dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(InvalidValueError, match='updated_at не может предшествовать created_at'):
        Payment(
            payment_id=PaymentId.new(),
            from_account_id=AccountId.new(),
            amount=rub('10.00'),
            # --- 3. Матричные тесты переходов статус-машины ---
            created_at=aware_dt,
            updated_at=aware_dt - timedelta(seconds=1),
        )


VALID_TRANSITIONS: list[tuple[PaymentStatus, PaymentStatus]] = [
    (PaymentStatus.PENDING, PaymentStatus.PROCESSING),
    (PaymentStatus.PENDING, PaymentStatus.CANCELLED),
    (PaymentStatus.PROCESSING, PaymentStatus.SETTLED),
    (PaymentStatus.PROCESSING, PaymentStatus.FAILED),
]

ALL_STATUSES = list(PaymentStatus)
INVALID_TRANSITIONS: list[tuple[PaymentStatus, PaymentStatus]] = [
    (from_s, to_s)
    for from_s, to_s in itertools.product(ALL_STATUSES, ALL_STATUSES)
    if (from_s, to_s) not in VALID_TRANSITIONS
]


@pytest.mark.parametrize(('from_status', 'to_status'), VALID_TRANSITIONS)
def test_valid_transitions_succeed_and_bump_version(
    from_status: PaymentStatus,
    to_status: PaymentStatus,
) -> None:
    initial_version = 2
    payment = make_payment(status=from_status, version=initial_version)
    created_at = payment.created_at

    payment.transition_to(to_status)

    assert payment.status == to_status
    assert payment.version == initial_version + 1
    assert payment.updated_at >= created_at


@pytest.mark.parametrize(('from_status', 'to_status'), INVALID_TRANSITIONS)
def test_invalid_transitions_are_rejected_and_do_not_bump_version(
    from_status: PaymentStatus,
    to_status: PaymentStatus,
) -> None:
    initial_version = 1
    payment = make_payment(status=from_status, version=initial_version)
    initial_updated_at = payment.updated_at

    with pytest.raises(InvalidTransition) as exc_info:
        payment.transition_to(to_status)

    assert issubclass(InvalidTransition, DomainError)
    assert f'из {from_status} в {to_status}' in str(exc_info.value)
    assert payment.status == from_status
    assert payment.version == initial_version
    assert payment.updated_at == initial_updated_at


def test_transition_to_rejects_non_payment_status() -> None:
    payment = make_payment(status=PaymentStatus.PENDING)
    with pytest.raises(InvalidValueError, match='target_status: ожидается экземпляр PaymentStatus'):
        payment.transition_to('PROCESSING')  # type: ignore[arg-type]


# --- 4. Тесты вспомогательных методов жизненного цикла ---


def test_process_helper_sets_provider_payment_id() -> None:
    payment = make_payment(status=PaymentStatus.PENDING)
    assert payment.provider_payment_id is None

    payment.process('provider-tx-12345')

    assert payment.status == PaymentStatus.PROCESSING
    assert payment.provider_payment_id == 'provider-tx-12345'
    assert payment.version == INITIAL_VERSION + 1


def test_settle_helper_settles_processing_payment() -> None:
    payment = make_payment(status=PaymentStatus.PROCESSING, provider_payment_id='tx-1')

    payment.settle()

    assert payment.status == PaymentStatus.SETTLED
    assert payment.provider_payment_id == 'tx-1'
    assert payment.version == INITIAL_VERSION + 1


def test_fail_helper_sets_failure_reason() -> None:
    payment = make_payment(status=PaymentStatus.PROCESSING, provider_payment_id='tx-1')

    payment.fail('Card declined by bank')

    assert payment.status == PaymentStatus.FAILED
    assert payment.failure_reason == 'Card declined by bank'
    assert payment.version == INITIAL_VERSION + 1


def test_cancel_helper_cancels_pending_payment() -> None:
    payment = make_payment(status=PaymentStatus.PENDING)

    payment.cancel()

    assert payment.status == PaymentStatus.CANCELLED
    assert payment.version == INITIAL_VERSION + 1


@pytest.mark.parametrize('bad_id', ['', '  ', 123])
def test_transition_to_rejects_invalid_provider_payment_id(bad_id: object) -> None:
    payment = make_payment(status=PaymentStatus.PENDING)
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        payment.transition_to(PaymentStatus.PROCESSING, provider_payment_id=bad_id)  # type: ignore[arg-type]


@pytest.mark.parametrize('bad_reason', ['', '  ', 123])
def test_transition_to_rejects_invalid_failure_reason(bad_reason: object) -> None:
    payment = make_payment(status=PaymentStatus.PROCESSING)
    with pytest.raises(InvalidValueError, match='failure_reason'):
        payment.transition_to(PaymentStatus.FAILED, failure_reason=bad_reason)  # type: ignore[arg-type]


# --- 5. Тождество сущности и хеширование ---


def test_payment_equality_is_by_identity() -> None:
    payment_id = PaymentId.new()
    account_1 = AccountId.new()
    account_2 = AccountId.new()

    p1 = Payment(payment_id=payment_id, from_account_id=account_1, amount=rub('100.00'))
    p2 = Payment(
        payment_id=payment_id,
        from_account_id=account_2,
        amount=rub('999.00'),
        status=PaymentStatus.SETTLED,
    )

    assert p1 == p2
    assert p1 != Payment(payment_id=PaymentId.new(), from_account_id=account_1, amount=rub('100.00'))
    assert p1 != 'not-a-payment'


def test_payment_is_hashable_by_id_even_when_mutated() -> None:
    p = make_payment(status=PaymentStatus.PENDING)
    payment_set = {p}

    assert p in payment_set

    p.process('tx-123')
    assert p in payment_set


def test_payment_repr() -> None:
    p = make_payment(amount='123.45', status=PaymentStatus.PENDING)
    repr_str = repr(p)
    assert 'Payment(' in repr_str
    assert '123.45' in repr_str
    assert 'PENDING' in repr_str


# --- Ожидаемая версия в хранилище (T-3.6) ---


def test_persisted_version_is_separate_from_version() -> None:
    """Ожидаемая версия платежа остаётся прочитанной, пока запись не подтверждена.

    Переход статуса поднимает ``version``, но не ``persisted_version``: именно с
    последней сверяется оптимистичный ``UPDATE``, и она обязана описывать то, что
    лежит в базе, а не то, что платёж уже успел изменить в памяти.
    """
    payment = make_payment(version=4)

    payment.process('ext-1')

    assert payment.version == 5
    assert payment.persisted_version == 4

    payment.mark_persisted()

    assert payment.persisted_version == 5


def test_persisted_version_matches_version_for_new_payment() -> None:
    """Новый платёж ждёт в хранилище свою начальную версию."""
    payment = make_payment()

    assert payment.persisted_version == payment.version == INITIAL_VERSION
