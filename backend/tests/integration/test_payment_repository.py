"""Интеграционные тесты репозитория платежей на Postgres (T-3.4).

DoD задачи — «интеграционные тесты». Юнит-тесты маппера доказывают только форму
преобразования; здесь проверяется поведение, ради которого репозиторий и написан
и чего в схеме не видно:

* **изоляция транзакции.** Репозиторий не коммитит: ``add``/``update`` обязаны
  быть не видны второй транзакции до ``commit`` ``UnitOfWork``. Репозиторий,
  который фиксирует изменения сам, разорвал бы атомарность саги «списать холд —
  создать платёж», и юнит-тест этого не поймал бы никогда.
* **валюта читается у счёта.** В таблице платежа колонки с валютой нет, и каждое
  чтение — джойн с ``accounts``. Тест проверяет, что платёж по счёту в иенах
  читается в иенах: ошибка здесь выглядит правдоподобно (число то же), но
  считает деньги в чужой денежной единице.
* **согласованная строка после ``update``.** ``updated_at`` вычислила база, и
  после обновления ORM помечает атрибут просроченным: маппер читает метки
  синхронно, и лишний ``refresh`` здесь означал бы обмен на каждый переход
  статуса. Проверяется на живом Postgres, потому что юнит-тест не воспроизводит
  ни identity map сессии, ни состояние просроченного атрибута.
* **выборка по статусу — это запрос сверки.** Порядок «от старых к новым»,
  предел и отсечка по ``updated_at`` проверяются на данных с явно заданными
  метками: ``now()`` в Postgres — это время начала транзакции, поэтому все
  платежи, записанные в одной транзакции, получили бы одинаковую метку и
  проверить порядок было бы нечем.
* **соблюдение ограничений базы.** Дубликат первичного ключа, чужой счёт,
  неположительная сумма, статус вне шкалы и повторный идентификатор операции у
  провайдера отсекаются самой БД. Репозиторий эти проверки намеренно не
  дублирует — и потому его молчание о них означает ровно то, что написано в
  схеме.

Стенд не разрушается: тесты откатывают свои транзакции, а таблицы чистятся
хелпером, поэтому прогон оставляет базу в том же виде. Без Postgres-стенда тесты
берут временный контейнер (testcontainer, T-3.7), а без Docker вовсе —
пропускаются: ``make test`` обязан оставаться зелёным на машине без Docker
(AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Row, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.config import settings
from src.core_service.application import ports
from src.core_service.domain.entities.account import Account
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import EntityNotFoundError, InvalidValueError, OptimisticLockError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from src.core_service.domain.versioning import INITIAL_VERSION
from src.db.models.account import AccountModel
from src.db.models.payment import PaymentModel
from src.db.repositories import PostgresPaymentRepository, new_payment_model
from tests.integration.conftest import capture_sql, open_transaction

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('postgres_stack')]

#: Баланс и сумма платежа. Берутся из ``Decimal``, а не из ``float``: в деньгах
#: ``float`` недопустим, и тест обязан это демонстрировать, а не провоцировать
#: расхождение округления.
BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')

#: Момент, от которого считаются искусственные метки ``updated_at``. Фиксирован,
#: чтобы «зависшие» и «свежие» платежи не зависели от того, когда идёт прогон.
STALE_AT = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)


@pytest.fixture(scope='module', autouse=True)
def clean_payments_tables() -> AsyncIterator[None]:
    """Очистить ``payments`` и ``accounts`` до и после прогона модуля.

    Откат транзакции в каждом тесте оставляет базу чистой сам по себе, но упавший
    тест на ``add`` может оставить строку за пределами своей транзакции (например,
    если ошибка прилетела уже после ``commit``). Повторный прогон на таких данных
    падал бы на первичном ключе и выглядел бы как поломка репозитория, а не как
    мусор от прошлого прогона. Порядок обязателен: ``payments`` ссылается на
    ``accounts`` с ``ON DELETE RESTRICT``.
    """

    async def truncate() -> None:
        engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
        try:
            async with engine.begin() as connection:
                await connection.execute(delete(PaymentModel))
                await connection.execute(delete(AccountModel))
        finally:
            await engine.dispose()

    asyncio.run(truncate())
    yield
    asyncio.run(truncate())


@pytest.fixture
def repository(session: AsyncSession) -> PostgresPaymentRepository:
    """Репозиторий поверх сессии фикстуры: ровно так его поднимет UoW (T-3.5)."""
    return PostgresPaymentRepository(session)


def repository_for(session: AsyncSession) -> PostgresPaymentRepository:
    """Репозиторий над произвольной сессией.

    Отдельная функция, а не только фикстура: проверки видимости работают на
    нескольких независимых транзакциях сразу, и такой «репозиторий по аргументу»
    там читается честнее, чем плодить фикстуры под каждую транзакцию.
    """
    return PostgresPaymentRepository(session)


def _account(currency: Currency = Currency.RUB, balance: Decimal = BALANCE) -> Account:
    """Доменный счёт для записи в хранилище."""
    return Account(account_id=AccountId.new(), balance=Money.from_number(balance, currency))


def _payment(account: Account, amount: Decimal = AMOUNT) -> Payment:
    """Новый платёж в ``PENDING`` по счёту плательщика."""
    return Payment.create(
        payment_id=PaymentId.new(),
        from_account_id=account.id,
        amount=Money.from_number(amount, account.currency),
    )


async def _store_account(session: AsyncSession, account: Account) -> None:
    """Положить счёт, к которому привязываются платежи.

    Счёт нужен первым: ``payments.from_account_id`` — внешний ключ с
    ``ON DELETE RESTRICT``, и платёж без счёта база не примет.
    """
    session.add(
        AccountModel(
            id=account.id.value,
            currency=account.currency.value,
            balance=account.balance.amount,
            is_blocked=account.is_blocked,
            version=account.version,
        ),
    )
    await session.flush()


async def _stored_row(session: AsyncSession, payment_id: PaymentId) -> Row[tuple[object, ...]] | None:
    """Прочитать строку ``payments`` сырым SQL из сессии вызывающего.

    Так проверка не зависит от адаптера: сломанный маппер не смог бы обнаружить,
    что именно он же и записал не то. Чтение идёт из той же транзакции, что и
    запись, — иначе несохранённые данные были бы не видны и проверка ничего не
    сказала бы о том, что легло в колонку.
    """
    result = await session.execute(
        text(
            'SELECT amount, status, provider_payment_id, failure_reason, version, created_at, updated_at'
            ' FROM payments WHERE id = :payment_id',
        ),
        {'payment_id': payment_id.value},
    )
    return result.first()


async def _set_timeline(
    session: AsyncSession,
    payment_id: PaymentId,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    """Задать платежу обе метки времени сразу.

    Нужна там, где проверяется порядок или отсечка по времени. Причины две, и обе
    проверяемые:

    * ``now()`` в Postgres — это время **начала транзакции**, поэтому платежи,
      записанные в одной транзакции, неразличимы по метке и проверить сортировку
      нечем;
    * метку времени нельзя задать «в прошлом» в одиночку: домен запрещает
      ``updated_at`` раньше ``created_at``, и маппер честно отказался бы читать
      такую строку. Зависший платёж — это тот, который и создан, и последний раз
      изменён давно, поэтому задаются обе метки.
    """
    await session.execute(
        text('UPDATE payments SET created_at = :created_at, updated_at = :updated_at WHERE id = :payment_id'),
        {'created_at': created_at, 'updated_at': updated_at, 'payment_id': payment_id.value},
    )


def _processing_payment(account: Account, provider_payment_id: str) -> Payment:
    """Платёж, уже отправленный провайдеру: ``PROCESSING`` и идентификатор операции."""
    payment = _payment(account)
    payment.process(provider_payment_id)
    return payment


# --- Форма контракта ----------------------------------------------------------


def test_repository_satisfies_the_port() -> None:
    """Адаптер отвечает порту ``PaymentRepository`` «по форме» (T-2.1).

    Проверяется и то, что порт **не** наследуется: наследование от ``Protocol``
    подмешивало бы заглушки и сделало бы проверку самоподтверждающейся.
    """
    assert isinstance(PostgresPaymentRepository(AsyncSession()), ports.PaymentRepository)
    assert ports.PaymentRepository not in PostgresPaymentRepository.__mro__


# --- Чтение -------------------------------------------------------------------


async def test_get_returns_none_for_unknown_payment(repository: PostgresPaymentRepository) -> None:
    """Отсутствующий платёж — это ``None``, а не исключение.

    Сценарий сам различает «платежа нет» (и отдаёт клиенту понятный отказ) и
    «платёж есть»: репозиторию бросать исключение из ``SELECT`` значило бы
    превратить нормальный ответ хранилища в ошибку.
    """
    assert await repository.get(PaymentId.new()) is None


async def test_get_returns_saved_payment_with_full_state(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Платёж, прочитанный из базы, совпадает с записанным во всех полях.

    Проверяется полный набор, а не только статус: потерянный идентификатор
    операции у провайдера выглядел бы как «платёж идёт», а вебхук по нему уже не
    нашёл бы наш платёж и был бы отброшен как чужой.
    """
    account = _account()
    await _store_account(session, account)
    saved = _processing_payment(account, 'ext-42')
    await repository.add(saved)

    loaded = await repository.get(saved.id)
    row = await _stored_row(session, saved.id)

    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.from_account_id == account.id
    assert loaded.amount == Money.from_number(AMOUNT, Currency.RUB)
    assert loaded.currency is Currency.RUB
    assert loaded.status is PaymentStatus.PROCESSING
    assert loaded.version == saved.version == INITIAL_VERSION + 1
    assert loaded.provider_payment_id == 'ext-42'
    assert loaded.failure_reason is None
    # Метки времени берутся из строки, а не выдумываются маппером.
    assert loaded.created_at == row[5]
    assert loaded.updated_at == row[6]


