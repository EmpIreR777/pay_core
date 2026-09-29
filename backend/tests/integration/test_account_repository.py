"""Интеграционные тесты репозитория счетов на Postgres (T-3.3).

DoD задачи — «интеграционный тест репозитория». Юнит-тесты маппера (T-3.3) доказывают
только форму преобразования; здесь проверяется поведение, ради которого репозиторий
и написан и чего в схеме не видно:

* **изоляция транзакции.** Репозиторий не коммитит: `add`/`update` обязаны быть
  не видны второй транзакции до `commit` `UnitOfWork` и исчезать после отката.
  Репозиторий, который фиксирует изменения сам, разорвал бы атомарность саги
  (списание холда + запись платежа), и юнит-тест этого не поймал бы никогда.
* **настоящая блокировка `SELECT ... FOR UPDATE`.** Ключевое свойство `get_for_update`
  проверяется двумя живыми соединениями: вторая транзакция обязана упереться в
  блокировку первой, а после её завершения — увидеть новое значение. Никаких
  `sleep()`: ожидание сделано через `lock_timeout`, и тест падает, если блокировки
  нет, вместо того чтобы пройти «по счастливому случаю».
* **соблюдение ограничений базы.** Дубликат первичного ключа и код валюты вне шкалы
  отсекаются самой БД. Репозиторий не дублирует эти проверки — и потому его
  молчание о них означает ровно то, что написано в схеме.
* **округление до точности валюты.** `NUMERIC(27,3)` обслуживает все валюты сразу;
  иена без минорной единицы обязана читаться как целое, а кувей — с тремя знаками.
  Это проверка того, что валюту несёт значение `Money`, а не число знаков колонки.

Стенд не разрушается: каждый тест откатывает свою транзакцию, а таблица чистится
хелпером, поэтому прогон оставляет базу в том же виде, в каком её нашёл. Без
Postgres модуль пропускается — `make test` обязан оставаться зелёным без Docker
(AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.config import settings
from src.core_service.application import ports
from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import EntityNotFoundError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.versioning import INITIAL_VERSION
from src.db.models.account import AccountModel
from src.db.repositories import PostgresAccountRepository, new_account_model
from tests.integration.conftest import LOCK_WAIT_SECONDS, capture_sql, open_transaction

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('postgres_stack')]

#: Баланс и сумма операции в тестах. Берутся из ``Decimal``, а не из ``float``:
#: в деньгах ``float`` недопустим, и тест обязан это демонстрировать, а не
#: провоцировать расхождение округления.
BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')


@pytest.fixture(scope='module', autouse=True)
def clean_accounts_table() -> AsyncIterator[None]:
    """Очистить таблицу ``accounts`` до и после прогона модуля.

    Откат транзакции в каждом тесте оставляет базу чистой сам по себе, но упавший
    тест на ``add`` может оставить строку за пределами своей транзакции (например,
    если ошибка прилетела уже после ``commit``). Повторный запуск на таких данных
    падал бы на первичном ключе и выглядел бы как поломка репозитория, а не как
    мусор от прошлого прогона.
    """

    async def truncate() -> None:
        engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
        try:
            async with engine.begin() as connection:
                # payments ссылается на accounts с ON DELETE RESTRICT, поэтому
                # при появлении платежей чистить придётся обе таблицы.
                await connection.execute(delete(AccountModel))
        finally:
            await engine.dispose()

    asyncio.run(truncate())
    yield
    asyncio.run(truncate())


@pytest.fixture
def repository(session: AsyncSession) -> PostgresAccountRepository:
    """Репозиторий поверх сессии фикстуры: ровно так его поднимет UoW (T-3.5)."""
    return PostgresAccountRepository(session)


def repository_for(session: AsyncSession) -> PostgresAccountRepository:
    """Репозиторий над произвольной сессией.

    Отдельная функция, а не только фикстура: проверки блокировки работают на
    нескольких независимых сессиях сразу, и такой «репозиторий по аргументу» там
    читается честнее, чем плодить фикстуры под каждую транзакцию.
    """
    return PostgresAccountRepository(session)


def _account(currency: Currency = Currency.RUB, balance: Decimal = BALANCE) -> Account:
    """Доменный счёт для записи в хранилище."""
    return Account(account_id=AccountId.new(), balance=Money.from_number(balance, currency))


async def _stored_balance(session: AsyncSession, account_id: AccountId) -> Decimal | None:
    """Прочитать баланс **сырым SQL** из сессии вызывающего, минуя репозиторий.

    Так проверка не зависит от самого адаптера: иначе сломанный ``get`` не смог бы
    обнаружить, что именно он же и записал не то. Читание идёт из той же транзакции,
    что и запись, — иначе несохранённые данные были бы не видны и проверка ничего
    не сказала бы о том, что легло в колонку.
    """
    result = await session.execute(
        text('SELECT balance FROM accounts WHERE id = :account_id'),
        {'account_id': account_id.value},
    )
    row = result.first()
    return None if row is None else row[0]


async def _committed_balance(account_id: AccountId) -> Decimal | None:
    """Прочитать баланс из **чужого** соединения — как его увидит другая транзакция."""
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


async def _stored_block_flag(account_id: AccountId) -> bool | None:
    """Прочитать признак блокировки сырым SQL — как его увидит другая транзакция."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text('SELECT is_blocked FROM accounts WHERE id = :account_id'),
                {'account_id': account_id.value},
            )
            row = result.first()
            return None if row is None else row[0]
    finally:
        await engine.dispose()


