"""Судьба артефактов ресурсоёмких проверок: кандидаты и решения (FR-T10, шаг 6).

Тяжёлые проверки оставляют на стенде дорого добытый результат — обученную модель
(`TC-TR-02`/`TC-TR-04`) и выгруженный файл `.h5`/`.onnx` (`TC-TR-06`, `TC-INF-01`).
Убрать его молча нельзя (результат нужен отчёту), оставить «как есть» — тоже: стенд
общий (`NFR-T4`). Поэтому решение принимает **оператор**, а пульт показывает по
каждому объекту, что уже решено (`FR-T10`, `TC-CLEAN-04`).

Модуль чистый: стенд читает сервер (`pult.server._artifacts_view`), здесь — только
правила. Три вещи, которые легко сделать по-разному и потому описаны явно:

    * **кто кандидат** — объект **стенда**, а не запись сессии: модель или файл
      `.h5`/`.onnx` с префиксом `__TEST__`. Так в список попадает и то, что оставил
      прошлый прогон: оператор видит чужие следы и решает их судьбу (они помечены
      «прошлый прогон», `ours=False`);
    * **какие действия допустимы** — `keep` (сохранить артефактом испытаний) и
      `delete` (убрать со стенда). Принимаются и формулировки карточки запуска
      (`run_card.ARTIFACT_ACTIONS`): оператор выбирает судьбу до прогона и
      подтверждает её после;
    * **как решение видно отчёту** — записью в `session.artifacts`
      (`session.add_artifact`, поля `entity_id` и `action`): таблица решений одна
      для всех тяжёлых проверок, по ней закрывается `TC-CLEAN-04`.

Запись `.h5` тоже кандидат: удаление модели убирает её каскадом не всегда
(`TC-MOD-07`), а скачанный ONNX остаётся доказательством, даже если модель со
стенда снята.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from acceptance.endpoints import TEST_PREFIX
from acceptance.session import ARTIFACT_DELETED, ARTIFACT_KEPT

#: Действия оператора: сохранить артефактом испытаний или убрать со стенда.
DECISION_KEEP = "keep"
DECISION_DELETE = "delete"
DECISIONS: tuple[str, ...] = (DECISION_KEEP, DECISION_DELETE)

#: Подписи кнопок панели «Артефакты» (та же формулировка в API и в отчёте).
DECISION_LABELS: dict[str, str] = {
    DECISION_KEEP: "💾 в артефакты",
    DECISION_DELETE: "🗑 удалить",
}

#: Слова карточки запуска (`run_card.ARTIFACT_ACTIONS`) → действие панели.
CARD_DECISIONS: dict[str, str] = {
    "удалить после проверки": DECISION_DELETE,
    "оставить для разбора": DECISION_KEEP,
    "выгрузить в доказательства": DECISION_KEEP,
}

#: Виды объектов стенда, судьбу которых решает оператор.
ENTITY_MODEL = "model"
ENTITY_FILE = "file"
ENTITY_KINDS: tuple[str, ...] = (ENTITY_MODEL, ENTITY_FILE)

KIND_LABELS: dict[str, str] = {ENTITY_MODEL: "модель", ENTITY_FILE: "файл"}

#: Порядок видов в панели: сначала модели (главное решение — обученный результат),
#: затем файлы. Ключи — те же, что в `ENTITY_KINDS`.
KIND_ORDER: dict[str, int] = {ENTITY_MODEL: 0, ENTITY_FILE: 1}

#: Расширения файлов, которые считаются результатом ресурсоёмкой проверки.
ARTIFACT_SUFFIXES: tuple[str, ...] = (".h5", ".onnx")


def normalize_decision(raw: Any) -> str:
    """Действие оператора из текста запроса (пустая строка — действие не распознано).

    Принимаются и слова панели (`keep`/`delete`, регистр не важен), и формулировки
    карточки запуска: оператор видит их в карточке и вправе ожидать, что пульт
    поймёт их и в решении.
    """
    value = str(raw or "").strip().lower()
    if value in DECISIONS:
        return value
    return CARD_DECISIONS.get(value, "")


def decision_word(action: Any) -> str:
    """Слово панели по записи сессии: `keep` / `delete` (пусто — решения не было).

    В сессии решение хранится словами отчёта (`ARTIFACT_KEPT` / `ARTIFACT_DELETED`),
    а интерфейсу нужно машинное значение — по нему подсвечивается принятое решение
    и строится запрос к `POST /api/artifacts`. Соответствие держится здесь, рядом с
    `normalize_decision`, чтобы слова не разошлись в двух местах.
    """
    text = str(action or "")
    if text == ARTIFACT_KEPT:
        return DECISION_KEEP
    if text == ARTIFACT_DELETED:
        return DECISION_DELETE
    return ""


def is_artifact_file_name(name: Any) -> bool:
    """True, если файл стенда — результат ресурсоёмкой проверки (`.h5`/`.onnx`)."""
    text = str(name or "").strip().lower()
    return bool(text) and text.endswith(ARTIFACT_SUFFIXES)


def candidate_rows(
    models: Iterable[Any], files: Iterable[Any], *, session_id: str = ""
) -> list[dict[str, Any]]:
    """Кандидаты на решение: `__TEST__`-модели и файлы `.h5`/`.onnx` стенда.

    Принимает объекты реестров (`client.schemas.ModelMetadata`,
    `FileMetadataResponse`) — строки собираются из тех же данных, что показаны в
    «Данных стенда», и не расходятся с ними. Сортировка: сначала следы своей
    сессии, затем чужие; внутри — по виду и имени (порядок стабилен для протокола).
    """
    marker = str(session_id or "")
    rows: list[dict[str, Any]] = []

    for model in models:
        name = str(getattr(model, "name", "") or "")
        if not name.startswith(TEST_PREFIX):
            continue
        rows.append(
            {
                "kind": ENTITY_MODEL,
                "entity_id": str(getattr(model, "id", "") or ""),
                "name": name,
                "detail": str(getattr(model, "architecture", "") or ""),
                "version": str(getattr(model, "version", "") or ""),
                "size_bytes": None,
                "created_at": str(getattr(model, "created_at", "") or ""),
                "ours": bool(marker) and marker in name,
            }
        )

    for file in files:
        name = str(getattr(file, "file_name", "") or "")
        if not name.startswith(TEST_PREFIX) or not is_artifact_file_name(name):
            continue
        rows.append(
            {
                "kind": ENTITY_FILE,
                "entity_id": str(getattr(file, "id", "") or ""),
                "name": name,
                "detail": str(getattr(file, "file_type", "") or ""),
                "version": "",
                "size_bytes": int(getattr(file, "size", 0) or 0),
                "created_at": str(getattr(file, "import_date", "") or ""),
                "ours": bool(marker) and marker in name,
            }
        )

    rows.sort(key=lambda row: (not row["ours"], KIND_ORDER.get(row["kind"], 9), row["name"]))
    return rows


def decision_index(records: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Индекс решений по артефактам сессии: `entity_id` → последняя запись решения.

    Решение может быть переписано: оператор вправе передумать (сначала «оставить»,
    потом убрать). Побеждает последняя запись — по ней и отчитывается сессия, и по
    ней видно, чем дело закончилось (`FR-T10`). Записи без `entity_id` (выгрузки
    доказательств) в индекс не попадают: это не решения.
    """
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        data = dict(record) if isinstance(record, dict) else {}
        entity_id = str(data.get("entity_id") or "")
        if entity_id and str(data.get("action") or ""):
            index[entity_id] = data
    return index


