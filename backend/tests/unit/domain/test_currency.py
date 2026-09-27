"""Тесты валюты как Value Object (T-1.1).

Валюта — закрытое множество (``StrEnum``), поэтому часть проверок
делегирована самому Enum: несуществующий код отвергается на этапе поиска
member'а, а не ручной валидацией.
"""

from decimal import Decimal

import pytest

from src.core_service.domain import Currency
from src.core_service.domain.exceptions import InvalidCurrencyError
from src.core_service.domain.value_objects.currency import normalize_currency_code

# --- Нормализация и разбор ---


@pytest.mark.parametrize(
    ('raw_code', 'expected'),
    [
        ('RUB', Currency.RUB),
        ('rub', Currency.RUB),
        ('RuB', Currency.RUB),
        ('  usd  ', Currency.USD),
    ],
)
def test_currency_normalizes_code(raw_code: str, expected: Currency) -> None:
    """Код приводится к каноническому виду: заглавные буквы без пробелов."""
    assert Currency(raw_code) is expected


def test_currency_members_are_singletons() -> None:
    """Enum-member'ы синглтоны: ``Currency('RUB') is Currency.RUB``."""
    assert Currency('rub') is Currency.RUB


def test_currencies_are_distinct_objects() -> None:
    assert Currency.RUB != Currency.USD
    assert Currency.RUB == Currency.RUB


def test_currencies_are_hashable() -> None:
    assert len({Currency.RUB, Currency('rub'), Currency.USD}) == 2


def test_currency_iteration_yields_all_members() -> None:
    """Полный набор валют — точка расширения шлюза (T-1.1)."""
    assert {member.value for member in Currency} == {'RUB', 'USD', 'EUR', 'KZT', 'JPY', 'KWD'}


# --- Отказ на некорректном коде ---


@pytest.mark.parametrize(
    'raw_code',
    ['RU', 'RUBR', '', 'RU1', 'R U', 'РУБ', 'R$', 'руб', '1'],
)
def test_currency_rejects_non_iso_codes(raw_code: str) -> None:
    """ISO 4217 — ровно три ASCII-буквы. Кириллица «РУБ» не проходит."""
    with pytest.raises(InvalidCurrencyError, match='ISO 4217'):
        Currency(raw_code)


@pytest.mark.parametrize('raw_code', [42, None, 3.5, b'RUB', ['RUB']])
def test_currency_rejects_non_string(raw_code: object) -> None:
    with pytest.raises(InvalidCurrencyError, match='должен быть строкой'):
        Currency(raw_code)  # type: ignore[arg-type]


def test_currency_reports_supported_values_for_unknown_iso_code() -> None:
    """Валидный по формату, но неподдерживаемый код: перечисляем доступные."""
    with pytest.raises(InvalidCurrencyError, match='не поддерживается шлюзом'):
        Currency('XTS')


# --- Производные свойства ---


@pytest.mark.parametrize(
    ('currency', 'expected_minor_unit'),
    [
        (Currency.RUB, 2),
        (Currency.USD, 2),
        (Currency.EUR, 2),
        (Currency.KZT, 2),
        (Currency.JPY, 0),
        (Currency.KWD, 3),
    ],
)
def test_minor_unit_per_currency(currency: Currency, expected_minor_unit: int) -> None:
    """Точность валюты разная: у иен нет копеек, у динара — три знака."""
    assert currency.minor_unit == expected_minor_unit


@pytest.mark.parametrize(
    ('currency', 'expected_quantum'),
    [
        (Currency.RUB, Decimal('0.01')),
        (Currency.JPY, Decimal('1')),
        (Currency.KWD, Decimal('0.001')),
    ],
)
def test_quantum_matches_minor_unit(currency: Currency, expected_quantum: Decimal) -> None:
    assert currency.quantum == expected_quantum


# --- Отображение в тексте ---


def test_currency_str_is_plain_code() -> None:
    """Именно StrEnum, а не обычный Enum: иначе в сообщениях было бы 'Currency.RUB'."""
    assert str(Currency.RUB) == 'RUB'
    assert f'{Currency.RUB}' == 'RUB'
    assert repr(Currency.RUB) == "<Currency.RUB: 'RUB'>"


def test_currency_behaves_as_str_on_the_boundary() -> None:
    """Строка на входе транспорта (JSON/protobuf) не требует ручного разбора."""
    assert Currency.RUB == 'RUB'


# --- Нормализация как отдельная функция ---


def test_normalize_currency_code_returns_canonical_form() -> None:
    assert normalize_currency_code(' rub ') == 'RUB'


@pytest.mark.parametrize('raw_code', ['РУБ', 'RU', 42])
def test_normalize_currency_code_rejects_invalid(raw_code: object) -> None:
    """Функцией пользуются адаптеры перед записью в БД и перед логированием."""
    with pytest.raises(InvalidCurrencyError):
        normalize_currency_code(raw_code)
