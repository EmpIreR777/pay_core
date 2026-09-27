"""Порт внешнего платёжного провайдера (T-2.2).

Граница между ядром и системой эквайринга. Прикладной слой знает только этот
контракт; что за ним стоит — фейк для разработки или реальный шлюз — задаётся
конфигурацией (``PAYMENT_PROVIDER = 'fake' | 'yookassa'``) и не влияет на
сценарии. Реализации порождает композиционный корень (ЭПИК 6/7), а не сценарий.

Место порта в саге создания платежа (T-2.4):

    UoW#1: списание с баланса + Payment(PENDING) + outbox ──► commit
        │
        └─► PaymentProvider.create_payment   ← вызов СТРОГО ВНЕ транзакции БД
        │
    UoW#2: статус PROCESSING/FAILED + provider_payment_id + outbox ──► commit

Сетевой вызов вынесен за границу транзакции намеренно (AGENT.md, §4.2):
транзакция не держит блокировки строк всё время ответа внешней системы.

Контракт ошибок — главное различие двух видов отказа:

* **Технический отказ** (таймаут, сеть, 5xx, неразбираемый ответ) — реализация
  поднимает :class:`~src.core_service.domain.exceptions.PaymentProviderError`.
  Для сценария вызов не состоялся; повтор допустим с тем же ``idempotency_key``;
* **Бизнес-отказ** (провайдер отклонил операцию) — это НЕ исключение, а
  :class:`ProviderResult` со статусом ``FAILED``. Отклонение — легальный исход,
  который сценарий обязан обработать (перевести платёж в ``FAILED``).

Смешивать эти два вида нельзя: исключение означает «не знаем исхода», статус
``FAILED`` — «знаем: операция отклонена».

Контракт идемпотентности:

* ``idempotency_key`` обязателен для ``create_payment`` и стабилен между
  повторами: тот же ключ — та же операция, повтор не создаёт второе списание.
  Адаптер строит ключ из нашего ``PaymentId``, чтобы ретраи были безопасны;
* ``get_status`` идемпотентен по своей природе (только чтение состояния);
* ``refund`` не должен допускать повторный возврат уже возвращённой суммы —
  это ответственность реализации и провайдера.

Перевод шкал статусов. Разбор «сырых» ответов конкретного шлюза в
``ProviderStatus`` — забота адаптера; перевод ``ProviderStatus`` в доменный
``PaymentStatus`` — забота прикладного слоя:

    ProviderStatus   PaymentStatus   Комментарий
    PENDING          PENDING         принят, но ещё не обработан
    PROCESSING       PROCESSING      проводится
    SUCCEEDED        SETTLED         операция успешно завершена
    FAILED           FAILED          отклонена или неуспешна
    REFUNDED         SETTLED         возврат постфактум: жизненный статус
                                     платежа остаётся SETTLED, а факт возврата
                                     оформляется событием PaymentRefunded

Терминальны на стороне провайдера ``SUCCEEDED``, ``FAILED`` и ``REFUNDED``:
после них опрашивать статус больше не нужно.

Реализации обязаны быть неблокирующими (``async``) и безопасными для
параллельных вызовов. Порт не обещает никаких гарантий по времени ответа —
дедлайны, ретраи и circuit breaker навешиваются снаружи (ЭПИК 11).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from src.core_service.domain.exceptions import InvalidValueError
from src.core_service.domain.value_objects.identifiers import PaymentId
from src.core_service.domain.value_objects.money import Money


class ProviderStatus(StrEnum):
    """Канонический статус операции на стороне провайдера.

    Это отдельная от доменного ``PaymentStatus`` шкала: конкретный шлюз может
    возвращать свои значения (``canceled``, ``waiting_for_capture``, ...) —
    адаптер приводит их к этому закрытому набору, а прикладной слой переводит
    ``ProviderStatus`` в ``PaymentStatus``. Так «сырой» вокабуляр провайдера не
    протекает ни в домен, ни в сценарии.
    """

    PENDING = 'PENDING'  # принят, но ещё не обработан
    PROCESSING = 'PROCESSING'  # проводится
    SUCCEEDED = 'SUCCEEDED'  # успешно завершён
    FAILED = 'FAILED'  # отклонён или неуспешен
    REFUNDED = 'REFUNDED'  # по операции выполнен возврат


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Ответ провайдера на изменяющую операцию (создание платежа или возврат).

    Иммутабельная структура: результат вызова — зафиксированный факт, а не
    объект для последующей правки. ``ProviderResult`` отвечает только на
    изменяющие вызовы; ``get_status`` возвращает голый :class:`ProviderStatus` —
    идентификатор операции там уже известен вызывающему.

    :param provider_payment_id: идентификатор операции во внешней системе. Его
        сценарий сохраняет в ``Payment.provider_payment_id``, чтобы затем
        сопоставлять вебхуки и запрашивать статус;
    :param status: статус на стороне провайдера на момент ответа. Может быть
        промежуточным (``PENDING``/``PROCESSING``): окончательный статус часто
        приходит отдельным вебхуком, поэтому ответ не гарантирует завершения.
    :raises InvalidValueError: если ``provider_payment_id`` не является
        непустой строкой или ``status`` не принадлежит :class:`ProviderStatus`.
    """

    provider_payment_id: str
    status: ProviderStatus

    def __post_init__(self) -> None:
        if not isinstance(self.provider_payment_id, str) or not self.provider_payment_id.strip():
            raise InvalidValueError('provider_payment_id должен быть непустой строкой')
        if not isinstance(self.status, ProviderStatus):
            raise InvalidValueError(f'status должен быть ProviderStatus, получено {type(self.status).__name__}')


