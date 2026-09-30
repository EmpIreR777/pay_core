"""Таблица платежей (T-3.1).

Одна строка — один ``Payment``. Решения, которые стоит проговорить:

* **Валюта платежа не хранится.** Она приходит из ``Money`` и в домене, и в
  DTO, а в таблице её можно было бы вывести джойном по счёту. Дублировать
  значит завести второе место, где живёт валюта платежа: счёт однажды
  переведут в другую валюту, и две колонки разойдутся. Сумма при этом лежит
  рядом с ``from_account_id``, поэтому «валюта платежа» читается одним
  дополнительным джойном, а не рассинхронизированной копией.
* **Сумма строго положительна.** Нулевой платёж — не «бесплатный», а ошибка
  сценария: по нему нечего проводить, и его нечего отменять. ``Money`` ноль
  допускает (нулевой баланс счёта — законное состояние), поэтому запрет
  «строго больше нуля» живёт именно здесь, в схеме платежа.
* **Внешний ключ на счёт — ``ON DELETE RESTRICT``.** Каскадное удаление счёта
  стёрло бы историю платежей, по которой сверяются деньги; удалять счёт, у
  которого есть платежи, нельзя в принципе.
* **``provider_payment_id`` уникален там, где он есть.** Уникальность частичная
  по смыслу: ``NULL`` у ещё не отправленных провайдеру платежей повторяется
  законно, а два разных платежа с одним и тем же идентификатором операции —
  это всегда ошибка данных. Валидные значения задаёт FK на счёт плюс
  ``CHECK`` по ``ProviderStatus``: без него провайдерский статус в базу мог бы
  попасть любой строкой и «зависнуть» вне нашей шкалы.
* **Индекс ``(status, updated_at)`` — под выборку зависших платежей.**
  Сценарий сверки (T-2.5) ищет платежи в статусе, которые не менялись дольше
  порога: составной индекс отдаёт такой запрос из индекса целиком, а
  сканирование всей таблицы на каждом проходе сверки растиралось бы по мере
  роста ``payments``.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from src.core_service.domain.versioning import INITIAL_VERSION, MIN_VERSION
from src.db.models.base import (
    PAYMENT_STATUS_LENGTH,
    PROVIDER_PAYMENT_ID_LENGTH,
    PROVIDER_STATUS_LENGTH,
    Base,
    UpdatedAtMixin,
    enum_values_constraint,
    money_type,
)


class PaymentModel(Base, UpdatedAtMixin):
    """Платёж: счёт списания, сумма, статус, версия и привязка к провайдеру."""

    __tablename__ = 'payments'

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    from_account_id: Mapped[UUID] = mapped_column(
        ForeignKey('accounts.id', ondelete='RESTRICT'),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(money_type(), nullable=False)
    status: Mapped[str] = mapped_column(String(PAYMENT_STATUS_LENGTH), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(PROVIDER_PAYMENT_ID_LENGTH),
        nullable=True,
        unique=True,
    )
    provider_status: Mapped[str | None] = mapped_column(
        String(PROVIDER_STATUS_LENGTH),
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text(str(INITIAL_VERSION)))

    __table_args__ = (
        enum_values_constraint('status', PaymentStatus),
        # Провайдерский статус nullable: до вызова провайдера его не существует.
        enum_values_constraint('provider_status', ProviderStatus),
        CheckConstraint(column('amount') > 0, name='amount_positive'),
        CheckConstraint(column('version') >= MIN_VERSION, name='version_positive'),
        Index('ix_payments_from_account_id', 'from_account_id'),
        Index('ix_payments_status_updated_at', 'status', 'updated_at'),
    )