# --- Форма контракта ----------------------------------------------------------


def test_repository_satisfies_the_port() -> None:
    """Адаптер отвечает порту ``AccountRepository`` «по форме» (T-2.1).

    Проверяется и то, что порт **не** наследуется: наследование от ``Protocol``
    подмешивало бы заглушки и сделало бы проверку самоподтверждающейся.
    """
    assert isinstance(PostgresAccountRepository(AsyncSession()), ports.AccountRepository)
    assert ports.AccountRepository not in PostgresAccountRepository.__mro__


# --- Чтение -------------------------------------------------------------------


async def test_get_returns_none_for_unknown_account(repository: PostgresAccountRepository) -> None:
    """Отсутствующий счёт — это ``None``, а не исключение.

    Сценарий сам различает «счёта нет» (и отдаёт клиенту понятный отказ) и «счёт
    есть»: репозиторию бросать исключение из ``SELECT`` значило бы превращать
    нормальный ответ хранилища в ошибку.
    """
    assert await repository.get(AccountId.new()) is None


async def test_get_for_update_returns_none_for_unknown_account(repository: PostgresAccountRepository) -> None:
    """То же для ``get_for_update``: несуществующую строку блокировать нечего."""
    assert await repository.get_for_update(AccountId.new()) is None


async def test_get_returns_saved_account_with_full_state(repository: PostgresAccountRepository) -> None:
    """Счёт, прочитанный из базы, совпадает с записанным во всех полях.

    Проверяется полный набор, а не только баланс: потерянный при маппинге флаг
    блокировки или версия выглядели бы как «счёт открыт» и «оптимистичный лок не
    сработал» — то есть отказом денежного характера, а не опечаткой.
    """
    saved = _account()
    await repository.add(saved)

    loaded = await repository.get(saved.id)

    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.balance == saved.balance
    assert loaded.currency is Currency.RUB
    assert loaded.is_blocked is False
    assert loaded.version == saved.version == INITIAL_VERSION


async def test_get_preserves_currency_scale_of_each_currency(repository: PostgresAccountRepository) -> None:
    """Иена читается как целое, кувей — с тремя знаками.

    Колонка ``balance`` одна на все валюты (``NUMERIC(27,3)``), поэтому точность
    может уехать из ``Money`` только в одну сторону: ``100.000`` иен не должно
    читаться как дробная сумма. Валюту несёт значение, а не число знаков колонки,
    и тест падает, если это перестанет быть так.
    """
    yen = _account(Currency.JPY, Decimal(100))
    dinar = _account(Currency.KWD, Decimal('1.234'))
    await repository.add(yen)
    await repository.add(dinar)

    loaded_yen = await repository.get(yen.id)
    loaded_dinar = await repository.get(dinar.id)

    assert loaded_yen is not None
    assert loaded_yen.balance == Money.from_number(100, Currency.JPY)
    assert loaded_dinar is not None
    assert loaded_dinar.balance == Money.from_number('1.234', Currency.KWD)
    assert loaded_dinar.currency is Currency.KWD


# --- Запись -------------------------------------------------------------------


async def test_add_stores_balance_and_currency(
    session: AsyncSession,
    repository: PostgresAccountRepository,
) -> None:
    """``add`` кладёт в базу ровно то, что отдал домен.

    Читается сырым SQL: если бы проверка шла через ``get``, она доказала бы только
    согласованность адаптера с самим собой, а не то, что в денежной колонке лежат
    деньги. Ожидается именно ``1000.000``, а не ``1000.00``: колонка ``NUMERIC(27,3)``
    приводит значение к своей точности, и именно это значение потом читает ``Money``.
    """
    saved = _account()
    await repository.add(saved)

    assert await _stored_balance(session, saved.id) == Decimal('1000.000')


