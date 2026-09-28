# ЭПИК 2: Application Layer (Use-cases & Ports)

> **Статус эпика:** `[ ] TODO`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Оркестрация бизнес-логики через чистые порты и юзкейсы.
Порт PaymentProvider для внешнего эквайринга, сагаподобный флоу платежа с вызовом провайдера вне БД-транзакции.

---

## Задачи эпика

### [x] T-2.1. Порты
- **Что сделать:** Создать Protocol интерфейсы: AccountRepository, PaymentRepository, UnitOfWork, LockManager, IdempotencyStore, EventPublisher, PaymentProvider, Clock.
- **DoD:** `mypy --strict` успешен.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `application/ports/` — контракты инфраструктуры как `typing.Protocol` + `@runtime_checkable`. Реализациям
    не нужно наследоваться от портов: достаточно совпасть по «форме» (structural typing), что и проверяют тесты.
  - `ports/clock.py` — `Clock.now()` (синхронный: чтение времени не требует `await`, в тестах время замораживается).
  - `ports/account_repository.py` — `AccountRepository`: `get`, `get_for_update` (`SELECT ... FOR UPDATE`,
    блокировка строки до конца транзакции), `add`, `update`. Коммит репозитории не делают — это граница `UnitOfWork`.
  - `ports/payment_repository.py` — `PaymentRepository`: `get`, `get_by_provider_payment_id` (для вебхуков),
    `add`, `update`, `find_by_status(status, *, limit, updated_before)` (выборка «зависших» платежей для сверки).
  - `ports/unit_of_work.py` — `UnitOfWork`: `accounts`/`payments` (property) + `__aenter__`/`__aexit__`/`commit`/`rollback`
    (`__aenter__ -> Self`). Транзакционная граница, чтобы баланс + платёж + outbox фиксировались атомарно.
  - `ports/lock_manager.py` — `LockManager` (`lock` → `AbstractAsyncContextManager[DistributedLock]`, `acquire`, `release`)
    и `DistributedLock` (`resource`, `token`, `is_held`, `release`, `__aenter__`/`__aexit__`). `lock` синхронный —
    он лишь возвращает контекстный менеджер, захват происходит в `__aenter__`.
  - `ports/idempotency_store.py` — `IdempotencyStore` (`get`, `save`, `try_acquire` = `SET NX`, `release`) и
    `IdempotencyRecord` (`frozen`-dataclass; `request_hash` ловит конфликт «тот же ключ, другое тело»).
  - `ports/event_publisher.py` — `EventPublisher.publish(DomainEvent)` — абстракция outbox.
  - `ports/payment_provider.py` — `PaymentProvider` (`create_payment(*, payment_id, amount, idempotency_key)`,
    `get_status`, `refund`), `ProviderResult`, `ProviderStatus` (отдельная шкала статусов провайдера).
  - `application/__init__.py`, `ports/__init__.py` — реэкспорт портов наружу слоя.
  - `tests/unit/application/test_ports.py` — 60 архитектурных (contract) тестов: `_is_protocol` / `_is_runtime_protocol`,
    наличие и `async`-ность методов (`inspect.iscoroutinefunction`), `property` (`inspect.getattr_static`),
    прохождение `isinstance(fake, Port)` для класса без наследования, негативная проверка, неизменяемость
    `ProviderResult`/`IdempotencyRecord` и валидация их значений.
- **Проверки:** `make -C backend lint` (ruff check + format check + mypy --strict) — зелёные; `make -C backend test` — 370 passed.
- **Нюансы:**
  - `PaymentProvider` объявлен уже в T-2.1 (он есть в списке портов задачи), поэтому его минимальный контракт и
    типы `ProviderResult`/`ProviderStatus` определены здесь — иначе пакет не импортируется и `mypy --strict` не проходит.
    Детальная финализация контракта — T-2.2, полный DTO-пакет (может реэкспортировать эти типы) — T-2.3.
  - В `create_payment` использован доменный `PaymentId` вместо `UUID` из формулировки T-2.2 — строже и консистентно с доменом.
  - `PaymentRepository.get_by_provider_payment_id` добавлен под обработку вебхуков (T-2.7).

