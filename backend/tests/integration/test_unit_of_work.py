"""Интеграционные тесты единицы работы на Postgres (T-3.5).

DoD задачи — «тест атомарности транзакции», и именно он здесь. Юнит-тесты
(`tests/unit/db/test_unit_of_work.py`) доказывают, что адаптер *вызывает* коммит
и откат в нужный момент; но вызовы `commit` и `rollback` дают одинаковое состояние
сессии, поэтому по ним нельзя отличить работающую атомарность от полностью
отсутствующей. Здесь атомарность доказывается единственным честным способом:
**что видит другая транзакция**.

Проверяется то, ради чего единица работы и написана, и что не проверяется ни
одним из предыдущих модулей:

* **«Списание холда + запись платежа» неделимо.** Это шаг 3 саги T-2.4: счёт
  уходит в минус и платёж появляется в одной транзакции. Проверяется с обеих
  сторон — что оба изменения видны после коммита и что оба исчезают после
  отката. Половина выполненной пары («списали, но платёж не создали») означает
  потерянные клиенту деньги, и никакой юнит-тест этого не увидит;
* **транзакция реально открыта, а не «вроде бы».** Проверяется настоящей
  блокировкой строки: пока транзакция открыта, чужая упирается в `FOR UPDATE`,
  а после выхода проходит свободно. Отпуск блокировки — тоже проверка границы:
  забытый откат оставил бы её висеть до конца соединения;
* **откат настоящий, а не «репозиторий ничего не записал».** Поэтому внутри
  транзакции сначала делается *проверяемое* изменение, и уже потом — отказ. Иначе
  тест прошёл бы и при полностью неработающей записи;
* **счёт и платёж пишутся одной сессией.** Иначе откат одного оставил бы второй
  зафиксированным, и сага развалилась бы на две независимые транзакции;
* **транзакции не текут соединениями.** Серия транзакций подряд обязана
  возвращать пул к нулю: утечка соединения проявляется на проде не сразу, а
  отказами обслуживать платежи под нагрузкой.

Стенд не разрушается: общая очистка таблиц живёт в `conftest` и вызывается
фикстурой модуля. Без Postgres-стенда тесты берут временный контейнер
(testcontainer, T-3.7), а без Docker вовсе — пропускаются: `make test` обязан
оставаться зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.db.repositories import PostgresAccountRepository, PostgresUnitOfWork
from tests.integration.conftest import (
    LOCK_WAIT_SECONDS,
    build_session_maker,
    clear_payment_data,
    open_transaction,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('postgres_stack')]

#: Баланс и сумма операции. Берутся из ``Decimal``, а не из ``float``: в деньгах
#: ``float`` недопустим, и тест обязан это демонстрировать, а не провоцировать
#: расхождение округления.
BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')

#: Сколько транзакций подряд прогоняет проверка утечки соединений. Число
#: произвольное, но не единица: одиночная транзакция вернула бы соединение и при
#: медленной утечке, и утечка обнаружилась бы на проде через часы работы.
LEAK_PROBE_TRANSACTIONS = 6

#: Размер пула по умолчанию в SQLAlchemy. Пустой пул после серии транзакций
#: остаётся меньше либо равен исходному размеру: если соединения не возвращались,
#: пул вырос бы за счёт ``max_overflow``.
DEFAULT_POOL_SIZE = 5


class _SagaFailureError(Exception):
    """Отказ сценария после того, как часть изменений уже записана.

    Суффикс ``Error`` обязателен (N818): тесты держат то же правило, что и
    production-код, иначе пришлось бы гасить линтер на каждом новом модуле.
    """


# --- Фикстуры -----------------------------------------------------------------


@pytest.fixture(scope='module', autouse=True)
def clean_tables() -> Iterator[None]:
    """Очистить рабочие таблицы до и после прогона модуля.

    Здесь, в отличие от тестов репозиториев, очистка обязательна по существу:
    единица работы **коммитит по-настоящему**, и оставленные ею строки пережили бы
    тест. Повторный прогон на них падал бы на первичном ключе и выглядел бы как
    поломка атомарности, а не как мусор от прошлого прогона.
    """
    asyncio.run(clear_payment_data())
    yield
    asyncio.run(clear_payment_data())


@dataclass(frozen=True)
class _Harness:
    """Фабрика единиц работы и пул, из которого она берёт соединения.

    Хранятся вместе не для удобства: проверка утечки соединения обязана смотреть
    в **тот самый** пул, из которого работает единица работы. Отдельный движок
    создал бы свой, всегда пустой пул, и тест проходил бы, ничего не проверяя.
    """

    build: Callable[[], PostgresUnitOfWork]
    pool: AsyncEngine


@pytest.fixture
def harness() -> Iterator[_Harness]:
    """Обвязка для транзакций: фабрика UoW и её пул.

    Движок освобождается после теста: он держит сокеты, и незакрытый пул заставил бы
    ``make test`` упираться в лимит файловых дескрипторов на длинных прогонах.
    """
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        yield _Harness(build=lambda: PostgresUnitOfWork(build_session_maker(engine)), pool=engine)
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture
def uow_factory(harness: _Harness) -> Callable[[], PostgresUnitOfWork]:
    """Фабрика единиц работы — ровно то, что внедряется в сценарии (T-2.4).

    Именно фабрика, а не один экземпляр: сага открывает транзакции по очереди
    (шаг 3, затем шаг 5), и общий экземпляр заставил бы держать вторую транзакцию
    в уже закрытой.
    """
    return harness.build


@pytest.fixture
def stored_account() -> Account:
    """Счёт, зафиксированный отдельной транзакцией: точка отсчёта для проверок."""
    account = _account()
    asyncio.run(_store_account(account))
    return account


# --- Хелперы ------------------------------------------------------------------


def _account(balance: Decimal = BALANCE) -> Account:
    """Доменный счёт для записи в хранилище."""
    return Account(account_id=AccountId.new(), balance=Money.from_number(balance, Currency.RUB))


def _payment(account: Account) -> Payment:
    """Новый платёж в ``PENDING`` по счёту плательщика."""
    return Payment.create(
        payment_id=PaymentId.new(),
        from_account_id=account.id,
        amount=Money.from_number(AMOUNT, account.currency),
    )


async def _store_account(account: Account) -> None:
    """Записать счёт своей транзакцией, минуя проверяемую единицу работы.

    Сырой ``INSERT``, а не репозиторий: точка отсчёта должна быть создана
    заведомо исправным путём, иначе поломка чтения была бы неотличима от поломки
    атомарности, которую мы и проверяем.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with build_session_maker(engine)() as session, session.begin():
            await session.execute(
                text(
                    'INSERT INTO accounts (id, currency, balance, is_blocked, version)'
                    ' VALUES (:id, :currency, :balance, false, 1)',
                ),
                {
                    'id': account.id.value,
                    'currency': account.currency.value,
                    'balance': account.balance.amount,
                },
            )
    finally:
        await engine.dispose()


