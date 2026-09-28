"""Архитектурные стражей проекта: правила не дублируются, слои не текут (T-2.3).

Два независимых инварианта, оба ломаются тихо и поэтому проверяются автоматически:

1. **Одно правило — одна реализация** (AGENT.md §4.4). Через месяц кто-то напишет
   локальный ``_require_utc`` в новом модуле — и правило снова начнёт жить в двух
   местах, расходясь в самый неподходящий момент (например, одна копия разрешит
   offset-aware время, а вторая нет).
2. **Домен — чистый Python, application не знает инфраструктуру.** Регексом это
   не поймать: ``from src.core_service.domain...`` начинается так же, как запрещённый
   ``from src.core.config...``. Разбираем импорты через ``ast`` — точно.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = BACKEND_ROOT / 'src'
DOMAIN_ROOT = SRC_ROOT / 'core_service' / 'domain'
APPLICATION_ROOT = SRC_ROOT / 'core_service' / 'application'

#: Стандартная библиотека — единственное, кроме внутренних пакетов, что имеет
#: право импортировать домен.
STDLIB_MODULES = frozenset(sys.stdlib_module_names)

#: Модули, в которых по кодуексу положено определять функции-валидаторы.
#: ``payment_status.py`` в списке не случайно: правила шкалы статусов живут
#: вместе с ``ALLOWED_TRANSITIONS``, а не в «энциклопедии» общих правил.
ALLOWED_VALIDATION_MODULES = frozenset(
    {
        'src/core_service/domain/validation.py',
        'src/core_service/domain/value_objects/payment_status.py',
    }
)

#: Имена функций-валидаторов: такие функции запрещено заводить вне allowlist.
#: Проверяется только модульный уровень — методы сущностей (``_require_operable``,
#: ``_require_same_currency``) работают с состоянием объекта и дублями не являются.
FORBIDDEN_NAME_PATTERN = re.compile(r'^_?require_[a-z0-9_]+$')

#: Канонические формулировки сквозных правил: каждая должна встречаться ровно
#: в одном файле ``src/``. Список — «детектор» типовых дублей.
CANONICAL_RULE_TEXTS = (
    'ожидается экземпляр',  # строгая проверка типа
    'должен быть timezone-aware',  # метка времени строго в UTC
    'сумма не может быть нулевой',  # ненулевая сумма
    'ожидается непустая строка',  # непустой идентификатор/ключ
)

VALIDATION_PRIMITIVES = (
    'require_type',
    'require_int',
    'require_min_int',
    'require_utc',
    'require_non_empty_str',
    'require_optional_non_empty_str',
    'require_money',
    'require_non_zero_money',
    'require_positive_money',
)

#: Продублированные элементы, которые временно разрешены. Формат: ``'ключ' -> причина``.
#: Ключ совпадает с тем, что выводит страж в сообщении об ошибке. Причина обязательна
#: (проверяет ``test_allowlist_of_duplicates_is_documented``): иначе это не решение,
#: а молчаливое отключение проверки.
ALLOWED_DUPLICATES: dict[str, str] = {
    '%(levelname)s: %(name)s - %(message)s': (
        'формат логов объявлен в двух точках входа (app.py, run_migrations.py) — это код ЭПИКА 0, '
        'не входит в T-2.3; уедет в core/logging.py при подключении structlog (ЭПИК 12)'
    ),
}

#: Минимальная длина строкового литерала, который считаем «возможным дублем текста».
DUPLICATE_TEXT_MIN_LENGTH = 25


def _source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob('*.py'))


def _module_key(path: Path) -> str:
    return path.relative_to(BACKEND_ROOT).as_posix()


def _imported_modules(path: Path) -> set[str]:
    """Модули, которые файл импортирует (абсолютные; относительные пропускаются)."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _forbidden_imports(root: Path, allowed_prefixes: tuple[str, ...]) -> list[str]:
    """Импорты за пределами stdlib и разрешённых внутренних пакетов."""
    violations: list[str] = []
    for path in sorted(root.rglob('*.py')):
        for module in _imported_modules(path):
            if module.split('.')[0] in STDLIB_MODULES:
                continue
            if any(module == prefix or module.startswith(f'{prefix}.') for prefix in allowed_prefixes):
                continue
            violations.append(f'{_module_key(path)} -> {module}')
    return violations


