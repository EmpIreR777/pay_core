"""Идемпотентность сценария создания платежа на живых Postgres и Redis (T-4.3).

Юнит-тесты T-2.4 доказывают, что сценарий **обращается** к хранилищу идемпотентности,
а модуль T-4.2 — что адаптер умеет захватывать ключ и хранить ответ. Ни то, ни другое
не показывает, что вместе они дают требуемое: **повторный запрос не создаёт второй
платёж**. Здесь сценарий собран с настоящими адаптерами — Postgres-репозиториями,
``PostgresUnitOfWork``, ``RedisPostgresIdempotencyStore`` — и вся проверка идёт по
данным, которые остались в базе после завершения запросов.

Почему это нельзя было проверить раньше. Фейки портов (T-2.9) живут в одном процессе и
разделяют общий словарь, поэтому гонки в них нет by construction: там нельзя заставить
две задачи event loop вклиниться в одну запись. Живые Postgres и Redis дают настоящие
точки прерывания — ``await`` на сокете, — а значит и настоящее окно, в котором второй
запрос успевает прочитать пустоту. Юнит-тест на фейках прошёл бы на коде с гонкой;
этот — нет.

Главный сюжет модуля — **два параллельных запроса с одним ключом** (DoD задачи). Он
проверяется в двух видах, и второй вид важнее первого:

* «честный» запуск через ``gather`` — оба запроса стартуют одновременно; проверяется,
  что платёж ровно один и баланс списан один раз;
* **управляемое окно** — первый запрос намеренно задерживается внутри внешнего вызова,
  а второй в это время читает хранилище. Так воспроизводится ровно то расположение
  событий, которое ломало порядок «сначала прочитать ответ, потом захватить ключ»:
  первый успевает записать ответ и снять захват, а второй — прочитать пустоту и взять
  ключ уже свободным. На исправленном коде второй всё равно получает тот же ответ,
  потому что читает хранилище уже **после** захвата.

Остальные проверки — про то, что повтор отдаёт именно тот же ответ: хэш тела запроса
различает повтор и конфликт, а после «рестарта» (кэш холодный) ответ всё равно находится
в Postgres.

Стенд не разрушается: таблицы чистятся фикстурой модуля, Redis — фикстурой
``redis_client``. Без Postgres или Redis тесты пропускаются либо берут временный
testcontainer, а без Docker вовсе пропускаются: ``make test`` обязан оставаться
зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core_service.application.dto.payment import CreatePaymentInput
from src.core_service.application.ports.payment_provider import ProviderResult, ProviderStatus
from src.core_service.application.use_cases.create_payment import CreatePaymentUseCase
from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import DuplicateOperation, InsufficientFunds
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from src.db.idempotency_store import RedisPostgresIdempotencyStore, reservation_key
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

#: Ключ идемпотентности клиента и сумма платежа. Сумма меньше баланса по умолчанию,
#: чтобы отказ «недостаточно средств» никогда не маскировал проверку идемпотентности.
KEY = 'create-payment-race-4d1f'
AMOUNT = Money.from_number(Decimal('250.00'), Currency.RUB)
OPENING_BALANCE = Money.from_number(Decimal('1000.00'), Currency.RUB)

#: Ответ провайдера по умолчанию: операция принята и в работе. Промежуточный статус
#: выбран не случайно — терминальный заставил бы сценарий вернуть холд, и проверка
#: «списано ровно один раз» мерила бы уже не то.
PROVIDER_STATUS = ProviderStatus.PROCESSING

#: Потолок ожидания затвора. Пауза нужна, чтобы тест гарантированно успел прочитать
#: хранилище до того, как первый запрос допишет ответ; потолок нужен, чтобы при поломке
#: тест честно падал, а не висел (AGENT.md, §5: никаких бесконечных ожиданий).
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


class _Gate:
    """Ручной затвор на внешнем вызове провайдера.

    Существует ради одного: дать тесту вклиниться в сагу **между** первым коммитом и
    сохранением ответа. Никакой другой способ не задаёт нужное расположение событий:
    два ``gather`` не гарантируют, что второй запрос прочитает хранилище именно в этом
    окне, и без этого проверка гонки была бы проверкой удачи.
    """

    def __init__(self) -> None:
        self._arrived = asyncio.Event()
        self._release = asyncio.Event()
        self._opened = False

    async def __aenter__(self) -> None:
        self._arrived.set()
        await asyncio.wait_for(self._release.wait(), timeout=GATE_TIMEOUT_SECONDS)

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def open(self) -> None:
        """Разрешить провайдеру ответить."""
        self._opened = True
        self._release.set()

    async def wait_until_arrived(self) -> None:
        """Дождаться, пока провайдер действительно упрётся в затвор.

        Без этого ожидания тест отпустил бы затвор до того, как первый запрос до него
        дошёл, и проверял бы не то расположение событий, которое задумано.
        """
        await asyncio.wait_for(self._arrived.wait(), timeout=GATE_TIMEOUT_SECONDS)

    @property
    def is_opened(self) -> bool:
        return self._opened


class _GatedProvider:
    """Провайдер, который держит ответ в затворе, пока тест не отпустит.

    Всё остальное повторяет фейк T-2.9: успех, счётчик вызовов и
    ``provider_payment_id`` в форме ``provider-<PaymentId>``.
    """

    def __init__(self, gate: _Gate | None = None) -> None:
        self.gate = gate
        self.calls: list[PaymentId] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def create_payment(self, *, payment_id: PaymentId, amount: Money, idempotency_key: str) -> ProviderResult:
        del amount, idempotency_key
        self.calls.append(payment_id)
        if self.gate is not None:
            async with self.gate:
                pass
        return ProviderResult(provider_payment_id=f'provider-{payment_id}', status=PROVIDER_STATUS)

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        raise AssertionError(f'Тест не должен опрашивать статус у провайдера: {provider_payment_id}')

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        raise AssertionError(f'Тест не должен запускать возврат: {provider_payment_id}, {amount}')


class _NoopLockManager:
    """Менеджер блокировок, всегда отдающий захват.

    Настоящий адаптер блокировок — ЭПИК 5, но подставлять здесь фейк T-2.9 нельзя:
    тот обрывает второй захват и **сам** остановил бы дубль, замаскировав гонку, ради
    которой модуль и написан. Счёт всё равно защищён настоящей блокировкой строки в
    Postgres, а проверяется здесь идемпотентность, а не распределённая блокировка.
    """

    @asynccontextmanager
    async def _acquired(self) -> AsyncIterator[None]:
        yield None

    def lock(self, resource: str, **kwargs: Any) -> Any:
        del resource, kwargs
        return self._acquired()

    async def acquire(self, resource: str, **kwargs: Any) -> None:
        del resource, kwargs
        return

    async def release(self, handle: object) -> None:
        del handle
        return


class _RecordingPublisher:
    """Публикатор событий в список; сам outbox проверяется в ЭПИКЕ 8.

    События здесь интересуют только как маркер «сценарий дошёл до конца шага»; их
    состав проверяют юнит-тесты T-2.4.
    """

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


class _PausedStore:
    """Декоратор над **настоящим** хранилищем: задерживает один вызов второго запроса.

    Хранилище внутри — то же, что и без декоратора, со всеми обращениями к живому Redis
    и Postgres. Декоратор добавляет ровно одно — управление чередованием: он умеет
    остановить второй запрос сразу после его обращения к хранилищу, чтобы тест успел
    завершить первый.

    Зачем это нужно. На исправленном коде дубль, читающий хранилище, когда ключ ещё
    захвачен, получает честный отказ, и второй платёж не создаётся. Но именно поэтому
    та же проверка **прошла бы и на неправильном коде**: окно, в котором старый порядок
    «прочитать, потом захватить» создавал второй платёж, на новом порядке просто
    невозможно — захват идёт первым. Тест, который не может отличить исправленный код
    от неисправленного, не доказывает ничего.

    Остановка нужна, чтобы воспроизвести это окно принудительно: первый запрос
    доходит до провайдера и **удерживает** захват; второй тем временем читает
    хранилище (ответа ещё нет) и замирает; тест отпускает первый — тот дописывает ответ
    и снимает захват; второй продолжает. На старом порядке его ``SET NX`` теперь
    проходит на свободном ключе, и создаётся второй платёж. На новом — отказ.

    Останавливается именно та задача, которая указана при создании (``create_task``
    даёт разные объекты), поэтому сохранение ответа первым запросом — обычный вызов
    хранилища, и оно замирания не касается.
    """

    def __init__(self, inner: RedisPostgresIdempotencyStore) -> None:
        self._inner = inner
        self._paused_task: asyncio.Task[object] | None = None
        self._calls = 0
        self._reached = asyncio.Event()
        self._release = asyncio.Event()

    def pause_task(self, task: asyncio.Task[object]) -> None:
        """Указать задачу, чьё первое обращение к хранилищу будет остановлено.

        Задача известна только после ``create_task``, а хранилище нужно собрать раньше —
        поэтому оно назначается здесь, а не в конструкторе.
        """
        self._paused_task = task

    async def _call(self, operation: Any) -> Any:
        result = await operation
        if asyncio.current_task() is not self._paused_task:
            return result
        self._calls += 1
        if self._calls == 1:
            self._reached.set()
            await asyncio.wait_for(self._release.wait(), timeout=GATE_TIMEOUT_SECONDS)
        return result

    async def get(self, key: str) -> Any:
        return await self._call(self._inner.get(key))

    async def save(self, record: Any) -> None:
        await self._call(self._inner.save(record))

    async def try_acquire(self, key: str, *, ttl_seconds: int) -> bool:
        return await self._call(self._inner.try_acquire(key, ttl_seconds=ttl_seconds))

    async def release(self, key: str) -> None:
        await self._call(self._inner.release(key))

    async def wait_until_paused(self) -> None:
        """Дождаться, пока второй запрос действительно дошёл до хранилища."""
        await asyncio.wait_for(self._reached.wait(), timeout=GATE_TIMEOUT_SECONDS)

    def resume(self) -> None:
        """Отпустить второй запрос."""
        self._release.set()


# --- Фикстуры ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    """Очистить рабочие таблицы до и после модуля.

    Очистка обязательна по существу: сценарий коммитит по-настоящему, и оставленные им
    строки пережили бы тест — повторный прогон споткнулся бы о первичный ключ и выглядел
    бы как поломка идемпотентности, а не как мусор от прошлого прогона.
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
def gate() -> _Gate:
    return _Gate()


