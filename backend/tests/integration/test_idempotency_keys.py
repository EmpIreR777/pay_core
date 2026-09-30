"""Схема таблицы идемпотентных ключей на живом Postgres (T-4.1).

DoD задачи — «миграция создаёт таблицу и индексы». Юнит-тесты схемы
(`tests/unit/db/test_models.py`) доказывают только форму: что SQLAlchemy умеет
описать таблицу с нужными колонками и индексами. Этого мало — описание ничего не
говорит о том, создали ли таблицу **ревизии Alembic**, а проверить это можно
только против настоящей базы. Поэтому здесь проверяется фактическое состояние
схемы, прочитанное из каталога Postgres.

Что и почему проверяется именно так:

* **таблицу и индексы создаёт миграция, а не «осталось от прошлого прогона».**
  Первый тест начинается с ``downgrade base``: без отката он прошёл бы на любой
  базе, где таблица когда-либо существовала, и доказал бы ровно ничего;
* **колонки соответствуют модели.** Сверяются не только имена, но и ширина
  (``key`` — из порта, ``request_hash`` — под ``sha256``) и ``nullability``.
  Расхождение ширины с портом означало бы, что длинный ключ клиента обрежется
  на вставке, и заметить это можно только на проде;
* **ограничения работают, а не просто объявлены.** ``CHECK expires_at >
  created_at`` и первичный ключ проверяются поведением — отказом базы на плохой
  строке. Схема, в которой ограничение записано, но не действует, выглядит в
  метаданных точно так же, как рабочая;
* **индекс на ``expires_at`` есть и лежит на той колонке.** Под уборку T-4.4 он
  обязателен, а имя индекса само по себе ничего не значит — он может висеть на
  чужой колонке;
* **JSONB отдаёт объект, а ``NULL`` допустим.** ``response IS NULL`` — это
  «ключ занят, ответа ещё нет», состояние, которое обязано отличаться от «ключа
  нет»: иначе параллельная доставка прошла бы в обработку повторно.

Стенд не разрушается: каждая запись живёт в откатываемой транзакции фикстуры
``session``, а ``restore_head`` возвращает схему к ``head``. Без Postgres-стенда
тесты берут временный контейнер (testcontainer, T-3.7), а без Docker вовсе —
пропускаются: ``make test`` обязан оставаться зелёным на машине без Docker
(AGENT.md, §5).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.db.models.base import SHA256_HEX_LENGTH
from src.db.models.idempotency_key import IdempotencyKeyModel
from src.run_migrations import build_alembic_config
from tests.integration.conftest import (
    fetch_rows,
    list_public_indexes,
    query_table_presence,
)

#: Фикстуры готовности и возврата к ``head`` запрашиваются явно: первый тест
#: гоняет откат ревизий, и без возврата к ``head`` он оставил бы стенд без схемы.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures('postgres_stack', 'restore_head'),
]

#: Имя таблицы как в схеме. Встречается в запросах к каталогу, поэтому живёт
#: одной константой: две строки с именем таблицы разошлись бы при первом же
#: переименовании.
TABLE = 'idempotency_keys'

#: Первичный ключ таблицы. Имена индексов формируются по соглашению имён из
#: ``models/base.py``, поэтому проверяется именно оно, а не «какой-то индекс».

#: Колонки, ожидаемые в таблице, вместе с их шириной. ``nullability`` — часть
#: ожидания: ``response`` единственная nullable-колонка, и это осмысленно
#: («ключ занят, ответа ещё нет»). Ширины взяты из порта и ``models/base.py``:
#: числа в тесте означали бы, что он зафиксировал магические константы, а не
#: правило «колонка совпадает с контрактом слоем ниже».
EXPECTED_COLUMNS = {
    'key': ('character varying', MAX_IDEMPOTENCY_KEY_LENGTH, 'NO'),
    'request_hash': ('character varying', SHA256_HEX_LENGTH, 'NO'),
    'response': ('jsonb', None, 'YES'),
    'created_at': ('timestamp with time zone', None, 'NO'),
    'expires_at': ('timestamp with time zone', None, 'NO'),
}

COLUMNS_QUERY = """
SELECT column_name, data_type, character_maximum_length, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'idempotency_keys'
"""

#: Колонки, на которых построен каждый индекс таблицы. Читается именно состав
#: индекса: имя ``ix_idempotency_keys_expires_at`` есть в каталоге и у индекса,
#: случайно построенного на другой колонке, и такое расхождение заметно только
#: здесь — в плане запроса, которого в проекте пока нет.
INDEX_COLUMNS_QUERY = """
SELECT index_class.relname AS index_name, attribute.attname AS column_name
FROM pg_index AS index_info
JOIN pg_class AS table_class ON table_class.oid = index_info.indrelid
JOIN pg_class AS index_class ON index_class.oid = index_info.indexrelid
JOIN pg_attribute AS attribute
  ON attribute.attrelid = index_info.indrelid AND attribute.attnum = ANY(index_info.indkey)
