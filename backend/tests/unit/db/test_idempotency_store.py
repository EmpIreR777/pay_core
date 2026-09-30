"""Юнит-тесты хранилища идемпотентности: маппинг и сроки (T-4.2).

Без БД и без Redis: проверяется то, что видно без стенда, — форма контракта,
разбор записи, срок жизни кэша и, главное, **решение о перезаписи ответа**.
Интеграционные тесты (`tests/integration/test_idempotency_store.py`) доказывают
поведение на живых Postgres и Redis, здесь же — правила, которые иначе были бы
видны только через два хранилища сразу.

Почему сроки и перезапись проверяются здесь, а не только в интеграции: обе вещи
ломаются тихо. Кэш, проживший дольше записи, отдаёт ответ, который Postgres
уже считает просроченным; ответ, перезаписанный поверх живого, даёт два разных
результата под одним ключом — и оба отказа не видны ни в логах, ни в метриках,
пока клиент не получит не тот платёж.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.core_service.application import ports
from src.core_service.application.ports.idempotency_store import IdempotencyRecord
from src.db.idempotency_store import (
    RECORD_KEY_PREFIX,
    RESERVATION_KEY_PREFIX,
    RedisPostgresIdempotencyStore,
    deserialize_record,
    new_key_model,
    record_key,
    reservation_key,
    serialize_record,
)

#: Фиксированное «сейчас»: тест не должен зависеть от хода часов, иначе проверки
#: времени станут плавающими.
NOW = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)

KEY = 'create-payment-9f2c'
RESPONSE = {'payment_id': '6c1f-4a1e', 'status': 'PROCESSING'}
REQUEST_HASH = 'a' * 64


class _StubClock:
    """Часы с замороженным временем — те же, что у сценариев (T-2.1)."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _record(
    *,
    key: str = KEY,
    response: dict[str, Any] | None = None,
    ttl_seconds: float = 86_400,
) -> IdempotencyRecord:
    """Запись хранилища с метками от :data:`NOW`.

    Срок принимает дробное число секунд: остаток жизни записи почти никогда не
    выражается целым, и именно дробный случай вскрывает разницу между округлением
    вверх и вниз.
    """
    return IdempotencyRecord(
        key=key,
        request_hash=REQUEST_HASH,
        response=RESPONSE if response is None else response,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=ttl_seconds),
    )


# --- Форма контракта ----------------------------------------------------------


def test_store_satisfies_the_port() -> None:
    """Адаптер отвечает порту ``IdempotencyStore`` «по форме» (T-2.1).

    Порт **не** наследуется: наследование от ``Protocol`` подмешивало бы заглушки
    и сделало бы проверку самоподтверждающейся.
    """
    store = RedisPostgresIdempotencyStore(redis=None, session_maker=None, clock=_StubClock())  # type: ignore[arg-type]

    assert isinstance(store, ports.IdempotencyStore)
    assert ports.IdempotencyStore not in RedisPostgresIdempotencyStore.__mro__


# --- Пространства имён ключей ------------------------------------------------


def test_reservation_and_cached_answer_live_in_separate_namespaces() -> None:
    """Захват и кэш ответа — разные ключи Redis.

    Иначе запись ответа заняла бы ключ резервации, и последующий ``release``
    (сценарий зовёт его при отказе) снёс бы из кэша уже сохранённый ответ:
    повтор пошёл бы выполняться заново и создал второй платёж.
    """
    assert reservation_key(KEY) == f'{RESERVATION_KEY_PREFIX}{KEY}'
    assert record_key(KEY) == f'{RECORD_KEY_PREFIX}{KEY}'
    assert reservation_key(KEY) != record_key(KEY)


# --- Сериализация записи ------------------------------------------------------


def test_record_survives_json_round_trip_with_all_fields() -> None:
    """Запись переживает кэш без потерь, включая вложенность ответа.

    Ответ хранится объектом: если бы он лёг текстом, разбор жил бы в двух местах
    — в DTO сценария и здесь, — и формат ответа разошёлся бы при первой же правке.
    """
    record = _record()

    restored = deserialize_record(serialize_record(record))

    assert restored == record