@pytest.fixture
async def account(session_maker: AsyncSessionFactory) -> AccountId:
    """Счёт плательщика с 1000 RUB, заведённый в базе по-настоящему.

    Счёт создаётся настоящим маппером репозитория, а не сырым ``INSERT``: тест должен
    идти тем же путём, что и сценарий, иначе строка оказалась бы не такой, какую создаёт
    боевое окружение.
    """
    payer = Account(account_id=AccountId.new(), balance=OPENING_BALANCE)
    async with session_maker() as session:
        session.add(new_account_model(payer))
        await session.commit()
    return payer.id


# --- Сборка сценария и чтение того, что осталось в базе -----------------------


def _build_use_case(
    *,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    provider: _GatedProvider,
) -> CreatePaymentUseCase:
    """Сценарий на настоящих адаптерах.

    Единица работы и хранилище идемпотентности получают одну и ту же фабрику сессий:
    так они работают в бою, и проверка идёт по одной базе, а не по двум копиям схемы.
    """
    return CreatePaymentUseCase(
        uow_factory=lambda: PostgresUnitOfWork(session_maker),
        lock_manager=_NoopLockManager(),
        idempotency_store=store,
        payment_provider=provider,
        event_publisher=_RecordingPublisher(),
        clock=clock,
    )


