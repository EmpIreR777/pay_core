"""Хранилище идемпотентности: Redis — захват и кэш, Postgres — ответы (T-4.2).

Адаптер порта :class:`IdempotencyStore`. Задача уровня — повторный запрос с тем же
ключом возвращает ранее сохранённый ответ и **не** выполняет операцию второй раз.
Достигается это двумя уровнями хранения, у каждого своя работа:

* **Redis** держит *захват* ключа (``SET NX EX``) и кэш недавнего ответа. Захват
  нужен, чтобы два параллельных дубля одного запроса не исполнились одновременно:
  первый проходит к работе, второй получает отказ. Живёт он в оперативной памяти и
  исчезает вместе с процессом — потерять его нечего.
* **Postgres** хранит *ответ* в ``idempotency_keys``. Это единственное долговременное
  место: клиентские ретраи после обрыва связи или рестарта сервиса находят ответ
  именно там.

Решения, которые стоит проговорить:

* **Захват и кэш ответа лежат в разных пространствах имён.** Иначе запись ответа
  заняла бы тот же ключ, что и резервация, а последующий ``release`` (он вызывается
  при отказе сценария) удалил бы из кэша уже сохранённый ответ. Разные префиксы
  делают эти операции независимыми, а не «случайно совпадающими по имени».
  Формат ключей задан здесь, а не в порту: сценарий их никогда не строит — он
  передаёт хранилищу только клиентский ключ, — и второй источник правды в порту
  разошёлся бы с первым при первом же переименовании.
* **Просроченная запись не считается ответом.** Срок жизни записи (T-4.1) —
  это ровно то окно, в котором повтор обещан вернуть тот же ответ. ``get`` отсекает
  записи с ``expires_at`` в прошлом, поэтому по истёкшему ключу запрос проходит как
  новый. Без этой проверки ключ нельзя было бы переиспользовать никогда, а кэш
  в Redis отдавал бы ответ уже после срока.
* **Ответ под ключом не перезаписывается, пока он жив.** ``save`` обновляет строку
  только если она просрочена. Иначе гонка двух исполнений (если захват всё же
  проиграл) дала бы два разных ответа под одним ключом, и инвариант «повтор
  возвращает тот же ответ» перестал бы выполняться. Просроченная же строка
  переиспользуется свободно: это уже другой запрос, и его ответ вправе занять место.
* **Отказ Redis не роняет чтение, но роняет захват.** Чтение ответа — это кэш:
  при недоступном Redis сценарий идёт в Postgres и получает тот же ответ, просто
  медленнее. Захват же — механизм корректности: молча пропустить параллельный дубль
  нельзя, поэтому ошибка Redis на ``try_acquire`` поднимается наружу, и клиент
  получит отказ с честной причиной, а не ложное «уже выполняется».
* **Освобождение захвата не поднимает ошибку.** Резервация сама истекает по TTL, и
  сценарий зовёт ``release`` в ветке отказа, чтобы не потерять исходную причину:
  поднятая здесь ошибка Redis замаскировала бы настоящую — «недостаточно средств»
  или отказ провайдера, — и клиент получил бы неверный диагноз.
* **Ответ в Redis кэшируется с TTL, равным остатку срока записи.** Кэш не имеет
  права пережить саму запись: иначе после срока он отдавал бы ответ, который
  Postgres уже не отдаёт.
"""

from __future__ import annotations

import json
from datetime import datetime
from math import ceil
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core_service.application.ports.clock import Clock
from src.core_service.application.ports.idempotency_store import IdempotencyRecord
from src.db.models.idempotency_key import IdempotencyKeyModel

#: Пространство имён резерваций. Существует только пока сценарий работает с ключом,
#: и снимается либо успешной записью ответа, либо отказом, либо истечением TTL.
RESERVATION_KEY_PREFIX: Final = 'idempotency:lock:'

#: Пространство имён кэша ответов. Отдельное от резерваций намеренно: см. модульный
#: докстринг. Живёт ровно до ``expires_at`` записи.
RECORD_KEY_PREFIX: Final = 'idempotency:record:'

#: Значение резервации. Содержимым не читается никогда: важен сам факт наличия ключа,
#: который и говорит «операция уже идёт». Пишем осмысленное значение, а не ``1``,
#: чтобы ``redis-cli KEYS`` на стенде показывал читаемое содержимое.
RESERVATION_VALUE: Final = 'reserved'


def reservation_key(key: str) -> str:
    """Ключ Redis, под которым живёт захват клиентского ключа."""
    return f'{RESERVATION_KEY_PREFIX}{key}'


def record_key(key: str) -> str:
    """Ключ Redis, под которым кэшируется сохранённый ответ."""
    return f'{RECORD_KEY_PREFIX}{key}'


