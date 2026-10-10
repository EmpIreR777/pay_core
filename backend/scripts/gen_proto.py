"""Генерация Python-контрактов gRPC из ``.proto`` (T-6.2).

Единственное место, где объявлены пути контрактов: таргет ``gen-proto`` в
Makefile вызывает только ``python -m scripts.gen_proto`` и ничего не знает про
каталоги. Это требование «факт с владельцем» (AGENT.md §4.4): путь к ``.proto``
и путь к сгенерированному коду объявлены один раз здесь, а не продублированы в
Makefile, Dockerfile и тестах — иначе правка одного пути разъехалась бы с
остальными тремя.

Зачем после ``protoc`` нужен шаг починки импортов
-------------------------------------------------
``protoc`` умеет писать код в поддерево каталога, но не умеет выбрать
*Python-путь* этого поддерева. Сгенерированный стаб приходит с топ-левел
импортом::

    from payments.v1 import payments_pb2 as payments__pb2

Он работает только если ``proto/`` лежит в корне проекта, то есть попадает в
``sys.path``. Мы же кладём сгенерированный код внутрь пакета
``src.core_service.interfaces.grpc.generated``, и такой импорт не находится
никогда: модуль ``payments`` никто не импортирует, а сгенерированный стаб
падает на импорте. Штатного флага, который это исправляет, у grpc-плагина нет:
``--grpc_python_opt`` в этой версии protoc не существует, а ``M``-маппинг внутри
``--grpc_python_out`` плагин не понимает («Invalid grpc version»). Поэтому
импорт переписывается один раз, сразу после генерации.

Правило переписывания узкое: префиксом ``PROJECT_PACKAGE`` дополняются только
те импорты, чей верхний пакет совпадает с каталогом внутри ``proto/``
(``payments``). Импорты third-party (``grpc``, ``google.protobuf``) не трогаются
— они по определению живут вне нашего пакета.

Идемпотентность
---------------
Каталог генерации очищается целиком перед запуском ``protoc``. Сам ``protoc``
файлы с теми же именами перезаписывает, но после переименования или удаления
``.proto`` прежний ``*_pb2.py`` остался бы лежать и продолжал импортироваться —
в проекте жил бы контракт, которого больше нет. Полная очистка даёт свойство,
которое и требуется по DoD: **один и тот же вход — один и тот же выход**.

Ещё одно следствие очистки: битых файлов не остаётся и в том случае, когда
генерация упала на середине, — следующий запуск начинается с чистого листа.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

#: Корень бэкенда — от него отсчитываются все пути генерации.
BACKEND_ROOT: Final = Path(__file__).resolve().parents[1]

#: Каталог контрактов: ``proto/`` лежит рядом с ``backend/`` (общий корень репозитория).
PROTO_ROOT: Final = BACKEND_ROOT.parent / 'proto'

#: Куда складывается сгенерированный код. Каталог внутри пакета Core Service, а не
#: отдельная папка в ``src/``: контракт принадлежит сервису, и импортировать его
#: должны модули Core Service, а не кто-то с верхнего уровня репозитория.
GENERATED_ROOT: Final = BACKEND_ROOT / 'src' / 'core_service' / 'interfaces' / 'grpc' / 'generated'

#: Python-путь сгенерированного пакета. Он же префикс для починки импортов.
PROJECT_PACKAGE: Final = 'src.core_service.interfaces.grpc.generated'

#: Содержимое сгенерированных ``__init__.py``. Одинаковое во всех каталогах
#: намеренно: это маркер машинного вывода, а не объявление о чём-то конкретном.
PACKAGE_MARKER: Final = '"""Сгенерировано make gen-proto: правки будут перезаписаны."""\n'

#: Импорт в начале строки — ровно тот вид, который protoc пишет в стабах.
#: Якорь ``^`` ловит только настоящие операторы импорта, а не любое упоминание
#: пакета в сгенерированном коде.
IMPORT_STATEMENT: Final = re.compile(r'^(?P<statement>from|import) (?P<module>[A-Za-z_][A-Za-z0-9_.]*)')


def collect_proto_files(proto_root: Path) -> list[Path]:
    """Пути ``.proto`` относительно ``proto_root``, отсортированные.

    Сортировка не для красоты: порядок файлов в командной строке protoc должен
    быть один и тот же, иначе воспроизводимость генерации не гарантирована.

    :returns: относительные пути, пригодные для ``proto_root / relative``.
    """
    return sorted(path.relative_to(proto_root) for path in proto_root.rglob('*.proto'))


def contract_packages(proto_root: Path) -> set[str]:
    """Верхнеуровневые пакеты контрактов — каталоги верхнего уровня в ``proto/``.

    Это «белый список» того, что вообще можно переписывать при починке импортов:
    пакет ``payments`` наш, а ``google`` и ``grpc`` — нет.

    :returns: имена каталогов верхнего уровня, например ``{'payments'}``.
    """
    return {path.name for path in proto_root.iterdir() if path.is_dir()}


def build_protoc_command(proto_files: list[Path]) -> list[str]:
    """Собирает командную строку одного запуска ``protoc``.

    Один запуск на все контракты, а не по одному на файл: именно это делает
    ``make gen-proto`` быстрым (DoD T-6.2) — плата за запуск одинакова
    независимо от числа контрактов.

    ``--pyi_out`` обязателен не для красоты: ``.pyi``-стабы — единственная
    причина, по которой mypy вообще увидит типы сообщений (T-6.3).

    ``--experimental_allow_proto3_optional`` здесь **намеренно не передаётся**.
    Флаг вводился в 3.12 как экспериментальная поддержка proto3-``optional`` и
    перестал быть нужен с 3.15: контракт собирает явное наличие поля из коробки
    (проверено на вшитом ``libprotoc 35.1``). «На вся случай» добавлять его не
    следует — искать причину поломки в лишнем ключе никто не захочет.

    :param proto_files: контракты из :func:`collect_proto_files`;
    :returns: готовый список аргументов для запуска без оболочки.
    """
    command = [
        sys.executable,
        '-m',
        'grpc_tools.protoc',
        f'-I{PROTO_ROOT}',
        f'--python_out={GENERATED_ROOT}',
        f'--pyi_out={GENERATED_ROOT}',
        f'--grpc_python_out={GENERATED_ROOT}',
    ]
    return [*command, *(str(PROTO_ROOT / relative) for relative in proto_files)]


def compile_contracts(proto_files: list[Path]) -> None:
    """Компилирует контракты и поднимает наружу код возврата ``protoc``.

    Вызов без оболочки и список аргументов: подставлять в команду нечего, все
    пути собраны из констант модуля, поэтому проверка S603 здесь неприменима.

    :raises SystemExit: с кодом ``protoc``, если компиляция не удалась —
        генерация обязана падать, а не рапортовать успехом наполовину.
    """
    completed = subprocess.run(  # noqa: S603
        build_protoc_command(proto_files),
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def fix_local_imports(generated_root: Path, top_packages: set[str], project_package: str) -> int:
    """Переписывает топ-левел импорты контрактов на абсолютные (см. докстринг модуля).

    :param generated_root: корень сгенерированного дерева;
    :param top_packages: верхнеуровневые пакеты из :func:`contract_packages`;
    :param project_package: префикс, который добавляется к нашим импортам;
    :returns: сколько строк исправлено. Число выводится в лог намеренно: молчаливое
        «исправлено 0 строк» — это сигнал, что формат вывода protoc изменился и
        починка больше не работает, и заметить это лучше по логу, чем по факту
        падения на импорте в самом сервере.
    """
    fixed = 0
    for path in sorted(generated_root.rglob('*.py')):
        patched: list[str] = []
        for line in path.read_text(encoding='utf-8').splitlines(keepends=True):
            match = IMPORT_STATEMENT.match(line)
            if match and match.group('module').split('.')[0] in top_packages:
                line = line.replace(match.group('module'), f'{project_package}.{match.group("module")}', 1)
                fixed += 1
            patched.append(line)
        path.write_text(''.join(patched), encoding='utf-8')
    return fixed


def write_package_markers(generated_root: Path) -> None:
    """Создаёт ``__init__.py`` в каждом каталоге сгенерированного дерева.

    Без них каталоги остались бы ``namespace``-пакетами: импорт кое-как
    сработал бы, но mypy увидел бы в них не-пакеты и потерял типы сообщений,
    а дерево вело бы себя по-разному в зависимости от окружения.

    :param generated_root: корень сгенерированного дерева.
    """
    directories = [generated_root, *sorted(path for path in generated_root.rglob('*') if path.is_dir())]
    for directory in directories:
        (directory / '__init__.py').write_text(PACKAGE_MARKER, encoding='utf-8')


def main() -> None:
    """Полный цикл: очистить, скомпилировать, починить импорты, разметить пакеты."""
    proto_files = collect_proto_files(PROTO_ROOT)
    if not proto_files:
        raise SystemExit(f'gen-proto: в {PROTO_ROOT} нет ни одного .proto')

    # Очистка до компиляции, а не после: если protoc упадёт, в каталоге не
    # останется прошлого (возможно, рабочего) контракта — есть что перегенерировать.
    if GENERATED_ROOT.exists():
        shutil.rmtree(GENERATED_ROOT)
    GENERATED_ROOT.mkdir(parents=True)

    compile_contracts(proto_files)
    fixed = fix_local_imports(GENERATED_ROOT, contract_packages(PROTO_ROOT), PROJECT_PACKAGE)
    write_package_markers(GENERATED_ROOT)

    print(f'gen-proto: контрактов {len(proto_files)}, исправлено импортов: {fixed}')
    print(f'gen-proto: результат в {GENERATED_ROOT.relative_to(BACKEND_ROOT)}')


if __name__ == '__main__':
    main()
