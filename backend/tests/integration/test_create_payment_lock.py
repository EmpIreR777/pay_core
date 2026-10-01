"""Блокировка счёта в сценарии создания платежа на живых Postgres и Redis (T-5.4).

Сценарий берёт замок счёта плательщика ещё с T-2.4, а юнит-тесты на фейках
проверяют, что он **обращается** к менеджеру. Ни то, ни другое не показывает
требуемого: что настоящий ``RedisLockManager`` действительно держит ресурс всё
время списания и **отпускает его при любом исходе** — включая отказы.

Модуль собирает сценарий на боевых адаптерах (Postgres-репозитории,
``PostgresUnitOfWork``, ``RedisPostgresIdempotencyStore``, ``RedisLockManager``)
и смотрит на ключ ``lock:account:<uuid>`` в Redis в те моменты, когда это имеет
смысл: внутри транзакции списания, после успеха, после доменного и после
непредвиденного отказа.

Главный приём — **пауза внутри замка**. Первое событие сценарий публикует уже
внутри блокировки и транзакции списания, поэтому издатель, задерживающий это
одно событие, даёт тесту детерминированную точку «сценарий стоит, ресурс занят»
— без гонок и без ``sleep()`` на угаданный срок (AGENT.md, §5).

Отдельно проверяются две вещи, которые легко упустить:

* **замок точечный** — счёт другого плательщика продолжает работать, пока заперт
  первый, иначе «защита от гонки» превратилась бы в глобальную блокировку;
* **замок не передерживается** — к моменту сетевого вызова провайдера ресурс уже
  свободен: держать его на время внешнего ответа незачем, это сузило бы окно для
  других платежей того же счёта.

Стенд не разрушается: таблицы чистятся фикстурой модуля, Redis — фикстурой
``redis_client``. Без Postgres или Redis тесты пропускаются либо берут временный
testcontainer, а без Docker вовсе пропускаются: ``make test`` обязан оставаться
зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core_service.application.dto.payment import CreatePaymentInput
from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.lock_manager import ACCOUNT_LOCK_RESOURCE_PREFIX
from src.core_service.application.ports.payment_provider import (
    PaymentProvider,
    ProviderResult,
    ProviderStatus,
)
from src.core_service.application.use_cases.create_payment import CreatePaymentUseCase
from src.core_service.domain.entities.account import Account
from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.exceptions import InsufficientFunds, LockAcquisitionError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.db.idempotency_store import RedisPostgresIdempotencyStore, reservation_key
from src.db.lock_manager import RedisLockManager, lock_key
from src.db.models.account import AccountModel
from src.db.models.payment import PaymentModel
from src.db.repositories import PostgresUnitOfWork, new_account_model
from tests.integration.conftest import (
    build_session_maker,
    clear_idempotency_records,
    clear_payment_data,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures('postgres_stack', 'redis_stack'),
]

#: Фабрика сессий приложения — ровно тот тип, которого ждут адаптеры.
AsyncSessionFactory = async_sessionmaker[AsyncSession]

KEY = 'create-payment-lock-71ab'
OTHER_KEY = 'create-payment-lock-91cd'
AMOUNT = Money.from_number(Decimal('250.00'), Currency.RUB)
OPENING_BALANCE = Money.from_number(Decimal('1000.00'), Currency.RUB)
#: Сумма заведомо больше баланса — для отказа по существу, а не по гонке.
TOO_MUCH = Money.from_number(Decimal('5000.00'), Currency.RUB)

#: Баланс после одного успешного платежа (1000 минус 250) и баланс счёта, которого
#: отказ не коснулся. Оба читаются из базы, а не считаются в тесте.
BALANCE_AFTER_PAYMENT = Decimal('750.00')
BALANCE_UNTOUCHED = Decimal('1000.00')

#: Ответ провайдера по умолчанию: операция принята и в работе. Терминальный ответ
#: заставил бы сценарий вернуть холд и проверить тут уже другое правило.
PROVIDER_STATUS = ProviderStatus.PROCESSING

#: Потолок ожидания затвора: пауза нужна, чтобы тест гарантированно успел заглянуть
#: в Redis, а потолок нужен, чтобы при поломке тест честно падал, а не висел
#: (AGENT.md, §5: никаких бесконечных ожиданий).
GATE_TIMEOUT_SECONDS = 5.0


class _StubClock:
    """Часы с замороженным временем — те же, что у сценариев (T-2.1).

    Заморожены намеренно: срок жизни ответа считается от этих часов, и плавающее
    «сейчас» превращало бы проверки TTL в проверки скорости машины.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class _BlockingPublisher:
    """Издатель, задерживающий первое событие до тех пор, пока тест не отпустит.

    Пауза нужна в одном-единственном месте: первое событие сценарий публикует
    **внутри** блокировки счёта и транзакции списания. Это даёт тесту точку, где
    можно проверить «ресурс занят» — без гонок и без угадывания момента.

    Режим ``failure`` заставляет первое же событие упасть: так проверяется
    освобождение замка при непредвиденном сбое, когда до коммита дело не дошло.
    """

    def __init__(self, *, failure: Exception | None = None) -> None:
        self._arrived = asyncio.Event()
        self._release = asyncio.Event()
        self._failure = failure
        self._paused = True
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)
        if not self._paused:
            return
        self._paused = False
        if self._failure is not None:
            raise self._failure
        self._arrived.set()
        await asyncio.wait_for(self._release.wait(), timeout=GATE_TIMEOUT_SECONDS)

    async def wait_until_inside(self, running: Awaitable[object]) -> None:
        """Дождаться паузы; если сценарий упал раньше неё — показать его ошибку.

        Без этого ожидания тест отпустил бы затвор до того, как сценарий до него
        дошёл, и проверял бы не то расположение событий, которое задумано.

        Отдельно разбираем случай «сценарий упал, не дойдя до точки»: тогда ждать
        затвора бессмысленно, и голый ``TimeoutError`` скрыл бы настоящую причину —
        сбой соединения, отказ домена, нарушение инварианта. Поэтому при таймауте
        задача сначала дожидается и пробрасывает **её** ошибку.
        """
        try:
            await asyncio.wait_for(self._arrived.wait(), timeout=GATE_TIMEOUT_SECONDS)
        except TimeoutError:
            if running is not None:
                await running
            raise

    def release(self) -> None:
        """Отпустить сценарий."""
        self._release.set()