def _input(account_id: AccountId, *, amount: Money = AMOUNT, key: str = KEY) -> CreatePaymentInput:
    return CreatePaymentInput(from_account_id=account_id, amount=amount, idempotency_key=key)


async def _payment_count(session_maker: AsyncSessionFactory) -> int:
    """Сколько платежей осталось в базе — то, что на самом деле создалось."""
    async with session_maker() as session:
        result = await session.execute(select(func.count()).select_from(PaymentModel))
        return int(result.scalar_one())


async def _only_payment_id(session_maker: AsyncSessionFactory) -> Any:
    """Идентификатор единственного платежа из базы; падает, если их больше одного."""
    async with session_maker() as session:
        result = await session.execute(select(PaymentModel.id))
        ids = list(result.scalars())
    assert len(ids) == 1, f'ожидался ровно один платёж, найдено {len(ids)}'
    return ids[0]


async def _balance(session_maker: AsyncSessionFactory, account_id: AccountId) -> Decimal:
    """Баланс счёта по данным базы, а не по объекту, оставшемуся в памяти теста."""
    async with session_maker() as session:
        result = await session.execute(
            select(AccountModel.balance).where(AccountModel.id == account_id.value),
        )
        return result.scalar_one()


# --- DoD: два параллельных запроса создают ровно один платёж -------------------


