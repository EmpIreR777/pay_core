"""Тесты платёжного счёта как доменной сущности (T-1.2).

DoD задачи: тесты на все методы и ошибочные сценарии (блокировка, нехватка
средств).
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from src.core_service.domain import (
    Account,
    AccountBlocked,
    AccountId,
    Currency,
    DomainError,
    InsufficientFunds,
    Money,
)
from src.core_service.domain.entities.account import INITIAL_VERSION, MIN_VERSION
from src.core_service.domain.exceptions import (
    CurrencyMismatchError,
    InvalidAmountError,
    InvalidIdentifierError,
    InvalidValueError,
)


def make_account(
    balance: str = '100.00',
    currency: Currency = Currency.RUB,
    *,
    is_blocked: bool = False,
    version: int = INITIAL_VERSION,
) -> Account:
    return Account(
        account_id=AccountId.new(),
        balance=Money.from_number(balance, currency),
        is_blocked=is_blocked,
        version=version,
    )


def rub(value: str) -> Money:
    return Money.from_number(value, Currency.RUB)


# --- Создание и инварианты состояния ---


def test_create_makes_empty_active_account_with_initial_version() -> None:
    account = Account.create(Currency.RUB)

    assert account.balance == Money.zero(Currency.RUB)
    assert account.currency is Currency.RUB
    assert account.is_blocked is False
    assert account.is_active is True
    assert account.version == INITIAL_VERSION


def test_create_generates_unique_ids() -> None:
    assert Account.create(Currency.RUB).id != Account.create(Currency.RUB).id


def test_create_accepts_preset_id() -> None:
    """Импорт данных и восстановление из БД приносят готовый идентификатор."""
    account_id = AccountId.new()

    assert Account.create(Currency.RUB, account_id).id == account_id


def test_create_keeps_currency_of_account() -> None:
    """Валюта берётся у баланса, а не из настроек сервиса."""
    assert Account.create(Currency.KWD).currency is Currency.KWD


def test_create_rejects_non_currency() -> None:
    with pytest.raises(InvalidValueError, match='Валюта счёта: ожидается экземпляр Currency'):
        Account.create('RUB')  # type: ignore[arg-type]


def test_constructor_rejects_plain_uuid_instead_of_account_id() -> None:
    """Сильная типизация: UUID сам по себе не «идентификатор счёта»."""
    with pytest.raises(InvalidIdentifierError, match='Идентификатор счёта: ожидается экземпляр AccountId'):
        Account(uuid4(), Money.zero(Currency.RUB))  # type: ignore[arg-type]


def test_constructor_rejects_non_money_balance() -> None:
    with pytest.raises(InvalidValueError, match='Баланс: ожидается экземпляр Money'):
        Account(AccountId.new(), Decimal('10.00'))  # type: ignore[arg-type]


@pytest.mark.parametrize('raw_flag', [1, 0, 'true', None])
def test_constructor_rejects_non_bool_block_flag(raw_flag: object) -> None:
    """bool — подкласс int: без проверки ``is_blocked=1`` заблокировал бы счёт."""
    with pytest.raises(InvalidValueError, match='Признак блокировки: ожидается экземпляр bool'):
        Account(AccountId.new(), Money.zero(Currency.RUB), is_blocked=raw_flag)  # type: ignore[arg-type]


@pytest.mark.parametrize('raw_version', [True, False, '3', 3.0, None])
def test_constructor_rejects_non_int_version(raw_version: object) -> None:
    with pytest.raises(InvalidValueError, match='Версия: ожидается int'):
        Account(AccountId.new(), Money.zero(Currency.RUB), version=raw_version)  # type: ignore[arg-type]


@pytest.mark.parametrize('raw_version', [0, -1, -100])
def test_constructor_rejects_version_below_minimum(raw_version: int) -> None:
    with pytest.raises(InvalidValueError, match='не может быть меньше'):
        Account(AccountId.new(), Money.zero(Currency.RUB), version=raw_version)


def test_minimum_version_is_positive() -> None:
    assert MIN_VERSION >= 1


def test_state_is_read_only() -> None:
    """Единственный путь изменить счёт — доменный метод, проверивший инварианты."""
    account = make_account()

    with pytest.raises(AttributeError):
        account.balance = rub('999.00')  # type: ignore[misc]
    with pytest.raises(AttributeError):
        account.is_blocked = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        account.version = 100  # type: ignore[misc]


def test_slots_forbid_accidental_attributes() -> None:
    account = make_account()

    with pytest.raises(AttributeError):
        account.owner_id = 'ivan'  # type: ignore[attr-defined]


# --- Пополнение ---


def test_deposit_increases_balance_and_version() -> None:
    account = make_account('100.00')

    account.deposit(rub('250.50'))

    assert account.balance == rub('350.50')
    assert account.version == INITIAL_VERSION + 1


def test_deposit_into_empty_account() -> None:
    account = Account.create(Currency.RUB)

    account.deposit(rub('10.00'))

    assert account.balance == rub('10.00')


def test_deposit_accumulates_over_several_operations() -> None:
    account = Account.create(Currency.RUB)

    for _ in range(3):
        account.deposit(rub('100.00'))

    assert account.balance == rub('300.00')
    assert account.version == INITIAL_VERSION + 3


def test_deposit_of_zero_is_rejected() -> None:
    """«Пополнение на 0 ₽» не меняет состояние, но разбудит версию и события."""
    account = make_account()

    with pytest.raises(InvalidAmountError, match='не может быть нулевой'):
        account.deposit(Money.zero(Currency.RUB))

    assert account.version == INITIAL_VERSION


def test_deposit_in_another_currency_is_rejected() -> None:
    """Неявной конвертации нет: валюта счёта задана балансом раз и навсегда."""
    account = make_account(currency=Currency.RUB)

    with pytest.raises(CurrencyMismatchError, match='открыт в валюте RUB'):
        account.deposit(Money.from_number(10, Currency.USD))

    assert account.balance == rub('100.00')
    assert account.version == INITIAL_VERSION


def test_deposit_of_raw_number_is_rejected() -> None:
    """Голая сумма без валюты — ошибка вызова, а не AttributeError изнутри."""
    account = make_account()

    with pytest.raises(InvalidValueError, match='ожидается экземпляр Money'):
        account.deposit(500)  # type: ignore[arg-type]


def test_deposit_on_blocked_account_is_rejected() -> None:
    account = make_account(is_blocked=True)

    with pytest.raises(AccountBlocked, match='заблокирован'):
        account.deposit(rub('10.00'))

    assert account.balance == rub('100.00')
    assert account.version == INITIAL_VERSION


# --- Списание ---


def test_withdraw_decreases_balance_and_version() -> None:
    account = make_account('100.00')

    account.withdraw(rub('30.25'))

    assert account.balance == rub('69.75')
    assert account.version == INITIAL_VERSION + 1


def test_withdraw_whole_balance_leaves_zero_not_negative() -> None:
    account = make_account('100.00')

    account.withdraw(rub('100.00'))

    assert account.balance == Money.zero(Currency.RUB)


def test_withdraw_more_than_balance_is_rejected() -> None:
    """Овердрафта в платёжном шлюзе нет: минус — это отказ, а не «долг»."""
    account = make_account('100.00')

    with pytest.raises(InsufficientFunds, match='Недостаточно средств'):
        account.withdraw(rub('100.01'))

    assert account.balance == rub('100.00')
    assert account.version == INITIAL_VERSION


def test_withdraw_from_empty_account_is_rejected() -> None:
    account = Account.create(Currency.RUB)

    with pytest.raises(InsufficientFunds):
        account.withdraw(rub('0.01'))


def test_insufficient_funds_message_reports_shortfall() -> None:
    account = make_account('100.00')

    with pytest.raises(InsufficientFunds, match=r'не хватает 25.00'):
        account.withdraw(rub('125.00'))


def test_withdraw_by_exactly_available_amount_is_allowed() -> None:
    """Граница «хватает ровно» — разрешённый случай; отказ начинается с копейки сверх."""
    account = make_account('100.00')

    account.withdraw(rub('100.00'))

    assert account.balance == Money.zero(Currency.RUB)
    assert account.version == INITIAL_VERSION + 1


def test_withdraw_of_zero_is_rejected() -> None:
    account = make_account()

    with pytest.raises(InvalidAmountError, match='не может быть нулевой'):
        account.withdraw(Money.zero(Currency.RUB))

    assert account.version == INITIAL_VERSION


def test_withdraw_in_another_currency_is_rejected() -> None:
    account = make_account(currency=Currency.RUB)

    with pytest.raises(CurrencyMismatchError, match='открыт в валюте RUB'):
        account.withdraw(Money.from_number(10, Currency.USD))

    assert account.version == INITIAL_VERSION


def test_withdraw_of_raw_number_is_rejected() -> None:
    account = make_account()

    with pytest.raises(InvalidValueError, match='ожидается экземпляр Money'):
        account.withdraw(500)  # type: ignore[arg-type]


def test_withdraw_on_blocked_account_is_rejected() -> None:
    """Проверка блокировки идёт до проверки денег: сначала «можно ли вообще»."""
    account = make_account(is_blocked=True)

    with pytest.raises(AccountBlocked, match='заблокирован'):
        account.withdraw(rub('10.00'))

    assert account.balance == rub('100.00')
    assert account.version == INITIAL_VERSION


def test_withdraw_on_blocked_account_with_huge_amount_reports_blocking() -> None:
    account = make_account('100.00', is_blocked=True)

    with pytest.raises(AccountBlocked):
        account.withdraw(rub('99999.00'))


# --- Проверка достаточности средств ---


def test_has_sufficient_funds() -> None:
    account = make_account('100.00')

    assert account.has_sufficient_funds(rub('100.00')) is True
    assert account.has_sufficient_funds(rub('99.99')) is True
    assert account.has_sufficient_funds(rub('100.01')) is False


def test_has_sufficient_funds_is_false_for_other_currency() -> None:
    account = make_account(currency=Currency.RUB)

    assert account.has_sufficient_funds(Money.from_number(1, Currency.USD)) is False


def test_has_sufficient_funds_is_false_for_non_money() -> None:
    account = make_account()

    assert account.has_sufficient_funds(Decimal('10.00')) is False  # type: ignore[arg-type]


def test_has_sufficient_funds_does_not_change_state() -> None:
    account = make_account('100.00')

    account.has_sufficient_funds(rub('50.00'))
    account.has_sufficient_funds(rub('500.00'))

    assert account.balance == rub('100.00')
    assert account.version == INITIAL_VERSION


# --- Блокировка и разблокировка ---


def test_block_sets_flag_and_bumps_version() -> None:
    account = make_account()

    account.block()

    assert account.is_blocked is True
    assert account.is_active is False
    assert account.version == INITIAL_VERSION + 1


def test_block_is_idempotent_and_does_not_bump_version_twice() -> None:
    """Ретраи и вебхуки приходят повторно: «уже заблокирован» — не ошибка."""
    account = make_account()

    account.block()
    account.block()
    account.block()

    assert account.is_blocked is True
    assert account.version == INITIAL_VERSION + 1


def test_unblock_restores_operations() -> None:
    account = make_account()
    account.block()

    account.unblock()
    account.withdraw(rub('10.00'))

    assert account.is_blocked is False
    assert account.balance == rub('90.00')


def test_unblock_is_idempotent() -> None:
    account = make_account()

    account.unblock()
    account.unblock()

    assert account.is_blocked is False
    assert account.version == INITIAL_VERSION


def test_block_unblock_cycle_bumps_version_every_time() -> None:
    account = make_account()

    account.block()
    account.unblock()

    assert account.version == INITIAL_VERSION + 2


def test_blocked_account_can_still_be_unblocked() -> None:
    """Блокировка — «заморозка», а не «счёт насовсем»."""
    account = make_account(is_blocked=True)

    account.unblock()
    account.deposit(rub('10.00'))

    assert account.balance == rub('110.00')


# --- Версия (оптимистичная блокировка) ---


def test_version_grows_monotonically_on_every_change() -> None:
    account = Account.create(Currency.RUB)
    versions = [account.version]

    account.deposit(rub('10.00'))
    versions.append(account.version)
    account.withdraw(rub('5.00'))
    versions.append(account.version)
    account.block()
    versions.append(account.version)
    account.unblock()
    versions.append(account.version)

    assert versions == sorted(versions)
    assert versions == [1, 2, 3, 4, 5]


def test_failed_operations_do_not_bump_version() -> None:
    """Версия описывает реальные изменения: отказы не должны их имитировать."""
    account = make_account('100.00', version=7)
    # Отказы проверяются по факту состояния счёта ниже, а не через pytest.raises:
    # здесь важна не сама ошибка, а неизменность версии.
    failing_operations: tuple[tuple[Exception, object], ...] = (
        (InsufficientFunds, lambda: account.withdraw(rub('500.00'))),
        (CurrencyMismatchError, lambda: account.deposit(Money.from_number(1, Currency.USD))),
        (InvalidAmountError, lambda: account.deposit(Money.zero(Currency.RUB))),
    )

    for expected_error, operation in failing_operations:
        with pytest.raises(expected_error):  # type: ignore[arg-type]
            operation()  # type: ignore[operator]
        assert account.version == 7

    assert account.balance == rub('100.00')


def test_version_is_restored_as_loaded_from_storage() -> None:
    """Репозиторий отдаёт счёт с уже набранной версией, а не с нуля."""
    account = make_account('500.00', version=42)

    assert account.version == 42

    account.deposit(rub('1.00'))

    assert account.version == 43


def test_persisted_version_stays_put_until_storage_confirms() -> None:
    """Ожидаемая версия в хранилище не двигается вместе с изменениями.

    Оптимистичный ``UPDATE`` сверяется именно с ней (T-3.6): пока адаптер хранилища
    не подтвердил запись, она остаётся прочитанной. Иначе проверка версии не поймала
    бы чужую запись — сравнивать было бы не с чем.
    """
    account = make_account('100.00', version=5)

    assert account.persisted_version == 5

    account.deposit(rub('1.00'))

    assert account.version == 6
    assert account.persisted_version == 5

    account.mark_persisted()

    assert account.persisted_version == 6


def test_persisted_version_matches_version_for_new_account() -> None:
    """Новый счёт ждёт в хранилище свою начальную версию."""
    account = Account.create(Currency.RUB)

    assert account.persisted_version == account.version == INITIAL_VERSION


# --- Тождество ---


def test_equality_is_by_identity_not_by_state() -> None:
    """Один счёт, прочитанный дважды с разными балансами, — всё равно один счёт."""
    account_id = AccountId.new()
    first = Account(account_id, rub('100.00'))
    second = Account(account_id, rub('250.00'))

    assert first == second


def test_accounts_with_different_ids_are_not_equal() -> None:
    assert make_account() != make_account()


def test_account_is_not_equal_to_other_types() -> None:
    account = make_account()

    assert account != str(account.id)  # type: ignore[comparison-overlap]
    assert account is not None
    assert (account == object()) is False


def test_account_is_hashable_by_identity() -> None:
    """Мутируемый счёт в set/dict: хеш считается по неизменяемому id."""
    account = make_account()
    same = Account(account.id, rub('0.00'))

    assert len({account, same}) == 1
    registry: dict[Account, str] = {account: 'main'}

    account.withdraw(rub('10.00'))

    assert registry[account] == 'main'


def test_repr_contains_identity_and_state() -> None:
    """Репрезентация попадает в логи и должна отличать счета друг от друга."""
    account = make_account('100.00')

    rendered = repr(account)

    assert str(account.id) in rendered
    assert 'version=1' in rendered
    assert 'is_blocked=False' in rendered
    assert rendered.startswith('Account(')


# --- Иерархия доменных исключений ---


@pytest.mark.parametrize(
    'error',
    [
        pytest.param(InsufficientFunds, id='insufficient_funds'),
        pytest.param(AccountBlocked, id='account_blocked'),
        pytest.param(CurrencyMismatchError, id='currency_mismatch'),
        pytest.param(InvalidAmountError, id='invalid_amount'),
        pytest.param(InvalidValueError, id='invalid_value'),
        pytest.param(InvalidIdentifierError, id='invalid_identifier'),
    ],
)
def test_all_errors_are_catchable_as_domain_error(error: type[Exception]) -> None:
    """Application-слой ловит одно ``DomainError`` — и это не ломается."""
    assert issubclass(error, DomainError)
