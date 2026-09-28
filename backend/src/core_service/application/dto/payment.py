"""DTO платежей прикладного слоя (T-2.3).

``CreatePaymentInput`` / ``CreatePaymentOutput`` / ``GetPaymentInput`` — входы и
выходы сценариев оплаты. Это не доменные объекты и не транспортные схемы:
домен описывает правила поведения, транспорт (gRPC/HTTP) — сериализацию, а DTO
фиксирует контракт между ними и не пропускает «сырые» данные внутрь сценария.

Почему не Pydantic: ядро прикладного слоя обязано оставаться независимым от
веб-фреймворков (Pydantic-схемы — уровень BFF, ЭПИК 7). DTO — иммутабельные
``frozen``-dataclass'ы с валидацией в ``__post_init__``, ровно как типы,
пересекающие порты (``ProviderResult``, ``IdempotencyRecord``).

Поля принимают доменные типы (``AccountId``, ``PaymentId``, ``Money``,
``PaymentStatus``), а не строки и числа: разбор «сырого» входа — задача
транспортного адаптера, и если он пришлёт не то, DTO откажет сразу, на границе
сценария, а не где-то в глубине расчёта баланса.

Все проверки берутся из единого модуля ``domain/validation.py`` (AGENT.md §5:
одно правило — одна реализация). Собственных проверок в DTO не осталось: даже
граница длины ключа идемпотентности — общее правило
(``require_max_length_str``), а сама цифра живёт в порте хранилища ключей
(:data:`MAX_IDEMPOTENCY_KEY_LENGTH`), потому что соответствует ширине колонки БД.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import InvalidIdentifierError
from src.core_service.domain.validation import (
    require_max_length_str,
    require_non_empty_str,
    require_optional_non_empty_str,
    require_payment_output_fields,
    require_payment_source,
    require_positive_money,
    require_type,
    require_utc,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import (
    PaymentStatus,
    require_provider_payment_coherence,
)


def _response_str(response: Mapping[str, Any], field: str) -> str:
    """Достаёт обязательное строковое поле сохранённого ответа.

    Ответ хранилища идемпотентности приходит из БД, а не из нашего кода, поэтому
    проверяется тем же правилом, что и любой вход (``require_non_empty_str``):
    повреждённая запись обязана падать с доменной ошибкой, а не с ``KeyError``
    или ``TypeError`` из глубины разбора.
    """
    return require_non_empty_str(response.get(field), f'ответ идемпотентности: {field}')


@dataclass(frozen=True, slots=True, kw_only=True)
class CreatePaymentInput:
    """Вход сценария создания платежа.

    :param from_account_id: счёт списания (доменный тип, а не строка/UUID);
    :param amount: сумма с валютой; именно она задаёт валюту платежа —
        конвертации в шлюзе нет;
    :param idempotency_key: ключ идемпотентности от клиента. Повтор с тем же
        ключом не создаёт второй платёж, поэтому ключ обрезается по краям и
        проверяется на пустоту и длину.
    :raises InvalidValueError: некорректный тип или значение любого поля;
    :raises InvalidAmountError: нулевая сумма.
    """

    from_account_id: AccountId
    amount: Money
    idempotency_key: str

    def __post_init__(self) -> None:
        require_type(self.from_account_id, AccountId, 'from_account_id', error_type=InvalidIdentifierError)
        require_positive_money(self.amount, 'amount')
        # Ключ идемпотентности: тип и пустота — require_non_empty_str (он же trim),
        # длина — require_max_length_str по границе из порта хранилища ключей.
        key = require_non_empty_str(self.idempotency_key, 'idempotency_key')
        # frozen: нормализуем значение через object.__setattr__, чтобы в поле
        # лежал уже обрезанный ключ, а не то, что прислал клиент.
        object.__setattr__(
            self,
            'idempotency_key',
            require_max_length_str(key, 'idempotency_key', max_length=MAX_IDEMPOTENCY_KEY_LENGTH),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CreatePaymentOutput:
    """Результат сценария создания платежа.

    Отдаётся наружу после саги: сначала платёж зафиксирован в ``PENDING``, затем
    провайдер вернул свой идентификатор и статус, поэтому в ``status`` обычно
    ``PROCESSING`` (провайдер принял) или ``FAILED`` (провайдер отклонил).

    :param payment_id: идентификатор созданного платежа;
    :param status: статус платежа после саги;
    :param amount: сумма платежа;
    :param created_at: время создания (UTC);
    :param provider_payment_id: идентификатор операции у провайдера; ``None``
        только там, где операция до провайдера не дошла (например, ``FAILED``
        из-за технического сбоя);
    :raises InvalidValueError: некорректный тип/значение поля или рассинхрон
        статуса и ``provider_payment_id``.
    """

    payment_id: PaymentId
    status: PaymentStatus
    amount: Money
    created_at: datetime
    provider_payment_id: str | None = None

    def __post_init__(self) -> None:
        require_payment_output_fields(self.payment_id, self.status, self.amount)
        require_utc(self.created_at, 'created_at')
        provider_payment_id = require_optional_non_empty_str(self.provider_payment_id, 'provider_payment_id')
        object.__setattr__(self, 'provider_payment_id', provider_payment_id)
        require_provider_payment_coherence(self.status, provider_payment_id)

    # --- Преобразования (владение форматом — здесь, а не в сценарии) -----------

    @classmethod
    def from_payment(cls, payment: Payment) -> Self:
        """Собирает результат сценария из сущности платежа.

        Единственное место, где доменный объект превращается в DTO: правило
        «какие поля сущности попадают наружу» не должно повторяться по сценариям
        (T-2.5, T-2.7 собирают такие же выходы).

        :raises InvalidValueError: если сущность в статусе, несовместимом с её
            же идентификатором операции у провайдера.
        """
        require_type(payment, Payment, 'payment')
        return cls(
            payment_id=payment.id,
            status=payment.status,
            amount=payment.amount,
            created_at=payment.created_at,
            provider_payment_id=payment.provider_payment_id,
        )

    def to_idempotency_response(self) -> dict[str, Any]:
        """Приводит результат к виду, пригодному для хранения ответа-ключа.

        Формат ответа хранилища идемпотентности — тоже «факт с владельцем»:
        его читает повторный запрос (T-4.2), поэтому он живёт рядом с самим
        DTO, а не собирается по месту вызова. Всё, что не выражается
        JSON-скалярами, кодируется строкой и разбирается обратно в
        :meth:`from_idempotency_response` — доменные типы на границе хранилища
        не сериализуются «как получится».
        """
        return {
            'payment_id': str(self.payment_id),
            'status': str(self.status),
            'amount': str(self.amount.amount),
            'currency': str(self.amount.currency),
            'created_at': self.created_at.isoformat(),
            'provider_payment_id': self.provider_payment_id,
        }

    @classmethod
    def from_idempotency_response(cls, response: Mapping[str, Any]) -> Self:
        """Восстанавливает результат из сохранённого ответа хранилища.

        Обратная операция к :meth:`to_idempotency_response`. Платеж, который
        повторный запрос вернёт клиенту, проходит ту же валидацию, что и
        только что созданный, — иначе «повтор» отдавал бы ослабленный DTO.

        :raises InvalidValueError: если ответ повреждён или неполон.
        """
        return cls(
            payment_id=PaymentId.from_string(_response_str(response, 'payment_id')),
            status=PaymentStatus(_response_str(response, 'status')),
            amount=Money(
                amount=Decimal(_response_str(response, 'amount')),
                currency=Currency(_response_str(response, 'currency')),
            ),
            created_at=datetime.fromisoformat(_response_str(response, 'created_at')),
            provider_payment_id=require_optional_non_empty_str(
                response.get('provider_payment_id'),
                'provider_payment_id',
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GetPaymentOutput:
    """Результат чтения платежа.

    Отдельный тип, а не переиспользование ``CreatePaymentOutput``: чтение отдаёт
    больше, чем создание. Клиенту (и SSE-стриму T-2.8) нужны обе временные метки,
    счёт-источник и текст причины отказа — иначе UI пришлось бы угадывать, почему
    платёж ``FAILED`` и когда он последний раз менялся. Дублировать поля «на
    всякий случай» в том же классе тоже нельзя: у создания часть полей
    принципиально не заполняется, и лишние ``None`` протекали бы в контракт.

    :param payment_id: идентификатор платежа;
    :param status: статус на момент чтения (после актуализации у провайдера);
    :param amount: сумма платежа;
    :param from_account_id: счёт списания;
    :param created_at: время создания (UTC);
    :param updated_at: время последнего изменения статуса (UTC);
    :param provider_payment_id: идентификатор операции у провайдера, если была;
    :param failure_reason: причина отказа при ``FAILED``, иначе ``None``;
    :raises InvalidValueError: некорректный тип/значение поля, рассинхрон статуса
        и ``provider_payment_id`` либо ``updated_at`` раньше ``created_at``.
    """

    payment_id: PaymentId
    status: PaymentStatus
    amount: Money
    from_account_id: AccountId
    created_at: datetime
    updated_at: datetime
    provider_payment_id: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        require_payment_output_fields(self.payment_id, self.status, self.amount)
        require_payment_source(self.from_account_id, self.created_at, self.updated_at)
        provider_payment_id = require_optional_non_empty_str(self.provider_payment_id, 'provider_payment_id')
        object.__setattr__(self, 'provider_payment_id', provider_payment_id)
        object.__setattr__(
            self,
            'failure_reason',
            require_optional_non_empty_str(self.failure_reason, 'failure_reason'),
        )
        require_provider_payment_coherence(self.status, provider_payment_id)

    @classmethod
    def from_payment(cls, payment: Payment) -> Self:
        """Собирает результат чтения из сущности платежа.

        :raises InvalidValueError: если сущность нарушает свои же инварианты
            (например, несогласованные статус и идентификатор операции).
        """
        require_type(payment, Payment, 'payment')
        return cls(
            payment_id=payment.id,
            status=payment.status,
            amount=payment.amount,
            from_account_id=payment.from_account_id,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
            provider_payment_id=payment.provider_payment_id,
            failure_reason=payment.failure_reason,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GetPaymentInput:
    """Вход сценария чтения платежа.

    Единственное поле — идентификатор: актуализацию статуса у провайдера сценарий
    выполняет сам, увидев «зависший» ``PROCESSING`` (T-2.5). Отдельный флаг
    «сходить к провайдеру» не нужен — он позволил бы обойти это правило.

    :raises InvalidValueError: если передан не ``PaymentId``.
    """

    payment_id: PaymentId

    def __post_init__(self) -> None:
        require_type(self.payment_id, PaymentId, 'payment_id', error_type=InvalidIdentifierError)


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelPaymentInput:
    """Вход сценария отмены платежа (T-2.6).

    Отменить можно только платёж в статусе ``PENDING``.

    :param payment_id: идентификатор отменяемого платежа;
    :param reason: необязательная причина отмены (для аудита и событий).
    :raises InvalidValueError: если передан не ``PaymentId`` или пустая строка причины.
    """

    payment_id: PaymentId
    reason: str | None = None

    def __post_init__(self) -> None:
        require_type(self.payment_id, PaymentId, 'payment_id', error_type=InvalidIdentifierError)
        object.__setattr__(
            self,
            'reason',
            require_optional_non_empty_str(self.reason, 'reason'),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelPaymentOutput:
    """Результат сценария отмены платежа (T-2.6).

    :param payment_id: идентификатор отменённого платежа;
    :param status: статус после отмены (``CANCELLED``);
    :param amount: сумма платежа, возвращённая на баланс;
    :param from_account_id: счёт списания, на который вернулись деньги;
    :param created_at: время создания платежа (UTC);
    :param updated_at: время отмены платежа (UTC);
    """

    payment_id: PaymentId
    status: PaymentStatus
    amount: Money
    from_account_id: AccountId
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        require_payment_output_fields(self.payment_id, self.status, self.amount)
        require_payment_source(self.from_account_id, self.created_at, self.updated_at)

    @classmethod
    def from_payment(cls, payment: Payment) -> Self:
        """Собирает результат отмены из сущности платежа.

        :raises InvalidValueError: если сущность нарушает свои инварианты.
        """
        require_type(payment, Payment, 'payment')
        return cls(
            payment_id=payment.id,
            status=payment.status,
            amount=payment.amount,
            from_account_id=payment.from_account_id,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )
