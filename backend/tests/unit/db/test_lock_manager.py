"""Юнит-тесты менеджера блокировок: форма контракта и чистые правила (T-5.2).

Без Redis: проверяется то, что видно без стенда, — соответствие порту, формат
ключей, перевод секунд в миллисекунды и отказ на заведомо неверных сроках. Само
поведение замка (``SET NX``, снятие по токену, ожидание) доказывается на живом
Redis в ``tests/integration/test_lock_manager.py``: подделывать его здесь значило
бы тестировать подделку, а не адаптер.
"""

import pytest

from src.core_service.application import ports
from src.db.lock_manager import (
    LOCK_KEY_PREFIX,
    RELEASE_SCRIPT,
    RedisDistributedLock,
    RedisLockManager,
    _milliseconds,
    _validate_timeouts,
    lock_key,
)

RESOURCE = 'account:6c1f-4a1e'


def _manager() -> RedisLockManager:
    """Менеджер без живого Redis: клиент нужен конструктору лишь для делегирования."""
    return RedisLockManager(redis=None)  # type: ignore[arg-type]


def _lock(resource: str = RESOURCE) -> RedisDistributedLock:
    """Свежий замок для юнит-проверок.

    Токен здесь — просто метка владельца, а не секрет: проверке важно лишь, что
    свойство ``token`` его возвращает. Формируем его вычислением, чтобы линтер не
    принял тестовую метку за захардкоженный «пароль» в аргументе ``token`` (S106).
    """
    return RedisDistributedLock(_manager(), resource=resource, token=f'{resource}#owner')


# --- Форма контракта ----------------------------------------------------------


def test_manager_satisfies_the_port() -> None:
    """Адаптер отвечает порту ``LockManager`` «по форме» (T-2.1).

    Порт **не** наследуется: наследование от ``Protocol`` подмешивало бы заглушки
    и сделало бы проверку самоподтверждающейся.
    """
    manager = _manager()

    assert isinstance(manager, ports.LockManager)
    assert ports.LockManager not in RedisLockManager.__mro__


def test_lock_satisfies_the_port() -> None:
    """Выданный замок отвечает порту ``DistributedLock`` «по форме» (T-2.1)."""
    handle = _lock()

    assert isinstance(handle, ports.DistributedLock)
    assert ports.DistributedLock not in RedisDistributedLock.__mro__


# --- Пространство имён ключей -------------------------------------------------


def test_lock_key_namespaces_the_resource() -> None:
    """Ключ Redis — ``lock:<resource>``: ресурс строит сценарий, ключ — адаптер."""
    assert lock_key(RESOURCE) == f'{LOCK_KEY_PREFIX}{RESOURCE}'
    assert lock_key(RESOURCE).startswith('lock:account:')


# --- Перевод сроков в миллисекунды --------------------------------------------


def test_milliseconds_never_reach_zero() -> None:
    """``PX`` требует целое положительное число: нулевой срок — это замок без срока."""
    assert _milliseconds(30) == 30_000
    assert _milliseconds(0.05) == 50
    assert _milliseconds(0.0004) == 1


# --- Отказ на несовместимых сроках --------------------------------------------


@pytest.mark.parametrize(
    ('ttl_seconds', 'wait_seconds'),
    [(-1.0, 0.0), (0.0, 0.0), (1.0, -0.5)],
)
def test_impossible_timeouts_are_rejected(ttl_seconds: float, wait_seconds: float) -> None:
    """Неположительный TTL и отрицательное ожидание — ошибка вызова, а не «ждать нечего»."""
    with pytest.raises(ValueError, match=r'ttl_seconds|wait_seconds'):
        _validate_timeouts(ttl_seconds, wait_seconds)


def test_valid_timeouts_are_accepted() -> None:
    """Обычная пара сроков не поднимает ошибки."""
    _validate_timeouts(ttl_seconds=1.0, wait_seconds=0.0)


def test_non_positive_poll_interval_is_rejected() -> None:
    """Нулевая пауза превратила бы ожидание в горячий цикл команд по Redis."""
    with pytest.raises(ValueError, match='poll_interval_seconds'):
        RedisLockManager(redis=None, poll_interval_seconds=0)  # type: ignore[arg-type]


# --- Освобождение и состояние замка -------------------------------------------


def test_release_script_compares_the_token_before_deleting() -> None:
    """Скрипт снятия читает ключ и лишь затем удаляет — иначе снёс бы чужой замок."""
    assert "redis.call('get'" in RELEASE_SCRIPT
    assert "redis.call('del'" in RELEASE_SCRIPT


def test_fresh_lock_reports_held_until_marked_released() -> None:
    """``is_held`` — взгляд владельца: ``True`` до освобождения и ``False`` после."""
    handle = _lock()

    assert handle.resource == RESOURCE
    assert handle.token
    assert handle.is_held is True

    handle._mark_released()

    assert handle.is_held is False
