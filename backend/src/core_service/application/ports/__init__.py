"""Порты инфраструктуры и внешних адаптеров (Application Layer) (T-2.1).

В соответствии с Clean Architecture и DDD, прикладной слой не зависит от
конкретных реализаций баз данных, брокеров или сторонних API. Все взаимодействия
с внешним миром происходят строго через Protocol-интерфейсы.
"""

from src.core_service.application.ports.account_repository import AccountRepository
from src.core_service.application.ports.clock import Clock
from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.idempotency_store import (
    IdempotencyRecord,
    IdempotencyStore,
)
from src.core_service.application.ports.lock_manager import DistributedLock, LockManager
from src.core_service.application.ports.payment_provider import (
    PaymentProvider,
    ProviderResult,
    ProviderStatus,
)
from src.core_service.application.ports.payment_repository import PaymentRepository
from src.core_service.application.ports.unit_of_work import UnitOfWork

__all__ = (
    'AccountRepository',
    'Clock',
    'DistributedLock',
    'EventPublisher',
    'IdempotencyRecord',
    'IdempotencyStore',
    'LockManager',
    'PaymentProvider',
    'PaymentRepository',
    'ProviderResult',
    'ProviderStatus',
    'UnitOfWork',
)
