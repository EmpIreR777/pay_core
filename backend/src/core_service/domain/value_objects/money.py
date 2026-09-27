"""Денежная сумма как Value Object (T-1.1).

Ключевые инварианты:

* ``amount`` — только :class:`~decimal.Decimal`, никогда ``float`` (float
  в деньгах недопустим: ``0.1 + 0.2 != 0.3``);
* сумма неотрицательна — знак операции задаётся доменным методом, а не знаком
  внутри денег;
* сумма всегда приведена к точности валюты (``quantum``);
* операции между разными валютами запрещены: неявной конвертации нет.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from src.core_service.domain.exceptions import (
    CurrencyMismatchError,
    InvalidAmountError,
    NegativeAmountError,
)
from src.core_service.domain.value_objects.currency import Currency

#: Правило округления по умолчанию. ``ROUND_HALF_UP`` — «денежное» правило
#: (половина всегда уходит вверх), в отличие от банковского ROUND_HALF_EVEN.
DEFAULT_ROUNDING: Final = ROUND_HALF_UP

#: Порог величины суммы (число разрядов целой части). Защищает и от
#: «несанкционированно длинной» суммы, и от ошибок округления Decimal.
MAX_MAGNITUDE: Final = 24


def _to_decimal(value: Decimal | int | str) -> Decimal:
    """Приводит допустимое значение к :class:`Decimal` без потери точности.

    ``float`` отвергается намеренно: он уже потерял точность к моменту
    попадания в систему, и никакое округление её не вернёт.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise InvalidAmountError('Сумма не может быть bool')
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError as exc:
            raise InvalidAmountError(f'Не удалось разобрать сумму из строки: {value!r}') from exc
    raise InvalidAmountError(
        f'Сумма должна быть Decimal, int или str, получено {type(value).__name__}. '
        'Использование float запрещено: он теряет точность',
    )


@dataclass(frozen=True, slots=True, repr=False)
class Money:
    """Сумма в конкретной валюте. Иммутабельна: любая операция возвращает новый объект."""

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.currency, Currency):
            raise InvalidAmountError(
                f'Валюта должна быть экземпляром Currency, получено {type(self.currency).__name__}',
            )
        amount = _to_decimal(self.amount)
        if not amount.is_finite():
            raise InvalidAmountError(f'Сумма должна быть конечным числом, получено {amount}')
        if amount.adjusted() >= MAX_MAGNITUDE:
            raise InvalidAmountError(f'Сумма {amount} слишком велика: допустимо не более {MAX_MAGNITUDE} разрядов')
        if amount < 0:
            raise NegativeAmountError(f'Сумма не может быть отрицательной: {amount} {self.currency}')
        # frozen dataclass: нормализуем через object.__setattr__, иначе
        # количество знаков после запятой «плыло» бы от выражения к выражению.
        object.__setattr__(self, 'amount', self._quantize(amount))

    def _quantize(self, amount: Decimal) -> Decimal:
        return amount.quantize(self.currency.quantum, rounding=DEFAULT_ROUNDING)

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        """Нулевая сумма в заданной валюте."""
        return cls(amount=Decimal(0), currency=currency)

    @classmethod
    def from_number(cls, value: Decimal | int | str, currency: Currency) -> Money:
        """Явный фабричный метод: читается как «сумма из числа и валюты»."""
        return cls(amount=_to_decimal(value), currency=currency)

    # --- Арифметика ---

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f'Нельзя оперировать разными валютами: {self.currency} и {other.currency}',
            )

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        """Умножение на коэффициент (например, ``amount * Decimal('1.2')``).

        Отрицательный коэффициент запрещён: он сделал бы сумму отрицательной,
        а значит нарушил бы инвариант ``Money``.
        """
        multiplier = _to_decimal(factor)
        if multiplier < 0:
            raise NegativeAmountError(f'Коэффициент умножения не может быть отрицательным: {multiplier}')
        return Money(amount=self.amount * multiplier, currency=self.currency)

    def __rmul__(self, factor: Decimal | int) -> Money:
        return self.__mul__(factor)

    # --- Сравнения ---
    # Равенство (``__eq__``) генерирует dataclass: суммы в разных валютах
    # просто не равны. Порядковые же сравнения бросают CurrencyMismatchError.

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount >= other.amount

    # --- Прочее ---

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        return f'{self.amount} {self.currency}'

    def __repr__(self) -> str:
        return f'{type(self).__name__}(amount={self.amount!r}, currency={self.currency!r})'
