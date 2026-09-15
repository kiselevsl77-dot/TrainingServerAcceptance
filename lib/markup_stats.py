"""Структурные характеристики разметки субдатасета (задел замечаний №3/№4).

Сервер не отдаёт число записей и чанков для файла/субдатасета (`GET /api/data/files`
возвращает только метаданные: имя, размер, дату, тип) — замечание к API. Поэтому UI
считает характеристики по markup-файлу, который невелик (единицы МБ):

    * записи — число строк данных (первая строка считается заголовком);
    * чанки — число уникальных значений колонки `chunkID`.

Формат markup (проверено на живом сервисе):
    `markupId;chunkID;LoadID;Ph_n;Category`

RAW-файлы (до 2.1 ГБ) не разбираются: их структура известна из заголовка
(`chunkID;timestamp;U_A_rms;…;I_C_signal`), но объём не позволяет читать их в UI.
"""

from __future__ import annotations

from dataclasses import dataclass

CHUNK_COLUMN = "chunkID"
DELIMITER = ";"


@dataclass(frozen=True)
class MarkupStats:
    """Число записей и чанков в markup-файле."""

    records: int
    chunks: int


def parse_markup_stats(content: bytes, *, delimiter: str = DELIMITER) -> MarkupStats:
    """Считает записи и уникальные `chunkID` в содержимом markup-файла.

    Args:
        content: содержимое файла (кодировка UTF-8, ошибки заменяются).
        delimiter: разделитель колонок (в файлах сервера — `;`).
    """
    text = content.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return MarkupStats(records=0, chunks=0)

    header = [column.strip() for column in lines[0].split(delimiter)]
    chunk_index = next(
        (index for index, column in enumerate(header) if column.lower() == CHUNK_COLUMN.lower()),
        None,
    )

    records = 0
    chunks: set[str] = set()
    for line in lines[1:]:
        records += 1
        if chunk_index is None:
            continue

        cells = line.split(delimiter)
        if chunk_index < len(cells) and cells[chunk_index].strip():
            chunks.add(cells[chunk_index].strip())

    return MarkupStats(records=records, chunks=len(chunks))
