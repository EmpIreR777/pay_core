"""Тесты DTO прикладного слоя (T-2.3).

Проверяем DoD задачи с двух сторон: **строгую типизацию** (поля объявлены
доменными типами, DTO иммутабельны и keyword-only) и **валидацию** (нулевые
суммы, пустые/длинные ключи идемпотентности, рассинхрон статуса и идентификатора
провайдера, timezone-naive время).
"""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core_service.application import dto
from src.core_service.application.dto import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    PROVIDER_BOUND_PAYMENT_STATUSES,
    CancelPaymentInput,
    CancelPaymentOutput,
    CreatePaymentInput,
    CreatePaymentOutput,
    GetPaymentInput,
)
from src.core_service.application.ports import payment_provider as provider_port
from src.core_service.domain.exceptions import (
    DomainError,
    InvalidAmountError,
    InvalidValueError,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus

ACCOUNT_ID = AccountId.new()
PAYMENT_ID = PaymentId.new()
AMOUNT = Money.from_number(Decimal('100.50'), Currency.RUB)
CREATED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

#: Ожидаемые типы полей: доменные VO, а не примитивы.
DTO_FIELDS: dict[type, dict[str, type]] = {
    CreatePaymentInput: {
        'from_account_id': AccountId,
        'amount': Money,
        'idempotency_key': str,
    },
    CreatePaymentOutput: {
        'payment_id': PaymentId,
        'status': PaymentStatus,
        'amount': Money,
        'created_at': datetime,
        'provider_payment_id': str | None,
    },
    CancelPaymentInput: {
        'payment_id': PaymentId,
        'reason': str | None,
    },
    CancelPaymentOutput: {
        'payment_id': PaymentId,
        'status': PaymentStatus,
        'amount': Money,
        'from_account_id': AccountId,
        'created_at': datetime,
        'updated_at': datetime,
    },
    GetPaymentInput: {'payment_id': PaymentId},
}


def _input(**overrides: object) -> CreatePaymentInput:
    """Собирает валидный вход создания платежа с точечными переопределениями."""
    kwargs: dict[str, object] = {
        'from_account_id': ACCOUNT_ID,
        'amount': AMOUNT,
        'idempotency_key': 'key-1',
    }
    kwargs.update(overrides)
    return CreatePaymentInput(**kwargs)  # type: ignore[arg-type]


def _output(**overrides: object) -> CreatePaymentOutput:
    """Собирает валидный выход создания платежа с точечными переопределениями."""
    kwargs: dict[str, object] = {
        'payment_id': PAYMENT_ID,
        'status': PaymentStatus.PROCESSING,
        'amount': AMOUNT,
        'created_at': CREATED_AT,
        'provider_payment_id': 'prov-1',
    }
    kwargs.update(overrides)
    return CreatePaymentOutput(**kwargs)  # type: ignore[arg-type]


def _cancel_output(**overrides: object) -> CancelPaymentOutput:
    """Собирает валидный выход отмены платежа с точечными переопределениями."""
    kwargs: dict[str, object] = {
        'payment_id': PAYMENT_ID,
        'status': PaymentStatus.CANCELLED,
        'amount': AMOUNT,
        'from_account_id': ACCOUNT_ID,
        'created_at': CREATED_AT,
        'updated_at': CREATED_AT,
    }
    kwargs.update(overrides)
    return CancelPaymentOutput(**kwargs)  # type: ignore[arg-type]


INSTANCES: dict[type, object] = {
    CancelPaymentInput: CancelPaymentInput(payment_id=PAYMENT_ID),
    CancelPaymentOutput: _cancel_output(),
    CreatePaymentInput: _input(),
    CreatePaymentOutput: _output(),
    GetPaymentInput: GetPaymentInput(payment_id=PAYMENT_ID),
}


# --- Строгая типизация и иммутабельность --------------------------------------


@pytest.mark.parametrize('dto_type', list(DTO_FIELDS))
def test_dto_fields_declare_domain_types(dto_type: type) -> None:
    """Поля типизированы доменными VO (``AccountId``/``Money``/...), а не ``str``/``Decimal``."""
    declared = {field.name: field.type for field in dataclasses.fields(dto_type)}
    assert declared == DTO_FIELDS[dto_type]


@pytest.mark.parametrize('dto_type', list(DTO_FIELDS))
def test_dto_fields_are_keyword_only(dto_type: type) -> None:
    """``kw_only`` защищает от перепутывания однотипных полей на call-site."""
    assert all(field.kw_only for field in dataclasses.fields(dto_type))


@pytest.mark.parametrize('dto_type', list(INSTANCES))
def test_dto_is_frozen_and_slotted(dto_type: type) -> None:
    """DTO нельзя изменить после создания и нельзя «дополнить» случайным атрибутом."""
    instance = INSTANCES[dto_type]
    assert not hasattr(instance, '__dict__')
    assert dto_type.__slots__ == tuple(field.name for field in dataclasses.fields(dto_type))

    first_field = dataclasses.fields(dto_type)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, first_field, None)