class _LockWatchingProvider:
    """Провайдер, фиксирующий, держит ли кто-то счёт в момент внешнего вызова.

    Идентификатор операции у провайдера — **свой на каждый вызов**: в базе он
    уникален, и один общий на два платежа ронял бы второй тест подряд, выглядя
    поломкой блокировки вместо мусора от провайдера.
    """

    def __init__(self, redis: Redis, *, account_id: AccountId) -> None:
        self._redis = redis
        self._resource = f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{account_id}'
        self.calls = 0
        self.lock_held_during_call: bool | None = None

    async def create_payment(self, *, payment_id: PaymentId, amount: Money, idempotency_key: str) -> ProviderResult:
        del payment_id, amount, idempotency_key
        self.calls += 1
        self.lock_held_during_call = bool(await self._redis.exists(lock_key(self._resource)))
        return ProviderResult(provider_payment_id=f'prov-lock-{self.calls}', status=PROVIDER_STATUS)

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        del provider_payment_id
        return PROVIDER_STATUS

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        del provider_payment_id, amount
        return ProviderResult(provider_payment_id=f'prov-lock-refund-{self.calls}', status=PROVIDER_STATUS)


# --- Фикстуры ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    """Очистить рабочие таблицы до и после модуля.

    Очистка обязательна по существу: сценарий коммитит по-настоящему, и оставленные
    им строки пережили бы тест — повторный прогон споткнулся бы о первичный ключ и
    выглядел бы как поломка блокировки, а не как мусор от прошлого прогона.
    """
    asyncio.run(clear_payment_data())
    asyncio.run(clear_idempotency_records())
    yield
    asyncio.run(clear_payment_data())
    asyncio.run(clear_idempotency_records())


@pytest.fixture
async def session_maker() -> AsyncIterator[AsyncSessionFactory]:
    """Фабрика сессий приложения поверх живого Postgres.

    Пул не отключается: сценарий и хранилище работают одновременно и вправе держать
    несколько соединений, а ``NullPool`` поднимал бы новое соединение на каждый запрос.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        yield build_session_maker(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def clock() -> _StubClock:
    return _StubClock()


@pytest.fixture
def store(
    redis_client: Redis,
    session_maker: AsyncSessionFactory,
    clock: _StubClock,
) -> RedisPostgresIdempotencyStore:
    """Настоящее хранилище идемпотентности на живых Redis и Postgres."""
    return RedisPostgresIdempotencyStore(redis=redis_client, session_maker=session_maker, clock=clock)


@pytest.fixture
def manager(redis_client: Redis) -> RedisLockManager:
    """Настоящий менеджер блокировок на живом Redis — ради него модуль и написан."""
    return RedisLockManager(redis=redis_client)


@pytest.fixture
async def account(session_maker: AsyncSessionFactory) -> AccountId:
    """Счёт плательщика с 1000 RUB, заведённый по-настоящему.

    Счёт создаётся настоящим маппером репозитория, а не сырым ``INSERT``: тест должен
    идти тем же путём, что и сценарий, иначе строка оказалась бы не такой, какую создаёт
    боевое окружение.
    """
    return await _seed_account(session_maker)


async def _seed_account(session_maker: AsyncSessionFactory) -> AccountId:
    """Завести ещё один счёт — для проверки точечности блокировки."""
    payer = Account(account_id=AccountId.new(), balance=OPENING_BALANCE)
    async with session_maker() as session:
        session.add(new_account_model(payer))
        await session.commit()
    return payer.id


# --- Сборка сценария и чтение того, что осталось в базе ------------------------


def _build_use_case(
    *,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    provider: PaymentProvider,
    publisher: EventPublisher,
) -> CreatePaymentUseCase:
    """Сценарий на настоящих адаптерах, включая боевой менеджер блокировок.

    Единица работы и хранилище идемпотентности получают одну и ту же фабрику сессий:
    так они работают в бою, и проверка идёт по одной базе, а не по двум копиям схемы.
    """
    return CreatePaymentUseCase(
        uow_factory=lambda: PostgresUnitOfWork(session_maker),
        lock_manager=manager,
        idempotency_store=store,
        payment_provider=provider,
        event_publisher=publisher,
        clock=clock,
    )


def _resource(account_id: AccountId) -> str:
    """Имя блокируемого ресурса — ровно то, что строит сам сценарий."""
    return f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{account_id}'


def _input(account_id: AccountId, *, amount: Money = AMOUNT, key: str = KEY) -> CreatePaymentInput:
    return CreatePaymentInput(from_account_id=account_id, amount=amount, idempotency_key=key)


async def _lock_held(redis_client: Redis, account_id: AccountId) -> bool:
    """Занят ли счёт в Redis прямо сейчас."""
    return bool(await redis_client.exists(lock_key(_resource(account_id))))


async def _balance(session_maker: AsyncSessionFactory, account_id: AccountId) -> Decimal:
    """Баланс счёта по данным базы, а не по объекту, оставшемуся в памяти теста."""
    async with session_maker() as session:
        result = await session.execute(select(AccountModel.balance).where(AccountModel.id == account_id.value))
        return result.scalar_one()


async def _payment_count(session_maker: AsyncSessionFactory) -> int:
    """Сколько платежей осталось в базе — то, что на самом деле создалось."""
    async with session_maker() as session:
        result = await session.execute(select(func.count()).select_from(PaymentModel))
        return int(result.scalar_one())


def _open_publisher() -> _BlockingPublisher:
    """Издатель, который не задерживает сценарий: затвор открыт с самого начала."""
    publisher = _BlockingPublisher()
    publisher.release()
    return publisher


# --- DoD: замок держится во время списания и снимается при любом исходе ---------


async def test_lock_is_held_while_the_debit_runs(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """DoD: пока идёт списание, счёт плательщика действительно заперт.

    На фейках такую проверку написать нельзя: всё в одном процессе, и «занято»
    обеспечено словарём, а не Redis. Здесь пауза стоит внутри блокировки и внутри
    транзакции списания — ровно то состояние, ради которого замок и берётся.
    """
    publisher = _BlockingPublisher()
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=_LockWatchingProvider(redis_client, account_id=account),
        publisher=publisher,
    )

    running = asyncio.create_task(use_case.execute(_input(account)))
    await publisher.wait_until_inside(running)

    assert await _lock_held(redis_client, account) is True
    # Чужая попытка взять тот же ресурс отклоняется: взаимное исключение живое.
    assert await manager.acquire(_resource(account)) is None

    publisher.release()
    await running

    assert await _lock_held(redis_client, account) is False
    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == BALANCE_AFTER_PAYMENT


async def test_lock_is_released_after_a_successful_payment(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """Успешный путь не оставляет за собой ни ключа в Redis, ни незакрытой транзакции."""
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=_LockWatchingProvider(redis_client, account_id=account),
        publisher=_open_publisher(),
    )

    await use_case.execute(_input(account))

    assert await _lock_held(redis_client, account) is False
    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == BALANCE_AFTER_PAYMENT


async def test_lock_is_released_when_funds_are_insufficient(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """Отказ по существу (списать нечего) тоже обязан отпустить счёт.

    Иначе висящий в Redis счёт блокировал бы все следующие платежи клиента до
    конца TTL — за обычную нехватку денег.
    """
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=_LockWatchingProvider(redis_client, account_id=account),
        publisher=_open_publisher(),
    )

    with pytest.raises(InsufficientFunds):
        await use_case.execute(_input(account, amount=TOO_MUCH))

    assert await _lock_held(redis_client, account) is False
    assert await _balance(session_maker, account) == BALANCE_UNTOUCHED
    assert await _payment_count(session_maker) == 0
    # Ключ идемпотентности отпущен: клиент вправе повторить этот же запрос.
    assert not await redis_client.exists(reservation_key(KEY))


async def test_lock_is_released_when_the_scenario_breaks_unexpectedly(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """Непредвиденный сбой посреди транзакции тоже освобождает счёт.

    Проверяется худший случай: до коммита дело не дошло, так что откатывать
    нечего — а счёт всё равно не должен остаться запертым.
    """
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=_LockWatchingProvider(redis_client, account_id=account),
        publisher=_BlockingPublisher(failure=RuntimeError('сбой публикации')),
    )

    with pytest.raises(RuntimeError, match='сбой публикации'):
        await use_case.execute(_input(account))

    assert await _lock_held(redis_client, account) is False
    assert await _balance(session_maker, account) == BALANCE_UNTOUCHED
    assert await _payment_count(session_maker) == 0


async def test_second_payment_on_the_same_account_is_rejected(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """DoD: параллельный платёж по тому же счёту отбивается, а не проходит вторым.

    Ключ идемпотентности у второго запроса другой — иначе его отбил бы захват
    идемпотентности, и проверялась бы не блокировка счёта.
    """
    publisher = _BlockingPublisher()
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=_LockWatchingProvider(redis_client, account_id=account),
        publisher=publisher,
    )

    running = asyncio.create_task(use_case.execute(_input(account)))
    await publisher.wait_until_inside(running)

    with pytest.raises(LockAcquisitionError):
        await use_case.execute(_input(account, key=OTHER_KEY))

    # Замок по-прежнему у первого, а резервация отбитого — отпущена.
    assert await _lock_held(redis_client, account) is True
    assert not await redis_client.exists(reservation_key(OTHER_KEY))

    publisher.release()
    await running

    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == BALANCE_AFTER_PAYMENT


async def test_lock_does_not_block_other_accounts(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """Заблокирован один счёт — остальные платёжи проходят как обычно.

    Иначе «защита от гонки по счёту» выродилась бы в глобальную блокировку, и
    платёж одного клиента останавливал бы всех.
    """
    publisher = _BlockingPublisher()
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=_LockWatchingProvider(redis_client, account_id=account),
        publisher=publisher,
    )

    running = asyncio.create_task(use_case.execute(_input(account)))
    await publisher.wait_until_inside(running)

    other = await _seed_account(session_maker)
    await use_case.execute(_input(other, key=OTHER_KEY))

    assert await _lock_held(redis_client, account) is True
    assert await _lock_held(redis_client, other) is False
    assert await _balance(session_maker, other) == BALANCE_AFTER_PAYMENT

    publisher.release()
    await running

    assert await _payment_count(session_maker) == 2


async def test_lock_is_released_before_the_provider_call(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
    account: AccountId,
) -> None:
    """К сетевому вызову провайдера замок уже снят: счёт не держится на время ответа.

    Иначе окно для других платежей того же счёта сузилось бы на всю длительность
    внешнего запроса — а это ровно то, чего блокировка не должна делать.
    """
    provider = _LockWatchingProvider(redis_client, account_id=account)
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=provider,
        publisher=_open_publisher(),
    )

    await use_case.execute(_input(account))

    assert provider.calls == 1
    assert provider.lock_held_during_call is False