WHERE table_class.relname = 'idempotency_keys'
  AND table_class.relnamespace = 'public'::regnamespace
"""

STORED_ROW_QUERY = """
SELECT request_hash, response, created_at, expires_at
FROM idempotency_keys
WHERE key = :key
"""

KEY = 'create-payment-9f2c'


def _key_row(*, response: dict[str, object] | None = None, expires_at: datetime | None = None) -> IdempotencyKeyModel:
    """Строка идемпотентного ключа для записи в базу."""
    return IdempotencyKeyModel(
        key=KEY,
        request_hash=sha256(REQUEST_BODY).hexdigest(),
        response=response,
        expires_at=expires_at or datetime.now(UTC) + timedelta(days=1),
    )


async def _stored_row(session: AsyncSession) -> dict[str, object] | None:
    """Прочитать сохранённую строку **сырым SQL** из сессии вызывающего.

    Минуя ORM: проверка через модель доказала бы согласованность модели с самой
    собой, а не то, что в колонках лежит нужное. Запрос выполняется в той же
    транзакции, что и запись, — иначе несохранённые данные были бы не видны и
    проверка ничего не сказала бы о том, что легло в базу.
    """
    result = await session.execute(text(STORED_ROW_QUERY), {'key': KEY})
    row = result.mappings().first()
    return None if row is None else dict(row)


async def _index_columns() -> dict[str, list[str]]:
    """Индексы таблицы как отображение ``имя индекса -> список его колонок``."""
    columns: dict[str, list[str]] = {}
    for row in await fetch_rows(INDEX_COLUMNS_QUERY):
        columns.setdefault(str(row['index_name']), []).append(str(row['column_name']))
    return columns


# --- Миграция создаёт таблицу и индексы ---------------------------------------


def test_migration_creates_table_with_its_indexes() -> None:
    """DoD: таблица и оба индекса созданы ревизией, а не остались от прошлого прогона.

    Откат до ``base`` обязателен: на базе, где таблица уже есть, тест прошёл бы
    «по инерции» и не доказал бы ничего о миграции.
    """
    config = build_alembic_config()
    command.downgrade(config, 'base')
    assert asyncio.run(query_table_presence(TABLE)) is False

    command.upgrade(config, 'head')
    assert asyncio.run(query_table_presence(TABLE)) is True
    assert asyncio.run(list_public_indexes()).get(EXPIRY_INDEX) == TABLE
    assert asyncio.run(list_public_indexes()).get(PRIMARY_KEY_INDEX) == TABLE


def test_index_on_expiry_covers_the_expiry_column() -> None:
    """Индекс уборки построен на ``expires_at``, а не на чужой колонке.

    Само имя индекса не доказывает ничего: с тем же именем он может висеть на
    любой колонке, и удаление просроченных ключей (T-4.4) тогда сканировало бы
    таблицу целиком.
    """
    columns = asyncio.run(_index_columns())

    assert columns[EXPIRY_INDEX] == ['expires_at']
    assert columns[PRIMARY_KEY_INDEX] == ['key']


def test_columns_match_the_model_contract() -> None:
    """Колонки, их ширина и ``nullability`` совпадают с моделью и портом.

    Особенно важна ширина ``key``: она приходит из
    :data:`MAX_IDEMPOTENCY_KEY_LENGTH`, и расхождение означало бы, что длинный
    ключ клиента обрежется на вставке — то есть два разных ключа молча склеились
    бы в один, и повтор перестал бы быть повтором.
    """
    rows = asyncio.run(fetch_rows(COLUMNS_QUERY))

    actual = {
        str(row['column_name']): (str(row['data_type']), row['character_maximum_length'], str(row['is_nullable']))
        for row in rows
    }

    assert actual == EXPECTED_COLUMNS


# --- Ограничения работают, а не просто объявлены ------------------------------


async def test_database_rejects_key_that_expires_before_creation(session: AsyncSession) -> None:
    """``CHECK expires_at > created_at`` отбивает ключ, умерший раньше, чем появился.

    Проверяется поведение базы на записи, собранной мимо модели: ограничение,
    объявленное в метаданных, но не проверяемое сервером, выглядит в каталоге
    точно так же, как рабочее. ``created_at`` проставляет сама БД — текущим
    временем, поэтому заведомо просроченный ключ отбивается всегда.
    """
    session.add(_key_row(expires_at=datetime.now(UTC) - timedelta(days=1)))

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_database_rejects_second_record_with_the_same_key(session: AsyncSession) -> None:
    """Первичный ключ не даёт записать один и тот же ключ дважды.

    Это последний рубеж идемпотентности на стороне БД: даже если захват ключа в
    Redis (T-4.2) проиграет гонку, вторая запись не пройдёт, и повтор не создаст
    второй платёж.
    """
    session.add(_key_row())
    await session.flush()
    session.add(_key_row())

    with pytest.raises(IntegrityError):
        await session.flush()


# --- Ответ хранится как JSONB, а занятый ключ отличается от отсутствующего ----


async def test_response_is_stored_as_json_object(session: AsyncSession) -> None:
    """Сохранённый ответ читается обратно тем же объектом, а не строкой.

    ``JSONB`` хранит структуру, поэтому вложенность ответа клиенту (T-4.2)
    сохраняется без ручной пересборки.
    """
    response = {'payment_id': '6c1f-4a1e', 'status': 'SETTLED', 'meta': {'attempts': 1, 'tags': ['a', 'b']}}
    session.add(_key_row(response=response))
    await session.flush()

    stored = await _stored_row(session)

    assert stored is not None
    assert stored['response'] == response


async def test_response_may_be_absent_while_the_key_is_taken(session: AsyncSession) -> None:
    """``NULL`` в ответе — это «ключ занят, ответа ещё нет», а не «ключа нет».

    Состояние обязано отличаться от отсутствующей строки: иначе параллельная
    доставка увидела бы «свободный» ключ и прошла в обработку повторно.
    """
    session.add(_key_row(response=None))
    await session.flush()

    stored = await _stored_row(session)

    assert stored is not None
    assert stored['response'] is None


async def test_created_at_is_filled_by_the_database(session: AsyncSession) -> None:
    """Метку появления строки проставляет БД, даже если её не передали.

    Ключ может появиться в обход ORM (миграция, ручной ``INSERT``, скрипт сверки),
    и тогда метка времени должна быть у него в любом случае.
    """
    session.add(_key_row())
    await session.flush()

    stored = await _stored_row(session)

    assert stored is not None
    assert stored['created_at'].tzinfo is not None


async def test_key_holds_the_full_request_hash_it_was_written_with(session: AsyncSession) -> None:
    """Отпечаток тела доезжает до базы целиком, без обрезки по длине колонки."""
    saved = _key_row()
    session.add(saved)
    await session.flush()

    stored = await _stored_row(session)

    assert stored is not None
    assert stored['request_hash'] == saved.request_hash
    assert len(str(stored['request_hash'])) == SHA256_HEX_LENGTH


PRIMARY_KEY_INDEX = 'pk_idempotency_keys'

#: Индекс под периодическую уборку просроченных ключей (T-4.4).
EXPIRY_INDEX = 'ix_idempotency_keys_expires_at'

#: Тело запроса, отпечаток которого кладётся в ключ. Ровно тот формат, который
#: хешируют сценарии T-2.4 и T-2.7: тест обязан работать с настоящим значением,
#: иначе он проверял бы строку, которой в проде не бывает.
REQUEST_BODY = b'{"amount": "100.000"}'
