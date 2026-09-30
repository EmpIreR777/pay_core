"""Тесты сценария создания платежа (T-2.4).

DoD задачи — три исхода: успех, ошибка провайдера, дубликат ключа. Проверяется
не только результат, но и **порядок** шагов: главный риск саги — выполнить
сетевой вызов внутри транзакции БД (AGENT.md §4.2), и ни один финальный
ассерт этого не покажет. Поэтому фейки ведут журнал, а тесты читают его.

Особое внимание — идемпотентности в её финансовом смысле: повтор с тем же
ключом не должен ни создать второй платёж, ни списать сумму второй раз, даже
если первый запуск упал на внешнем вызове.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.core_service.application.dto import CreatePaymentInput, CreatePaymentOutput
from src.core_service.application.ports.idempotency_store import IdempotencyRecord
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.application.use_cases.create_payment import (
    IDEMPOTENCY_RECORD_TTL_SECONDS,
    _request_hash,
)
from src.core_service.application.use_cases.payment_sync import PROVIDER_REJECTION_REASON
from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import (
    AccountBlocked,
    CurrencyMismatchError,
    DuplicateOperation,
    EntityNotFoundError,
    InsufficientFunds,
    InvalidValueError,
    PaymentProviderError,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from tests.fakes import FROZEN_NOW, SagaEnvironment

KEY = 'idem-key-1'


def _input(
    account_id: AccountId,
    *,
    amount: Money | None = None,
    idempotency_key: str = KEY,
) -> CreatePaymentInput:
    return CreatePaymentInput(
        from_account_id=account_id,
        amount=amount or Money.from_number(Decimal('250.00'), Currency.RUB),
        idempotency_key=idempotency_key,
    )


@pytest.fixture
def account() -> Account:
    """Счёт плательщика с 1000 RUB на балансе."""
    return Account(
        account_id=AccountId.new(),
        balance=Money.from_number(Decimal('1000.00'), Currency.RUB),
    )


# --- DoD: успех --------------------------------------------------------------


async def test_successful_payment_debits_account_and_returns_processing(
    account: Account,
) -> None:
    """Успех: деньги списаны, платёж PROCESSING, клиенту выдан результат."""
    environment = SagaEnvironment.build(status=ProviderStatus.PROCESSING)
    environment.add_account(account)

    result = await environment.build_use_case().execute(_input(account.id))

    assert result.status is PaymentStatus.PROCESSING
    assert result.amount == Money.from_number(Decimal('250.00'), Currency.RUB)
    assert result.provider_payment_id == f'provider-{result.payment_id}'
    assert environment.account_balance(account.id) == Money.from_number(Decimal('750.00'), Currency.RUB)


async def test_payment_is_persisted_with_provider_identifier(account: object) -> None:
    """Платёж сохранён в БД и связан с операцией провайдера."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    result = await environment.build_use_case().execute(_input(account.id))

    payment = environment.only_payment()
    assert payment.id == result.payment_id
    assert payment.status is PaymentStatus.PROCESSING
    assert payment.provider_payment_id == f'provider-{result.payment_id}'
    assert payment.from_account_id == account.id


async def test_creation_publishes_payment_created_event(account: object) -> None:
    """В outbox попадает PaymentCreated — потребителям нужно знать о платеже."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.event_publisher.types_published() == ['PaymentCreated']


async def test_provider_receives_domain_types_and_client_key(account: object) -> None:
    """Провайдеру уходят наш PaymentId/Money и клиентский ключ идемпотентности."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    payment_id, amount, idempotency_key = environment.provider.calls[0]
    assert payment_id == environment.only_payment().id
    assert amount == Money.from_number(Decimal('250.00'), Currency.RUB)
    assert idempotency_key == KEY


# --- DoD: ошибка провайдера ---------------------------------------------------


async def test_business_rejection_marks_payment_failed(account: Account) -> None:
    """Бизнес-отказ (FAILED) — известный исход: платёж переходит в FAILED."""
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)

    result = await environment.build_use_case().execute(_input(account.id))

    assert result.status is PaymentStatus.FAILED
    assert environment.only_payment().status is PaymentStatus.FAILED
    assert environment.only_payment().failure_reason == PROVIDER_REJECTION_REASON


