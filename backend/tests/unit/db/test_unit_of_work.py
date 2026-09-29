"""Юнит-тесты единицы работы: стейт-машина транзакции без БД (T-3.5).

DoD задачи — «тест атомарности транзакции», и он закрыт интеграционным модулем
``tests/integration/test_unit_of_work.py``: там атомарность доказывается тем, что
видно из чужого соединения. Здесь проверяется то, чего не видно и с живой базы, —
**последовательность и условия** вызовов сессии.

Почему заглушка на ``AsyncSession``, а не мок: проверяется решение адаптера (когда
коммит, когда откат, что происходит при упавшем ``close``), а не поведение
SQLAlchemy. Сессия без ``bind`` — настоящий объект SQLAlchemy: она честно
выполнит ``begin``/``commit``/``rollback``/``close``, не выходя в сеть, поэтому
подменяется не библиотека, а база.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core_service.application import ports
from src.db.repositories import PostgresAccountRepository, PostgresPaymentRepository, PostgresUnitOfWork


class _ScenarioError(Exception):
    """Ошибка, которую поднимает сценарий внутри транзакции."""


class _FailingCloseSession(AsyncSession):
    """Сессия, у которой ``close`` падает: проверяется путь утечки соединения."""

    async def close(self) -> None:
        raise _ScenarioError('соединение не вернулось в пул')


class _RecordingSession(AsyncSession):
    """Сессия, записывающая порядок границ транзакции.

    Состояния ``in_transaction`` для этого недостаточно: и коммит, и откат
    оставляют сессию «не в транзакции», поэтому тест, смотрящий только на него,
    прошёл бы и при откате, которого нет вообще. Фиксируется именно **что**
    вызвано и в каком порядке — включая то, что ``close`` происходит последним.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append('commit')
        await super().commit()

    async def rollback(self) -> None:
        self.calls.append('rollback')
        await super().rollback()

    async def close(self) -> None:
        self.calls.append('close')
        await super().close()