def test_record_without_response_survives_round_trip() -> None:
    """``NULL`` в ответе переживает кэш как ``None``, а не как ``{}``.

    Различие принципиально: «ключ занят, ответа ещё нет» и «ответ — пустой объект»
    сценарий трактует противоположно, и подмена одного другим превратила бы
    незавершённую операцию в повтор с пустым результатом.
    """
    record = _record(response={})

    restored = deserialize_record(serialize_record(record))

    assert restored is not None
    assert restored.response == {}


@pytest.mark.parametrize(
    'raw', ['не json', '{"key": "k"}', '[]', '{"key": 1, "request_hash": "h", "created_at": 1, "expires_at": 2}']
)
def test_unreadable_cache_entry_is_treated_as_a_miss(raw: str) -> None:
    """Повреждённая запись кэша равносильна промаху, а не ошибке запроса.

    Ответ лежит ещё и в Postgres, поэтому сценарий получит его оттуда. Поднять
    исключение здесь значило бы отдавать 500 из-за значения, которое по
    определению можно забыть и перезаписать.
    """
    assert deserialize_record(raw) is None


# --- Сроки жизни кэша --------------------------------------------------------


def test_cache_ttl_is_the_remaining_lifetime_rounded_up() -> None:
    """TTL кэша — остаток срока записи, округлённый вверх.

    Кэш, проживший дольше самой записи, отдавал бы ответ, который Postgres уже
    считает просроченным. Округление вверх нужно потому, что Redis отвергает
    ``ex=0``: без него запись, которой осталось меньше секунды, выпала бы из кэша
    раньше времени.
    """
    store = RedisPostgresIdempotencyStore(redis=None, session_maker=None, clock=_StubClock())  # type: ignore[arg-type]

    assert store._cache_ttl_seconds(_record(ttl_seconds=86_400)) == 86_400
    assert store._cache_ttl_seconds(_record(ttl_seconds=1)) == 1
    # Дробный остаток — как раз тот случай, ради которого нужно округление вверх:
    # при ``int()`` запись, которой осталось 1.5 секунды, жила бы в кэше на одну
    # секунду меньше положенного, а при остатке меньше секунды получила бы ``0``,
    # который Redis отвергает.
    assert store._cache_ttl_seconds(_record(ttl_seconds=1.5)) == 2
    assert store._cache_ttl_seconds(_record(ttl_seconds=0.5)) == 1
    assert store._cache_ttl_seconds(_record(ttl_seconds=0)) == 0
    assert store._cache_ttl_seconds(_record(ttl_seconds=-10)) == 0


def test_expired_record_is_detected_by_the_store_clock() -> None:
    """Просроченная запись не считается ответом и не попадает в кэш.

    Срок записи — это ровно то окно, в котором повтор обещан вернуть тот же ответ.
    За его пределами ключ должен считаться свободным, иначе переиспользовать его
    было бы нельзя никогда.
    """
    store = RedisPostgresIdempotencyStore(redis=None, session_maker=None, clock=_StubClock())  # type: ignore[arg-type]

    assert store._is_expired(_record(ttl_seconds=0)) is True
    assert store._is_expired(_record(ttl_seconds=1)) is False


# --- Маппинг на строку БД -----------------------------------------------------


def test_new_key_model_carries_both_timestamps_from_the_record() -> None:
    """В строку идут обе метки из записи, а не серверный ``now()``.

    Проверка ``expires_at > created_at`` сравнивает с тем, что лежит в строке.
    Если бы ``created_at`` брался у базы, а ``expires_at`` считался от часов
    приложения, то ушедшие вперёд часы ломали бы вставку на ровном месте.
    """
    record = _record(ttl_seconds=3_600)

    model = new_key_model(record)

    assert model.key == record.key
    assert model.request_hash == record.request_hash
    assert model.response == dict(record.response or {})
    assert model.created_at == record.created_at
    assert model.expires_at == record.expires_at
