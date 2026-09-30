"""Таблица обработанных событий для идемпотентности консьюмеров (T-3.1).

Нужна воркерам из ЭПИКА 9: Kafka гарантирует доставку «хотя бы раз», поэтому
одно и то же событие приходит к консьюмеру повторно при рестарте, ребалансировке
или ретрае. Повторная обработка платежа — это повторное движение денег, поэтому
консьюмер сначала спрашивает «это событие уже обработано?» и только потом
выполняет работу.

Ключевые решения:

* **Первичный ключ составной — ``(consumer, event_id)``, а не ``id`` счётчик.**
  Уникальность «это событие уже обработано **этим** консьюмером» — ровно то, что
  нужно проверить. Счётчик добавил бы отдельную колонку и уникальное
  ограничение поверх, чтобы выразить то же самое, а составной ключ проверяет
  это самой БД, без гонки между «проверил — вставил».
* **Имя события — текст, не UUID.** В Kafka событие приходит извне, и его
  идентификатор принадлежит внешней системе: наш доменный ``event_id`` у
  событий шлюза есть, но таблица должна пережить и чужой формат ключа.
* **Индекс на ``processed_at`` — под уборку.** Записи нужны только как
  доказательство «уже обработано» в пределах окна повторов, а не навсегда
  (политика хранения — задача ЭПИКА 14), поэтому их периодически удаляют, и
  удалять удобно по времени.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import CONSUMER_NAME_LENGTH, EVENT_ID_LENGTH, Base, CreatedAtMixin


class ProcessedEventModel(Base, CreatedAtMixin):
    """Отметка «событие уже обработано этим консьюмером»."""

    __tablename__ = 'processed_events'

    consumer: Mapped[str] = mapped_column(String(CONSUMER_NAME_LENGTH), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(EVENT_ID_LENGTH), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
