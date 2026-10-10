"""Тесты генерации gRPC-контрактов (T-6.2).

Задача T-6.2 — не «скомпилировалось один раз», а «генерация устроена правильно»,
и правильность здесь проверяется на четырёх свойствах:

1. **Контракт собран.** В сгенерированном коде есть ``PaymentsService`` со всеми
   пятью методами, а ``WatchPayment`` — именно server-streaming (T-6.1);
2. **Код лежит в пакете проекта.** Импорт идёт через
   ``src.core_service.interfaces.grpc.generated.*``, а не через топ-левел
   ``payments``. Это не вкусовое требование: сгенерированный стаб импортирует
   соседний модуль по абсолютному пути, и топ-левел импорт просто не находится
   в sys.path, то есть сервер не поднялся бы вовсе;
3. **Пакет не закоммичен.** Иначе в репозитории появился бы второй, устаревший
   источник правды о контракте, рано или поздно разошедшийся с ``.proto``;
4. **Генерация идемпотентна и быстра** — ровно это и записано в DoD задачи.

Пути в тестах **не выписаны руками**: они импортируются из самого генератора
(:mod:`scripts.gen_proto`). Дублировать их здесь нельзя — «факт с владельцем»
(AGENT.md §4.4): сменился каталог генерации, и тест должен упасть на правом
месте, а не молча проверять старую папку.

Поэтому же тесты **генерируют код сами** (фикстура уровня session): сгенерированное
не коммитится, и на чистой клоне ``make test`` обязан оставаться зелёным.
"""

import hashlib
import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from scripts.gen_proto import GENERATED_ROOT, PROJECT_PACKAGE, PROTO_ROOT

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent

#: Модуль-генератор: точка входа таргета ``make gen-proto``.
GENERATOR_MODULE = 'scripts.gen_proto'

#: Методы контракта из T-6.1. Ровно пять, ни больше, ни меньше.
EXPECTED_METHODS = frozenset(
    {
        'CancelPayment',
        'CreatePayment',
        'GetPayment',
        'HandleProviderCallback',
        'WatchPayment',
    },
)

#: Единственный метод, отдающий поток кадров, а не один ответ.
EXPECTED_STREAMING_METHODS = frozenset({'WatchPayment'})

#: Потолок времени генерации. Намеренно очень generous: фактически ``make gen-proto``
#: занимает доли секунды, и этот порог не «проверяет скорость» — он лишь ловит
#: патологию (зависший процесс), из-за которой ``make test`` подвисал бы вместо
#: того, чтобы упасть.
GENERATION_BUDGET_SECONDS = 60.0

#: Имена модулей, которые protoc пишет в начале строки.
_IMPORT_LINE = re.compile(r'^(?:from|import)\s+([A-Za-z_][\w.]*)', re.MULTILINE)


def _contract_module_names() -> tuple[str, str]:
    """Имена сгенерированных модулей, выведенные из дерева ``.proto``.

    Берутся из первого контракта: ``payments/v1/payments.proto`` даёт
    ``...generated.payments.v1.payments_pb2``. Ни одно имя здесь не выписано
    руками — добавление второго контракта не должно требовать правки теста.

    :returns: (``<пакет>_pb2``, ``<пакет>_pb2_grpc``);
    :raises AssertionError: если в ``proto/`` нет ни одного контракта.
    """
    contracts = sorted(PROTO_ROOT.rglob('*.proto'))
    assert contracts, f'в {PROTO_ROOT} нет ни одного .proto'
    parts = contracts[0].relative_to(PROTO_ROOT).with_suffix('').parts
    package = '.'.join((*PROJECT_PACKAGE.split('.'), *parts))
    return f'{package}_pb2', f'{package}_pb2_grpc'


def _stub_path() -> Path:
    """Путь к сгенерированному grpc-стабу (без модуля импорта, только файл)."""
    _, stub_module = _contract_module_names()
    tail = stub_module.split('.')[len(PROJECT_PACKAGE.split('.')) :]
    return GENERATED_ROOT.joinpath(*tail).with_suffix('.py')


def _package_directories() -> list[Path]:
    """Каталоги сгенерированного дерева без служебных ``__pycache__``.

    ``__pycache__`` появляется после первого же импорта сгенерированного кода, но
    пакетом он не является: наличия ``__init__.py`` в нём никто не ждёт, и без
    фильтра проверка «у каждого каталога есть маркер» падала бы на кэше Python.
    """
    found = [path for path in GENERATED_ROOT.rglob('*') if path.is_dir() and path.name != '__pycache__']
    return [GENERATED_ROOT, *sorted(found)]


def run_generation() -> subprocess.CompletedProcess[str]:
    """Запускает генератор так же, как это делает ``make gen-proto``."""
    return subprocess.run(
        [sys.executable, '-m', GENERATOR_MODULE],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot(directory: Path) -> dict[str, str]:
    """Снимок дерева: относительный путь -> sha256 содержимого."""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob('*'))
        if path.is_file() and path.suffix != '.pyc'
    }


@pytest.fixture(scope='session')
def generated_contract() -> ModuleType:
    """Сгенерированный модуль сообщений; генерация выполняется один раз за сессию."""
    result = run_generation()
    assert result.returncode == 0, f'генерация упала: {result.stderr}'
    module_name, _ = _contract_module_names()
    return importlib.import_module(module_name)


