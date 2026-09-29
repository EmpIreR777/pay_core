"""Таблица принятых вебхуков провайдера (T-3.1).

Журнал входящих уведомлений: по нему видно, что приходило от шлюза, даже если
сценарий обработки (T-2.7) упал или уведомление было подозрительным.

Ключевые решения:

* **Первичный ключ — ``provider_event_id``, а не счётчик.** Идентификатор
  события у провайдера уникален в пределах провайдера, и именно он служит
  ключом идемпотентности в T-2.7. Сделать его же ключом таблицы значит, что
  повторная доставка того же уведомления отсекается самой БД: сценарий
  попадёт в ``IntegrityError`` даже раньше, чем дойдёт до ``IdempotencyStore``.
* **Сырое тело сохраняется целиком.** Провайдеры расширяют формат, и
  «разобрали и отбросили неизвестные поля» означает, что через месяц разобрать
  уже нечего. Полезная нагрузка хранится как пришла — разбор это забота
  адаптера.
* **Статус провайдера — из ``ProviderStatus`` через ``CHECK``.** В журнале
  лежит ровно то, что прислал шлюз; ограничение гарантирует, что это значение
  из нашей шкалы, а не произвольная строка.
* **Индекс по времени приёма — под разбор инцидентов и уборку.** Журнал
  растёт с каждым уведомлением, и «что приходило вечером 14-го, когда платежи
  встали» — типовой вопрос расследования, на который нужен доступ по времени,
  а не только по идентификатору.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.db.models.base import (
    PROVIDER_PAYMENT_ID_LENGTH,
    PROVIDER_STATUS_LENGTH,
    Base,
    CreatedAtMixin,
    enum_values_constraint,
)


class ProviderWebhookEventModel(Base, CreatedAtMixin):
    """Запись о вебхуке, полученном от платёжного провайдера."""

    __tablename__ = 'provider_webhook_events'

    provider_event_id: Mapped[str] = mapped_column(String(MAX_IDEMPOTENCY_KEY_LENGTH), primary_key=True)
    provider_payment_id: Mapped[str] = mapped_column(String(PROVIDER_PAYMENT_ID_LENGTH), nullable=False)
    provider_status: Mapped[str] = mapped_column(String(PROVIDER_STATUS_LENGTH), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        enum_values_constraint('provider_status', ProviderStatus),
        # Уведомление без идентификатора операции сопоставить с платежом нечем:
        # в нём нет нашего payment_id, а провайдерский статус без ссылки на
        # операцию — просто текст. Такое уведомление бессмысленно хранить.
        CheckConstraint(column('provider_payment_id') != '', name='provider_payment_id_not_empty'),
    )