async def test_parallel_requests_create_exactly_one_payment(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
) -> None:
    """DoD задачи: два одновременных запроса с одним ключом — один платёж.

    Оба стартуют через ``gather``, без всякой координации: кто первым возьмёт ключ,
    тот и выполнит сагу, второй обязан получить отказ и **не** пройти в неё. Проверяется
    то, что осталось в базе, а не то, что вернулось: возврат мог бы быть и отказом, и
    платежом — деньги же важнее ответа.
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    data = _input(account)

    results = await asyncio.gather(
        use_case.execute(data),
        use_case.execute(data),
        return_exceptions=True,
    )

    assert await _payment_count(session_maker) == 1
    # Ровно один вызов провайдера: если бы в сагу прошли оба, платежей было бы два, —
    # но лишний вызов означал бы ещё и лишнюю операцию у внешнего эквайринга.
    assert provider.call_count == 1
    succeeded = [result for result in results if not isinstance(result, BaseException)]
    rejected = [result for result in results if isinstance(result, DuplicateOperation)]
    assert len(succeeded) + len(rejected) == 2, f'непредвиденный исход: {results}'
    assert succeeded, f'оба запроса отказали, платёж не создан: {results}'


async def test_parallel_requests_debit_the_account_exactly_once(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
) -> None:
    """Деньги — мера строже, чем количество строк: списывание должно быть одно.

    Отдельная проверка, а не дополнение к предыдущей: два платежа и два списания
    выглядели бы снаружи одинаково, но означают разное. Первое — неверная запись в
    ``payments``, второе — **удвоенное списание клиенту**, и это уже невозможно отменить
    одним откатом.
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)

    await asyncio.gather(
        use_case.execute(_input(account)),
        use_case.execute(_input(account)),
        return_exceptions=True,
    )

    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount


async def test_duplicate_arriving_mid_flight_is_rejected_without_touching_money(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    gate: _Gate,
) -> None:
    """Первый запрос ещё висит на провайдере — второй обязан получить отказ.

    Здесь отказ **правильный**: операция действительно идёт, ответа ещё нет, и отдавать
    нечего. Проверяется и то, что отказ не превращается в «тихий успех» с нулевым
    результатом: платёж должен остаться один, а баланс — списан один раз.
    """
    provider = _GatedProvider(gate)
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    data = _input(account)

    first = asyncio.create_task(use_case.execute(data))
    await gate.wait_until_arrived()

    with pytest.raises(DuplicateOperation):
        await use_case.execute(data)

    gate.open()
    await first

    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount
    assert provider.call_count == 1


async def test_duplicate_reading_the_store_mid_flight_still_gets_one_payment(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    gate: _Gate,
) -> None:
    """**Ключевая проверка модуля.** Дубль читает пустоту и всё равно не создаёт платёж.

    Расположение событий задано принудительно (:class:`_PausedStore`): первый доходит до
    провайдера и **удерживает** захват; второй тем временем читает хранилище — ответа
    ещё нет — и замирает; тест отпускает первый, и тот дописывает ответ и снимает захват;
    после этого продолжает второй.

    Именно на этом шаге ломался прежний порядок «сначала прочитать ответ, потом захватить
    ключ»: к моменту ``SET NX`` второго захват уже снят, ключ свободен, и второй уходил в
    сагу вторым. Теперь захват идёт **до** чтения, поэтому второй упирается в чужой
    захват и отказ получает честно — второй платёж не создаётся.

    Проверка не может пройти за счёт блокировки счёта: ``_NoopLockManager`` отдаёт захват
    любому, помешать дублю может только идемпотентность.
    """
    provider = _GatedProvider(gate)
    data = _input(account)
    first_use_case = _build_use_case(
        session_maker=session_maker,
        store=store,
        clock=clock,
        provider=provider,
    )
    first = asyncio.create_task(first_use_case.execute(data))
    await gate.wait_until_arrived()

    # Пока первый висит, под ключом ответа нет — это и есть исходная точка гонки.
    assert await store.get(KEY) is None

    # Второй запрос идёт через декоратор и замирает на своём первом обращении.
    paused_store = _PausedStore(store)
    second_use_case = _build_use_case(
        session_maker=session_maker,
        store=paused_store,
        clock=clock,
        provider=provider,
    )
    second = asyncio.create_task(second_use_case.execute(data))
    paused_store.pause_task(second)
    await paused_store.wait_until_paused()

    # Первый дописывает ответ и снимает захват — теперь ключ свободен.
    gate.open()
    await first

    paused_store.resume()
    second_result = await second

    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount
    assert provider.call_count == 1
    # Второй не прошёл в сагу: он либо получил тот же платёж из хранилища, либо отказ.
    if isinstance(second_result, BaseException):
        assert isinstance(second_result, DuplicateOperation), f'непредвиденный отказ: {second_result!r}'
    else:
        assert second_result.payment_id.value == await _only_payment_id(session_maker)