async def _committed_balance(account_id: AccountId) -> Decimal | None:
    """Баланс, каким его видит **чужая** транзакция.

    Отдельное соединение обязательно: из сессии самой единицы работы увидно и
    несохранённое, и проверка молча ничего бы не утверждала.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text('SELECT balance FROM accounts WHERE id = :account_id'),
                {'account_id': account_id.value},
            )
            row = result.first()
            return None if row is None else row[0]
    finally:
        await engine.dispose()


async def _payment_exists(payment_id: PaymentId) -> bool:
    """Есть ли платёж в базе с точки зрения чужой транзакции."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text('SELECT 1 FROM payments WHERE id = :payment_id'),
                {'payment_id': payment_id.value},
            )
            return result.first() is not None
    finally:
        await engine.dispose()


async def _withdraw_and_add_payment(uow: PostgresUnitOfWork, account: Account, payment: Payment) -> None:
    """Списание холда и запись платежа в открытой транзакции — шаг 3 саги.

    Порядок именно такой, как в ``CreatePaymentUseCase._create_pending_payment``
    (T-2.4): сначала движение денег, затем платёж. Проверка отката после этого шага
    ловит ровно тот отказ, ради которого существует единица работы: деньги списаны,
    а платёж не заведён.
    """
    stored = await uow.accounts.get_for_update(account.id)
    assert stored is not None, 'счёт-эталон должен существовать'
    stored.withdraw(Money.from_number(AMOUNT, account.currency))
    await uow.accounts.update(stored)
    await uow.payments.add(payment)