async def test_business_rejection_publishes_payment_failed_event(account: Account) -> None:
    """Отказ провайдера попадает в outbox отдельным событием."""
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.event_publisher.types_published() == ['PaymentCreated', 'PaymentFailed', 'PaymentRefunded']


async def test_business_rejection_returns_money_to_account(account: Account) -> None:
    """Отказ по существу ⇒ холд возвращается: клиент не потерял деньги.

    Шлюз отклонил операцию — деньги у него не забраны, значит и у нас они не должны
    пропасть. Без этого баланс клиента «съедал» бы сумму за платёж, который не прошёл.
    """
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.account_balance(account.id) == Money.from_number(Decimal('1000.00'), Currency.RUB)


async def test_business_rejection_returns_full_amount_to_account(account: Account) -> None:
    """Возвращается ровно та сумма, которая была списана (полный холд)."""
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)
    other_account = environment.add_account(
        Account(account_id=AccountId.new(), balance=Money.from_number(Decimal('1000.00'), Currency.RUB))
    )
    use_case = environment.build_use_case()

    await use_case.execute(_input(account.id, amount=Money.from_number(Decimal('300.00'), Currency.RUB)))
    await use_case.execute(
        _input(
            other_account.id, amount=Money.from_number(Decimal('120.00'), Currency.RUB), idempotency_key='idem-key-2'
        )
    )

    assert environment.account_balance(account.id) == Money.from_number(Decimal('1000.00'), Currency.RUB)
    assert environment.account_balance(other_account.id) == Money.from_number(Decimal('1000.00'), Currency.RUB)


async def test_business_rejection_returns_money_in_same_transaction(account: Account) -> None:
    """Возврат и терминальный статус фиксируются одним коммитом — полумер не бывает."""
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    # Две транзакции: первая — списание, вторая — возврат вместе со статусом FAILED.
    assert environment.uow_factory.commits == 2
    assert environment.uow_factory.units[1].accounts.get_for_update_calls == [account.id]


async def test_business_rejection_publishes_refund_event_with_amount(account: Account) -> None:
    """Событие возврата несёт сумму, чтобы потребители знали размер движения."""
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    refund = environment.event_publisher.published[-1]
    assert type(refund).__name__ == 'PaymentRefunded'
    assert refund.refunded_amount == Money.from_number(Decimal('250.00'), Currency.RUB)
    assert refund.reason == PROVIDER_REJECTION_REASON


async def test_immediate_success_settles_payment(account: Account) -> None:
    """SUCCEEDED сразу на создании — это SETTLED: шкалы переводятся явно."""
    environment = SagaEnvironment.build(status=ProviderStatus.SUCCEEDED)
    environment.add_account(account)

    result = await environment.build_use_case().execute(_input(account.id))

    assert result.status is PaymentStatus.SETTLED
    assert environment.event_publisher.types_published() == ['PaymentCreated', 'PaymentSettled']


async def test_provider_pending_leaves_payment_processing(account: Account) -> None:
    """Промежуточный ответ (PENDING) ждёт вебхука: терминальных событий нет."""
    environment = SagaEnvironment.build(status=ProviderStatus.PENDING)
    environment.add_account(account)

    result = await environment.build_use_case().execute(_input(account.id))

    assert result.status is PaymentStatus.PROCESSING
    assert environment.event_publisher.types_published() == ['PaymentCreated']


async def test_provider_refunded_settles_payment(account: Account) -> None:
    """REFUNDED у провайдера — тоже проведённый платёж, статус SETTLED."""
    environment = SagaEnvironment.build(status=ProviderStatus.REFUNDED)
    environment.add_account(account)

    result = await environment.build_use_case().execute(_input(account.id))

    assert result.status is PaymentStatus.SETTLED


async def test_technical_failure_keeps_payment_pending(account: Account) -> None:
    """Технический сбой: исход у провайдера неизвестен, платёж остаётся PENDING."""
    environment = SagaEnvironment.build(error=PaymentProviderError('провайдер недоступен'))
    environment.add_account(account)

    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(_input(account.id))

    payment = environment.only_payment()
    assert payment.status is PaymentStatus.PENDING
    assert payment.provider_payment_id is None


