"""Живая проверка объединения записей (ручной прогон, этап T1).

Запуск из корня пульта:

    python -m tools.probe_records

Скрипт только читает реестр и один небольшой markup-файл; печатает показатели
объединения и проверяет дефект API: скачивание файла с не-ASCII именем.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from acceptance.api import Apis, build_client  # noqa: E402
from acceptance.http_log import Journal  # noqa: E402
from acceptance.records import build_records  # noqa: E402
from client.errors import ClientError  # noqa: E402
from client.settings import load_settings  # noqa: E402
from lib.markup_stats import parse_markup_stats  # noqa: E402
from lib.subdatasets import format_size  # noqa: E402


def main() -> int:
    """Печатает объединение живого реестра в записи и проверяет дефект скачивания."""
    settings = load_settings()
    if not settings.is_configured:
        print("TRAINING_SERVER_BASE_URL не задан — проверка невозможна.")
        return 2

    journal = Journal(max_records=50)
    client = build_client(settings, journal)
    try:
        apis = Apis.build(client)
        files = apis.files.list_files().files
        overview = build_records(files)

        print(f"Сервер: {settings.base_url}")
        print(f"Файлов: {overview.stats.files} · записей: {overview.stats.records}")
        print(
            f"С разметкой: {overview.stats.paired} ({overview.stats.coverage}%) · "
            f"без разметки: {overview.stats.without_markup} · "
            f"разметка без RAW: {overview.stats.unpaired_markup} · "
            f"прочих: {overview.stats.other}"
        )
        print(
            f"Дублей: {overview.stats.duplicates} записей в {overview.stats.duplicate_bases} базах · "
            f"подтверждено по времени: {overview.stats.confirmed}"
        )
        print(
            f"Объём: RAW {format_size(overview.stats.raw_bytes)} · "
            f"markup {format_size(overview.stats.markup_bytes)}"
        )
        print(f"Полнота манифеста: {overview.manifest_is_complete()}")

        print("\nБазы с дублями (первые 5):")
        for base in overview.duplicate_bases[:5]:
            records = overview.for_base(base)
            versions = ", ".join(
                f"v{record.version}: {record.raw.size} Б ({record.raw.import_date:%d.%m.%Y %H:%M})"
                for record in records
            )
            print(f"  - {base}: версий {len(records)}: {versions}")

        ascii_record = next(
            (
                record
                for record in overview.records
                if record.markup is not None
                and str(record.markup.file_name).isascii()
                and record.markup.size < 5 * 1024 * 1024
            ),
            None,
        )
        if ascii_record is not None and ascii_record.markup is not None:
            markup = ascii_record.markup
            payload = apis.files.download(markup.id)
            stats = parse_markup_stats(payload.content)
            print(
                f"\nРазбор разметки {markup.file_name}: записей {stats.records}, чанков {stats.chunks}"
            )

        non_ascii = next(
            (record for record in overview.records if not str(record.raw.file_name).isascii()),
            None,
        )
        if non_ascii is not None:
            try:
                apis.files.download(non_ascii.raw.id)
            except ClientError as exc:
                print(f"\nДефект API подтверждён ({non_ascii.raw.file_name}): {exc}")
            else:
                print(f"\nФайл с не-ASCII именем скачался: {non_ascii.raw.file_name}")
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
