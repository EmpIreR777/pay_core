"""Базовый класс для доменных событий (T-1.4).

Доменное событие (Domain Event) фиксирует факт того, что в бизнес-логике
произошло нечто значимое.

Ключевые свойства:
* **Неизменяемость:** событие описывает факт в прошлом, его нельзя модифицировать
  после создания (``frozen=True``).
* **Идентифицируемость:** каждое событие имеет уникальный ``event_id`` (UUIDv4).
* **Временная метка:** событие фиксирует точный момент происхождения ``occurred_at``
  в UTC с проверкой timezone-awareness.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.core_service.domain.validation import require_type, require_utc


def _default_occurred_at() -> datetime:
    """Возвращает текущее время в UTC."""
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Базовый класс всех доменных событий системы."""

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_default_occurred_at)

    def __post_init__(self) -> None:
        require_type(self.event_id, UUID, 'event_id')
        require_utc(self.occurred_at, 'occurred_at')