async def test_technical_failure_does_not_invent_terminal_status(account: Account) -> None:
    """При сбое не публикуется ни одного терминального события."""
    environment = SagaEnvironment.build(error=PaymentProviderError('таймаут шлюза'))
    environment.add_account(account)

    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(_input(account.id))

    assert environment.event_publisher.types_published() == ['PaymentCreated']


async def test_technical_failure_keeps_debited_money(account: Account) -> None:
    """Списание не откатывается: операция у провайдера, возможно, создана.

    И — ключевое отличие от бизнес-отказа — деньги **не возвращаются**: исход
    неизвестен, возврат здесь означал бы риск двойного возврата, если провайдер
    операцию всё-таки провёл. Разрулит сверка (T-2.10).
    """
    environment = SagaEnvironment.build(error=PaymentProviderError('сеть недоступна'))
    environment.add_account(account)

    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(_input(account.id))

    assert environment.account_balance(account.id) == Money.from_number(Decimal('750.00'), Currency.RUB)


async def test_technical_failure_emits_no_refund_event(account: Account) -> None:
    """При неизвестном исходе событие возврата не публикуется — деньги не двигались."""
    environment = SagaEnvironment.build(error=PaymentProviderError('обрыв соединения'))
    environment.add_account(account)

    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(_input(account.id))

    assert 'PaymentRefunded' not in environment.event_publisher.types_published()


# --- DoD: дубликат ключа идемпотентности --------------------------------------


async def test_repeat_request_returns_saved_response(account: Account) -> None:
    """Повтор с тем же ключом и телом возвращает тот же ответ (DoD)."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    first = await use_case.execute(_input(account.id))
    second = await use_case.execute(_input(account.id))

    assert second == first


async def test_repeat_request_does_not_create_second_payment(account: Account) -> None:
    """Главное финансовое свойство: повтор не создаёт второй платёж."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    await use_case.execute(_input(account.id))
    await use_case.execute(_input(account.id))

    assert len(environment.database.payments) == 1


async def test_repeat_request_does_not_debit_account_twice(account: Account) -> None:
    """И не списывает сумму второй раз — ради этого и нужна идемпотентность."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    await use_case.execute(_input(account.id))
    await use_case.execute(_input(account.id))

    assert environment.account_balance(account.id) == Money.from_number(Decimal('750.00'), Currency.RUB)


async def test_repeat_request_does_not_call_provider_again(account: Account) -> None:
    """Провайдер повторно не дёргается: операция уже заведена."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    await use_case.execute(_input(account.id))
    await use_case.execute(_input(account.id))

    assert environment.provider.call_count == 1


async def test_repeat_after_technical_failure_returns_same_payment(account: Account) -> None:
    """Повтор после сбоя провайдера не должен списать деньги ещё раз."""
    environment = SagaEnvironment.build(error=PaymentProviderError('шлюз недоступен'))
    environment.add_account(account)
    use_case = environment.build_use_case()

    with pytest.raises(PaymentProviderError):
        await use_case.execute(_input(account.id))
    environment.provider.error = None
    replayed = await use_case.execute(_input(account.id))

    assert replayed.payment_id == environment.only_payment().id
    assert len(environment.database.payments) == 1
    assert environment.account_balance(account.id) == Money.from_number(Decimal('750.00'), Currency.RUB)


async def test_same_key_with_different_body_is_rejected(account: Account) -> None:
    """Тот же ключ с другим телом — конфликт, а не повтор: чужой ответ не отдаём."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    await use_case.execute(_input(account.id))
    other_amount = Money.from_number(Decimal('10.00'), Currency.RUB)

    with pytest.raises(DuplicateOperation):
        await use_case.execute(_input(account.id, amount=other_amount))

    assert environment.account_balance(account.id) == Money.from_number(Decimal('750.00'), Currency.RUB)


async def test_concurrent_reservation_stops_second_run(account: Account) -> None:
    """Параллельный повтор получает отказ, а не второй платёж."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    environment.idempotency_store.reserved.add(KEY)

    with pytest.raises(DuplicateOperation):
        await environment.build_use_case().execute(_input(account.id))

    assert environment.provider.call_count == 0
    assert not environment.database.payments


