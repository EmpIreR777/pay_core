"""Сценарий создания платежа (T-2.4).

Сагиподобный флоу, в котором внешний вызов стоит **между** двумя транзакциями:

    1. Проверка ключа идемпотентности: сохранённый ответ (повтор) либо
       резервирование ключа (``SET NX``) под текущий запрос;
    2. Блокировка счёта плательщика — на время списания (шаг 3), а не навсегда;
    3. UoW#1: списание счёта, создание ``Payment(PENDING)``, ``PaymentCreated``
       в outbox, коммит;
    4. ``PaymentProvider.create_payment`` — **вне** транзакции (AGENT.md §4.2);
    5. UoW#2: перевод платежа в статус по ответу провайдера, сохранение
       ``provider_payment_id``, событие в outbox, коммит;
    6. Сохранение ответа под ключом идемпотентности.

Почему транзакций две, а не одна: держать блокировку строк и соединение с БД
время ответа внешнего шлюза нельзя — это убивает параллелизм и повышает шанс
конфликта. Цена решения известна и осознанна: если провайдер упал технически на
шаге 4, деньги уже списаны, а платёж остался ``PENDING``. «Откатить» списание
нельзя — оно зафиксировано, и обратное движение денег стало бы второй финансовой
операцией. Поэтому такое состояние не маскируется: сценарий пробрасывает
``PaymentProviderError`` наружу, а исход разрулит сверка с провайдером (T-2.10) —
платёж найдётся по ``provider_payment_id`` запросом ``get_status``.

Два разных отказа провайдера трактуются по-разному (контракт порта, T-2.2), и
различие не косметическое — оно решает судьбу денег:

* **бизнес-отказ** (``ProviderResult`` со статусом ``FAILED``) — исход **известен**:
  шлюз отклонил операцию, деньги у него не забраны. Поэтому сценарий не только
  переводит платёж в ``FAILED`` и публикует ``PaymentFailed``, но и **возвращает
  холд** на счёт плательщика (``PaymentRefunded``) — в той же транзакции. Без
  возврата деньги клиента висели бы замороженными навсегда за платёж, который не
  прошёл;
* **технический отказ** (``PaymentProviderError``) — исход **неизвестен**: возможно,
  провайдер всё же провёл операцию. Состояние не выдумывается, платёж остаётся
  ``PENDING``, холд **остаётся** (возврат при неизвестном исходе — это риск
  двойного возврата), наружу уходит ошибка. Исход разрулит сверка (T-2.10) по
  ``provider_payment_id``.

Ключ идемпотентности освобождается только тогда, когда операция не состоялась:
если деньги не списаны (отказ на шаге 3), повтор с тем же ключом честно должен
дойти до конца. Как только списание зафиксировано, ключ остаётся захваченным —
иначе повтор создал бы второй платёж и списал сумму дважды.
"""

from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from typing import Final

from src.core_service.application.dto.payment import CreatePaymentInput, CreatePaymentOutput
from src.core_service.application.ports.clock import Clock
from src.core_service.application.ports.event_publisher import EventPublisher
from src.core_service.application.ports.idempotency_store import IdempotencyRecord, IdempotencyStore
from src.core_service.application.ports.lock_manager import ACCOUNT_LOCK_RESOURCE_PREFIX, LockManager
from src.core_service.application.ports.payment_provider import (
    PaymentProvider,
    ProviderResult,
)
from src.core_service.application.ports.unit_of_work import UnitOfWork
from src.core_service.application.use_cases.payment_sync import apply_provider_status
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.events.base import DomainEvent
from src.core_service.domain.events.payment import PaymentCreated
from src.core_service.domain.exceptions import DuplicateOperation, EntityNotFoundError
from src.core_service.domain.value_objects.identifiers import PaymentId

#: Сколько живёт резервация ключа, пока сценарий его держит. Это верхняя граница
#: «хвоста» сценария (блокировка + две транзакции + вызов провайдера), взятая с
#: запасом: резервация, истёкшая посреди живой операции, выпустит наружу
#: параллельный повтор того же запроса.
IDEMPOTENCY_RESERVATION_TTL_SECONDS: Final[int] = 300

#: Сколько хранится сохранённый ответ (сутки): за это время клиентские ретраи
#: после обрыва сети находят тот же платёж, а не создают новый.
IDEMPOTENCY_RECORD_TTL_SECONDS: Final[int] = 86_400


def _request_hash(data: CreatePaymentInput) -> str:
    """Отпечаток тела запроса для хранилища идемпотентности.

    Хэшируется смысловая часть запроса (счёт и сумма), а не весь DTO: повтор с
    тем же ключом и тем же содержанием обязан узнать свой же хэш. Включать
    ``idempotency_key`` бессмысленно — он и есть ключ, по которому идёт сравнение.
    """
    payload = f'{data.from_account_id}|{data.amount.amount}|{data.amount.currency}'
    return sha256(payload.encode()).hexdigest()


