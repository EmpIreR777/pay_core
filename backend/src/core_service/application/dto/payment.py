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

from dataclasses import dataclass
from datetime import datetime

from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.core_service.domain.exceptions import InvalidIdentifierError
from src.core_service.domain.validation import (
    require_max_length_str,
    require_non_empty_str,
    require_optional_non_empty_str,
    require_positive_money,
    require_type,
    require_utc,
)
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import (
    PaymentStatus,
    require_provider_payment_coherence,
)


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
        require_type(self.payment_id, PaymentId, 'payment_id', error_type=InvalidIdentifierError)
        require_type(self.status, PaymentStatus, 'status')
        require_positive_money(self.amount, 'amount')
        require_utc(self.created_at, 'created_at')
        provider_payment_id = require_optional_non_empty_str(self.provider_payment_id, 'provider_payment_id')
        object.__setattr__(self, 'provider_payment_id', provider_payment_id)
        require_provider_payment_coherence(self.status, provider_payment_id)


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