@runtime_checkable
class PaymentProvider(Protocol):
    """Контракт взаимодействия с внешним платёжным провайдером.

    Реализации: ``FakePaymentProvider`` (по умолчанию в разработке и тестах) и
    ``YooKassaPaymentProvider`` (``PAYMENT_PROVIDER=yookassa``). Выбор конкретной
    реализации — забота композиционного корня, а не сценариев.

    Единый контракт ошибок: любая техническая невозможность выполнить операцию
    (сеть, таймаут, 5xx, неразбираемый ответ) поднимает
    ``PaymentProviderError`` из доменного слоя, тогда как отказ по бизнес-причине
    возвращается как ``ProviderResult`` со статусом ``FAILED``.
    """

    async def create_payment(
        self,
        *,
        payment_id: PaymentId,
        amount: Money,
        idempotency_key: str,
    ) -> ProviderResult:
        """Создаёт платёж во внешней системе.

        Вызывается вне транзакции БД: между коммитом UoW#1 и этим вызовом
        транзакция закрыта, блокировки строк отпущены.

        :param payment_id: наш идентификатор платежа (доменный тип, не строка);
        :param amount: сумма с валютой. Конвертации на стороне шлюза нет: сумма
            уходит в валюте платежа как есть;
        :param idempotency_key: стабильный ключ повтора. Повтор с тем же ключом
            обязан вернуть тот же ``provider_payment_id`` и не создать второе
            списание;
        :returns: ``ProviderResult`` с идентификатором операции у провайдера и
            её текущим статусом;
        :raises PaymentProviderError: технический сбой при вызове провайдера.
        """
        ...

    async def get_status(self, provider_payment_id: str) -> ProviderStatus:
        """Запрашивает актуальный статус ранее созданной операции.

        Операция только читает состояние и потому идемпотентна. Нужна сценарию
        ``GetPayment`` (актуализация «зависшего» платежа в ``PROCESSING``) и
        периодической сверке (reconciliation) из ЭПИК 10.

        :param provider_payment_id: идентификатор операции у провайдера
            (``Payment.provider_payment_id``);
        :returns: текущий статус операции на стороне провайдера;
        :raises PaymentProviderError: технический сбой либо операция неизвестна
            провайдеру.
        """
        ...

    async def refund(self, provider_payment_id: str, amount: Money) -> ProviderResult:
        """Инициирует возврат по проведённой операции.

        Возврат может быть полным или частичным: ``amount`` не превышает сумму
        исходного платежа. Для полного возврата передаётся сумма платежа.

        :param provider_payment_id: идентификатор операции у провайдера;
        :param amount: сумма возврата в валюте платежа;
        :returns: ``ProviderResult`` с идентификатором операции возврата и её
            статусом (часто промежуточным — итог приходит вебхуком);
        :raises PaymentProviderError: технический сбой при вызове провайдера.
        """
        ...
