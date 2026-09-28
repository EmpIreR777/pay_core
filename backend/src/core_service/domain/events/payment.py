"""Доменные события жизненного цикла платежей (T-1.4).

* ``PaymentCreated`` — платёж создан в начальном статусе PENDING.
* ``PaymentSettled`` — платёж успешно проведён внешним провайдером / системой.
* ``PaymentFailed`` — платёж завершился ошибкой / отклонён.
* ``PaymentRefunded`` — совершён полный или частичный возврат средств по платежу.
* ``PaymentCancelled`` — платёж отменён до передачи провайдеру, холд возвращён (T-2.6).
"""

from dataclasses import dataclass

from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.validation import (
    require_non_empty_str,
    require_optional_non_empty_str,
    require_payment_fields,
)
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money


@dataclass(frozen=True, kw_only=True)
class PaymentCreated(DomainEvent):
    """Событие создания платежа."""

    payment_id: PaymentId
    from_account_id: AccountId
    amount: Money

    def __post_init__(self) -> None:
        super().__post_init__()
        require_payment_fields(self.payment_id, self.from_account_id, self.amount)


@dataclass(frozen=True, kw_only=True)
class PaymentSettled(DomainEvent):
    """Событие успешного проведения платежа."""

    payment_id: PaymentId
    from_account_id: AccountId
    amount: Money
    provider_payment_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        require_payment_fields(self.payment_id, self.from_account_id, self.amount)
        object.__setattr__(
            self,
            'provider_payment_id',
            require_optional_non_empty_str(self.provider_payment_id, 'provider_payment_id'),
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
        require_payment_fields(self.payment_id, self.from_account_id, self.amount)
        object.__setattr__(self, 'reason', require_non_empty_str(self.reason, 'reason'))


@dataclass(frozen=True, kw_only=True)
class PaymentRefunded(DomainEvent):
    """Событие возврата средств по платежу."""

    payment_id: PaymentId
    from_account_id: AccountId
    refunded_amount: Money
    reason: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        # Поле у этого события называется refunded_amount, а правило одинаковое,
        # поэтому передаём значение явно — подпись правила не «помнит» имён полей.
        require_payment_fields(self.payment_id, self.from_account_id, self.refunded_amount)
        object.__setattr__(self, 'reason', require_optional_non_empty_str(self.reason, 'reason'))


@dataclass(frozen=True, kw_only=True)
class PaymentCancelled(DomainEvent):
    """Событие отмены платежа (T-2.6)."""

    payment_id: PaymentId
    from_account_id: AccountId
    amount: Money
    reason: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        require_payment_fields(self.payment_id, self.from_account_id, self.amount)
        object.__setattr__(self, 'reason', require_optional_non_empty_str(self.reason, 'reason'))
