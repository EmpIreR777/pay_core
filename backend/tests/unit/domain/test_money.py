"""Тесты денежной суммы как Value Object (T-1.1).

DoD задачи: unit-тесты на арифметику, равенство и ошибки.
"""

from dataclasses import FrozenInstanceError
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest

from src.core_service.domain import Currency, Money
from src.core_service.domain.exceptions import (
    CurrencyMismatchError,
    InvalidAmountError,
    NegativeAmountError,
)

# --- Создание и нормализация ---


def test_money_from_decimal() -> None:
    assert Money(amount=Decimal('10.50'), currency=Currency.RUB).amount == Decimal('10.50')


def test_money_from_int_and_str() -> None:
    """int и str допустимы на границе слоя — точной потери они не дают."""
    assert Money.from_number(10, Currency.RUB).amount == Decimal('10.00')
    assert Money.from_number('10.5', Currency.RUB).amount == Decimal('10.50')


@pytest.mark.parametrize('raw_amount', [0.1, 10.5, float('1.0')])
def test_money_rejects_float(raw_amount: float) -> None:
    """float в деньгах недопустим: точность потеряна ещё до вызова конструктора."""
    with pytest.raises(InvalidAmountError, match='float запрещен'):
        Money(amount=raw_amount, currency=Currency.RUB)  # type: ignore[arg-type]


def test_money_rejects_bool() -> None:
    """bool — подкласс int, но «сумма True» — очевидная ошибка вызывающего."""
    with pytest.raises(InvalidAmountError, match='bool'):
        Money(amount=True, currency=Currency.RUB)  # type: ignore[arg-type]


def test_money_rejects_unparsable_string() -> None:
    with pytest.raises(InvalidAmountError, match='Не удалось разобрать'):
        Money.from_number('сто рублей', Currency.RUB)


@pytest.mark.parametrize('raw_amount', [object(), None, ['10']])
def test_money_rejects_unsupported_types(raw_amount: object) -> None:
    with pytest.raises(InvalidAmountError, match='должна быть Decimal'):
        Money.from_number(raw_amount, Currency.RUB)  # type: ignore[arg-type]


def test_money_rejects_non_finite_amounts() -> None:
    """NaN и Infinity — не деньги; арифметика с ними ломает инварианты."""
    for raw_amount in (Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')):
        with pytest.raises(InvalidAmountError, match='конечным числом'):
            Money(amount=raw_amount, currency=Currency.RUB)


def test_money_rejects_absurdly_large_amount() -> None:
    with pytest.raises(InvalidAmountError, match='слишком велика'):
        Money(amount=Decimal('1E+30'), currency=Currency.RUB)


def test_money_rejects_currency_given_as_string() -> None:
    """Строка вместо Currency — ошибка даже при внешней «похожести» на StrEnum."""
    with pytest.raises(InvalidAmountError, match='Валюта должна быть экземпляром Currency'):
        Money(amount=Decimal('1'), currency='RUB')  # type: ignore[arg-type]


def test_money_rejects_negative_amount() -> None:
    with pytest.raises(NegativeAmountError, match='не может быть отрицательной'):
        Money.from_number('-0.01', Currency.RUB)


def test_money_normalizes_scale_to_currency_minor_unit() -> None:
    """Точность всегда приведена к валюте: иначе суммы визуально «разъезжаются»."""
    assert Money.from_number('10', Currency.RUB).amount == Decimal('10.00')
    assert Money.from_number('10.005', Currency.RUB).amount == Decimal('10.01')  # ROUND_HALF_UP
    assert Money.from_number('10.004', Currency.RUB).amount == Decimal('10.00')
    assert Money.from_number('1234.56', Currency.JPY).amount == Decimal('1235')  # иены: целые
    assert Money.from_number('1.2345', Currency.KWD).amount == Decimal('1.235')  # динар: 3 знака


def test_money_rounding_mode_is_half_up() -> None:
    """Явно фиксируем правило: половина копейки уходит вверх, а не к чётной цифре.

    ``0.005`` — различающий случай: ROUND_HALF_UP даёт ``0.01``,
    банковский ROUND_HALF_EVEN — ``0.00``. Если правило сменится, тест упадёт.
    """
    assert Money.from_number('0.005', Currency.RUB).amount == Decimal('0.01')
    assert Decimal('0.005').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) == Decimal('0.01')
    assert Decimal('0.005').quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN) == Decimal('0.00')


# --- Равенство и хеширование ---


def test_money_equality() -> None:
    assert Money.from_number(10, Currency.RUB) == Money.from_number('10.00', Currency.RUB)
    assert Money.from_number(10, Currency.RUB) != Money.from_number('10.01', Currency.RUB)


def test_money_equality_is_false_for_different_currencies() -> None:
    """Разные валюты просто не равны (без исключения) — как 1 != '1'."""
    assert Money.from_number(10, Currency.RUB) != Money.from_number(10, Currency.USD)


def test_money_is_hashable() -> None:
    """Неизменяемость => безопасное хеширование (Money может быть ключом dict)."""
    amounts = {
        Money.from_number(10, Currency.RUB),
        Money.from_number('10.00', Currency.RUB),
        Money.from_number(10, Currency.USD),
    }
    assert len(amounts) == 2


def test_money_is_immutable() -> None:
    amount = Money.from_number(10, Currency.RUB)
    with pytest.raises(FrozenInstanceError):
        amount.amount = Decimal('999')  # type: ignore[misc]


# --- Арифметика ---


def test_money_addition() -> None:
    assert Money.from_number('10.10', Currency.RUB) + Money.from_number('0.20', Currency.RUB) == Money.from_number(
        '10.30', Currency.RUB
    )


