"""Таблица outbox — Transactional Outbox (T-3.1, схема; relay — T-8.1).

Запись появляется в той же транзакции, что и изменение бизнес-данных, поэтому
«событие есть, а факта нет» и «факт есть, а события нет» невозможны.

Ключевые решения:

* **Первичный ключ — ``id`` доменного события, а не счётчик.** ``event_id`` в
  :class:`~src.core_service.domain.events.base.DomainEvent` уникален, и
  повторная запись того же события падает на ``PRIMARY KEY``. Это дешёвый
  последний рубеж идемпотентности: если ретрай сценария всё же дойдёт до
  вставки, дубль не создаст второе событие в Kafka.
* **Полезная нагрузка — ``JSONB``.** События сериализуются в JSON для Kafka, и
  ``JSONB`` хранит их без повторного разбора текста на стороне Postgres:
  индексировать поля и фильтровать по ним можно прямо в БД.
* **Индекс только на неопубликованные записи.** Relay из T-8.1 опрашивает
  таблицу постоянно, а опубликованные записи ему не нужны никогда. Обычный
  индекс на ``created_at`` рос бы вместе с таблицей, а вместо него в
  PostgreSQL есть частичный: он индексирует только ``published_at IS NULL``,
  то есть ровно ту работу, которую делает relay. Размер индекса держится на
  размере очереди, а не на размере истории.
* **``published_at`` вместо булева ``published``.** Отметка времени отвечает на
  вопрос «когда событие ушло», из которого напрямую считается лаг outbox для
  дашборда (T-12.8). Булев флаг давал бы «да/нет» и требовал бы отдельной
  таблицы истории ради одного числа.
* **Повторные попытки.** ``attempts`` растёт при каждой неудачной публикации:
  relay из T-8.1 по нему отличает «событие уходит третий раз» от «уходит
  впервые», а ``last_error`` хранит причину последней неудачи, чтобы ретрай
  не был слепым.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import EVENT_TYPE_LENGTH, Base, CreatedAtMixin


class OutboxMessageModel(Base, CreatedAtMixin):
    """Событие, ожидающее публикации в брокер."""

    __tablename__ = 'outbox'

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(EVENT_TYPE_LENGTH), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(EVENT_TYPE_LENGTH), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('0'))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index('ix_outbox_unpublished', 'created_at', postgresql_where=text('published_at IS NULL')),)
