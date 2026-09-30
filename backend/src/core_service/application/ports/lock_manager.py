"""Порт менеджера распределённых блокировок (T-2.1).

Прикладной слой описывает, *что* блокировать (ресурс) и *как долго*,
но не знает, что за этим стоит Redis: конкретная реализация появится в ЭПИК 5.
"""

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Final, Protocol, Self, runtime_checkable

#: TTL блокировки по умолчанию: защита от «вечной» блокировки упавшим процессом.
DEFAULT_LOCK_TTL_SECONDS: float = 30.0

#: Ожидание освобождения по умолчанию: 0 — не ждать, сразу вернуть неудачу.
DEFAULT_LOCK_WAIT_SECONDS: float = 0.0

#: Префикс имени блокируемого ресурса «счёт плательщика» (``account:<uuid>``).
#: Формат задан здесь, а не в сценарии: по нему строятся и блокировки сценариев
#: (T-2.4), и ключи Redis в адаптере (ЭПИК 5) — второй источник правды разошёлся
#: бы с первым при первом же переименовании.
ACCOUNT_LOCK_RESOURCE_PREFIX: Final = 'account:'


@runtime_checkable
class DistributedLock(Protocol):
    """Уже захваченная блокировка: ей можно управлять и как контекстным менеджером.

    Именно этот объект, а не сам ``LockManager``, ловит выход из ``async with``
    и снимает блокировку, — иначе освобождать её пришлось бы вручную.
    """

    @property
    def resource(self) -> str:
        """Имя защищаемого ресурса (например, ``account:<uuid>``)."""
        ...

    @property
    def token(self) -> str:
        """Уникальный токен владельца: нужен для безопасного снятия блокировки."""
        ...

    @property
    def is_held(self) -> bool:
        """Удерживается ли блокировка прямо сейчас."""
        ...

    async def release(self) -> None:
        """Освобождает блокировку; повторный вызов не является ошибкой."""
        ...

    async def __aenter__(self) -> Self:
        """Возвращает саму блокировку: захват уже произошёл в ``LockManager``."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Гарантированно снимает блокировку при выходе из ``async with``."""
        ...


@runtime_checkable
class LockManager(Protocol):
    """Контракт менеджера блокировок.

    Сценарий может работать двумя способами: высокоуровневым ``lock`` (контекстный
    менеджер, сам освобождает блокировку) или парой низкоуровневых
    ``acquire``/``release``, когда нужен ручной контроль.
    """

    def lock(
        self,
        resource: str,
        *,
        ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> AbstractAsyncContextManager[DistributedLock]:
        """Возвращает асинхронный контекстный менеджер, захватывающий ``resource``.

        Блокировка снимается при выходе из ``async with``; если захватить не
        удалось — на входе поднимается доменная ошибка
        ``LockAcquisitionError``. Класс объявлен один раз в домене, а не в
        каждом адаптере: имя ошибки — часть контракта порта, и второй источник
        правды разошёлся бы с первым при первой же правке.
        """
        ...

    async def acquire(
        self,
        resource: str,
        *,
        ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> DistributedLock | None:
        """Пытается захватить блокировку и возвращает её, либо ``None`` при неудаче."""
        ...

    async def release(self, handle: DistributedLock) -> None:
        """Снимает блокировку, переданную ранее из ``acquire``."""
        ...
