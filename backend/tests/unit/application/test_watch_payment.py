"""Тесты сценария потока статусов платежа (T-2.8).

DoD задачи — «unit-тест генератора», поэтому проверяется именно то, что делает
генератор, а не сценарий чтения за ним: порядок кадров, границы потока и его
поведение, пока потребитель разбирает очередной кадр.

Группы тестов отвечают на разные вопросы:

* **Базовый поток (DoD)** — первый кадр несёт текущее состояние, каждая смена
  статуса становится кадром, терминальный платёж закрывает поток сразу;
* **Срок и дубли** — поток конечен по сроку и не шлёт кадры без изменений;
* **Границы и ошибки** — платёж отсутствует, исчез или провайдер упал:
  честная ошибка, а не «тишина» в стриме;
* **Деньги и актуализация** — поток читает через T-2.5, поэтому доезжает и
  отказ провайдера с возвратом холда, и запись вебхука;
* **Инварианты потока** — транзакция не живёт между кадрами, отключение
  клиента закрывает генератор чисто;
* **Контракт сценария** — генераторность точки входа и экспорт из слоя.
"""

from contextlib import aclosing
from datetime import timedelta
from decimal import Decimal

import pytest

from src.core_service.application.dto import (
    CreatePaymentInput,
    GetPaymentInput,
    GetPaymentOutput,
    HandleProviderWebhookInput,
)
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.application.use_cases.payment_sync import PROVIDER_REJECTION_REASON
from src.core_service.application.use_cases.watch_payment import (
    WATCH_MAX_DURATION_SECONDS,
    WATCH_POLL_INTERVAL_SECONDS,
)
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import EntityNotFoundError, PaymentProviderError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from tests.fakes import FROZEN_NOW, SagaEnvironment

BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')


def _money(value: Decimal) -> Money:
    return Money.from_number(value, Currency.RUB)


@pytest.fixture
def account() -> Account:
    return Account(account_id=AccountId.new(), balance=_money(BALANCE))


def _input(payment: Payment) -> GetPaymentInput:
    return GetPaymentInput(payment_id=payment.id)


async def _processing_payment(environment: SagaEnvironment, account: Account) -> Payment:
    """Создаёт платёж настоящей сагой T-2.4: создание у провайдера прошло.

    Ручная сборка платежа с нужным статусом тестировала бы не то, что работает
    в бою: у настоящего пути есть идентификатор операции у провайдера и
    списанный холд, без которых поток читал бы другое состояние.
    """
    environment.add_account(account)
    await environment.build_use_case().execute(
        CreatePaymentInput(
            from_account_id=account.id,
            amount=_money(AMOUNT),
            idempotency_key='setup-key',
        )
    )
    return environment.only_payment()


async def _pending_payment(environment: SagaEnvironment, account: Account) -> Payment:
    """Сага с техническим сбоем провайдера: деньги списаны, статус остался PENDING.

    Сбой исхода неизвестен, поэтому сага поднимает ошибку наружу (контракт
    T-2.4) — падение здесь ожидаемо: платёж уже создан первой транзакцией, и
    это ровно то состояние, в котором он живёт до вебхука или отмены.
    """
    environment.provider.error = PaymentProviderError('провайдер недоступен')
    environment.add_account(account)
    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(
            CreatePaymentInput(
                from_account_id=account.id,
                amount=_money(AMOUNT),
                idempotency_key='setup-key',
            )
        )
    return environment.only_payment()


async def _collect(environment: SagaEnvironment, payment: Payment) -> list[GetPaymentOutput]:
    """Прочитывает поток до конца — для случаев, когда действий между кадрами нет."""
    return [frame async for frame in environment.build_watch_use_case().execute(_input(payment))]


# --- Базовый поток (DoD) ------------------------------------------------------


async def test_first_frame_carries_current_state(account: Account) -> None:
    """Первый кадр — текущее состояние платежа, а не «ждите изменений»."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    stream = environment.build_watch_use_case().execute(_input(payment))

    frame = await stream.__anext__()
    await stream.aclose()

    assert frame.payment_id == payment.id
    assert frame.status is PaymentStatus.PROCESSING
    assert frame.amount == _money(AMOUNT)
    assert frame.from_account_id == account.id
    assert frame.created_at == payment.created_at
    assert frame.updated_at == payment.updated_at
    assert frame.provider_payment_id == payment.provider_payment_id


async def test_status_change_is_streamed_then_stream_completes(account: Account) -> None:
    """Смена статуса между чтениями становится вторым кадром и закрывает поток."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    frames: list[GetPaymentOutput] = []
    async for frame in environment.build_watch_use_case().execute(_input(payment)):
        frames.append(frame)
        if frame.status is PaymentStatus.PROCESSING:
            payment.settle()
            environment.database.payments[payment.id] = payment

    assert [frame.status for frame in frames] == [PaymentStatus.PROCESSING, PaymentStatus.SETTLED]
    assert len(environment.sleeper.delays) == 1


