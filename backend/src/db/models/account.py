"""Таблица платёжных счетов (T-3.1).

Одна строка — один ``Account``. Ключевые решения:

* **Идентификатор — доменный UUID без обёртки.** ``AccountId`` живёт в домене, а
  в базе лежит ``UUID``: обёртка нужна, чтобы не спутать ``AccountId`` с
  ``PaymentId`` в коде ядра, и в колонке она только мешала бы. Обратное
  преобразование (и обратно) — дело репозитория (T-3.3), не схемы.
* **Валюта — ``CHECK`` по ``Currency``, а не PostgreSQL ``ENUM``-тип.** Тип
  ``ENUM`` в Postgres меняется только через ``ALTER TYPE``, и добавление
  валюты в шлюзе стало бы миграцией с блокировкой таблицы. Столбец ``VARCHAR``
  с проверкой даёт то же самое ограничение данных, но меняется обычным
  ``ALTER TABLE``.
* **Версия — целое с ``CHECK`` от ``MIN_VERSION``.** Число одно (домен владеет
  им через ``versioning.py``), поэтому и «по умолчанию», и «не меньше» взяты
  из одной константы, а не записаны числами. Оптимистичная блокировка по этой
  колонке выполняется в репозитории (T-3.6): ``UPDATE`` сверяет её с прочитанным
  значением, поэтому колонке нужен не только ``CHECK``, но и точный смысл.
* **Индексов на ``currency``/``is_blocked`` сознательно нет.** Выборки идут по
  ``id``; индекс «на всякий случай» платит записью при каждом обновлении
  баланса и ничего не ускоряет.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Integer,
    String,
    Uuid,
    column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core_service.domain.value_objects.currency import ISO_4217_CODE_LENGTH, Currency
from src.core_service.domain.versioning import INITIAL_VERSION, MIN_VERSION
from src.db.models.base import (
    Base,
    UpdatedAtMixin,
    enum_values_constraint,
    money_type,
)


class AccountModel(Base, UpdatedAtMixin):
    """Платёжный счёт: баланс, валюта, признак блокировки и версия."""

    __tablename__ = 'accounts'

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    currency: Mapped[str] = mapped_column(String(ISO_4217_CODE_LENGTH), nullable=False)
    balance: Mapped[Decimal] = mapped_column(money_type(), nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text(str(INITIAL_VERSION)))

    __table_args__ = (
        enum_values_constraint('currency', Currency),
        # Баланс отрицательным быть не может: Money этого не допускает, а
        # «минус на счёте» в базе — это уже испорченные деньги, а не долг.
        CheckConstraint(column('balance') >= 0, name='balance_non_negative'),
        CheckConstraint(column('version') >= MIN_VERSION, name='version_positive'),
    )
