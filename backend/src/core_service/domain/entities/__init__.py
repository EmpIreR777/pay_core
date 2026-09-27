"""Сущности доменного слоя (T-1.2, T-1.3)."""

from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment

__all__ = (
    'Account',
    'Payment',
)
