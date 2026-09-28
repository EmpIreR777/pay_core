"""Сценарий обработки вебхука провайдера (T-2.7).

Уведомление от шлюза — это **доставка факта**: провайдер сообщает, чем
закончилась операция, которую мы у него создали. Сценарий делает ровно три
вещи, и ни одну из них нельзя пропустить:

1. **Узнаёт наш платёж.** Во входящем уведомлении нет нашего ``payment_id`` —
   есть только ``provider_payment_id``, поэтому поиск идёт по нему
   (``PaymentRepository.get_by_provider_payment_id``);
2. **Применяет статус.** Перевод «шкала провайдера → статус платежа» (включая
   возврат холда при отказе) не пишется здесь заново: он живёт в
   :mod:`application.use_cases.payment_sync`, потому что тот же переход делают
   создание (T-2.4) и чтение (T-2.5). Своя копия правила — это ровно тот случай,
   когда одна из копий забудет вернуть деньги клиенту;
3. **Пишет событие в outbox** — в той же транзакции, что и смену статуса.
   Событие о платеже, которого в базе нет, — «призрак», на который потребитель
   не найдёт ни баланса, ни сверки.

Идемпотентность (DoD задачи) построена на ``provider_event_id`` и устроена так
же, как у создания платежа (T-2.4), — тем же портом ``IdempotencyStore``:

* **Повторная доставка** (провайдер обязан ретраить, пока не получит 200)
  находит сохранённый ответ и возвращает **его же**, не трогая ни платёж, ни
  баланс, ни outbox. Ответ, а не пересчёт состояния: идемпотентность — это
  «тот же ответ на тот же запрос», и сохранённый ответ не зависит от того, что
  успело случиться с платежом позже;
* **Параллельная доставка** того же события (провайдер повторил, пока первая
  ещё в работе) не пропускается в обработку: ``try_acquire`` (``SET NX``)
  оставляет работать только первую, остальные получают ``DuplicateOperation``;
* **Ошибка обработки освобождает ключ.** Если платёж не найден (например,
  уведомление обогнало наш собственный коммит) или база отказала, событие не
  считается обработанным: резервация снимается, и следующая доставка того же
  события проходит заново. Иначе один сбой навсегда «съел» бы уведомление.

Что сценарий осознанно **не** делает: не переводит платёж по статусу, которого
не знает его статус-машина, и не выдумывает исход, когда уведомление сообщает
промежуточное состояние (``PENDING``/``PROCESSING``) — это значит «ещё
обрабатывается», и записывать тут нечего. Позднее уведомление о уже закрытом
платеже тоже ничего не меняет: переход проверяет статус-машина, а ``SETTLED``
после ``SETTLED`` не переход.
"""

from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from typing import Final

from src.core_service.application.dto.payment import (
    HandleProviderWebhookInput,
    HandleProviderWebhookOutput,
)
from src.core_service.application.ports.clock import Clock
from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.idempotency_store import IdempotencyRecord, IdempotencyStore
from src.core_service.application.ports.unit_of_work import UnitOfWork
from src.core_service.application.use_cases.payment_sync import apply_provider_status
from src.core_service.domain.exceptions import DuplicateOperation, EntityNotFoundError

#: Сколько живёт резервация события, пока сценарий его обрабатывает. Верхняя
#: граница «хвоста» обработки (одна транзакция), взятая с запасом: резервация,
#: истёкшая посреди живой обработки, впустила бы вторую доставку того же события.
WEBHOOK_RESERVATION_TTL_SECONDS: Final[int] = 300

#: Сколько хранится отметка «событие обработано». Провайдеры ретраят уведомления
#: часами, поэтому сутки — окно, в котором повтор не повторит операцию.
WEBHOOK_RECORD_TTL_SECONDS: Final[int] = 86_400


def _event_fingerprint(data: HandleProviderWebhookInput) -> str:
    """Отпечаток содержимого события для хранилища идемпотентности.

    Нужен, чтобы отличить повтор от конфликта: если под одним и тем же
    ``provider_event_id`` пришло другое содержимое (операция или статус), это
    не повтор доставки, а нарушение контракта провайдером — и применять такое
    событие нельзя. Сам идентификатор события в отпечаток не входит: он и есть
    ключ, по которому идёт сравнение.
    """
    payload = f'{data.provider_payment_id}|{data.provider_status}'
    return sha256(payload.encode()).hexdigest()


