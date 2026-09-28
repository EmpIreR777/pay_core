"""In-memory фейки портов для тестов прикладных сценариев (T-2.4).

Переносимые фейки слоя приложения появятся в T-2.9 — там они нужны, чтобы
запускать сценарии без инфраструктуры. Здесь только тестовые doubles: их дело —
дать сценарию проверяемое окружение, поэтому они помнят вызовы («провайдера звали
один раз») и ведут журнал шагов.

Фейки намеренно **не наследуют** порты. Наследование от ``Protocol`` сделало бы
проверку формы бессмысленной (недостающий метод молча унаследуется заглушкой), а
именно совпадение «по форме» — то, что требуется от настоящих адаптеров. Если
фейк не удовлетворяет порту, тест падает сам: это дешёвый способ не разойтись
с контрактом.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from src.core_service.application.ports.idempotency_store import IdempotencyRecord
from src.core_service.application.ports.lock_manager import (
    DEFAULT_LOCK_TTL_SECONDS,
    DEFAULT_LOCK_WAIT_SECONDS,
    DistributedLock,
)
from src.core_service.application.ports.payment_provider import ProviderResult, ProviderStatus
from src.core_service.application.use_cases.create_payment import CreatePaymentUseCase
from src.core_service.application.use_cases.get_payment import GetPaymentUseCase
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.exceptions import DuplicateOperation
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus

#: Фиксированное «сейчас» фейковых часов: тесты не должны зависеть от того, что
#: часы тикают, иначе проверки времени станут плавающими.
FROZEN_NOW = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)


class FakeClock:
    """Часы с замороженным временем: ``advance`` двигает их вручную."""

    def __init__(self, now: datetime = FROZEN_NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class Journal:
    """Порядок событий окружения: «сначала блокировка, потом провайдер».

    Самое ценное здесь — увидеть, *где* произошёл вызов провайдера: между
    открытием и закрытием транзакции БД он означал бы, что сага держит
    блокировки во время сетевого вызова (AGENT.md §4.2).
    """

    def __init__(self) -> None:
        self.entries: list[str] = []

    def record(self, entry: str) -> None:
        self.entries.append(entry)

    def __contains__(self, entry: object) -> bool:
        return entry in self.entries

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def position_of(self, entry: str) -> int:
        """Индекс первого вхождения: для проверок порядка шагов."""
        return self.entries.index(entry)


@dataclass
class InMemoryDatabase:
    """Хранилище, общее для всех единиц работы: коммит виден следующей транзакции.

    Реплик в БД не существует, поэтому и разделения данных между транзакциями
    не нужно. Важно лишь одно: изменения видны только после ``commit``.
    """

    accounts: dict[AccountId, Account] = field(default_factory=dict)
    payments: dict[PaymentId, Payment] = field(default_factory=dict)

    def snapshot(self) -> tuple[dict[AccountId, Account], dict[PaymentId, Payment]]:
        """Глубокая копия состояния, чтобы откат вернул всё как было.

        Сущности мутируемы, и репозиторий отдаёт объект из словаря: без копии
        откат «отменил» бы запись, но не изменение баланса внутри неё.
        """
        return deepcopy((self.accounts, self.payments))

    def restore(self, snapshot: tuple[dict[AccountId, Account], dict[PaymentId, Payment]]) -> None:
        self.accounts, self.payments = deepcopy(snapshot)


class FakeAccountRepository:
    """Репозиторий счетов поверх :class:`InMemoryDatabase`."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database
        self.get_for_update_calls: list[AccountId] = []

    async def get(self, account_id: AccountId) -> Account | None:
        return self._database.accounts.get(account_id)

    async def get_for_update(self, account_id: AccountId) -> Account | None:
        self.get_for_update_calls.append(account_id)
        return self._database.accounts.get(account_id)

    async def add(self, account: Account) -> None:
        self._database.accounts[account.id] = account

    async def update(self, account: Account) -> None:
        self._database.accounts[account.id] = account


