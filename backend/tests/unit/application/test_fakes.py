"""Тесты переносимых фейков портов (T-2.9).

Фейки обязаны уметь две вещи: **формально** отвечать контрактам портов и
**практически** давать прогнать все юзкейсы эпика без внешней инфры. Отсюда
три группы тестов:

* **Форма** — каждый фейк проходит ``isinstance`` с ``runtime_checkable``-
  протоколом и при этом порту не наследуется: наследование от ``Protocol``
  сделало бы такую проверку самоподтверждающейся (недостающий метод
  унаследовался бы заглушкой);
* **Настраиваемость провайдера** — success/failure/delay: исход каждого вызова
  задаётся полем, а задержка идёт по часам теста, а не по реальному времени;
* **DoD** — создание, чтение, поток, вебхук и отмена проходят на одном
  ``SagaEnvironment``: ни БД, ни Redis, ни шлюза за спиной сценария.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.core_service.application import ports
from src.core_service.application.dto import (
    CancelPaymentInput,
    CreatePaymentInput,
    GetPaymentInput,
    HandleProviderWebhookInput,
)
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import PaymentProviderError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from tests.fakes import (
    FROZEN_NOW,
    FakeAccountRepository,
    FakeClock,
    FakeDistributedLock,
    FakeIdempotencyStore,
    FakeLockManager,
    FakePaymentProvider,
    FakePaymentRepository,
    FakeSleeper,
    FakeUnitOfWorkFactory,
    InMemoryDatabase,
    Journal,
    RecordingEventPublisher,
    SagaEnvironment,
)

BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')


def _money(value: Decimal) -> Money:
    return Money.from_number(value, Currency.RUB)


def _fake_port_pairs() -> list[tuple[object, type]]:
    """Каждый фейк — рядом со своим портом: форма сверяется по парам."""
    journal = Journal()
    database = InMemoryDatabase()
    clock = FakeClock()
    return [
        (clock, ports.Clock),
        (FakeAccountRepository(database), ports.AccountRepository),
        (FakePaymentRepository(database), ports.PaymentRepository),
        (FakeUnitOfWorkFactory(database=database, journal=journal)(), ports.UnitOfWork),
        (FakeLockManager(journal=journal), ports.LockManager),
        (FakeDistributedLock(FakeLockManager(journal=journal), 'account:1'), ports.DistributedLock),
        (FakeIdempotencyStore(journal=journal), ports.IdempotencyStore),
        (RecordingEventPublisher(journal=journal), ports.EventPublisher),
        (FakePaymentProvider(journal=journal), ports.PaymentProvider),
    ]


# --- Форма: контракты портов -------------------------------------------------


def test_fakes_satisfy_their_ports_structurally() -> None:
    """Фейк проходит контракт порта через ``isinstance`` — совпадение «по форме»."""
    for fake, port in _fake_port_pairs():
        assert isinstance(fake, port), f'{type(fake).__name__} не проходит контракт {port.__name__}'


def test_fakes_do_not_inherit_their_ports() -> None:
    """Наследования от портов нет: ``Protocol`` не подмешивает заглушку метода."""
    for fake, port in _fake_port_pairs():
        assert not any(base is port for base in type(fake).__mro__), (
            f'{type(fake).__name__} не должен наследоваться от {port.__name__}'
        )


# --- Настраиваемость провайдера: success/failure/delay -----------------------


async def test_provider_success_is_the_default_response() -> None:
    """По умолчанию провайдер успешен: create, опрос и возврат отвечают штатно."""
    journal = Journal()
    provider = FakePaymentProvider(journal=journal)
    payment_id = PaymentId.new()

    created = await provider.create_payment(payment_id=payment_id, amount=_money(AMOUNT), idempotency_key='dod-key')

    assert created.status is ProviderStatus.PROCESSING
    assert created.provider_payment_id == f'provider-{payment_id}'
    assert await provider.get_status(created.provider_payment_id) is ProviderStatus.PROCESSING

    refunded = await provider.refund(created.provider_payment_id, _money(AMOUNT))
    assert refunded.status is ProviderStatus.REFUNDED
    assert 'provider.refund' in journal


async def test_provider_failure_is_configurable_per_operation() -> None:
    """Отказ задаётся полем: падает ровно тот вызов, которому назначен исход."""
    provider = FakePaymentProvider(
        journal=Journal(),
        error=PaymentProviderError('create недоступен'),
        status_query_error=PaymentProviderError('опрос недоступен'),
        refund_error=PaymentProviderError('возврат недоступен'),
    )

    with pytest.raises(PaymentProviderError, match='create'):
        await provider.create_payment(payment_id=PaymentId.new(), amount=_money(AMOUNT), idempotency_key='key')
    with pytest.raises(PaymentProviderError, match='опрос'):
        await provider.get_status('prov-1')
    with pytest.raises(PaymentProviderError, match='возврат'):
        await provider.refund('prov-1', _money(AMOUNT))


async def test_provider_delay_waits_on_test_clock_not_wall_clock() -> None:
    """Задержка — это сдвиг часов теста: реальное время в сценарии не участвует."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    provider = FakePaymentProvider(journal=Journal(), delay=1.5, sleeper=sleeper)

    created = await provider.create_payment(
        payment_id=PaymentId.new(),
        amount=_money(AMOUNT),
        idempotency_key='dod-key',
    )
    await provider.get_status(created.provider_payment_id)

    assert sleeper.delays == [1.5, 1.5]
    assert clock.now() == FROZEN_NOW + timedelta(seconds=3.0)


