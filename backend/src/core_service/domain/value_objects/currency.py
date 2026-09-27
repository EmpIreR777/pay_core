"""Валюта как Value Object (T-1.1).

Валюты составляют закрытое множество, поэтому представлены
:class:`~enum.StrEnum`

* валидацию кода — ``Currency('XXX')`` и ``Currency('РУБ')`` отвергаются
  самим Enum;
* корректное отображение в тексте — ``str(Currency.RUB) == 'RUB'``, а не
  ``'Currency.RUB'``, как у обычного ``Enum``;
* совместимость с инфраструктурой: pydantic v2 валидирует и сериализует
  такой тип сам, SQLAlchemy ``ENUM(Currency)`` даёт ``VARCHAR(3)``.

Набор валют расширяется только правкой этого класса — сознательно: валюта,
которой нет в коде, не должна молча проходить в расчёты.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from src.core_service.domain.exceptions import InvalidCurrencyError

#: Длина кода валюты по ISO 4217.
ISO_4217_CODE_LENGTH: Final = 3

#: Число знаков после запятой по умолчанию (у большинства валют).
DEFAULT_MINOR_UNIT: Final = 2

#: Валюты без минорных единиц: целые иены, воны, кроны.
ZERO_DECIMAL_CURRENCIES: Final = frozenset(
    {
        'BIF',
        'CLP',
        'DJF',
        'GNF',
        'ISK',
        'JPY',
        'KMF',
        'KRW',
        'PYG',
        'RWF',
        'UGX',
        'VND',
        'VUV',
        'XAF',
        'XOF',
        'XPF',
    }
)

#: Валюты с тремя знаками после запятой (динар, кувей, риал).
THREE_DECIMAL_CURRENCIES: Final = frozenset(
    {
        'BHD',
        'IQD',
        'JOD',
        'KWD',
        'LYD',
        'OMR',
        'TND',
    }
)


def normalize_currency_code(raw_code: Any) -> str:
    """Приводит код валюты к каноническому виду ``XXX``.

    Вынесено из ``_missing_``, потому что код валюты приходит не только
    через ``Currency(...)``: адаптеры нормализуют его перед сохранением в БД
    и перед логированием.
    """
    if not isinstance(raw_code, str):
        raise InvalidCurrencyError(f'Код валюты должен быть строкой, получено {type(raw_code).__name__}')

    normalized = raw_code.strip().upper()
    # isalpha() без isascii() пропустил бы кириллицу («РУБ»), а такие коды
    # в SQL-колонку попасть не должны.
    if len(normalized) != ISO_4217_CODE_LENGTH or not (normalized.isascii() and normalized.isalpha()):
        raise InvalidCurrencyError(
            f'Код валюты {raw_code!r} не соответствует ISO 4217: ожидается ровно {ISO_4217_CODE_LENGTH} ASCII-буквы'
        )
    return normalized


class Currency(StrEnum):
    """Валюта платежа: код ISO 4217 плюс производные от него свойства.

    Только код и свойства. Ни курса, ни позиции Decimal — это уже данные
    application-слоя.
    """

    RUB = 'RUB'
    USD = 'USD'
    EUR = 'EUR'
    KZT = 'KZT'
    JPY = 'JPY'
    KWD = 'KWD'

    @classmethod
    def _missing_(cls, value: object) -> Currency | None:
        """Нормализует вход и поднимает доменную ошибку вместо ValueError.

        Вызывается, когда обычного поиска по значению не хватило: пробуем
        привести код к каноническому виду и найти уже существующий member.
        Исключение, брошенное здесь, пробрасывается наружу как есть — в отличие
        от ``return None``, после которого stdlib выбросил бы «голый»
        ``ValueError``, а домен не должен отдавать наружу исключения stdlib.
        """
        normalized = normalize_currency_code(value)
        for member in cls:
            if member.value == normalized:
                return member
        raise InvalidCurrencyError(
            f'Валюта {normalized} не поддерживается шлюзом. Доступны: {", ".join(m.value for m in cls)}',
        )

    @property
    def minor_unit(self) -> int:
        """Сколько знаков после запятой у этой валюты (0, 2 или 3)."""
        if self.value in ZERO_DECIMAL_CURRENCIES:
            return 0
        if self.value in THREE_DECIMAL_CURRENCIES:
            return 3
        return DEFAULT_MINOR_UNIT

    @property
    def quantum(self) -> Decimal:
        """Шаг округления суммы: ``0.01`` для рублей, ``1`` для иен."""
        return Decimal(1).scaleb(-self.minor_unit)