# --- Повтор возвращает тот же ответ -------------------------------------------


async def test_repeat_returns_the_same_payment_and_does_not_debit_twice(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
) -> None:
    """Обычный повтор по горячему кэшу: тот же платёж, одно списание, один вызов шлюза.

    Базовый сюжет задачи целиком — «повторный запрос с тем же ключом возвращает тот же
    ответ без повтора логики» — но уже на настоящих адаптерах. Проверяется и то, что
    повтор **не трогает** внешний шлюз: лишний вызов ``create_payment`` означал бы вторую
    операцию у эквайринга, которую потом пришлось бы отменять.
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    data = _input(account)

    first = await use_case.execute(data)
    second = await use_case.execute(data)

    assert second == first
    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount
    assert provider.call_count == 1


async def test_repeat_after_restart_finds_the_answer_in_postgres(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    redis_client: Redis,
) -> None:
    """«Рестарт процесса»: кэш пуст, ответ лежит только в Postgres — повтор его находит.

    Именно ради этого в T-4.1 предусмотрена долговременная таблица: Redis живёт в
    оперативной памяти и после перезапуска пуст. Если бы ответ хранился только в кэше,
    клиентский ретрай после рестарта создал бы **второй** платёж и списал сумму дважды.
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    data = _input(account)

    first = await use_case.execute(data)
    await redis_client.flushdb()

    second = await use_case.execute(data)

    assert second == first
    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount
    assert provider.call_count == 1


async def test_replay_does_not_keep_the_key_reserved(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    redis_client: Redis,
) -> None:
    """Повтор не оставляет захват висеть: иначе следующий повтор получил бы отказ.

    Прямое следствие порядка «захват, потом чтение»: повтор берёт ключ (предыдущий запуск
    его уже отпустил) и обязан отпустить сам — иначе ключ провисел бы до конца TTL, и
    весь срок дальнейшие запросы по нему получали бы «уже выполняется» вместо готового
    ответа. Проверяется на настоящем Redis, где захват и правда отдельный ключ.
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    data = _input(account)

    await use_case.execute(data)
    await use_case.execute(data)

    assert not await redis_client.exists(reservation_key(KEY))
    # И третий повтор обязан получить тот же ответ, а не отказ по захвату.
    third = await use_case.execute(data)
    assert third == await use_case.execute(data)
    assert await _payment_count(session_maker) == 1


async def test_same_key_with_another_body_is_a_conflict_not_a_repeat(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
) -> None:
    """Тот же ключ с другой суммой — конфликт, а не повтор: чужой ответ отдавать нельзя.

    Ключ хранит хэш **тела** запроса не просто так: клиент, переиспользовавший ключ под
    другую операцию, получит в ответ чужой платёж и решит, что его сумма списана. Хэш —
    единственное, что отличает «тот же запрос» от «другой запрос с тем же ключом».
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    await use_case.execute(_input(account))

    other_amount = Money.from_number(Decimal('10.00'), Currency.RUB)
    with pytest.raises(DuplicateOperation):
        await use_case.execute(_input(account, amount=other_amount))

    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount
    assert provider.call_count == 1


async def test_failed_attempt_releases_the_key_for_the_next_one(
    account: AccountId,
    session_maker: AsyncSessionFactory,
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    redis_client: Redis,
) -> None:
    """Отказ до списания освобождает ключ: тот же запрос клиент вправе повторить.

    Деньги не списаны — платежа нет, отменять нечего, и удерживать ключ до конца TTL
    было бы наказанием за отказ, который клиент не виноват. Проверяется настоящим
    Redis: если бы ``release`` не дошёл до него, повтор получил бы «уже выполняется».
    """
    provider = _GatedProvider()
    use_case = _build_use_case(session_maker=session_maker, store=store, clock=clock, provider=provider)
    too_much = Money.from_number(Decimal('5000.00'), Currency.RUB)

    with pytest.raises(InsufficientFunds):
        await use_case.execute(_input(account, amount=too_much))

    assert await _payment_count(session_maker) == 0
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount
    assert not await redis_client.exists(reservation_key(KEY))

    retried = await use_case.execute(_input(account))

    assert retried.status is PaymentStatus.PROCESSING
    assert await _payment_count(session_maker) == 1
    assert await _balance(session_maker, account) == OPENING_BALANCE.amount - AMOUNT.amount
    assert provider.call_count == 1
