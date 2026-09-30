"""Проверка миграций против живой схемы Postgres (T-3.2).

DoD задачи — «``alembic upgrade head`` и ``alembic downgrade base`` работают
корректно». Единственный честный способ это подтвердить — прогнать оба
направления по-настоящему: рендеринг DDL ничего не говорит о том, применится ли
DDL, и тем более ничего — про то, что откат не оставит половину схемы.

Модуль отвечает за целый цикл ревизий: сохранённые ответы, повторное применение и
обратимость. Схема отдельных таблиц проверяется по месту — там, где у таблицы есть
свой смысл (например, ``idempotency_keys`` в T-4.1), — а этот файл следит, чтобы
миграции вообще создают то, что задумано.

Стенд не разрушается: тесты приводят базу к нужному состоянию сами, а после
каждого теста возвращают ``head``. Без Postgres-стенда тесты работают против
временного контейнера (testcontainer, T-3.7), а когда нет ни стенда, ни Docker,
модуль пропускается — ``make test`` обязан оставаться зелёным без Docker
(AGENT.md, §5).
"""

import asyncio

import pytest
from alembic import command

from src.run_migrations import EXPECTED_TABLES, build_alembic_config
from tests.integration.conftest import list_public_tables, query_first_value

#: Фикстуры готовности и возврата к ``head`` запрашиваются явно: без них откат,
#: прогнанный тестом, остался бы в базе навсегда.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures('postgres_stack', 'restore_head'),
]

#: Служебная таблица Alembic переживает откат: в ней хранится номер ревизии, и
#: без неё Alembic не поймёт, с чего продолжать. Единственная таблица, которая
#: обязана остаться после ``downgrade base``.
ALEMBIC_TABLE = 'alembic_version'

#: Проверка того, что частичный индекс outbox ушёл вместе с таблицей.
INDEX_QUERY = "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ix_outbox_unpublished'"


def test_upgrade_head_creates_every_planned_table() -> None:
    """DoD: ``upgrade head`` создаёт все шесть таблиц схемы.

    Перед проверкой делается полный откат, чтобы тест не прошёл «по инерции» —
    на базе, где таблицы остались от предыдущего прогона.
    """
    config = build_alembic_config()
    command.downgrade(config, 'base')
    assert asyncio.run(list_public_tables()) == {ALEMBIC_TABLE}

    command.upgrade(config, 'head')
    created = asyncio.run(list_public_tables())

    assert created >= EXPECTED_TABLES


def test_downgrade_base_removes_every_table() -> None:
    """DoD: ``downgrade base`` убирает все таблицы, кроме служебной.

    Проверяется и сам откат, и отсутствие «хвоста»: повторный ``upgrade`` на такой
    базе падал бы, а заметить это можно было бы только на следующем развёртывании.
    """
    config = build_alembic_config()
    command.upgrade(config, 'head')
    assert asyncio.run(list_public_tables()) >= EXPECTED_TABLES

    command.downgrade(config, 'base')
    remaining = asyncio.run(list_public_tables())

    assert remaining == {ALEMBIC_TABLE}


def test_migrations_are_reversible_and_reappliable() -> None:
    """Цикл «применить — откатить — применить» сходится.

    Откат, который нельзя повторить, — это миграция, ломающая рестарт сервиса:
    на проде это означает, что откатиться нельзя, а вперёд — можно.
    """
    config = build_alembic_config()
    for _ in range(2):
        command.upgrade(config, 'head')
        assert asyncio.run(list_public_tables()) >= EXPECTED_TABLES
        command.downgrade(config, 'base')
        assert asyncio.run(list_public_tables()) == {ALEMBIC_TABLE}


def test_repeated_upgrade_is_a_noop() -> None:
    """Повторный ``upgrade head`` не падает и не меняет схему.

    Именно этим свойством пользуется контейнер миграций: compose запускает его
    при каждом ``up``, и «уже применено» должно быть штатным состоянием, а не
    ошибкой.
    """
    config = build_alembic_config()
    command.upgrade(config, 'head')
    first = asyncio.run(list_public_tables())

    command.upgrade(config, 'head')
    second = asyncio.run(list_public_tables())

    assert first == second


def test_downgrade_drops_partial_index_with_its_table() -> None:
    """Откат убирает частичный индекс вместе с таблицей.

    Рукописные миграции часто забывают ``drop_index`` перед ``drop_table`` — и
    падают на существующем индексе. У outbox индекс частичный, с ``WHERE``, так
    что его имя в каталоге отличается от обычного и требует явной проверки.
    """
    config = build_alembic_config()
    command.upgrade(config, 'head')
    assert asyncio.run(query_first_value(INDEX_QUERY)) is not None

    command.downgrade(config, 'base')

    assert asyncio.run(query_first_value(INDEX_QUERY)) is None