def test_dtos_require_all_mandatory_fields() -> None:
    """Обязательные поля нельзя «забыть»: конструктор их требует."""
    with pytest.raises(TypeError):
        CreatePaymentInput(from_account_id=ACCOUNT_ID)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CreatePaymentOutput(status=PaymentStatus.FAILED)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GetPaymentInput()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CancelPaymentInput()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CancelPaymentOutput(status=PaymentStatus.CANCELLED)  # type: ignore[call-arg]


# --- CreatePaymentInput: валидация входа --------------------------------------


def test_input_accepts_valid_values_and_strips_key() -> None:
    """Ключ идемпотентности нормализуется: пробелы по краям не создают «новый» ключ."""
    dto_input = _input(idempotency_key='  key-1  ')
    assert dto_input.idempotency_key == 'key-1'
    assert dto_input.from_account_id == ACCOUNT_ID
    assert dto_input.amount == AMOUNT
    assert dto_input.amount.currency is Currency.RUB


@pytest.mark.parametrize('bad_account_id', ['acc-1', str(ACCOUNT_ID), uuid4(), None, 123])
def test_input_rejects_non_account_id(bad_account_id: object) -> None:
    """Строка или «сырой» UUID вместо ``AccountId`` — ошибка вызова, а не счёта."""
    with pytest.raises(InvalidValueError, match='from_account_id'):
        _input(from_account_id=bad_account_id)


@pytest.mark.parametrize('bad_amount', [Decimal('100.50'), 10050, '100.50', None, 1.5])
def test_input_rejects_non_money_amount(bad_amount: object) -> None:
    """Сумма обязана быть ``Money``: без валюты и точности платёж не создать."""
    with pytest.raises(InvalidValueError, match='amount'):
        _input(amount=bad_amount)


def test_input_rejects_zero_amount() -> None:
    """Нулевой платёж не переводит денег, но занимает строку, версию и событие."""
    with pytest.raises(InvalidAmountError, match='нулевой'):
        _input(amount=Money.zero(Currency.RUB))


@pytest.mark.parametrize('bad_key', [None, 123, '', '   '])
def test_input_rejects_bad_idempotency_key(bad_key: object) -> None:
    """Без ключа идемпотентности повтор запроса создаст второй платёж."""
    with pytest.raises(InvalidValueError, match='idempotency_key'):
        _input(idempotency_key=bad_key)


def test_input_rejects_too_long_idempotency_key() -> None:
    """Длина ключа ограничена колонкой ``idempotency_keys.key`` (VARCHAR(255))."""
    with pytest.raises(InvalidValueError, match='длина'):
        _input(idempotency_key='k' * (MAX_IDEMPOTENCY_KEY_LENGTH + 1))


def test_input_accepts_idempotency_key_of_max_length() -> None:
    """Граница включительна: ровно 255 символов — ещё валидный ключ."""
    key = 'k' * MAX_IDEMPOTENCY_KEY_LENGTH
    assert _input(idempotency_key=key).idempotency_key == key


# --- CreatePaymentOutput: валидация выхода саги -------------------------------


def test_output_accepts_processing_with_provider_payment_id() -> None:
    """Успешный путь саги: провайдер принял платёж, идентификатор операции сохранён."""
    dto_output = _output()
    assert dto_output.payment_id == PAYMENT_ID
    assert dto_output.status is PaymentStatus.PROCESSING
    assert dto_output.provider_payment_id == 'prov-1'
    assert dto_output.created_at == CREATED_AT


def test_output_accepts_failed_without_provider_payment_id() -> None:
    """Технический сбой до провайдера: платёж FAILED, идентификатора операции нет."""
    dto_output = _output(status=PaymentStatus.FAILED, provider_payment_id=None)
    assert dto_output.status is PaymentStatus.FAILED
    assert dto_output.provider_payment_id is None


@pytest.mark.parametrize('provider_bound_status', sorted(PROVIDER_BOUND_PAYMENT_STATUSES))
def test_output_requires_provider_payment_id_for_provider_bound_statuses(
    provider_bound_status: PaymentStatus,
) -> None:
    """STATUS ⇒ provider_payment_id: в PROCESSING/SETTLED платёж уже у провайдера."""
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        _output(status=provider_bound_status, provider_payment_id=None)


def test_output_rejects_provider_payment_id_for_pending() -> None:
    """PENDING означает «провайдер ещё не вызывался», поэтому чужой id там ложь."""
    with pytest.raises(InvalidValueError, match='PENDING'):
        _output(status=PaymentStatus.PENDING, provider_payment_id='prov-1')


@pytest.mark.parametrize('bad_provider_payment_id', [42, '', '   '])
def test_output_rejects_blank_provider_payment_id(bad_provider_payment_id: object) -> None:
    """Пустой или нестроковый идентификатор операции недопустим."""
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        _output(provider_payment_id=bad_provider_payment_id)


def test_output_strips_provider_payment_id() -> None:
    """Идентификатор провайдера нормализуется так же, как в домене."""
    assert _output(provider_payment_id='  prov-1  ').provider_payment_id == 'prov-1'


