"""Порт репозитория платёжных счетов (T-2.1).

Абстракция доступа к хранилищу сущности ``Account``: чтение, чтение под
блокировкой строки и запись. Конкретная реализация (Postgres + async SQLAlchemy)
появится в ЭПИК 3 и не должна влиять на сценарии прикладного слоя.
"""

from typing import Protocol, runtime_checkable

from src.core_service.domain.entities.account import Account
from src.core_service.domain.value_objects.identifiers import AccountId


@runtime_checkable
class AccountRepository(Protocol):
    """Контракт хранилища счетов.

    Методы не коммитят транзакцию: границей транзакции управляет ``UnitOfWork``.
    """

    async def get(self, account_id: AccountId) -> Account | None:
        """Возвращает счёт по идентификатору или ``None``, если он не найден."""
        ...

    async def get_for_update(self, account_id: AccountId) -> Account | None:
        """Возвращает счёт, блокируя его строку в БД (``SELECT ... FOR UPDATE``).

        Нужен там, где между чтением и записью баланса нельзя допустить гонку:
        блокировка удерживается до конца транзакции ``UnitOfWork``.
        """
        ...

    async def add(self, account: Account) -> None:
        """Добавляет новый счёт."""
        ...

    async def update(self, account: Account) -> None:
        """Сохраняет изменения существующего счёта."""
        ...
