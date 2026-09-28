"""Сценарий отмены платежа (T-2.6).

Ключевые инварианты:

* **Отмена только из PENDING.** Платёж в статусе ``PROCESSING`` уже передан внешнему
  провайдеру (или ждёт от него ответа), поэтому отменить его локально нельзя — иначе
  провайдер спишет средства, а мы будем считать операцию отменённой. Платёж в
  терминальных статусах (``SETTLED``, ``FAILED``, ``CANCELLED``) уже завершён и не
  может быть отменён повторно. Попытка отмены не-``PENDING`` платежа отвергается
  доменным исключением ``InvalidTransition``.
* **Возврат холда.** При создании платежа (T-2.4) сумма списывается со счёта
  (холд). При отмене средства обязаны вернуться на баланс того же счёта — это
  делает :func:`application.use_cases.hold.release_hold`, общее правило с отказом
  провайдера; здесь оно вызывается, а не переписывается.
* **Атомарность (UnitOfWork).** Перевод платежа в ``CANCELLED``, возвращение
  средств на счёт плательщика и запись доменного события ``PaymentCancelled`` в outbox
  происходят строго в одной транзакции. Если любая часть падает, откатывается всё.
* **Распределённая блокировка.** Операция захватывает распределённую блокировку
  счёта (``ACCOUNT_LOCK_RESOURCE_PREFIX``), предотвращая гонки с параллельными
  платежами или другими операциями по счёту.
* **Заблокированный счёт.** Если счёт заблокирован (``is_blocked=True``), возврат
  денег падает с ``AccountBlocked``, транзакция откатывается, платёж остаётся
  в текущем состоянии.
"""

from collections.abc import Callable

from src.core_service.application.dto.payment import CancelPaymentInput, CancelPaymentOutput
from src.core_service.application.ports.clock import Clock
from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.lock_manager import ACCOUNT_LOCK_RESOURCE_PREFIX, LockManager
from src.core_service.application.ports.unit_of_work import UnitOfWork
from src.core_service.application.use_cases.hold import release_hold
from src.core_service.application.use_cases.payment_lookup import read_payment
from src.core_service.domain.events.payment import PaymentCancelled
from src.core_service.domain.exceptions import EntityNotFoundError


class CancelPaymentUseCase:
    """Сценарий отмены PENDING платежа с возвратом зарезервированных средств."""

    __slots__ = ('_clock', '_event_publisher', '_lock_manager', '_uow_factory')

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        lock_manager: LockManager,
        event_publisher: EventPublisher,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._lock_manager = lock_manager
        self._event_publisher = event_publisher
        self._clock = clock

    async def execute(self, data: CancelPaymentInput) -> CancelPaymentOutput:
        """Отменяет платёж и возвращает средства на счёт плательщика.

        :param data: вход сценария (идентификатор отменяемого платежа и причина);
        :returns: результат отмены с актуальным статусом платежа;
        :raises EntityNotFoundError: платёж или счёт плательщика не найден;
        :raises InvalidTransition: платёж не находится в статусе PENDING;
        :raises AccountBlocked: счёт плательщика заблокирован;
        """
        # Короткое чтение вне блокировки: по платежу узнаём счёт, чей ресурс
        # блокировать. Транзакция здесь закрывается сразу — держать её ради одного
        # SELECT'а незачем.
        payment = await read_payment(self._uow_factory, data.payment_id)
        lock_resource = f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{payment.from_account_id}'

        async with (
            self._lock_manager.lock(lock_resource),
            self._uow_factory() as uow,
        ):
            # Перечитываем платёж: между чтением и блокировкой вебхок или сверка
            # могли закрыть платёж. Решение принимается по актуальному состоянию,
            # и переход проверяет его сам — повторная отмена или отмена
            # ``PROCESSING`` отвергаются доменной ``InvalidTransition``.
            fresh = await uow.payments.get(data.payment_id)
            if fresh is None:
                # Платёж исчез между чтением и записью: сбой хранилища или
                # конкурентное удаление. Отменять «по памяти» нельзя — вернём
                # деньги у платежа, которого нет.
                raise EntityNotFoundError(f'Платёж {data.payment_id} исчез при отмене')

            fresh.cancel()
            await release_hold(uow, fresh)
            await uow.payments.update(fresh)

            await self._event_publisher.publish(
                PaymentCancelled(
                    payment_id=fresh.id,
                    from_account_id=fresh.from_account_id,
                    amount=fresh.amount,
                    reason=data.reason,
                )
            )
            # Статус, возврат денег и событие обязаны попасть в БД вместе: событие
            # об отмене без возврата холда — это «призрак», на который потребитель
            # не найдёт ни баланса, ни платежа.
            await uow.commit()

            return CancelPaymentOutput.from_payment(fresh)
