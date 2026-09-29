# ЭПИК 3: Postgres Infrastructure

> **Статус эпика:** `[ ] IN_PROGRESS`
> **Подтверждение пользователя:** `[ ] Подтверждено`

## Цель
Хранилище на async SQLAlchemy 2.0 + Alembic, оптимистичные блокировки по version, пессимистичные SELECT FOR UPDATE, outbox и testcontainers.
Архитектура: одиночный инстанс Postgres 16.

---

## Задачи эпика

### [x] T-3.1. SQLAlchemy модели
- **Что сделать:** Модели accounts, payments (provider_payment_id, provider_status), outbox, idempotency_keys, processed_events, provider_webhook_events.
- **DoD:** Схема описана, constraints и индексы на месте.
- **Подтверждение пользователя:** `[x]` (подтверждено)
- **Реализация:**
  - `src/db/models/` — пакет вместо прежнего одиночного `src/db/models.py`. Причина в том, что
    `alembic/env.py` делает `from src.db.models import Base`: если метаданные не собраны в
    одном месте, `autogenerate` увидит пустую схему и предложит пустую миграцию. `__init__.py`
    реэкспортирует `Base` и все шесть моделей — точка входа для Alembic одна.
  - `models/base.py` — корень декларативной модели. Владеет двумя вещами, которые иначе
    разъезжаются по слоям:
    - `NAMING_CONVENTION` — шаблоны имён `pk`/`fk`/`uq`/`ck`/`ix`. Ручные имена в моделях
      однажды дали бы два `CHECK` с одинаковым именем, и `autogenerate` стал бы предлагать
      миграции, которые ничего не меняют;
    - ширины колонок, **посчитанные из домена**: `MONEY_PRECISION = MAX_MAGNITUDE + MONEY_SCALE`,
      где `MONEY_SCALE` — максимум `Currency.minor_unit` (у KWD три знака, у JPY ноль), плюс
      длина колонки `idempotency_keys.key` из `MAX_IDEMPOTENCY_KEY_LENGTH` порта. Проверяемое
      свойство: любая валидная по домену сумма помещается в колонку. Миксины `CreatedAtMixin` /
      `UpdatedAtMixin` — общее время жизни строки.
  - `models/account.py` — `accounts`: `id` UUID, `balance NUMERIC(27,3)`, `currency VARCHAR(3)`
    с `CHECK` по `Currency`, `is_blocked`, `version`. Валюта — `VARCHAR` + `CHECK`, а не
    PostgreSQL `ENUM`: `ALTER TYPE` на изменение типа блокирует таблицу, а добавить валюту в
    шлюзе должно быть обычной миграцией. `CHECK` по балансу `>= 0` и по версии `>= MIN_VERSION`.
    Индексов на `currency`/`is_blocked` сознательно нет — выборки идут по `id`.
  - `models/payment.py` — `payments`: FK на счёт с `ON DELETE RESTRICT` (каскад стёр бы историю
    движения денег), `amount > 0` (нулевой платёж — ошибка сценария), `CHECK` по `PaymentStatus`
    и по `ProviderStatus`, уникальный `provider_payment_id`, `version` по тем же правилам, что у
    счёта. **Валюта платежа не хранится**: она принадлежит счёту, а дубликат однажды разошёлся бы
    с ним. Индексы `ix_payments_from_account_id` (история по счёту) и
    `ix_payments_status_updated_at` (выборка «зависших» платежей для сверки).
  - `models/outbox.py` — `outbox`: PK = `event_id` доменного события, поэтому повторная запись
    дубля отсекается самой БД; `payload JSONB`; `occurred_at`; `published_at` вместо булева
    `published` (из него напрямую считается лаг outbox для дашборда T-12.8); `attempts` и
    `last_error` для ретраев relay'я (T-8.1). Ключевой индекс **частичный**:
    `ix_outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL` — relay опрашивает
    только неопубликованные, поэтому индекс держится на размере очереди, а не архива.
  - `models/idempotency_key.py` — `idempotency_keys`: `key` PK шириной из порта, `request_hash`
    (`sha256`-совместимая длина), `response JSONB` (nullable = «ключ занят, ответа ещё нет»,
    это состояние отличает повтор от первого захода), `expires_at` с индексом под очистку T-4.4
    и `CHECK expires_at > created_at`.
  - `models/processed_event.py` — `processed_events`: составной PK `(consumer, event_id)` вместо
    счётчика — «уже обработано этим консьюмером» проверяется самой БД, без гонки между
    «проверил — вставил». Индекс на `processed_at` под уборку.
  - `models/provider_webhook_event.py` — `provider_webhook_events`: PK = `provider_event_id`
    (он же ключ идемпотентности T-2.7, поэтому повторная доставка отсекается на БД), сырое тело
    в `JSONB` целиком (провайдеры расширяют формат — «разобрали и отбросили» означает, что
    через месяц разобрать уже нечего), `CHECK` по `ProviderStatus`, запрет пустого
    `provider_payment_id`, индекс на `received_at` под разбор инцидентов.
  - `tests/unit/db/test_models.py` — 34 теста по метаданным, без БД: состав таблиц ровно по
    плану, выводы ширин из домена, все `CHECK`/FK/UNIQUE/индексы, частичность индекса outbox и
    рендеринг каждой таблицы в DDL. Ключевая идея: проверяется не «колонка `NUMERIC(27,3)`»
    (это зафиксировало бы магическое число), а «колонка вмещает любую валидную сумму» — добавят
    валюту с тремя знаками, и тест упадёт, если про неё забыли.