async def test_add_is_not_visible_to_other_transactions_before_commit(
    repository: PostgresAccountRepository,
) -> None:
    """Ключевое свойство: репозиторий **не коммитит**.

    Если бы ``add`` фиксировал транзакцию сам, откат на уровне ``UnitOfWork``
    перестал бы откатывать, и сага «списать холд — не провести платёж — вернуть»
    оставила бы деньги списанными навсегда. Видимость из чужого соединения —
    единственное наблюдение, которое это доказывает.
    """
    saved = _account()
    await repository.add(saved)

    assert await _committed_balance(saved.id) is None


async def test_add_becomes_visible_after_commit(
    session: AsyncSession,
    repository: PostgresAccountRepository,
) -> None:
    """После ``commit`` запись видна другим транзакциям — граница транзакции на месте.

    Обратная сторона предыдущей проверки: вместе они доказывают, что репозиторий
    пишет в транзакцию вызывающего, а не заводит свою.
    """
    saved = _account()
    await repository.add(saved)
    await session.commit()

    assert await _committed_balance(saved.id) == Decimal('1000.000')


async def test_add_rejects_duplicate_account_id(repository: PostgresAccountRepository) -> None:
    """Дубль отсекает первичный ключ самой базы.

    Репозиторий не дублирует эту проверку: две реализации одного правила разойдутся
    (код проверит что-то одно, ``CHECK`` — другое), и отказ придёт не там, где его
    ждали. Второй счёт строится с **тем же** идентификатором: фабрика ``_account``
    выдаёт новый UUID, и дубля в такой проверке не было бы вовсе.
    """
    saved = _account()
    await repository.add(saved)
    duplicate = Account(account_id=saved.id, balance=Money.from_number(Decimal('50.00'), Currency.RUB))

    with pytest.raises(IntegrityError):
        await repository.add(duplicate)


async def test_add_rejects_currency_outside_the_scale(session: AsyncSession) -> None:
    """Код валюты, которого нет в ``Currency``, база не принимает.

    ``CHECK`` в схеме — единственная линия обороны: репозиторию негде взять список
    валют шлюза, не продублировав его у домена. Поэтому проверяется поведение самой
    БД на строке, собранной мимо репозитория.
    """
    model = new_account_model(_account())
    model.currency = 'XXX'
    session.add(model)

    with pytest.raises(IntegrityError):
        await session.flush()


# --- Обновление ---------------------------------------------------------------


async def test_update_saves_balance_block_flag_and_version(repository: PostgresAccountRepository) -> None:
    """``update`` записывает баланс, блокировку и версию, которые выставил домен.

    Версия проверяется отдельно от баланса: она кормит оптимистичный лок T-3.6, и
    потерянная при маппинге версия сделала бы несуществующую защиту от гонки
    неотличимой от работающей.
    """
    saved = _account()
    await repository.add(saved)
    saved.deposit(Money.from_number(AMOUNT, Currency.RUB))
    saved.block()

    await repository.update(saved)
    reloaded = await repository.get(saved.id)

    assert reloaded is not None
    assert reloaded.balance == Money.from_number(BALANCE + AMOUNT, Currency.RUB)
    assert reloaded.is_blocked is True
    assert reloaded.version == saved.version == INITIAL_VERSION + 2


async def test_update_keeps_currency_unchanged(repository: PostgresAccountRepository) -> None:
    """Валюта при обновлении не переписывается — она неизменна по инварианту домена."""
    saved = _account()
    await repository.add(saved)
    saved.deposit(Money.from_number(AMOUNT, Currency.RUB))

    await repository.update(saved)
    reloaded = await repository.get(saved.id)

    assert reloaded is not None
    assert reloaded.currency is Currency.RUB


async def test_update_of_missing_account_raises_entity_not_found(
    repository: PostgresAccountRepository,
) -> None:
    """Сохранение несуществующего счёта — отказ, а не тихий успех.

    Молчание здесь означало бы потерянные деньги: сценарий решил, что пополнил счёт,
    а база об изменении не узнала. Отказ всплывает там, где вызывающий умеет его
    обработать, вместо тихой потери денег на следующем шаге.
    """
    with pytest.raises(EntityNotFoundError, match='не найден'):
        await repository.update(_account())


async def test_update_is_not_visible_to_other_transactions_before_commit(
    session: AsyncSession,
    repository: PostgresAccountRepository,
) -> None:
    """``update`` тоже не коммитит: граница транзакции принадлежит ``UnitOfWork``."""
    saved = _account()
    await repository.add(saved)
    await session.commit()
    saved.withdraw(Money.from_number(AMOUNT, Currency.RUB))

    await repository.update(saved)

    assert await _committed_balance(saved.id) == BALANCE


