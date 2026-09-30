"""Менеджер блокировок на живом Redis (T-5.2).

DoD задачи — «второй конкурентный acquire завершается неудачей или ждёт
таймаут». Юнит-тесты доказывают форму и чистые правила; здесь проверяется то,
ради чего адаптер существует, — **взаимное исключение**: пока ресурс держит один
владелец, второй не пройдёт — ни сразу, ни дождавшись окна, если ресурс так и не
освободился. Отдельно проверяются две тонкие вещи: снятие по токену (чужой замок
не трогаем) и различие «занято» / «Redis недоступен».

Стенд не разрушается: ключи чистятся фикстурой ``redis_client``. Без Redis тесты
берут временный testcontainer, а без Docker пропускаются: ``make test`` обязан
оставаться зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
import time

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core_service.domain.exceptions import LockAcquisitionError
from src.db.lock_manager import RedisLockManager, lock_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures('redis_stack'),
]

RESOURCE = 'account:6c1f-4a1e'

#: Адрес, на котором никто не слушает: порт 1 зарезервирован и закрыт. Нужен,
#: чтобы поднять **настоящий** отказ Redis, а не его имитацию.
DEAD_REDIS_URL = 'redis://127.0.0.1:1/0'

#: Сколько ждать освобождения, прежде чем отступить.
WAIT_SECONDS = 0.2

#: Короткий срок жизни замка — для проверки истечения по TTL.
SHORT_TTL_SECONDS = 0.05


@pytest.fixture
def manager(redis_client: Redis) -> RedisLockManager:
    """Адаптер на живом Redis, общем для теста."""
    return RedisLockManager(redis=redis_client)


def _dead_client() -> Redis:
    """Клиент заведомо недоступного Redis: реальный отказ, а не имитация."""
    return Redis.from_url(DEAD_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)


async def _stored_token(redis_client: Redis, resource: str) -> str | None:
    """Токен владельца из Redis или ``None``, если ресурс свободен."""
    raw = await redis_client.get(lock_key(resource))
    return None if raw is None else raw.decode()


async def _wait_until_free(redis_client: Redis, resource: str, *, timeout: float = 2.0) -> None:
    """Дождаться истечения замка по TTL.

    Явное ожидание условия с потолком вместо ``sleep`` на угаданный срок: без
    потолка тест, в котором TTL не сработал, висел бы вместо честного падения.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await redis_client.exists(lock_key(resource)) == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError('замок не истёк по TTL за отведённое время')


# --- Взаимное исключение ------------------------------------------------------


async def test_acquire_takes_the_resource_and_release_frees_it(
    manager: RedisLockManager,
    redis_client: Redis,
) -> None:
    """Захват занимает ресурс, снятие возвращает его и переключает ``is_held``."""
    handle = await manager.acquire(RESOURCE)

    assert handle is not None
    assert handle.resource == RESOURCE
    assert handle.is_held is True
    assert await _stored_token(redis_client, RESOURCE) == handle.token

    await handle.release()

    assert handle.is_held is False
    assert await _stored_token(redis_client, RESOURCE) is None


async def test_second_concurrent_acquire_fails_without_waiting(
    manager: RedisLockManager,
    redis_client: Redis,
) -> None:
    """DoD: пока ресурс занят, второй захват не проходит (ожидание по умолчанию — ноль)."""
    first = await manager.acquire(RESOURCE)
    assert first is not None

    second = await manager.acquire(RESOURCE)

    assert second is None
    # Владелец не подменён: второй не «украл» замок у первого.
    assert await _stored_token(redis_client, RESOURCE) == first.token


async def test_waiting_acquire_times_out_while_the_resource_stays_busy(manager: RedisLockManager) -> None:
    """DoD: если ресурс так и не освободился, ожидание завершается неудачей по таймауту."""
    first = await manager.acquire(RESOURCE)
    assert first is not None

    started = time.monotonic()
    second = await manager.acquire(RESOURCE, wait_seconds=WAIT_SECONDS)
    elapsed = time.monotonic() - started

    assert second is None
    # Ждал именно таймаут, а не «сдался сразу»: без ожидания вызов вернулся бы
    # за доли миллисекунды.
    assert elapsed >= WAIT_SECONDS / 2


