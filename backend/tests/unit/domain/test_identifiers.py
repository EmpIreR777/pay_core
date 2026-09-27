"""Тесты идентификаторов сущностей как Value Objects (T-1.1)."""

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from src.core_service.domain import AccountId, Identifier, PaymentId
from src.core_service.domain.exceptions import InvalidIdentifierError


def test_new_generates_unique_uuid4() -> None:
    first = AccountId.new()
    second = AccountId.new()

    assert first.value != second.value
    assert first.value.version == 4


def test_new_preserves_concrete_type() -> None:
    """Фабричный метод конкретного класса не должен вернуть базовый Identifier."""
    account_id = AccountId.new()

    assert isinstance(account_id, AccountId)
    assert not isinstance(account_id, PaymentId)


def test_account_id_and_payment_id_are_not_interchangeable() -> None:
    """Сильная типизация: с UUID внутри эти типы всё равно разные сущности."""
    same_uuid = uuid4()

    assert AccountId(same_uuid) != PaymentId(same_uuid)
    assert AccountId(same_uuid) == AccountId(same_uuid)


def test_equality_with_plain_uuid_is_false() -> None:
    """Идентификатор сущности не равен «голому» UUID: это разные понятия."""
    raw_uuid = uuid4()

    assert AccountId(raw_uuid) != raw_uuid


def test_from_string_round_trip() -> None:
    raw_uuid = uuid4()

    assert AccountId.from_string(str(raw_uuid)) == AccountId(raw_uuid)
    assert PaymentId.from_string(str(raw_uuid)) == PaymentId(raw_uuid)


def test_accepts_string_directly_in_constructor() -> None:
    """Граница слоя (JSON/protobuf) приносит строку — VO её разбирает сам."""
    raw_uuid = str(uuid4())

    assert AccountId(raw_uuid).value == UUID(raw_uuid)


@pytest.mark.parametrize(
    'raw_value',
    ['', 'not-a-uuid', '1234', '00000000-0000-0000-0000-00000000000', 12345, None, 3.5, object()],
)
def test_rejects_invalid_identifiers(raw_value: object) -> None:
    with pytest.raises(InvalidIdentifierError):
        AccountId(raw_value)  # type: ignore[arg-type]


def test_rejects_nil_uuid() -> None:
    """Нулевой UUID не выдан ни одной сущности — принимать его нельзя."""
    with pytest.raises(InvalidIdentifierError, match='Нулевой UUID'):
        AccountId(UUID(int=0))
    with pytest.raises(InvalidIdentifierError, match='Нулевой UUID'):
        PaymentId('00000000-0000-0000-0000-000000000000')


def test_is_hashable_and_usable_as_dict_key() -> None:
    account_id = AccountId.new()
    registry: dict[AccountId, str] = {account_id: 'main'}

    assert registry[AccountId(account_id.value)] == 'main'
    assert len({account_id, AccountId(account_id.value)}) == 1


def test_is_immutable() -> None:
    account_id = AccountId.new()
    with pytest.raises(FrozenInstanceError):
        account_id.value = uuid4()  # type: ignore[misc]


def test_str_and_repr() -> None:
    account_id = AccountId.new()

    assert str(account_id) == str(account_id.value)
    assert repr(account_id) == f'AccountId({account_id.value!r})'


def test_identifier_is_base_class() -> None:
    """Базовый класс нужен для обобщённых репозиториев и сервисов (T-1.2+)."""
    assert isinstance(AccountId.new(), Identifier)
    assert isinstance(PaymentId.new(), Identifier)