async def test_every_status_change_is_a_frame(account: Account) -> None:
    """Каждая смена статуса видна клиенту по порядку: PENDING → PROCESSING → SETTLED."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)

    frames: list[GetPaymentOutput] = []
    async for frame in environment.build_watch_use_case().execute(_input(payment)):
        frames.append(frame)
        if frame.status is PaymentStatus.PENDING:
            payment.process('provider-777')
            environment.database.payments[payment.id] = payment
        elif frame.status is PaymentStatus.PROCESSING:
            payment.settle()
            environment.database.payments[payment.id] = payment

    assert [frame.status for frame in frames] == [
        PaymentStatus.PENDING,
        PaymentStatus.PROCESSING,
        PaymentStatus.SETTLED,
    ]
    # Ровно один запрос провайдера — на кадре PROCESSING: PENDING спрашивать
    # некого, а к моменту третьего кадра платёж уже терминален.
    assert len(environment.provider.status_queries) == 1


async def test_terminal_payment_yields_single_frame_without_waiting(account: Account) -> None:
    """Терминальный платёж: один кадр, ни одной паузы, провайдер не трогаем."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.PENDING)
    payment = await _processing_payment(environment, account)
    payment.settle()
    environment.database.payments[payment.id] = payment

    frames = await _collect(environment, payment)

    assert [frame.status for frame in frames] == [PaymentStatus.SETTLED]
    assert environment.sleeper.delays == []
    assert environment.provider.status_queries == []


async def test_execute_is_lazy_until_first_frame(account: Account) -> None:
    """Тело генератора не выполняется до первого кадра: вызов ещё ничего не читает."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    opened_before = len(environment.uow_factory.units)

    stream = environment.build_watch_use_case().execute(_input(payment))
    assert len(environment.uow_factory.units) == opened_before

    frame = await stream.__anext__()
    await stream.aclose()

    assert frame.status is PaymentStatus.PROCESSING
    # Ровно одна транзакция на одно чтение — и она закрыта до кадра.
    assert len(environment.uow_factory.units) == opened_before + 1


# --- Срок и дубли -------------------------------------------------------------


async def test_stream_stops_at_deadline_and_stays_read_only(account: Account) -> None:
    """Поток с неизменившимся платежом закрывается по сроку и ничего не пишет."""
    environment = SagaEnvironment.build()
    payment = await _pending_payment(environment, account)
    commits_before = environment.uow_factory.commits
    version_before = payment.version

    frames = await _collect(environment, payment)

    assert [frame.status for frame in frames] == [PaymentStatus.PENDING]
    expected_sleeps = int(WATCH_MAX_DURATION_SECONDS / WATCH_POLL_INTERVAL_SECONDS)
    assert len(environment.sleeper.delays) == expected_sleeps
    assert set(environment.sleeper.delays) == {WATCH_POLL_INTERVAL_SECONDS}
    assert environment.clock.now() == FROZEN_NOW + timedelta(seconds=WATCH_MAX_DURATION_SECONDS)
    # Поток читает, а не пишет: ни одного лишнего коммита и нетронутая версия строки.
    assert environment.uow_factory.commits == commits_before
    assert payment.version == version_before
    assert not environment.uow_factory.is_any_open


async def test_unchanged_status_does_not_produce_frames(account: Account) -> None:
    """300 тиков без изменений — один кадр, а провайдер спрошен на каждом тике."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    frames = await _collect(environment, payment)

    assert [frame.status for frame in frames] == [PaymentStatus.PROCESSING]
    assert len(environment.provider.status_queries) == len(environment.sleeper.delays) + 1


# --- Границы и ошибки ---------------------------------------------------------


async def test_missing_payment_fails_before_first_frame(account: Account) -> None:
    """Нет платежа — ошибка на первом кадре, а не пустой поток «всё хорошо»."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    stream = environment.build_watch_use_case().execute(GetPaymentInput(payment_id=PaymentId.new()))
    opened_before = len(environment.uow_factory.units)

    with pytest.raises(EntityNotFoundError):
        await stream.__anext__()

    assert len(environment.uow_factory.units) == opened_before + 1
    assert environment.sleeper.delays == []


async def test_payment_vanishing_mid_stream_fails(account: Account) -> None:
    """Платёж исчез между кадрами — поток падает, а не продолжает врать состоянием."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    stream = environment.build_watch_use_case().execute(_input(payment))

    first = await stream.__anext__()
    assert first.status is PaymentStatus.PROCESSING
    environment.database.payments.clear()

    with pytest.raises(EntityNotFoundError):
        await stream.__anext__()


async def test_provider_error_aborts_stream(account: Account) -> None:
    """Сбой провайдера при актуализации обрывает поток честной ошибкой."""
    environment = SagaEnvironment.build(status_query_error=PaymentProviderError('шлюз недоступен'))
    payment = await _processing_payment(environment, account)

    with pytest.raises(PaymentProviderError):
        [frame async for frame in environment.build_watch_use_case().execute(_input(payment))]


