"""Тесты портов прикладного слоя (T-2.1).

Проверяем не поведение (реализаций ещё нет), а сам контракт портов:
что это Protocol-интерфейсы, что наборы методов соответствуют сценариям
ЭПИКА 2 и что асинхронные операции объявлены именно асинхронными.
"""

import inspect
from uuid import uuid4

import pytest

from src.core_service.application import ports
from src.core_service.domain.exceptions import DomainError, LockAcquisitionError

#: Все порты, которые обязан предоставить прикладной слой по T-2.1.
PROTOCOL_PORTS: tuple[type, ...] = (
    ports.AccountRepository,
    ports.PaymentRepository,
    ports.UnitOfWork,
    ports.LockManager,
    ports.DistributedLock,
    ports.IdempotencyStore,
    ports.EventPublisher,
    ports.PaymentProvider,
    ports.Clock,
)

#: Ожидаемый набор асинхронных методов у каждого порта.
ASYNC_METHODS: dict[type, tuple[str, ...]] = {
    ports.AccountRepository: ('get', 'get_for_update', 'add', 'update'),
    ports.PaymentRepository: ('get', 'get_by_provider_payment_id', 'add', 'update', 'find_by_status'),
    ports.UnitOfWork: ('__aenter__', '__aexit__', 'commit', 'rollback'),
    ports.DistributedLock: ('release', '__aenter__', '__aexit__'),
    ports.LockManager: ('acquire', 'release'),
    ports.IdempotencyStore: ('get', 'save', 'try_acquire', 'release'),
    ports.EventPublisher: ('publish',),
    ports.PaymentProvider: ('create_payment', 'get_status', 'refund'),
}

#: Синхронные методы, которые обязаны остаться синхронными.
SYNC_METHODS: dict[type, tuple[str, ...]] = {
    ports.LockManager: ('lock',),
    ports.Clock: ('now',),
}

#: Свойства (property) у портов-протоколов.
PROPERTIES: dict[type, tuple[str, ...]] = {
    ports.UnitOfWork: ('accounts', 'payments'),
    ports.DistributedLock: ('resource', 'token', 'is_held'),
}


@pytest.mark.parametrize('port', PROTOCOL_PORTS)
def test_port_is_protocol(port: type) -> None:
    """Порт обязан быть именно Protocol, а не абстрактным базовым классом."""
    assert getattr(port, '_is_protocol', False) is True


@pytest.mark.parametrize('port', PROTOCOL_PORTS)
def test_port_is_runtime_checkable(port: type) -> None:
    """runtime_checkable нужен, чтобы адаптеры можно было проверять через isinstance."""
    assert getattr(port, '_is_runtime_protocol', False) is True


@pytest.mark.parametrize(
    ('port', 'method_name'),
    [(port, name) for port, names in ASYNC_METHODS.items() for name in names],
)
def test_async_methods_are_coroutine_functions(port: type, method_name: str) -> None:
    """Асинхронные операции должны быть объявлены через ``async def``."""
    method = getattr(port, method_name, None)
    assert method is not None, f'{port.__name__}.{method_name} отсутствует'
    assert inspect.iscoroutinefunction(method), f'{port.__name__}.{method_name} должен быть async'


@pytest.mark.parametrize(
    ('port', 'method_name'),
    [(port, name) for port, names in SYNC_METHODS.items() for name in names],
)
def test_sync_methods_are_plain_functions(port: type, method_name: str) -> None:
    """Эти методы не должны быть корутинами: их вызов не требует await."""
    method = getattr(port, method_name, None)
    assert method is not None, f'{port.__name__}.{method_name} отсутствует'
    assert not inspect.iscoroutinefunction(method)


@pytest.mark.parametrize(
    ('port', 'property_name'),
    [(port, name) for port, names in PROPERTIES.items() for name in names],
)
def test_port_properties(port: type, property_name: str) -> None:
    """Доступ к репозиториям и состоянию блокировки — через property."""
    assert isinstance(inspect.getattr_static(port, property_name), property)


def test_lock_returns_async_context_manager_factory() -> None:
    """``LockManager.lock`` — синхронная фабрика асинхронного контекстного менеджера."""
    assert not inspect.iscoroutinefunction(ports.LockManager.lock)


# --- Контракт распределённых блокировок (T-5.1) -------------------------------


