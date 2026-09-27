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

from src.core_service.domain.exceptions import InvalidValueError


def _default_occurred_at() -> datetime:
    """Возвращает текущее время в UTC."""
    return datetime.now(UTC)


def _require_utc(dt: datetime, field_name: str) -> datetime:
    """Проверяет, что временная метка является timezone-aware в UTC."""
    if not isinstance(dt, datetime):
        raise InvalidValueError(f'{field_name}: ожидается datetime, получено {type(dt).__name__}')
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise InvalidValueError(f'{field_name}: datetime должен быть timezone-aware (UTC)')
    if dt.tzinfo.utcoffset(dt) != UTC.utcoffset(dt):
        raise InvalidValueError(f'{field_name}: временная зона должна быть строго UTC, получено {dt.tzinfo}')
    return dt


def _require_uuid(val: UUID, field_name: str) -> UUID:
    """Проверяет, что передан корректный UUID."""
    if not isinstance(val, UUID):
        raise InvalidValueError(f'{field_name}: ожидается UUID, получено {type(val).__name__}')
    return val


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Базовый класс всех доменных событий системы."""

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_default_occurred_at)

    def __post_init__(self) -> None:
        _require_uuid(self.event_id, 'event_id')
        _require_utc(self.occurred_at, 'occurred_at')
