"""Менеджер распределённых блокировок на Redis (T-5.2).

Адаптер порта :class:`LockManager`: защищает ресурс (в сценариях — счёт
плательщика) от одновременного изменения двумя операциями. Замок нужен не «для
порядка»: две корутины, читающие один баланс и пишущие поверх, теряют одно из
движений денег, а строка БД спасает только внутри одной транзакции — сага
выходит за её границы (внешний вызов провайдера стоит между двумя транзакциями).

Из чего собран замок:

* **Атомарный захват** — ``SET key token NX PX ttl``. ``NX`` гарантирует, что
  ключ займёт ровно один претендент: разнесённые ``GET`` и ``SET`` оставляли бы
  окно, в которое пролез бы второй. ``PX`` задаёт срок: если процесс умрёт,
  удерживая замок, ключ истечёт, и ресурс не останется недоступным навсегда.
* **Освобождение только своим токеном** — Lua-скрипт сравнивает хранимое
  значение с токеном владельца и удаляет ключ лишь при совпадении. Пара
  ``GET``+``DEL`` сделала бы то же двумя командами, но между ними TTL мог истечь
  и замок мог забрать другой — и ``DEL`` снёс бы **чужую** блокировку.
* **Ожидание (``wait_seconds``)** — необязательный опрос с паузой: сценарий
  может подождать освобождения ресурса и лишь потом отступить. Ожидания по
  умолчанию нет (``DEFAULT_LOCK_WAIT_SECONDS``): держать операцию в очереди —
  осознанное решение вызывающего, а не поведение по умолчанию.
* **Продление срока (watchdog)** — пока операция выполняется под ``lock``, фоновая
  задача периодически продлевает TTL (``RENEW_SCRIPT``, тоже со сверкой токена).
  Без этого долгая операция «пережила» бы свой TTL: замок истёк бы прямо посреди
  работы, и параллельная операция вошла бы в тот же ресурс. Продление живёт ровно
  столько, сколько тело ``async with``, и останавливается на выходе; если процесс
  умрёт, watchdog умрёт вместе с ним и замок освободится по TTL.

Решения, которые стоит проговорить:

* **Недоступный Redis роняет захват, но не освобождение.** Захватить замок при
  недоступном Redis нельзя, и подменять отказ на ``None`` тоже нельзя: сценарий
  принял бы «недоступно» за «занято». Освобождение же не поднимает ошибку — оно
  вызывается в ``finally``, и поднятое там исключение замаскировало бы настоящий
  исход операции. Замок истечёт по TTL — цена ограничена и безопасна: ресурс на
  время окажется пере-защищён (лишние операции отобьются), а не наоборот.
* **``acquire`` различает «занято» и «недоступно».** ``None`` означает ровно
  «ресурс держит кто-то другой»; отказ инфраструктуры поднимается как
  ``RedisError``. Свести оба случая к ``None`` значило бы врать вызывающему.
* **``is_held`` — взгляд владельца, а не запрос в Redis.** Свойство синхронное,
  а проверка «чей это ключ» требует сети. Оно честно отвечает на вопрос
  «снимал ли я замок сам»: ``True`` до ``release`` и ``False`` после. Истечение
  TTL его не переключает — узнать об этом можно только новой попыткой захвата.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from types import TracebackType
from typing import Final, Self
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core_service.application.ports.lock_manager import (
    DEFAULT_LOCK_TTL_SECONDS,
    DEFAULT_LOCK_WAIT_SECONDS,
    DistributedLock,
)
from src.core_service.domain.exceptions import LockAcquisitionError

#: Пространство имён ключей блокировок: полный ключ — ``lock:<resource>``.
#: Ресурс строит сценарий (``account:<uuid>``), а Redis-ключ — уже забота
#: адаптера: прикладной слой не знает, где лежит замок, и знать не должен.
LOCK_KEY_PREFIX: Final = 'lock:'

#: Пауза между попытками, когда сценарий согласился подождать (``wait_seconds``).
#: Малá настолько, чтобы не тормозить операцию, и достаточна, чтобы ожидающая
#: корутина не крутила команды Redis в горячем цикле без передышки.
DEFAULT_LOCK_POLL_INTERVAL_SECONDS: Final = 0.05

#: Снятие замка только своим токеном, одной атомарной операцией (см. модульный
#: докстринг). Возвращает ``1``, если ключ был наш и удалён, и ``0`` во всех
#: остальных случаях — просрочен, перехвачен или уже снят.
RELEASE_SCRIPT: Final = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

#: Продление срока замка тем же токеном, одной атомарной операцией. Как и снятие,
#: сверяет владельца: продлить (то есть «воскресить») чужой замок нельзя — иначе
#: истёкший замок вернулся бы к жизни уже после того, как ресурс занял новый
#: владелец.
RENEW_SCRIPT: Final = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

#: Во сколько раз пауза между продлениями короче TTL (``ttl / LOCK_RENEWAL_DIVISOR``).
#: Продлеваем заметно чаще, чем истекает срок: одна задержка планировщика не должна
#: оставить операцию без защиты.
LOCK_RENEWAL_DIVISOR: Final = 3


def lock_key(resource: str) -> str:
    """Ключ Redis, под которым живёт замок ресурса."""
    return f'{LOCK_KEY_PREFIX}{resource}'


def _milliseconds(seconds: float) -> int:
    """Перевести секунды в целые миллисекунды для ``PX``.

    Redis требует целое положительное число: ``PX 0`` он отвергает, а замок без
    срока — это «вечная» блокировка, от которой мы как раз уходим. Поэтому
    нижняя граница — одна миллисекунда.
    """
    return max(1, int(seconds * 1000))


def _validate_timeouts(ttl_seconds: float, wait_seconds: float) -> None:
    """Отвергнуть несовместимые сроки до обращения к Redis.

    Неположительный TTL не защитил бы ресурс, а отрицательное ожидание — это
    ошибка вызова, а не «ждать нечего». Проверяем здесь, чтобы не отправить
    Redis заведомо неверную команду и получить невнятный ответ сервера.
    """
    if ttl_seconds <= 0:
        raise ValueError('ttl_seconds: ожидается положительное число секунд')
    if wait_seconds < 0:
        raise ValueError('wait_seconds: ожидается неотрицательное число секунд')


class RedisDistributedLock:
    """Замок, выданный :class:`RedisLockManager`.

    Отвечает порту ``DistributedLock``: несёт ресурс и токен владельца и умеет
    сниматься — вручную (``release``) и автоматически (``async with``).
    """

    __slots__ = ('_held', '_manager', '_resource', '_token', '_ttl_ms', '_watchdog')

    def __init__(self, manager: RedisLockManager, *, resource: str, token: str, ttl_ms: int) -> None:
        self._manager = manager
        self._resource = resource
        self._token = token
        self._ttl_ms = ttl_ms
        self._watchdog: asyncio.Task[None] | None = None
        self._held = True

    @property
    def resource(self) -> str:
        """Имя защищаемого ресурса (``account:<uuid>``)."""
        return self._resource

    @property
    def token(self) -> str:
        """Уникальный токен владельца: по нему Lua отличает нашу блокировку от чужой."""
        return self._token

    @property
    def is_held(self) -> bool:
        """Удерживает ли замок его владелец (см. модульный докстринг)."""
        return self._held

    async def release(self) -> None:
        """Снять замок; повторный вызов — не ошибка (T-2.1)."""
        if not self._held:
            return
        await self._manager.release(self)

    async def __aenter__(self) -> Self:
        """Вернуть сам замок: захват уже произошёл в ``LockManager``."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Снять замок на выходе из ``async with``, даже если тело упало."""
        await self.release()

    def _start_renewal(self) -> None:
        """Запустить фоновое продление срока на время операции (T-5.3).

        Заводит его менеджер из ``lock``: контекстный менеджер и есть «операция»,
        поэтому watchdog живёт ровно столько, сколько тело ``async with``.
        Низкоуровневый ``acquire`` продления не заводит — он остаётся замком с
        явным сроком, который истечёт сам, если его не снять.
        """
        self._watchdog = asyncio.create_task(self._renew_forever())

    async def _renew_forever(self) -> None:
        """Продлевать срок, пока замок удерживается и остаётся нашим."""
        interval = self._ttl_ms / 1000 / LOCK_RENEWAL_DIVISOR
        while True:
            await asyncio.sleep(interval)
            if not self._held:
                return
            try:
                extended = await self._manager._renew(
                    resource=self._resource,
                    token=self._token,
                    ttl_ms=self._ttl_ms,
                )
            except RedisError:
                # Redis моргнул: срок ещё не вышел — пробуем следующим тактом.
                continue
            if not extended:
                # Замок больше не наш (истёк и перехвачен) — продлевать нечего.
                return

    async def _stop_renewal(self) -> None:
        """Остановить продление и перевести замок в состояние «снят».

        Отменённую таску **дожидаемся** (``await`` после ``cancel``): иначе она
        осталась бы висеть до следующего такта, а на выходе из процесса сыпалось
        бы «Task was destroyed but it is pending». Через этот метод проходят оба
        пути освобождения — ``await handle.release()`` и
        ``await manager.release(handle)``, — поэтому ``is_held`` не расходится с
        действительностью.
        """
        self._held = False
        task = self._watchdog
        self._watchdog = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class RedisLockManager:
    """Реализация порта ``LockManager`` на Redis (T-5.2, T-5.3).

    Владельцем соединения не является: клиент Redis общий на процесс (как и у
    хранилища идемпотентности), а адаптер лишь пользуется им.
    """

    __slots__ = ('_poll_interval', '_redis')

    def __init__(
        self,
        *,
        redis: Redis,
        poll_interval_seconds: float = DEFAULT_LOCK_POLL_INTERVAL_SECONDS,
    ) -> None:
        """:param redis: клиент Redis;

        :param poll_interval_seconds: пауза между попытками ожидания.
        """
        if poll_interval_seconds <= 0:
            raise ValueError('poll_interval_seconds: ожидается положительное число секунд')
        self._redis = redis
        self._poll_interval = poll_interval_seconds

    def lock(
        self,
        resource: str,
        *,
        ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> AbstractAsyncContextManager[DistributedLock]:
        """Контекстный менеджер: захват на входе, снятие на выходе (T-2.1).

        :raises LockAcquisitionError: ресурс занят и ожидание (``wait_seconds``)
            истекло — замок не захвачен, снимать нечего.
        """
        return self._context(resource, ttl_seconds=ttl_seconds, wait_seconds=wait_seconds)

    @asynccontextmanager
    async def _context(
        self,
        resource: str,
        *,
        ttl_seconds: float,
        wait_seconds: float,
    ) -> AsyncGenerator[DistributedLock]:
        """Тело ``lock``: захват, продление на время операции и снятие на выходе.

        Пока тело ``async with`` выполняется, фоновый watchdog (T-5.3) продлевает
        срок замка, поэтому длинная операция не теряет его на середине. На выходе —
        в том числе при исключении — продление останавливается, а замок снимается.
        """
        handle = await self.acquire(resource, ttl_seconds=ttl_seconds, wait_seconds=wait_seconds)
        if handle is None:
            raise LockAcquisitionError(f'Ресурс {resource} занят: ожидание {wait_seconds} с истекло')
        handle._start_renewal()
        try:
            yield handle
        finally:
            await handle.release()

    async def acquire(
        self,
        resource: str,
        *,
        ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> RedisDistributedLock | None:
        """Захватить замок: ``SET NX PX`` и, если нужно, ожидание освобождения.

        Продления здесь нет: ``acquire`` — низкоуровневый путь с явным сроком, и
        ему неизвестно, где закончится «операция» вызывающего. Watchdog заводит
        высокоуровневый ``lock`` (T-5.3); замок, взятый ``acquire``, истечёт по TTL,
        если его не снять.

        Возвращается конкретный ``RedisDistributedLock``, а не порт
        ``DistributedLock``: сузить тип возврата — законно (ковариантность), а
        ``lock`` нужен доступ к запуску watchdog без приведения типов. Вызывающим
        через порт это всё тот же ``DistributedLock``.

        :returns: замок с уникальным токеном либо ``None``, если ресурс держит
            кто-то другой и ожидание истекло (``wait_seconds == 0`` — одна
            попытка без ожидания);
        :raises RedisError: Redis недоступен — координировать нельзя, и это не
            «занято»: подменять отказ на ``None`` значило бы соврать.
        """
        _validate_timeouts(ttl_seconds, wait_seconds)
        token = uuid4().hex
        key = lock_key(resource)
        ttl_ms = _milliseconds(ttl_seconds)
        deadline = time.monotonic() + wait_seconds
        while True:
            if await self._redis.set(key, token, nx=True, px=ttl_ms):
                return RedisDistributedLock(self, resource=resource, token=token, ttl_ms=ttl_ms)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(self._poll_interval, remaining))

    async def release(self, handle: DistributedLock) -> None:
        """Снять замок, если он всё ещё наш (Lua-скрипт).

        Ошибка Redis подавляется осознанно: снятие зовётся в ``finally``, и
        поднятое здесь исключение замаскировало бы настоящий исход операции, а
        замок и так истечёт по TTL — пере-защита безопаснее потери координации.
        """
        if isinstance(handle, RedisDistributedLock):
            await handle._stop_renewal()
        try:
            await self._redis.eval(RELEASE_SCRIPT, 1, lock_key(handle.resource), handle.token)
        except RedisError:
            return

    async def _renew(self, *, resource: str, token: str, ttl_ms: int) -> bool:
        """Продлить срок замка тем же токеном; ``False`` — замок уже не наш.

        Сверка токена обязательна (Lua): «воскресить» чужой замок нельзя — иначе
        истёкший замок вернулся бы к жизни уже после того, как ресурс занял новый
        владелец, и взаимное исключение сломалось бы в обратную сторону.
        """
        extended = await self._redis.eval(RENEW_SCRIPT, 1, lock_key(resource), token, ttl_ms)
        return bool(extended)
