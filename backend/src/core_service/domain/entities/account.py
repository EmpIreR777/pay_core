"""Платёжный счёт как доменная сущность (T-1.2).

Ключевые инварианты:

* **Тождество, а не значение.** Два счёта с одинаковым ``id`` — это один и тот
  же счёт, даже если балансы различаются. Поэтому ``__eq__``/``__hash__``
  построены на идентификаторе, а не на полях, а ``__slots__`` запрещает
  «случайные» атрибуты.
* **Валюта счёта неизменна.** Задаётся балансом при создании и дальше только
  проверяется: пополнение в чужой валюте — отказ, а не конвертация.
* **Баланс неотрицателен** (следствие инварианта ``Money``).
* **Заблокированный счёт не двигает деньги.** ``deposit``/``withdraw`` отказаны,
  ``block``/``unblock`` — можно.
* **Версия меняется только при реальном изменении состояния.** Неудачная
  операция и повторная блокировка версию не трогают: иначе в поток
  оптимистичных блокировок попадали бы записи об изменениях, которых не было.

Состояние спрятано в приватных полях и отдаётся через read-only property:
единственный способ изменить счёт — вызвать доменный метод, который и
проверяет инварианты.
"""

from typing import Self

from src.core_service.domain.exceptions import (
    AccountBlocked,
    CurrencyMismatchError,
    InsufficientFunds,
    InvalidIdentifierError,
)
from src.core_service.domain.validation import (
    require_min_int,
    require_money,
    require_non_zero_money,
    require_type,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.versioning import INITIAL_VERSION, MIN_VERSION

__all__ = ('INITIAL_VERSION', 'MIN_VERSION', 'Account')


class Account:
    """Платёжный счёт в одной валюте с балансом, блокировкой и версией."""

    __slots__ = ('_balance', '_id', '_is_blocked', '_version')

    def __init__(
        self,
        account_id: AccountId,
        balance: Money,
        *,
        is_blocked: bool = False,
        version: int = INITIAL_VERSION,
    ) -> None:
        """
        :param account_id: идентификатор счёта; строка с границы слоя не
            принимается — «не-ACCOUNT_ID» это ошибка вызова, а не счёта;
        :param balance: баланс, он же задаёт валюту счёта;
        :param is_blocked: заблокирован ли счёт;
        :param version: версия для оптимистичной блокировки.
        """
        require_type(account_id, AccountId, 'Идентификатор счёта', error_type=InvalidIdentifierError)
        require_type(balance, Money, 'Баланс')
        # bool — подкласс int, поэтому 0/1 «прошли» бы как False/True
        # и тихо превратили бы 1 в заблокированный счёт.
        require_type(is_blocked, bool, 'Признак блокировки')
        checked_version = require_min_int(version, 'Версия', minimum=MIN_VERSION)

        self._id = account_id
        self._balance = balance
        self._is_blocked = is_blocked
        self._version = checked_version

    # --- Создание ---

    @classmethod
    def create(cls, currency: Currency, account_id: AccountId | None = None) -> Self:
        """Новый пустой счёт в заданной валюте.

        Идентификатор по умолчанию генерируется здесь: вызывающий всё равно не
        может выбрать осмысленный ``id`` заранее, а на границе (импорт данных)
        готовый идентификатор передаётся явно.
        """
        require_type(currency, Currency, 'Валюта счёта')
        return cls(account_id=account_id or AccountId.new(), balance=Money.zero(currency))

    # --- Read-only доступ к состоянию ---

    @property
    def id(self) -> AccountId:
        return self._id

    @property
    def balance(self) -> Money:
        return self._balance

    @property
    def currency(self) -> Currency:
        """Валюта счёта — производное от баланса, отдельного поля не имеет."""
        return self._balance.currency

    @property
    def version(self) -> int:
        """Версия для оптимистичной блокировки; растёт при каждом изменении."""
        return self._version

    @property
    def is_blocked(self) -> bool:
        return self._is_blocked

    @property
    def is_active(self) -> bool:
        """Готов ли счёт принимать операции (то есть не заблокирован)."""
        return not self._is_blocked

    # --- Операции с балансом ---

    def deposit(self, amount: Money) -> None:
        """Пополняет счёт. Знак суммы задаёт метод, а не значение."""
        operation = 'Пополнение счёта'
        self._require_operable(operation)
        checked = require_money(amount, operation)
        self._require_same_currency(checked, operation)
        require_non_zero_money(checked, operation)

        self._balance = self._balance + checked
        self._touch()

    def withdraw(self, amount: Money) -> None:
        """Списывает средства. Уход баланса в минус — не «овердрафт», а отказ."""
        operation = 'Списание со счёта'
        self._require_operable(operation)
        checked = require_money(amount, operation)
        self._require_same_currency(checked, operation)
        require_non_zero_money(checked, operation)

        if checked > self._balance:
            raise InsufficientFunds(
                f'Недостаточно средств на счёте {self._id}: доступно {self._balance}, '
                f'запрошено {checked} (не хватает {checked - self._balance})',
            )

        self._balance = self._balance - checked
        self._touch()

    def has_sufficient_funds(self, amount: Money) -> bool:
        """Хватит ли средств. Чистая проверка: состояние не меняется.

        Нужна вызывающему коду, чтобы отклонить операцию до похода в базу;
        сама по себе она не заменяет проверку в ``withdraw`` — между вопросом и
        списанием баланс мог измениться.
        """
        if not isinstance(amount, Money) or amount.currency != self._balance.currency:
            return False
        return amount <= self._balance

    # --- Блокировка ---

    def block(self) -> None:
        """Замораживает счёт: операции с балансом становятся недоступны.

        Идемпотентна — повторная блокировка не ошибка, а «уже так». Платежи
        приходят повторно (ретраи, вебхуки), и требовать от вызывающего знать
        текущее состояние значило бы возложить на него проверку инварианта.
        """
        if self._is_blocked:
            return
        self._is_blocked = True
        self._touch()

    def unblock(self) -> None:
        """Размораживает счёт. Идемпотентна, как и ``block``."""
        if not self._is_blocked:
            return
        self._is_blocked = False
        self._touch()

    # --- Внутренние проверки инвариантов ---

    def _require_operable(self, operation: str) -> None:
        if self._is_blocked:
            raise AccountBlocked(f'Счёт {self._id} заблокирован: {operation.lower()} невозможна')

    def _require_same_currency(self, amount: Money, operation: str) -> None:
        if amount.currency != self._balance.currency:
            raise CurrencyMismatchError(
                f'{operation}: счёт {self._id} открыт в валюте {self._balance.currency}, '
                f'а сумма задана в {amount.currency}',
            )

    def _touch(self) -> None:
        """Отмечает факт изменения состояния для оптимистичной блокировки."""
        self._version += 1

    # --- Тождество ---

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Account):
            return NotImplemented
        return self._id == other._id

    def __hash__(self) -> int:
        # Хеш по неизменяемому идентификатору: счёт мутируем, но его «разместить»
        # в set/dict можно, и переезд по хешу при списании не случится.
        return hash(self._id)

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}(id={self._id!r}, balance={self._balance!r}, '
            f'version={self._version}, is_blocked={self._is_blocked})'
        )