def test_lock_signature_defaults_match_port_constants() -> None:
    """Дефолты ``lock``/``acquire`` берутся из констант порта, а не из литералов.

    Две копии одной цифры разошлись бы: адаптер (T-5.2) и сценарий (T-5.4)
    обязаны говорить об одном и том же TTL и окне ожидания.
    """
    lock_params = inspect.signature(ports.LockManager.lock).parameters
    acquire_params = inspect.signature(ports.LockManager.acquire).parameters

    assert lock_params['ttl_seconds'].default == ports.DEFAULT_LOCK_TTL_SECONDS
    assert lock_params['wait_seconds'].default == ports.DEFAULT_LOCK_WAIT_SECONDS
    assert acquire_params['ttl_seconds'].default == ports.DEFAULT_LOCK_TTL_SECONDS
    assert acquire_params['wait_seconds'].default == ports.DEFAULT_LOCK_WAIT_SECONDS


def test_default_lock_ttl_is_positive() -> None:
    """TTL по умолчанию положителен: нулевая блокировка не защищала бы ресурс."""
    assert ports.DEFAULT_LOCK_TTL_SECONDS > 0


def test_default_lock_wait_does_not_block() -> None:
    """По умолчанию захват не ждёт: конкуренция не должна подвешивать вызов."""
    assert ports.DEFAULT_LOCK_WAIT_SECONDS == 0.0


def test_account_lock_resource_prefix_builds_resource_name() -> None:
    """Формат ресурса счёта — источник правды и для сценариев, и для Redis-адаптера."""
    account_id = uuid4()

    resource = f'{ports.ACCOUNT_LOCK_RESOURCE_PREFIX}{account_id}'

    assert resource == f'account:{account_id}'


def test_lock_acquisition_error_is_domain_coordination_failure() -> None:
    """Ошибку неудачного ``lock`` поднимает домен, и это не ошибка значения входа."""
    error = LockAcquisitionError('Ресурс account:acc-1 занят: ожидание 1.0 с истекло')

    assert isinstance(error, DomainError)
    assert not isinstance(error, ValueError)


# --- Структурное соответствие: объект с нужными методами проходит isinstance ---


class _FakeAccountRepository:
    async def get(self, _account_id: object) -> None:
        return None

    async def get_for_update(self, _account_id: object) -> None:
        return None

    async def add(self, _account: object) -> None:
        return None

    async def update(self, _account: object) -> None:
        return None


class _FakeClock:
    def now(self) -> object:
        return None


class _FakeEventPublisher:
    async def publish(self, _event: object) -> None:
        return None


def test_fake_satisfies_account_repository() -> None:
    assert isinstance(_FakeAccountRepository(), ports.AccountRepository)


def test_fake_satisfies_clock() -> None:
    assert isinstance(_FakeClock(), ports.Clock)


def test_fake_satisfies_event_publisher() -> None:
    assert isinstance(_FakeEventPublisher(), ports.EventPublisher)


def test_incomplete_object_does_not_satisfy_protocol() -> None:
    """Объект без нужных методов не должен проходить проверку на порт."""
    assert not isinstance(object(), ports.AccountRepository)


# --- Типы, пересекающие порт провайдера ---


def test_provider_status_is_str_enum() -> None:
    assert ports.ProviderStatus.SUCCEEDED == 'SUCCEEDED'
    assert str(ports.ProviderStatus.SUCCEEDED) == 'SUCCEEDED'


def test_provider_result_is_immutable_frozen_dataclass() -> None:
    from dataclasses import FrozenInstanceError

    result = ports.ProviderResult(provider_payment_id='prov-1', status=ports.ProviderStatus.PENDING)
    assert result.provider_payment_id == 'prov-1'
    with pytest.raises(FrozenInstanceError):
        result.status = ports.ProviderStatus.SUCCEEDED


def test_provider_result_rejects_blank_id_and_non_status() -> None:
    from src.core_service.domain.exceptions import InvalidValueError

    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        ports.ProviderResult(provider_payment_id='   ', status=ports.ProviderStatus.PENDING)
    with pytest.raises(InvalidValueError, match='ProviderStatus'):
        ports.ProviderResult(provider_payment_id='prov-1', status='PENDING')


def test_idempotency_record_is_frozen() -> None:
    from dataclasses import FrozenInstanceError
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    record = ports.IdempotencyRecord(key='k', request_hash='h', response=None, created_at=now, expires_at=now)
    with pytest.raises(FrozenInstanceError):
        record.key = 'other'
