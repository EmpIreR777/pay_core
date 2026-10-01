"""Уборка просроченных ключей на живой Postgres (T-4.4).

Юнит-тесты доказывают форму запроса и регистрацию в расписании, но не отвечают
на вопрос, ради которого уборка и написана: **убирает ли база просроченные
записи, оставляя живые**. Здесь проверяется именно поведение — по данным,
оставшимся в таблице после прохода.

Главный сюжет модуля — **граница отсечения**, и она проверяется в обе стороны:

* запись, чей срок вышел, обязана исчезнуть;
* запись, срок которой ещё не наступил, обязана остаться, даже если она
  пролежала в базе дольше, чем длится срок просроченной записи.

Вторая половина важнее первой. Ошибка «убирать по ``created_at``» или «считать
срок от серверного времени» снаружи выглядит так же, как успех: уборка отработала
и вернула число. Но удалённые живые записи означают, что повтор с тем же ключом
не найдёт ответа и создаст **второй платёж** — то есть уборка сама станет
причиной двойного списания, которое весь эпик обязан исключить.

Отдельно проверяется порция: проход с ``batch_size`` меньшим числа просроченных
строк обязан удалить их все несколькими короткими транзакциями. Уборка, удаляющая
за раз всё, выглядит снаружи точно так же, просто держит блокировки до конца
прохода и откатывает всю работу разом при сбое.

Стенд не разрушается: таблица чистится фикстурой модуля. Без Postgres тесты
пропускаются либо берут временный testcontainer, а без Docker вовсе пропускаются:
``make test`` обязан оставаться зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.db.idempotency_cleanup import delete_expired_idempotency_keys
from src.db.models.idempotency_key import IdempotencyKeyModel
from tests.integration.conftest import build_session_maker, capture_sql, clear_idempotency_records

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures('postgres_stack'),
]

#: Фиксированное «сейчас» тестовых часов. Отсечка проверяется точно, на границе,
#: поэтому время заморожено: иначе тест зависел бы от того, как быстро он шёл.
NOW = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)

#: Сколько живёт ответ: сутки, как в сценарии создания платежа (T-2.4).
RECORD_TTL_SECONDS = 86_400

#: Сколько просроченных записей нужно, чтобы проход не уложился в одну порцию.
#: Число взято с запасом относительно ``BATCH_SIZE``: при равных количествах цикл
#: отработал бы ровно один раз, и проверка «всё удалено» не отличала бы код с
#: порциями от кода без них.
BATCH_SIZE = 3
EXPIRED_ABOVE_BATCH = 5


@pytest.fixture(autouse=True)
def clean_records_table() -> Iterator[None]:
    """Очистить ``idempotency_keys`` до и после модуля.

    Общая обвязка очистки живёт в ``conftest`` (``clear_idempotency_records``):
    её читает и хранилище (T-4.2), и сценарий (T-4.3), и копия здесь разошлась
    бы с ними при первой же правке.
    """
    asyncio.run(clear_idempotency_records())
    yield
    asyncio.run(clear_idempotency_records())


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Движок на время теста.

    Отдельный движок, а не общий модульный: у каждого теста своё соединение, и
    завершённый движок не должен уносить с собой чужие сессии.
    """
    test_engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
def session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий приложения — та же, что у хранилища на стенде."""
    return build_session_maker(engine)


async def _insert(
    session_maker: async_sessionmaker[AsyncSession],
    key: str,
    expires_at: datetime,
    *,
    created_at: datetime | None = None,
) -> None:
    """Положить запись напрямую, минуя сценарий.

    Через хранилище писать нельзя: оно само решает, просрочена запись или нет, и
    запись с заданным сроком в прошнем просто не сохранилась бы в читаемом виде.
    А проверять уборку нужно на строках, срок которых база ещё не тронула.

    ``created_at`` задаётся явно там, где срок сильно отличается от времени
    появления: база требует ``expires_at > created_at`` (T-4.1), и запись,
    «протухшая тридцать дней назад», обязана была появиться раньше этого срока —
    иначе она не протухла, а никогда не была действительной.
    """
    async with session_maker() as session:
        session.add(
            IdempotencyKeyModel(
                key=key,
                request_hash='a' * 64,
                response={'payment_id': '6c1f-4a1e'},
                created_at=NOW - timedelta(days=2) if created_at is None else created_at,
                expires_at=expires_at,
            ),
        )
        await session.commit()


async def _keys(session_maker: async_sessionmaker[AsyncSession]) -> set[str]:
    """Ключи, оставшиеся в таблице после прохода."""
    async with session_maker() as session:
        rows = await session.execute(select(IdempotencyKeyModel.key))
        return set(rows.scalars().all())


# --- DoD: удаляет просроченные, оставляя живые --------------------------------


async def test_cleanup_removes_expired_and_keeps_live_records(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Просроченная запись исчезает, живая остаётся — в одной таблице, за один проход.

    Это и есть DoD задачи целиком. Проверяются обе стороны сразу по остатку в
    базе: проверка «просроченных стало ноль» сама по себе пропустила бы уборку,
    сносящую всё без разбора, — а такая уборка опаснее отсутствия уборки.
    """
    await _insert(session_maker, 'expired-hour', NOW - timedelta(hours=1))
    await _insert(session_maker, 'live-hour', NOW + timedelta(hours=1))

    deleted = await delete_expired_idempotency_keys(session_maker, NOW)

    assert deleted == 1
    assert await _keys(session_maker) == {'live-hour'}