class FakePaymentRepository:
    """Репозиторий платежей поверх :class:`InMemoryDatabase`."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database

    async def get(self, payment_id: PaymentId) -> Payment | None:
        return self._database.payments.get(payment_id)

    async def get_by_provider_payment_id(self, provider_payment_id: str) -> Payment | None:
        for payment in self._database.payments.values():
            if payment.provider_payment_id == provider_payment_id:
                return payment
        return None

    async def add(self, payment: Payment) -> None:
        self._database.payments[payment.id] = payment

    async def update(self, payment: Payment) -> None:
        self._database.payments[payment.id] = payment

    async def find_by_status(
        self,
        status: PaymentStatus,
        *,
        limit: int = 100,
        updated_before: datetime | None = None,
    ) -> Sequence[Payment]:
        found = [payment for payment in self._database.payments.values() if payment.status is status]
        if updated_before is not None:
            found = [payment for payment in found if payment.updated_at < updated_before]
        return sorted(found, key=lambda payment: payment.updated_at)[:limit]


class FakeUnitOfWork:
    """Единица работы с честной семантикой транзакции.

    Изменения не видны следующей транзакции, пока не выполнен ``commit``, а
    откат возвращает снимок: иначе тесты саги проверяли бы фейк, а не сценарий.
    """

    def __init__(self, database: InMemoryDatabase, journal: Journal) -> None:
        self._database = database
        self._journal = journal
        self._snapshot: tuple[dict[AccountId, Account], dict[PaymentId, Payment]] | None = None
        self.accounts = FakeAccountRepository(database)
        self.payments = FakePaymentRepository(database)
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        self._snapshot = self._database.snapshot()
        self._journal.record('uow.open')
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._journal.record('uow.close')
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        self.commits += 1
        self._journal.record('uow.commit')

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._journal.record('uow.rollback')
        if self._snapshot is not None:
            self._database.restore(self._snapshot)


class FakeDistributedLock:
    """Блокировка, выданная :class:`FakeLockManager`."""

    def __init__(self, manager: FakeLockManager, resource: str) -> None:
        self._manager = manager
        self._resource = resource
        self._token = f'token-{resource}'
        self._is_held = True

    @property
    def resource(self) -> str:
        return self._resource

    @property
    def token(self) -> str:
        return self._token

    @property
    def is_held(self) -> bool:
        return self._is_held

    async def release(self) -> None:
        # Повторный release не ошибка: так ведёт себя и Lua-скрипт адаптера.
        if not self._is_held:
            return
        self._is_held = False
        self._manager.held.discard(self._resource)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()


class FakeLockManager:
    """Менеджер блокировок в одном процессе.

    Повторный захват занятого ресурса копирует поведение Redis-адаптера
    (ЭПИК 5): поднимается доменная ошибка, а не «вечная блокировка».
    """

    def __init__(self, journal: Journal) -> None:
        self.held: set[str] = set()
        self.acquired_resources: list[str] = []
        self._journal = journal

    @asynccontextmanager
    async def _acquired(self, resource: str) -> AsyncIterator[FakeDistributedLock]:
        lock = await self.acquire(resource)
        if lock is None:
            raise DuplicateOperation(f'Ресурс {resource} уже заблокирован')
        try:
            yield lock
        finally:
            await self.release(lock)

    def lock(
        self,
        resource: str,
        *,
        ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> AbstractAsyncContextManager[FakeDistributedLock]:
        """Контекстный менеджер с точной сигнатурой порта (T-2.1).

        ``ttl_seconds``/``wait_seconds`` принимаются и игнорируются: в одном
        процессе ждать нечего, захват либо удаётся сразу, либо нет. Подпись
        повторяет порт буквально — иначе фейк проверял бы не тот контракт,
        который предъявит настоящий Redis-адаптер (ЭПИК 5).
        """
        del ttl_seconds, wait_seconds
        return self._acquired(resource)

    async def acquire(
        self,
        resource: str,
        *,
        ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> FakeDistributedLock | None:
        del ttl_seconds, wait_seconds
        if resource in self.held:
            return None
        self.held.add(resource)
        self.acquired_resources.append(resource)
        self._journal.record(f'lock.acquire:{resource}')
        return FakeDistributedLock(self, resource)

    async def release(self, handle: DistributedLock) -> None:
        # Параметр — именно тип порта, а не FakeDistributedLock: так же, как в
        # настоящем адаптере. Свойство resource есть у обоих, поэтому здесь
        # достаточно доступа к нему.
        self.held.discard(handle.resource)
        self._journal.record(f'lock.release:{handle.resource}')


class FakeIdempotencyStore:
    """Хранилище ответов в памяти: записи, резервации и их выдача.

    Резервация (``try_acquire``) и запись ответа разделены, как в Redis+Postgres
    (ЭПИК 4): резервация живёт в оперативной памяти, ответ — в базе.
    """

    def __init__(self, journal: Journal) -> None:
        self.records: dict[str, IdempotencyRecord] = {}
        self.reserved: set[str] = set()
        self.acquire_calls: list[str] = []
        self._journal = journal

    async def get(self, key: str) -> IdempotencyRecord | None:
        self._journal.record('idempotency.get')
        return self.records.get(key)

    async def save(self, record: IdempotencyRecord) -> None:
        self._journal.record('idempotency.save')
        self.records[record.key] = record
        self.reserved.discard(record.key)

    async def try_acquire(self, key: str, *, ttl_seconds: int) -> bool:
        # ttl_seconds в одноранговой среде не нужен: резервация живёт в памяти
        # процесса, и падение процесса её уносит автоматически. Параметр нужен
        # ради соответствия порту, поэтому явно помечается как неиспользуемый.
        del ttl_seconds
        self._journal.record('idempotency.acquire')
        self.acquire_calls.append(key)
        if key in self.reserved:
            return False
        self.reserved.add(key)
        return True

    async def release(self, key: str) -> None:
        self._journal.record('idempotency.release')
        self.reserved.discard(key)


class RecordingEventPublisher:
    """События в памяти — так удобно проверить, что в outbox попало нужное."""

    def __init__(self, journal: Journal) -> None:
        self.published: list[DomainEvent] = []
        self._journal = journal

    async def publish(self, event: DomainEvent) -> None:
        self._journal.record(f'event:{type(event).__name__}')
        self.published.append(event)

    def types_published(self) -> list[str]:
        return [type(event).__name__ for event in self.published]


class FakePaymentProvider:
    """Настраиваемый провайдер: успех, отказ по бизнесу, технический сбой.

    Исход задаётся полями, а не наследованием — каждый тест выражает нужный
    исход в одной строке. Провайдер заодно проверяет инвариант саги: запоминает,
    была ли в момент вызова открыта транзакция БД.
    """

    def __init__(
        self,
        *,
        status: ProviderStatus = ProviderStatus.PROCESSING,
        error: Exception | None = None,
        journal: Journal,
        uow_factory: FakeUnitOfWorkFactory | None = None,
        status_query: ProviderStatus | None = None,
        status_query_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.error = error
        # Ответ get_status отделён от ответа create_payment: у сценария чтения
        # (T-2.5) провайдер сначала принимает платёж, а потом (возможно, другим
        # вызовом) сообщает, чем всё закончилось. Одно поле на оба ответа не
        # позволяло бы выразить «создали успешно, а актуализация выявила отказ».
        self.status_query = status_query
        self.status_query_error = status_query_error
        self.calls: list[tuple[PaymentId, Money, str]] = []
        self.status_queries: list[str] = []
        self.open_uow_during_calls: list[bool] = []
        self.open_uow_during_queries: list[bool] = []
        self._journal = journal
        self._uow_factory = uow_factory

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _is_uow_open(self) -> bool:
        return self._uow_factory.open_units > 0 if self._uow_factory else False

    async def create_payment(
        self,
        *,
        payment_id: PaymentId,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResult:
        self.calls.append((payment_id, amount, idempotency_key))
        self.open_uow_during_calls.append(self._is_uow_open())
        self._journal.record('provider.create_payment')
        if self.error is not None:
            raise self.error
        return ProviderResult(provider_payment_id=f'provider-{payment_id}', status=self.status)

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        self.status_queries.append(provider_payment_id)
        self.open_uow_during_queries.append(self._is_uow_open())
        self._journal.record('provider.get_status')
        if self.status_query_error is not None:
            raise self.status_query_error
        # Если отдельный ответ не задан, провайдер сообщает тот же статус, что и
        # при создании: этого достаточно для теста «провайдер ещё обрабатывает».
        return self.status if self.status_query is None else self.status_query

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        # Сумма возврата здесь не влияет на результат: возврат всегда полный и
        # успешный. Настраиваемые исходы для refund появятся вместе с T-2.6.
        del amount
        return ProviderResult(
            provider_payment_id=f'refund-{provider_payment_id}',
            status=ProviderStatus.REFUNDED,
        )


@dataclass
class FakeUnitOfWorkFactory:
    """Фабрика новых единиц работы над общей базой.

    Отдельный объект, а не одна переиспользуемая транзакция: сага обязана
    открывать их две, иначе «промежуточный» коммит не имел бы смысла.
    """

    database: InMemoryDatabase
    journal: Journal
    units: list[FakeUnitOfWork] = field(default_factory=list)
    open_units: int = 0

    def __call__(self) -> FakeUnitOfWork:
        unit = _CountingUnitOfWork(self)
        self.units.append(unit)
        return unit

    @property
    def commits(self) -> int:
        return sum(unit.commits for unit in self.units)

    @property
    def rollbacks(self) -> int:
        return sum(unit.rollbacks for unit in self.units)

    @property
    def is_any_open(self) -> bool:
        return self.open_units > 0


class _CountingUnitOfWork(FakeUnitOfWork):
    """UoW, ведущая счётчик открытых транзакций в фабрике."""

    def __init__(self, factory: FakeUnitOfWorkFactory) -> None:
        super().__init__(factory.database, factory.journal)
        self._factory = factory

    async def __aenter__(self) -> Self:
        self._factory.open_units += 1
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._factory.open_units -= 1
        await super().__aexit__(exc_type, exc, tb)


@dataclass
class SagaEnvironment:
    """Готовое окружение сценария: база, порты и фабрика единиц работы."""

    database: InMemoryDatabase
    journal: Journal
    clock: FakeClock
    lock_manager: FakeLockManager
    idempotency_store: FakeIdempotencyStore
    event_publisher: RecordingEventPublisher
    uow_factory: FakeUnitOfWorkFactory
    provider: FakePaymentProvider

    @classmethod
    def build(
        cls,
        *,
        status: ProviderStatus = ProviderStatus.PROCESSING,
        error: Exception | None = None,
        status_query: ProviderStatus | None = None,
        status_query_error: Exception | None = None,
    ) -> Self:
        """Собирает окружение; ``status``/``error`` задают исход провайдера."""
        journal = Journal()
        database = InMemoryDatabase()
        uow_factory = FakeUnitOfWorkFactory(database=database, journal=journal)
        return cls(
            database=database,
            journal=journal,
            clock=FakeClock(),
            lock_manager=FakeLockManager(journal=journal),
            idempotency_store=FakeIdempotencyStore(journal=journal),
            event_publisher=RecordingEventPublisher(journal=journal),
            uow_factory=uow_factory,
            provider=FakePaymentProvider(
                status=status,
                error=error,
                journal=journal,
                uow_factory=uow_factory,
                status_query=status_query,
                status_query_error=status_query_error,
            ),
        )

    def add_account(self, account: Account) -> Account:
        self.database.accounts[account.id] = account
        return account

    def build_use_case(self) -> CreatePaymentUseCase:
        """Сценарий создания платежа, связанный с этим окружением."""
        return CreatePaymentUseCase(
            uow_factory=self.uow_factory,
            lock_manager=self.lock_manager,
            idempotency_store=self.idempotency_store,
            payment_provider=self.provider,
            event_publisher=self.event_publisher,
            clock=self.clock,
        )

    def build_get_use_case(self) -> GetPaymentUseCase:
        """Сценарий чтения платежа, связанный с этим окружением."""
        return GetPaymentUseCase(
            uow_factory=self.uow_factory,
            payment_provider=self.provider,
            event_publisher=self.event_publisher,
            clock=self.clock,
        )

    def account_balance(self, account_id: AccountId) -> Money:
        return self.database.accounts[account_id].balance

    def only_payment(self) -> Payment:
        """Единственный сохранённый платёж (в этих тестах он всегда один)."""
        assert len(self.database.payments) == 1, f'ожидался один платёж, найдено {len(self.database.payments)}'
        return next(iter(self.database.payments.values()))

    def stored_response(self, key: str) -> dict[str, Any]:
        record = self.idempotency_store.records.get(key)
        assert record is not None, f'нет записи по ключу {key}'
        assert record.response is not None, f'по ключу {key} сохранён ответ без тела'
        return dict(record.response)