async def test_reservation_is_taken_before_the_answer_is_read(account: Account) -> None:
    """Захват ключа идёт **до** чтения ответа — это и есть защита от гонки (T-4.3).

    Обратный порядок («прочитать, потом захватить») оставлял окно: ``save`` снимает
    захват, записав ответ, поэтому повтор успевал прочитать пустоту, а к моменту его
    ``try_acquire`` ключ уже был свободен — и дубль уходил в сагу вторым платежом.
    На живых Postgres и Redis это окно воспроизводится детерминированно
    (``tests/integration/test_create_payment_idempotency.py``); здесь порядок
    закреплён прямо, чтобы фейк поймал его за миллисекунды, без стенда.
    """
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    entries = list(environment.journal)
    assert entries.index('idempotency.acquire') < entries.index('idempotency.get')


async def test_replay_releases_the_reservation_it_took(account: Account) -> None:
    """Повтор отпускает захват, который взял сам: иначе ключ провисел бы до конца TTL.

    Прямое следствие порядка «захват, потом чтение»: под ключом уже есть ответ, и наш
    ``try_acquire`` проходит (предыдущий запуск отпустил ключ). Держать захват ради
    готового ответа незачем, а следующий повтор обязан получить этот же ответ, а не
    отказ «уже выполняется».
    """
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    await use_case.execute(_input(account.id))
    await use_case.execute(_input(account.id))

    assert KEY not in environment.idempotency_store.reserved


async def test_replay_does_not_release_a_reservation_it_did_not_take(account: Account) -> None:
    """Чужой захват повтор не снимает — это делает тот, кто его взял.

    Состояние достижимо: ``save`` пишет ответ в базу и только затем снимает захват, и
    повтор, пришедший в этот промежуток, читает уже готовый ответ, не имея захвата.
    Сбросить чужой захват здесь нельзя — следующий запрос по этому ключу прошёл бы в сагу
    по горячему следу, не имея никаких прав на ключ.
    """
    environment = SagaEnvironment.build()
    environment.add_account(account)
    data = _input(account.id)
    environment.idempotency_store.records[KEY] = IdempotencyRecord(
        key=KEY,
        request_hash=_request_hash(data),
        response=CreatePaymentOutput(
            payment_id=PaymentId.new(),
            status=PaymentStatus.PROCESSING,
            amount=Money.from_number(Decimal('250.00'), Currency.RUB),
            created_at=FROZEN_NOW,
            provider_payment_id='provider-already-done',
        ).to_idempotency_response(),
        created_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + timedelta(seconds=IDEMPOTENCY_RECORD_TTL_SECONDS),
    )
    environment.idempotency_store.reserved.add(KEY)

    await environment.build_use_case().execute(data)

    assert KEY in environment.idempotency_store.reserved
    assert environment.provider.call_count == 0


async def test_response_is_saved_with_ttl_and_clock_timestamps(account: Account) -> None:
    """Ответ хранится с метками часов порта и TTL жизни записи."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    record = environment.idempotency_store.records[KEY]
    assert record.key == KEY
    assert record.request_hash
    assert record.created_at == FROZEN_NOW
    assert record.expires_at == FROZEN_NOW + timedelta(seconds=IDEMPOTENCY_RECORD_TTL_SECONDS)


async def test_saved_response_restores_all_output_fields(account: Account) -> None:
    """Сохранённый ответ reconstitируется в полноценный DTO, а не в «хвост»."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    use_case = environment.build_use_case()

    first = await use_case.execute(_input(account.id))
    replayed = await use_case.execute(_input(account.id))

    assert replayed.amount == first.amount
    assert replayed.status == first.status
    assert replayed.provider_payment_id == first.provider_payment_id
    assert replayed.created_at == first.created_at


# --- Инварианты саги ---------------------------------------------------------


async def test_provider_is_called_outside_any_transaction(account: Account) -> None:
    """Ключевой инвариант: сетевой вызов не внутри транзакции БД (AGENT.md §4.2)."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.provider.open_uow_during_calls == [False]


async def test_provider_call_happens_after_first_commit(account: Account) -> None:
    """Порядок шагов: сначала коммит списания, потом обращение к провайдеру."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    entries = list(environment.journal)
    assert entries.index('uow.commit') < entries.index('provider.create_payment')


