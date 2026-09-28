"""Тесты единых примитивов валидации (T-2.3).

Каждое правило проверяется здесь ровно один раз — именно для этого модуль
``domain/validation.py`` и создан. Тесты конкретных правил в домене (``Account``,
``Payment``, события, DTO) проверяют поведение, а не текст ошибки, поэтому
формат сообщений живёт в одном месте.
"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.core_service.domain.exceptions import (
    InvalidAmountError,
    InvalidIdentifierError,
    InvalidValueError,
)
from src.core_service.domain.validation import (
    require_int,
    require_min_int,
    require_money,
    require_non_empty_str,
    require_non_zero_money,
    require_optional_non_empty_str,
    require_positive_money,
    require_type,
    require_utc,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import (
    PaymentStatus,
    require_provider_payment_coherence,
)


def rub(value: str) -> Money:
    return Money.from_number(Decimal(value), Currency.RUB)


# --- require_type -------------------------------------------------------------


def test_require_type_returns_value() -> None:
    account_id = AccountId.new()
    assert require_type(account_id, AccountId, 'account_id') is account_id


@pytest.mark.parametrize('bad_value', [str(AccountId.new()), 1, None, Money.zero(Currency.RUB)])
def test_require_type_rejects_wrong_type(bad_value: object) -> None:
    with pytest.raises(InvalidValueError, match='account_id: ожидается экземпляр AccountId'):
        require_type(bad_value, AccountId, 'account_id')


def test_require_type_supports_custom_error_type() -> None:
    """Правило одно, семантика ошибки разная: идентификаторы — ``InvalidIdentifierError``."""
    with pytest.raises(InvalidIdentifierError, match='account_id: ожидается экземпляр AccountId'):
        require_type('x', AccountId, 'account_id', error_type=InvalidIdentifierError)  # type: ignore[arg-type]


# --- require_int / require_min_int --------------------------------------------


@pytest.mark.parametrize('bad_value', [True, False, '3', 3.0, None])
def test_require_int_rejects_bool_and_non_int(bad_value: object) -> None:
    with pytest.raises(InvalidValueError, match='version: ожидается int'):
        require_int(bad_value, 'version')


def test_require_int_accepts_int() -> None:
    assert require_int(7, 'version') == 7


@pytest.mark.parametrize('bad_value', [0, -1])
def test_require_min_int_rejects_too_small(bad_value: int) -> None:
    with pytest.raises(InvalidValueError, match='version: не может быть меньше 1'):
        require_min_int(bad_value, 'version', minimum=1)


def test_require_min_int_accepts_boundary() -> None:
    """Нижняя граница включительна: версия 1 — валидное состояние записи."""
    assert require_min_int(1, 'version', minimum=1) == 1


# --- require_utc --------------------------------------------------------------


def test_require_utc_accepts_utc_aware() -> None:
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert require_utc(aware, 'created_at') is aware


def test_require_utc_rejects_naive() -> None:
    with pytest.raises(InvalidValueError, match='должен быть timezone-aware'):
        require_utc(datetime(2026, 1, 1, 12, 0), 'created_at')


def test_require_utc_rejects_other_timezone() -> None:
    with pytest.raises(InvalidValueError, match='строго UTC'):
        require_utc(datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=3))), 'created_at')


def test_require_utc_rejects_non_datetime() -> None:
    with pytest.raises(InvalidValueError, match='created_at: ожидается datetime'):
        require_utc('2026-01-01', 'created_at')  # type: ignore[arg-type]


# --- строки -------------------------------------------------------------------


def test_require_non_empty_str_strips_value() -> None:
    assert require_non_empty_str('  prov-1  ', 'provider_payment_id') == 'prov-1'


@pytest.mark.parametrize('bad_value', ['', '   ', None, 42])
def test_require_non_empty_str_rejects_bad_values(bad_value: object) -> None:
    with pytest.raises(InvalidValueError, match='provider_payment_id: ожидается непустая строка'):
        require_non_empty_str(bad_value, 'provider_payment_id')


def test_require_optional_non_empty_str_allows_none() -> None:
    assert require_optional_non_empty_str(None, 'failure_reason') is None


def test_require_optional_non_empty_str_strips_value() -> None:
    assert require_optional_non_empty_str(' declined ', 'failure_reason') == 'declined'


@pytest.mark.parametrize('bad_value', ['', '   ', 42])
def test_require_optional_non_empty_str_rejects_bad_values(bad_value: object) -> None:
    with pytest.raises(InvalidValueError, match='failure_reason: ожидается непустая строка или None'):
        require_optional_non_empty_str(bad_value, 'failure_reason')


# --- деньги -------------------------------------------------------------------


def test_require_money_accepts_money() -> None:
    amount = rub('10.00')
    assert require_money(amount, 'amount') is amount


@pytest.mark.parametrize('bad_value', [10, 10.5, '10.00', Decimal('10.00'), None])
def test_require_money_rejects_non_money(bad_value: object) -> None:
    with pytest.raises(InvalidValueError, match='amount: ожидается экземпляр Money'):
        require_money(bad_value, 'amount')


def test_require_non_zero_money_rejects_zero() -> None:
    with pytest.raises(InvalidAmountError, match='amount: сумма не может быть нулевой'):
        require_non_zero_money(Money.zero(Currency.RUB), 'amount')


def test_require_positive_money_enforces_type_and_non_zero() -> None:
    """Композиция двух правил: и тип, и ненулевая сумма."""
    assert require_positive_money(rub('0.01'), 'amount') == rub('0.01')
    with pytest.raises(InvalidAmountError, match='amount: сумма не может быть нулевой'):
        require_positive_money(Money.zero(Currency.RUB), 'amount')
    with pytest.raises(InvalidValueError, match='amount: ожидается экземпляр Money'):
        require_positive_money(10, 'amount')  # type: ignore[arg-type]


# --- согласованность статуса и идентификатора провайдера ----------------------


@pytest.mark.parametrize('provider_bound_status', [PaymentStatus.PROCESSING, PaymentStatus.SETTLED])
def test_provider_bound_status_requires_provider_payment_id(provider_bound_status: PaymentStatus) -> None:
    with pytest.raises(InvalidValueError, match='обязан иметь provider_payment_id'):
        require_provider_payment_coherence(provider_bound_status, None)


def test_pending_status_forbids_provider_payment_id() -> None:
    with pytest.raises(InvalidValueError, match='status=PENDING'):
        require_provider_payment_coherence(PaymentStatus.PENDING, 'prov-1')


@pytest.mark.parametrize('status', list(PaymentStatus))
def test_coherence_accepts_missing_provider_payment_id_for_other_statuses(status: PaymentStatus) -> None:
    """Без идентификатора допустим любой статус, кроме «уже у провайдера»."""
    if status in {PaymentStatus.PROCESSING, PaymentStatus.SETTLED}:
        return
    require_provider_payment_coherence(status, None)


def test_coherence_accepts_processing_with_provider_payment_id() -> None:
    require_provider_payment_coherence(PaymentStatus.PROCESSING, 'prov-1')