class _SingleSessionMaker(async_sessionmaker[AsyncSession]):
    """Фабрика сессий, всегда выдающая одну и ту же заранее созданную сессию.

    Наследуется именно ``async_sessionmaker``, а не подменяется лямбдой: тип,
    который ждёт адаптер, тогда остаётся настоящим, и тест не ломается от
    изменения требований к фабрике. Сессия без ``bind`` — с полностью
    офлайновым поведением: ``begin``/``commit``/``rollback``/``close`` честно
    отрабатывают, не выходя в сеть, поэтому подменяется база, а не библиотека.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(bind=None)
        self._fixed = session

    def __call__(self, **local_kw: object) -> AsyncSession:
        # Параметры вызова игнорируются намеренно: адаптер зовёт фабрику без них,
        # а проверка «с какими аргументами» здесь была бы проверкой SQLAlchemy.
        del local_kw
        return self._fixed


def _uow_with(session: AsyncSession | None = None) -> tuple[PostgresUnitOfWork, AsyncSession]:
    """Единица работы и та самая сессия, на которой она будет работать."""
    created = session if session is not None else AsyncSession()
    return PostgresUnitOfWork(_SingleSessionMaker(created)), created


# --- Форма контракта ----------------------------------------------------------


def test_unit_of_work_satisfies_the_port() -> None:
    """Адаптер отвечает порту ``UnitOfWork`` «по форме» (T-2.1).

    Как и у репозиториев, порт **не** наследуется: наследование от ``Protocol``
    подмешивало бы заглушки и сделало бы проверку самоподтверждающейся.
    """
    uow, _ = _uow_with()

    assert isinstance(uow, ports.UnitOfWork)
    assert ports.UnitOfWork not in PostgresUnitOfWork.__mro__


# --- Открытие и доступ к репозиториям ----------------------------------------


async def test_aenter_returns_the_unit_of_work_itself() -> None:
    """``async with`` отдаёт саму единицу работы, а не голую сессию.

    Иначе сценарий писал бы ``async with uow_factory() as session:`` и потерял бы
    доступ к репозиториям — то есть к единственному, ради чего контекст и открывается.
    """
    uow, _ = _uow_with()

    async with uow as entered:
        assert entered is uow


async def test_repositories_are_available_inside_the_context() -> None:
    """Внутри контекста доступны оба репозитория поверх одной сессии.

    Проверяется именно то, что нужно сценарию: счёт и платёж обязаны писаться
    в одну транзакцию, иначе «списание холда + запись платежа» перестаёт быть
    атомарным (T-2.4).
    """
    uow, session = _uow_with()

    async with uow:
        assert isinstance(uow.accounts, PostgresAccountRepository)
        assert isinstance(uow.payments, PostgresPaymentRepository)
        assert uow.accounts._session is session
        assert uow.payments._session is session


async def test_aenter_opens_a_transaction_on_the_new_session() -> None:
    """Транзакция открывается явно в ``__aenter__``.

    Проверяется состоянием сессии, а не тем, что ``begin`` позвали: инвариант,
    ради которого он и написан, — «внутри контекста открыта транзакция», и он
    должен быть верен до первого запроса, а не «с момента, когда что-то спросили».
    """
    uow, session = _uow_with()

    async with uow:
        assert session.in_transaction()


async def test_second_enter_is_refused() -> None:
    """Вложенный вход в открытую единицу работы запрещён.

    Наивная реализация просто пересоздала бы сессию: внешний контекст продолжил бы
    писать в уже закрытую транзакцию, и сценарий узнал бы об этом уже на выходе,
    когда откатывать будет нечего.
    """
    uow, _ = _uow_with()

    async with uow:
        with pytest.raises(RuntimeError, match='уже открыта'):
            await uow.__aenter__()


@pytest.mark.parametrize('attribute', ['accounts', 'payments'])
async def test_repositories_are_unavailable_outside_the_context(attribute: str) -> None:
    """Вне контекста репозиторий недоступен с внятной причиной.

    Отказ в сценарии, потерявшем ``async with``, обязан быть громким: молчание
    означало бы, что запись ушла в базу мимо транзакции именно тогда, когда
    сценарий рассчитывал на откат.
    """
    uow, _ = _uow_with()

    with pytest.raises(RuntimeError, match='не открыта'):
        getattr(uow, attribute)


@pytest.mark.parametrize('method', ['commit', 'rollback'])
async def test_transaction_control_outside_the_context_is_refused(method: str) -> None:
    """``commit``/``rollback`` вне контекста тоже отвергаются.

    Иначе сценарий тихо «закоммитил бы» ничего и решил бы, что запись сохранена.
    """
    uow, _ = _uow_with()

    with pytest.raises(RuntimeError, match='не открыта'):
        await getattr(uow, method)()


# --- Фиксация и откат ---------------------------------------------------------


async def test_clean_exit_commits_and_closes() -> None:
    """Выход без исключения коммитит транзакцию и возвращает сессию в пул.

    Проверяется и порядок: ``close`` обязан быть последним. Если бы сессия
    закрывалась раньше фиксации, соединение ушло бы в пул с неотправленным
    ``COMMIT`` — и данные пропали бы вместе с транзакцией.
    """
    session = _RecordingSession()
    uow, _ = _uow_with(session)

    async with uow:
        pass

    assert session.calls == ['commit', 'close']
    assert not session.in_transaction()


async def test_exception_rolls_back_and_is_not_suppressed() -> None:
    """Исключение внутри контекста откатывает транзакцию и уходит наружу.

    Откат здесь — не «вежливость», а часть контракта: сага обязана увидеть отказ,
    снять резервацию ключа идемпотентности и вернуть холд (T-2.4, T-2.7).
    Подавление исключения сломало бы ровно это, поэтому проверяется и оно само.
    """
    session = _RecordingSession()
    uow, _ = _uow_with(session)

    with pytest.raises(_ScenarioError, match='сбой сценария'):
        async with uow:
            raise _ScenarioError('сбой сценария')

    assert session.calls == ['rollback', 'close']
    assert not session.in_transaction()


async def test_session_is_closed_even_when_close_itself_fails() -> None:
    """Упавший ``close`` не оставляет единицу работы «открытой».

    Утечка соединения — полбед-незаметная: пул исчерпывается постепенно, и первым
    признаком будет не отказ конкретного запроса, а общая деградация шлюза. Здесь
    проверяется вторая половина того же отказа: после неудачного закрытия
    состояние обязано быть снято, иначе следующий заход напишет в мёртвую сессию.
    """
    uow, _ = _uow_with(_FailingCloseSession())

    with pytest.raises(_ScenarioError, match='соединение не вернулось в пул'):
        async with uow:
            pass

    with pytest.raises(RuntimeError, match='не открыта'):
        _ = uow.accounts


async def test_unit_of_work_is_reusable_after_a_finished_transaction() -> None:
    """Законченная единица работы не остаётся «занятой».

    Сага открывает транзакции последовательно, а не вложенно (T-2.4), и фабрика
    может отдать тот же экземпляр повторно: забытое состояние после выхода
    заставило бы второй заход падать на ровном месте.
    """
    uow, _ = _uow_with()

    async with uow:
        pass
    async with uow:
        pass


async def test_explicit_rollback_inside_the_context_is_not_overridden_by_exit() -> None:
    """Явный ``rollback`` внутри контекста окончателен.

    Коммит на выходе не должен воскресить откатанное: иначе сценарий, откативший
    транзакцию по своей воле, получил бы в базу ровно то, что отменил.
    """
    uow, session = _uow_with()

    async with uow:
        await uow.rollback()

    assert not session.in_transaction()


async def test_explicit_commit_inside_the_context_is_allowed() -> None:
    """Явный ``commit`` внутри контекста — штатный путь сценария (T-2.4).

    Коммит на выходе после него фиксирует уже пустую транзакцию, и это не должно
    считаться ошибкой: запрещать сценарию решать, когда закрывать транзакцию, —
    значило бы отнять у него право, которым он пользуется на каждом шаге саги.
    """
    uow, session = _uow_with()

    async with uow:
        await uow.commit()

    assert not session.in_transaction()
