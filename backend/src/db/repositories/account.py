"""Репозиторий платёжных счетов на Postgres + async SQLAlchemy (T-3.3).

Адаптер порта ``AccountRepository``: единственное место, где доменный ``Account``
встречается со строкой ``accounts``. Идентификатор-обёртка ``AccountId``
превращается в UUID, ``Money`` — в пару ``balance``/``currency``. Обратное
преобразование обязано быть **полным**: из строки собирается ровно тот счёт,
который сценарий до этого читал, иначе «прочитали — изменили — сохранили» тихо
теряет валюту или флаг блокировки.

Решения, которые стоит проговорить:

* **Репозиторий не коммитит.** Транзакцией владеет ``UnitOfWork`` (T-3.5): сага
  держит транзакцию открытой через вызов провайдера и коммитит в два шага. Здесь
  принимается уже открытая ``AsyncSession``; методы ограничены ``flush`` — данные
  уходят в базу внутри чужой транзакции, но не фиксируются. Репозиторий, который
  коммитит сам, разорвал бы атомарность «списание холда + запись платежа», а
  открывающий свою транзакцию отдавал бы сценарию чужой ``session``.
* **Явный ``flush``, а не тишина.** Сессия в проекте создаётся с
  ``autoflush=False``, поэтому без ``flush`` ``INSERT``/``UPDATE`` уехал бы в
  момент коммита ``UnitOfWork``, и ошибка ограничения (дубликат ключа, код валюты
  не из шкалы) прилетела бы на границе, где её никто не ждёт. ``flush`` ничего не
  фиксирует, зато приводит отказ к месту, где он возник по делу.
* **``get_for_update`` — блокировка строки, а не «счёт занят».** Счёт читают перед
  тем, как решить, можно ли с него списать. Второй такой сценарий обязан встать в
  очередь, а не получить молчание и потерять деньги. Блокировка держится до конца
  транзакции — ровно столько, сколько нужно, чтобы решение, принятое по
  прочитанному балансу, было записано.
* **Версия пишется, но не проверяется.** ``update`` сохраняет то ``version``,
  которое выставил домен. Проверка «а не изменилась ли строка с тех пор, как её
  прочитали» (``UPDATE ... WHERE version = :current``) — задача T-3.6; в T-3.3 она
  была бы преждевременной, потому что блокировка строки уже закрывает гонку
  внутри одной транзакции, а оптимистичный замок нужен там, где её не берут.
* **Валюта при обновлении не пишется.** Она задаётся балансом при создании и
  доменом не меняется, поэтому в ``UPDATE`` баланса ей не место: колонка
  «обновилась бы сама в себя» только раздувает запрос, идущий на каждый платёж.
"""

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import EntityNotFoundError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money
from src.db.models.account import AccountModel


def to_domain_account(model: AccountModel) -> Account:
    """Собрать доменный счёт из строки ``accounts``.

    ``Money`` приводит сумму к точности валюты, поэтому валюта без минорной
    единицы (иены: ``NUMERIC`` вернёт ``100.000``) читается как ``100``, а не как
    дробь. Это не потеря точности, а нормализация: колонка одна на все валюты, и
    различать их должна валюта, а не число знаков после запятой в колонке.
    """
    return Account(
        account_id=AccountId(model.id),
        balance=Money(amount=model.balance, currency=Currency(model.currency)),
        is_blocked=model.is_blocked,
        version=model.version,
    )


def new_account_model(account: Account) -> AccountModel:
    """Новая строка ``accounts`` по значениям доменного счёта.

    Время (``created_at``/``updated_at``) не задаётся: его проставляет сама база
    через ``server_default``, и метка должна появиться даже у строки, записанной
    мимо репозитория (импорт данных, ручной ``INSERT``, скрипт сверки).
    """
    return AccountModel(
        id=account.id.value,
        currency=account.currency.value,
        balance=account.balance.amount,
        is_blocked=account.is_blocked,
        version=account.version,
    )


class PostgresAccountRepository:
    """Хранилище счетов в Postgres: реализация порта ``AccountRepository``."""

    def __init__(self, session: AsyncSession) -> None:
        """:param session: сессия уже открытой транзакции ``UnitOfWork``."""
        self._session = session

    async def get(self, account_id: AccountId) -> Account | None:
        """Вернуть счёт по идентификатору или ``None``, если записи нет.

        Чтение без блокировки: годно там, где по балансу дальше ничего не
        меняется (показ счёта, сбор истории).
        """
        model = (await self._session.execute(self._select(account_id))).scalar_one_or_none()
        if model is None:
            return None
        return to_domain_account(model)

    async def get_for_update(self, account_id: AccountId) -> Account | None:
        """Вернуть счёт, заблокировав его строку до конца транзакции.

        Единственный способ увидеть баланс и записать новый так, чтобы две
        одновременные операции не потеряли одну из них: второй ждёт освобождения
        строки и читает уже новое значение.
        """
        statement = self._select(account_id).with_for_update()
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None
        return to_domain_account(model)

    async def add(self, account: Account) -> None:
        """Добавить новый счёт.

        :raises IntegrityError: счёт с таким идентификатором уже есть. Дубликат
            отсекается первичным ключом самой базой; вторая проверка в коде была бы
            копией того же правила, которая рано или поздно с базой разошлась бы.
        """
        self._session.add(new_account_model(account))
        await self._session.flush()

    async def update(self, account: Account) -> None:
        """Сохранить изменения существующего счёта.

        Обновляются баланс, признак блокировки и версия — то, чем домен владеет
        безусловно. Валюта не пишется: она неизменна по инварианту ``Account``
        (задаётся балансом при создании), и переписывать её в том же ``UPDATE``
        незачем.

        Строка читается заново, а не берётся из identity map сессии: map держит
        модели по **слабым** ссылкам, и ``to_domain_account`` модель отпускает сразу
        (наружу ушёл доменный объект), так что к моменту ``update`` кеш пуст и
        ``session.get`` делает ``SELECT``. Лишнего обмена нет по смыслу — блокировка
        строки держится на уровне БД до конца транзакции независимо от кеша, — но
        он исчезнет сам в T-3.6, где ``update`` станет условным
        ``UPDATE ... WHERE version = :current`` и перестанет читать строку вовсе.
        Сейчас этот обмен платится за то, что ``update`` без предшествующего
        ``get_for_update`` не защищён от гонки; закрывает это тоже T-3.6.

        :raises EntityNotFoundError: строки нет. Молчаливый выход здесь означал бы
            потерянное движение денег: сценарий посчитал, что пополнил счёт, а база
            об этом не узнала.
        """
        model = await self._session.get(AccountModel, account.id.value)
        if model is None:
            raise EntityNotFoundError(f'Счёт {account.id} не найден: сохранять нечего')
        model.balance = account.balance.amount
        model.is_blocked = account.is_blocked
        model.version = account.version
        await self._session.flush()

    def _select(self, account_id: AccountId) -> Select[tuple[AccountModel]]:
        """Запрос чтения одного счёта.

        Отдельный метод, а не повторённый в ``get``/``get_for_update`` код: у них
        различается ровно одно — блокировка, — и различие обязано быть видно
        целиком, а не потеряться среди повторов.
        """
        return select(AccountModel).where(AccountModel.id == account_id.value)
