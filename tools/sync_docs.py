"""Копирование документов постановки в `docs/` пульта (README, ТЗ, чек-лист, отчёты).

Запуск из корня пульта:

    python -m tools.sync_docs

Скрипт находит каталог постановки в основном репозитории по маске `*/05. *`
(без литеральных не-ASCII путей — так команда одинаково работает в любой консоли),
копирует из него все `*.md` в `docs/`, дополнительно копирует материалы уровня
постановки `SOM1.json` (спецификация API — из неё генерируется реестр эндпоинтов
консоли) и `SOM1. Комментарии.md` (замечания к API, §5/§6), и печатает протокол
(что скопировано, SHA-256). Основной репозиторий можно переопределить:
`python -m tools.sync_docs --source <путь>`.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS = ROOT / "docs"
DEFAULT_SOURCE_ROOT = Path(r"D:\VSC Python projects\TrainingServerUI\context")

#: Файлы уровня постановки (лежат рядом с каталогом `05. …`), нужные пульту.
EXTRA_FILES = ("SOM1.json", "SOM1. Комментарии.md")


def find_source(root: Path) -> Path:
    """Каталог постановки: первый `*/05. *` с markdown-документами."""
    candidates = [
        path for path in root.glob("*/05. *") if path.is_dir() and list(path.glob("*.md"))
    ]
    if not candidates:
        raise FileNotFoundError(f"каталог постановки не найден в {root.as_posix()}")
    return sorted(candidates, key=lambda path: len(list(path.glob("*.md"))), reverse=True)[0]


def sha256(path: Path) -> str:
    """Короткая контрольная сумма файла (для протокола синхронизации)."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:16]


def main(argv: list[str] | None = None) -> int:
    """Копирует документы постановки в `docs/` и печатает протокол."""
    parser = argparse.ArgumentParser(description="Синхронизация docs/ с постановкой")
    parser.add_argument("--source", default=None, help="каталог постановки (по умолчанию — поиск)")
    parser.add_argument("--target", default=str(DEFAULT_DOCS), help="каталог docs/ пульта")
    parser.add_argument(
        "--no-extra", action="store_true", help="не копировать SOM1.json и SOM1. Комментарии.md"
    )
    args = parser.parse_args(argv)

    source = Path(args.source) if args.source else find_source(DEFAULT_SOURCE_ROOT)
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)

    print(f"Источник: {source.as_posix()}")
    print(f"Назначение: {target.as_posix()}")
    for document in sorted(source.glob("*.md")):
        destination = target / document.name
        shutil.copy2(document, destination)
        print(f"  + {document.name}  ({sha256(document)})")

    if not args.no_extra:
        for name in EXTRA_FILES:
            extra = source.parent / name
            if not extra.is_file():
                print(f"  ! {name} не найден в {source.parent.as_posix()}")
                continue
            destination = target / name
            shutil.copy2(extra, destination)
            print(f"  + {name}  ({sha256(extra)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