def test_generated_code_declares_payments_service(generated_contract: ModuleType) -> None:
    """Контракт из T-6.1 доехал до сгенерированного кода целиком."""
    service = generated_contract.DESCRIPTOR.services_by_name['PaymentsService']
    assert service.full_name == 'payments.v1.PaymentsService'
    assert {method.name for method in service.methods} == EXPECTED_METHODS
    assert {method.name for method in service.methods if method.server_streaming} == EXPECTED_STREAMING_METHODS


def test_generated_code_is_importable_from_project_package(generated_contract: ModuleType) -> None:
    """Контракт импортируется как часть проекта, а не как чужой топ-левел пакет."""
    assert generated_contract.__name__.startswith(f'{PROJECT_PACKAGE}.')
    assert importlib.util.find_spec('payments') is None, (
        'контракт импортируется из src.core_service..., а не из топ-левел payments: '
        'иначе починенный импорт стаба не понадобился бы вовсе'
    )


def test_generated_stub_import_is_absolute_not_toplevel() -> None:
    """Стаб ссылается на соседний модуль по абсолютному пути внутри пакета.

    Проверяется именно исходник сгенерированного файла, а не успешность импорта:
    успешный импорт ничего не говорит о том, **как** стаб ссылается на соседа, а
    неправильная ссылка — это ровно та поломка, которая однажды проявится после
    переноса каталога или запуска из другого рабочего каталога.
    """
    stub_path = _stub_path()
    assert stub_path.is_file(), f'стаб не сгенерирован: {stub_path}'

    imported = set(_IMPORT_LINE.findall(stub_path.read_text(encoding='utf-8')))
    assert {module for module in imported if module.split('.')[0] == 'payments'} == set(), (
        'стаб сохранил топ-левел импорт контракта — он не находится в sys.path'
    )
    assert any(module.startswith(PROJECT_PACKAGE) for module in imported), 'стаб не ссылается на пакет проекта'
    assert 'grpc' in imported, 'сторонние импорты починка не должна была трогать'


def test_generated_package_is_a_regular_package() -> None:
    """В каждом каталоге генерации есть ``__init__.py``.

    Иначе каталоги стали бы ``namespace``-пакетами: импорт кое-как прошёл бы, но
    mypy перестал бы видеть типы сообщений, а поведение дерева зависело бы от
    окружения. Проверяются все каталоги сразу, чтобы пропуск нельзя было не заметить.
    """
    directories = _package_directories()
    assert directories, 'генерация не создала ни одного каталога'
    for directory in directories:
        assert (directory / '__init__.py').is_file(), f'в каталоге {directory} нет __init__.py'


def test_generated_package_is_not_committed() -> None:
    """Каталог генерации игнорируется git и не содержит отслеживаемых файлов."""
    if shutil.which('git') is None:
        pytest.skip('git недоступен в окружении')

    relative_root = GENERATED_ROOT.relative_to(REPO_ROOT)
    tracked = subprocess.run(
        ['git', 'ls-files', str(relative_root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == '', f'в git попал машинный вывод: {tracked.stdout.strip()}'

    ignored = subprocess.run(
        ['git', 'check-ignore', '-q', str(relative_root)],
        cwd=REPO_ROOT,
        check=False,
    )
    assert ignored.returncode == 0, f'{relative_root} не попал под ignore-правило'


def test_generation_is_idempotent() -> None:
    """Два прогона подряд дают побайтово одинаковое дерево (DoD T-6.2).

    Проверяются не только имена файлов, но и их содержимое: иначе «идемпотентность»
    означала бы лишь «файлы называются одинаково». Особенно важна вторая половина —
    починка импортов дописывает в файл префикс, и повторный проход по уже
    исправленному файлу легко дал бы ``generated.generated.payments.v1``.
    """
    assert run_generation().returncode == 0
    first = _snapshot(GENERATED_ROOT)
    assert run_generation().returncode == 0
    second = _snapshot(GENERATED_ROOT)

    assert first, 'генерация не создала ни одного файла'
    assert first.keys() == second.keys(), 'набор файлов изменился между прогонами'
    assert first == second, 'содержимое файлов изменилось между прогонами'


def test_generation_removes_stale_generated_files() -> None:
    """Каталог очищается целиком: осиротевшие файлы не переживают генерацию.

    ``protoc`` перезаписывает только те файлы, чьи ``.proto`` он видел. Если
    контракт переименовать или удалить, его прежний ``*_pb2.py`` остался бы лежать и
    продолжал бы импортироваться — в проекте жил бы контракт, которого нет.
    """
    stale = GENERATED_ROOT / 'payments' / 'v1' / 'stale_pb2.py'
    stale.write_text('# осиротевший файл от удалённого контракта\n', encoding='utf-8')
    try:
        assert run_generation().returncode == 0
        assert not stale.exists(), 'файл потерянного контракта пережил генерацию'
    finally:
        if stale.exists():
            stale.unlink()


def test_generation_is_fast() -> None:
    """Генерация укладывается в разумный бюджет времени (DoD T-6.2)."""
    started = time.monotonic()
    result = run_generation()
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < GENERATION_BUDGET_SECONDS, f'генерация заняла {elapsed:.1f} с'
