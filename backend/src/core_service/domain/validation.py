"""Единые примитивы валидации ядра (T-2.3).

Правило проекта (AGENT.md, §5): **одно правило — одна реализация**. Любая
проверка, нужная более чем в одном месте (домен, события, порты, DTO), живёт
здесь; локальные копии ``_require_*`` в других модулях запрещены. Это стережёт
архитектурный тест ``tests/unit/test_architecture.py``.

Модуль — чистый Python: только стандартная библиотека и доменные исключения.
Поэтому его спокойно импортируют все слои ядра (application и адаптеры зависят
от домена, но не наоборот).

Соглашения:

* ``context`` — имя поля или операции для сообщения об ошибке (``'amount'``,
  ``'Пополнение счёта'``);
* текст сообщения собирается **только здесь**: меняется правило или формат
  ошибки — правится одна функция, а не N копий по слоям;
* ошибки всегда доменные (``InvalidValueError``/``InvalidAmountError``), а не
  ``TypeError``/``AttributeError`` из глубины stdlib;
* ``require_non_empty_str`` возвращает уже обрезанное значение: нормализация и
  проверка — один шаг, чтобы вызывающий не забыл ``strip``.
"""

from datetime import UTC, datetime

from src.core_service.domain.exceptions import (
    DomainError,
    InvalidAmountError,
    InvalidIdentifierError,
    InvalidValueError,
)
from src.core_service.domain.value_objects.identifiers import AccountId, PaymentId
from src.core_service.domain.value_objects.money import Money
from src.core_service.domain.value_objects.payment_status import PaymentStatus, require_time_order


def require_type[T](
    value: object,
    expected_type: type[T],
    context: str,
    *,
    error_type: type[DomainError] = InvalidValueError,
) -> T:
    """Требует, чтобы значение было экземпляром ожидаемого типа.

    :param error_type: доменное исключение для поднятия. Идентификаторы поднимают
        ``InvalidIdentifierError``, остальные поля — ``InvalidValueError``:
        правило одно, различается лишь семантика ошибки.
    """
    if not isinstance(value, expected_type):
        raise error_type(f'{context}: ожидается экземпляр {expected_type.__name__}, получено {type(value).__name__}')
    return value


def require_int(value: object, context: str) -> int:
    """Требует ``int`` и отвергает ``bool``.

    ``bool`` — подкласс ``int``, поэтому ``True``/``False`` прошли бы проверку и
    молча превратились бы в ``1``/``0`` (например, в версии для optimistic lock).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValueError(f'{context}: ожидается int, получено {type(value).__name__}')
    return value


def require_min_int(value: object, context: str, *, minimum: int) -> int:
    """Требует целое не меньше ``minimum`` (тип проверяет ``require_int``)."""
    checked = require_int(value, context)
    if checked < minimum:
        raise InvalidValueError(f'{context}: не может быть меньше {minimum}, передано {checked}')
    return checked


def require_utc(value: object, context: str) -> datetime:
    """Требует timezone-aware ``datetime`` строго в UTC.

    Наивное время запрещено: без зоны одна и та же метка читается по-разному, и
    сравнение «зависших» платежей разваливается. Другие зоны тоже отвергаются —
    нормализацию в UTC делает вызывающий (например, ``datetime.now(UTC)``).
    """
    if not isinstance(value, datetime):
        raise InvalidValueError(f'{context}: ожидается datetime, получено {type(value).__name__}')
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidValueError(f'{context}: datetime должен быть timezone-aware (UTC)')
    if value.tzinfo.utcoffset(value) != UTC.utcoffset(value):
        raise InvalidValueError(f'{context}: временная зона должна быть строго UTC, получено {value.tzinfo}')
    return value


def require_non_empty_str(value: object, context: str) -> str:
    """Требует непустую строку и возвращает её без пробелов по краям."""
    if not isinstance(value, str):
        raise InvalidValueError(f'{context}: ожидается непустая строка, получено {type(value).__name__}')
    stripped = value.strip()
    if not stripped:
        raise InvalidValueError(f'{context}: ожидается непустая строка')
    return stripped


def require_optional_non_empty_str(value: object, context: str) -> str | None:
    """Как ``require_non_empty_str``, но ``None`` — легальное «не задано»."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(f'{context}: ожидается непустая строка или None, получено {type(value).__name__}')
    stripped = value.strip()
    if not stripped:
        raise InvalidValueError(f'{context}: ожидается непустая строка или None')
    return stripped


