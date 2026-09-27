# ЭПИК 1: Domain Layer

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Чистый Python (никаких sqlalchemy, grpc, fastapi).
DDD: Value Objects, Entities, Domain Events, Domain Exceptions, инварианты финансовой логики.

---

## Задачи эпика

### [x] T-1.1. Value Objects: Money, Currency, AccountId, PaymentId
- **Что сделать:** Иммутабельные dataclass (frozen=True). Money с валидацией валют, округлением и запретом сложения разных валют.
- **DoD:** Unit-тесты на арифметику, равенство и ошибки.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `backend/src/core_service/domain/exceptions.py` — базовый `DomainError` (AGENT.md §4.1) и
    VO-ошибки: `InvalidValueError`, `InvalidCurrencyError`, `CurrencyMismatchError`,
    `InvalidAmountError`, `NegativeAmountError`, `InvalidIdentifierError`. Бизнес-исключения
    (`InsufficientFunds`, `AccountBlocked` и др.) намеренно не добавлены — это T-1.5.
  - `value_objects/currency.py` — `Currency` на `StrEnum`, а не на dataclass: валюты образуют
    закрытое множество, поэтому Enum даёт валидацию кода, синглтонность members
    (`Currency(' rub ') is Currency.RUB`) и совместимость с pydantic v2 / SQLAlchemy бесплатно.
    Именно `StrEnum`, а не `Enum`: у обычного `Enum` `str()` даёт `'Currency.RUB'`, что портит
    сообщения об ошибках. Свойства `minor_unit`/`quantum` учитывают разную точность валют
    (JPY — 0 знаков, KWD — 3, RUB — 2). `_missing_` нормализует код и бросает
    `InvalidCurrencyError`, не выпуская наружу «голый» `ValueError` из stdlib.
  - `value_objects/money.py` — `Money` с инвариантами: только `Decimal` (float запрещён явно,
    bool отвергается до int, т.к. bool — подкласс int), неотрицательность, отказ от NaN/Infinity
    и абсурдной величины, квантование до точности валюты через `object.__setattr__`
    (frozen-dataclass иначе не даёт нормализовать значение при создании). Арифметика `+ - *`
    и порядковые сравнения бросают `CurrencyMismatchError`; `==` генерирует dataclass, поэтому
    разные валюты просто не равны. `NotImplemented` вместо исключения при работе с не-`Money` —
    тогда Python сам выдаёт корректный `TypeError`. Правило округления `ROUND_HALF_UP`
    зафиксировано тестом.
  - `value_objects/identifiers.py` — `AccountId` / `PaymentId` поверх общего `Identifier`.
    Enum здесь невозможен: пространство UUID бесконечно. Наследник помечен `dataclass`, чтобы
    `__eq__` генерировался от лица своего класса и `AccountId(x) != PaymentId(x)`.
    Отклоняется nil-UUID; строка с границы слоя приводится к UUID.
- **Проверки:** `make lint` (ruff + format + mypy --strict) — зелёные; `make test` — 120 passed;
  покрытие `src/core_service/domain` — 100 % (178/178 stmts); `make pre-commit` — все хуки зелёные.
- **Известный нюанс:** `Currency.RUB == 'RUB'` истинно (следствие `StrEnum`) — удобно для
  транспортного слоя, но в доменном коде на такое сравнение полагаться не следует; поведение
  зафиксировано отдельным тестом.