- **Проверки:** `make -C backend lint` — зелёные (`All checks passed!`, `57 files already
  formatted`, `Success: no issues found in 56 source files`); `make -C backend test` — **717
  passed** (683 прежних без правок + 34 новых); `make -C backend pre-commit` — все хуки зелёные.
- **Нюансы:**
  - **Архитектурный страж сработал на первой же версии и это к лучшему:** строки
    `provider_status_enum_values` оказались продублированы в `payment.py` и
    `provider_webhook_event.py`. Лечение — не `ALLOWED_DUPLICATES` (подавление симптома), а
    изменение API хелпера: имя `CHECK` выводится из имени колонки (`ENUM_CONSTRAINT_SUFFIX` в
    `base.py`), и правило «проверка называется по своей колонке» стало одно на все модели.
  - `CHECK` рендерятся с bind-параметрами, поэтому в тестах текст собирается с `literal_binds` —
    только так видны значения, которые действительно уедут в базу, и их можно сравнить с доменным
    перечислением.
  - Схема проверена рендерингом в PostgreSQL DDL, но **не применена к живой базе**: это задачи
    T-3.2 (миграции) и T-3.7 (testcontainers). Здесь заявляется форма, а не поведение.
  - Старый `src/db/models.py` удалён, а не переименован: он содержал `Base` с `__abstract__` и
    колонкой `created_at`, которые новый `base.py` заменяет миксинами с `server_default`/`onupdate`.
  - `session_make.py` и `run_migrations.py` не тронуты: перевод Alembic на async-паттерн — T-3.2,
    и он удалит помеченный в `config.py` `SQLALCHEMY_SYNC_DB_URL`.
  - **Известный дубль, оставленный осознанно (решение пользователя при закрытии T-3.1).**
    Строка `'version_positive'` встречается в `account.py` и `payment.py`. Страж её не ловит:
    `DUPLICATE_TEXT_MIN_LENGTH = 25`, а строка короче — в отличие от
    `provider_status_enum_values`, который поймался. Коллизии в Postgres нет (имена получаются
    разными за счёт префикса таблицы: `ck_accounts_version_positive` / `ck_payments_version_positive`),
    и само правило хранится в одной константе `MIN_VERSION` — расходиться нечему. Правильное
    лечение, когда дойдёт очередь: вынести проверки версии и положительности суммы в хелперы
    `base.py` по образцу `enum_values_constraint`. Проверить, не вернулось ли правило в
    `ALLOWED_DUPLICATES`: там его нет, и добавлять нечего — страж такого дубля не находит.
  - Проверено на живой БД (временные таблицы в контейнере `pay-postgres`, затем удалены):
    `NUMERIC(27,3)` принимает 27 знаков и отвергает 28 с `numeric field overflow`; при нехватке
    `scale` Postgres **молча округляет** (`NUMERIC(27,2)` превращает `1.999` в `2.00`) — именно
    поэтому `MONEY_SCALE` берётся как `max(Currency.minor_unit)`, а не из `DEFAULT_MINOR_UNIT`:
    тихое округление денег опаснее явной ошибки. Также подтверждено, что `NULL` проходит `CHECK`
    (`NULL IN (...)` даёт не `FALSE`), поэтому nullable-колонка `provider_status` законно
    остаётся пустой до вызова провайдера, а мусорное значение отсекается.


### [ ] T-3.2. Alembic setup + первая миграция
- **Что сделать:** Настроить асинхронный env.py alembic, сгенерировать начальную миграцию.
- **DoD:** `alembic upgrade head` и `alembic downgrade base` работают корректно.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.3. AccountRepository на Postgres
- **Что сделать:** Реализация AccountRepository: get_for_update (SELECT ... FOR UPDATE), add, update.
- **DoD:** Интеграционный тест репозитория.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.4. PaymentRepository на Postgres
- **Что сделать:** Сохранение, поиск по id, фильтрация по статусу.
- **DoD:** Интеграционные тесты.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.5. UnitOfWork
- **Что сделать:** Асинхронный контекстный менеджер UoW: commit, rollback при исключениях.
- **DoD:** Тест атомарности транзакции.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.6. Оптимистичный лок через version
- **Что сделать:** UPDATE accounts SET balance=..., version=version+1 WHERE id=... AND version=current_version. Если 0 строк - OptimisticLockError.
- **DoD:** Тест на ошибку при гонке версий.
- **Подтверждение пользователя:** `[ ]`

### [ ] T-3.7. Testcontainers
- **Что сделать:** Pytest фикстура с Postgres testcontainer.
- **DoD:** `pytest tests/integration/` работает автономно.
- **Подтверждение пользователя:** `[ ]`
