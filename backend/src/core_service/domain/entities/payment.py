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
from typing import Final, Self

from src.core_service.domain.exceptions import (
    InvalidAmountError,
    InvalidIdentifierError,
    InvalidTransition,
    InvalidValueError,
)
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import (
    ALLOWED_TRANSITIONS,
    PaymentStatus,
)

#: Начальная версия оптимистичной блокировки (согласована с SQLAlchemy version_id_col).
INITIAL_VERSION: Final = 1

#: Минимально допустимая версия записи платежа.
MIN_VERSION: Final = 1


def _require_utc(dt: datetime, field_name: str) -> datetime:
    """Проверяет, что временная метка является timezone-aware в UTC."""
    if not isinstance(dt, datetime):
        raise InvalidValueError(f'{field_name}: ожидается datetime, получено {type(dt).__name__}')
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise InvalidValueError(f'{field_name}: datetime должен быть timezone-aware (UTC)')
    return dt


class Payment:
    """Сущность платежа со строгой статус-машиной и контролем версий."""

    __slots__ = (
        '_amount',
        '_created_at',
        '_failure_reason',
        '_from_account_id',
        '_id',
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
        if not isinstance(payment_id, PaymentId):
            raise InvalidIdentifierError(f'payment_id должен быть PaymentId, получено {type(payment_id).__name__}')
        if not isinstance(from_account_id, AccountId):
            raise InvalidIdentifierError(
                f'from_account_id должен быть AccountId, получено {type(from_account_id).__name__}'
            )
        if not isinstance(amount, Money):
            raise InvalidValueError(f'amount должен быть Money, получено {type(amount).__name__}')
        if amount.is_zero:
            raise InvalidAmountError('Сумма платежа не может быть нулевой')

        if not isinstance(status, PaymentStatus):
            raise InvalidValueError(f'status должен быть PaymentStatus, получено {type(status).__name__}')

        if not isinstance(version, int) or isinstance(version, bool):
            raise InvalidValueError(f'version должен быть int, получено {type(version).__name__}')
        if version < MIN_VERSION:
            raise InvalidValueError(f'version не может быть меньше {MIN_VERSION}, передано {version}')

        if provider_payment_id is not None and (
            not isinstance(provider_payment_id, str) or not provider_payment_id.strip()
        ):
            raise InvalidValueError('provider_payment_id должен быть непустой строкой или None')

        if failure_reason is not None and (not isinstance(failure_reason, str) or not failure_reason.strip()):
            raise InvalidValueError('failure_reason должен быть непустой строкой или None')

        now = datetime.now(UTC)
        effective_created_at = _require_utc(created_at, 'created_at') if created_at is not None else now
        effective_updated_at = (
            _require_utc(updated_at, 'updated_at') if updated_at is not None else effective_created_at
        )

        if effective_updated_at < effective_created_at:
            raise InvalidValueError('updated_at не может предшествовать created_at')

        self._id: PaymentId = payment_id
        self._from_account_id: AccountId = from_account_id
        self._amount: Money = amount
        self._status: PaymentStatus = status
        self._version: int = version
        self._provider_payment_id: str | None = provider_payment_id.strip() if provider_payment_id else None
        self._failure_reason: str | None = failure_reason.strip() if failure_reason else None
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
        if not isinstance(target_status, PaymentStatus):
            raise InvalidValueError(f'target_status должен быть PaymentStatus, получено {type(target_status).__name__}')

        allowed = ALLOWED_TRANSITIONS.get(self._status, frozenset())
        if target_status not in allowed:
            raise InvalidTransition(
                f'Недопустимый переход платежа {self._id}: '
                f'из {self._status} в {target_status}. Разрешённые переходы: '
                f'{sorted(s.value for s in allowed) if allowed else "нет (терминальный статус)"}'
            )

        if provider_payment_id is not None and (
            not isinstance(provider_payment_id, str) or not provider_payment_id.strip()
        ):
            raise InvalidValueError('provider_payment_id должен быть непустой строкой или None')

        if failure_reason is not None and (not isinstance(failure_reason, str) or not failure_reason.strip()):
            raise InvalidValueError('failure_reason должен быть непустой строкой или None')

        self._status = target_status
        if provider_payment_id is not None:
            self._provider_payment_id = provider_payment_id.strip()
        if failure_reason is not None:
            self._failure_reason = failure_reason.strip()

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