# --- Правила: одна реализация на правило --------------------------------------


def test_validation_primitives_module_exists() -> None:
    """«Энциклопедия правил» существует и отдаёт весь ожидаемый набор примитивов."""
    from src.core_service.domain import validation

    assert validation.__doc__
    for name in VALIDATION_PRIMITIVES:
        assert callable(getattr(validation, name)), f'отсутствует примитив {name}'


def test_allowed_validation_modules_exist() -> None:
    """Allowlist не должен протухать: перечисленные модули реально на месте."""
    for module_key in ALLOWED_VALIDATION_MODULES:
        assert (BACKEND_ROOT / module_key).is_file(), module_key


def test_no_local_validation_helpers_outside_allowed_modules() -> None:
    """Никаких собственных ``_require_*`` вне allowlist — только переиспользование."""
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in tree.body:  # только модульный уровень, не методы классов
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not FORBIDDEN_NAME_PATTERN.match(node.name):
                continue
            if _module_key(path) not in ALLOWED_VALIDATION_MODULES:
                offenders.append(f'{_module_key(path)}:{node.name}')
    assert offenders == [], (
        'Локальные копии правил валидации запрещены (AGENT.md §5). '
        f'Перенесите правило в domain/validation.py и переиспользуйте его. Найдено: {offenders}'
    )


@pytest.mark.parametrize('rule_text', CANONICAL_RULE_TEXTS)
def test_cross_cutting_rule_is_implemented_exactly_once(rule_text: str) -> None:
    """Формулировка сквозного правила встречается ровно в одном файле ``src/``."""
    holders = [_module_key(path) for path in _source_files() if rule_text in path.read_text(encoding='utf-8')]
    assert len(holders) == 1, f'Правило {rule_text!r} продублировано в файлах: {holders}'


# --- Границы слоёв (Clean Architecture) ---------------------------------------


def test_domain_layer_is_pure_python() -> None:
    """Домен импортирует только стандартную библиотеку и сам себя.

    Запрещены и внешние библиотеки (pydantic, sqlalchemy, grpc, redis...), и
    внутренние слои: ``domain`` — самое независимое кольцо, оно не знает про
    application и инфраструктуру (AGENT.md §4.1).
    """
    violations = _forbidden_imports(DOMAIN_ROOT, ('src.core_service.domain',))
    assert violations == [], f'Домен перестал быть чистым Python: {violations}'


# --- Дубли в любых формах (AGENT.md §4.4) -------------------------------------

#: Проверяем весь production-код, а не только ``src/core_service``: дубли любят
#: селиться в entrypoint'ах, утилитах и скриптах миграций.
PRODUCTION_ROOTS = ('src', 'alembic', 'scripts')
TRIVIAL_STATEMENTS = (ast.Pass, ast.Raise)


def _production_files() -> list[Path]:
    return sorted(path for root in PRODUCTION_ROOTS for path in (BACKEND_ROOT / root).rglob('*.py'))


def _reexported_names(tree: ast.Module) -> set[str]:
    """Строковые константы из ``__all__``: это реэкспорт, а не дубль определения."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == '__all__' for target in node.targets
        ):
            names.update(elt.value for elt in getattr(node.value, 'elts', []) if isinstance(elt, ast.Constant))
    return names


#: Сосуды, внутрь которых запрятаны инструкции. Раньше тело целиком внутри одного
#: такого блока считалось «тривиальным» (верхнеуровневых инструкций меньше двух),
#: и любой код вида ``async with ...: if ...: raise ...; return`` выпадал из
#: проверки. На этом слепом месте разошлись две копии ``_read_payment``.
NESTING_STATEMENTS = (ast.With, ast.AsyncWith, ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)


def _real_statements(statements: list[ast.stmt]) -> int:
    """Считает содержательные инструкции, заходя внутрь вложенных блоков.

    Докстринги и ``pass`` не считаются: они не несут логики, ради которой функцию
    имеет смысл сравнивать с другой.
    """
    total = 0
    for stmt in statements:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, TRIVIAL_STATEMENTS):
            continue
        total += 1
        if isinstance(stmt, NESTING_STATEMENTS):
            total += _real_statements(list(ast.iter_child_nodes(stmt)))
    return total


def _meaningful_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Нормализованное тело функции либо ``None``, если содержательной логики нет.

    Тривиальны только заглушки (``...``/``pass``/``raise NotImplemented``) и тела
    с одной содержательной инструкцией (геттеры вида ``return self._x``). Проверка
    «if + return» содержательной считается: именно такие маленькие валидаторы
    копируют чаще всего, и именно их нельзя выпускать из-под контроля.

    Считается **с учётом вложенности**: весь код может лежать внутри одного
    ``async with``, и это не делает его заготовкой. Раньше такой случай выпадал
    из проверки, и дубль проходил незамеченным.
    """
    if _real_statements(list(node.body)) < 2:
        return None
    return ast.dump(ast.Module(body=list(node.body), type_ignores=[]))


