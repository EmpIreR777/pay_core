"""Платёж как доменная сущность (T-1.3).

Ключевые инварианты:

* **Тождество, а не значение.** Два объекта платежа с одинаковым ``id`` —
  это один и тот же платёж. Равенство (``__eq__``) и хеширование (``__hash__``)
  строятся строго по ``PaymentId``.
* **Неизменность финансовых атрибутов.** Идентификатор счёта списания
  (``from_account_id``) и сумма (``amount``) фиксируются при создании платежа
  и не подлежат мутации.
* **Сумма платежа положительна.** Нельзя создать платёж на ноль или отрицательную
  сумму (инвариант ``Money`` + запрет нулевых транзакций).
* **Жёсткая статус-машина переходов.**
    PENDING ──► PROCESSING ──┬──► SETTLED
       │                     └──► FAILED
       ▼
    CANCELLED
  Любой несанкционированный переход отвергается с ``InvalidTransition``.
* **Терминальные статусы окончательны.** Из ``SETTLED``, ``FAILED``, ``CANCELLED``
  нельзя перейти ни в какой другой статус, включая повторный переход в тот же.
* **Версия меняется только при успешной смене статуса.** Попытка невалидного
  перехода оставляет версию нетронутой (защита оптимистичной блокировки).
* **Временные метки (UTC).** ``created_at`` фиксируется при создании платежа,
  ``updated_at`` обновляется при каждом валидном переходе статуса.
"""

from datetime import UTC, datetime
from typing import Self