async def test_waiting_acquire_takes_over_when_the_lock_is_freed(manager: RedisLockManager) -> None:
    """Ожидание опрашивает ресурс и захватывает его, как только владелец отпустит."""
    first = await manager.acquire(RESOURCE)
    assert first is not None

    async def release_soon() -> None:
        await asyncio.sleep(WAIT_SECONDS / 2)
        await first.release()

    releaser = asyncio.create_task(release_soon())
    try:
        second = await manager.acquire(RESOURCE, wait_seconds=2.0)
    finally:
        await releaser

    assert second is not None
    assert second.token != first.token
    await second.release()


# --- Снятие только своим токеном ----------------------------------------------


async def test_release_does_not_delete_a_lock_taken_by_someone_else(
    manager: RedisLockManager,
    redis_client: Redis,
) -> None:
    """Lua сверяет токен: перехваченный (или истёкший) замок чужой рукой не снять."""
    handle = await manager.acquire(RESOURCE)
    assert handle is not None
    await redis_client.set(lock_key(RESOURCE), 'someone-else')

    await manager.release(handle)

    assert await _stored_token(redis_client, RESOURCE) == 'someone-else'


async def test_release_is_idempotent(manager: RedisLockManager, redis_client: Redis) -> None:
    """Повторное снятие — не ошибка и не трогает освобождённый ресурс."""
    handle = await manager.acquire(RESOURCE)
    assert handle is not None

    await handle.release()
    await handle.release()

    assert await _stored_token(redis_client, RESOURCE) is None


# --- Контекстный менеджер -----------------------------------------------------


async def test_context_manager_releases_the_lock_after_a_failure(
    manager: RedisLockManager,
    redis_client: Redis,
) -> None:
    """Снятие обязано сработать и при исключении в теле — иначе замок повиснет."""
    with pytest.raises(RuntimeError):
        async with manager.lock(RESOURCE):
            raise RuntimeError('сбой сценария')

    assert await _stored_token(redis_client, RESOURCE) is None


async def test_context_manager_reports_a_busy_resource_as_domain_error(manager: RedisLockManager) -> None:
    """Занятый ресурс — доменная ошибка, а не тихое исполнение без блокировки."""
    first = await manager.acquire(RESOURCE)
    assert first is not None

    with pytest.raises(LockAcquisitionError):
        async with manager.lock(RESOURCE):
            pytest.fail('замок занят — тело контекста не должно исполниться')


# --- Истечение по TTL ---------------------------------------------------------


async def test_lock_expires_by_ttl_and_the_resource_becomes_available(
    manager: RedisLockManager,
    redis_client: Redis,
) -> None:
    """Умерший владелец не держит ресурс вечно: замок истекает и освобождается."""
    handle = await manager.acquire(RESOURCE, ttl_seconds=SHORT_TTL_SECONDS)
    assert handle is not None

    await _wait_until_free(redis_client, RESOURCE)

    second = await manager.acquire(RESOURCE)
    assert second is not None
    await second.release()


# --- Отказ инфраструктуры ≠ «занято» ------------------------------------------


async def test_unreachable_redis_fails_the_acquire_instead_of_pretending_busy() -> None:
    """Недоступный Redis — это не «занято»: подмена отказом скрыла бы поломку."""
    dead = _dead_client()
    try:
        manager = RedisLockManager(redis=dead)
        with pytest.raises(RedisError):
            await manager.acquire(RESOURCE)
    finally:
        await dead.aclose()


async def test_release_survives_an_unreachable_redis(manager: RedisLockManager) -> None:
    """Снятие в ``finally`` не поднимает ошибку, иначе замаскировало бы исход операции."""
    handle = await manager.acquire(RESOURCE)
    assert handle is not None
    dead = _dead_client()
    try:
        await RedisLockManager(redis=dead).release(handle)
    finally:
        await dead.aclose()

    assert handle.is_held is False