async def _withdraw_and_create_payment(uow: PostgresUnitOfWork, account: Account) -> Payment:
    """Шаг 3 саги, платёж создаётся на месте.

    Отдельная от ``_withdraw_and_add_payment`` обёртка нужна там, где тесту
    достаточно самого факта успеха и идентификатор платежа не важен.
    """
    payment = _payment(account)
    await _withdraw_and_add_payment(uow, account, payment)
    return payment


async def _failing_step(
    uow_factory: Callable[[], PostgresUnitOfWork],
    account: Account,
    payment: Payment,
) -> None:
    """Шаг 3 саги, который падает **после** обеих записей.

    Вынесено отдельной функцией, а не телом прямо в тесте, по существенной
    причине: отказ обязан случиться уже после того, как списание и платёж записаны,
    иначе проверка отката прошла бы и при неработающем откате — откатывать было бы
    нечего.

    Платёж создаёт и передаёт вызывающий: тест обязан знать идентификатор строки,
    которой после отката не будет. Создавать его здесь и «возвращать через
    исключение» было бы враньём в типах — функция ничего не возвращает.
    """
    async with uow_factory() as uow:
        await _withdraw_and_add_payment(uow, account, payment)
        raise _SagaFailureError(f'сбой после обеих записей для счёта {account.id}')


async def _failing_duplicate_step(
    uow_factory: Callable[[], PostgresUnitOfWork],
    account: Account,
    duplicate: Payment,
) -> None:
    """Тот же шаг 3, но падает он **самой базой** на второй записи.

    Сначала списываются деньги, затем добавляется платёж, который уже лежит в
    таблице. Отказ прилетает из ограничения целостности внутри транзакции, и это
    второй по частоте случай на платёжном пути после отказа провайдера.
    Проверяется то же свойство — ранее записанное не выживает.

    Дубль передаётся **снаружи** и обязательно тем же объектом, что уже записан:
    фабрика ``_payment`` выдаёт новый ``PaymentId`` на каждый вызов, и второй
    «дубль» сгенерировал бы новый ключ — ограничение не сработало бы, а тест
    прошёл бы, ничего не проверив (та же ошибка, что была с первым вариантом теста
    дубля в T-3.4).
    """
    async with uow_factory() as uow:
        stored = await uow.accounts.get_for_update(account.id)
        assert stored is not None, 'счёт-эталон должен существовать'
        stored.withdraw(Money.from_number(AMOUNT, account.currency))
        await uow.accounts.update(stored)
        await uow.payments.add(duplicate)


async def _failing_lock_step(uow_factory: Callable[[], PostgresUnitOfWork], account: Account) -> None:
    """Взять блокировку строки и упасть, не записав ничего.

    Отдельный хелпер, потому что записей здесь нет вовсе: проверяется освобождение
    блокировки, а не откат данных, и смешивать эти два свойства в одном тесте
    означало бы, что непонятно, что именно сломалось.
    """
    async with uow_factory() as uow:
        await uow.accounts.get_for_update(account.id)
        raise _SagaFailureError(f'откат после блокировки счёта {account.id}')


# --- Атомарность (DoD) --------------------------------------------------------