from src.core_service.domain.exceptions import (
    InvalidTransition,
)
from src.core_service.domain.validation import (
    require_min_int,
    require_optional_non_empty_str,
    require_payment_fields,
    require_type,
    require_utc,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import (
    ALLOWED_TRANSITIONS,
    PaymentStatus,
    require_time_order,
)
from src.core_service.domain.versioning import INITIAL_VERSION, MIN_VERSION

__all__ = ('INITIAL_VERSION', 'MIN_VERSION', 'Payment')


class Payment:
    """Сущность платежа со строгой статус-машиной и контролем версий."""

    __slots__ = (
        '_amount',
        '_created_at',
        '_failure_reason',
        '_from_account_id',
        '_id',
        '_persisted_version',
        '_provider_payment_id',
        '_status',
        '_updated_at',
        '_version',
    )

    def __init__(
        self,
        payment_id: PaymentId,
        from_account_id: AccountId,
        amount: Money,
        *,
        status: PaymentStatus = PaymentStatus.PENDING,
        version: int = INITIAL_VERSION,
        provider_payment_id: str | None = None,
        failure_reason: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        require_payment_fields(payment_id, from_account_id, amount)
        require_type(status, PaymentStatus, 'status')
        checked_version = require_min_int(version, 'version', minimum=MIN_VERSION)
        checked_provider_payment_id = require_optional_non_empty_str(provider_payment_id, 'provider_payment_id')
        checked_failure_reason = require_optional_non_empty_str(failure_reason, 'failure_reason')

        now = datetime.now(UTC)
        effective_created_at = require_utc(created_at, 'created_at') if created_at is not None else now
        effective_updated_at = require_utc(updated_at, 'updated_at') if updated_at is not None else effective_created_at

        require_time_order(effective_created_at, effective_updated_at)

        self._id: PaymentId = payment_id
        self._from_account_id: AccountId = from_account_id
        self._amount: Money = amount
        self._status: PaymentStatus = status
        self._version: int = checked_version
        self._persisted_version: int = checked_version
        self._provider_payment_id: str | None = checked_provider_payment_id
        self._failure_reason: str | None = checked_failure_reason
        self._created_at: datetime = effective_created_at
        self._updated_at: datetime = effective_updated_at

    @classmethod
    def create(
        cls,
        payment_id: PaymentId,
        from_account_id: AccountId,
        amount: Money,
    ) -> Self:
        """Фабричный метод создания нового платежа в статусе PENDING."""
        return cls(
            payment_id=payment_id,
            from_account_id=from_account_id,
            amount=amount,
            status=PaymentStatus.PENDING,
            version=INITIAL_VERSION,
        )

    # --- Свойства чтения состояния ---

    @property
    def id(self) -> PaymentId:
        """Идентификатор платежа."""
        return self._id

    @property
    def from_account_id(self) -> AccountId:
        """Идентификатор счёта списания."""
        return self._from_account_id

    @property
    def amount(self) -> Money:
        """Сумма платежа."""
        return self._amount

    @property
    def currency(self) -> Currency:
        """Валюта платежа (производное от amount)."""
        return self._amount.currency

    @property
    def status(self) -> PaymentStatus:
        """Текущий статус платежа."""
        return self._status

    @property
    def version(self) -> int:
        """Версия оптимистичной блокировки."""
        return self._version

    @property
    def persisted_version(self) -> int:
        """Номер версии, о котором платёж знает, что он лежит в базе.

        Оптимистичный ``UPDATE`` сверяется именно с ним, а не с ``version``:
        последний успел вырасти на каждом переходе статуса, а хранилище ждёт то
        значение, которое было прочитано.
        """
        return self._persisted_version

    @property
    def provider_payment_id(self) -> str | None:
        """Идентификатор платежа во внешней платёжной системе."""
        return self._provider_payment_id

    @property
    def failure_reason(self) -> str | None:
        """Причина ошибки (для статуса FAILED)."""
        return self._failure_reason

    @property
    def created_at(self) -> datetime:
        """Время создания (UTC)."""
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        """Время последнего изменения (UTC)."""
        return self._updated_at

    # --- Статус-машина переходов ---

    def transition_to(
        self,
        target_status: PaymentStatus,
        *,
        provider_payment_id: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Выполняет переход в целевой статус с валидацией бизнес-инвариантов.

        :param target_status: целевой статус жизненного цикла;
        :param provider_payment_id: идентификатор внешней транзакции (опционально);
        :param failure_reason: причина сбоя (заполняется при FAILED).
        :raises InvalidTransition: если переход недопустим правилами статус-машины.
        :raises InvalidValueError: если переданы некорректные аргументы перехода.
        """
        require_type(target_status, PaymentStatus, 'target_status')

        allowed = ALLOWED_TRANSITIONS.get(self._status, frozenset())
        if target_status not in allowed:
            raise InvalidTransition(
                f'Недопустимый переход платежа {self._id}: '
                f'из {self._status} в {target_status}. Разрешённые переходы: '
                f'{sorted(s.value for s in allowed) if allowed else "нет (терминальный статус)"}'
            )

        checked_provider_payment_id = require_optional_non_empty_str(provider_payment_id, 'provider_payment_id')
        checked_failure_reason = require_optional_non_empty_str(failure_reason, 'failure_reason')

        self._status = target_status
        if checked_provider_payment_id is not None:
            self._provider_payment_id = checked_provider_payment_id
        if checked_failure_reason is not None:
            self._failure_reason = checked_failure_reason

        self._updated_at = datetime.now(UTC)
        self._version += 1

    # --- Хелперы жизненного цикла ---

    def process(self, provider_payment_id: str) -> None:
        """Переводит платёж в статус обработки провайдером эквайринга."""
        self.transition_to(PaymentStatus.PROCESSING, provider_payment_id=provider_payment_id)

    def settle(self) -> None:
        """Успешно завершает платёж (SETTLED)."""
        self.transition_to(PaymentStatus.SETTLED)

    def fail(self, reason: str) -> None:
        """Переводит платёж в статус ошибки (FAILED) с указанием причины."""
        self.transition_to(PaymentStatus.FAILED, failure_reason=reason)

    def cancel(self) -> None:
        """Отменяет платёж из статуса PENDING (CANCELLED)."""
        self.transition_to(PaymentStatus.CANCELLED)

    # --- Синхронизация версии с хранилищем ---

    def mark_persisted(self) -> None:
        """Запомнить, что текущее состояние платежа дошло до хранилища.

        Без этого повторный ``update`` того же объекта сверялся бы со старой
        версией — и падал бы на оптимистичном условии, хотя гонки не было.
        """
        self._persisted_version = self._version

    # --- Тождество сущности ---

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Payment):
            return NotImplemented
        return self._id == other._id

    def __hash__(self) -> int:
        return hash(self._id)

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}(id={self._id!r}, from_account_id={self._from_account_id!r}, '
            f'amount={self._amount!r}, status={self._status}, version={self._version})'
        )
