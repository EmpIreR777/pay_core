"""SQLAlchemy-модели схемы хранения платёжного шлюза (T-3.1).

Пакет собран как единая точка входа: ``src.db.models.Base`` — корень
декларативной модели с общим реестром таблиц, а реэкспорт всех классов
моделей нужен Alembic. ``env.py`` делает ``from src.db.models import Base`` и
получает метаданные уже со всеми таблицами, только если модели импортированы;
без ``__init__`` с реэкспортом ``autogenerate`` увидел бы пустую схему и
предложил бы миграцию, которая ничего не создаёт.

Состав таблиц и смысл каждой — в её модуле.
"""

from src.db.models.account import AccountModel
from src.db.models.base import (
    Base,
    CreatedAtMixin,
    UpdatedAtMixin,
    enum_values_constraint,
    money_type,
)
from src.db.models.idempotency_key import IdempotencyKeyModel
from src.db.models.outbox import OutboxMessageModel
from src.db.models.payment import PaymentModel
from src.db.models.processed_event import ProcessedEventModel
from src.db.models.provider_webhook_event import ProviderWebhookEventModel

__all__ = (
    'AccountModel',
    'Base',
    'CreatedAtMixin',
    'IdempotencyKeyModel',
    'OutboxMessageModel',
    'PaymentModel',
    'ProcessedEventModel',
    'ProviderWebhookEventModel',
    'UpdatedAtMixin',
    'enum_values_constraint',
    'money_type',
)