def _scan() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]]:
    """Один проход по production-коду: константы, тела функций, текстовые литералы."""
    constants: dict[str, list[str]] = {}
    bodies: dict[str, list[str]] = {}
    texts: dict[str, list[str]] = {}
    for path in _production_files():
        module = _module_key(path)
        tree = ast.parse(path.read_text(encoding='utf-8'))
        exported = _reexported_names(tree)

        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                names = [node.target.id] if isinstance(node.target, ast.Name) else []
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            else:
                names = []
            if names and node.value is not None:
                signature = f'{" = ".join(names)} = {ast.unparse(node.value)}'
                constants.setdefault(signature, []).append(f'{module}:{node.lineno}')

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                signature = _meaningful_body(node)
                if signature is not None:
                    bodies.setdefault(signature, []).append(f'{module}:{node.lineno} {node.name}()')
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) >= DUPLICATE_TEXT_MIN_LENGTH
                and node.value not in exported
            ):
                texts.setdefault(node.value, []).append(f'{module}:{node.lineno}')
    return constants, bodies, texts


def _spread(locations: list[str]) -> int:
    """Сколько разных файлов занимает группа находок."""
    return len({location.split(':')[0] for location in locations})


def _forbidden(findings: list[tuple[str, list[str]]]) -> list[str]:
    """Отбрасывает находки, которые явно разрешены в ``ALLOWED_DUPLICATES``."""
    return [f'{key} -> {locs}' for key, locs in findings if key not in ALLOWED_DUPLICATES]


def test_no_duplicated_constants_with_same_value() -> None:
    """Константа с одинаковым именем и значением в двух файлах — дубль одного факта.

    Так были продублированы ``INITIAL_VERSION``/``MIN_VERSION`` (счёт и платёж):
    изменили в одном модуле — получили расхождение версий, которое всплывает
    уже на ``UPDATE`` в проде.
    """
    constants, _, _ = _scan()
    assert _forbidden(sorted((sig, locs) for sig, locs in constants.items() if _spread(locs) > 1)) == [], (
        'Константа объявлена в нескольких файлах — вынесите в один модуль'
    )


def test_no_identical_function_bodies() -> None:
    """Содержательные тела функций не должны совпадать в разных файлах."""
    _, bodies, _ = _scan()
    assert _forbidden(sorted((' | '.join(locs), locs) for locs in bodies.values() if _spread(locs) > 1)) == [], (
        'Одинаковые тела функций — вынесите в общий помощник'
    )


def test_no_duplicated_text_literals() -> None:
    """Один и тот же длинный текст в двух файлах — почти всегда копипаста.

    Так были продублированы тексты правил валидации. Строки из ``__all__`` —
    реэкспорт, а не дубль, поэтому не проверяются.
    """
    _, _, texts = _scan()
    assert _forbidden(sorted((text, locs) for text, locs in texts.items() if _spread(locs) > 1)) == [], (
        'Текст продублирован в нескольких файлах — вынесите в константу'
    )


def test_allowlist_of_duplicates_is_documented() -> None:
    """Разрешённый дубль обязан быть объяснён: иначе это не решение, а отключение."""
    undocumented = [key for key, reason in ALLOWED_DUPLICATES.items() if not reason.strip()]
    assert undocumented == [], f'В ALLOWED_DUPLICATES нет объяснения: {undocumented}'


def test_application_layer_does_not_import_infrastructure() -> None:
    """Application зависит только от домена и себя: ни БД, ни настроек, ни веба."""
    violations = _forbidden_imports(
        APPLICATION_ROOT,
        ('src.core_service.domain', 'src.core_service.application'),
    )
    assert violations == [], f'Прикладной слой протянул руку в инфраструктуру: {violations}'
