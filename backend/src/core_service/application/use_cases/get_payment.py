"""Сценарий чтения платежа с актуализацией у провайдера (T-2.5).

Задача сценария проще, чем кажется: вернуть платёж клиенту. Но есть одна
существенная часть — **актуализация**. Платёж в статусе ``PROCESSING`` означает
«мы отправили операцию провайдеру и ждём». Ждать могут долго, и читать такой
платёж как «вот он, PROCESSING» нельзя: клиент увидит вечно висящий платёж,
хотя у провайдера всё давно завершилось.

Поэтому при чтении «зависшего» платежа сценарий один раз спрашивает у провайдера
актуальный статус и, если тот изменился, фиксирует переход. Это ровно тот же
приём, что и сверка по расписанию (T-2.10), только срабатывающий по факту чтения.

Свойства флоу, на которые стоит обратить внимание:

* **Внешний вызов — вне транзакции.** Как и в T-2.4: чтение состояния у шлюза
  занимает сеть, а держать ради этого блокировки строк незачем. Схема: прочитать
  платёж → закрыть транзакцию → спросить провайдера → снова открыть транзакцию и
  записать результат;
* **Терминальный платёж не опрашивается.** У ``SETTLED``/``FAILED``/``CANCELLED``
  исход уже известен, поход в сеть был бы пустой тратой времени и денег;
* **Ответ применяется к актуальному состоянию.** Между чтением и ответом провайдера
  вебхок мог уже закрыть платёж. Поэтому переход применяется к тому, что лежит в
  базе сейчас, и сам проверяет, что переход ещё возможен;
* **Технический сбой не ломает чтение.** Провайдер недоступен — значит у клиента
  будет ошибка, но это честная ошибка, а не «платёж не найден».
"""

from collections.abc import Callable

from src.core_service.application.dto.payment import GetPaymentInput, GetPaymentOutput
from src.core_service.application.ports.clock import Clock
from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.payment_provider import PaymentProvider
from src.core_service.application.ports.unit_of_work import UnitOfWork
from src.core_service.application.use_cases.payment_sync import apply_provider_status, is_provider_status_actionable
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import EntityNotFoundError
from src.core_service.domain.value_objects.identifiers import PaymentId
from src.core_service.domain.value_objects.payment_status import PaymentStatus


class GetPaymentUseCase:
    """Возвращает платёж и, если он «завис», освежает его у провайдера."""

    __slots__ = ('_clock', '_event_publisher', '_payment_provider', '_uow_factory')

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        payment_provider: PaymentProvider,
        event_publisher: EventPublisher,
        clock: Clock,
    ) -> None:
        """:param uow_factory: фабрика единиц работы; сценарий открывает транзакцию
            дважды — на чтение и на запись результата актуализации;
        :param payment_provider: внешний эквайринг, у него спрашивается статус;
        :param event_publisher: outbox событий по итогам актуализации;
        :param clock: порт часов. В самом чтении он не нужен — метки проставляет
            платёж, — но держится ради единообразия с T-2.4 и для будущих
            сценариев чтения.
        """
        self._uow_factory = uow_factory
        self._payment_provider = payment_provider
        self._event_publisher = event_publisher
        self._clock = clock

    async def execute(self, data: GetPaymentInput) -> GetPaymentOutput:
        """Читает платёж, при необходимости актуализируя его у провайдера.

        :param data: вход сценария (идентификатор платежа);
        :returns: актуальное состояние платежа;
        :raises EntityNotFoundError: платежа с таким идентификатором нет;
        :raises PaymentProviderError: провайдер недоступен **и** платёж требовал
            актуализации. Терминальные платежи читаются всегда: их статус
            известен, обращаться к сети незачем.
        """
        payment = await self._read_payment(data.payment_id)
        provider_payment_id = self._provider_operation_id(payment)
        if provider_payment_id is None:
            return GetPaymentOutput.from_payment(payment)
        return await self._actualize(payment, provider_payment_id)

    async def _read_payment(self, payment_id: PaymentId) -> Payment:
        """Короткое чтение: открываем и сразу закрываем транзакцию.

        Отдельный метод нужен, чтобы транзакция гарантированно закрылась **до**
        сетевого вызова: если бы чтение и запись жили в одном ``async with``, сеть
        оказалась бы внутри транзакции, и мы вернулись бы к проблеме из T-2.4.
        """
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(payment_id)
            if payment is None:
                raise EntityNotFoundError(f'Платёж {payment_id} не найден')
            return payment

    def _provider_operation_id(self, payment: Payment) -> str | None:
        """Идентификатор операции у провайдера, если к платежу есть что спрашивать.

        ``None`` у терминальных платежей (исход уже известен, поход в сеть — пустая
        трата денег провайдера и задержка клиенту) и у ``PENDING``: до вызова
        провайдера идентификатора операции ещё не существует, спрашивать некого.
        """
        if payment.status is not PaymentStatus.PROCESSING:
            return None
        return payment.provider_payment_id

    async def _actualize(self, payment: Payment, provider_payment_id: str) -> GetPaymentOutput:
        """Спрашивает провайдера и фиксирует изменившийся статус.

        Вызов провайдера намеренно вне транзакции (см. :meth:`_read_payment`).
        """
        provider_status = await self._payment_provider.get_status(provider_payment_id)
        if not is_provider_status_actionable(payment.status, provider_status):
            # Провайдер ещё обрабатывает (PENDING/PROCESSING) либо платёж уже
            # терминальный. Записывать нечего — лишний коммит только создал бы
            # лишнюю версию строки.
            return GetPaymentOutput.from_payment(payment)

        async with self._uow_factory() as uow:
            fresh = await uow.payments.get(payment.id)
            if fresh is None:
                # Платёж исчез между чтением и записью: сбой хранилища или
                # конкурентное удаление. Отдать устаревшее состояние нельзя —
                # клиент решит, что платёж есть, а его нет.
                raise EntityNotFoundError(f'Платёж {payment.id} исчез при актуализации')

            events = await apply_provider_status(uow, fresh, provider_status, self._event_publisher)
            if not events:
                # Ответ провайдера терминальный, но к текущему состоянию уже
                # неприменим: вебхок успел закрыть платёж раньше нас. Запись
                # не нужна, и лишний коммит был бы враньём в версии строки.
                return GetPaymentOutput.from_payment(fresh)

            # Статус, provider_payment_id и запись в outbox обязаны попасть в БД
            # вместе: событие о платеже, которого в базе нет, — «призрак».
            await uow.payments.update(fresh)
            for event in events:
                await self._event_publisher.publish(event)
            await uow.commit()
            return GetPaymentOutput.from_payment(fresh)
