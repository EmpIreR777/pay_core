"""Тест-молот: сто параллельных переводов с одного счёта (T-5.5).

Задача эпика, ради которой написан весь ЭПИК 5: **ни одного потерянного движения
денег** при тысяче одновременных желаний списать один и тот же баланс.

Сценарий вызывает ``lock()`` с параметрами по умолчанию, то есть ``wait_seconds = 0``:
конкурент не ждёт, а получает честный отказ. Это не поломка, а замысел — и
тест-молот построен ровно на этом: каждый перевод повторяет попытку после
``LockAcquisitionError``, пока не пройдёт. Так проверяется не «отказ», а
**сериализация**: сто гоняющихся за одним ресурсом в итоге обязаны дать ровно
сто корректных движений и ни одного потерянного.

Два сюжета:

* **ровно на баланс** — 100 переводов по 10.00 со счёта в 1000.00 обязаны обнулить
  его до копейки и создать ровно 100 платежей. Любая потерянная запись дала бы
  ненулевой остаток, любая двойная — отрицательный;
* **всегда в минус не уйти** — те же 100 переводов, но счёт всего 100.00. Успешных
  обязано быть ровно 10, остальные 90 — честный ``InsufficientFunds``. Именно эта
  проверка ловит оверсписание: без блокировки часть «переводов» прошла бы, когда
  деньги уже кончились.

Конкуренция в обоих сюжетах не выдумывается, а **проверяется**: тест требует, чтобы
хоть часть попыток реально была отбита блокировкой. Молот, который ни разу не
стукнул, ничего не доказывает.

Стенд не разрушается: таблицы чистятся фикстурой модуля, Redis — фикстурой
``redis_client``. Без Postgres или Redis тесты пропускаются либо берут временный
testcontainer, а без Docker вовсе пропускаются (AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core_service.application.dto.payment import CreatePaymentInput
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
from src.db.idempotency_store import RedisPostgresIdempotencyStore
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

#: Сколько переводов бьёт по одному счёту одновременно — как требует DoD.
HAMMER_SIZE = 100

#: Один перевод: сто штук по 10.00 обнуляют счёт в 1000.00 ровно, без копейки остатка.
TRANSFER = Money.from_number(Decimal('10.00'), Currency.RUB)
FULL_BALANCE = Money.from_number(Decimal('1000.00'), Currency.RUB)
#: Счёт, на котором переводов больше, чем денег: успешных будет ровно 10.
SHORT_BALANCE = Money.from_number(Decimal('100.00'), Currency.RUB)

#: Потолок попыток на один перевод. Он не «для красоты»: без него зациклившийся
#: перевод превратил бы тест в бесконечное ожидание, а AGENT.md, §5 запрещает такие
#: ожидания. Значение заведомо велико — при корректной блокировке хватает единиц.
MAX_ATTEMPTS = 2_000

#: Ответ провайдера: операция принята и в работе. Терминальный ответ вернул бы холд
#: и заговорил бы уже о другом правиле, а молот должен бить по списанию.
PROVIDER_STATUS = ProviderStatus.PROCESSING


class _StubClock:
    """Часы с замороженным временем — те же, что у сценариев (T-2.1)."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class _RecordingPublisher:
    """Издатель, который только запоминает события: outbox тут никто не читает."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)


class _CountingProvider:
    """Провайдер, считающий вызовы и выдающий уникальный идентификатор операции.

    Счёт вызовов здесь — проверка против двойного исполнения: если бы перевод прошёл
    дважды, увидели бы два вызова на один платёж. Идентификатор операции в базе
    уникален, поэтому он свой на каждый вызов.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.provider_payment_ids: list[str] = []

    async def create_payment(self, *, payment_id: PaymentId, amount: Money, idempotency_key: str) -> ProviderResult:
        del payment_id, amount, idempotency_key
        self.calls += 1
        provider_payment_id = f'prov-hammer-{self.calls}'
        self.provider_payment_ids.append(provider_payment_id)
        return ProviderResult(provider_payment_id=provider_payment_id, status=PROVIDER_STATUS)

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        del provider_payment_id
        return PROVIDER_STATUS

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        del provider_payment_id, amount
        self.calls += 1
        return ProviderResult(provider_payment_id=f'prov-hammer-refund-{self.calls}', status=PROVIDER_STATUS)


# --- Фикстуры ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    """Очистить рабочие таблицы до и после модуля.

    Очистка обязательна по существу: молот коммитит сотни платежей по-настоящему, и
    оставленные ими строки пережили бы тест — повторный прогон споткнулся бы о
    первичный ключ и выглядел бы как поломка баланса, а не как мусор от прошлого.
    """
    asyncio.run(clear_payment_data())
    asyncio.run(clear_idempotency_records())
    yield
    asyncio.run(clear_payment_data())
    asyncio.run(clear_idempotency_records())


@pytest.fixture
async def session_maker() -> AsyncIterator[AsyncSessionFactory]:
    """Фабрика сессий приложения поверх живого Postgres.

    Пул не отключается: сотня конкурентов вправе держать несколько соединений, а
    ``NullPool`` поднимал бы новое соединение на каждый запрос.
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


# --- Сборка сценария и чтение того, что осталось в базе ------------------------


