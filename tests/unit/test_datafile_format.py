"""Тесты определения роли и формата CSV стенда (`pult/datafile_format.py`).

Проверяется то, что раньше приводило к обезличенному отказу стенда:
    * роль markup-файла определяется по заголовку, а не по выбору оператора;
    * недостающие обязательные колонки называются поимённо, а не «502»;
    * заголовок снимается из первых байт потока — файл в память не читается.
"""

from __future__ import annotations

from acceptance.ml_samples import RAW_COLUMNS, SAMPLE_SIGNAL_FIELDS, raw_row
from pult import datafile_format as fmt


def _raw_body() -> bytes:
    """Достоверный RAW-CSV по каноническому заголовку стенда."""
    line = ";".join(RAW_COLUMNS)
    row = raw_row(dict.fromkeys(SAMPLE_SIGNAL_FIELDS, "AAAA"))
    return f"{line}\n{row}\n".encode()


MARKUP_BODY = b"markupId;chunkID;LoadID;Ph_n;Category\n1;1;Antminer_S19;A;miner\n"


def test_raw_required_excludes_identity_columns() -> None:
    """Обязательный набор RAW — сигнально-параметрический (без `chunkID`/`timestamp`)."""
    assert len(fmt.RAW_REQUIRED) == 22
    lowered = {name.lower() for name in fmt.RAW_REQUIRED}
    assert {"chunkid", "timestamp"} & lowered == set()
    assert lowered <= {name.lower() for name in RAW_COLUMNS}


def test_parse_header_supports_semicolon_and_comma() -> None:
    """Разделитель стенда — `;`; запятая как частый экспорт понимается тоже."""
    assert fmt.parse_header("chunkID;timestamp;U_A_rms") == ["chunkID", "timestamp", "U_A_rms"]
    assert fmt.parse_header("a,b,c") == ["a", "b", "c"]


def test_first_line_strips_bom_and_blank_lines() -> None:
    """Первая непустая строка без BOM — заголовок, а не «пустая строка файла»."""
    assert fmt.first_line(b"\xef\xbb\xbfchunkID;timestamp\n1;2\n") == "chunkID;timestamp"
    assert fmt.first_line(b"\n\nchunkID;timestamp\n") == "chunkID;timestamp"
    assert fmt.first_line(b"") == ""


def test_detect_role_by_header() -> None:
    """Роль определяется по колонкам: измерения (сигналы) против разметки (LoadID/Ph_n)."""
    raw_header = fmt.parse_header(fmt.first_line(_raw_body()))
    markup_header = fmt.parse_header(fmt.first_line(MARKUP_BODY))

    assert fmt.detect_role(raw_header) == fmt.ROLE_RAW
    assert fmt.detect_role(markup_header) == fmt.ROLE_MARKUP
    assert fmt.detect_role(["a", "b"]) is None


def test_missing_columns_reports_names_case_insensitively() -> None:
    """Недостающие колонки называются поимённо и не зависят от регистра файла."""
    header = fmt.parse_header(fmt.first_line(MARKUP_BODY))

    assert fmt.missing_columns(header, fmt.ROLE_MARKUP) == []
    missing = fmt.missing_columns(header, fmt.ROLE_RAW)
    assert "U_A_signal" in missing and "chunkID".lower() not in {name.lower() for name in missing}


class _Chunks:
    """Поток, отдающий тело порциями: имитирует `rfile` сервера пульта."""

    def __init__(self, data: bytes, size: int) -> None:
        self._data = data
        self._size = size
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        """Порция не крупнее `size` байт (как настроено в `save_stream`)."""
        if self._pos >= len(self._data):
            return b""
        step = self._size if n is None or n < 0 else min(n, self._size)
        block = self._data[self._pos : self._pos + step]
        self._pos += len(block)
        return block


def test_header_recorder_keeps_first_bytes_only() -> None:
    """Заголовок снимается из первых байт потока и не копит весь файл."""
    body = _raw_body()
    recorder = fmt.HeaderRecorder(_Chunks(body, 8), limit=32)

    while recorder.read(8):
        pass

    assert recorder.head == body[:32]
    assert fmt.first_line(recorder.head).startswith("chunkID")
