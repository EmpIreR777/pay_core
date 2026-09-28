"""Сценарии прикладного слоя (Application Layer).

Сценарий — единственное место, где собирается последовательность шагов
бизнес-операции: он держит порядок вызовов портов (блокировка → транзакция →
внешний вызов → транзакция), а сами правила оставляет домену.

* ``create_payment.py`` — сага создания платежа (T-2.4)
* ``get_payment.py`` — чтение платежа с актуализацией у провайдера (T-2.5)
* ``cancel_payment.py`` — отмена PENDING платежа с возвратом холда (T-2.6)
* ``handle_provider_webhook.py`` — обработка уведомления провайдера с
  идемпотентностью по ``provider_event_id`` (T-2.7)
* ``payment_sync.py`` — общее правило «ответ провайдера → статус платежа»,
  которым пользуются T-2.4, T-2.5 и вебхук (T-2.7)
"""

from src.core_service.application.use_cases.cancel_payment import CancelPaymentUseCase
from src.core_service.application.use_cases.create_payment import CreatePaymentUseCase
from src.core_service.application.use_cases.get_payment import GetPaymentUseCase
from src.core_service.application.use_cases.handle_provider_webhook import HandleProviderWebhookUseCase

__all__ = ('CancelPaymentUseCase', 'CreatePaymentUseCase', 'GetPaymentUseCase', 'HandleProviderWebhookUseCase')