async def test_commit_makes_debit_and_payment_visible_together(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """DoD: после коммита списание и платёж видны **обе** записи сразу.

    Проверяется пара «списание + платёж», а не по отдельности: по отдельности
    прошёл бы и репозиторий, коммитющий сам (T-3.3, T-3.4), и тогда сага
    «списать холд — не провести платёж» оставила бы деньги у клиента навсегда.
    """
    async with uow_factory() as uow:
        payment = await _withdraw_and_create_payment(uow, stored_account)
        await uow.commit()

    assert await _committed_balance(stored_account.id) == BALANCE - AMOUNT
    assert await _payment_exists(payment.id)


async def test_rollback_undoes_debit_and_payment_together(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """DoD: откат возвращает и баланс, и платёж в исходное состояние.

    Отказ поднимается **после** обеих записей. Это ключевая тонкость проверки:
    откат до первой записи прошёл бы и при неработающем откате вообще, а здесь
    тест видит именно то, что вернулось не всё, — то есть деньги, списанные без
    платежа.
    """
    payment = _payment(stored_account)
    with pytest.raises(_SagaFailureError):
        await _failing_step(uow_factory, stored_account, payment)

    assert await _committed_balance(stored_account.id) == BALANCE
    assert not await _payment_exists(payment.id)


async def test_nothing_is_visible_before_commit(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Внутри транзакции изменения ещё не видны снаружи.

    Обратная сторона предыдущих двух проверок: вместе они доказывают, что
    граница транзакции существует в обе стороны, а не только «в конце откатили».
    """
    async with uow_factory() as uow:
        payment = await _withdraw_and_create_payment(uow, stored_account)

        assert await _committed_balance(stored_account.id) == BALANCE
        assert not await _payment_exists(payment.id)


async def test_rollback_from_database_error_undoes_earlier_writes(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Отказ самой базы откатывает всё, записанное до него.

    Отличается от предыдущего теста источником отказа: там его поднимает сценарий,
    здесь — ограничение целостности на втором запросе. Для платёжного шлюза это
    второй по частоте случай (дубль ключа, превышение лимита), и он не должен
    оставлять счёт списанным.
    """
    existing = _payment(stored_account)
    async with uow_factory() as uow:
        await uow.payments.add(existing)
        await uow.commit()

    with pytest.raises(DBAPIError):
        await _failing_duplicate_step(uow_factory, stored_account, existing)

    assert await _committed_balance(stored_account.id) == BALANCE


# --- Границы транзакции -------------------------------------------------------


async def test_row_lock_is_held_inside_the_context_and_released_on_exit(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Транзакция действительно открыта: блокировка строки держится до выхода.

    Это проверка «настоящей» транзакции, а не ``flush``-а в пустоту. Пока контекст
    открыт, чужое соединение обязано упереться в ``FOR UPDATE``; после выхода —
    пройти свободно и увидеть новое значение. Второе важно отдельно: забытый
    откат держал бы блокировку до конца соединения, и платёжный шлюз встал бы
    колом на первом же конкурентном списании.

    Чужая сторона — обычный репозиторий поверх сессии ``open_transaction``, а не
    вторая единица работы: её собственная транзакция спрятала бы проверку внутри
    адаптера, который и проверяется.
    """
    async with uow_factory() as uow:
        stored = await uow.accounts.get_for_update(stored_account.id)
        assert stored is not None
        stored.deposit(Money.from_number(AMOUNT, stored.currency))
        await uow.accounts.update(stored)

        async with open_transaction(lock_wait_seconds=LOCK_WAIT_SECONDS) as second:
            with pytest.raises(DBAPIError, match=r'lock timeout|canceling statement'):
                await PostgresAccountRepository(second).get_for_update(stored_account.id)

    assert await _committed_balance(stored_account.id) == BALANCE + AMOUNT


async def test_rollback_releases_the_row_lock(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Откат отпускает блокировку строки, а не только отменяет записи.

    Сначала блокировка берётся, затем транзакция откатывается — и чужая обязана
    пройти свободно. Без отката (или с зависшей транзакцией) она упёрлась бы в
    таймаут, и это был бы уже отказ шлюза, а не ошибка теста.
    """
    with pytest.raises(_SagaFailureError):
        await _failing_lock_step(uow_factory, stored_account)

    async with open_transaction(lock_wait_seconds=LOCK_WAIT_SECONDS) as second:
        locked = await PostgresAccountRepository(second).get_for_update(stored_account.id)

    assert locked is not None
    assert locked.balance == Money.from_number(BALANCE, Currency.RUB)


# --- Связь транзакций между собой ---------------------------------------------


async def test_next_transaction_sees_the_committed_step(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Следующая транзакция видит уже зафиксированный шаг — как шаг 5 саги.

    Моделируется ровно последовательность T-2.4: шаг 3 коммитит списание, между
    шагами ходит провайдер, шаг 5 открывает **новую** единицу работы и читает
    платёж заново. Если бы сессия переиспользовалась, вторая транзакция увидела бы
    не те данные — а на платёжном пути это значит потерянное обновление статуса.
    """
    async with uow_factory() as uow:
        payment = await _withdraw_and_create_payment(uow, stored_account)
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.payments.get(payment.id)

    assert stored is not None
    assert stored.from_account_id == stored_account.id
    assert await _committed_balance(stored_account.id) == BALANCE - AMOUNT


async def test_rolled_back_step_is_invisible_to_the_next_transaction(
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Откатанный шаг не «просачивается» в следующую транзакцию.

    Отличается от предыдущего теста тем, что читает уже **откатанное** состояние.
    Именно это нужно саге: повтор запроса с тем же ключом идемпотентности обязан
    начаться с чистого состояния (T-2.4), иначе клиент получил бы платёж, за
    который деньги уже списаны, — или наоборот, счёт со списанными деньгами без
    платежа.
    """
    payment = _payment(stored_account)
    with pytest.raises(_SagaFailureError):
        await _failing_step(uow_factory, stored_account, payment)

    async with uow_factory() as uow:
        found = await uow.payments.get(payment.id)
        balance = await uow.accounts.get(stored_account.id)

    assert found is None
    assert balance is not None
    assert balance.balance == Money.from_number(BALANCE, Currency.RUB)


# --- Соединения ---------------------------------------------------------------


async def test_transactions_do_not_leak_connections(
    harness: _Harness,
    uow_factory: Callable[[], PostgresUnitOfWork],
    stored_account: Account,
) -> None:
    """Транзакции не держат соединения после выхода — ни успешные, ни откатанные.

    Утечка соединения — самая коварная поломка в шлюзе: она не выглядит ошибкой и
    проявляется через минуты под нагрузкой, когда пул исчерпан и платежи
    отказывают один за другим. Поэтому проверяется много последовательных
    транзакций, а не одна: одиночная утечка дала бы «1 в простое» и ничего не
    сказала бы о накоплении.

    Проверяется **свойство, важное в проде** (пул возвращается к нулю), а не сам
    вызов ``close``: коммит и откат уже возвращают соединение, поэтому чувствителен
    к ``close()`` юнит-тест, сверяющий порядок вызовов сессии.
    """
    for attempt in range(LEAK_PROBE_TRANSACTIONS):
        if attempt % 2 == 0:
            async with uow_factory() as uow:
                assert await uow.accounts.get(stored_account.id) is not None
                assert _checked_out(harness) == 1, 'транзакция обязана держать ровно одно соединение'
        else:
            with pytest.raises(_SagaFailureError):
                await _failing_lock_step(uow_factory, stored_account)
        assert _checked_out(harness) == 0, f'после транзакции {attempt} соединение не вернулось в пул'

    assert harness.pool.pool.size() <= DEFAULT_POOL_SIZE, 'пул разросшился: соединения не возвращались'


def _checked_out(harness: _Harness) -> int:
    """Сколько соединений занято пулом обвязки прямо сейчас.

    Пул опрашивается синхронно и ничего не открывает, поэтому дополнительный
    движок не нужен; ``getattr`` — потому что у пула без проверки соединений
    (``NullPool``) такого счётчика просто нет.
    """
    checked_out = getattr(harness.pool.pool, 'checkedout', None)
    return 0 if checked_out is None else int(checked_out())
