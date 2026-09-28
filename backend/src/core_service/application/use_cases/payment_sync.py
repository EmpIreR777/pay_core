"""Применение ответа провайдера к платежу: единственное место с этим правилом (T-2.5).

Правило «ответ провайдера переводит платёж в статус, а бизнес-отказ ещё и
возвращает холд» одинаково для трёх сценариев: создания (T-2.4), чтения с
актуализацией (T-2.5) и вебхука (T-2.7). Если у каждого будет своя копия, то
одна из них рано или поздно забудет про возврат денег — и клиент потеряет сумму
на платёж, который провайдер отклонил. Поэтому правило живёт здесь, а сценарии
вызывают его.

Все изменения выполняются **внутри уже открытой транзакции** вызывающего: платёж,
события и движение денег обязаны зафиксироваться одним коммитом. Открывать и
коммитить транзакцию здесь нельзя — сценарий решает, когда она закрывается.
"""

from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.payment_provider import TERMINAL_PROVIDER_STATUSES, ProviderStatus
from src.core_service.application.ports.unit_of_work import UnitOfWork
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.events.payment import PaymentFailed, PaymentRefunded, PaymentSettled
from src.core_service.domain.exceptions import EntityNotFoundError
from src.core_service.domain.value_objects.payment_status import PaymentStatus

#: Причина отказа, которую сценарии пишут в платёж и в ``PaymentFailed``. Свой
#: текст провайдера сюда не попадает намеренно: разбор вокабуляра шлюза — дело
#: адаптера, а не ядра. Владелец константы здесь, потому что её используют все
#: сценарии, работающие с ответом провайдера.
PROVIDER_REJECTION_REASON: str = 'Платёж отклонён платёжным провайдером'


async def apply_provider_status(
    uow: UnitOfWork,
    payment: Payment,
    provider_status: ProviderStatus,
    event_publisher: EventPublisher,
) -> tuple[DomainEvent, ...]:
    """Переводит платёж из ``PROCESSING`` в статус по ответу провайдера.

    Платёж **не** сохраняется: вызывающий решает, когда коммитить, и обязан
    сделать это вместе с публикацией возвращённых событий.

    Промежуточные ответы (``PENDING``/``PROCESSING``) не меняют ничего: операция у
    провайдера ещё в работе, терминальный статус выдумывать рано.

    :param uow: открытая транзакция, в которой лежат репозитории;
    :param payment: платёж в статусе ``PROCESSING`` (или уже терминальный — тогда
        ответ игнорируется, см. :func:`is_provider_status_actionable`);
    :param provider_status: актуальный статус операции у провайдера;
    :param event_publisher: outbox для событий по итогам перехода;
    :returns: события, которые вызывающий обязан опубликовать в этой же транзакции;
    :raises EntityNotFoundError: счёт плательщика исчез — вернуть холд некуда, и
        это повод упасть, а не «списать в никуда».
    """
    if not is_provider_status_actionable(payment.status, provider_status):
        return ()

    if provider_status is ProviderStatus.FAILED:
        await _release_hold(uow, payment)
        payment.fail(PROVIDER_REJECTION_REASON)
        return (
            PaymentFailed(
                payment_id=payment.id,
                from_account_id=payment.from_account_id,
                amount=payment.amount,
                reason=PROVIDER_REJECTION_REASON,
            ),
            PaymentRefunded(
                payment_id=payment.id,
                from_account_id=payment.from_account_id,
                refunded_amount=payment.amount,
                reason=PROVIDER_REJECTION_REASON,
            ),
        )

    # SUCCEEDED и REFUNDED: у провайдера операция завершена, у нас платёж проведён.
    # Статус платежа остаётся SETTLED в обоих случаях, а факт возврата
    # постфактум оформляется отдельным событием (T-2.7).
    payment.settle()
    return (
        PaymentSettled(
            payment_id=payment.id,
            from_account_id=payment.from_account_id,
            amount=payment.amount,
            provider_payment_id=payment.provider_payment_id,
        ),
    )


def is_provider_status_actionable(payment_status: PaymentStatus, provider_status: ProviderStatus) -> bool:
    """Стоит ли вообще что-то делать с платёжом при таком ответе провайдера.

    «Ничего» — это два разных случая, и их важно различать:

    * платёж уже терминальный (вебхук или сверка успели раньше): ответ провайдера
      запоздал, применять его нельзя — статус-машина отвергла бы переход;
    * провайдер ещё обрабатывает (``PENDING``/``PROCESSING``): ждём дальше.
    """
    if payment_status.is_terminal:
        return False
    return provider_status in TERMINAL_PROVIDER_STATUSES


async def _release_hold(uow: UnitOfWork, payment: Payment) -> None:
    """Возвращает списанную под платёж сумму на счёт плательщика.

    Бизнес-отказ — единственный исход, где мы **точно знаем**, что деньги у
    провайдера не забраны: шлюз отклонил операцию по существу. Значит, холд нужно
    отпустить, иначе деньги клиента остались бы замороженными навсегда за платёж,
    который не прошёл.

    Повторный возврат исключён статус-машиной: ``FAILED`` терминален, значит
    второй заход в эту функцию невозможен.

    :raises EntityNotFoundError: счёт плательщика исчез из хранилища.
    :raises AccountBlocked: счёт заблокирован. Деньги тогда останутся в холде, а
        платёж — в ``PROCESSING``: это осознанно. «Пропустить» возврат и записать
        ``FAILED`` нельзя — клиент потеряет сумму, а починить холд потом будет
        некому. Освобождение счёта и повторный заход сценария сделают возврат
        позже; сценарий падает с доменной ошибкой, а не делает вид, что всё в порядке.
    """
    account = await uow.accounts.get_for_update(payment.from_account_id)
    if account is None:
        raise EntityNotFoundError(f'Счёт плательщика {payment.from_account_id} не найден для возврата средств')
    account.deposit(payment.amount)
    await uow.accounts.update(account)