# --- Деньги и актуализация ----------------------------------------------------


async def test_provider_settlement_reaches_client(account: Account) -> None:
    """Провайдер завершил операцию — клиент видит SETTLED на первом же кадре."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.SUCCEEDED)
    payment = await _processing_payment(environment, account)

    frames = await _collect(environment, payment)

    assert [frame.status for frame in frames] == [PaymentStatus.SETTLED]
    assert environment.provider.status_queries == [payment.provider_payment_id]
    assert environment.sleeper.delays == []
    # Успешный платёж оставляет деньги списанными: холд не «вернулся» сам.
    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)
    assert environment.event_publisher.types_published().count('PaymentSettled') == 1


async def test_provider_rejection_returns_hold(account: Account) -> None:
    """Отказ провайдера, увиденный в потоке, возвращает клиенту сумму."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.FAILED)
    payment = await _processing_payment(environment, account)
    assert environment.account_balance(account.id) == _money(BALANCE - AMOUNT)

    frames = await _collect(environment, payment)

    assert [frame.status for frame in frames] == [PaymentStatus.FAILED]
    assert frames[0].failure_reason == PROVIDER_REJECTION_REASON
    assert environment.account_balance(account.id) == _money(BALANCE)
    assert environment.event_publisher.types_published().count('PaymentFailed') == 1
    assert 'PaymentRefunded' in environment.event_publisher.types_published()


async def test_webhook_change_is_streamed(account: Account) -> None:
    """Вебхук, закрывший платёж между чтениями, доходит до клиента вторым кадром."""
    environment = SagaEnvironment.build(status_query=ProviderStatus.PROCESSING)
    payment = await _processing_payment(environment, account)
    assert payment.provider_payment_id is not None

    frames: list[GetPaymentOutput] = []
    async for frame in environment.build_watch_use_case().execute(_input(payment)):
        frames.append(frame)
        if frame.status is PaymentStatus.PROCESSING:
            await environment.build_webhook_use_case().execute(
                HandleProviderWebhookInput(
                    provider_event_id='yookassa-event-1',
                    provider_payment_id=payment.provider_payment_id,
                    provider_status=ProviderStatus.SUCCEEDED,
                )
            )

    assert [frame.status for frame in frames] == [PaymentStatus.PROCESSING, PaymentStatus.SETTLED]
    # Событие записано ровно один раз — вебхуком, а не ещё и потоком.
    assert environment.event_publisher.types_published().count('PaymentSettled') == 1


# --- Инварианты потока --------------------------------------------------------


async def test_no_transaction_open_while_consumer_processes_frame(account: Account) -> None:
    """Пока потребитель держит кадр, открытых транзакций нет — стрим не занимает базу."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    frames: list[GetPaymentOutput] = []
    async for frame in environment.build_watch_use_case().execute(_input(payment)):
        assert not environment.uow_factory.is_any_open
        frames.append(frame)
        if frame.status is PaymentStatus.PROCESSING:
            payment.settle()
            environment.database.payments[payment.id] = payment

    assert len(frames) == 2
    assert not environment.uow_factory.is_any_open


async def test_consumer_disconnect_closes_stream_cleanly(account: Account) -> None:
    """Клиент отключился после первого кадра: генератор закрыт, пауза не запланирована."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)

    async with aclosing(environment.build_watch_use_case().execute(_input(payment))) as stream:
        async for _frame in stream:
            break

    assert not environment.uow_factory.is_any_open
    assert environment.sleeper.delays == []


# --- Контракт сценария --------------------------------------------------------


def test_execute_is_an_async_generator_function() -> None:
    """Точка входа именно генератор (DoD): поток кадров, а не одиночный ответ."""
    import inspect

    from src.core_service.application import WatchPaymentUseCase

    assert inspect.isasyncgenfunction(WatchPaymentUseCase.execute)


def test_use_case_is_exported_from_application_layer() -> None:
    """Сценарий доступен из единой точки экспорта слоя, как и остальные."""
    from src.core_service.application import WatchPaymentUseCase

    assert WatchPaymentUseCase is not None
    assert (
        'WatchPaymentUseCase'
        in __import__(
            'src.core_service.application',
            fromlist=['__all__'],
        ).__all__
    )


async def test_frame_is_the_read_dto(account: Account) -> None:
    """Кадр — тот же DTO, что и чтение: стриму нужны метки, счёт и причина отказа."""
    environment = SagaEnvironment.build()
    payment = await _processing_payment(environment, account)
    stream = environment.build_watch_use_case().execute(_input(payment))

    frame = await stream.__anext__()
    await stream.aclose()

    assert isinstance(frame, GetPaymentOutput)
    assert frame.failure_reason is None