class CreatePaymentUseCase:
    """Создаёт платёж: списывает средства со счёта и заводит операцию у провайдера.

    Зависимости внедряет композиционный корень (ЭПИК 6/7). Сценарий не создаёт
    их сам и не выбирает реализации — он только оркестрирует вызовы портов в
    правильном порядке, а вся финансовая логика остаётся в домене.
    """

    __slots__ = (
        '_clock',
        '_event_publisher',
        '_idempotency_store',
        '_lock_manager',
        '_payment_provider',
        '_uow_factory',
    )

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        lock_manager: LockManager,
        idempotency_store: IdempotencyStore,
        payment_provider: PaymentProvider,
        event_publisher: EventPublisher,
        clock: Clock,
    ) -> None:
        """:param uow_factory: фабрика новых единиц работы; сага открывает две
            транзакции, поэтому переиспользовать один ``UnitOfWork`` нельзя;
        :param lock_manager: блокировки счёта плательщика;
        :param idempotency_store: хранение ответов по ключу запроса;
        :param payment_provider: внешний эквайринг;
        :param event_publisher: outbox доменных событий;
        :param clock: время для меток записи идемпотентности. Платеж же
            проставляет своё доменное время: подменять его снаружи нельзя.
        """
        self._uow_factory = uow_factory
        self._lock_manager = lock_manager
        self._idempotency_store = idempotency_store
        self._payment_provider = payment_provider
        self._event_publisher = event_publisher
        self._clock = clock

    async def execute(self, data: CreatePaymentInput) -> CreatePaymentOutput:
        """Выполняет сагу создания платежа.

        :param data: вход сценария (счёт, сумма, ключ идемпотентности);
        :returns: результат сценария; при повторе с тем же ключом — ранее
            сохранённый ответ, а не новый платёж;
        :raises DuplicateOperation: ключ занят параллельным запросом (идёт
            обработка) либо использован для другого тела запроса;
        :raises EntityNotFoundError: счёт плательщика не найден;
        :raises InsufficientFunds: на счёте не хватает средств;
        :raises AccountBlocked: счёт плательщика заблокирован;
        :raises CurrencyMismatchError: валюта суммы не совпадает с валютой счёта;
        :raises PaymentProviderError: технический сбой провайдера; платёж при
            этом остаётся ``PENDING`` — исход операции у него неизвестен.
        """
        request_hash = _request_hash(data)
        stored = await self._idempotency_store.get(data.idempotency_key)
        if stored is not None:
            return self._replay(data.idempotency_key, request_hash, stored)

        # Параллельный повтор того же запроса ещё не записал ответ. Пропустить
        # его в сагу нельзя — он списал бы сумму второй раз.
        if not await self._idempotency_store.try_acquire(
            data.idempotency_key,
            ttl_seconds=IDEMPOTENCY_RESERVATION_TTL_SECONDS,
        ):
            raise DuplicateOperation(f'Операция с ключом идемпотентности {data.idempotency_key} уже выполняется')

        try:
            payment = await self._create_pending_payment(data)
        except BaseException:
            # Денег не списано: клиент вправе повторить запрос с тем же ключом.
            await self._idempotency_store.release(data.idempotency_key)
            raise

        return await self._settle_with_provider(payment, data, request_hash)

    # --- Шаги 1 и 6: идемпотентность -------------------------------------------

    def _replay(self, key: str, request_hash: str, stored: IdempotencyRecord) -> CreatePaymentOutput:
        """Возвращает сохранённый ответ повторного запроса.

        :raises DuplicateOperation: под ключом идёт обработка без ответа либо
            ключ уже использован для другого тела запроса (конфликт, а не повтор).
        """
        if stored.request_hash != request_hash:
            raise DuplicateOperation(f'Ключ идемпотентности {key} уже использован для другого запроса')
        if stored.response is None:
            raise DuplicateOperation(f'Операция с ключом идемпотентности {key} ещё выполняется')
        return CreatePaymentOutput.from_idempotency_response(stored.response)

    async def _remember_response(
        self,
        data: CreatePaymentInput,
        request_hash: str,
        output: CreatePaymentOutput,
    ) -> None:
        """Сохраняет ответ под ключом, чтобы повтор его вернул (шаг 6)."""
        now = self._clock.now()
        await self._idempotency_store.save(
            IdempotencyRecord(
                key=data.idempotency_key,
                request_hash=request_hash,
                response=output.to_idempotency_response(),
                created_at=now,
                expires_at=now + timedelta(seconds=IDEMPOTENCY_RECORD_TTL_SECONDS),
            ),
        )

    # --- Шаги 2 и 3: блокировка и первая транзакция ----------------------------

    async def _create_pending_payment(self, data: CreatePaymentInput) -> Payment:
        """Списывает средства и заводит платёж в ``PENDING`` (шаги 2-3).

        Блокировка охватывает только транзакцию со списанием. После коммита
        деньги уже списаны, и держать ресурс на время обращения к провайдеру
        незачем: это сузило бы окно для других платежей того же счёта и свело
        на нет TTL блокировки.
        """
        async with (
            self._lock_manager.lock(f'{ACCOUNT_LOCK_RESOURCE_PREFIX}{data.from_account_id}'),
            self._uow_factory() as uow,
        ):
            account = await uow.accounts.get_for_update(data.from_account_id)
            if account is None:
                raise EntityNotFoundError(f'Счёт плательщика {data.from_account_id} не найден')
            # Отказы домена (недостаточно средств, заблокирован счёт, чужая
            # валюта) поднимаются здесь и отменяют обе записи транзакции.
            account.withdraw(data.amount)
            await uow.accounts.update(account)

            payment = Payment.create(
                payment_id=PaymentId.new(),
                from_account_id=data.from_account_id,
                amount=data.amount,
            )
            await uow.payments.add(payment)
            await self._event_publisher.publish(
                PaymentCreated(
                    payment_id=payment.id,
                    from_account_id=payment.from_account_id,
                    amount=payment.amount,
                ),
            )
            await uow.commit()
        return payment

    # --- Шаги 4 и 5: провайдер и вторая транзакция ----------------------------

    async def _settle_with_provider(
        self,
        payment: Payment,
        data: CreatePaymentInput,
        request_hash: str,
    ) -> CreatePaymentOutput:
        """Создаёт операцию у провайдера и фиксирует её результат (шаги 4-5).

        Вызов провайдера намеренно стоит между двумя ``async with``: обе
        транзакции к этому моменту закрыты, строки не заблокированы.
        """
        try:
            provider_result = await self._payment_provider.create_payment(
                payment_id=payment.id,
                amount=data.amount,
                # Ключ клиента, а не идентификатор платежа: по нему повтор того
                # же запроса уйдёт к провайдеру как та же самая операция.
                idempotency_key=data.idempotency_key,
            )
        except Exception:
            # Исход у провайдера неизвестен, платёж остаётся PENDING. Ответ под
            # ключом всё равно сохраняется: операция уже создана и деньги списаны,
            # поэтому повтор обязан вернуть этот платёж, а не начать новый.
            await self._remember_response(data, request_hash, CreatePaymentOutput.from_payment(payment))
            raise

        output = await self._apply_provider_result(payment, provider_result)
        await self._remember_response(data, request_hash, output)
        return output

    async def _apply_provider_result(self, payment: Payment, provider_result: ProviderResult) -> CreatePaymentOutput:
        """Переводит платёж в статус, соответствующий ответу провайдера (шаг 5).

        Само правило перевода и возврата холда живёт в
        :mod:`application.use_cases.payment_sync` — оно одинаково для создания
        (T-2.4), чтения с актуализацией (T-2.5) и вебхука (T-2.7). Здесь только
        оркестрация: открыть транзакцию, применить правило, опубликовать события,
        закоммитить.

        Платёж перечитывается в новой транзакции: между шагами прошло время и
        внешний вызов, а правила применяются к актуальному состоянию.

        Шкалы провайдера и платежа разные, поэтому перевод сделан явно, а не
        «по совпадению имён»: ``SUCCEEDED`` у шлюза — это ``SETTLED`` у нас.

        :raises EntityNotFoundError: платёж, только что зафиксированный шагом 3,
            не найден — сбой хранилища. Молчаливый успех означал бы потерянные
            деньги, поэтому это ошибка, а не пустой результат.
        """
        async with self._uow_factory() as uow:
            stored = await uow.payments.get(payment.id)
            if stored is None:
                raise EntityNotFoundError(f'Платёж {payment.id} не найден после создания')

            # Сначала PROCESSING: операция у провайдера уже существует, а в
            # PENDING мы к нему ещё не обращались. Дальше состояние либо
            # остаётся (ждём вебхук), либо становится терминальным.
            stored.process(provider_result.provider_payment_id)
            events = await apply_provider_status(uow, stored, provider_result.status, self._event_publisher)
            return await self._commit_payment_update(stored, uow, events=events)

    async def _commit_payment_update(
        self,
        payment: Payment,
        uow: UnitOfWork,
        *,
        events: tuple[DomainEvent, ...],
    ) -> CreatePaymentOutput:
        """Сохраняет обновлённый платёж и его события в одной транзакции.

        Статус, ``provider_payment_id`` и запись в outbox обязаны попасть в БД
        вместе: событие о платеже, которого в базе нет, — «призрак», на который
        потом не найти ни баланса, ни сверки.
        """
        await uow.payments.update(payment)
        for event in events:
            await self._event_publisher.publish(event)
        await uow.commit()
        return CreatePaymentOutput.from_payment(payment)
