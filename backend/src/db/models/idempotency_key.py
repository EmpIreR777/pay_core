"""Таблица идемпотентных ключей (T-3.1, схема; наполнение — T-4.1/T-4.2).

Хранит однажды вычисленный ответ на запрос по ключу, чтобы повтор с тем же
ключом и тем же телом вернул тот же ответ, не выполняя логику второй раз.

Ключевые решения:

* **Ширина колонки ``key`` — из порта, а не из модели.**
  :data:`~src.core_service.application.ports.idempotency_store.MAX_IDEMPOTENCY_KEY_LENGTH`
  живёт в контракте хранилища именно потому, что это ширина колонки; модель
  импортирует константу, а не повторяет ``255`` своими буквами. Второе место с
  той же цифрой — это расхождение, которое обнаружилось бы уже на длинном
  ключе от клиента.
* **``request_hash`` — отпечаток тела запроса.** Он отличает «повтор» от
  «другого запроса с тем же ключом»: во втором случае вернуть сохранённый ответ
  означало бы отдать чужой результат, и сценарий поднимает
  ``DuplicateOperation``.
* **``response`` nullable.** Значение ``NULL`` означает «ключ занят, ответа
  ещё нет» — это состояние нужно различить с «ключа нет», иначе параллельная
  доставка прошла бы в обработку повторно. Сценарий T-2.7 так и трактует его.
* **Индекс на ``expires_at`` — под периодическую очистку.** Задача T-4.4
  удаляет просроченные записи по этому полю; без индекса каждая очистка читала
  бы всю таблицу целиком, а таблица растёт только от клиентских повторов.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core_service.application.ports.idempotency_store import MAX_IDEMPOTENCY_KEY_LENGTH
from src.db.models.base import SHA256_HEX_LENGTH, Base, CreatedAtMixin


class IdempotencyKeyModel(Base, CreatedAtMixin):
    """Сохранённый ответ на запрос по ключу идемпотентности."""

    __tablename__ = 'idempotency_keys'

    key: Mapped[str] = mapped_column(String(MAX_IDEMPOTENCY_KEY_LENGTH), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)
    response: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        # Срок жизни записи обязан быть положительным: иначе очистка удалила бы
        # ключ раньше, чем повтор сможет получить сохранённый ответ.
        CheckConstraint(column('expires_at') > column('created_at'), name='expires_after_created'),
    )
