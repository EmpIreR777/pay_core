"""Порт внешнего платёжного провайдера (T-2.1, контракт уточняется в T-2.2).

Прикладной слой описывает лишь те операции эквайринга, которые нужны сценариям:
создание платежа, запрос статуса и возврат. Детали транспорта (HTTP, ретраи,
разбор ответа) остаются в инфраструктурном адаптере.

``ProviderResult`` и ``ProviderStatus`` — типы, пересекающие границу порта.
Полноценный DTO-пакет (входы/выходы сценариев) формируется в T-2.3, здесь же
лежит минимальный контракт ответа провайдера.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from src.core_service.domain.exceptions import InvalidValueError
from src.core_service.domain.value_objects.identifiers import PaymentId
from src.core_service.domain.value_objects.money import Money


class ProviderStatus(StrEnum):
    """Статус платежа на стороне внешнего провайдера.

    Это отдельная от доменного ``PaymentStatus`` шкала: провайдер вправе иметь
    свои значения, а перевод к доменным статусам — забота адаптера.
    """

    PENDING = 'PENDING'
    PROCESSING = 'PROCESSING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    REFUNDED = 'REFUNDED'


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Ответ провайдера на создание платежа или возврат.

    ``provider_payment_id`` — идентификатор операции во внешней системе; именно
    его прикладной слой сохраняет в платеже для последующих сверок и вебхуков.
    """

    provider_payment_id: str
    status: ProviderStatus

    def __post_init__(self) -> None:
        if not isinstance(self.provider_payment_id, str) or not self.provider_payment_id.strip():
            raise InvalidValueError('provider_payment_id должен быть непустой строкой')
        if not isinstance(self.status, ProviderStatus):
            raise InvalidValueError(f'status должен быть ProviderStatus, получено {type(self.status).__name__}')


@runtime_checkable
class PaymentProvider(Protocol):
    """Контракт взаимодействия с внешним платёжным провайдером."""

    async def create_payment(
        self,
        *,
        payment_id: PaymentId,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResult:
        """Создаёт платёж во внешней системе.

        ``payment_id`` — наш идентификатор, ``idempotency_key`` — ключ повторов:
        он защищает от двойного списания при сетевом таймауте и retry.
        """
        ...

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        """Запрашивает актуальный статус платежа у провайдера."""
        ...

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        """Инициирует возврат (полный или частичный) по проведённому платежу."""
        ...
