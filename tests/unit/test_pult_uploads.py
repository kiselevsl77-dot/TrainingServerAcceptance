"""Тесты потокового приёма файлов (этап Э2.1, `llm_tasks/64`).

Проверяется то, что ломается на боевых данных незаметно: файл в 2 ГБ нельзя прочитать
в память (проверяем порционное чтение и `sha256`), обрыв передачи нельзя принять за
успех (размер обязан совпасть с `Content-Length`), а имя от оператора не должно
управлять путём на диске (`..\\` и управляющие символы).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from pult import uploads


class ShortStream(io.BytesIO):
    """Поток, который «обрывается» раньше заявленного `Content-Length`."""

    def read(self, size: int | None = None) -> bytes:
        """Всегда отдаёт пустой ответ: имитация обрыва передачи."""
        return b""


def test_save_stream_writes_file_and_digest(tmp_path: Path) -> None:
    """Тело пишется целиком, размер и `sha256` считаются на лету."""
    body = b"chunkID;timestamp;U_A_signal\n1;2026-10-02T10:00:00;AAAA\n" * 100

    saved = uploads.save_stream(
        io.BytesIO(body), len(body), file_name="raw 01.csv", dest_dir=tmp_path
    )

    assert saved.path.parent == tmp_path
    assert saved.path.read_bytes() == body
    assert saved.size == len(body)
    assert saved.sha256 == hashlib.sha256(body).hexdigest()


def test_save_stream_uses_chunks_not_whole_body(tmp_path: Path) -> None:
    """Чтение идёт порциями: запрос размера никогда не превышает `chunk`."""
    sizes: list[int | None] = []

    class CountingStream(io.BytesIO):
        """Поток, запоминающий запрошенные размеры порций."""

        def read(self, size: int | None = None) -> bytes:
            """Запоминает запрошенный размер порции и отдаёт данные."""
            sizes.append(size)
            return super().read(size)

    body = b"x" * 25
    uploads.save_stream(
        CountingStream(body), len(body), file_name="a.csv", dest_dir=tmp_path, chunk=10
    )

    assert sizes == [10, 10, 5]
    assert max(size or 0 for size in sizes) <= 10


def test_save_stream_rejects_empty_body(tmp_path: Path) -> None:
    """Пустое тело — понятная ошибка, а не «файл из нуля байт»."""
    with pytest.raises(uploads.UploadError) as error:
        uploads.save_stream(io.BytesIO(b""), 0, file_name="empty.csv", dest_dir=tmp_path)

    assert error.value.status == 400
    assert "пустое тело" in error.value.message
    assert list(tmp_path.iterdir()) == []


def test_save_stream_rejects_oversize_body(tmp_path: Path) -> None:
    """Превышение лимита отклоняется **до** чтения: гигабайты не проходят через пульт."""
    with pytest.raises(uploads.UploadError) as error:
        uploads.save_stream(
            io.BytesIO(b"data"),
            4096,
            file_name="big.csv",
            dest_dir=tmp_path,
            max_bytes=1024,
        )

    assert error.value.status == 413
    assert "больше лимита" in error.value.message
    assert list(tmp_path.iterdir()) == []


def test_save_stream_detects_truncated_stream(tmp_path: Path) -> None:
    """Обрыв потока не превращается в «успешную» загрузку половины файла."""
    with pytest.raises(uploads.UploadError) as error:
        uploads.save_stream(ShortStream(b""), 10, file_name="raw.csv", dest_dir=tmp_path)

    assert error.value.status == 400
    assert "поток прерван" in error.value.message
    assert list(tmp_path.iterdir()) == [], "недописанный временный файл обязан удаляться"


def test_safe_name_keeps_user_name_without_paths() -> None:
    """Пути, управляющие символы и «хвосты» отсекаются, кириллица и пробелы — нет."""
    assert uploads.safe_name("..\\..\\etc\\raw-01.csv") == "raw-01.csv"
    assert uploads.safe_name("C:/data/измерения 01.csv") == "измерения 01.csv"
    assert uploads.safe_name("raw\x00\x07.csv") == "raw.csv"
    assert uploads.safe_name("") == uploads.DEFAULT_NAME


def test_discard_removes_temporary_file(tmp_path: Path) -> None:
    """Временный файл удаляется после отправки на стенд (иначе диск пульта распухнет)."""
    saved = uploads.save_stream(io.BytesIO(b"a"), 1, file_name="a.csv", dest_dir=tmp_path)

    saved.discard()
    saved.discard()  # повторный вызов не падает

    assert not saved.path.exists()
