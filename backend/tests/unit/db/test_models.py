"""Тесты схемы хранения: состав таблиц, ограничения и индексы (T-3.1).

Тесты работают по метаданным SQLAlchemy, без подключения к Postgres. Это
осознанно: DoD задачи — «схема описана, constraints и индексы на месте», а
проверять форму DDL не значит проверять её работу в базе. Реальное поведение
ограничений на живом Postgres проверяется интеграционными тестами (T-3.7) и
миграцией (T-3.2), для которых нужен стенд; здесь же важно другое — что
схема не «разъехалась» с доменом.

Особое внимание уделено выводам из доменных констант. Если бы ширины колонок
стояли числами прямо в моделях, проверять тут было бы нечего: тест на «колонка
``NUMERIC(27,3)``» зафиксировал бы магическое число, а не правило «колонка
вмещает любую валидную сумму». Поэтому тесты сравнивают метаданные с доменом:
добавим валюту с тремя знаками — тест упадёт, если про неё забыли.
"""

from typing import Any

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.money import MAX_MAGNITUDE
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from src.core_service.domain.versioning import INITIAL_VERSION, MIN_VERSION
from src.db.models import (
    AccountModel,
    Base,
    IdempotencyKeyModel,
    OutboxMessageModel,
    PaymentModel,
    ProcessedEventModel,
)
from src.db.models.base import (
    CONSUMER_NAME_LENGTH,
    EVENT_ID_LENGTH,
    MONEY_PRECISION,
    MONEY_SCALE,
    NAMING_CONVENTION,
    SHA256_HEX_LENGTH,
)

#: Все таблицы эпика. Лишняя таблица в схеме — не «будущая функциональность»,
#: а незаявленное изменение контракта: миграция создаст её молча, и первое же
#: расхождение с планом обнаружится на проде.
EXPECTED_TABLES = frozenset(
    {
        'accounts',
        'payments',
        'outbox',
        'idempotency_keys',
        'processed_events',
        'provider_webhook_events',
    }
)


