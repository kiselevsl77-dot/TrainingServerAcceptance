"""Потоковый приём файлов от браузера (этап Э2.1, `llm_tasks/64`).

Зачем отдельный модуль: у пульта сервер принимает только JSON (`_body()` →
`json.loads`, лимит `MAX_BODY` = 1 МБ), а файлы данных стенда — это CSV **до 2,18 ГБ**
(реестр `$Файлов`, снято 02.10.2026). Поэтому файл идёт **сырым телом запроса**, а
параметры (имя, тип, описание) — в query-строке; тело пишется во временный файл
порциями, с подсчётом `sha256` и размера.

Решения, заложенные в модуль:

* **память ограничена порцией** (`chunk`): файл любого размера не занимает ОЗУ пульта;
* **`sha256` считается на лету** — это паспорт файла в сессии испытаний (что именно
  загружено в датасет), пересчёт «потом» потребовал бы повторного чтения гигабайтов;
* **лимит — часть контракта**: превышение даёт `UploadError(status=413)`, а не обрыв
  соединения; JSON-лимит `MAX_BODY` намеренно не меняется;
* модуль не знает ни о стенде, ни об интерфейсе (правило `llm_tasks/31.4` §3) —
  он принимает поток и отдаёт путь с паспортом.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "CHUNK",
    "MAX_UPLOAD",
    "ReadableStream",
    "SavedUpload",
    "UploadError",
    "safe_name",
    "save_stream",
]


class ReadableStream(Protocol):
    """Поток только для чтения: подходит и `rfile` сервера, и обёртка заголовка файла."""

    def read(self, size: int = -1) -> bytes:
        """Отдаёт порцию тела не крупнее `size` байт."""
        ...


#: Лимит размера загружаемого файла (по умолчанию 3 ГиБ): самый большой RAW в реестре
#: стенда — 2,18 ГБ. Лимит защищает пульт от «случайного» ISO, а не от боевого файла.
MAX_UPLOAD = 3 << 30

#: Размер порции чтения: 1 МБ — компромисс между памятью и числом системных вызовов.
CHUNK = 1 << 20

#: Символы, которые заменяются в имени временного файла (пробелы и кириллица допустимы).
BAD_NAME = re.compile(r"[^\w.\-+ ()]+", re.UNICODE)

#: Управляющие символы (включая NUL) в имени файла недопустимы — удаляются целиком.
CONTROL_NAME = re.compile(r"[\x00-\x1f\x7f]+")

#: Имя, если оператор не передал его вовсе.
DEFAULT_NAME = "upload.csv"


class UploadError(Exception):
    """Ошибка приёма файла: пустое тело, превышение лимита, обрыв потока."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = str(message)
        self.status = int(status)


@dataclass(frozen=True)
class SavedUpload:
    """Принятый файл: путь во временном каталоге, размер и `sha256`."""

    path: Path
    name: str
    size: int
    sha256: str

    def discard(self) -> None:
        """Удаляет временный файл (ошибки удаления не важнее основной операции)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001 — временный файл уберёт система
            pass


def safe_name(value: str) -> str:
    """Имя файла без путей и управляющих символов (`..\\x00` не должен влиять на путь).

    Кириллица и пробелы сохраняются: оператор должен узнавать свои файлы, а имя
    в реестре стенда — это метка, по которой он ищет загрузку.
    """
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = text.replace("\\", "/").split("/")[-1]
    text = CONTROL_NAME.sub("", text)
    text = BAD_NAME.sub("_", text).strip(" .")
    return text or DEFAULT_NAME


def save_stream(
    stream: ReadableStream,
    length: int,
    *,
    file_name: str,
    dest_dir: Path | None = None,
    max_bytes: int = MAX_UPLOAD,
    chunk: int = CHUNK,
) -> SavedUpload:
    """Пишет тело запроса во временный файл, считая `sha256` и размер.

    Args:
        stream: поток с телом запроса (`rfile` сервера пульта).
        length: `Content-Length` — сколько байт обязано прийти.
        file_name: имя файла от оператора (используется как основа имени на диске).
        dest_dir: каталог для временного файла (по умолчанию — системный `temp`).
        max_bytes: лимит размера; превышение — `UploadError(status=413)`.
        chunk: размер порции чтения.

    Returns:
        `SavedUpload` — путь, имя, фактический размер и `sha256`.

    Raises:
        UploadError: пустое тело, превышение лимита или обрыв потока.
    """
    name = safe_name(file_name)
    if length <= 0:
        raise UploadError("пустое тело запроса: файл не передан")
    if length > max_bytes:
        raise UploadError(
            f"файл больше лимита пульта: {length} Б > {max_bytes} Б",
            status=413,
        )

    target_dir = (
        Path(dest_dir) if dest_dir is not None else Path(tempfile.mkdtemp(prefix="pult-upload-"))
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 — файл живёт до отправки на стенд
        mode="wb",
        prefix="upload-",
        suffix=Path(name).suffix,
        dir=target_dir,
        delete=False,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with handle:
            remaining = int(length)
            while remaining > 0:
                block = stream.read(min(chunk, remaining))
                if not block:
                    raise UploadError(
                        f"поток прерван: получено {size} Б из {length} Б",
                        status=400,
                    )
                handle.write(block)
                digest.update(block)
                size += len(block)
                remaining -= len(block)
    except UploadError:
        Path(handle.name).unlink(missing_ok=True)
        raise
    except OSError as exc:
        Path(handle.name).unlink(missing_ok=True)
        raise UploadError(f"файл не сохранён: {exc}", status=500) from exc

    return SavedUpload(path=Path(handle.name), name=name, size=size, sha256=digest.hexdigest())