async def test_saga_uses_two_separate_transactions(account: Account) -> None:
    """Списание и обновление статуса — разные транзакции, а не одна длинная."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert len(environment.uow_factory.units) == 2
    assert environment.uow_factory.commits == 2


async def test_account_is_read_under_row_lock(account: Account) -> None:
    """Баланс читается под блокировкой строки — иначе гонка при параллельных списаниях."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.uow_factory.units[0].accounts.get_for_update_calls == [account.id]


async def test_lock_is_taken_on_payer_account(account: Account) -> None:
    """Блокируется именно счёт плательщика."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.lock_manager.acquired_resources == [f'account:{account.id}']


async def test_lock_is_released_after_success(account: Account) -> None:
    """Блокировка не залипает: иначе счёт заблокируется навсегда."""
    environment = SagaEnvironment.build()
    environment.add_account(account)

    await environment.build_use_case().execute(_input(account.id))

    assert environment.lock_manager.held == set()


async def test_lock_is_released_when_provider_fails(account: Account) -> None:
    """Сбой внешнего вызова тоже обязан освободить блокировку."""
    environment = SagaEnvironment.build(error=PaymentProviderError('обрыв шлюза'))
    environment.add_account(account)

    with pytest.raises(PaymentProviderError):
        await environment.build_use_case().execute(_input(account.id))

    assert environment.lock_manager.held == set()


async def test_lock_is_released_when_debit_fails(account: Account) -> None:
    """Доменный отказ при списании тоже освобождает ресурс."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    too_much = Money.from_number(Decimal('5000.00'), Currency.RUB)

    with pytest.raises(InsufficientFunds):
        await environment.build_use_case().execute(_input(account.id, amount=too_much))

    assert environment.lock_manager.held == set()


# --- Отказы домена на шаге списания -------------------------------------------


async def test_insufficient_funds_does_not_create_payment(account: Account) -> None:
    """Недостаточно средств: транзакция откатывается, платежа нет."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    too_much = Money.from_number(Decimal('5000.00'), Currency.RUB)

    with pytest.raises(InsufficientFunds):
        await environment.build_use_case().execute(_input(account.id, amount=too_much))

    assert not environment.database.payments
    assert environment.provider.call_count == 0


async def test_insufficient_funds_keeps_balance_untouched(account: Account) -> None:
    """Баланс не тронут: списание откатилось вместе с транзакцией."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    too_much = Money.from_number(Decimal('5000.00'), Currency.RUB)

    with pytest.raises(InsufficientFunds):
        await environment.build_use_case().execute(_input(account.id, amount=too_much))

    assert environment.account_balance(account.id) == Money.from_number(Decimal('1000.00'), Currency.RUB)