def test_provider_delay_requires_sleeper() -> None:
    """Задержка без sleeper означала бы wall-clock сон — это ошибка конфигурации."""
    with pytest.raises(ValueError, match='sleeper'):
        FakePaymentProvider(journal=Journal(), delay=0.5)


def test_provider_rejects_negative_delay() -> None:
    """Отрицательная задержка — опечатка в настройке, а не «ускоренный» ответ."""
    with pytest.raises(ValueError, match='отрицательной'):
        FakePaymentProvider(journal=Journal(), delay=-1.0)


async def test_environment_build_passes_delay_and_sleeper_to_provider() -> None:
    """``SagaEnvironment.build(delay=...)`` делит с провайдером часы и сон окружения."""
    environment = SagaEnvironment.build(delay=2.0)

    await environment.provider.get_status('prov-1')

    assert environment.sleeper.delays == [2.0]
    assert environment.clock.now() == FROZEN_NOW + timedelta(seconds=2.0)


# --- DoD: запуск юзкейсов без внешней инфры -----------------------------------


async def test_all_epic_use_cases_run_without_external_infrastructure() -> None:
    """DoD T-2.9: все пять сценариев эпика проходят на одних фейках.

    Ни БД, ни Redis, ни платёжного шлюза за спиной сценария: сага создания,
    чтение с актуализацией, поток статусов, вебхук и отмена работают поверх
    одного ``SagaEnvironment``. Проверяется и главный инвариант саги — провайдер
    вызывается вне транзакций БД, а транзакции все закрыты.
    """
    environment = SagaEnvironment.build()
    account = Account(account_id=AccountId.new(), balance=_money(BALANCE))
    environment.add_account(account)

    # Создание: сага списывает холд и зовёт провайдера вне транзакции.
    created = await environment.build_use_case().execute(
        CreatePaymentInput(from_account_id=account.id, amount=_money(AMOUNT), idempotency_key='dod-key'),
    )
    assert created.status is PaymentStatus.PROCESSING

    # Поток статусов: первый кадр отдан без действий между чтениями.
    stream = environment.build_watch_use_case().execute(GetPaymentInput(payment_id=created.payment_id))
    first_frame = await stream.__anext__()
    await stream.aclose()
    assert first_frame.status is PaymentStatus.PROCESSING

    # Чтение: провайдер подтверждает PROCESSING, статус не меняется.
    read = await environment.build_get_use_case().execute(GetPaymentInput(payment_id=created.payment_id))
    assert read.status is PaymentStatus.PROCESSING

    # Вебхук шлюза закрывает платёж, повторное чтение видит терминальный статус.
    assert created.provider_payment_id is not None
    settled = await environment.build_webhook_use_case().execute(
        HandleProviderWebhookInput(
            provider_event_id='dod-settle',
            provider_payment_id=created.provider_payment_id,
            provider_status=ProviderStatus.SUCCEEDED,
        )
    )
    assert settled.status is PaymentStatus.SETTLED
    reread = await environment.build_get_use_case().execute(GetPaymentInput(payment_id=created.payment_id))
    assert reread.status is PaymentStatus.SETTLED

    # Второй платёж: технический сбой оставляет PENDING — клиент идёт отменять.
    environment.provider.error = PaymentProviderError('провайдер недоступен')
    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(
            CreatePaymentInput(from_account_id=account.id, amount=_money(AMOUNT), idempotency_key='dod-key-2'),
        )
    environment.provider.error = None
    pending = next(p for p in environment.database.payments.values() if p.status is PaymentStatus.PENDING)
    cancelled = await environment.build_cancel_use_case().execute(
        CancelPaymentInput(payment_id=pending.id, reason='DoD T-2.9'),
    )
    assert cancelled.status is PaymentStatus.CANCELLED
    # Холд второго платежа вернулся, удержанный первым — остался списанным.
    assert environment.account_balance(account.id) == _money(Decimal('750.00'))

    # Провайдер звали только вне транзакций и все транзакции закрыты.
    assert environment.provider.open_uow_during_calls == [False, False]
    assert not environment.uow_factory.is_any_open