# --- Блокировка строки -------------------------------------------------------


async def test_get_for_update_blocks_another_transaction_until_first_commits() -> None:
    """DoD: ``get_for_update`` действительно блокирует строку.

    Проверяется двумя одновременными транзакциями: вторая обязана упереться в
    ``FOR UPDATE`` первой и после её фиксации увидеть новое значение.

    Счёт заранее **зафиксирован**, и это не подготовка «для красоты»: незакоммиченный
    ``INSERT`` для чужой транзакции невидим, и ``SELECT ... FOR UPDATE`` по такому
    id вернул бы ноль строк без всякого ожидания. Тест, построенный на только что
    добавленном счёте, прошёл бы, ни разу не проверив блокировку.

    Ожидание сделано через ``lock_timeout``, а не через ``sleep``: тест, в котором
    блокировки нет, обязан упасть сразу, а не пройти по счастливому случаю.
    """
    account = _account()
    async with open_transaction() as setup:
        await repository_for(setup).add(account)
        await setup.commit()

    async with open_transaction() as first:
        repository = repository_for(first)
        locked = await repository.get_for_update(account.id)
        assert locked is not None
        locked.deposit(Money.from_number(AMOUNT, Currency.RUB))
        await repository.update(locked)

        # Вторая транзакция обязана упереться в блокировку первой.
        async with open_transaction(lock_wait_seconds=LOCK_WAIT_SECONDS) as second:
            with pytest.raises(DBAPIError, match=r'lock timeout|canceling statement'):
                await repository_for(second).get_for_update(account.id)

        # Свидетель: до коммита изменения не видны никому.
        assert await _committed_balance(account.id) == BALANCE
        await first.commit()

    assert await _committed_balance(account.id) == BALANCE + AMOUNT


async def test_second_transaction_sees_committed_balance_after_lock_is_released() -> None:
    """Дождавшись блокировки, второй сценарий читает новое значение, а не старое.

    Это и есть смысл пессимистичной блокировки: прочитанный баланс и записанный
    относятся к одной и той же версии строки. Без блокировки второй сценарий
    успевал бы решить по устаревшему балансу, и одно из двух списаний потерялось бы.
    """
    account = _account()
    async with open_transaction() as first:
        await repository_for(first).add(account)
        locked = await repository_for(first).get_for_update(account.id)
        assert locked is not None
        locked.withdraw(Money.from_number(AMOUNT, Currency.RUB))
        await repository_for(first).update(locked)
        await first.commit()

    async with open_transaction(lock_wait_seconds=LOCK_WAIT_SECONDS) as second:
        seen = await repository_for(second).get_for_update(account.id)

    assert seen is not None
    assert seen.balance == Money.from_number(BALANCE - AMOUNT, Currency.RUB)


# --- Контракт SQL -------------------------------------------------------------


async def test_get_for_update_issues_exactly_one_locking_select(
    session: AsyncSession,
    repository: PostgresAccountRepository,
) -> None:
    """``get_for_update`` — это ровно один ``SELECT ... FOR UPDATE`` и никаких обновлений.

    Проверяется текст SQL, а не результат: репозиторий, который дополнительно
    обновляет строку или читает её дважды, вернул бы те же данные и прошёл бы
    поведенческие тесты, а на платёжном пути это лишние блокировки и обмены с БД.
    """
    account = _account()
    await repository.add(account)
    await session.commit()

    statements = await capture_sql(session, repository.get_for_update(account.id))

    assert len(statements) == 1
    assert statements[0].startswith('SELECT accounts.id')
    assert 'FROM accounts' in statements[0]
    assert statements[0].endswith('FOR UPDATE')


async def test_plain_get_does_not_lock_the_row(
    session: AsyncSession,
    repository: PostgresAccountRepository,
) -> None:
    """``get`` читает без ``FOR UPDATE``: блокировка без нужды строки не должна.

    Наивная реализация, в которой оба чтения идут одним и тем же запросом,
    поведенческими тестами поймана не будет: результат тот же, а платить пришлось бы
    ожиданием чужой транзакции на каждом чтении счёта — включая отмену и разбор
    платежа, где баланс не меняется.
    """
    account = _account()
    await repository.add(account)
    await session.commit()

    statements = await capture_sql(session, repository.get(account.id))

    assert len(statements) == 1
    assert 'FOR UPDATE' not in statements[0]