### [x] T-2.2. Порт PaymentProvider
- **Что сделать:** Интерфейс Protocol:
  - `create_payment(*, payment_id: UUID, amount: Money, idempotency_key: str) -> ProviderResult`
  - `get_status(provider_payment_id: str) -> ProviderStatus`
  - `refund(provider_payment_id: str, amount: Money) -> ProviderResult`
- **DoD:** Документированный контракт и строгие типы.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `ports/payment_provider.py` — контракт внешнего эквайринга финализирован (в T-2.1 типы
    `ProviderResult`/`ProviderStatus` и методы были объявлены минимально, потому что пакет портов без них
    не собирался). Три операции зафиксированы ровно в таком составе: `create_payment` (keyword-only,
    `PaymentId` + `Money` + `idempotency_key: str` → `ProviderResult`), `get_status(str) -> ProviderStatus`,
    `refund(str, Money) -> ProviderResult`.
  - Принят **контракт ошибок** — главное различие, которого не хватало: технический отказ (сеть, таймаут,
    5xx, неразбираемый ответ) реализация поднимает доменным `PaymentProviderError`, а бизнес-отказ
    (провайдер отклонил операцию) возвращается как `ProviderResult(status=FAILED)` и **не** является
    исключением. Исключение = «исход неизвестен», `FAILED` = «знаем: отклонено».
  - Зафиксирован **контракт идемпотентности**: `idempotency_key` стабилен между повторами и строится
    адаптером из `PaymentId`; `get_status` идемпотентен по природе; `refund` не допускает повторного
    возврата уже возвращённой суммы.
  - Задокументирована **карта шкал статусов**: адаптер приводит «сырые» значения шлюза к закрытому
    `ProviderStatus`, а прикладной слой переводит `ProviderStatus` → `PaymentStatus`
    (`PENDING→PENDING`, `PROCESSING→PROCESSING`, `SUCCEEDED→SETTLED`, `FAILED→FAILED`,
    `REFUNDED→SETTLED` + событие `PaymentRefunded`). Терминальные у провайдера: `SUCCEEDED`, `FAILED`,
    `REFUNDED` — после них опрос статуса не нужен.
  - В docstring модуля описано **место вызова в саге**: `create_payment` вызывается строго вне
    транзакции БД (между коммитами UoW#1 и UoW#2), чтобы не держать блокировки строк на время ответа
    внешней системы (AGENT.md §4.2). Дедлайны/ретраи/circuit breaker навешиваются снаружи (ЭПИК 11).
  - `tests/unit/application/test_payment_provider_port.py` — 19 contract-тестов: точные сигнатуры через
    `inspect.signature` (`KEYWORD_ONLY` у `create_payment`, аннотации тождественны `PaymentId`/`Money`/`str`,
    `return_annotation` тождествен `ProviderResult`/`ProviderStatus`), закрытость состава операций,
    документированность (наличие `:param`/`:returns:`/`:raises PaymentProviderError:` у методов и текста
    контракта ошибок в docstring порта), модель ошибок (`PaymentProviderError ⊂ DomainError`), закрытость
    шкалы `ProviderStatus`, строгость `ProviderResult` (`slots` + `frozen`, обязательность обоих полей,
    отказ на `''`/`'   '`/`None`/`123`) и структурное соответствие фейка порту без наследования.
- **Проверки:** `make -C backend lint` (ruff check + format check + mypy --strict) — зелёные
  (`36 source files`, `37 files already formatted`); `make -C backend test` — 389 passed.
- **Нюансы:**
  - В `create_payment` сохранён доменный `PaymentId` вместо `UUID` из формулировки задачи: решение T-2.1
    (консистентность с доменом и запрет на протаскивание сырых UUID) подтверждено тестом на аннотацию.
  - `ProviderResult`/`ProviderStatus` остались в модуле порта, как их ввёл T-2.1; DTO-пакет (T-2.3) может
    их реэкспортировать, не меняя контракт. Нового поведения в типах T-2.2 не добавляет (задел под T-2.3).
  - Тест «ровно три операции» сознательно ломается при добавлении операции в порт: это защита от
    случайного расширения контракта, правка теста — часть легального изменения порта.

### [x] T-2.3. DTO
- **Что сделать:** CreatePaymentInput/Output, GetPaymentInput, ProviderResult, ProviderStatus.
- **DoD:** Валидация и строгая типизация.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `application/dto/payment.py` — три DTO как `@dataclass(frozen=True, slots=True, kw_only=True)`
    с валидацией в `__post_init__` (тот же стиль, что у типов, пересекающих порты: `ProviderResult`,
    `IdempotencyRecord`):
    - `CreatePaymentInput(from_account_id: AccountId, amount: Money, idempotency_key: str)` — вход саги
      создания платежа (T-2.4);
    - `CreatePaymentOutput(payment_id: PaymentId, status: PaymentStatus, amount: Money, created_at: datetime,
      provider_payment_id: str | None = None)` — результат саги (обычно `PROCESSING`, при отказе провайдера — `FAILED`);
    - `GetPaymentInput(payment_id: PaymentId)` — вход чтения платежа (T-2.5).
  - **Валидация** (DoD): строгие проверки типов полей с доменными ошибками вместо
    `TypeError`/`AttributeError`; нулевая сумма -> `InvalidAmountError`; `idempotency_key`
    нормализуется (`strip`) и отвергается при пустоте и длине >
    `MAX_IDEMPOTENCY_KEY_LENGTH = 255` (граница хранилища ключей, ЭПИК 4);
    `provider_payment_id` — непустая строка или `None`; `created_at` — только timezone-aware UTC.
    Проверки берутся из `domain/validation.py` — единого источника правил (AGENT.md §4.4),
    локальных копий в DTO нет.
  - **Согласованность статуса и идентификатора провайдера**: правило
    `require_provider_payment_coherence` (живёт рядом со статус-машиной,
    `value_objects/payment_status.py`):
    для `PROCESSING`/`SETTLED` (набор `PROVIDER_BOUND_PAYMENT_STATUSES`) `provider_payment_id` обязателен,
    а для `PENDING` запрещён. Так маппер, «забывший» перенести идентификатор операции, падает на границе
    сценария, а не при обработке вебхука.
  - `application/dto/__init__.py` — реэкспорт DTO + **реэкспорт `ProviderResult`/`ProviderStatus` из порта**
    (`is`-идентичность закреплена тестом): один источник правды, дублей типов провайдера в DTO нет.
  - `application/__init__.py` — DTO добавлены в единую точку экспорта слоя (`CreatePaymentInput`,
    `CreatePaymentOutput`, `GetPaymentInput`, `MAX_IDEMPOTENCY_KEY_LENGTH`).
  - `tests/unit/application/test_dto.py` — 57 тестов: строгая типизация через `dataclasses.fields()`
    (типы полей тождественны доменным VO, все поля `kw_only`), иммутабельность (`slots` + `FrozenInstanceError`),
    обязательность полей, валидация каждого поля (не-`AccountId`/`Money`/`PaymentId`/`PaymentStatus`, ноль,
    пустой/длинный ключ, обрезка пробелов, наивное и не-UTC время, пустой `provider_payment_id`),
    согласованность статуса и `provider_payment_id`, реэкспорт портовых типов и таксономия ошибок
    (валидация поднимает `DomainError`).
- **Проверки:** `make -C backend lint` (ruff check + format check + mypy --strict) — зелёные;
  `make -C backend test` — все тесты проходят.
- **Нюансы:**
  - **Pydantic в ядре не используем осознанно**: DTO прикладного слоя — иммутабельные dataclass'ы с ручной
    валидацией. Pydantic-схемы остаются уровнем BFF (T-7.1), чтобы ядро не зависело от веб-фреймворка,
    а транспорт не диктовал форму доменного контракта.
  - DTO принимают **доменные типы** (`AccountId`, `Money`), а не «сырые» `str`/`UUID`/`Decimal`: разбор
    входа — задача транспортного адаптера (ЭПИК 6/7), поэтому в ядре нет повторной валидации UUID/валюты.
  - Мапперы (`from_payment`) не добавлялись: преобразование сущности в DTO — работа сценария (T-2.4/T-2.5),
    и её форма зависит от фактического флоу саги.
  - DTO для отмены (`T-2.6`), вебхука (`T-2.7`) и стрима (`T-2.8`) не создавались: в списке T-2.3 их нет,
    при необходимости они появятся вместе со своими сценариями.

### [x] T-2.4. UseCase CreatePayment
- **Что сделать:** Сагаподобный флоу:
  1. Idempotency check.
  2. Redis lock на from_account.
  3. UoW: списание с аккаунта + создание Payment(PENDING) + outbox PaymentCreated. Commit.
  4. Вне транзакции: вызов PaymentProvider.create_payment.
  5. Новый UoW: обновление статуса (PROCESSING / FAILED) + provider_payment_id + outbox. Commit.
  6. Сохранение Idempotency ответа.
- **DoD:** Тесты с FakePaymentProvider: успех, ошибка провайдера, дубликат ключа.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `application/use_cases/create_payment.py` — `CreatePaymentUseCase` с шагами 1-6 ровно по
    плану. Ключевая структура: **две транзакции и сетевой вызов между ними**. Вызов
    провайдера физически находится вне обоих `async with` — это и есть требование
    AGENT.md §4.2, проверяется тестом «во время вызова провайдера не открыта транзакция»
    (фейк провайдера запоминает состояние фабрики UoW).
  - **Блокировка охватывает только списание** (шаг 3), а не весь флоу: после коммита деньги
    уже списаны, и удерживать ресурс на время ответа шлюза незачем — это сужает окно для
    других платежей того же счёта и обесценивает TTL блокировки. Ресурс блокировки
    строится из `ACCOUNT_LOCK_RESOURCE_PREFIX` (владелец формата — порт `LockManager`, а не
    сценарий: по нему же строятся ключи Redis в ЭПИК 5).
  - **Два вида отказа провайдера разведены** по контракту T-2.2, и различие решает судьбу денег:
    - **бизнес-отказ** (`ProviderResult(FAILED)`) — исход **известен** (шлюз отклонил, деньги у него
      не забраны): платёж → `FAILED` + события `PaymentFailed` **и возврат холда** на счёт
      (`PaymentRefunded`), всё в одной транзакции;
    - **технический отказ** (`PaymentProviderError`) — исход **неизвестен**: платёж остаётся
      `PENDING`, холд **не возвращается** (возврат при неизвестном исходе = риск двойного
      возврата), наружу уходит ошибка. Разрулит сверка (T-2.10) по `provider_payment_id`.
  - **Ключ идемпотентности освобождается только если операция не состоялась** (отказ на
    шаге 3). Как только списание зафиксировано, ключ остаётся захваченным: иначе повтор
    создал бы второй платёж и списал сумму дважды. Отдельно сохранён ответ даже при
    техническом сбое провайдера — операция уже создана, повтор обязан вернуть её, а не
    начать новую.
  - `request_hash` считается по смысловой части запроса (счёт + сумма): повтор с тем же
    ключом и тем же телом обязан узнать свой же хэш. Тот же ключ с другим телом —
    `DuplicateOperation` (конфликт, а не повтор).
  - `dto/payment.py` — `CreatePaymentOutput.from_payment()` (маппер, отложенный T-2.3) и
    `to_idempotency_response()`/`from_idempotency_response()`. Формат ответа хранилища —
    факт с владельцем, поэтому живёт рядом с DTO, а не собирается по месту вызова;
    повторный ответ проходит ту же валидацию, что и свежий.
  - `domain/exceptions.py` — добавлен `EntityNotFoundError` («счёта/платежа нет»):
    идентификатор корректен, записи просто нет, поэтому это не подкласс `ValueError`
    (иначе «исправь запрос» смешивалось бы с «404 не найдено»). Добавление минимально и
    продиктовано сценарием: «счёт не найден» — бизнес-отказ, а не ошибка разбора входа.
  - `tests/unit/application/fakes.py` — in-memory фейки портов + настраиваемый
    `FakePaymentProvider` (status/error) и `SagaEnvironment`. Фейки **не наследуют**
    порты: совпадение «по форме» проверяется и не может молча сойтись.
  - `tests/unit/application/test_create_payment.py` — 48 тестов: DoD (успех, бизнес- и
    технический отказ провайдера, дубликат ключа, конфликт тела по тому же ключу,
    параллельная резервация), инварианты саги (провайдер вне транзакции, две отдельные
    транзакции, `get_for_update`, освобождение блокировки на всех путях, порядок шагов по
    журналу), денежный путь (возврат холда при отказе, отсутствие возврата при
    неизвестном исходе, атомарность возврата и статуса) и доменные отказы
    (недостаточно средств, заблокированный счёт, чужая валюта, нет счёта) + защитные
    ветки (потеря платежа между транзакциями, потеря счёта при возврате, запись без ответа).
- **Проверки:** `make -C backend lint` (ruff check + format check + mypy --strict) — зелёные
  (`44 files already formatted`, `Success: no issues found in 43 source files`);
  `make -C backend test` — 552 passed; покрытие `application` — 100%
  (`create_payment.py` 101 строка, 0 пропусков); 13 архитектурных стражей — зелёные
  (в т.ч. поймали дубль `__all__` в `use_cases/__init__.py` и `create_payment.py`).
- **Нюансы:**
  - `UnitOfWork` внедряется как **фабрика** (`Callable[[], UnitOfWork]`), а не как
    единственный экземпляр: саге нужны две транзакции. Сам порт не менялся.
  - Признак «платёж исчез между транзакциями» поднят как `EntityNotFoundError`, а не
    «успех с пустым результатом»: молчаливый успех означал бы потерянные деньги.
  - **Холд (зарезервированные под платёж деньги) и его возврат.** Деньги списываются
    вперёд (шаг 3) и живут на счёте как «холд» до терминального статуса. Отказ
    провайдера по существу — единственный исход, где исход **известен наверняка**, и
    холд возвращается в той же транзакции, что и `FAILED` (метод `_reject_payment`).
    При техническом сбое холд **остаётся**: возвращать при неизвестном исходе — значит
    рисковать двойным возвратом, если провайдер операцию всё-таки провёл.
  - **Контракты фейков сверены с портами по сигнатурам** — mypy проверяет и тестовые
    doubles (они достижимы из `scripts/`), и изначально нашёл расхождения: `lock()`
    с `**_: object` вместо `ttl_seconds`/`wait_seconds` и `release(FakeDistributedLock)`
    вместо `release(DistributedLock)`. Оба исправлены: фейк, «проглатывающий» лишние
    аргументы, проверял бы не тот контракт, который предъявит настоящий адаптер.
  - Реальные (переносимые) фейки портов и `FakePaymentProvider` с настройкой success/failure/delay
    остаются за **T-2.9**: здесь только тестовые doubles, чтобы не забегать вперёд.
  - `payment_provider.py` пополнился константой `TERMINAL_PROVIDER_STATUSES` — она кодирует
    уже задокументированный в модуле факт («после SUCCEEDED/FAILED/REFUNDED опрос не нужен»),
    чтобы сценарий и будущая сверка отсекали опрос по одному правилу.

### [x] T-2.5. UseCase GetPayment
- **Что сделать:** Получение платежа + актуализация через провайдера при зависании в PROCESSING.
- **DoD:** Тест сценария.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `use_cases/get_payment.py` — `GetPaymentUseCase`: прочитать платёж → если он
    «завис» в `PROCESSING`, спросить провайдера и зафиксировать изменившийся статус.
    Схема из четырёх шагов: чтение (транзакция закрывается) → сетевой вызов (без
    транзакции) → перечитывание и запись (новая транзакция) → возврат DTO.
  - **Терминальные платежи не опрашиваются** — исход известен, поход в сеть стоит
    денег провайдера и задержки клиенту. `PENDING` тоже: идентификатора операции у
    него ещё нет, спрашивать некого. Решение о походе принимает
    `_provider_operation_id` — единственное место с этим правилом.
  - **Актуализация возвращает деньги при отказе.** Правило «ответ провайдера →
    статус платежа + возврат холда» вынесено в `use_cases/payment_sync.py` и
    теперь общее для T-2.4, T-2.5 и будущего вебхука (T-2.7). Дублировать его в
    чтении было бы прямой ошибкой: забытый в одной копии возврат стоит клиенту
    денег за отклонённый платёж.
  - **Две гонки обработаны явно.** Между чтением и ответом провайдера вебхок мог
    закрыть платёж: тогда переход неприменим, и сценарий отдаёт актуальное
    терминальное состояние вместо 500-го перехода. Если платёж исчез — `EntityNotFoundError`,
    а не устаревший ответ.
  - `dto/payment.py` — добавлен `GetPaymentOutput` (в T-2.3 существовал только вход).
    Отдельный тип, а не переиспользование `CreatePaymentOutput`: чтению нужны обе
    временные метки, счёт-источник и причина отказа — без них клиент угадывал бы,
    почему платёж `FAILED`.
  - `domain/value_objects/payment_status.py` — `require_time_order()`: правило
    «`updated_at` не раньше `created_at`» было продублировано в сущности и в DTO,
    что поймал архитектурный страж (`test_no_duplicated_text_literals`). Правило
    принадлежит предметной области, поэтому переехало туда и переиспользуется обоими.
  - `fakes.py` — у `FakePaymentProvider` разделены ответы `create_payment` и
    `get_status` (`status_query`/`status_query_error`): иначе нельзя выразить
    «создали успешно, а при актуализации выяснился отказ» — сам частый случай в
    чтении. Также `SagaEnvironment.build_get_use_case()`.
  - `test_get_payment.py` — 26 тестов: чтение (DoD), кого и когда спрашиваем,
    инвариант «сеть вне транзакции», промежуточные ответы (ничего не меняем,
    лишнего коммита нет), успешная актуализация, **возврат холда при отказе**,
    повторное чтение (деньги не вернулись второй раз), гонки с вебхуком и
    исчезновением платежа, технический сбой и восстановление после него.
    Платёж в тестах создаётся настоящей сагой T-2.4, а не вручную собранной
    фикстурой, — иначе тестировалось бы не то, что работает в бою.
- **Проверки:** `make -C backend lint` — зелёные (`46 files already formatted`,
  `Success: no issues found in 45 source files`); `make -C backend test` — 578 passed;
  покрытие `application` — 100% (`get_payment.py` 50 строк, `payment_sync.py` 28 строк,
  0 пропусков); 13 архитектурных стражей — зелёные.
- **Нюансы:**
  - `CreatePaymentUseCase` переведён на `payment_sync.apply_provider_status`. Рефакторинг
    не изменил поведение — все 48 тестов T-2.4 прошли сразу после выноса, что и было
    проверкой корректности выноса.
  - `PROVIDER_REJECTION_REASON` переехал в `payment_sync`: он нужен всем сценариям,
    работающим с ответом провайдера, и держать его в `create_payment` было бы
    случайным выбором модуля.
  - **Ограничение по таймингу:** актуализация происходит по факту чтения, а не по
    расписанию. Платеж, который никто не читает, останется `PROCESSING` до сверки
    (T-2.10) или вебхука (T-2.7) — это осознанно, фоновой сверки в T-2.5 нет.
  - `Clock` внедрён в сценарий, но не используется: метки проставляет сам платёж.
    Оставлен ради единообразия с T-2.4 и для будущих задач чтения; в коде это
    отмечено явно, чтобы не выглядело забытым аргументом.
  - Тест `test_webhook_won_race_does_not_break_read` проверен мутацией: при замене
    ожидания на `FAILED` он падает, то есть действительно ловит применение
    перехода к уже закрытому платежу, а не «проходит всегда».

### [ ] T-2.6. UseCase CancelPayment
- **Что сделать:** Отмена для PENDING, возврат средств на баланс, генерация события отмены.
- **DoD:** Тесты на успех и отказ.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.7. UseCase HandleProviderWebhook
- **Что сделать:** Обработка webhook: поиск платежа, перевод в SETTLED/FAILED, запись outbox. Идемпотентность по provider_event_id.
- **DoD:** Тест: повторный webhook дает тот же результат без дублирования.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.8. UseCase WatchPayment
- **Что сделать:** Async generator потока статусов платежа.
- **DoD:** Unit-тест генератора.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.9. In-memory fakes
- **Что сделать:** Реализация фейков портов + FakePaymentProvider (настраиваемый: success/failure/delay).
- **DoD:** Запуск юзкейсов без внешней инфры.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-2.10. Покрытие application >= 85%
- **DoD:** `pytest --cov=core_service/application` показывает >= 85%.
- **Подтверждение пользователя:** `[ ]`
