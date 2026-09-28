"""DTO прикладного слоя (T-2.3).

* ``payment.py`` — входы и выходы сценариев оплаты
  (``CreatePaymentInput`` / ``CreatePaymentOutput`` / ``GetPaymentInput``);
* ``ProviderResult`` / ``ProviderStatus`` реэкспортируются из порта провайдера
  (``ports/payment_provider.py``): это типы, пересекающие границу порта, и их
  единственный источник правды — там. Дублировать их в DTO нельзя, иначе
  адаптеры и сценарии разойдутся по типам одного и того же ответа провайдера.
"""

from src.core_service.application.dto.payment import (
    CancelPaymentInput,
    CancelPaymentOutput,
    CreatePaymentInput,
    CreatePaymentOutput,
    GetPaymentInput,
    GetPaymentOutput,
)
from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.core_service.application.ports.payment_provider import ProviderResult, ProviderStatus
from src.core_service.domain.value_objects.payment_status import PROVIDER_BOUND_PAYMENT_STATUSES

__all__ = (
    'MAX_IDEMPOTENCY_KEY_LENGTH',
    'PROVIDER_BOUND_PAYMENT_STATUSES',
    'CancelPaymentInput',
    'CancelPaymentOutput',
    'CreatePaymentInput',
    'CreatePaymentOutput',
    'GetPaymentInput',
    'GetPaymentOutput',
    'ProviderResult',
    'ProviderStatus',
)
