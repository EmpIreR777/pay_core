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

from src.core_service.domain.exceptions import InvalidValueError


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

#: Статусы, в которых платёж уже связан с операцией у провайдера и потому обязан
#: нести её идентификатор. ``FAILED``/``CANCELLED`` в набор не входят: платёж мог
#: упасть до вызова провайдера или быть отменённым, не дойдя до эквайринга.
PROVIDER_BOUND_PAYMENT_STATUSES: Final[frozenset[PaymentStatus]] = frozenset(
    {
        PaymentStatus.PROCESSING,
        PaymentStatus.SETTLED,
    }
)


def require_provider_payment_coherence(status: PaymentStatus, provider_payment_id: str | None) -> None:
    """Проверяет согласованность статуса платежа и идентификатора операции у провайдера.

    Единственная реализация правила «``PROCESSING``/``SETTLED`` ⇒ идентификатор
    операции задан, ``PENDING`` ⇒ он ещё не мог появиться»: её переиспользуют DTO
    и любые будущие мапперы, вместо того чтобы повторять условие по слоям.
    Статус-машина ``Payment`` остаётся источником истины для допустимых переходов.

    :param status: статус платежа (уже проверенный как ``PaymentStatus``);
    :param provider_payment_id: нормализованный идентификатор операции у
        провайдера или ``None``;
    :raises InvalidValueError: если статус и идентификатор противоречат друг другу.
    """
    if status in PROVIDER_BOUND_PAYMENT_STATUSES and provider_payment_id is None:
        raise InvalidValueError(f'status={status}: платёж в этом статусе обязан иметь provider_payment_id')
    if status is PaymentStatus.PENDING and provider_payment_id is not None:
        raise InvalidValueError('status=PENDING: до вызова провайдера provider_payment_id должен быть пустым')
