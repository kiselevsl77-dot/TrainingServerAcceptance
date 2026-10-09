"""Роль и формат CSV стенда при загрузке (правки по итогам приёмки К2, `llm_tasks/67`).

Живой стенд (сборка 30.09.2026) валидирует заголовок RAW-CSV и отклоняет файл ответом
`CSV header is missing required columns: …`. Раньше пульт отправлял файл «как есть»:
при смешанной загрузке (измерения + разметка) markup-файл уходил с ролью `RAW`, а оператор
получал обезличенный отказ стенда. Здесь — проверка **до** отправки на стенд:

    * роль файла определяется по заголовку (`detect_role`), а не по выбору оператора;
    * заголовок сверяется с перечнем обязательных колонок роли (`missing_columns`);
    * читается **только первая строка** — боевые RAW доходят до 2,18 ГБ, файл в память
      не берётся (`HeaderRecorder` собирает первые байты во время потоковой записи).

Модуль чистый: без HTTP, без сессии и без интерфейса (правило `llm_tasks/31.4` §3).
Перечень колонок RAW берётся из `acceptance.ml_samples` — единый источник с приёмочными
проверками (`ROUND_TRIP_BODY`), поэтому список колонок не двоится.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from acceptance.ml_samples import RAW_COLUMNS

#: Разделитель колонок стенда (проверено: `;`, а не `,`).
DELIMITER = ";"

#: Роли файлов данных (совпадают со значениями `FileType` стенда).
ROLE_RAW = "RAW"
ROLE_MARKUP = "LOADS"

#: Обязательные колонки RAW: сигнально-параметрический набор. `chunkID` и `timestamp` —
#: первые колонки файла, но их отсутствие стенд в ответе не называет (проверено 30.09.2026),
#: поэтому обязательным считается набор без этих двух.
RAW_REQUIRED: tuple[str, ...] = tuple(
    name for name in RAW_COLUMNS if name.lower() not in {"chunkid", "timestamp"}
)

#: Обязательные колонки markup (формат проверен на живом сервере, `lib/markup_stats.py`).
LOADS_REQUIRED: tuple[str, ...] = ("markupId", "chunkID", "LoadID", "Ph_n", "Category")

#: Перечень обязательных колонок по роли.
REQUIRED: dict[str, tuple[str, ...]] = {ROLE_RAW: RAW_REQUIRED, ROLE_MARKUP: LOADS_REQUIRED}

#: Колонки-признаки формата (в нижнем регистре) — для определения роли по заголовку.
RAW_MARKERS: tuple[str, ...] = (
    "u_a_signal",
    "i_a_signal",
    "i_c_signal",
    "sample_rate",
    "channel_mask",
)
MARKUP_MARKERS: tuple[str, ...] = ("markupid", "loadid", "ph_n", "category")

#: Сколько байт тела читаем, чтобы получить заголовок (первая строка короткая).
HEADER_LIMIT = 64 * 1024

_ROLE_LABELS: dict[str, str] = {
    ROLE_RAW: "измерения (RAW)",
    ROLE_MARKUP: "разметка (LOADS)",
}


class HeaderRecorder:
    """Обёртка над потоком: пропускает чтение и запоминает первые байты (заголовок).

    Пульт пишет тело во временный файл порциями, поэтому заголовок снимается «на лету»,
    без повторного чтения файла: `save_stream` работает поверх этой обёртки, а вызывающая
    сторона после записи читает `head`.
    """

    def __init__(self, stream: Any, limit: int = HEADER_LIMIT) -> None:
        self._stream = stream
        self._limit = max(0, int(limit))
        self.head = b""

    def read(self, size: int = -1) -> bytes:
        """Читает порцию и досыпает её в буфер заголовка (не более `limit` байт)."""
        block = self._stream.read(size)
        if block and len(self.head) < self._limit:
            self.head += block[: self._limit - len(self.head)]
        return block


def first_line(raw: bytes | str) -> str:
    """Первая непустая строка тела без BOM и завершающих переводов (пустая — если тела нет)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    if text.startswith("\ufeff"):
        text = text[1:]
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def parse_header(line: str | bytes) -> list[str]:
    """Колонки заголовка. Разделитель — `;`; если его нет, пробуем запятую (частый экспорт)."""
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
    text = text.lstrip("\ufeff")
    delimiter = DELIMITER if DELIMITER in text else ","
    return [cell.strip() for cell in text.split(delimiter)]


def detect_role(header: Sequence[str]) -> str | None:
    """Роль файла по заголовку: `RAW`, `LOADS` или `None` (формат не распознан)."""
    lowered = {str(name).strip().lower() for name in header}
    if lowered & set(MARKUP_MARKERS):
        return ROLE_MARKUP
    required = {name.lower() for name in RAW_REQUIRED}
    if lowered & set(RAW_MARKERS) or len(lowered & required) >= 5:
        return ROLE_RAW
    return None


def missing_columns(header: Sequence[str], role: str) -> list[str]:
    """Отсутствующие обязательные колонки роли (регистронезависимо, порядок — как в перечне)."""
    expected = REQUIRED.get(str(role).upper())
    if not expected:
        return []
    present = {str(name).strip().lower() for name in header}
    return [name for name in expected if name.lower() not in present]


def role_label(role: str) -> str:
    """Человекочитаемая подпись роли для сообщений оператору."""
    return _ROLE_LABELS.get(str(role).upper(), str(role))