async def test_get_reads_currency_from_the_account(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Валюта платежа — валюта его счёта, а не дробь с точностью колонки.

    В ``payments`` колонки с валютой нет: она принадлежит счёту, поэтому чтение
    платежа идёт джойном с ``accounts``. Иена в ``NUMERIC(27,3)`` лежит как
    ``100.000`` и обязана читаться как ``100``: иначе платёж по счёту в иенах
    выглядел бы как сумма с тремя знаками после запятой, и сравнение с тем, что
    посчитал домен, разошлось бы на копейки.
    """
    yen = _account(Currency.JPY, Decimal(100))
    dinar = _account(Currency.KWD, Decimal('1.234'))
    await _store_account(session, yen)
    await _store_account(session, dinar)
    yen_payment = _payment(yen, Decimal(100))
    dinar_payment = _payment(dinar, Decimal('1.234'))
    await repository.add(yen_payment)
    await repository.add(dinar_payment)

    loaded_yen = await repository.get(yen_payment.id)
    loaded_dinar = await repository.get(dinar_payment.id)

    assert loaded_yen is not None
    assert loaded_yen.amount == Money.from_number(100, Currency.JPY)
    assert loaded_yen.currency is Currency.JPY
    assert loaded_dinar is not None
    assert loaded_dinar.amount == Money.from_number('1.234', Currency.KWD)
    assert loaded_dinar.currency is Currency.KWD


async def test_get_by_provider_payment_id_finds_the_payment(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Вебхук ищет платёж по идентификатору операции снаружи — и находит его.

    Это единственный путь сопоставления: во входящем уведомлении нашего
    ``payment_id`` нет, есть только идентификатор операции у провайдера.
    """
    account = _account()
    await _store_account(session, account)
    saved = _processing_payment(account, 'ext-77')
    await repository.add(saved)

    found = await repository.get_by_provider_payment_id('ext-77')

    assert found is not None
    assert found == saved
    assert found.status is PaymentStatus.PROCESSING


async def test_get_by_provider_payment_id_returns_none_for_unknown_operation(
    repository: PostgresPaymentRepository,
) -> None:
    """Неизвестная операция — это ``None``: уведомление чужое или преждевременное."""
    assert await repository.get_by_provider_payment_id('ext-does-not-exist') is None


async def test_get_by_provider_payment_id_ignores_payment_not_sent_to_provider(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Платёж, ещё не отправленный провайдеру, не находится ни по какой операции.

    У него идентификатора операции просто нет, и он остаётся ``None`` в базе.
    Репозиторий, который искал бы по «пустому» значению, нашёл бы первый
    такой платёж и применил к нему чужой статус — деньги ушли бы не туда.
    """
    account = _account()
    await _store_account(session, account)
    await repository.add(_payment(account))

    assert await repository.get_by_provider_payment_id('') is None


# --- Запись -------------------------------------------------------------------


async def test_add_stores_amount_status_and_version(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """``add`` кладёт в базу ровно то, что отдал домен.

    Читается сырым SQL: если бы проверка шла через ``get``, она доказала бы только
    согласованность адаптера с самим собой, а не то, что в денежной колонке лежат
    деньги. Ожидается именно ``250.000``, а не ``250.00``: колонка приводит
    значение к своей точности, и именно это значение потом читает ``Money``.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)

    await repository.add(saved)
    row = await _stored_row(session, saved.id)

    assert row is not None
    assert row.amount == Decimal('250.000')
    assert row.status == 'PENDING'
    assert row.provider_payment_id is None
    assert row.failure_reason is None
    assert row.version == INITIAL_VERSION


async def test_add_is_not_visible_to_other_transactions_before_commit(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Ключевое свойство: репозиторий **не коммитит**.

    Если бы ``add`` фиксировал транзакцию сам, откат на уровне ``UnitOfWork``
    перестал бы откатывать, и сага «списать холд — создать платёж» оставила бы
    деньги списанными без платежа. Видимость из чужого соединения — единственное
    наблюдение, которое это доказывает.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)

    await repository.add(saved)

    async with open_transaction() as other:
        assert await repository_for(other).get(saved.id) is None


async def test_add_of_duplicate_payment_id_is_rejected_by_the_database(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Дубль первичного ключа отсекает сама база.

    Идентификатор выдаёт наша доменная фабрика, поэтому вторая строка с тем же
    ``id`` — ошибка сценария, а не штатная ситуация. Репозиторию негде взять
    список известных идентификаторов, не продублировав его у домена, — и
    молчание репозитория о дубле означает ровно то, что написано в схеме.
    """
    account = _account()
    await _store_account(session, account)
    original = _payment(account)
    await repository.add(original)
    twin = Payment(
        payment_id=original.id,
        from_account_id=account.id,
        amount=Money.from_number(AMOUNT, Currency.RUB),
    )

    with pytest.raises(IntegrityError):
        await repository.add(twin)


async def test_add_rejects_payment_for_unknown_account(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Платёж по несуществующему счёту база не принимает.

    Это ровно то свойство, на котором держится джойн при чтении: внешний ключ не
    даёт платежу остаться без счёта, поэтому внутренний джойн не может потерять
    строку молча. Проверяется поведение самой БД на строке, собранной мимо
    репозитория.
    """
    orphan = _payment(_account())

    with pytest.raises(IntegrityError):
        await repository.add(orphan)


@pytest.mark.parametrize('amount', ['0', '-10.00'])
async def test_add_rejects_non_positive_amount(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
    amount: str,
) -> None:
    """Нулевой и отрицательный платёж отсекает ``CHECK`` в схеме.

    ``Money`` этого не допускает, а «минус на платеже» в базе — уже испорченные
    деньги. Проверяется поведение БД на строке, собранной мимо репозитория: в
    репозитории такого заказать нельзя.
    """
    account = _account()
    await _store_account(session, account)
    model = new_payment_model(_payment(account))
    model.amount = Decimal(amount)
    session.add(model)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_add_rejects_status_outside_the_scale(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Статус не из статус-машины не попадает в базу.

    ``CHECK`` по ``PaymentStatus`` — единственная линия обороны: репозиторию
    негде взять шкалу статусов, не продублировав её у домена. Поэтому проверяется
    поведение самой БД на строке, собранной мимо репозитория.
    """
    account = _account()
    await _store_account(session, account)
    model = new_payment_model(_payment(account))
    model.status = 'BROKEN'
    session.add(model)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_add_rejects_duplicate_provider_payment_id(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Два платежа не могут ссылаться на одну операцию провайдера.

    Уникальность частичная по смыслу: ``NULL`` у ещё не отправленных платежей
    повторяется законно, а два разных платежа с одним идентификатором операции —
    всегда ошибка данных. Проверяется поведение уникального индекса на строке,
    собранной мимо репозитория.
    """
    account = _account()
    await _store_account(session, account)
    await repository.add(_processing_payment(account, 'ext-dup'))

    model = new_payment_model(_processing_payment(account, 'ext-dup'))
    session.add(model)

    with pytest.raises(IntegrityError):
        await session.flush()


# --- Обновление ---------------------------------------------------------------


async def test_update_saves_status_provider_id_and_version(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """``update`` записывает смысловые поля, которые выставил домен.

    Проверяется всё, чем платёж живёт после создания: статус, идентификатор
    операции у провайдера и версия. Версия — отдельно от статуса: она кормит
    оптимистичный лок T-3.6, и потерянная при маппинге версия сделала бы
    несуществующую защиту от гонки неотличимой от работающей.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    saved.process('ext-42')

    await repository.update(saved)
    row = await _stored_row(session, saved.id)

    assert row is not None
    assert row.status == 'PROCESSING'
    assert row.provider_payment_id == 'ext-42'
    assert row.version == saved.version == INITIAL_VERSION + 1


async def test_update_saves_failure_reason(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Причина отказа доезжает до базы: по ней разбираются, что произошло."""
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    saved.process('ext-42')
    await repository.update(saved)
    saved.fail('Отказ банка получателя')

    await repository.update(saved)
    row = await _stored_row(session, saved.id)

    assert row is not None
    assert row.status == 'FAILED'
    assert row.failure_reason == 'Отказ банка получателя'
    assert row.version == saved.version == INITIAL_VERSION + 2


async def test_update_does_not_rewrite_amount_and_account(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """``UPDATE`` не трогает сумму и счёт: они неизменны по инварианту платежа.

    Проверяется текст запроса, а не результат: переписать эти колонки тем же
    значением выглядело бы безобидно, но означало бы, что репозиторий доверяет
    базе проверить чужой идентификатор счёта — а на ``from_account_id`` висит
    внешний ключ.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    saved.process('ext-42')

    statements = await capture_sql(session, repository.update(saved))
    updates = [statement for statement in statements if statement.startswith('UPDATE payments')]

    assert len(updates) == 1
    assert 'amount=' not in updates[0]
    assert 'from_account_id=' not in updates[0]
    assert 'status=' in updates[0]
    assert 'version=' in updates[0]
    # Метку изменения проставляет база: на неё опирается выборка зависших
    # платежей, и писать её из Python означало бы считать её по двум часам.
    assert 'updated_at=now()' in updates[0]


async def test_update_of_missing_payment_raises_entity_not_found(
    repository: PostgresPaymentRepository,
) -> None:
    """Сохранение несуществующего платежа — отказ, а не тихий успех.

    Молчание здесь означало бы потерянное движение денег: сценарий посчитал, что
    вернул холд по отказу провайдера, а база об этом не узнала.
    """
    with pytest.raises(EntityNotFoundError, match='не найден'):
        await repository.update(_processing_payment(_account(), 'ext-42'))


async def test_update_is_not_visible_to_other_transactions_before_commit(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """``update`` тоже не коммитит: граница транзакции принадлежит ``UnitOfWork``."""
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    await session.commit()
    saved.process('ext-42')

    await repository.update(saved)

    async with open_transaction() as other:
        seen = await repository_for(other).get(saved.id)
    assert seen is not None
    assert seen.status is PaymentStatus.PENDING
    assert seen.provider_payment_id is None


async def test_get_after_update_in_the_same_transaction_reads_stored_row(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Чтение сразу после ``update`` отдаёт строку, а не устаревший кеш.

    ``updated_at`` вычислила база, поэтому после обновления ORM помечает атрибут
    просроченным. Маппер читает метки синхронно, и такое состояние — единственное
    место, где репозиторий мог бы упасть с ``MissingGreenlet``: чтения идут через
    ``SELECT``, который просроченную метку перечитывает. Проверяется это на живой
    БД: юнит-тест такого отказа не воспроизводит — там нет сессии и identity map.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    saved.process('ext-42')
    await repository.update(saved)

    loaded = await repository.get(saved.id)
    row = await _stored_row(session, saved.id)

    assert loaded is not None
    assert loaded.status is PaymentStatus.PROCESSING
    assert loaded.provider_payment_id == 'ext-42'
    assert loaded.version == saved.version
    assert loaded.created_at == row[5]
    assert loaded.updated_at == row[6]


# --- Выборка по статусу -------------------------------------------------------


async def _seed_processing_payments(moments: list[datetime]) -> list[PaymentId]:
    """Положить платежи в ``PROCESSING`` с явно заданными метками изменения.

    Две особенности, и обе обязательны для честной проверки порядка:

    * **таблица чистится перед посевом.** Эти тесты коммитят данные — иначе нельзя
      прочитать их в *другой* транзакции, как это делает настоящая сверка. Без
      чистки выборка видела бы платежи соседних тестов и порядок был бы случайным;
    * **метки задаются сырым ``UPDATE``, а чтение идёт в другой транзакции.** Так
      проверяется настоящий порядок в базе, а не тот, что вернёт кеш уже
      загруженных строк, и результат не зависит от того, когда идёт прогон.
    """
    ids: list[PaymentId] = []
    async with open_transaction() as setup:
        await setup.execute(delete(PaymentModel))
        await setup.execute(delete(AccountModel))
        account = _account()
        await _store_account(setup, account)
        for index, moment in enumerate(moments):
            payment = _processing_payment(account, f'ext-seed-{index}')
            await repository_for(setup).add(payment)
            await _set_timeline(setup, payment.id, created_at=STALE_AT - timedelta(days=1), updated_at=moment)
            ids.append(payment.id)
        await setup.commit()
    return ids


async def test_find_by_status_orders_oldest_first() -> None:
    """Выборка идёт от старых к новым — так её и обещает порт.

    Порядок нужен сверке: она берёт платежи, которые дольше всего не менялись, и
    разбирает их раньше свежих. Обратный порядок заставил бы сверку каждый раз
    начинать с одних и тех же свежих платежей и не добираться до зависших.
    """
    ids = await _seed_processing_payments(
        [STALE_AT + timedelta(hours=2), STALE_AT + timedelta(hours=1), STALE_AT + timedelta(hours=3)],
    )

    async with open_transaction() as reader:
        found = await repository_for(reader).find_by_status(PaymentStatus.PROCESSING)

    assert [payment.id for payment in found] == [ids[1], ids[0], ids[2]]


async def test_find_by_status_respects_limit() -> None:
    """Предел отрезает выборку — иначе сверка забрала бы всю таблицу разом."""
    ids = await _seed_processing_payments(
        [STALE_AT + timedelta(hours=1), STALE_AT + timedelta(hours=2), STALE_AT + timedelta(hours=3)],
    )

    async with open_transaction() as reader:
        found = await repository_for(reader).find_by_status(PaymentStatus.PROCESSING, limit=2)

    assert [payment.id for payment in found] == ids[:2]


async def test_find_by_status_filters_by_updated_before() -> None:
    """Отсечка по времени оставляет только те платежи, что менялись раньше порога.

    Это и есть «зависшие»: свежие строки сверка не трогает, иначе она гоняла бы
    платёж, у которого провайдер ещё отвечает.
    """
    ids = await _seed_processing_payments(
        [STALE_AT + timedelta(hours=1), STALE_AT + timedelta(hours=2), STALE_AT + timedelta(hours=3)],
    )

    async with open_transaction() as reader:
        found = await repository_for(reader).find_by_status(
            PaymentStatus.PROCESSING,
            updated_before=STALE_AT + timedelta(hours=2, minutes=30),
        )

    assert [payment.id for payment in found] == ids[:2]


@pytest.mark.parametrize('limit', [0, -1, -100])
async def test_find_by_status_rejects_non_positive_limit(
    repository: PostgresPaymentRepository,
    limit: int,
) -> None:
    """Предел проверяется до запроса, а не базой.

    Postgres отвергает отрицательный ``LIMIT`` ошибкой драйвера, а ``LIMIT 0``
    молча вернул бы пустую выборку — и сверка решила бы, что зависших платежей
    нет. Правило «предел положителен» принадлежит ``domain/validation.py``, и
    репозиторий берёт его оттуда, а не заводит своё.
    """
    with pytest.raises(InvalidValueError, match='limit'):
        await repository.find_by_status(PaymentStatus.PROCESSING, limit=limit)


async def test_find_by_status_rejects_naive_updated_before(
    repository: PostgresPaymentRepository,
) -> None:
    """Наивная метка отвергается: Postgres трактовал бы её в зоне сервера.

    Молчаливое смещение окна на несколько часов выглядит как «зависших платежей
    нет» либо как «зависли все», и сверка потеряла бы деньги в обе стороны.
    """
    with pytest.raises(InvalidValueError, match='updated_before'):
        await repository.find_by_status(
            PaymentStatus.PROCESSING,
            updated_before=datetime(2026, 3, 14, 15, 9, 26),
        )


# --- Контракт SQL -------------------------------------------------------------


async def test_get_issues_exactly_one_select_with_account_join(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """``get`` — ровно один ``SELECT`` с джойном по счёту и без блокировки.

    Проверяется текст SQL, а не результат: репозиторий, который читал бы строку
    дважды или блокировал её без нужды, вернул бы те же данные и прошёл бы
    поведенческие тесты, а на платёжном пути это лишние обмены с БД и ожидание
    чужой транзакции там, где её не ждут.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    await session.commit()

    statements = await capture_sql(session, repository.get(saved.id))

    assert len(statements) == 1
    assert statements[0].startswith('SELECT payments.id')
    assert 'JOIN accounts ON payments.from_account_id = accounts.id' in statements[0]
    assert 'FOR UPDATE' not in statements[0]


async def test_get_by_provider_payment_id_issues_exactly_one_select(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Поиск по операции провайдера — один запрос по уникальному индексу.

    Отдельный запрос «сначала найти, потом прочитать» вернул бы тот же платёж и
    прошёл поведенческие тесты, но на каждом вебхуке платил бы вторым обменом.
    """
    account = _account()
    await _store_account(session, account)
    saved = _processing_payment(account, 'ext-42')
    await repository.add(saved)
    await session.commit()

    statements = await capture_sql(session, repository.get_by_provider_payment_id('ext-42'))

    assert len(statements) == 1
    assert 'payments.provider_payment_id =' in statements[0]
    assert 'JOIN accounts' in statements[0]


async def test_find_by_status_issues_exactly_one_ordered_and_limited_select(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Выборка по статусу — один упорядоченный и ограниченный запрос.

    Порядок и предел проверяются в тексте запроса, а не по результату: сортировка
    в Python после выборки всей таблицы вернула бы те же платежи, но сортировала
    бы всю таблицу в памяти на каждом проходе сверки. Запрос отдаётся по индексу
    ``ix_payments_status_updated_at``, и его форма обязана этому соответствовать.
    """
    account = _account()
    await _store_account(session, account)
    await repository.add(_processing_payment(account, 'ext-sweep'))
    await session.commit()

    statements = await capture_sql(
        session,
        repository.find_by_status(PaymentStatus.PROCESSING, limit=7),
    )

    assert len(statements) == 1
    assert 'WHERE payments.status =' in statements[0]
    assert 'ORDER BY payments.updated_at' in statements[0]
    assert 'LIMIT' in statements[0]
    assert 'JOIN accounts' in statements[0]


# --- Оптимистичная блокировка по версии (T-3.6) --------------------------------


async def test_optimistic_lock_rejects_concurrent_payment_change() -> None:
    """DoD: гонка версий по платежу — вторая запись падает, а не стирает первую.

    Оба сценария читают ``PENDING``-платёж с одной версией, первый переводит его в
    ``PROCESSING`` (операция у провайдера уже начата), второй — в ``CANCELLED``.
    Запись второго обязана упереться в версию: иначе отмена молча затёрла бы уже
    начатую операцию, и вебхук по ней не нашёл бы наш платёж.
    """
    account = _account()
    async with open_transaction() as setup:
        await _store_account(setup, account)
        payment = _payment(account)
        await repository_for(setup).add(payment)
        await setup.commit()

    async with open_transaction() as first, open_transaction() as second:
        first_repo, second_repo = repository_for(first), repository_for(second)
        first_payment = await first_repo.get(payment.id)
        second_payment = await second_repo.get(payment.id)
        assert first_payment is not None
        assert second_payment is not None
        assert first_payment.persisted_version == second_payment.persisted_version

        first_payment.process('ext-race')
        await first_repo.update(first_payment)
        await first.commit()

        second_payment.cancel()
        with pytest.raises(OptimisticLockError, match='переписала другая транзакция'):
            await second_repo.update(second_payment)

    async with open_transaction() as other:
        stored = await repository_for(other).get(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatus.PROCESSING
    assert stored.provider_payment_id == 'ext-race'


async def test_update_checks_payment_version_in_the_where_clause(
    session: AsyncSession,
    repository: PostgresPaymentRepository,
) -> None:
    """Условие по версии уходит в SQL: без него платежи переписывали бы друг друга.

    Проверяется текст запроса, а не результат: ``UPDATE`` без ``version = ...``
    вернул бы тот же статус и прошёл бы поведенческие тесты, а защиты от гонки в
    нём не было бы.
    """
    account = _account()
    await _store_account(session, account)
    saved = _payment(account)
    await repository.add(saved)
    await session.commit()
    # Идентификатор операции уникален в таблице: соседние тесты коммитят свои
    # значения, и повтор чужого упёрся бы в уникальный индекс, а не в версию.
    saved.process('ext-version-check')

    statements = await capture_sql(session, repository.update(saved))
    updates = [statement for statement in statements if statement.startswith('UPDATE payments')]

    assert len(updates) == 1
    assert 'payments.version =' in updates[0]
    assert 'RETURNING payments.version' in updates[0]
    assert len(statements) == 1