@pytest.mark.parametrize('bad_payment_id', [str(PAYMENT_ID), uuid4(), None])
def test_output_rejects_non_payment_id(bad_payment_id: object) -> None:
    """Строка или «сырой» UUID вместо ``PaymentId`` не проходят границу DTO."""
    with pytest.raises(InvalidValueError, match='payment_id'):
        _output(payment_id=bad_payment_id)


def test_output_rejects_non_payment_status() -> None:
    """Строка-статус (``'PROCESSING'``) — не ``PaymentStatus``, хотя и равна ему по значению."""
    with pytest.raises(InvalidValueError, match='status'):
        _output(status='PROCESSING')


@pytest.mark.parametrize('bad_created_at', ['2026-09-27T12:00:00', 1727438400, None])
def test_output_rejects_non_datetime_created_at(bad_created_at: object) -> None:
    with pytest.raises(InvalidValueError, match='created_at'):
        _output(created_at=bad_created_at)


def test_output_rejects_naive_created_at() -> None:
    """Наивное время запрещено: в разных зонах оно читается по-разному."""
    with pytest.raises(InvalidValueError, match='timezone-aware'):
        _output(created_at=datetime(2026, 9, 27, 12, 0))


def test_output_rejects_non_utc_created_at() -> None:
    """Допустим только UTC: локальные зоны ломают сравнение «зависших» платежей."""
    with pytest.raises(InvalidValueError, match='UTC'):
        _output(created_at=datetime(2026, 9, 27, 12, 0, tzinfo=timezone(timedelta(hours=3))))


# --- GetPaymentInput ----------------------------------------------------------


def test_get_input_accepts_payment_id() -> None:
    assert GetPaymentInput(payment_id=PAYMENT_ID).payment_id == PAYMENT_ID


@pytest.mark.parametrize('bad_payment_id', ['pay-1', uuid4(), None, 123])
def test_get_input_rejects_non_payment_id(bad_payment_id: object) -> None:
    with pytest.raises(InvalidValueError, match='payment_id'):
        GetPaymentInput(payment_id=bad_payment_id)


# --- CancelPaymentInput / CancelPaymentOutput ---------------------------------


def test_cancel_input_accepts_valid_payment_id_and_reason() -> None:
    inp = CancelPaymentInput(payment_id=PAYMENT_ID, reason='Customer request')
    assert inp.payment_id == PAYMENT_ID
    assert inp.reason == 'Customer request'


def test_cancel_input_accepts_none_reason() -> None:
    inp = CancelPaymentInput(payment_id=PAYMENT_ID)
    assert inp.reason is None


@pytest.mark.parametrize('bad_payment_id', ['pay-1', uuid4(), None, 123])
def test_cancel_input_rejects_non_payment_id(bad_payment_id: object) -> None:
    with pytest.raises(InvalidValueError, match='payment_id'):
        CancelPaymentInput(payment_id=bad_payment_id)  # type: ignore[arg-type]


def test_cancel_input_rejects_empty_reason() -> None:
    with pytest.raises(InvalidValueError, match='reason: ожидается непустая строка или None'):
        CancelPaymentInput(payment_id=PAYMENT_ID, reason='   ')


def test_cancel_output_accepts_valid_data() -> None:
    out = _cancel_output()
    assert out.payment_id == PAYMENT_ID
    assert out.status == PaymentStatus.CANCELLED
    assert out.amount == AMOUNT
    assert out.from_account_id == ACCOUNT_ID


def test_cancel_output_rejects_updated_at_before_created_at() -> None:
    with pytest.raises(InvalidValueError, match='updated_at не может предшествовать created_at'):
        _cancel_output(
            created_at=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
        )


# --- Единый источник правды и таксономия ошибок -------------------------------


def test_dto_reexports_provider_types_from_port() -> None:
    """Провайдерские типы живут в порту, DTO лишь реэкспортирует их (одна правда)."""
    assert dto.ProviderResult is provider_port.ProviderResult
    assert dto.ProviderStatus is provider_port.ProviderStatus


def test_application_layer_reexports_dtos() -> None:
    """Наружу слоя DTO видны через ``src.core_service.application``."""
    from src.core_service import application

    assert application.CreatePaymentInput is CreatePaymentInput
    assert application.CreatePaymentOutput is CreatePaymentOutput
    assert application.GetPaymentInput is GetPaymentInput
    assert application.MAX_IDEMPOTENCY_KEY_LENGTH == MAX_IDEMPOTENCY_KEY_LENGTH
    assert application.CancelPaymentInput is CancelPaymentInput
    assert application.CancelPaymentOutput is CancelPaymentOutput


def test_provider_bound_statuses_cover_processing_and_settled() -> None:
    assert set(PROVIDER_BOUND_PAYMENT_STATUSES) == {PaymentStatus.PROCESSING, PaymentStatus.SETTLED}


@pytest.mark.parametrize(
    'error_type',
    [InvalidValueError, InvalidAmountError],
)
def test_validation_errors_are_domain_errors(error_type: type) -> None:
    """Валидация DTO поднимает доменные ошибки: транспорт маппит их единообразно."""
    assert issubclass(error_type, DomainError)


def test_validation_failure_is_catchable_as_domain_error() -> None:
    with pytest.raises(DomainError):
        _input(idempotency_key='')