def summary(rows: Iterable[dict[str, Any]], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Итог по панели: сколько решений принято и сколько объектов ещё ждут.

    Считаются две разные вещи, и путать их нельзя:

    * **решения** (`kept`/`deleted`) — из сессии: объект может быть уже убран со
      стенда, но решение о нём осталось и видно отчёту (`FR-T10`);
    * **ждут решения** (`undecided`) — из списка кандидатов стенда, у которых записи
      нет. Это вход `TC-CLEAN-04`: проверка выполняется, когда ждущих не осталось.
    """
    listed = list(rows)
    kept = sum(1 for item in index.values() if str(item.get("action") or "") == ARTIFACT_KEPT)
    deleted = len(index) - kept
    pending = sum(1 for row in listed if str(row.get("entity_id") or "") not in index)
    ours = sum(1 for row in listed if row.get("ours"))
    return {
        "total": len(listed),
        "ours": ours,
        "foreign": len(listed) - ours,
        "kept": kept,
        "deleted": deleted,
        "decided": kept + deleted,
        "undecided": pending,
    }


def conclusion(rows: Iterable[dict[str, Any]], index: dict[str, dict[str, Any]]) -> str:
    """Фраза для оператора: что решено и что ещё ждёт (вход `TC-CLEAN-04`).

    После удаления объекта список кандидатов пустеет, но решение о нём остаётся в
    сессии: панель не должна говорить «кандидатов нет» там, где работа сделана —
    иначе оператор ставит `TC-CLEAN-04` вслепую (найдено живой приёмкой 01.10.2026).
    """
    data = summary(rows, index)
    if not data["total"]:
        if not data["decided"]:
            return "Кандидатов нет: тяжёлые проверки не оставили объектов на стенде."
        return (
            "Объектов на стенде не осталось; решения зафиксированы: "
            f"сохранено артефактами {data['kept']}, удалено {data['deleted']} "
            "(решения есть у каждого объекта)"
        )
    parts = [
        f"объектов: {data['total']}",
        f"сохранено артефактами: {data['kept']}",
        f"удалено: {data['deleted']}",
    ]
    if data["undecided"]:
        parts.append(f"ждут решения: {data['undecided']} — TC-CLEAN-04 ставить рано")
    else:
        parts.append("решение принято по каждому объекту (TC-CLEAN-04)")
    return "; ".join(parts)
