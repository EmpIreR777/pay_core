"""Порт репозитория платежей (T-2.1).

Абстракция доступа к хранилищу сущности ``Payment``: чтение, запись и выборки
по статусу. Нужна прикладному слою, чтобы не знать ни SQL, ни ORM.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.value_objects.identifiers import PaymentId
from src.core_service.domain.value_objects.payment_status import PaymentStatus

#: Сколько платежей отдаёт ``find_by_status``, если вызывающий не задал предел.
#: Значение живёт в контракте, а не в адаптере хранилища: предел по умолчанию —
#: часть соглашения между сценарием и базой (сверка идёт порциями), и вторая
#: копия числа в Postgres-репозитории рано или поздно разошлась бы с портом.
DEFAULT_FIND_BY_STATUS_LIMIT: Final[int] = 100


@runtime_checkable
class PaymentRepository(Protocol):
    """Контракт хранилища платежей.

    Методы не коммитят транзакцию: границей транзакции управляет ``UnitOfWork``.
    """

    async def get(self, payment_id: PaymentId) -> Payment | None:
        """Возвращает платёж по идентификатору или ``None``, если он не найден."""
        ...

    async def get_by_provider_payment_id(self, provider_payment_id: str) -> Payment | None:
        """Возвращает платёж по идентификатору во внешней системе.

        Нужен обработчику вебхуков: во входящем уведомлении есть только
        ``provider_payment_id``, сопоставить его с нашим платежом можно лишь тут.
        """
        ...

    async def add(self, payment: Payment) -> None:
        """Добавляет новый платёж."""
        ...

    async def update(self, payment: Payment) -> None:
        """Сохраняет изменения существующего платежа."""
        ...

    async def find_by_status(
        self,
        status: PaymentStatus,
        *,
        limit: int = DEFAULT_FIND_BY_STATUS_LIMIT,
        updated_before: datetime | None = None,
    ) -> Sequence[Payment]:
        """Возвращает платежи в заданном статусе, от старых к новым.

        ``updated_before`` ограничивает выборку по времени последнего изменения:
        так, например, отбираются «зависшие» платежи для сверки с провайдером.
        """
        ...