def require_max_length_str(value: str, context: str, *, max_length: int) -> str:
    """Ограничивает длину строки (например, шириной колонки БД).

    Правило сквозное: транспорт, DTO и хранилище обязаны мерить ключ по одной
    границе, иначе длинный ключ дойдёт до БД и упадёт там.
    """
    if len(value) > max_length:
        raise InvalidValueError(f'{context}: длина {len(value)} превышает допустимые {max_length} символов')
    return value


def require_money(value: object, context: str) -> Money:
    """Требует ``Money`` (только тип — без проверки величины).

    Отдельная функция нужна там, где между проверкой типа и проверкой нуля
    должно уместиться ещё одно правило (валюты в ``Account``): так каждое правило
    остаётся реализовано ровно один раз. Формат сообщения — тот же, что у
    ``require_type``: «одно правило — одна реализация» относится и к тексту ошибки.
    """
    return require_type(value, Money, context)


def require_non_zero_money(amount: Money, context: str) -> Money:
    """Запрещает нулевую сумму: она ничего не меняет, но двигает версию и события."""
    if amount.is_zero:
        raise InvalidAmountError(f'{context}: сумма не может быть нулевой')
    return amount


def require_positive_money(value: object, context: str) -> Money:
    """Композиция ``require_money`` + ``require_non_zero_money`` (тип и ненулевая сумма).

    Удобна на полях-суммах, где между двумя правилами нет третьего (DTO,
    конструктор ``Payment``). Реализации правил при этом не дублируются —
    функция лишь вызывает их по очереди.
    """
    return require_non_zero_money(require_money(value, context), context)


# --- Составные правила о платёжных данных --------------------------------------
#
# Ниже — не примитивы «поле за полем», а готовые правила «какой набор полей
# обязателен». Их пришлось вынести после того, как ядро проверок оказалось
# расписано вручную в пяти событиях и трёх выходах сценария: восемь копий одного
# правила разъезжаются при первой же правке, и заметить это можно только на проде.


def require_payment_fields(payment_id: PaymentId, from_account_id: AccountId, amount: Money) -> None:
    """Проверяет обязательное ядро платёжных данных: платёж, его счёт и сумму.

    Одно и то же ядро носят и сущность ``Payment``, и все события жизненного цикла
    (``PaymentCreated``, ``PaymentSettled``, ``PaymentFailed``, ``PaymentRefunded``,
    ``PaymentCancelled``). Платежа без счёта плательщика и без суммы не существует
    ни в базе, ни в сообщении для потребителя: по нему нельзя ни найти платёж,
    ни свести баланс.

    :param payment_id: идентификатор платежа;
    :param from_account_id: счёт плательщика;
    :param amount: сумма движения. Правило принимает значение, а не поле, поэтому
        одинаково проверяет и ``amount``, и ``refunded_amount``;
    :raises InvalidValueError: если поле имеет неверный тип или сумма неположительна.
    """
    require_type(payment_id, PaymentId, 'payment_id', error_type=InvalidIdentifierError)
    require_type(from_account_id, AccountId, 'from_account_id', error_type=InvalidIdentifierError)
    require_positive_money(amount, 'amount')


def require_payment_output_fields(payment_id: PaymentId, status: PaymentStatus, amount: Money) -> None:
    """Проверяет обязательное ядро любого выхода сценария оплаты.

    Ответ клиенту без идентификатора, статуса или суммы бесполезен: UI не покажет
    ни строки платежа, ни состояния, ни суммы. Ядро одинаково у результата
    создания (T-2.4), чтения (T-2.5) и отмены (T-2.6).

    :param payment_id: идентификатор платежа;
    :param status: статус платежа на момент ответа;
    :param amount: сумма платежа;
    :raises InvalidValueError: если поле имеет неверный тип или сумма неположительна.
    """
    require_type(payment_id, PaymentId, 'payment_id', error_type=InvalidIdentifierError)
    require_type(status, PaymentStatus, 'status')
    require_positive_money(amount, 'amount')


def require_payment_source(
    from_account_id: AccountId,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    """Проверяет счёт-источник и временные метки выхода сценария.

    Отдельное правило от :func:`require_payment_output_fields`, потому что набор
    полей у них разный: результат создания не отдаёт счёт и метку изменения, а
    чтение и отмена — отдают.

    :param from_account_id: счёт, с которого платёж списывался;
    :param created_at: момент создания платежа;
    :param updated_at: момент последнего изменения платежа;
    :raises InvalidValueError: если счёт или метки некорректны либо ``updated_at``
        раньше ``created_at``.
    """
    require_type(from_account_id, AccountId, 'from_account_id', error_type=InvalidIdentifierError)
    require_utc(created_at, 'created_at')
    require_utc(updated_at, 'updated_at')
    require_time_order(created_at, updated_at)
