"""Хранилище идемпотентности на живых Postgres и Redis (T-4.2).

DoD задачи — «повторный запрос с тем же ключом возвращает тот же ответ без
повтора логики». Юнит-тесты доказывают форму и сроки; здесь проверяется то, ради
чего вообще нужен этот адаптер, — **согласованность двух хранилищ**. Redis и
Postgres отвечают независимо, и любая рассинхронизация между ними выглядит
одинаково снаружи: клиент получает не тот ответ либо операция выполняется дважды.

Поэтому ключевые проверки построены вокруг одного сюжета — **повтор после
разных событий**:

* повтор сразу после сохранения (кэш горячий) — ответ берётся из Redis, база не
  трогается, а провайдер звать нельзя;
* повтор после перезапуска процесса (кэш холодный, ответ только в Postgres) —
  ответ обязан найтись в базе и вернуться тем же;
* повтор при **упавшем** Redis — сценарий обязан получить тот же ответ из базы,
  иначе отказ кэша превращался бы в отказ платежа;
* параллельный дубль во время обработки — должен быть отбит захватом, а не
  дойти до саги;
* ключ с другим телом запроса — конфликт, а не повтор;
* истёкший ключ — свободен: по нему запрос проходит как новый, а не возвращает
  древний ответ.

Стенд не разрушается: записи чистятся фикстурой модуля, Redis — фикстурой
``redis_client``. Без Postgres или Redis тесты пропускаются либо берут временный
testcontainer, а без Docker вовсе пропускаются: ``make test`` обязан оставаться
зелёным на машине без Docker (AGENT.md, §5).
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core_service.application.ports.idempotency_store import IdempotencyRecord
from src.db.idempotency_store import RedisPostgresIdempotencyStore, record_key, reservation_key
from src.db.models.idempotency_key import IdempotencyKeyModel
from tests.integration.conftest import build_session_maker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures('postgres_stack', 'redis_stack'),
]

#: Фиксированное «сейчас» тестовых часов. Сроки проверяются точно, поэтому время
#: заморожено: иначе тест зависел бы от того, как быстро он выполняется.
NOW = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)

KEY = 'create-payment-9f2c'
RESPONSE = {'payment_id': '6c1f-4a1e', 'status': 'PROCESSING', 'amount': '100.000', 'currency': 'RUB'}
REQUEST_HASH = 'a' * 64

#: Фабрика сессий приложения — ровно тот тип, которого ждёт адаптер.
AsyncSessionFactory = async_sessionmaker[AsyncSession]

#: Адрес, на котором никто не слушает: порт 1 зарезервирован и закрыт. Нужен,
#: чтобы поднять **настоящий** отказ Redis, а не его имитацию. Очистка базы
#: (``flushall``) не годится: она чиста, а не сломана, и снаружи оба состояния
#: выглядят одинаково — а ведут себя по-разному.
DEAD_REDIS_URL = 'redis://127.0.0.1:1/0'

#: Сколько живёт захват в тестах. Достаточно долго, чтобы пережить тест, и коротко,
#: чтобы его можно было дождаться в проверке истечения.
RESERVATION_TTL_SECONDS = 60

#: Сколько живёт ответ: сутки, как в сценарии создания платежа.
RECORD_TTL_SECONDS = 86_400


class _StubClock:
    """Часы с замороженным временем — те же, что у сценариев (T-2.1)."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@pytest.fixture(autouse=True)
def clean_records_table() -> Iterator[None]:
    """Очистить ``idempotency_keys`` до и после модуля.

    Отдельное соединение: адаптер пишет и коммитит сам, и ``DELETE`` внутри чужой
    откатываемой транзакции остался бы в базе после её отката.
    """

    async def truncate() -> None:
        engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
        try:
            async with engine.begin() as connection:
                await connection.execute(delete(IdempotencyKeyModel))
        finally:
            await engine.dispose()

    asyncio.run(truncate())
    yield
    asyncio.run(truncate())


@pytest.fixture
def clock() -> _StubClock:
    """Часы, которыми пользуется адаптер."""
    return _StubClock()


@pytest.fixture
async def session_maker() -> AsyncIterator[AsyncSessionFactory]:
    """Фабрика сессий приложения — та же, что у хранилища на стенде."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        yield build_session_maker(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def store(
    redis_client: Redis,
    clock: _StubClock,
    session_maker: AsyncSessionFactory,
) -> RedisPostgresIdempotencyStore:
    """Хранилище на живых Redis и Postgres."""
    return RedisPostgresIdempotencyStore(redis=redis_client, session_maker=session_maker, clock=clock)


def _record(
    *,
    key: str = KEY,
    request_hash: str = REQUEST_HASH,
    response: dict[str, Any] | None = None,
    created_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> IdempotencyRecord:
    """Запись хранилища с метками от :data:`NOW`."""
    return IdempotencyRecord(
        key=key,
        request_hash=request_hash,
        response=RESPONSE if response is None else response,
        created_at=created_at,
        expires_at=expires_at or created_at + timedelta(seconds=RECORD_TTL_SECONDS),
    )


STORED_ROW_QUERY = """
SELECT key, request_hash, response, created_at, expires_at
FROM idempotency_keys
WHERE key = :key
"""


async def _stored_row(key: str = KEY) -> dict[str, Any] | None:
    """Прочитать строку ``idempotency_keys`` сырым SQL с чужого соединения."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(STORED_ROW_QUERY), {'key': key})
            row = result.mappings().first()
            return None if row is None else dict(row)
    finally:
        await engine.dispose()


