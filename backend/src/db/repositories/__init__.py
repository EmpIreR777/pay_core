"""Репозитории на async SQLAlchemy (T-3.3, T-3.4).

Пакет собран как единая точка входа по той же причине, что и ``src.db.models``:
реализаций портов несколько (счета T-3.3, платежи T-3.4), и ``UnitOfWork``
(T-3.5) поднимает их все — точку подъёма удобно держать в одном месте, а не
разбрасывать по модулям агрегатов.

Конкретная причина для моделей была жёстче: ``alembic/env.py`` делает
``from src.db.models import Base``, и без реэкспорта ``autogenerate`` увидел бы
пустую схему. Здесь такой жёсткой зависимости нет, но единая точка входа всё равно
нужна: ``UnitOfWork`` и композиция приложения ссылаются на набор адаптеров, а не
на внутренности пакета. Реэкспортируется только то, чем этот слой владеет сам;
значения, которые по контракту принадлежат порту (``AccountRepository``,
``PaymentRepository``, предел выборки), берутся из
``src.core_service.application.ports`` и в пакет не дублируются.

Состав и смысл каждой реализации — в её модуле.
"""

from src.db.repositories.account import (
    PostgresAccountRepository,
    new_account_model,
    to_domain_account,
)
from src.db.repositories.payment import (
    PostgresPaymentRepository,
    new_payment_model,
    to_domain_payment,
)

__all__ = (
    'PostgresAccountRepository',
    'PostgresPaymentRepository',
    'new_account_model',
    'new_payment_model',
    'to_domain_account',
    'to_domain_payment',
)
