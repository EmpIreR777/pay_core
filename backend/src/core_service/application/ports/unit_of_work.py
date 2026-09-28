"""Порт Unit of Work (T-2.1).

Транзакционная граница для сценариев прикладного слоя: объединяет репозитории и
управляет ``commit``/``rollback``. Сценарий не открывает транзакцию сам — он
входит в ``UnitOfWork`` и работает с её репозиториями, поэтому все изменения
(включая записи в outbox) фиксируются атомарно.
"""

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from src.core_service.application.ports.account_repository import AccountRepository
from src.core_service.application.ports.payment_repository import PaymentRepository


@runtime_checkable
class UnitOfWork(Protocol):
    """Контракт единицы работы.

    Поддерживает и явный ``commit``/``rollback``, и работу через
    ``async with``: выход из контекста без исключения фиксирует транзакцию,
    выход с исключением — откатывает.
    """

    @property
    def accounts(self) -> AccountRepository:
        """Репозиторий счетов в границах текущей транзакции."""
        ...

    @property
    def payments(self) -> PaymentRepository:
        """Репозиторий платежей в границах текущей транзакции."""
        ...

    async def __aenter__(self) -> Self:
        """Открывает транзакцию и возвращает саму единицу работы."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Закрывает транзакцию: ``commit`` без исключения, иначе ``rollback``."""
        ...

    async def commit(self) -> None:
        """Фиксирует все изменения текущей транзакции."""
        ...

    async def rollback(self) -> None:
        """Откатывает все изменения текущей транзакции."""
        ...
