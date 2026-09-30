"""Юнит-тесты регистрации задачи уборки в брокере и расписании (T-4.4).

Часть DoD — «задача зарегистрирована». Ломается она тихо: задача, лишённая
ярлыка расписания, не падает, а просто никогда не запускается, и уборка
прекращается без единой записи в логах. Проверяется тем же путём, каким
расписание читает шедулер, а не «посмотрели ярлык задачи» — именно неудачная
форма ярлыка даёт пустое расписание при зарегистрированной задаче.

Проверки самого SQL уборки живут отдельно, в
``tests/unit/db/test_idempotency_cleanup.py``: уборка лежит в слое ``db`` и
тестируется вместе со своим слоем, а не по месту запуска.
"""

from src.tasks.broker import broker
from src.tasks.scheduler import scheduler
from src.tasks.tasks import IDEMPOTENCY_CLEANUP_CRON, cleanup_idempotency_keys

# --- Регистрация в расписании -------------------------------------------------


def test_cleanup_task_is_registered_in_the_broker() -> None:
    """Задача зарегистрирована в брокере, воркер её найдёт.

    Незарегистрированная задача не падает: её просто никто не вызовет, и
    уборка не случится без единой записи в логах. Проверяется фактом присутствия
    в реестре брокера — тем же, по которому задачу ищет воркер.
    """
    assert cleanup_idempotency_keys.task_name in broker.get_all_tasks()


async def test_cleanup_task_is_scheduled_hourly() -> None:
    """Задача попадает в расписание шедулера с часовым cron.

    Ключевое утверждение DoD. Расписание проверяется через тот же путь, каким
    его читает шедулер: источник поднимается и отдаёт свои расписания. Проверка
    ярлыка задачи «просто на всякий случай» ничего не доказала бы — именно
    неудачная форма ярлыка даёт пустое расписание при зарегистрированной задаче,
    и это не падает.

    Порядок вызовов повторяет CLI шедулера: источник поднимается отдельно от
    самого шедулера, иначе расписаний не будет.
    """
    for source in scheduler.sources:
        await source.startup()

    schedules = [task for source in scheduler.sources for task in await source.get_schedules()]

    matching = [task for task in schedules if task.task_name == cleanup_idempotency_keys.task_name]
    assert [task.cron for task in matching] == [IDEMPOTENCY_CLEANUP_CRON]


def test_cleanup_cron_is_a_valid_five_field_expression() -> None:
    """Cron уборки — пятиполевое выражение, а не опечатка.

    Кривое расписание не всегда означает отказ: ``0 * * * *`` — это тоже пять
    полей, но другое расписание, и задача молча поедет не туда. Здесь
    проверяется именно форма выражения, а не смысл конкретной минуты.
    """
    fields = IDEMPOTENCY_CLEANUP_CRON.split()

    assert len(fields) == 5
    assert all(field.strip() for field in fields)


def test_cleanup_cron_fires_every_hour() -> None:
    """Расписание срабатывает раз в час: минута зафиксирована, час — любой.

    Проверяется структурой выражения, а не сравнением с константой из той же
    строки — иначе тест повторял бы сам себя и зелёнел бы при любой правке.
    """
    minute, hour, day_of_month, month, day_of_week = IDEMPOTENCY_CLEANUP_CRON.split()

    assert (minute, hour) == ('0', '*')
    assert (day_of_month, month, day_of_week) == ('*', '*', '*')


def test_task_module_declares_only_the_cleanup_task() -> None:
    """Модуль объявляет ровно одну задачу — уборку.

    Две задачи, удаляющие просроченные ключи, делят одну таблицу, и одна из
    них со временем уберёт запись раньше срока. Отбираются настоящие объекты
    Taskiq, а не имена: модуль импортирует и саму функцию уборки, и такой же
    поиск по именам нашёл бы её и принял за вторую задачу. Список берётся из
    модуля, а не из реестра брокера: реестр общий на процесс и однажды
    накопит задачи следующих эпиков, и проверка стала бы проверкой не того.
    """
    from taskiq import AsyncTaskiqDecoratedTask

    from src.tasks import tasks as tasks_module

    declared = [name for name in dir(tasks_module) if isinstance(getattr(tasks_module, name), AsyncTaskiqDecoratedTask)]

    assert declared == ['cleanup_idempotency_keys']


async def test_scheduler_runs_on_the_shared_broker() -> None:
    """Шедулер и воркер работают через один и тот же брокер.

    Разные брокеры означали бы, что задача, поставленная шедулером, уехала бы
    в очередь, которую никто не читает: уборка не падала бы, а просто не
    происходила. Это утверждение об идентичности объектов, а не о равенстве
    описаний, поэтому сравниваются сами брокеры.
    """
    from src.tasks import tasks as tasks_module

    assert scheduler.broker is broker
    assert tasks_module.cleanup_idempotency_keys.broker is broker