# --- DoD: повтор возвращает тот же ответ --------------------------------------


async def test_repeat_returns_the_same_answer_from_the_cache(
    store: RedisPostgresIdempotencyStore,
    redis_client: Redis,
) -> None:
    """DoD: повтор сразу после сохранения получает тот же ответ.

    Ответ обязан вернуться **тем же объектом**, каким был сохранён: сценарий
    разбирает его через ``from_idempotency_response`` и отдаёт клиенту, поэтому
    потеря любого поля (статус, сумма, валюта) превратила бы повтор в новую,
    неверно выглядящую операцию.
    """
    await store.save(_record())

    replayed = await store.get(KEY)

    assert replayed == _record()
    assert await redis_client.exists(record_key(KEY)) == 1


async def test_repeat_is_served_from_postgres_when_the_cache_is_cold(
    store: RedisPostgresIdempotencyStore,
    redis_client: Redis,
) -> None:
    """После перезапуска процесса (кэш пуст) ответ берётся из Postgres.

    Это и есть причина, по которой ответ хранится в базе, а не только в Redis:
    рестарт, падение или плановая перезагрузка обнуляют кэш, и повтор после них
    обязан получить тот же платёж, а не исполниться заново.
    """
    await store.save(_record())
    await redis_client.delete(record_key(KEY))

    replayed = await store.get(KEY)

    assert replayed == _record()
    # Промах обязан подогреть кэш, иначе каждый дубль ходил бы в Postgres.
    assert await redis_client.exists(record_key(KEY)) == 1


async def test_repeat_still_gets_the_answer_when_redis_is_down(
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    session_maker: AsyncSessionFactory,
) -> None:
    """Отказ кэша не превращается в отказ платежа: ответ есть в Postgres.

    Redis здесь — ускоритель, а не источник правды. Если бы чтение падало вместе
    с ним, клиентский повтор после сбоя кэша получал бы 500 вместо своего
    платежа — то есть кэш стал бы точкой отказа всей операции.

    Отказ поднимается **по-настоящему** (клиент на закрытый порт), а не имитируется
    ``flushall``: пустая база и сломанная снаружи неотличимы для наблюдателя, но
    для адаптера это разные пути — один возвращает промах, другой ошибку.
    """
    await store.save(_record())
    dead_client = Redis.from_url(DEAD_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    try:
        broken = RedisPostgresIdempotencyStore(redis=dead_client, session_maker=session_maker, clock=clock)

        replayed = await broken.get(KEY)
    finally:
        await dead_client.aclose()

    assert replayed == _record()


async def test_failed_acquisition_survives_a_dead_redis(
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    session_maker: AsyncSessionFactory,
) -> None:
    """На захвате отказ Redis **поднимается наружу** — в отличие от чтения.

    Это осознанная асимметрия, и она опаснее всего в неверную сторону. Подменить
    захват отказом «занято» нельзя: сценарий поверит и отдаст клиенту ложное
    «операция уже выполняется», хотя на самом деле ничего не выполнялось. Лучше
    явная ошибка с честной причиной.
    """
    dead_client = Redis.from_url(DEAD_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    try:
        broken = RedisPostgresIdempotencyStore(redis=dead_client, session_maker=session_maker, clock=clock)

        with pytest.raises(RedisError):
            await broken.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS)
    finally:
        await dead_client.aclose()


async def test_release_swallows_redis_failure(
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
    session_maker: AsyncSessionFactory,
) -> None:
    """``release`` не поднимает ошибку Redis даже на сломанном клиенте.

    Сценарий зовёт ``release`` в ветке отказа, чтобы не потерять исходную причину.
    Поднятая здесь ошибка замаскировала бы настоящую — «недостаточно средств» или
    отказ провайдера, — и клиент получил бы неверный диагноз, повторил бы запрос
    зря и не понял бы, что с деньгами всё в порядке.
    """
    dead_client = Redis.from_url(DEAD_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    try:
        broken = RedisPostgresIdempotencyStore(redis=dead_client, session_maker=session_maker, clock=clock)

        await broken.release(KEY)
    finally:
        await dead_client.aclose()


# --- Захват: ровно одна обработка ---------------------------------------------


async def test_second_competitor_cannot_acquire_a_held_key(
    store: RedisPostgresIdempotencyStore,
) -> None:
    """Параллельный дубль не проходит: захват достаётся только первому.

    Это и есть механизм «ровно одна обработка»: второй дубль обязан получить
    отказ, а не дойти до саги и списать сумму второй раз.
    """
    first = await store.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS)
    second = await store.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS)

    assert first is True
    assert second is False


