"""Тесты на доменные исключения (T-1.5).

Проверяется иерархия наследования от базового DomainError, специфика
Value Object ошибок (наследование от ValueError), бизнес-исключений,
поведение при выбросе и перехвате через базовые классы и строковое
представление сообщений об ошибках.
"""

import pytest

from src.core_service.domain.exceptions import (
    AccountBlocked,
    CurrencyMismatchError,
    DomainError,
    DuplicateOperation,
    InsufficientFunds,
    InvalidAmountError,
    InvalidCurrencyError,
    InvalidIdentifierError,
    InvalidTransition,
    InvalidValueError,
    NegativeAmountError,
    PaymentProviderError,
)

ALL_DOMAIN_EXCEPTIONS = (
    DomainError,
    InvalidValueError,
    InvalidCurrencyError,
    CurrencyMismatchError,
    InvalidAmountError,
    NegativeAmountError,
    InvalidIdentifierError,
    InsufficientFunds,
    AccountBlocked,
    InvalidTransition,
    DuplicateOperation,
    PaymentProviderError,
)

BUSINESS_EXCEPTIONS = (
    InsufficientFunds,
    AccountBlocked,
    InvalidTransition,
    DuplicateOperation,
    PaymentProviderError,
)

VALUE_OBJECT_EXCEPTIONS = (
    InvalidValueError,
    InvalidCurrencyError,
    CurrencyMismatchError,
    InvalidAmountError,
    NegativeAmountError,
    InvalidIdentifierError,
)


@pytest.mark.parametrize('exc_cls', ALL_DOMAIN_EXCEPTIONS)
def test_all_exceptions_inherit_from_domain_error(exc_cls: type[Exception]) -> None:
    """Любое доменное исключение обязано наследоваться от базового DomainError."""
    assert issubclass(exc_cls, DomainError)
    assert issubclass(exc_cls, Exception)


@pytest.mark.parametrize('exc_cls', VALUE_OBJECT_EXCEPTIONS)
def test_value_object_exceptions_inherit_from_value_error(exc_cls: type[Exception]) -> None:
    """Ошибки разбора и валидации Value Object наследуются от ValueError."""
    assert issubclass(exc_cls, ValueError)


@pytest.mark.parametrize('exc_cls', BUSINESS_EXCEPTIONS)
def test_business_exceptions_do_not_inherit_from_value_error(exc_cls: type[Exception]) -> None:
    """Бизнес-отказы не являются ошибками формата значений (ValueError)."""
    assert not issubclass(exc_cls, ValueError)


def test_specific_inheritance_chains() -> None:
    """Проверка детальных цепочек наследования специализированных исключений."""
    assert issubclass(InvalidCurrencyError, InvalidValueError)
    assert issubclass(InvalidAmountError, InvalidValueError)
    assert issubclass(NegativeAmountError, InvalidAmountError)
    assert issubclass(InvalidIdentifierError, InvalidValueError)
    assert issubclass(CurrencyMismatchError, DomainError)
    assert issubclass(CurrencyMismatchError, ValueError)


@pytest.mark.parametrize(
    ('exc_cls', 'message'),
    [
        (DomainError, 'Базовая доменная ошибка'),
        (InvalidValueError, 'Некорректное значение'),
        (InvalidCurrencyError, 'Неверный код валюты XYZ'),
        (CurrencyMismatchError, 'Несовпадение валют RUB и USD'),
        (InvalidAmountError, 'Некорректная сумма'),
        (NegativeAmountError, 'Отрицательная сумма'),
        (InvalidIdentifierError, 'Невалидный UUID'),
        (InsufficientFunds, 'Недостаточно средств на счёте'),
        (AccountBlocked, 'Счёт заблокирован'),
        (InvalidTransition, 'Переход из SETTLED в PROCESSING запрещён'),
        (DuplicateOperation, 'Операция с ключом idempotency-123 уже выполняется'),
        (PaymentProviderError, 'Провайдер эквайринга вернул 502 Bad Gateway'),
    ],
)
def test_exception_instantiation_and_message(exc_cls: type[DomainError], message: str) -> None:
    """Исключение корректно инициализируется с переданным сообщением."""
    exc = exc_cls(message)
    assert str(exc) == message
    assert exc.args == (message,)


@pytest.mark.parametrize(
    'exc',
    [
        DomainError('generic'),
        InvalidValueError('invalid val'),
        InvalidCurrencyError('invalid curr'),
        CurrencyMismatchError('mismatch'),
        InvalidAmountError('invalid amount'),
        NegativeAmountError('negative amount'),
        InvalidIdentifierError('invalid id'),
        InsufficientFunds('no money'),
        AccountBlocked('blocked'),
        InvalidTransition('bad transition'),
        DuplicateOperation('duplicate operation'),
        PaymentProviderError('provider timeout'),
    ],
)
def test_catching_via_domain_error(exc: DomainError) -> None:
    """Любое доменное исключение может быть поймано общим блоком except DomainError."""
    with pytest.raises(DomainError) as exc_info:
        raise exc
    assert exc_info.value is exc
    assert str(exc_info.value) == exc.args[0]


def test_catching_value_errors_separately_from_business_errors() -> None:
    """Проверка фильтрации: ValueError перехватывает VO-ошибки, но пропускает бизнес-отказы."""
    vo_error = InvalidCurrencyError('bad currency')
    with pytest.raises(ValueError, match='bad currency'):
        raise vo_error

    business_error = InsufficientFunds('insufficient funds')
    assert not isinstance(business_error, ValueError)
    with pytest.raises(DomainError, match='insufficient funds'):
        raise business_error


def test_catching_duplicate_operation() -> None:
    """DuplicateOperation корректно выбрасывается и перехватывается."""
    with pytest.raises(DuplicateOperation, match='Duplicate request: op_123'):
        raise DuplicateOperation('Duplicate request: op_123')


def test_catching_payment_provider_error() -> None:
    """PaymentProviderError корректно выбрасывается и перехватывается."""
    with pytest.raises(PaymentProviderError, match='Provider connection timeout'):
        raise PaymentProviderError('Provider connection timeout')
