"""Сценарии прикладного слоя (Application Layer).

Сценарий — единственное место, где собирается последовательность шагов
бизнес-операции: он держит порядок вызовов портов (блокировка → транзакция →
внешний вызов → транзакция), а сами правила оставляет домену.

* ``create_payment.py`` — сага создания платежа (T-2.4)
"""

from src.core_service.application.use_cases.create_payment import CreatePaymentUseCase

__all__ = ('CreatePaymentUseCase',)
