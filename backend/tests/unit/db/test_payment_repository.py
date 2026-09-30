"""Маппинг доменного платежа в строку ``payments`` и обратно (T-3.4).

Без БД: интеграционные тесты доказывают поведение репозитория на живом Postgres,
здесь — только преобразование. Разделение существенно, потому что у маппера есть
ошибки, которые ни один интеграционный тест не поймает:

* **потеря состояния при round-trip.** Если ``to_domain_payment`` забудет
  ``failure_reason`` или ``provider_payment_id``, интеграционный тест «прочитал —
  изменил — сохранил» пройдёт (он меняет статус, а не реквизиты отказа) и
  пропустит платёж, у которого потерялась причина отказа;
* **выдуманные метки времени.** ``created_at``/``updated_at`` в строке есть
  всегда, а вот свежая строка, собранная руками, может их не иметь. Маппер,
  который на это махнёт рукой, подставил бы домену «сейчас» — и по выдуманному
  времени сверка либо не нашла бы зависший платёж, либо искала бы его вечно;
* **валюта не из своей колонки.** Её в строке платежа нет вовсе, поэтому каждый
  тест, где валюта задаётся явно, — это проверка того, что маппер берёт её у
  счёта и не путает валюты между собой.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import InvalidCurrencyError, InvalidIdentifierError, InvalidValueError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus
from src.core_service.domain.versioning import INITIAL_VERSION
from src.db.models import PaymentModel
from src.db.repositories import new_payment_model, to_domain_payment

#: Метки, под которыми строка лежит в базе. Разные значения по умолчанию важны:
#: при равных метках потеря одной из них не видна — round-trip обязан их различать.
CREATED_AT = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)
UPDATED_AT = datetime(2026, 3, 14, 15, 19, 26, tzinfo=UTC)


def _payment(currency: Currency = Currency.RUB, amount: str = '250.00') -> Payment:
    """Доменный платёж для записи в хранилище."""
    return Payment(
        payment_id=PaymentId.new(),
        from_account_id=AccountId.new(),
        amount=Money.from_number(Decimal(amount), currency),
    )


def _model(status: str = 'PENDING', amount: str = '250.000', **overrides: object) -> PaymentModel:
    """Строка ``payments`` как её вернул бы Postgres.

    ``amount`` приходит с точностью колонки: ``NUMERIC(27,3)`` отдаёт ``Decimal``,
    и именно эту форму маппер обязан понимать.
    """
    defaults: dict[str, object] = {
        'id': PaymentId.new().value,
        'from_account_id': AccountId.new().value,
        'amount': Decimal(amount),
        'status': status,
        'provider_payment_id': None,
        'provider_status': None,
        'failure_reason': None,
        'version': INITIAL_VERSION,
        'created_at': CREATED_AT,
        'updated_at': UPDATED_AT,
    }
    return PaymentModel(**{**defaults, **overrides})


# --- Строка -> домен ----------------------------------------------------------


def test_to_domain_payment_keeps_every_field() -> None:
    """Round-trip состояния: реквизиты операции, статус, версия, метки времени.

    Проверяются все поля сразу и по одному объекту: потеря любого из них снаружи
    выглядит как «операция прошла», а на деле оставляет платёж в другом
    состоянии — и заметить это можно только здесь.
    """
    model = _model(status='PROCESSING', provider_payment_id='ext-42', version=3)

    payment = to_domain_payment(model, Currency.RUB)

    assert payment.id.value == model.id
    assert payment.from_account_id.value == model.from_account_id
    assert payment.amount == Money.from_number(Decimal('250.00'), Currency.RUB)
    assert payment.status is PaymentStatus.PROCESSING
    assert payment.version == 3
    assert payment.provider_payment_id == 'ext-42'
    assert payment.failure_reason is None
    assert payment.created_at == CREATED_AT
    assert payment.updated_at == UPDATED_AT


def test_to_domain_payment_keeps_failure_reason() -> None:
    """Причина отказа — часть состояния платежа, а не украшение.

    Потерянная причина выглядит снаружи как обычный ``FAILED``-платёж, по
    которому никто не поймёт, что произошло: разбираться пришлось бы вручную.
    """
    payment = to_domain_payment(
        _model(status='FAILED', failure_reason='Отказ банка получателя', version=4),
        Currency.RUB,
    )

    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_reason == 'Отказ банка получателя'
    assert payment.version == 4


@pytest.mark.parametrize(
    ('currency', 'stored', 'expected'),
    [
        (Currency.RUB, '250.000', '250.00'),
        (Currency.JPY, '100.000', '100'),
        (Currency.KWD, '1.234', '1.234'),
    ],
)
def test_to_domain_payment_applies_currency_quantum(currency: Currency, stored: str, expected: str) -> None:
    """Колонка одна на все валюты, точность задаёт валюта.

    Иена в ``NUMERIC(27,3)`` лежит как ``100.000`` и читаться обязана как ``100``:
    иначе сумма в иенах была бы длиннее своей валюты, и любое сравнение с
    суммой, посчитанной доменом, разошлось бы.
    """
    payment = to_domain_payment(_model(amount=stored), currency)

    assert payment.amount == Money.from_number(Decimal(expected), currency)
    assert payment.currency is currency


def test_to_domain_payment_rejects_currency_outside_the_scale() -> None:
    """Мусор в колонке валюты не превращается в тихое значение по умолчанию.

    ``Currency('XXX')`` бросает доменную ошибку: в базу не может попасть код,
    которого нет в шкале шлюза, иначе деньги оказались бы посчитаны не в той
    денежной единице.
    """
    with pytest.raises(InvalidCurrencyError):
        to_domain_payment(_model(), Currency('XXX'))


def test_to_domain_payment_rejects_nil_uuid() -> None:
    """Нулевой UUID не превращается в валидный ``PaymentId``."""
    with pytest.raises(InvalidIdentifierError):
        to_domain_payment(_model(id=UUID(int=0)), Currency.RUB)


def test_to_domain_payment_rejects_status_outside_the_scale() -> None:
    """Статус вне статус-машины не становится ``PENDING`` по умолчанию.

    Тихая подстановка была бы худшим исходом: платёж, отклонённый провайдером,
    выглядел бы как ожидающий ответа, и сверка по «зависшим» забрала бы его
    снова и снова.
    """
    with pytest.raises(ValueError, match='BROKEN'):
        to_domain_payment(_model(status='BROKEN'), Currency.RUB)


def test_to_domain_payment_rejects_bound_status_without_provider_payment_id() -> None:
    """``PROCESSING`` без идентификатора операции — испорченная строка.

    По доменным правилам такой платёж невозможен: операция у провайдера создана
    раньше, чем платёж перешёл в ``PROCESSING``. Строкой ниже управляет только
    база, поэтому повод не доверять её данным — достаточный.
    """
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        to_domain_payment(_model(status='PROCESSING'), Currency.RUB)


def test_to_domain_payment_rejects_provider_payment_id_before_the_provider_call() -> None:
    """``PENDING`` с идентификатором операции — тоже мусор, и тоже заметный.

    Обратная ситуация: до вызова провайдера операции не существует, а значит и
    идентификатора у платежа быть не может. Придумывать его обратно нельзя.
    """
    with pytest.raises(InvalidValueError, match='provider_payment_id'):
        to_domain_payment(_model(status='PENDING', provider_payment_id='ext-1'), Currency.RUB)


@pytest.mark.parametrize('missing', ['created_at', 'updated_at'])
def test_to_domain_payment_refuses_to_invent_missing_timestamps(missing: str) -> None:
    """Нет метки времени — повод отказать, а не подставить «сейчас».

    Строка из базы метку всегда имеет (``NOT NULL`` плюс ``server_default``), так
    что её отсутствие — признак испорченных данных. Выдуманная метка опаснее
    явной ошибки: по ней сверка решила бы, что платёж «свежий», и не тронула бы
    его никогда.
    """
    model = _model()
    setattr(model, missing, None)

    with pytest.raises(InvalidValueError, match=missing):
        to_domain_payment(model, Currency.RUB)


# --- Домен -> строка ----------------------------------------------------------


def test_new_payment_model_carries_domain_state() -> None:
    """Все поля домена попадают в колонки без потерь и переименований."""
    payment = _payment(Currency.KWD, '1.234')
    payment.process('ext-7')

    model = new_payment_model(payment)

    assert model.id == payment.id.value
    assert model.from_account_id == payment.from_account_id.value
    assert model.amount == Decimal('1.234')
    assert model.status == 'PROCESSING'
    assert model.provider_payment_id == 'ext-7'
    assert model.failure_reason is None
    assert model.version == payment.version == INITIAL_VERSION + 1


def test_new_payment_model_keeps_currency_out_of_the_row() -> None:
    """В строке платежа нет колонки валюты — и добавлять её нельзя.

    Валюта принадлежит счёту. Появись она ещё и здесь, у шлюза стало бы два места,
    где живёт валюта платежа: перевели счёт в другую валюту — и платёж поехал бы
    по старой, а суммы у клиента и в базе разошлись бы навсегда.
    """
    model = new_payment_model(_payment(Currency.JPY, '100'))

    assert 'currency' not in PaymentModel.__table__.columns
    assert getattr(model, 'currency', None) is None


def test_new_payment_model_leaves_timestamps_to_the_database() -> None:
    """Время проставляет база, а не репозиторий.

    ``server_default`` нужен ради строк, записанных мимо репозитория (импорт,
    ручной ``INSERT``, скрипт сверки), — и он же требует, чтобы Python не
    подставлял своё время молча.
    """
    model = new_payment_model(_payment())

    assert model.created_at is None
    assert model.updated_at is None


def test_new_payment_model_leaves_provider_status_unset() -> None:
    """Сырой статус провайдера репозиторий не выдумывает.

    В доменном ``Payment`` его нет: перевод шкалы провайдера в нашу делает
    прикладной слой (T-2.5). Записать в колонку что-то, чего нет у домена, —
    значит завести в хранилище второе представление статуса, которое однажды
    разойдётся с первым.
    """
    model = new_payment_model(_payment())

    assert model.provider_status is None


def test_model_round_trip_preserves_payment_state() -> None:
    """``домен -> строка -> домен`` не меняет ни одного наблюдаемого поля.

    Метки времени в круг не входят: ``new_payment_model`` их не пишет, потому что
    они принадлежат базе. Здесь их роль играет база — строка получает метки перед
    обратным чтением, ровно как после ``INSERT``. Проверять «полноту» меток у
    ещё не сохранённой строки бессмысленно: их там нет по замыслу.
    """
    payment = _payment(Currency.JPY, '100')
    payment.process('ext-9')
    model = new_payment_model(payment)
    model.created_at = CREATED_AT
    model.updated_at = UPDATED_AT

    restored = to_domain_payment(model, Currency.JPY)

    assert restored == payment
    assert restored.amount == payment.amount
    assert restored.currency is Currency.JPY
    assert restored.status is PaymentStatus.PROCESSING
    assert restored.provider_payment_id == 'ext-9'
    assert restored.version == payment.version
    assert restored.created_at == CREATED_AT
    assert restored.updated_at == UPDATED_AT


def test_to_domain_payment_rejects_naive_timestamp() -> None:
    """Наивное время из строки не превращается в метку UTC.

    Правило «метка времени строго в UTC» общее для всех слоёв и принадлежит
    ``domain/validation.py``; маппер, смягчивший его, разошёлся бы с доменом там,
    где разойтись хуже всего, — в сравнении «зависших» платежей.
    """
    with pytest.raises(InvalidValueError, match='timezone-aware'):
        to_domain_payment(_model(updated_at=datetime(2026, 3, 14, 15, 19, 26)), Currency.RUB)