### [x] T-1.2. Entity Account
- **Что сделать:** Поля: id, balance: Money, version: int, is_blocked: bool. Методы: withdraw, deposit, block, unblock. Защита инвариантов.
- **DoD:** Тесты на все методы и ошибочные сценарии (блокировка, нехватка средств).
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `domain/entities/account.py` — `Account` как изменяемая сущность с тождеством по
    `AccountId`: приватные поля + read-only property, поэтому единственный путь изменить
    счёт — доменный метод, проверивший инварианты. `__slots__` запрещает завести
    «случайный» атрибут. Конструктор валидирует типы на границе: `AccountId` вместо
    «голого» UUID, `Money` вместо числа, строгий `bool` для `is_blocked` (иначе `1`
    тихо заблокировал бы счёт — `bool` подкласс `int`) и `int` для `version` c
    `MIN_VERSION = 1`. `INITIAL_VERSION = 1` совпадает с поведением SQLAlchemy
    `version_id_col`, чтобы в T-3.6 не возникло расхождения с БД.
  - `currency` — производное property от баланса, а не отдельное поле: валюта счёта
    неизменна и задаётся балансом. Пополнение/списание в чужой валюте отклоняются
    (`CurrencyMismatchError`) — неявной конвертации нет.
  - `deposit`/`withdraw` идут через общие проверки `_require_operable` →
    `_require_money` → `_require_same_currency` → `_require_non_zero`, каждая
    возвращает доменную ошибку, а не `AttributeError` из глубины. Проверка блокировки
    идёт **первой**: у заблокированного счёта «не хватает» всего, и отвечать на
    неверную сумму отказом по деньгам было бы ложью. `withdraw` при нехватке средств
    бросает `InsufficientFunds` с указанием доступного, запрошенного и недостающей
    суммы; минус на балансе невозможен (следствие инварианта `Money`).
  - `block`/`unblock` идемпотентны: повторная блокировка — «уже так», а не ошибка.
    Платежи приходят повторно (ретраи, вебхуки), и требование «сначала проверь
    состояние» выдало бы вызывающему инвариант наружу. `unblock` работает и на
    изначально заблокированном счёте — блокировка это заморозка, а не приговор.
  - **`version` растёт только при фактическом изменении состояния.** Отказы
    (`InsufficientFunds`, `AccountBlocked`, `CurrencyMismatchError`) и повторные
    `block`/`unblock` версию не трогают — иначе в поток оптимистичных блокировок
    попадали бы записи об изменениях, которых не было. Проверено тестом.
  - `has_sufficient_funds` — чистая проверка «хватит ли» без изменения состояния и
    без исключений (чужая валюта/не-Money → `False`), чтобы отклонять операцию до
    похода в БД. Документировано, что она не заменяет проверку в `withdraw`.
  - `domain/exceptions.py` — добавлены `InsufficientFunds` и `AccountBlocked`: без них
    сущность нечем защищать инварианты. Они наследуются только от `DomainError`, а не
    от `ValueError`, в отличие от ошибок разбора входа: это отказ бизнес-правила, и
    прикладной слой обязан отличать его от ошибки валидации. Остальные
    (`InvalidTransition`, `DuplicateOperation`, `PaymentProviderError`) остаются в T-1.5.
  - `pyproject.toml` — точечное отключение `N818` для `**/core_service/domain/exceptions.py`:
    линтер требует суффикс `Error`, но имена зафиксированы планом (T-1.5) как
    `InsufficientFunds`/`AccountBlocked`; переименовывать вопреки плану не стал.
    Паттерн с `**/` — потому что ruff запускается и из `backend/` (`make lint`), и из
    корня репозитория (pre-commit).
- **Проверки:** `make lint` (ruff + format + mypy --strict) — зелёные; `make test` — 184 passed;
  покрытие `src/core_service/domain` — 100 % (285/285 stmts); `make pre-commit` — все хуки зелёные.
- **Известный нюанс:** `Account` сравнивается и хешируется по `id`, а не по полям
  (тождество сущности, а не значение): два «прочитанных» счёта с разными балансами
  равны, если равны их идентификаторы. Хеш при этом стабилен при мутациях, поэтому
  счёт можно класть в `set`/`dict`.