async def test_cleanup_removes_record_expiring_exactly_now(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Запись с ``expires_at``, равным «сейчас», тоже убирается — граница ``<=``.

    Соглашение о сроке живёт в двух местах: адаптер T-4.2 отдаёт запись на повтор
    только при ``expires_at > now()``, то есть ровно в эту секунду запись уже
    не выдаётся. Уборка обязана считать мёртвым то же самое множество, иначе
    такая запись не удалялась бы никогда: она и не выдаётся, и не убирается, и
    занимает место молча.

    Проверка на границе, а не «за час до» — на часе раньше отсечение ``<=`` и
    ``<`` дали бы одинаковый результат и ничего бы не различали.
    """
    await _insert(session_maker, 'expires-this-instant', NOW)

    deleted = await delete_expired_idempotency_keys(session_maker, NOW)

    assert deleted == 1
    assert await _keys(session_maker) == set()


async def test_cleanup_keeps_record_expiring_a_moment_later(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Запись, которой остался хоть секунда срока, не трогается.

    Обратная сторона границы. Если отсечение уедет хотя бы на секунду вперёд,
    живой ответ исчезнет из базы раньше обещанного срока, и повтор с тем же
    ключом не найдёт ответа и выполнит операцию заново.
    """
    await _insert(session_maker, 'expires-next-second', NOW + timedelta(seconds=1))

    deleted = await delete_expired_idempotency_keys(session_maker, NOW)

    assert deleted == 0
    assert await _keys(session_maker) == {'expires-next-second'}


async def test_cleanup_does_not_judge_by_age_of_the_row(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Старая по времени появления запись с живым сроком остаётся.

    Проверка против реальной ошибки «отсекать по ``created_at``». Такая уборка
    снаружи неотличима от правильной: число удалённых записей верное, проход
    отработал, логи в порядке. Но запись лежит двое суток при суточном сроке —
    то есть ещё жива, и её удаление отдало бы клиенту второй платёж вместо его
    же ответа на повтор.
    """
    await _insert(session_maker, 'old-but-alive', NOW + timedelta(days=1))

    deleted = await delete_expired_idempotency_keys(session_maker, NOW)

    assert deleted == 0
    assert await _keys(session_maker) == {'old-but-alive'}


async def test_cleanup_uses_the_cutoff_passed_in_not_the_database_time(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Отсекает по переданному моменту, а не по времени базы.

    Отсечка уведена на год назад, и просроченной по ней записи быть не должно —
    при том, что база считает её просроченной сутки назад. Проверка наоборот:
    если бы уборка считала серверное время, запись исчезла бы, хотя приложение
    считает её обещанной клиенту. Именно это расхождение превращается в двойную
    оплату: повтор не находит ответа и создаёт платёж заново.
    """
    await _insert(
        session_maker,
        'long-lived',
        NOW - timedelta(days=30),
        created_at=NOW - timedelta(days=40),
    )

    deleted = await delete_expired_idempotency_keys(session_maker, NOW - timedelta(days=365))

    assert deleted == 0
    assert await _keys(session_maker) == {'long-lived'}


# --- Порция -------------------------------------------------------------------


async def test_cleanup_deletes_everything_across_several_batches(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Проход удаляет больше записей, чем помещается в одну порцию.

    Проверяется на количестве, превышающем ``batch_size`` намеренно: при равных
    количествах цикл отработал бы ровно один раз, и проверка «всё удалено» не
    отличала бы код с порциями от кода без них. Одна транзакция на всю таблицу
    выглядит снаружи точно так же — она просто держит блокировки до конца
    прохода и откатывает всю работу разом при сбое.
    """
    expired = [f'expired-{index}' for index in range(EXPIRED_ABOVE_BATCH)]
    for key in expired:
        await _insert(session_maker, key, NOW - timedelta(hours=1))
    await _insert(session_maker, 'live', NOW + timedelta(hours=1))

    deleted = await delete_expired_idempotency_keys(session_maker, NOW, batch_size=BATCH_SIZE)

    assert deleted == EXPIRED_ABOVE_BATCH
    assert await _keys(session_maker) == {'live'}


async def test_cleanup_commits_each_batch_separately(
    engine: AsyncEngine,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Каждая порция — своя транзакция, а не одна на весь проход.

    Проверяется по SQL, а не по итоговой таблице: удалённые записи выглядят
    одинаково и при одной транзакции, и при пяти. Различие — в удержании
    блокировок и в цене отката, и видно оно только по тому, сколько команд
    ушло на сервер и сколько раз база зафиксировала результат.
    """
    for index in range(EXPIRED_ABOVE_BATCH):
        await _insert(session_maker, f'expired-{index}', NOW - timedelta(hours=1))

    async def run() -> None:
        await delete_expired_idempotency_keys(session_maker, NOW, batch_size=BATCH_SIZE)

    async with session_maker() as observing_session:
        statements = await capture_sql(observing_session, run())

    deletes = [statement for statement in statements if statement.startswith('DELETE FROM idempotency_keys')]

    # Порция из трёх при пяти просроченных: три, затем два. Вторую порцию цикл
    # и считает признаком конца — она короче запрошенной, то есть кандидатов
    # больше нет. Третьей, «пустой», порции не будет: цикл завершается на
    # неполной, и лишнего запроса в базу не уходит.
    assert len(deletes) == 2
    assert all('LIMIT' in statement for statement in deletes)


async def test_cleanup_of_empty_table_is_a_no_op(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Пустая таблица — нулевой результат и никаких записей.

    Обычное состояние стенда между проходами. Проверка ловит цикл, который не
    находит кандидатов и потому не выходит: такой уборка ждала бы следующего
    часа, ничего не удалив, и на живой таблице выглядела бы как зависшая.
    """
    deleted = await delete_expired_idempotency_keys(session_maker, NOW)

    assert deleted == 0
    assert await _keys(session_maker) == set()


async def test_repeated_cleanup_is_idempotent(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Второй проход по уже убранному ничего не делает.

    Расписание не гарантирует, что запущенный экземпляр один: у шедулера есть
    право повторить постановку, и уборка обязана быть безопасна при повторе.
    Второй проход возвращает ноль и не трогает записи, записанные между
    проходами, — то есть не задевает даже то, что появилось позже.
    """
    await _insert(session_maker, 'expired', NOW - timedelta(hours=1))

    first = await delete_expired_idempotency_keys(session_maker, NOW)
    await _insert(session_maker, 'written-later', NOW + timedelta(hours=1))
    second = await delete_expired_idempotency_keys(session_maker, NOW)

    assert (first, second) == (1, 0)
    assert await _keys(session_maker) == {'written-later'}


async def test_cleanup_ignores_rows_appearing_during_the_pass(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Отсечка заморожена: протухшее после старта прохода дождётся следующего.

    Проверяется на часах, сдвинутых между проходами: первая убирает всё, что
    протухло на момент её старта, а вторая — то, что протухло уже после. Если бы
    отсечка пересчитывалась на каждой порции, множество «просроченных» ехало бы
    вместе с часами, и на большой таблице проход не завершился бы вовсе.
    """
    await _insert(session_maker, 'expired-first-round', NOW - timedelta(hours=1))
    # Эта запись протухнет только ко второму проходу.
    await _insert(session_maker, 'expires-between-rounds', NOW + timedelta(hours=1))

    first = await delete_expired_idempotency_keys(session_maker, NOW)
    second = await delete_expired_idempotency_keys(session_maker, NOW + timedelta(hours=2))

    assert (first, second) == (1, 1)
    assert await _keys(session_maker) == set()


async def test_cleanup_counts_only_what_it_deleted(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Возвращаемое число совпадает с тем, что реально исчезло из таблицы.

    Число — не украшение: по нему в логах видно, что уборка работает. Ноль на
    протяжении часов означает тихий сбой, а «нечего было убирать», и без сверки
    с таблицей эти два случая неразличимы. Считается независимым запросом, а не
    берётся у самой уборки.
    """
    await _insert(session_maker, 'expired-a', NOW - timedelta(hours=2))
    await _insert(session_maker, 'expired-b', NOW - timedelta(hours=3))
    await _insert(session_maker, 'live', NOW + timedelta(hours=1))

    deleted = await delete_expired_idempotency_keys(session_maker, NOW)

    async with session_maker() as session:
        remaining = await session.scalar(select(func.count()).select_from(IdempotencyKeyModel))

    assert deleted == 2
    assert remaining == 1


async def test_cleanup_stops_when_the_driver_reports_no_rowcount(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Драйвер, не считающий строки, обрывает проход, а не крутит его вечно.

    ``rowcount`` у ``DELETE`` равен ``-1``, когда драйвер не смог посчитать строки
    (например, подменённый на стенде). Без проверки минус сложился бы в итог —
    уборка вернула бы отрицательное число, — и ``-1 < batch_size`` выпустил бы
    цикл, то есть проход объявил бы себя завершённым, ничего не удалив. Проверка
    на подменённом драйвере: настоящий Postgres считает строки всегда, и без
    подмены путь был бы недостижим.
    """
    for index in range(EXPIRED_ABOVE_BATCH):
        await _insert(session_maker, f'expired-{index}', NOW - timedelta(hours=1))

    class _BlindSession:
        """Сессия, у которой ``execute`` не отдаёт число удалённых строк."""

        def __init__(self, inner: AsyncSession) -> None:
            self._inner = inner

        async def execute(self, statement: object) -> Any:
            return _UncountableResult(await self._inner.execute(statement))

        async def commit(self) -> None:
            await self._inner.commit()

    class _UncountableResult:
        """Результат без ``rowcount`` — как его отдаёт драйвер, не считающий строки."""

        rowcount = -1

        def __init__(self, inner: Any) -> None:
            self._inner = inner

    class _BlindSessionMaker:
        def __call__(self) -> Any:
            return _BlindSessionContext(session_maker)

    class _BlindSessionContext:
        def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
            self._maker = maker

        async def __aenter__(self) -> _BlindSession:
            self._inner_ctx = self._maker()
            self._inner = await self._inner_ctx.__aenter__()
            return _BlindSession(self._inner)

        async def __aexit__(self, *exc_info: object) -> Any:
            return await self._inner_ctx.__aexit__(*exc_info)

    blind = cast('async_sessionmaker[AsyncSession]', _BlindSessionMaker())
    deleted = await asyncio.wait_for(delete_expired_idempotency_keys(blind, NOW, batch_size=BATCH_SIZE), 5)

    assert deleted == 0


async def test_cleanup_keeps_one_cutoff_for_every_batch(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Одна отсечка на весь проход: все порции отсекают по ней, а не по-разному.

    Отсечка подана на год назад, и проходит в несколько порций. Записи, протухшие
    уже после неё, но до «сейчас», убираться не должны: проход судит по
    отсечке, а не по тому, что наступило, пока он шёл. Если бы каждая порция
    брала свежее время, вторая и третья снесли бы записи, живые на старте, —
    а это тот же вред, что и ошибка со знаком сравнения: повтор не находит
    ответа и создаёт второй платёж.

    Порция тут намеренно равна единице: при ней расхождение отсечек видно
    сразу, а не на последней порции.
    """
    # Протухли до отсечки — обязаны уйти.
    for index in range(3):
        await _insert(
            session_maker,
            f'expired-{index}',
            NOW - timedelta(days=400),
            created_at=NOW - timedelta(days=500),
        )
    # Протухли после отсечки, но до «сейчас» — обязаны остаться.
    for index in range(3):
        await _insert(
            session_maker,
            f'expired-later-{index}',
            NOW - timedelta(days=300),
            created_at=NOW - timedelta(days=400),
        )

    deleted = await delete_expired_idempotency_keys(
        session_maker,
        NOW - timedelta(days=365),
        batch_size=1,
    )

    assert deleted == 3
    assert await _keys(session_maker) == {f'expired-later-{index}' for index in range(3)}