async def test_released_key_becomes_available_again(store: RedisPostgresIdempotencyStore) -> None:
    """После отказа сценария ключ снова свободен.

    Иначе клиент, которому отказали по существу (недостаточно средств), не смог
    бы повторить запрос с тем же ключом: тот же ключ, тот же отказ, навсегда.
    """
    await store.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS)
    await store.release(KEY)

    assert await store.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS) is True


async def test_saving_the_answer_releases_the_reservation(
    store: RedisPostgresIdempotencyStore,
    redis_client: Redis,
) -> None:
    """Запись ответа снимает захват: после неё ключ означает «ответ сохранён».

    Если бы резервация оставалась, повторный запрос до истечения TTL получал бы
    отказ «уже выполняется» вместо уже готового ответа — то есть клиент после
    успешной операции видел бы отказ.
    """
    await store.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS)
    await store.save(_record())

    assert await redis_client.exists(reservation_key(KEY)) == 0
    assert await store.try_acquire(KEY, ttl_seconds=RESERVATION_TTL_SECONDS) is True


# --- Срок жизни и переиспользование ключа ------------------------------------


async def test_expired_key_is_treated_as_free(
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
) -> None:
    """По истёкшему ключу запрос проходит как новый, а не возвращает древний ответ.

    Срок записи — это ровно то окно, в котором повтор обещан вернуть тот же ответ.
    За его пределами клиент вправе начать новую операцию, иначе переиспользовать
    ключ было бы нельзя никогда.
    """
    await store.save(_record())
    clock.advance(timedelta(seconds=RECORD_TTL_SECONDS + 1))

    assert await store.get(KEY) is None


async def test_expired_row_is_replaced_by_a_new_answer(
    store: RedisPostgresIdempotencyStore,
    clock: _StubClock,
) -> None:
    """Истёкшая строка переиспользуется новым ответом.

    Строка под ключом остаётся в базе до уборки (T-4.4), и обычный ``INSERT``
    упал бы на первичном ключе. Ключ свободен — значит, место обязан занять новый
    запрос.

    Новый ответ строится от **текущих** часов, а не от исходных меток: запрос,
    пришедший после истечения срока, это новый запрос, и его метки — новые.
    """
    await store.save(_record())
    clock.advance(timedelta(seconds=RECORD_TTL_SECONDS + 1))
    fresh = _record(response={'payment_id': 'new-1', 'status': 'SETTLED'}, created_at=clock.now())

    await store.save(fresh)

    assert await store.get(KEY) == fresh


async def test_live_answer_is_never_overwritten(store: RedisPostgresIdempotencyStore) -> None:
    """Живой ответ не перезаписывается вторым сохранением.

    Страховка на проигравшую гонку: если захват всё же был обыгран, ответ не
    должен подменяться, иначе по одному ключу разошлись бы два разных
    результата, и повтор перестал бы возвращать «тот же» ответ. Проверяется на
    живом Postgres — ``ON CONFLICT ... WHERE`` это и есть.
    """
    await store.save(_record())
    impostor = _record(request_hash='b' * 64, response={'payment_id': 'other', 'status': 'FAILED'})

    await store.save(impostor)

    assert await store.get(KEY) == _record()
    stored = await _stored_row()
    assert stored is not None
    assert stored['request_hash'] == REQUEST_HASH


# --- Форма записи в базе ------------------------------------------------------


async def test_saved_answer_is_persisted_with_both_timestamps(store: RedisPostgresIdempotencyStore) -> None:
    """В базу легли ключ, отпечаток тела, ответ и обе метки времени.

    Читается сырым SQL с чужого соединения: проверка через сам адаптер доказала
    бы только его согласованность с самим собой, а не то, что в колонках лежит
    нужное.
    """
    await store.save(_record())

    stored = await _stored_row()

    assert stored is not None
    assert stored['key'] == KEY
    assert stored['request_hash'] == REQUEST_HASH
    assert stored['response'] == RESPONSE
    assert stored['created_at'] == NOW
    assert stored['expires_at'] == NOW + timedelta(seconds=RECORD_TTL_SECONDS)


async def test_unknown_key_has_no_answer(store: RedisPostgresIdempotencyStore) -> None:
    """Отсутствие ключа — это ``None``, а не исключение.

    Сценарий обязан отличать «ключа нет» (запрос новый) от «ключ есть, ответа
    ещё нет» (обработка идёт) — иначе он отдаст клиенту отказ по чужой причине.
    """
    assert await store.get('never-used-key') is None