def _check_constraint_texts(table_name: str) -> dict[str, str]:
    """Тексты всех ``CHECK``-ограничений таблицы по именам.

    Ограничения собираются с bind-параметрами ( Postgres получает их только при
    генерации DDL), поэтому текст рендерится с ``literal_binds``: так видны
    именно те значения, которые уедут в базу, — и есть возможность сравнить их
    с доменным перечислением.
    """
    table = Base.metadata.tables[table_name]
    dialect = postgresql.dialect()
    return {
        str(constraint.name): str(constraint.sqltext.compile(dialect=dialect, compile_kwargs={'literal_binds': True}))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _server_default_text(column: Column[Any]) -> str:
    """Значение ``server_default`` колонки строкой.

    ``server_default`` хранится как SQL-выражение (``TextClause``), поэтому число
    из доменной константы сравнивается с его текстом, а не с самим объектом.
    """
    return str(column.server_default.arg)


def _index_names(table_name: str) -> set[str]:
    """Имена всех индексов таблицы."""
    return {str(index.name) for index in Base.metadata.tables[table_name].indexes}


def test_metadata_contains_exactly_planned_tables() -> None:
    """В схеме ровно те таблицы, что заявлены в T-3.1 — и никаких лишних."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_naming_convention_is_declared() -> None:
    """Имена ограничений выводятся по шаблону, иначе миграции «дырявят»."""
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    for convention_key in ('ix', 'uq', 'ck', 'fk', 'pk'):
        assert convention_key in NAMING_CONVENTION


def test_money_column_precision_comes_from_domain_limits() -> None:
    """Точность денежной колонки выводится из домена, а не выдумана.

    Проверяется ровно то свойство, ради которого константы вынесены в ``base.py``:
    любая сумма, которую домен признал валидной, помещается в колонку.
    """
    assert max(currency.minor_unit for currency in Currency) == MONEY_SCALE
    assert MONEY_PRECISION == MAX_MAGNITUDE + MONEY_SCALE

    money_columns = [
        column
        for table_name in EXPECTED_TABLES
        for column in Base.metadata.tables[table_name].columns
        if column.type.__class__.__name__ == 'Numeric'
    ]
    assert money_columns, 'денежных колонок в схеме нет — NUMERIC где-то потерялся'
    for column in money_columns:
        assert column.type.precision == MONEY_PRECISION
        assert column.type.scale == MONEY_SCALE


# --- accounts -----------------------------------------------------------------


def test_account_identity_and_money_columns() -> None:
    """Счёт опознаётся по UUID, баланс — точная дробь, валюта — код из набора."""
    columns = AccountModel.__table__.columns

    assert columns['id'].primary_key
    assert not columns['id'].nullable
    assert not columns['balance'].nullable
    assert not columns['currency'].nullable
    assert columns['currency'].type.length == len(Currency.RUB.value)
    assert not columns['is_blocked'].nullable
    assert columns['is_blocked'].server_default is not None


def test_account_version_default_and_floor_come_from_domain() -> None:
    """Версия по умолчанию и запрет меньшей — одна константа ``versioning.py``."""
    version = AccountModel.__table__.columns['version']

    assert not version.nullable
    assert _server_default_text(version) == str(INITIAL_VERSION)
    assert _check_constraint_texts('accounts')['ck_accounts_version_positive'] == (f'version >= {MIN_VERSION}')


def test_account_currency_is_limited_to_domain_enum() -> None:
    """``CHECK`` по валюте строится из ``Currency``, поэтому список не вручную."""
    allowed = ', '.join(f"'{member.value}'" for member in Currency)
    assert _check_constraint_texts('accounts')['ck_accounts_currency_enum_values'] == (f'currency IN ({allowed})')


def test_account_balance_can_not_go_negative() -> None:
    """Отрицательный баланс в базе — испорченные деньги, а не долг."""
    assert 'balance >= 0' in _check_constraint_texts('accounts')['ck_accounts_balance_non_negative']


# --- payments -----------------------------------------------------------------


def test_payment_status_is_limited_to_domain_enum() -> None:
    """Статус в базе — из ``PaymentStatus``: значения выводит статус-машина."""
    allowed = ', '.join(f"'{member.value}'" for member in PaymentStatus)
    assert _check_constraint_texts('payments')['ck_payments_status_enum_values'] == (f'status IN ({allowed})')


def test_payment_provider_status_is_limited_to_provider_enum() -> None:
    """Провайдерский статус — из ``ProviderStatus``, а не произвольная строка."""
    allowed = ', '.join(f"'{member.value}'" for member in ProviderStatus)
    assert _check_constraint_texts('payments')['ck_payments_provider_status_enum_values'] == (
        f'provider_status IN ({allowed})'
    )


def test_payment_amount_must_be_strictly_positive() -> None:
    """Нулевой платёж — ошибка сценария: по нему нечего проводить и отменять."""
    checks = _check_constraint_texts('payments')

    assert 'amount > 0' in checks['ck_payments_amount_positive']
    assert not PaymentModel.__table__.columns['amount'].nullable


def test_payment_version_matches_account_version_rules() -> None:
    """Правило версий у платежа и счёта одно — из ``versioning.py``."""
    version = PaymentModel.__table__.columns['version']
    checks = _check_constraint_texts('payments')

    assert _server_default_text(version) == str(INITIAL_VERSION)
    assert checks['ck_payments_version_positive'] == f'version >= {MIN_VERSION}'


def test_payment_provider_columns_are_optional_until_provider_is_called() -> None:
    """До вызова провайдера у платежа нет ни его идентификатора, ни статуса."""
    columns = PaymentModel.__table__.columns

    assert columns['provider_payment_id'].nullable
    assert columns['provider_status'].nullable
    assert columns['failure_reason'].nullable
    assert not columns['status'].nullable


def test_payment_provider_payment_id_is_unique() -> None:
    """Два разных платежа с одной операцией провайдера — всегда ошибка данных."""
    unique_columns = [
        {column.name for column in constraint.columns}
        for constraint in PaymentModel.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert {'provider_payment_id'} in unique_columns


def test_payment_foreign_key_to_account_forbids_cascade_delete() -> None:
    """Удалить счёт с платежами нельзя: каскад стёр бы историю движения денег."""
    foreign_keys = [
        constraint for constraint in PaymentModel.__table__.constraints if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert {element.parent.name for element in foreign_key.elements} == {'from_account_id'}
    assert {element.target_fullname for element in foreign_key.elements} == {'accounts.id'}
    assert foreign_key.ondelete == 'RESTRICT'


def test_payment_has_indexes_for_account_history_and_stuck_payments() -> None:
    """Индексы под реальные выборки: платежи счёта и «зависшие» по статусу."""
    assert _index_names('payments') >= {'ix_payments_from_account_id', 'ix_payments_status_updated_at'}

    stuck_search = next(
        index for index in PaymentModel.__table__.indexes if index.name == 'ix_payments_status_updated_at'
    )
    assert [column.name for column in stuck_search.columns] == ['status', 'updated_at']


# --- outbox -------------------------------------------------------------------


def test_outbox_primary_key_is_domain_event_id() -> None:
    """PK outbox — ``event_id`` события: повторная запись дубля не создаст."""
    outbox = Base.metadata.tables['outbox']

    assert [column.name for column in outbox.primary_key.columns] == ['id']
    assert outbox.columns['payload'].type.__class__.__name__ == 'JSONB'
    assert not outbox.columns['payload'].nullable


def test_outbox_publish_bookkeeping_columns() -> None:
    """Метки публикации и неудач нужны relay'ю T-8.1 и метрике лага (T-12.8)."""
    outbox = OutboxMessageModel.__table__.columns

    assert outbox['published_at'].nullable
    assert not outbox['occurred_at'].nullable
    assert outbox['attempts'].server_default is not None
    assert outbox['last_error'].nullable


def test_outbox_index_covers_only_unpublished_rows() -> None:
    """Индекс частичный: relay опрашивает очередь, история ему не нужна.

    Проверяется именно условие ``WHERE published_at IS NULL`` — с индексом на всей
    таблице очередь и история лежали бы в одном индексе, и он рос бы вместе с
    архивом событий вместо размера очереди.
    """
    index = next(index for index in OutboxMessageModel.__table__.indexes if index.name == 'ix_outbox_unpublished')

    assert [column.name for column in index.columns] == ['created_at']
    assert index.dialect_options['postgresql']['where'] is not None
    where_text = str(index.dialect_options['postgresql']['where'])
    assert 'published_at IS NULL' in where_text


# --- idempotency_keys ---------------------------------------------------------


def test_idempotency_key_width_is_taken_from_port_contract() -> None:
    """Ширина колонки — из порта, а не из числа в модели.

    Ключ приходит от клиента и может оказаться длиннее 64 символов; если бы
    модель держала свою цифру, расхождение слоёв обнаружилось бы уже на таком
    ключе, и только в проде.
    """
    key_column = IdempotencyKeyModel.__table__.columns['key']

    assert key_column.primary_key
    assert key_column.type.length == MAX_IDEMPOTENCY_KEY_LENGTH


def test_idempotency_record_keeps_request_hash_and_optional_response() -> None:
    """Отпечаток тела обязателен, ответ может быть ещё не записан."""
    columns = IdempotencyKeyModel.__table__.columns

    assert columns['request_hash'].type.length == SHA256_HEX_LENGTH
    assert not columns['request_hash'].nullable
    assert columns['response'].nullable
    assert not columns['expires_at'].nullable


def test_idempotency_expiry_is_after_creation() -> None:
    """Срок жизни обязан быть положительным, иначе ключ умрёт раньше повтора."""
    assert (
        'expires_at > created_at'
        in _check_constraint_texts('idempotency_keys')['ck_idempotency_keys_expires_after_created']
    )


def test_idempotency_has_index_on_expiry_for_cleanup() -> None:
    """Очистка T-4.4 удаляет по ``expires_at`` — индекс обязателен."""
    assert 'ix_idempotency_keys_expires_at' in _index_names('idempotency_keys')


# --- processed_events ---------------------------------------------------------


def test_processed_event_primary_key_is_consumer_and_event() -> None:
    """Ключ «уже обработано» — консьюмер плюс событие, и БД проверяет его сама."""
    processed = Base.metadata.tables['processed_events']

    assert [column.name for column in processed.primary_key.columns] == ['consumer', 'event_id']
    assert processed.columns['consumer'].type.length == CONSUMER_NAME_LENGTH
    assert processed.columns['event_id'].type.length == EVENT_ID_LENGTH


def test_processed_event_has_index_on_processing_time() -> None:
    """Записи убираются по времени обработки (политика хранения — ЭПИК 14)."""
    assert 'ix_processed_events_processed_at' in _index_names('processed_events')
    assert not ProcessedEventModel.__table__.columns['processed_at'].nullable


# --- provider_webhook_events --------------------------------------------------


def test_provider_webhook_is_keyed_by_provider_event_id() -> None:
    """PK — идентификатор события провайдера: повторная доставка отсеется на БД."""
    webhooks = Base.metadata.tables['provider_webhook_events']

    assert [column.name for column in webhooks.primary_key.columns] == ['provider_event_id']
    assert webhooks.columns['provider_event_id'].type.length == MAX_IDEMPOTENCY_KEY_LENGTH
    assert not webhooks.columns['provider_payment_id'].nullable
    assert webhooks.columns['payload'].type.__class__.__name__ == 'JSONB'


def test_provider_webhook_status_is_limited_to_provider_enum() -> None:
    """В журнале лежит ровно то, что прислал шлюз, но только из нашей шкалы."""
    allowed = ', '.join(f"'{member.value}'" for member in ProviderStatus)
    assert (
        _check_constraint_texts('provider_webhook_events')['ck_provider_webhook_events_provider_status_enum_values']
        == f'provider_status IN ({allowed})'
    )


def test_provider_webhook_rejects_empty_payment_id() -> None:
    """Уведомление без идентификатора операции нечем сопоставить с платежом."""
    assert (
        "provider_payment_id != ''"
        in _check_constraint_texts('provider_webhook_events')[
            'ck_provider_webhook_events_provider_payment_id_not_empty'
        ]
    )


def test_provider_webhook_has_index_on_receipt_time() -> None:
    """Разбор инцидентов и уборка идут по времени приёма уведомления."""
    assert 'ix_provider_webhook_events_received_at' in _index_names('provider_webhook_events')


# --- рендеринг схемы ---------------------------------------------------------


@pytest.mark.parametrize('table_name', sorted(EXPECTED_TABLES))
def test_every_table_renders_to_postgres_ddl(table_name: str) -> None:
    """Схема обязана собираться в DDL, а не падать на этапе миграции.

    Самая частая причина падения здесь — забытое имя у ``CHECK`` при объявленном
    соглашении имён: ошибка проявляется только при генерации DDL, то есть уже на
    ``alembic upgrade``.
    """
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=postgresql.dialect()))

    assert ddl.strip().startswith(f'CREATE TABLE {table_name} (')
    assert 'CONSTRAINT' in ddl
