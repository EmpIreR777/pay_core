"""Маппинг доменного счёта в строку ``accounts`` и обратно (T-3.3).

Без БД: интеграционные тесты доказывают поведение репозитория на живом Postgres,
здесь — только преобразование. Разделение существенно, потому что у маппера есть
ошибка, которую ни один интеграционный тест не поймает: **потеря состояния при
round-trip**. Если ``to_domain_account`` забудет ``is_blocked``, интеграционный
тест «прочитал — изменил — сохранил» пройдёт (он меняет баланс, а не флаг) и
пропустит счёт, который после записи оказался разблокированным. Round-trip
проверяет все поля разом и на чистых данных, без стенда.
"""

from decimal import Decimal

import pytest

from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import InvalidCurrencyError, InvalidIdentifierError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.versioning import INITIAL_VERSION
from src.db.models.account import AccountModel
from src.db.repositories import new_account_model, to_domain_account


def _account(currency: Currency = Currency.RUB, balance: str = '1000.00') -> Account:
    return Account(account_id=AccountId.new(), balance=Money.from_number(Decimal(balance), currency))


def _model(currency: str = 'RUB', balance: str = '1000.000', **overrides: object) -> AccountModel:
    """Строка ``accounts`` как её вернул бы Postgres.

    ``balance`` приходит строкой с точностью колонки: ``NUMERIC(27,3)`` отдаёт
    ``Decimal``, и именно эту форму маппер обязан понимать.
    """
    defaults: dict[str, object] = {
        'id': AccountId.new().value,
        'currency': currency,
        'balance': Decimal(balance),
        'is_blocked': False,
        'version': INITIAL_VERSION,
    }
    return AccountModel(**{**defaults, **overrides})


# --- Строка -> домен ----------------------------------------------------------


def test_to_domain_account_keeps_every_field() -> None:
    """Round-trip состояния: идентификатор, баланс, валюта, блокировка, версия.

    Проверяются все поля сразу и по одному объекту: потеря любого из них
    выглядит снаружи как «операция прошла», а на деле оставляет счёт в другом
    состоянии — и заметить это можно только здесь.
    """
    model = _model(is_blocked=True, version=7)

    account = to_domain_account(model)

    assert account.id.value == model.id
    assert account.balance == Money.from_number(Decimal('1000.00'), Currency.RUB)
    assert account.currency is Currency.RUB
    assert account.is_blocked is True
    assert account.version == 7


@pytest.mark.parametrize(
    ('currency', 'stored', 'expected'),
    [
        (Currency.RUB, '1000.000', '1000.00'),
        (Currency.JPY, '100.000', '100'),
        (Currency.KWD, '1.234', '1.234'),
    ],
)
def test_to_domain_account_applies_currency_quantum(currency: Currency, stored: str, expected: str) -> None:
    """Колонка одна на все валюты, точность задаёт валюта.

    Иена в ``NUMERIC(27,3)`` лежит как ``100.000``, и читаться она обязана как
    ``100``: иначе сумма в иенах была бы на три знака длиннее своей валюты, и
    любое сравнение с суммой, посчитанной доменом, расходилось бы.
    """
    account = to_domain_account(_model(currency=currency.value, balance=stored))

    assert account.balance == Money.from_number(Decimal(expected), currency)
    assert account.currency is currency


def test_to_domain_account_rejects_currency_outside_the_scale() -> None:
    """Мусор в колонке валюты не превращается в тихое значение по умолчанию.

    ``Currency('XXX')`` бросает доменную ошибку, а не подставляет первую попавшуюся
    валюту: в базу не может попасть код, которого нет в шкале шлюза, иначе деньги
    оказались бы посчитаны в чужой валюте.
    """
    with pytest.raises(InvalidCurrencyError):
        to_domain_account(_model(currency='XXX'))


def test_to_domain_account_rejects_nil_uuid() -> None:
    """Нулевой UUID не превращается в валидный ``AccountId``.

    Идентификатор, которого не выдавали ни одной сущности, означает испорченные
    данные. Молча принять его значило бы выдать счёт с несуществующим id.
    """
    from uuid import UUID

    with pytest.raises(InvalidIdentifierError):
        to_domain_account(_model(id=UUID(int=0)))


# --- Домен -> строка ----------------------------------------------------------


def test_new_account_model_carries_domain_state() -> None:
    """Все поля домена попадают в колонки без потерь и переименований."""
    account = _account(Currency.KWD, '1.234')
    account.block()

    model = new_account_model(account)

    assert model.id == account.id.value
    assert model.currency == 'KWD'
    assert model.balance == Decimal('1.234')
    assert model.is_blocked is True
    assert model.version == account.version


def test_new_account_model_leaves_timestamps_to_the_database() -> None:
    """Время проставляет база, а не репозиторий.

    ``server_default`` нужен ради строк, записанных мимо репозитория (импорт,
    ручной ``INSERT``, скрипт сверки), — и он же требует, чтобы Python не подставлял
    своё время молча.
    """
    model = new_account_model(_account())

    assert model.created_at is None
    assert model.updated_at is None


def test_model_round_trip_preserves_account_state() -> None:
    """``домен -> строка -> домен`` не меняет ни одного наблюдаемого поля.

    Сквозная гарантия маппера: если в одну из сторон потеряется поле, состояние
    «прочитали — изменили — сохранили» начнёт незаметно его терять, и счёт,
    например, останется разблокированным после его блокировки.
    """
    account = _account(Currency.JPY, '100')
    account.deposit(Money.from_number(Decimal(50), Currency.JPY))
    account.block()

    restored = to_domain_account(new_account_model(account))

    assert restored == account
    assert restored.balance == account.balance
    assert restored.currency is account.currency
    assert restored.is_blocked is True
    assert restored.version == account.version
