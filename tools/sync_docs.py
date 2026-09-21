"""Копирование документов постановки в `docs/` пульта (README, ТЗ, чек-лист, отчёты).

Запуск из корня пульта:

    python -m tools.sync_docs

Скрипт находит каталог постановки в основном репозитории по маске `*/05. *`
(без литеральных не-ASCII путей — так команда одинаково работает в любой консоли),
копирует из него все `*.md` в `docs/`, дополнительно копирует материал уровня
постановки `SOM1. Комментарии.md` (замечания к API, §5/§6) и печатает протокол
(что скопировано, SHA-256). Основной репозиторий можно переопределить:
`python -m tools.sync_docs --source <путь>`.

**Контракт (`SOM1.json`) без флага не копируется.** Это защита от отката: в постановке
может лежать более старая версия спецификации, чем актуальная в пульте (например, после
получения нового контракта отдельным файлом `api_<дата>.json`). Обновление контракта —
осознанное действие с явным флагом:

    python -m tools.sync_docs --with-contract

Тогда берётся самый свежий по дате имени/времени файл среди `SOM1.json` и `api_*.json`
рядом с каталогом постановки, копируется как `docs/SOM1.json`, и печатается, что именно
изменилось (SHA-256 до/после). Актуальность зафиксированного контракта проверяет тест
`tests/unit/test_contract_version.py`, а разбор версий — `docs/README.md`.
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
#: Контракт (`SOM1.json`) сюда не входит намеренно — он обновляется только с `--with-contract`.
EXTRA_FILES = ("SOM1. Комментарии.md",)

#: Имя контракта в пульте (единый источник реестра эндпоинтов) и маска имён поставки.
CONTRACT_NAME = "SOM1.json"
CONTRACT_PATTERNS = ("SOM1.json", "api_*.json")


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


def contract_candidates(source_root: Path) -> list[Path]:
    """Файлы-кандидаты контракта рядом с каталогом постановки (`SOM1.json`, `api_*.json`)."""
    found: list[Path] = []
    for pattern in CONTRACT_PATTERNS:
        found.extend(path for path in source_root.glob(pattern) if path.is_file())
    return sorted(set(found))


def newest_contract(source_root: Path) -> Path | None:
    """Самый свежий кандидат контракта: по дате в имени (`api_ДД_ММ_ГГ`), затем по времени."""
    candidates = contract_candidates(source_root)
    if not candidates:
        return None

    def sort_key(path: Path) -> tuple[str, float, str]:
        digits = "".join(char for char in path.stem if char.isdigit())
        return (digits, path.stat().st_mtime, path.name)

    return max(candidates, key=sort_key)


def copy_contract(source_root: Path, target: Path) -> int:
    """Копирует контракт в `docs/SOM1.json` (только по явному флагу `--with-contract`)."""
    candidate = newest_contract(source_root)
    if candidate is None:
        print(f"  ! контракт не найден в {source_root.as_posix()} ({', '.join(CONTRACT_PATTERNS)})")
        return 1

    destination = target / CONTRACT_NAME
    before = sha256(destination) if destination.is_file() else "—"
    shutil.copy2(candidate, destination)
    after = sha256(destination)
    state = "без изменений" if before == after else "ОБНОВЛЁН"
    print(f"  + {CONTRACT_NAME}  ({after}, источник {candidate.name}) — контракт {state}")
    if before != after:
        print(
            f"    контракт изменился: {before} → {after}; пересоберите реестр и обновите docs/README.md"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Копирует документы постановки в `docs/` и печатает протокол."""
    parser = argparse.ArgumentParser(description="Синхронизация docs/ с постановкой")
    parser.add_argument("--source", default=None, help="каталог постановки (по умолчанию — поиск)")
    parser.add_argument("--target", default=str(DEFAULT_DOCS), help="каталог docs/ пульта")
    parser.add_argument(
        "--no-extra", action="store_true", help="не копировать SOM1. Комментарии.md"
    )
    parser.add_argument(
        "--with-contract",
        action="store_true",
        help="обновить и контракт (docs/SOM1.json) — берётся свежайший SOM1.json/api_*.json постановки",
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

    if args.with_contract:
        copy_contract(source.parent, target)
    else:
        print(
            "  = контракт не копировался (защита от отката версии; обновление — "
            "`python -m tools.sync_docs --with-contract`)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
