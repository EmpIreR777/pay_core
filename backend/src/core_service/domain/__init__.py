"""Доменный слой Core Service (T-1.1).

Чистый Python без внешних зависимостей: ни SQLAlchemy, ни gRPC, ни FastAPI,
ни Pydantic. Здесь живут инварианты финансовой логики; знание о том, где
сущность хранится и как уходит наружу, — в application и infrastructure.
"""

from src.core_service.domain.exceptions import (
    CurrencyMismatchError,
    DomainError,
    InvalidAmountError,
    InvalidCurrencyError,
    InvalidIdentifierError,
    InvalidValueError,
    NegativeAmountError,
)
from src.core_service.domain.value_objects import (
    AccountId,
    Currency,
    Identifier,
    Money,
    PaymentId,
)

__all__ = (
    'AccountId',
    'Currency',
    'CurrencyMismatchError',
    'DomainError',
    'Identifier',
    'InvalidAmountError',
    'InvalidCurrencyError',
    'InvalidIdentifierError',
    'InvalidValueError',
    'Money',
    'NegativeAmountError',
    'PaymentId',
)
