"""Contract-тесты порта ``PaymentProvider`` (T-2.2).

T-2.1 зафиксировал сам факт наличия портов; T-2.2 финализирует контракт
внешнего эквайринга: точные сигнатуры и строгие типы, модель ошибок,
идемпотентность и документированность. Тесты прибивают именно эти аспекты,
чтобы случайная правка подписи или удаление контракта из docstring'а ломали сборку.
"""

import inspect

import pytest

from src.core_service.application.ports import payment_provider as provider_port
from src.core_service.application.ports.payment_provider import (
    PaymentProvider,
    ProviderResult,
    ProviderStatus,
)
from src.core_service.domain.exceptions import (
    DomainError,
    InvalidValueError,
    PaymentProviderError,
)
from src.core_service.domain.value_objects.identifiers import PaymentId
from src.core_service.domain.value_objects.money import Money

# --- Сигнатуры и строгие типы -------------------------------------------------


def test_create_payment_signature_uses_domain_types_and_keyword_only() -> None:
    """``create_payment`` не принимает позиционных аргументов и оперирует доменом."""
    assert inspect.iscoroutinefunction(PaymentProvider.create_payment)

    signature = inspect.signature(PaymentProvider.create_payment)
    parameters = signature.parameters
    assert list(parameters) == ['self', 'payment_id', 'amount', 'idempotency_key']
    assert all(
        parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in ('payment_id', 'amount', 'idempotency_key')
    )
    assert parameters['payment_id'].annotation is PaymentId
    assert parameters['amount'].annotation is Money
    assert parameters['idempotency_key'].annotation is str
    assert signature.return_annotation is ProviderResult


def test_get_status_signature_returns_provider_status() -> None:
    """``get_status`` — идемпотентное чтение: на входе id провайдера, на выходе статус."""
    assert inspect.iscoroutinefunction(PaymentProvider.get_status)

    signature = inspect.signature(PaymentProvider.get_status)
    parameters = signature.parameters
    assert list(parameters) == ['self', 'provider_payment_id']
    assert parameters['provider_payment_id'].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters['provider_payment_id'].annotation is str
    assert signature.return_annotation is ProviderStatus


def test_refund_signature_returns_provider_result_with_money_amount() -> None:
    """``refund`` получает сумму как ``Money``: валюту нельзя «забыть» передать."""
    assert inspect.iscoroutinefunction(PaymentProvider.refund)

    signature = inspect.signature(PaymentProvider.refund)
    parameters = signature.parameters
    assert list(parameters) == ['self', 'provider_payment_id', 'amount']
    assert parameters['provider_payment_id'].annotation is str
    assert parameters['amount'].annotation is Money
    assert signature.return_annotation is ProviderResult


def test_port_exposes_exactly_three_operations() -> None:
    """Контракт порта закрыт: три операции, ровно как в T-2.2."""
    public_methods = {name for name in dir(PaymentProvider) if not name.startswith('_')}
    assert public_methods == {'create_payment', 'get_status', 'refund'}


# --- Документированность контракта (DoD T-2.2) --------------------------------


@pytest.mark.parametrize('method_name', ['create_payment', 'get_status', 'refund'])
def test_operation_is_documented(method_name: str) -> None:
    """У каждой операции есть docstring с описанием параметров и ошибок."""
    docstring = getattr(PaymentProvider, method_name).__doc__ or ''
    assert docstring.strip()
    assert ':param' in docstring
    assert ':returns:' in docstring
    assert ':raises PaymentProviderError:' in docstring


def test_port_documents_error_contract() -> None:
    """DoD: контракт ошибок зафиксирован текстом, а не только типами."""
    docstring = PaymentProvider.__doc__ or ''
    assert 'PaymentProviderError' in docstring
    assert 'FAILED' in docstring


def test_module_documents_saga_and_status_mapping() -> None:
    """Модуль объясняет место вызова в саге, идемпотентность и карту статусов."""
    module_docstring = provider_port.__doc__ or ''
    assert 'idempotency_key' in module_docstring
    assert 'вне транзакции' in module_docstring.lower()
    assert 'ProviderStatus' in module_docstring
    assert 'PaymentStatus' in module_docstring


# --- Модель ошибок ------------------------------------------------------------


def test_technical_failure_is_domain_error() -> None:
    """Технический сбой провайдера — доменная ошибка, которую сценарий может поймать."""
    assert issubclass(PaymentProviderError, DomainError)


# --- Шкала статусов провайдера ------------------------------------------------


def test_provider_status_is_closed_str_scale() -> None:
    """Набор статусов закрыт, значения сериализуемы в строку как есть."""
    assert [member.value for member in ProviderStatus] == [
        'PENDING',
        'PROCESSING',
        'SUCCEEDED',
        'FAILED',
        'REFUNDED',
    ]
    assert isinstance(ProviderStatus.SUCCEEDED, str)
    assert str(ProviderStatus.REFUNDED) == 'REFUNDED'


# --- Строгость ProviderResult -------------------------------------------------


def test_provider_result_is_slotted_and_immutable() -> None:
    """``slots`` + ``frozen``: результат операции нельзя расширять или менять."""
    result = ProviderResult(provider_payment_id='prov-1', status=ProviderStatus.PENDING)
    assert not hasattr(result, '__dict__')
    assert ProviderResult.__slots__ == ('provider_payment_id', 'status')


def test_provider_result_requires_both_fields() -> None:
    """Оба поля обязательны: неполный результат хуже отсутствия результата."""
    with pytest.raises(TypeError):
        ProviderResult(provider_payment_id='prov-1')  # type: ignore[call-arg]


@pytest.mark.parametrize('bad_provider_payment_id', ['', '   ', None, 123])
def test_provider_result_rejects_invalid_provider_payment_id(bad_provider_payment_id: object) -> None:
    """Пустой или нестроковый идентификатор операции недопустим."""
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        ProviderResult(
            provider_payment_id=bad_provider_payment_id,  # type: ignore[arg-type]
            status=ProviderStatus.PENDING,
        )


# --- Структурное соответствие порта -------------------------------------------


class _FakePaymentProvider:
    """Минимальная «форма» порта: наследования от Protocol нет и не требуется.

    Это именно заглушка формы, а не рабочий фейк: аргументы лишь проверяются и
    участвуют в идентификаторе операции, никакой настраиваемости и состояния нет —
    рабочий ``FakePaymentProvider`` (success/failure/delay) живёт в ``tests/fakes.py`` (T-2.9).
    """

    async def create_payment(
        self,
        *,
        payment_id: PaymentId,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResult:
        provider_payment_id = f'{payment_id}:{amount}:{idempotency_key}'
        return ProviderResult(provider_payment_id=provider_payment_id, status=ProviderStatus.PENDING)

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        assert provider_payment_id, 'provider_payment_id не может быть пустым'
        return ProviderStatus.PENDING

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        assert amount.amount > 0, 'сумма возврата должна быть положительной'
        return ProviderResult(provider_payment_id=provider_payment_id, status=ProviderStatus.REFUNDED)


class _IncompleteProvider:
    """Провайдер без ``refund`` не должен считаться реализацией порта."""

    async def create_payment(self, **_kwargs: object) -> None:
        return None

    async def get_status(self, _provider_payment_id: str) -> None:
        return None


def test_structural_object_satisfies_port() -> None:
    """Достаточно совпасть «по форме» — runtime_checkable это подтверждает."""
    assert isinstance(_FakePaymentProvider(), PaymentProvider)


def test_incomplete_object_does_not_satisfy_port() -> None:
    assert not isinstance(_IncompleteProvider(), PaymentProvider)
