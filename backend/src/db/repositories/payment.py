"""Репозиторий платежей на Postgres + async SQLAlchemy (T-3.4).

Адаптер порта ``PaymentRepository``: единственное место, где доменный ``Payment``
встречается со строкой ``payments``. Здесь принимаются решения, которых в схеме
не видно, и каждое из них касается денег, а не удобства кода.

* **Валюта платежа приходит из счёта, а не из своей колонки.** В таблице её нет
  сознательно (T-3.1): копия валюты рядом с суммой однажды разошлась бы со
  счётом, и платёж оказался бы посчитан не в той денежной единице. Поэтому каждое
  чтение — джойн с ``accounts``, а маппер принимает валюту отдельным аргументом.
  Джойн внутренний, и потери строк он не даёт: внешний ключ ``ON DELETE
  RESTRICT`` не оставляет платежу без счёта.
* **Репозиторий не коммитит.** Транзакцией владеет ``UnitOfWork`` (T-3.5): сага
  держит транзакцию открытой через вызов провайдера и коммитит в два шага. Здесь
  принимается уже открытая ``AsyncSession``, а методы ограничены ``flush``.
* **Метки времени принадлежат базе.** ``created_at``/``updated_at`` проставляет
  сама Postgres (``server_default``/``onupdate``), а не Python: строка может
  появиться в обход ORM (импорт, ручной ``INSERT``, оптимистичный ``UPDATE`` из
  T-3.6), и метка должна быть у неё в любом случае.
* **Сумма и счёт платежа при обновлении не пишутся.** Они неизменны по инварианту
  ``Payment``: переписать их в том же ``UPDATE`` — значит потребовать от базы
  согласия с чужим идентификатором счёта. Пишутся смысловые поля: статус,
  идентификатор операции у провайдера, причина отказа, версия.
* **``provider_status`` не пишется вообще.** Сырого статуса на шкале провайдера
  в доменном ``Payment`` нет: он переводится в ``PaymentStatus`` прикладным слоем
  (T-2.5), и переводчику понадобились бы две разные шкалы в одной строке. Исходный
  ответ провайдера хранится там, где он нужен как сырой, — в
  ``provider_webhook_events``. Выдумывать значение, которого у домена нет,
  репозиторий не станет.
* **Вход ``find_by_status`` проверяется до запроса.** Предел и метка времени уходят
  прямо в SQL, где неверное значение не всегда заметно: Postgres отвергает
  отрицательный ``LIMIT``, а наивный ``datetime`` в сравнении с ``timestamptz``
  молча трактуется в зоне сервера, и «зависшие» платежи отбирались бы не из
  того окна. Проверки взяты у владельцев правил (``domain/validation.py``).
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Row, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core_service.application.ports.payment_repository import DEFAULT_FIND_BY_STATUS_LIMIT
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import EntityNotFoundError
from src.core_service.domain.validation import require_min_int, require_utc
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus, require_provider_payment_coherence
from src.db.models.account import AccountModel
from src.db.models.payment import PaymentModel


def to_domain_payment(model: PaymentModel, currency: Currency) -> Payment:
    """Собрать доменный платёж из строки ``payments`` и валюты его счёта.

    Валюта приходит отдельным аргументом именно потому, что её нет в строке
    платежа: она принадлежит счёту, и репозиторий достаёт её джойном. Подпись
    «модель + валюта» делает это требование видимым: маппер, который забыл бы
    про валюту, не собрался бы, а молча отдал бы платёж в чужой денежной единице.

    ``Money`` приводит сумму к точности валюты, поэтому валюта без минорной
    единицы (иена: ``NUMERIC`` вернёт ``100.000``) читается как ``100``, а не как
    дробь. Это нормализация, а не потеря: колонка одна на все валюты, и
    различать их должна валюта, а не число знаков после запятой в колонке.

    :raises InvalidValueError: в строке нет меток времени либо статус не
        согласован с идентификатором операции у провайдера. Мусор в базе обязан
        быть виден, а не превращён в правдоподобный платёж: по выдуманному
        времени «зависший» платёж либо не найдётся сверкой, либо найдётся
        навсегда.
    """
    status = PaymentStatus(model.status)
    require_provider_payment_coherence(status, model.provider_payment_id)
    return Payment(
        payment_id=PaymentId(model.id),
        from_account_id=AccountId(model.from_account_id),
        amount=Money(amount=model.amount, currency=currency),
        status=status,
        version=model.version,
        provider_payment_id=model.provider_payment_id,
        failure_reason=model.failure_reason,
        created_at=require_utc(model.created_at, 'created_at'),
        updated_at=require_utc(model.updated_at, 'updated_at'),
    )


def new_payment_model(payment: Payment) -> PaymentModel:
    """Новая строка ``payments`` по значениям доменного платежа.

    Валюты здесь нет — она принадлежит счёту, — как и времени: ``created_at`` и
    ``updated_at`` проставляет база, чтобы метка была и у строки, записанной мимо
    репозитория.
    """
    return PaymentModel(
        id=payment.id.value,
        from_account_id=payment.from_account_id.value,
        amount=payment.amount.amount,
        status=payment.status.value,
        provider_payment_id=payment.provider_payment_id,
        failure_reason=payment.failure_reason,
        version=payment.version,
    )


class PostgresPaymentRepository:
    """Хранилище платежей в Postgres: реализация порта ``PaymentRepository``."""

    def __init__(self, session: AsyncSession) -> None:
        """:param session: сессия уже открытой транзакции ``UnitOfWork``: границу
        коммита держит единица работы, а не репозиторий.
        """
        self._session = session

    def _select(self) -> Select[tuple[PaymentModel, str]]:
        """Запрос платежа вместе с валютой его счёта.

        Отдельный метод, а не повторённый в трёх местах код: правило «валюта
        платежа читается джойном с ``accounts``» обязано быть видно целиком.
        Взять её из строки платежа нельзя — такой колонки просто нет.
        """
        return select(PaymentModel, AccountModel.currency).join(
            AccountModel,
            PaymentModel.from_account_id == AccountModel.id,
        )

    async def get(self, payment_id: PaymentId) -> Payment | None:
        """Вернуть платёж по идентификатору или ``None``, если записи нет.

        Чтение без блокировки: статус платежа меняют переходы статус-машины под
        защитой оптимистичного лока (T-3.6), а держать строку на каждом чтении —
        значит платить ожиданием чужой транзакции там, где её не ждут.
        """
        return await self._one(self._select().where(PaymentModel.id == payment_id.value))

    async def get_by_provider_payment_id(self, provider_payment_id: str) -> Payment | None:
        """Вернуть платёж по идентификатору операции у провайдера.

        Единственный путь обработчика вебхука: во входящем уведомлении нашего
        ``payment_id`` нет, есть только идентификатор операции снаружи.

        Уникальность обеспечивает сама база, поэтому «больше одной строки» —
        нарушение схемы, а не вариант ответа: такое состояние обязано упасть,
        а не молча выбрать одну из строк.
        """
        return await self._one(
            self._select().where(PaymentModel.provider_payment_id == provider_payment_id),
        )

    async def add(self, payment: Payment) -> None:
        """Добавить новый платёж.

        :raises IntegrityError: платёж с таким идентификатором уже есть, счёт
            плательщика не найден, сумма неположительна или статус вне шкалы.
            Все эти правила принадлежат базе, и копия их в коде завела бы второе
            место, где они живут.
        """
        self._session.add(new_payment_model(payment))
        await self._session.flush()

    async def update(self, payment: Payment) -> None:
        """Сохранить изменения существующего платежа.

        Пишутся смысловые поля — статус, идентификатор операции у провайдера,
        причина отказа и версия, то, чем домен владеет безусловно. Сумма и счёт
        платежа не пишутся: они неизменны по инварианту ``Payment``, а
        ``from_account_id`` вдобавок обслуживает внешний ключ.

        ``updated_at`` после ``flush`` остаётся просроченным в объекте сессии:
        значение вычислила база, и ORM не знает его. Дополнительный ``refresh``
        здесь не нужен — проверено на живом Postgres, что следующий ``SELECT``
        перечитывает просроченную метку, а чтения репозитория всегда идут через
        ``SELECT``. Читать эту колонку из ORM-объекта **наружу** нельзя: такого
        чтения здесь нет, а лишний обмен на каждый переход статуса платил бы
        ничего. Вместе с лишним ``SELECT`` из ``session.get`` всё это исчезнет в
        T-3.6, где ``update`` станет условным ``UPDATE`` по номеру версии.

        :raises EntityNotFoundError: строки нет. Молчаливый выход означал бы
            потерянное движение денег: сценарий посчитал, что платёж у провайдера
            отклонён, а база об этом не узнала.
        """
        model = await self._session.get(PaymentModel, payment.id.value)
        if model is None:
            raise EntityNotFoundError(f'Платёж {payment.id} не найден в базе: сохранять нечего')
        model.status = payment.status.value
        model.provider_payment_id = payment.provider_payment_id
        model.failure_reason = payment.failure_reason
        model.version = payment.version
        await self._session.flush()

    async def find_by_status(
        self,
        status: PaymentStatus,
        *,
        limit: int = DEFAULT_FIND_BY_STATUS_LIMIT,
        updated_before: datetime | None = None,
    ) -> Sequence[Payment]:
        """Платежи в заданном статусе, от старых к новым.

        Порядок и фильтр по времени — это запрос сверки: она идёт по индексу
        ``ix_payments_status_updated_at`` и берёт платежи, которые дольше всего
        не менялись. Плата за стабильный порядок известна: пока строки не
        обновились, повторный вызов отдаёт ту же страницу — для порций, идущих
        по кругу, это и нужно.

        :param status: статус, в котором ищем платежи;
        :param limit: максимум платежей в ответе, всегда положительный;
        :param updated_before: отсечь платежи, менявшиеся не раньше этой метки.
        :raises InvalidValueError: предел не положителен либо метка не в UTC.
        """
        statement = self._select().where(PaymentModel.status == status.value).order_by(PaymentModel.updated_at)
        if updated_before is not None:
            statement = statement.where(PaymentModel.updated_at < require_utc(updated_before, 'updated_before'))
        rows = (await self._session.execute(statement.limit(require_min_int(limit, 'limit', minimum=1)))).all()
        return tuple(self._to_domain(row) for row in rows)

    async def _one(self, statement: Select[tuple[PaymentModel, str]]) -> Payment | None:
        """Выполнить запрос платежа и превратить строку в доменный объект.

        Отсутствующая строка — это ``None``, а не исключение: сценарий сам
        различает «платежа нет» (и отдаёт клиенту понятный отказ) и «платёж
        есть». Превращать нормальный ответ хранилища в ошибку нельзя.
        """
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else self._to_domain(row)

    def _to_domain(self, row: Row[tuple[PaymentModel, str]]) -> Payment:
        """Строка джойна ``(платёж, валюта счёта)`` → доменный платёж."""
        return to_domain_payment(row[0], Currency(row[1]))