def test_money_subtraction() -> None:
    assert Money.from_number('10.30', Currency.RUB) - Money.from_number('0.30', Currency.RUB) == Money.from_number(
        '10.00', Currency.RUB
    )


def test_money_subtraction_to_zero() -> None:
    assert Money.from_number(10, Currency.RUB) - Money.from_number(10, Currency.RUB) == Money.zero(Currency.RUB)


def test_money_subtraction_below_zero_rejected() -> None:
    """Отрицательный остаток — доменное нарушение, а не «минус на балансе»."""
    with pytest.raises(NegativeAmountError, match='не может быть отрицательной'):
        Money.from_number(1, Currency.RUB) - Money.from_number(2, Currency.RUB)


def test_money_arithmetic_returns_new_object() -> None:
    original = Money.from_number(10, Currency.RUB)
    result = original + Money.from_number(5, Currency.RUB)

    assert original == Money.from_number(10, Currency.RUB)
    assert result == Money.from_number(15, Currency.RUB)


def test_money_multiplication_by_number() -> None:
    assert Money.from_number('10.00', Currency.RUB) * 3 == Money.from_number('30.00', Currency.RUB)
    assert Money.from_number('10.00', Currency.RUB) * Decimal('1.1') == Money.from_number('11.00', Currency.RUB)
    assert Money.from_number('10.00', Currency.RUB) * Decimal('0') == Money.zero(Currency.RUB)


def test_money_multiplication_rounds_result() -> None:
    assert Money.from_number('10.00', Currency.RUB) * Decimal('1.005') == Money.from_number('10.05', Currency.RUB)


def test_money_reflected_multiplication() -> None:
    """``Decimal('2') * amount`` должно работать так же, как ``amount * 2``."""
    assert Decimal('2') * Money.from_number('10.00', Currency.RUB) == Money.from_number('20.00', Currency.RUB)


def test_money_preserves_currency_on_operations() -> None:
    """Валюта результата операции не должна молча смениться на дефолтную."""
    result = Money.from_number(10, Currency.KWD) + Money.from_number(5, Currency.KWD)

    assert result.currency == Currency.KWD
    assert result == Money.from_number('15.000', Currency.KWD)


def test_money_rejects_negative_multiplier() -> None:
    with pytest.raises(NegativeAmountError, match='Коэффициент умножения'):
        Money.from_number(10, Currency.RUB) * -1


def test_money_rejects_float_multiplier() -> None:
    with pytest.raises(InvalidAmountError, match='float запрещен'):
        Money.from_number(10, Currency.RUB) * 1.5  # type: ignore[operator]


@pytest.mark.parametrize(
    'operate',
    [
        lambda amount: amount + 5,  # type: ignore[operator]
        lambda amount: amount - 5,  # type: ignore[operator]
    ],
    ids=['add', 'sub'],
)
def test_money_arithmetic_with_non_money_is_type_error(operate: object) -> None:
    """Вместо «lenient»-поведения Python выдаёт TypeError — так заметнее в коде."""
    with pytest.raises(TypeError):
        operate(Money.from_number(10, Currency.RUB))  # type: ignore[operator]


# --- Смешение валют запрещено ---


@pytest.mark.parametrize(
    'operation',
    [
        lambda left, right: left + right,
        lambda left, right: left - right,
        lambda left, right: left < right,
        lambda left, right: left <= right,
        lambda left, right: left > right,
        lambda left, right: left >= right,
    ],
    ids=['add', 'sub', 'lt', 'le', 'gt', 'ge'],
)
def test_money_forbids_mixing_currencies(operation: object) -> None:
    """Неявной конвертации нет: курс — внешняя политика, а не свойство денег."""
    with pytest.raises(CurrencyMismatchError, match='разными валютами'):
        operation(Money.from_number(10, Currency.RUB), Money.from_number(10, Currency.USD))  # type: ignore[operator]


# --- Сравнения ---


def test_money_ordering() -> None:
    small = Money.from_number('10.00', Currency.RUB)
    large = Money.from_number('20.00', Currency.RUB)

    assert small < large
    assert small <= large
    assert large > small
    assert large >= small
    assert small <= Money.from_number(10, Currency.RUB)
    assert small >= Money.from_number(10, Currency.RUB)


@pytest.mark.parametrize(
    'compare',
    [
        lambda amount: amount < 5,  # type: ignore[operator]
        lambda amount: amount <= 5,  # type: ignore[operator]
        lambda amount: amount > 5,  # type: ignore[operator]
        lambda amount: amount >= 5,  # type: ignore[operator]
    ],
    ids=['lt', 'le', 'gt', 'ge'],
)
def test_money_comparison_with_non_money_is_type_error(compare: object) -> None:
    """Не-Money возвращает NotImplemented, и Python честно сообщает TypeError."""
    with pytest.raises(TypeError):
        compare(Money.from_number(10, Currency.RUB))  # type: ignore[operator]


# --- Прочее ---


def test_money_zero() -> None:
    zero = Money.zero(Currency.RUB)

    assert zero.amount == Decimal('0.00')
    assert zero.currency == Currency.RUB
    assert zero.is_zero


def test_money_is_zero_is_false_for_non_zero() -> None:
    assert not Money.from_number('0.01', Currency.RUB).is_zero


def test_money_str_and_repr() -> None:
    amount = Money.from_number('10.50', Currency.RUB)

    assert str(amount) == '10.50 RUB'
    assert repr(amount) == "Money(amount=Decimal('10.50'), currency=<Currency.RUB: 'RUB'>)"