def serialize_record(record: IdempotencyRecord) -> str:
    """Привести запись к JSON для кэша в Redis.

    Ответ хранится объектом, а не текстом: иначе повторный запрос разбирал бы строку
    вместо готовой структуры, и формат ответа жил бы в двух местах — в DTO сценария
    и в разборе кэша. Время уходит в ISO-8601 и обязано остаться timezone-aware:
    разница между наивным и осведомлённым временем съела бы срок жизни записи.
    """
    return json.dumps(
        {
            'key': record.key,
            'request_hash': record.request_hash,
            'response': dict(record.response) if record.response is not None else None,
            'created_at': record.created_at.isoformat(),
            'expires_at': record.expires_at.isoformat(),
        },
    )


def deserialize_record(raw: str) -> IdempotencyRecord | None:
    """Восстановить запись из JSON кэша или ``None``, если разобрать не удалось.

    Повреждённая запись в кэше — это отсутствие кэша, а не ошибка запроса: ответ
    лежит ещё и в Postgres, поэтому чтение просто продолжает по дороге к базе.
    Поднимать исключение здесь значило бы отдавать 500 из-за значения, которое
    по определению можно забыть и перезаписать.
    """
    try:
        payload = json.loads(raw)
        return IdempotencyRecord(
            key=str(payload['key']),
            request_hash=str(payload['request_hash']),
            response=payload['response'],
            created_at=datetime.fromisoformat(str(payload['created_at'])),
            expires_at=datetime.fromisoformat(str(payload['expires_at'])),
        )
    except KeyError, TypeError, ValueError:
        return None


