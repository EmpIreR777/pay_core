"""Доменные события жизненного цикла платежей (T-1.4).

* ``PaymentCreated`` — платёж создан в начальном статусе PENDING.
* ``PaymentSettled`` — платёж успешно проведён внешним провайдером / системой.
* ``PaymentFailed`` — платёж завершился ошибкой / отклонён.
* ``PaymentRefunded`` — совершён полный или частичный возврат средств по платежу.
"""

from dataclasses import dataclass

from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.exceptions import InvalidValueError
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money


def _require_type[T](val: object, expected_type: type[T], field_name: str) -> T:
    if not isinstance(val, expected_type):
        raise InvalidValueError(
            f'{field_name}: ожидается экземпляр {expected_type.__name__}, получено {type(val).__name__}'
        )
    return val


@dataclass(frozen=True, kw_only=True)
class PaymentCreated(DomainEvent):
    """Событие создания платежа."""

    payment_id: PaymentId
    from_account_id: AccountId
    amount: Money

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_type(self.payment_id, PaymentId, 'payment_id')
        _require_type(self.from_account_id, AccountId, 'from_account_id')
        _require_type(self.amount, Money, 'amount')


@dataclass(frozen=True, kw_only=True)
class PaymentSettled(DomainEvent):
    """Событие успешного проведения платежа."""

    payment_id: PaymentId
    from_account_id: AccountId
    amount: Money
    provider_payment_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_type(self.payment_id, PaymentId, 'payment_id')
        _require_type(self.from_account_id, AccountId, 'from_account_id')
        _require_type(self.amount, Money, 'amount')
        if self.provider_payment_id is not None and not isinstance(self.provider_payment_id, str):
            raise InvalidValueError(
                f'provider_payment_id: ожидается str или None, получено {type(self.provider_payment_id).__name__}'
            )


@dataclass(frozen=True, kw_only=True)
class PaymentFailed(DomainEvent):
    """Событие неуспешного завершения платежа."""

    payment_id: PaymentId
    from_account_id: AccountId
    amount: Money
    reason: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_type(self.payment_id, PaymentId, 'payment_id')
        _require_type(self.from_account_id, AccountId, 'from_account_id')
        _require_type(self.amount, Money, 'amount')
        if not isinstance(self.reason, str):
            raise InvalidValueError(f'reason: ожидается str, получено {type(self.reason).__name__}')


@dataclass(frozen=True, kw_only=True)
class PaymentRefunded(DomainEvent):
    """Событие возврата средств по платежу."""

    payment_id: PaymentId
    from_account_id: AccountId
    refunded_amount: Money
    reason: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_type(self.payment_id, PaymentId, 'payment_id')
        _require_type(self.from_account_id, AccountId, 'from_account_id')
        _require_type(self.refunded_amount, Money, 'refunded_amount')
        if self.reason is not None and not isinstance(self.reason, str):
            raise InvalidValueError(f'reason: ожидается str или None, получено {type(self.reason).__name__}')
