"""Идентификаторы сущностей как типизированные Value Objects (T-1.1).

Сильная типизация на уровне системы: ``AccountId`` и ``PaymentId`` нельзя
случайно перепутать местами, хотя внутри оба — UUID. Реализовано через
наследование от общего ``Identifier``, поэтому конкретный идентификатор
достаётся одним словом, а проверка «UUID или нет» живёт в одном месте.
"""

from dataclasses import dataclass
from typing import Final, Self
from uuid import UUID, uuid4

from src.core_service.domain.exceptions import InvalidIdentifierError

#: «Нулевой» UUID (00000000-0000-0000-0000-000000000000) — не валидный
#: доменный идентификатор: он не был выдан ни одной сущности.
NIL_UUID: Final = UUID(int=0)


def _parse_uuid(raw_value: str) -> UUID:
    try:
        return UUID(raw_value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidIdentifierError(f'Некорректный UUID: {raw_value!r}') from exc


def _coerce_uuid(raw_value: object) -> UUID:
    """Приводит UUID или его строковое представление к :class:`UUID`.

    Строку принимаем на границе слоя (JSON/protobuf), но в поле домена
    всегда лежит UUID — иначе по сети поедут строки в разных форматах.
    """
    if isinstance(raw_value, UUID):
        return raw_value
    if isinstance(raw_value, str):
        return _parse_uuid(raw_value)
    raise InvalidIdentifierError(f'Идентификатор должен быть UUID, получено {type(raw_value).__name__}')


@dataclass(frozen=True, slots=True, repr=False)
class Identifier:
    """Базовый иммутабельный идентификатор на UUID4."""

    value: UUID

    def __post_init__(self) -> None:
        # Принимаем object, а не self.value: поле объявлено как UUID, и mypy
        # считает проверку isinstance(..., str) на нём недостижимой.
        value = _coerce_uuid(self.value)
        if value == NIL_UUID:
            raise InvalidIdentifierError('Нулевой UUID (00000000-...) не является валидным идентификатором')
        object.__setattr__(self, 'value', value)

    @classmethod
    def new(cls) -> Self:
        """Создаёт новый уникальный идентификатор (UUID4)."""
        return cls(value=uuid4())

    @classmethod
    def from_string(cls, raw_value: str) -> Self:
        """Разбирает идентификатор из строки (вход приложения/БД)."""
        return cls(value=_parse_uuid(raw_value))

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.value!r})'


@dataclass(frozen=True, slots=True, repr=False)
class AccountId(Identifier):
    """Идентификатор платёжного счёта."""


@dataclass(frozen=True, slots=True, repr=False)
class PaymentId(Identifier):
    """Идентификатор платежа."""
