"""Базовые доменные исключения.

Здесь лежит только то, что нужно Value Objects (T-1.1). Бизнес-исключения
``InsufficientFunds``, ``AccountBlocked``, ``InvalidTransition``,
``DuplicateOperation``, ``PaymentProviderError`` появятся в T-1.5 и тоже
наследуются от :class:`DomainError`.

Правило кодекса (AGENT.md, раздел 4): любое доменное исключение наследуется
от ``DomainError``. Дополнительное наследование от ``ValueError`` у ошибок
некорректного входного значения сделано намеренно — это позволяет обрабатывать
их обычным ``except ValueError`` на границах слоёв, не теряя доменной
семантики внутри ядра.
"""


class DomainError(Exception):
    """Базовая точка иерархии исключений доменного слоя.

    Ловится в application/ и инфраструктуре для маппинга на транспортный
    протокол (gRPC-код, HTTP-статус). Наружу наружу утекать не должен.
    """


class InvalidValueError(DomainError, ValueError):
    """Некорректное значение, переданное в Value Object.

    Наследует ``ValueError``, потому что по смыслу это подкласс ошибок
    разбора/валидации значения, а не логики агрегата.
    """


class InvalidCurrencyError(InvalidValueError):
    """Код валюты не соответствует формату ISO 4217 (три ASCII-буквы)."""


class CurrencyMismatchError(DomainError, ValueError):
    """Попытка сложить, вычесть или сравнить деньги в разных валютах.

    Молча конвертировать нельзя: курс — внешняя политика, а не доменное
    свойство денег. Сначала нужно явно получить новую сумму нужной валюты.
    """


class InvalidAmountError(InvalidValueError):
    """Сумма имеет неподдерживаемый тип, значение или величину."""


class NegativeAmountError(InvalidAmountError):
    """Сумма отрицательна.

    ``Money`` моделирует ненаправленную величину: знак операции задаётся
    доменным методом (пополнение/списание), а не знаком внутри суммы.
    """


class InvalidIdentifierError(InvalidValueError):
    """Идентификатор сущности не является корректным UUID."""
