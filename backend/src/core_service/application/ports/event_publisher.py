"""Порт публикации доменных событий (T-2.1).

Сценарии прикладного слоя отдают события этому порту, не зная, попадёт ли
событие сразу в Kafka или сначала в outbox-таблицу. Транзакционная гарантия
«событие уходит тогда и только тогда, когда зафиксировано изменение» —
ответственность реализации (ЭПИК 8), скрытая за этим контрактом.
"""

from typing import Protocol, runtime_checkable

from src.core_service.domain.events.base import DomainEvent


@runtime_checkable
class EventPublisher(Protocol):
    """Контракт публикации доменных событий."""

    async def publish(self, event: DomainEvent) -> None:
        """Ставит событие в очередь публикации в рамках текущей транзакции."""
        ...
