"""Порт системных часов (T-2.1).

Абстрагирует получение текущего системного времени, позволяя в тестах
детерминированно управлять временем (заморозка, сдвиг вперед для проверки TTL).
"""

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Порт получения текущего времени."""

    def now(self) -> datetime:
        """Возвращает текущую временную метку (timezone-aware в UTC)."""
        ...
