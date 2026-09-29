"""Декларативная база, общие колонки и ширины, выведенные из домена (T-3.1).

Модуль — единственный владелец двух вещей, которые иначе разъезжаются по слоям:

* **Соглашение об именах ограничений** (:data:`NAMING_CONVENTION`). Имена
  первичных ключей, внешних ключей, уникальностей и проверок выводятся по
  шаблону, а не пишутся руками в каждой модели. Alembic сравнивает ограничения по
  именам: без шаблона однажды в двух таблицах появятся два ``CHECK`` с
  одинаковым именем, и ``autogenerate`` начнёт предлагать миграции, которые
  ничего не меняют.
* **Ширины колонок**, посчитанные из доменных констант, а не выдуманные заново.
  Точность ``NUMERIC`` берётся из предела величины суммы и максимальной дробной
  части среди валют шлюза, длина колонки ``idempotency_keys.key`` — из границы
  порта хранилища ключей. Если бы цифры стояли в самих моделях, они бы со
  временем разошлись с доменом молча, и первый упавший ``NumericValueOutOfRange``
  оказался бы на проде.

Здесь же — общие миксины времени жизни строки. Отдельные ``created_at`` в
каждой модели означали бы, что про них однажды забыли.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    MetaData,
    Numeric,
    column,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.money import MAX_MAGNITUDE
from src.core_service.domain.value_objects.payment_status import PaymentStatus

#: Шаблоны имён ограничений и индексов. ``%(constraint_name)s`` в шаблоне
#: проверок означает, что забыть имя ``CHECK`` нельзя: Alembic не сможет собрать
#: DDL и упадёт на этапе миграции, а не на проде.
NAMING_CONVENTION: Final[dict[str, str]] = {
    'ix': 'ix_%(table_name)s_%(column_0_N_name)s',
    'uq': 'uq_%(table_name)s_%(column_0_N_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}

#: Сколько знаков после запятой нужно самой «мелкой» валюте шлюза. Берётся из
#: ``Currency.minor_unit``, а не из ``DEFAULT_MINOR_UNIT``: у иен и вон минорной
#: единицы нет, у кувей их три, и колонка обязана вмещать любую валюту набора.
MONEY_SCALE: Final[int] = max(currency.minor_unit for currency in Currency)

#: Полная ширина денежной колонки: предел величины суммы из домена плюс дробная
#: часть. Сумма, которую домен счёл валидной, обязана помещаться без округления и
#: без ``NumericValueOutOfRange`` на ``INSERT``.
MONEY_PRECISION: Final[int] = MAX_MAGNITUDE + MONEY_SCALE

#: Длина колонки статуса платежа — самая длинная строка в ``PaymentStatus``.
PAYMENT_STATUS_LENGTH: Final[int] = max(len(status.value) for status in PaymentStatus)

#: Длина колонки статуса провайдера — самая длинная строка в ``ProviderStatus``.
PROVIDER_STATUS_LENGTH: Final[int] = max(len(status.value) for status in ProviderStatus)

#: Длина ``hexdigest`` от ``sha256``: 32 байта хеша по два символа на байт. Именно
#: эту строку сценарии кладут в ``IdempotencyRecord.request_hash``.
SHA256_HEX_LENGTH: Final[int] = 64

#: Ширина колонки идентификатора операции у провайдера. Порт
#: ``PaymentProvider`` длину не ограничивает (домен проверяет только
#: непустоту), поэтому колонка берётся с запасом от типовых шлюзов. Когда порт
#: введёт свою границу, константа переедет в него — как ``MAX_IDEMPOTENCY_KEY_LENGTH``.
PROVIDER_PAYMENT_ID_LENGTH: Final[int] = 255

#: Ширина колонки имени типа доменного события в outbox: длиннее 64 символов имя
#: класса события не бывает, а цифра взята с запасом на префиксы пространств имён.
EVENT_TYPE_LENGTH: Final[int] = 64

#: Ширина колонки имени консьюмера в таблице обработанных событий.
CONSUMER_NAME_LENGTH: Final[int] = 128

#: Ширина колонки внешнего идентификатора события. Событие приходит из брокера,
#: идентификатор в нём принадлежит внешней системе, поэтому ширина отдельная от
#: имени консьюмера: измерять чужой константой — значит урезать ключ там, где
#: этого никто не просил.
EVENT_ID_LENGTH: Final[int] = 255


#: Суффикс имени ``CHECK``, ограничивающего колонку значениями перечисления.
#: Имя собирается как ``<колонка><суффикс>``, поэтому правило «проверка называется
#: по своей колонке» живёт в одном месте и не расходится между моделями.
ENUM_CONSTRAINT_SUFFIX: Final[str] = '_enum_values'


class Base(DeclarativeBase):
    """Корень декларативной модели: общий реестр таблиц и имена ограничений."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CreatedAtMixin:
    """Колонка момента появления строки.

    ``server_default`` на стороне Postgres, а не Python-значение по умолчанию:
    строка может появиться в обход ORM (миграция, ручной ``INSERT``, скрипт
    сверки), и метка времени должна быть у неё в любом случае.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class UpdatedAtMixin(CreatedAtMixin):
    """``created_at`` плюс ``updated_at`` — для строк, которые меняют состояние.

    ``onupdate`` проставляет время сама БД, поэтому «строку обновили» всегда
    видно, даже если обновление пришло не из доменной сущности (например,
    оптимистичный ``UPDATE`` по номеру версии из T-3.6).
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def money_type() -> TypeEngine[Decimal]:
    """Тип денежной колонки: точная десятичная дробь, без ``float``.

    ``float`` в денежной колонке означал бы накопление ошибки округления, а
    ``Numeric`` без явной точности — молчаливую подгонку под ``DOUBLE PRECISION``
    при переносе схемы. Ширина :data:`MONEY_PRECISION` выведена из домена.
    """
    return Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE)


def enum_values_constraint(column_name: str, enum_class: type[StrEnum]) -> CheckConstraint:
    """Собирает ``CHECK``, ограничивающий колонку значениями перечисления.

    Допустимые значения берутся из самого ``enum_class``, а не из строкового
    списка рядом с моделью. Список значений — это и есть шкала статусов или
    валют, и копия его в схеме рано или поздно разошлась бы с доменом: в базу
    попал бы статус, которого нет в статус-машине, и отказ пришёл бы в самом
    неожиданном месте.

    Имя ограничения выводится из имени колонки, а не передаётся вызывающим.
    Правило «проверка называется по своей колонке» тогда одно на все модели: с
    ручными именами одна и та же строка в двух файлах (например, статус
    провайдера он и в платеже, и в журнале вебхуков) — это ровно тот дубль,
    который ловит архитектурный страж.

    :param column_name: имя колонки в терминах таблицы;
    :param enum_class: перечисление-владелец допустимых значений.
    """
    return CheckConstraint(
        column(column_name).in_([member.value for member in enum_class]),
        name=f'{column_name}{ENUM_CONSTRAINT_SUFFIX}',
    )
