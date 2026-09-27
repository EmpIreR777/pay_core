"""Тесты базового класса DomainEvent (T-1.4).

Проверяются:
* Неизменяемость (frozen=True).
* Генерация уникального event_id (UUID4) по умолчанию и поддержка явного UUID.
* Временная метка occurred_at: по умолчанию UTC now, строгая валидация timezone-aware UTC.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from src.core_service.domain.events import (
    PaymentCreated,
    PaymentFailed,
    PaymentRefunded,
    PaymentSettled,
)
from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.exceptions import InvalidValueError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money


class TestDomainEventBase:
    """Тесты базового класса DomainEvent."""

    def test_default_values_generated(self) -> None:
        event = DomainEvent()
        assert isinstance(event.event_id, UUID)
        assert event.event_id.version == 4
        assert isinstance(event.occurred_at, datetime)
        assert event.occurred_at.tzinfo == UTC
        assert abs((datetime.now(UTC) - event.occurred_at).total_seconds()) < 5.0

    def test_unique_event_ids(self) -> None:
        event1 = DomainEvent()
        event2 = DomainEvent()
        assert event1.event_id != event2.event_id

    def test_custom_event_id_and_occurred_at(self) -> None:
        custom_id = uuid4()
        custom_time = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        event = DomainEvent(event_id=custom_id, occurred_at=custom_time)
        assert event.event_id == custom_id
        assert event.occurred_at == custom_time

    def test_immutability(self) -> None:
        event = DomainEvent()
        with pytest.raises(FrozenInstanceError):
            event.event_id = uuid4()  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            event.occurred_at = datetime.now(UTC)  # type: ignore[misc]

    def test_rejects_non_uuid_event_id(self) -> None:
        with pytest.raises(InvalidValueError, match='event_id: ожидается экземпляр UUID'):
            DomainEvent(event_id='not-a-uuid')  # type: ignore[arg-type]

    def test_rejects_naive_occurred_at(self) -> None:
        naive_dt = datetime(2025, 1, 1, 12, 0)
        with pytest.raises(InvalidValueError, match='timezone-aware'):
            DomainEvent(occurred_at=naive_dt)

    def test_rejects_non_datetime_occurred_at(self) -> None:
        with pytest.raises(InvalidValueError, match='occurred_at: ожидается datetime'):
            DomainEvent(occurred_at=123456789)  # type: ignore[arg-type]

    def test_rejects_non_utc_occurred_at(self) -> None:
        tz_plus_3 = timezone(timedelta(hours=3))
        dt_with_offset = datetime(2025, 1, 1, 12, 0, tzinfo=tz_plus_3)
        with pytest.raises(InvalidValueError, match='UTC'):
            DomainEvent(occurred_at=dt_with_offset)


def _create_sample_ids() -> tuple[PaymentId, AccountId, Money]:
    payment_id = PaymentId.new()
    account_id = AccountId.new()
    amount = Money.from_number(100, Currency.RUB)
    return payment_id, account_id, amount


class TestPaymentCreated:
    """Тесты события PaymentCreated."""

    def test_creation_success(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentCreated(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
        )
        assert isinstance(event, DomainEvent)
        assert event.payment_id == payment_id
        assert event.from_account_id == account_id
        assert event.amount == amount
        assert isinstance(event.event_id, UUID)
        assert event.occurred_at.tzinfo == UTC

    def test_immutability(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentCreated(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
        )
        with pytest.raises(FrozenInstanceError):
            event.amount = Money.from_number(200, Currency.RUB)  # type: ignore[misc]

    @pytest.mark.parametrize(
        ('invalid_arg', 'match'),
        [
            ({'payment_id': 'not-a-payment-id'}, 'payment_id: ожидается экземпляр PaymentId'),
            ({'from_account_id': 'not-an-account-id'}, 'from_account_id: ожидается экземпляр AccountId'),
            ({'amount': 100}, 'amount: ожидается экземпляр Money'),
        ],
    )
    def test_rejects_invalid_types(self, invalid_arg: dict[str, object], match: str) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        params: dict[str, object] = {
            'payment_id': payment_id,
            'from_account_id': account_id,
            'amount': amount,
        }
        params.update(invalid_arg)
        with pytest.raises(InvalidValueError, match=match):
            PaymentCreated(**params)  # type: ignore[arg-type]


class TestPaymentSettled:
    """Тесты события PaymentSettled."""

    def test_creation_success(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentSettled(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
            provider_payment_id='yoo_12345',
        )
        assert isinstance(event, DomainEvent)
        assert event.payment_id == payment_id
        assert event.from_account_id == account_id
        assert event.amount == amount
        assert event.provider_payment_id == 'yoo_12345'

    def test_creation_default_provider_payment_id_is_none(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentSettled(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
        )
        assert event.provider_payment_id is None

    def test_immutability(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentSettled(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
        )
        with pytest.raises(FrozenInstanceError):
            event.provider_payment_id = 'other'  # type: ignore[misc]

    def test_rejects_non_str_provider_payment_id(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        with pytest.raises(InvalidValueError, match='provider_payment_id: ожидается непустая строка или None'):
            PaymentSettled(
                payment_id=payment_id,
                from_account_id=account_id,
                amount=amount,
                provider_payment_id=12345,  # type: ignore[arg-type]
            )


class TestPaymentFailed:
    """Тесты события PaymentFailed."""

    def test_creation_success(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentFailed(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
            reason='Insufficient funds on external card',
        )
        assert isinstance(event, DomainEvent)
        assert event.payment_id == payment_id
        assert event.from_account_id == account_id
        assert event.amount == amount
        assert event.reason == 'Insufficient funds on external card'

    def test_immutability(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentFailed(
            payment_id=payment_id,
            from_account_id=account_id,
            amount=amount,
            reason='Error',
        )
        with pytest.raises(FrozenInstanceError):
            event.reason = 'New reason'  # type: ignore[misc]

    def test_rejects_non_str_reason(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        with pytest.raises(InvalidValueError, match='reason: ожидается непустая строка'):
            PaymentFailed(
                payment_id=payment_id,
                from_account_id=account_id,
                amount=amount,
                reason=123,  # type: ignore[arg-type]
            )


class TestPaymentRefunded:
    """Тесты события PaymentRefunded."""

    def test_creation_success(self) -> None:
        payment_id, account_id, _ = _create_sample_ids()
        refund_amount = Money.from_number(50, Currency.RUB)
        event = PaymentRefunded(
            payment_id=payment_id,
            from_account_id=account_id,
            refunded_amount=refund_amount,
            reason='Customer requested cancellation',
        )
        assert isinstance(event, DomainEvent)
        assert event.payment_id == payment_id
        assert event.from_account_id == account_id
        assert event.refunded_amount == refund_amount
        assert event.reason == 'Customer requested cancellation'

    def test_creation_default_reason_is_none(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentRefunded(
            payment_id=payment_id,
            from_account_id=account_id,
            refunded_amount=amount,
        )
        assert event.reason is None

    def test_immutability(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        event = PaymentRefunded(
            payment_id=payment_id,
            from_account_id=account_id,
            refunded_amount=amount,
        )
        with pytest.raises(FrozenInstanceError):
            event.refunded_amount = Money.from_number(10, Currency.RUB)  # type: ignore[misc]

    def test_rejects_non_str_reason(self) -> None:
        payment_id, account_id, amount = _create_sample_ids()
        with pytest.raises(InvalidValueError, match='reason: ожидается непустая строка или None'):
            PaymentRefunded(
                payment_id=payment_id,
                from_account_id=account_id,
                refunded_amount=amount,
                reason=999,  # type: ignore[arg-type]
            )