def _build_use_case(
    *,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    provider: PaymentProvider,
    publisher: _RecordingPublisher,
) -> CreatePaymentUseCase:
    """Сценарий на настоящих адаптерах, включая боевой менеджер блокировок."""
    return CreatePaymentUseCase(
        uow_factory=lambda: PostgresUnitOfWork(session_maker),
        lock_manager=manager,
        idempotency_store=store,
        payment_provider=provider,
        event_publisher=publisher,
        clock=clock,
    )


async def _seed_account(session_maker: AsyncSessionFactory, balance: Money) -> AccountId:
    """Завести счёт с заданным балансом по-настоящему, через маппер репозитория."""
    payer = Account(account_id=AccountId.new(), balance=balance)
    async with session_maker() as session:
        session.add(new_account_model(payer))
        await session.commit()
    return payer.id


def _input(account_id: AccountId, *, key: str, amount: Money = TRANSFER) -> CreatePaymentInput:
    return CreatePaymentInput(from_account_id=account_id, amount=amount, idempotency_key=key)


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


async def _payment_total(session_maker: AsyncSessionFactory) -> Decimal:
    """Сумма всех созданных платежей — сколько денег списано по факту."""
    async with session_maker() as session:
        result = await session.execute(select(func.sum(PaymentModel.amount)))
        return result.scalar_one()


async def _run_hammer(
    use_case: CreatePaymentUseCase,
    account_id: AccountId,
    *,
    size: int = HAMMER_SIZE,
    key_prefix: str = 'hammer',
) -> tuple[list[str], int]:
    """Прогнать ``size`` переводов конкурентно и собрать их исходы.

    Каждый перевод — своя задача и **свой** ключ идемпотентности: общий ключ отбил бы
    второй запрос захватом идемпотентности, и проверялась бы не блокировка счёта.
    Отказ по блокировке — не провал: замок по умолчанию не ждёт, поэтому перевод
    повторяет попытку. Именно повтор и доказывает, что блокировка сериализует.

    :returns: ``(статусы, отбитое блокировкой)``; статус — ``'ok'``, ``'funds'``
        (честный отказ по деньгам) либо ``'exhausted'`` (упёрлись в потолок попыток).
    """

    async def _transfer(index: int) -> tuple[str, int]:
        lock_refusals = 0
        for _ in range(MAX_ATTEMPTS):
            try:
                await use_case.execute(_input(account_id, key=f'{key_prefix}-{index}'))
            except LockAcquisitionError:
                lock_refusals += 1
                continue
            except InsufficientFunds:
                return 'funds', lock_refusals
            return 'ok', lock_refusals
        return 'exhausted', lock_refusals

    results = await asyncio.gather(*(_transfer(index) for index in range(size)))
    return [status for status, _ in results], sum(refusals for _, refusals in results)


# --- DoD: сто параллельных переводов не теряют и не воруют ---------------------


async def test_hundred_concurrent_transfers_leave_the_balance_exactly_zero(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
    redis_client: Redis,
) -> None:
    """DoD: 100 переводов по 10.00 обнуляют счёт до копейки, ни один не теряется.

    Потерянное обновление дало бы ненулевой остаток, двойное списание — отрицательный.
    Минус запрещён ещё и на уровне БД (``CHECK balance >= 0``), но проверяет-то тест
    именно сценарий: он обязан сходиться без участия ограничения.
    """
    payer = await _seed_account(session_maker, FULL_BALANCE)
    provider = _CountingProvider()
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=provider,
        publisher=_RecordingPublisher(),
    )

    statuses, lock_refusals = await _run_hammer(use_case, payer)

    assert 'exhausted' not in statuses, 'перевод упёрся в потолок попыток — блокировка зависла'
    assert statuses.count('ok') == HAMMER_SIZE
    # Конкуренция обязана была кого-то отбить: молот, который ни разу не стукнул,
    # ничего не доказывает — он прошёл бы и на коде вовсе без блокировки.
    assert lock_refusals > 0
    assert await _balance(session_maker, payer) == Decimal('0.00')
    assert await _payment_count(session_maker) == HAMMER_SIZE
    assert await _payment_total(session_maker) == Decimal('1000.00')
    # Каждый платёж дошёл до провайдера ровно один раз — двойного исполнения нет.
    assert provider.calls == HAMMER_SIZE
    assert len(set(provider.provider_payment_ids)) == HAMMER_SIZE
    # Сто операций подряд не оставили за собой висящего замка.
    assert not await redis_client.exists(lock_key(f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{payer}'))


async def test_concurrent_transfers_cannot_overdraw_the_account(
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    manager: RedisLockManager,
) -> None:
    """Сто переводов против 100.00 на счёте: успешных ровно 10, минус невозможен.

    Именно эта проверка ловит оверсписание: без блокировки часть «переводов» прошла бы
    уже после того, как деньги кончились, и сумма списаний превысила бы остаток.
    """
    payer = await _seed_account(session_maker, SHORT_BALANCE)
    provider = _CountingProvider()
    use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        manager=manager,
        provider=provider,
        publisher=_RecordingPublisher(),
    )

    statuses, lock_refusals = await _run_hammer(use_case, payer)

    assert 'exhausted' not in statuses
    assert statuses.count('ok') == 10
    assert statuses.count('funds') == HAMMER_SIZE - 10
    assert await _balance(session_maker, payer) == Decimal('0.00')
    assert await _payment_count(session_maker) == 10
    assert await _payment_total(session_maker) == Decimal('100.00')
    assert provider.calls == 10
    assert lock_refusals > 0