async def test_insufficient_funds_releases_idempotency_key(account: Account) -> None:
    """Ключ освобождён: операция не состоялась, значит повтор вправе дойти."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    too_much = Money.from_number(Decimal('5000.00'), Currency.RUB)
    use_case = environment.build_use_case()

    with pytest.raises(InsufficientFunds):
        await use_case.execute(_input(account.id, amount=too_much))

    assert environment.idempotency_store.reserved == set()
    result = await use_case.execute(_input(account.id))
    assert result.status is PaymentStatus.PROCESSING


async def test_missing_account_raises_domain_error(account: Account) -> None:
    """Счёта нет — это доменный отказ, а не «создадим платёж из воздуха»."""
    environment = SagaEnvironment.build()

    with pytest.raises(EntityNotFoundError):
        await environment.build_use_case().execute(_input(account.id))

    assert environment.provider.call_count == 0


async def test_blocked_account_is_rejected(account: Account) -> None:
    """Заблокированный счёт не двигает деньги."""
    environment = SagaEnvironment.build()
    account.block()
    environment.add_account(account)

    with pytest.raises(AccountBlocked):
        await environment.build_use_case().execute(_input(account.id))

    assert not environment.database.payments


async def test_currency_mismatch_is_rejected(account: Account) -> None:
    """Валюта суммы обязана совпадать с валютой счёта."""
    environment = SagaEnvironment.build()
    environment.add_account(account)
    usd = Money.from_number(Decimal('5.00'), Currency.USD)

    with pytest.raises(CurrencyMismatchError):
        await environment.build_use_case().execute(_input(account.id, amount=usd))

    assert not environment.database.payments


# --- Контракт сценария -------------------------------------------------------


def test_use_case_is_exported_from_application_layer() -> None:
    """Сценарий доступен из единой точки экспорта слоя, как и остальные DTO/порты."""
    from src.core_service.application import CreatePaymentUseCase

    assert CreatePaymentUseCase is not None
    assert (
        'CreatePaymentUseCase'
        in __import__(
            'src.core_service.application',
            fromlist=['__all__'],
        ).__all__
    )


async def test_execute_is_a_coroutine(account: Account) -> None:
    """Точка входа асинхронная: сценарий ждёт и БД, и сети."""
    import inspect

    from src.core_service.application import CreatePaymentUseCase

    assert inspect.iscoroutinefunction(CreatePaymentUseCase.execute)


def test_provider_error_is_a_domain_error() -> None:
    """Ошибка провайдера — доменная: транспорт маппит её сам, без try/except в сценарии."""
    from src.core_service.domain.exceptions import DomainError

    assert issubclass(PaymentProviderError, DomainError)


def test_entity_not_found_error_is_a_domain_error() -> None:
    """Отсутствие сущности — доменная ошибка, отличная от ошибки значения."""
    from src.core_service.domain.exceptions import DomainError

    assert issubclass(EntityNotFoundError, DomainError)
    assert not issubclass(EntityNotFoundError, InvalidValueError)


# --- Защитные ветки: сбой хранилища и незавершённый повтор -------------------


async def test_inflight_record_without_response_is_rejected(account: Account) -> None:
    """Запись по ключу есть, ответа нет: запрос ещё выполняется — повтор не пускаем.

    Хэш совпадает — иначе сработала бы защита от «другого тела», и нужная ветка
    не была бы проверена.
    """
    environment = SagaEnvironment.build()
    environment.add_account(account)
    data = _input(account.id)
    environment.idempotency_store.records[KEY] = IdempotencyRecord(
        key=KEY,
        request_hash=_request_hash(data),
        response=None,
        created_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + timedelta(seconds=IDEMPOTENCY_RECORD_TTL_SECONDS),
    )

    with pytest.raises(DuplicateOperation):
        await environment.build_use_case().execute(data)

    assert environment.provider.call_count == 0


async def test_payment_lost_between_transactions_is_reported(
    account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Платёж исчез между транзакциями — это ошибка, а не тихий «успех».

    Молчаливый успех здесь означал бы потерянные деньги: клиент получил бы
    результат по платежу, которого в базе уже нет. Сбой подменяется на вызове
    провайдера — к этому моменту первая транзакция уже зафиксировала платёж.
    """
    environment = SagaEnvironment.build()
    environment.add_account(account)
    original_create = environment.provider.create_payment

    async def create_then_lose_payment(**kwargs: object) -> Any:
        result = await original_create(**kwargs)  # type: ignore[arg-type]
        environment.database.payments.clear()
        return result

    monkeypatch.setattr(environment.provider, 'create_payment', create_then_lose_payment)

    with pytest.raises(EntityNotFoundError):
        await environment.build_use_case().execute(_input(account.id))


async def test_missing_account_during_refund_fails_loudly(
    account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Счёт исчез в момент возврата — падаем, а не «возвращаем в никуда»."""
    environment = SagaEnvironment.build(status=ProviderStatus.FAILED)
    environment.add_account(account)
    use_case = environment.build_use_case()
    original_create = environment.provider.create_payment

    async def create_then_lose_account(**kwargs: Any) -> Any:
        result = await original_create(**kwargs)  # type: ignore[arg-type]
        environment.database.accounts.clear()
        return result

    monkeypatch.setattr(environment.provider, 'create_payment', create_then_lose_account)

    with pytest.raises(EntityNotFoundError):
        await use_case.execute(_input(account.id))

    # Вторая транзакция откатилась: платёж остался PENDING, а не FAILED — «возврат
    # без статуса» и «статус без возврата» невозможны в принципе.
    assert environment.only_payment().status is PaymentStatus.PENDING
    assert environment.uow_factory.rollbacks == 1
    assert environment.event_publisher.types_published() == ['PaymentCreated']
