"""Статус платежа как типизированный Value Object / Enum (T-1.3).

Статусная модель платёжного шлюза:
    PENDING ──► PROCESSING ──┬──► SETTLED
       │                     └──► FAILED
       ▼
    CANCELLED

Терминальные статусы (SETTLED, FAILED, CANCELLED) являются окончательными.
Никакие дальнейшие переходы из них не допускаются.
"""

from enum import StrEnum
from typing import Final


class PaymentStatus(StrEnum):
    """Статусы жизненного цикла платежа.

    Наследует ``StrEnum`` для сериализуемости и безопасного отображения
    в логи / gRPC / JSON без вызова ``.value``.
    """

    PENDING = 'PENDING'
    PROCESSING = 'PROCESSING'
    SETTLED = 'SETTLED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'

    @property
    def is_terminal(self) -> bool:
        """Признак завершённости жизненного цикла платежа."""
        return self in TERMINAL_PAYMENT_STATUSES

    def can_transition_to(self, target: object) -> bool:
        """Проверяет, допустим ли переход из текущего статуса в ``target``."""
        if not isinstance(target, PaymentStatus):
            return False
        return target in ALLOWED_TRANSITIONS.get(self, frozenset())


#: Множество терминальных (конечных) статусов платежа.
TERMINAL_PAYMENT_STATUSES: Final[frozenset[PaymentStatus]] = frozenset(
    {
        PaymentStatus.SETTLED,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
    }
)

#: Карта допустимых переходов конечного автомата жизненного цикла платежа.
ALLOWED_TRANSITIONS: Final[dict[PaymentStatus, frozenset[PaymentStatus]]] = {
    PaymentStatus.PENDING: frozenset(
        {
            PaymentStatus.PROCESSING,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.PROCESSING: frozenset(
        {
            PaymentStatus.SETTLED,
            PaymentStatus.FAILED,
        }
    ),
    PaymentStatus.SETTLED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
}
