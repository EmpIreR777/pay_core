"""Порт хранилища идемпотентности (T-2.1).

Гарантирует, что повторный запрос с тем же ключом и тем же телом не выполнит
операцию второй раз, а вернёт ранее сохранённый ответ. Прикладной слой не знает,
где лежит запись — в Redis, Postgres или в обоих сразу (ЭПИК 4).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Сохранённый результат обработки идемпотентного запроса.

    ``request_hash`` нужен для обнаружения конфликта: если ключ тот же, а тело
    запроса другое, повтор использовать нельзя.
    """

    key: str
    request_hash: str
    response: Mapping[str, Any] | None
    created_at: datetime
    expires_at: datetime


@runtime_checkable
class IdempotencyStore(Protocol):
    """Контракт хранилища идемпотентных запросов."""

    async def get(self, key: str) -> IdempotencyRecord | None:
        """Возвращает сохранённую запись по ключу или ``None``, если её нет."""
        ...

    async def save(self, record: IdempotencyRecord) -> None:
        """Атомарно сохраняет результат обработки под ключом ``record.key``."""
        ...

    async def try_acquire(self, key: str, *, ttl_seconds: int) -> bool:
        """Пытается «застолбить» ключ (``SET NX``) и вернуть признак успеха.

        Нужен, чтобы параллельные дубли одного запроса не выполняли операцию
        одновременно: первую пропускаем к работе, остальные — нет.
        """
        ...

    async def release(self, key: str) -> None:
        """Снимает ранее установленную резервацию ключа."""
        ...