### [x] T-1.3. Entity Payment + статус-машина
- **Что сделать:** Статусы: PENDING -> PROCESSING -> SETTLED / FAILED / CANCELLED. Метод `transition_to` строго валидирует переходы.
- **DoD:** Тесты на все валидные и недопустимые переходы.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `src/core_service/domain/exceptions.py` — добавлено бизнес-исключение `InvalidTransition(DomainError)`, сигнализирующее о попытке совершить недопустимый переход статуса в FSM платежа.
  - `value_objects/payment_status.py` — `PaymentStatus(StrEnum)` с полным набором статусов (`PENDING`, `PROCESSING`, `SETTLED`, `FAILED`, `CANCELLED`), проверкой терминальности `is_terminal` и картой допустимых переходов `ALLOWED_TRANSITIONS`.
  - `entities/payment.py` — сущность `Payment` с инкапсулированным состоянием через `__slots__` и read-only свойства: `id`, `from_account_id`, `amount`, `currency`, `status`, `version`, `provider_payment_id`, `failure_reason`, `created_at`, `updated_at`. Валидация типов (строгие `PaymentId`, `AccountId`, `Money`, запрет нулевой суммы, UTC datetime).
  - Конечный автомат в методе `transition_to`: строгая валидация допустимости перехода; при недопустимом переходе выбрасывается `InvalidTransition`, а состояние и версия остаются нетронутыми; при успехе инкрементируется версия оптимистичной блокировки `version` и обновляется `updated_at`.
  - Вспомогательные методы жизненного цикла: `process`, `settle`, `fail`, `cancel` и фабричный метод `Payment.create`.
  - Тождество сущности: `__eq__` и `__hash__` строго по `PaymentId` (стабильность хеша при мутациях).
  - `tests/unit/domain/test_payment.py` — матричные параметризованные тесты всех $5 \times 5 = 25$ комбинаций переходов (4 валидных, 21 невалидный), тесты на инварианты, инкремент версий, методы жизненного цикла и идентичность.
- **Проверки:** `make -C backend lint` (ruff + mypy --strict) — зелёные; `make -C backend test` — 235 passed; покрытие `src/core_service/domain` — 100% (421/421 stmts); `pre-commit` — все хуки пройдены.


### [x] T-1.4. Domain Events
- **Что сделать:** PaymentCreated, PaymentSettled, PaymentFailed, PaymentRefunded. Уникальные event_id (UUID4), timestamp occurred_at (UTC).
- **DoD:** Тесты на генерацию событий и их неизменяемость.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `src/core_service/domain/events/base.py` — базовый класс `DomainEvent` (`frozen=True`, `kw_only=True`). По умолчанию генерирует уникальный `event_id` (`UUID4`) и фиксирует текущее время `occurred_at` (строго timezone-aware UTC datetime). Защита от мутаций и строгая валидация типов.
  - `src/core_service/domain/events/payment.py` — доменные события жизненного цикла платежей:
    - `PaymentCreated`: `payment_id: PaymentId`, `from_account_id: AccountId`, `amount: Money`.
    - `PaymentSettled`: `payment_id: PaymentId`, `from_account_id: AccountId`, `amount: Money`, `provider_payment_id: str | None = None`.
    - `PaymentFailed`: `payment_id: PaymentId`, `from_account_id: AccountId`, `amount: Money`, `reason: str`.
    - `PaymentRefunded`: `payment_id: PaymentId`, `from_account_id: AccountId`, `refunded_amount: Money`, `reason: str | None = None`.
    - Валидация типов в `__post_init__` с выбросом `InvalidValueError`.
  - `src/core_service/domain/events/__init__.py`, `src/core_service/domain/__init__.py` — реэкспорт событий в доменный слой.
  - `tests/unit/domain/test_events.py` — unit-тесты на неизменяемость (`FrozenInstanceError`), генерацию UUID4/UTC, валидацию типов и параметров, наследование.
- **Проверки:** `make -C backend lint` (ruff + format + mypy --strict) — зелёные; `make -C backend test` — 259 passed; покрытие `src/core_service/domain` — 100% (497/497 stmts).


### [ ] T-1.5. Domain Exceptions
- **Что сделать:** DomainError -> InsufficientFunds, AccountBlocked, InvalidTransition, DuplicateOperation, PaymentProviderError.
- **DoD:** Unit-тесты на выброс и перехват исключений.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-1.6. Покрытие domain >= 90%
- **Что сделать:** Запустить `pytest --cov=core_service/domain`.
- **DoD:** Покрытие кода domain >= 90%.
- **Подтверждение пользователя:** `[ ]`
