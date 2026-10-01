"""Шедулер периодических задач (T-4.4).

Один инстанс на развёртывание (AGENT.md §1). Шедулер не исполняет задачи сам —
он ставит их в очередь, а исполняет воркер: два разных процесса могут
подняться в разное время, и если бы уборку выполнял тот же процесс, что и
платит за неё, остановка шедулера означала бы ещё и остановку уборки.

Источник расписаний — :class:`LabelScheduleSource`: он читает ярлыки ``schedule``
с самих объявленных задач. Отдельный список расписаний в этом модуле был бы
вторым местом, где записано, когда запускать уборку, и разошёлся бы с задачей
при первом же изменении частоты.
"""

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from src.tasks.broker import broker

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