def new_key_model(record: IdempotencyRecord) -> IdempotencyKeyModel:
    """Строка ``idempotency_keys`` по записи хранилища.

    ``created_at`` передаётся явно, хотя у колонки есть ``server_default``: проверка
    ``expires_at > created_at`` сравнивает с серверным ``now()``, и если бы срок
    считался от него же, то часы приложения, ушедшие вперёд, ломали бы вставку на
    ровном месте. Здесь обе метки приходят из одного источника — часов сценария.
    """
    return IdempotencyKeyModel(
        key=record.key,
        request_hash=record.request_hash,
        response=dict(record.response) if record.response is not None else None,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


class RedisPostgresIdempotencyStore:
    """Хранилище идемпотентности на Redis и Postgres: реализация порта.

    Живёт не в ``UnitOfWork`` и от транзакций саги не зависит: запись ответа
    обязана пережить и откат саги (деньги списаны, провайдер ответил — повтор
    обязан получить этот же платёж), и наоборот, не должна делить транзакцию с
    шагами, которые к ней отношения не имеют. Поэтому у адаптера своя короткая
    сессия на операцию.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        session_maker: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        """:param redis: клиент Redis; резервация и кэш ответов;
        :param session_maker: фабрика сессий приложения — движком владеет процесс,
            и поднимать его внутри хранилища нельзя;
        :param clock: часы для срока жизни кэша. Ответ в Redis исчезает по TTL, и
            этот TTL обязан считаться тем же временем, что и ``expires_at`` записи,
            иначе кэш пережил бы саму запись.
        """
        self._redis = redis
        self._session_maker = session_maker
        self._clock = clock

    async def get(self, key: str) -> IdempotencyRecord | None:
        """Вернуть сохранённый ответ по ключу или ``None``.

        Порядок чтения — кэш, затем база: повторный запрос обслуживается из Redis,
        а в Postgres попадает только при промахе (холодный кэш после рестарта).
        Найденную в базе запись кэшируем — иначе каждый дубль ходил бы в Postgres.
        """
        cached = await self._read_cache(key)
        if cached is not None:
            return cached

        stored = await self._read_row(key)
        if stored is not None:
            await self._write_cache(stored)
        return stored

    async def save(self, record: IdempotencyRecord) -> None:
        """Сохранить ответ: в Postgres — надолго, в Redis — до конца срока.

        В кэш кладётся не поданный ответ, а тот, который **уцелел в базе**.
        Разница видна на проигравшей гонке: если захват был обыгран и база
        отказалась менять живой ответ, поданный всё равно уехал бы в Redis — и
        повтор получил бы из кэша чужой результат, а после истечения кэша тот же
        запрос вернул бы настоящий. Один и тот же ключ отдавал бы два разных
        ответа, и «повтор возвращает тот же ответ» перестал бы выполняться.
        Поэтому кэш — зеркало долговременной истины, а не второй её источник.

        Захват снимается после записи. До неё «ключ занят» означало бы «операция
        идёт», а после — «ответ сохранён», и клиентский повтор обязан получить
        этот ответ, а не отказ «уже выполняется». Порядок именно такой: освободить
        раньше значило бы открыть окно, в котором дубль прошёл бы в сагу, не
        найдя ответа.
        """
        await self._write_row(record)
        durable = await self._read_row(record.key)
        if durable is not None:
            await self._write_cache(durable)
        await self.release(record.key)

    async def try_acquire(self, key: str, *, ttl_seconds: int) -> bool:
        """Захватить ключ через ``SET NX EX``; ``True``, если захват наш.

        :raises RedisError: Redis недоступен. Ответ на повтор здесь подменять
            нельзя: «слышу, что занято» — ложь, которой сценарий поверит и
            отдаст клиенту отказ по несуществующей причине.
        """
        acquired = await self._redis.set(
            reservation_key(key),
            RESERVATION_VALUE,
            ex=ttl_seconds,
            nx=True,
        )
        return bool(acquired)

    async def _read_cache(self, key: str) -> IdempotencyRecord | None:
        """Прочитать ответ из Redis; ``None`` — промах, поломка или срок вышел.

        Кэш — ускоритель, а не источник правды, поэтому его поломка равносильна
        промаху: сценарий получит тот же ответ из Postgres, просто медленнее.
        """
        try:
            raw = await self._redis.get(record_key(key))
        except RedisError:
            return None
        if raw is None:
            return None

        record = deserialize_record(self._as_text(raw))
        if record is None or self._is_expired(record):
            return None
        return record

    async def _write_cache(self, record: IdempotencyRecord) -> None:
        """Положить ответ в Redis на остаток его срока.

        TTL считается от ``expires_at``, а не от момента записи: кэш, проживший
        дольше самой записи, отдавал бы ответ, который Postgres уже считает
        просроченным. Ошибка записи не поднимается по той же причине, что и
        ошибка чтения, — ответ всё равно сохранён в Postgres.
        """
        ttl_seconds = self._cache_ttl_seconds(record)
        if ttl_seconds <= 0:
            return
        try:
            await self._redis.set(record_key(record.key), serialize_record(record), ex=ttl_seconds)
        except RedisError:
            return

    async def _read_row(self, key: str) -> IdempotencyRecord | None:
        """Прочитать ответ из Postgres, пропустив просроченные записи.

        Отсев по сроку сделан в самом SQL, а не в Python: иначе срок решал бы код
        адаптера, а данные отдавала база, и «живая» запись и «действительная» были
        бы разными вещами.
        """
        async with self._session_maker() as session:
            statement = select(IdempotencyKeyModel).where(
                IdempotencyKeyModel.key == key,
                IdempotencyKeyModel.expires_at > self._clock.now(),
            )
            model = (await session.execute(statement)).scalar_one_or_none()
        return None if model is None else self._to_record(model)

    async def _write_row(self, record: IdempotencyRecord) -> None:
        """Записать ответ в Postgres, обновив строку лишь если она просрочена.

        ``ON CONFLICT ... DO UPDATE ... WHERE expires_at <= now()`` — то есть живой
        ответ не перезаписывается никогда. Иначе проигравший захват дописал бы свой
        ответ поверх чужого, и по одному ключу разошлись бы два разных результата:
        повтор перестал бы возвращать «тот же» ответ. Строка под ключом, у которой
        срок вышел, переиспользуется свободно — это уже другой запрос.
        """
        response = dict(record.response) if record.response is not None else None
        async with self._session_maker() as session:
            statement = (
                postgres_insert(IdempotencyKeyModel)
                .values(
                    key=record.key,
                    request_hash=record.request_hash,
                    response=response,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                )
                .on_conflict_do_update(
                    index_elements=[IdempotencyKeyModel.key],
                    set_={
                        'request_hash': record.request_hash,
                        'response': response,
                        'created_at': record.created_at,
                        'expires_at': record.expires_at,
                    },
                    where=IdempotencyKeyModel.expires_at <= self._clock.now(),
                )
            )
            await session.execute(statement)
            await session.commit()

    def _is_expired(self, record: IdempotencyRecord) -> bool:
        """Просрочена ли запись по часам адаптера."""
        return record.expires_at <= self._clock.now()

    def _cache_ttl_seconds(self, record: IdempotencyRecord) -> int:
        """Остаток срока записи — целыми секундами для Redis.

        Округление вверх: ``0`` секунд Redis отверг, а «почти истёкшая» запись
        должна дожить своего срока, а не исчезнуть из кэша на последней секунде.
        Всё, что не влезает ни в одну целую секунду, в кэш не попадает вовсе.
        """
        remaining = (record.expires_at - self._clock.now()).total_seconds()
        return 0 if remaining <= 0 else ceil(remaining)

    def _to_record(self, model: IdempotencyKeyModel) -> IdempotencyRecord:
        """Собрать запись хранилища из строки ``idempotency_keys``."""
        return IdempotencyRecord(
            key=model.key,
            request_hash=model.request_hash,
            response=model.response,
            created_at=model.created_at,
            expires_at=model.expires_at,
        )

    def _as_text(self, raw: Any) -> str:
        """Привести ответ Redis к тексту.

        Клиент может быть настроен на ``decode_responses`` или нет, и оба варианта
        официально допустимы. Приводим здесь, в одном месте, чтобы разбор JSON не
        зависел от того, как поднят клиент в конкретном окружении.
        """
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def release(self, key: str) -> None:
        """Снять резервацию ключа.

        Ошибки Redis подавляются намеренно: резервация истекает сама по TTL, а
        поднятая здесь ошибка замаскировала бы настоящую причину отказа сценария
        (недостаточно средств, отказ провайдера) — клиент получил бы «Redis недоступен»
        вместо «недостаточно средств» и повторил бы запрос зря.
        """
        try:
            await self._redis.delete(reservation_key(key))
        except RedisError:
            return
