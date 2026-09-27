"""Доменный слой Core Service (T-1.1, T-1.2, T-1.3, T-1.4).

Чистый Python без внешних зависимостей: ни SQLAlchemy, ни gRPC, ни FastAPI,
ни Pydantic. Здесь живут инварианты финансовой логики; знание о том, где
сущность хранится и как уходит наружу, — в application и infrastructure.
"""

from src.core_service.domain.entities import Account, Payment
from src.core_service.domain.events import (
    DomainEvent,
    PaymentCreated,
    PaymentFailed,
    PaymentRefunded,
    PaymentSettled,
)
from src.core_service.domain.exceptions import (
    AccountBlocked,
    CurrencyMismatchError,
    DomainError,
    DuplicateOperation,
    InsufficientFunds,
    InvalidAmountError,
    InvalidCurrencyError,
    InvalidIdentifierError,
    InvalidTransition,
    InvalidValueError,
    NegativeAmountError,
    PaymentProviderError,
)
from src.core_service.domain.value_objects import (
    ALLOWED_TRANSITIONS,
    TERMINAL_PAYMENT_STATUSES,
    AccountId,
    Currency,
    Identifier,
    Money,
    PaymentId,
    PaymentStatus,
)

__all__ = (
    'ALLOWED_TRANSITIONS',
    'TERMINAL_PAYMENT_STATUSES',
    'Account',
    'AccountBlocked',
    'AccountId',
    'Currency',
    'CurrencyMismatchError',
    'DomainError',
    'DomainEvent',
    'DuplicateOperation',
    'Identifier',
    'InsufficientFunds',
    'InvalidAmountError',
    'InvalidCurrencyError',
    'InvalidIdentifierError',
    'InvalidTransition',
    'InvalidValueError',
    'Money',
    'NegativeAmountError',
    'Payment',
    'PaymentCreated',
    'PaymentFailed',
    'PaymentId',
    'PaymentProviderError',
    'PaymentRefunded',
    'PaymentSettled',
    'PaymentStatus',
)