class HandleProviderWebhookUseCase:
    """Применяет уведомление провайдера к платежу ровно один раз.

    Зависимости внедряет композиционный корень (ЭПИК 6/7). Сценарий не знает,
    откуда пришло уведомление и подписан ли вызов: проверка подписи и разбор
    «сырого» тела — задача транспортного адаптера (T-7.8) и адаптера провайдера.
    """

    __slots__ = ('_clock', '_event_publisher', '_idempotency_store', '_uow_factory')

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        idempotency_store: IdempotencyStore,
        event_publisher: EventPublisher,
        clock: Clock,
    ) -> None:
        """:param uow_factory: фабрика единиц работы; вся обработка уведомления —
            одна транзакция (статус, деньги и outbox фиксируются вместе);
        :param idempotency_store: отметки обработанных событий по ``provider_event_id``;
        :param event_publisher: outbox доменных событий;
        :param clock: время для меток записи идемпотентности. Перевод платежа
            ставит своё доменное время, подменять его снаружи нельзя.
        """
        self._uow_factory = uow_factory
        self._idempotency_store = idempotency_store
        self._event_publisher = event_publisher
        self._clock = clock

    async def execute(self, data: HandleProviderWebhookInput) -> HandleProviderWebhookOutput:
        """Обрабатывает уведомление, соблюдая идемпотентность по событию.

        :param data: вход сценария (событие, операция и её статус у провайдера);
        :returns: состояние платежа после уведомления; при повторной доставке
            того же события — ранее сохранённый ответ;
        :raises DuplicateOperation: событие уже обрабатывается параллельно либо
            тот же ``provider_event_id`` пришёл с другим содержимым;
        :raises EntityNotFoundError: платежа с такой операцией у провайдера нет.
            Событие при этом не помечается обработанным — доставка повторится.
        """
        fingerprint = _event_fingerprint(data)
        stored = await self._idempotency_store.get(data.provider_event_id)
        if stored is not None:
            return self._replay(data, fingerprint, stored)

        # Параллельная доставка того же события ещё не записала ответ. Пропустить
        # её в обработку нельзя — она применила бы переход второй раз.
        if not await self._idempotency_store.try_acquire(
            data.provider_event_id,
            ttl_seconds=WEBHOOK_RESERVATION_TTL_SECONDS,
        ):
            raise DuplicateOperation(f'Событие вебхука {data.provider_event_id} уже обрабатывается')

        try:
            output = await self._apply(data)
        except BaseException:
            # Платёж не изменён (транзакция откатилась): провайдер вправе
            # повторить доставку с тем же идентификатором события, и тогда
            # обработка дойдёт до конца.
            await self._idempotency_store.release(data.provider_event_id)
            raise

        await self._remember(data, fingerprint, output)
        return output

    # --- Шаг 1: повторная доставка ----------------------------------------------

    def _replay(
        self,
        data: HandleProviderWebhookInput,
        fingerprint: str,
        stored: IdempotencyRecord,
    ) -> HandleProviderWebhookOutput:
        """Возвращает сохранённый ответ по уже обработанному событию.

        :raises DuplicateOperation: под ключом идёт обработка без ответа либо
            ключ использован для другого содержимого (конфликт, а не повтор).
        """
        if stored.request_hash != fingerprint:
            raise DuplicateOperation(f'Событие вебхука {data.provider_event_id} уже обработано с другим содержимым')
        if stored.response is None:
            raise DuplicateOperation(f'Событие вебхука {data.provider_event_id} ещё обрабатывается')
        return HandleProviderWebhookOutput.from_idempotency_response(stored.response)

    async def _remember(
        self,
        data: HandleProviderWebhookInput,
        fingerprint: str,
        output: HandleProviderWebhookOutput,
    ) -> None:
        """Помечает событие обработанным, сохраняя ответ для повторов."""
        now = self._clock.now()
        await self._idempotency_store.save(
            IdempotencyRecord(
                key=data.provider_event_id,
                request_hash=fingerprint,
                response=output.to_idempotency_response(),
                created_at=now,
                expires_at=now + timedelta(seconds=WEBHOOK_RECORD_TTL_SECONDS),
            ),
        )

    # --- Шаг 2: поиск платежа, применение статуса и outbox ----------------------

    async def _apply(self, data: HandleProviderWebhookInput) -> HandleProviderWebhookOutput:
        """Применяет уведомление к платежу в одной транзакции.

        Смена статуса, движение денег (возврат холда при отказе) и запись событий
        обязаны попасть в БД вместе: иначе возможен «призрак» — отказ без
        возврата денег или событие о платеже, которого нет.

        Если применять нечего (провайдер ещё обрабатывает, платёж уже закрыт или
        статус не изменил состояния), транзакцию не коммитим: лишний коммит
        создал бы новую версию строки без изменения, а ответ отдаётся по
        фактическому состоянию платежа.

        :raises EntityNotFoundError: платёж с таким ``provider_payment_id`` не
            найден.
        """
        async with self._uow_factory() as uow:
            payment = await uow.payments.get_by_provider_payment_id(data.provider_payment_id)
            if payment is None:
                raise EntityNotFoundError(
                    f'Платёж с идентификатором провайдера {data.provider_payment_id} не найден',
                )

            events = await apply_provider_status(uow, payment, data.provider_status, self._event_publisher)
            if events:
                await uow.payments.update(payment)
                for event in events:
                    await self._event_publisher.publish(event)
                await uow.commit()

            return HandleProviderWebhookOutput.from_payment(payment)
