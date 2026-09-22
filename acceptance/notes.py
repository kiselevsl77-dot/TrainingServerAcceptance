"""Реестр замечаний к API испытуемого сервера (FR-T7).

Замечание — результат испытаний: несоответствие спецификации и/или фактического
поведения сервера. Замечания формируются оператором (вручную, из любого места
пульта) и автоматически — по известным дефектам, выявленным ранее (`KNOWN_DEFECTS`).
Замечания попадают в отчёт отдельным разделом, сгруппированные по приоритету,
а также в требования к API, которые выносятся в перспективный бэклог
(`PROSPECTIVE_REQUIREMENTS` — в первую очередь субдатасеты).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

PRIORITIES = ("P0", "P1", "P2")

PRIORITY_HINTS: dict[str, str] = {
    "P0": "блокирует корректную работу UI/проверки",
    "P1": "мешает, но есть обходной путь",
    "P2": "улучшение или эксплуатационное требование",
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

#: Статусы замечания (`FR-P-37`, `DR-P-6`): дефект живёт от «открыто» до «закрыто».
NOTE_OPEN = "открыто"
NOTE_IN_PROGRESS = "в работе"
NOTE_CLOSED = "закрыто"
NOTE_STATUSES: tuple[str, ...] = (NOTE_OPEN, NOTE_IN_PROGRESS, NOTE_CLOSED)

MODULES = (
    "Система",
    "File Import",
    "Записи и разметка",
    "Loads",
    "Datasets",
    "ML models",
    "ML models: AutoML",
    "Task service",
    "Прочее",
)

SOURCE_OPERATOR = "оператор"
SOURCE_AUTO = "авто"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ApiNote:
    """Замечание к API (ручное или автоматическое)."""

    note_id: str
    created_at: str
    title: str
    module: str = "Прочее"
    endpoint: str = ""
    priority: str = "P1"
    fact: str = ""
    expected: str = ""
    reproduction: str = ""
    check_id: str | None = None
    evidence: str = ""
    source: str = SOURCE_OPERATOR
    #: Статус замечания (`FR-P-37`): «открыто» → «в работе» → «закрыто».
    status: str = NOTE_OPEN
    #: Комментарий последней смены статуса (чем подтверждено исправление).
    status_note: str = ""

    @property
    def priority_rank(self) -> int:
        """Порядок сортировки по приоритету (P0 → 0)."""
        return PRIORITY_ORDER.get(self.priority.upper(), len(PRIORITY_ORDER))

    def to_dict(self) -> dict[str, Any]:
        """Сериализует замечание."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiNote:
        """Восстанавливает замечание из JSON."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload.setdefault("note_id", uuid4().hex[:8])
        payload.setdefault("created_at", _now_iso())
        payload.setdefault("title", "Без названия")
        return cls(**payload)


def new_note(
    title: str,
    *,
    module: str = "Прочее",
    endpoint: str = "",
    priority: str = "P1",
    fact: str = "",
    expected: str = "",
    reproduction: str = "",
    check_id: str | None = None,
    evidence: str = "",
    source: str = SOURCE_OPERATOR,
    status: str = NOTE_OPEN,
) -> ApiNote:
    """Создаёт замечание к API с новым идентификатором."""
    return ApiNote(
        note_id=uuid4().hex[:8],
        created_at=_now_iso(),
        title=title.strip(),
        module=module,
        endpoint=endpoint.strip(),
        priority=priority.upper() if priority.upper() in PRIORITIES else "P1",
        fact=fact.strip(),
        expected=expected.strip(),
        reproduction=reproduction.strip(),
        check_id=check_id,
        evidence=evidence.strip(),
        source=source,
        status=status if status in NOTE_STATUSES else NOTE_OPEN,
    )


def set_status(note: ApiNote, status: str, *, comment: str = "") -> ApiNote:
    """Меняет статус замечания с комментарием (`FR-P-37`, `DR-P-6`).

    Закрытие требует комментария: в отчёте должно быть видно, чем подтверждено исправление
    (перепрогон, новая сборка), — иначе «закрыто» ничем не отличается от «открыто».

    Raises:
        ValueError: если статус вне перечня или закрытие без комментария.
    """
    if status not in NOTE_STATUSES:
        raise ValueError(
            f"статус замечания должен быть из {', '.join(NOTE_STATUSES)}, а не «{status}»"
        )
    text = str(comment or "").strip()
    if status == NOTE_CLOSED and not text:
        raise ValueError("закрытие замечания требует комментария: чем подтверждено исправление")
    note.status = status
    note.status_note = text
    return note


def find_note(notes: Sequence[dict[str, Any] | ApiNote], note_id: str) -> ApiNote | None:
    """Замечание реестра по `note_id` (None — если такого замечания нет)."""
    key = str(note_id or "").strip()
    if not key:
        return None
    for note in notes:
        item = note if isinstance(note, ApiNote) else ApiNote.from_dict(note)
        if item.note_id == key:
            return item
    return None


def apply_status(
    notes: Sequence[dict[str, Any]],
    note_id: str,
    status: str,
    *,
    comment: str = "",
) -> dict[str, Any]:
    """Меняет статус замечания прямо в реестре сессии (`session.notes`).

    Реестр сессии хранит замечания словарями, поэтому проверка статуса идёт через `ApiNote`
    (`set_status`), а затем обновляются два поля — статус и комментарий: остальные данные
    замечания не переписываются.

    Raises:
        ValueError: если замечания нет или статус не проходит проверку ядра.
    """
    target = next(
        (
            note
            for note in notes
            if isinstance(note, dict) and str(note.get("note_id") or "") == str(note_id or "")
        ),
        None,
    )
    if target is None:
        raise ValueError(f"замечание {note_id or '?'} не найдено в реестре сессии")
    checked = set_status(ApiNote.from_dict(dict(target)), status, comment=comment)
    target["status"] = checked.status
    target["status_note"] = checked.status_note
    return target


def sorted_notes(notes: Sequence[dict[str, Any] | ApiNote]) -> list[ApiNote]:
    """Замечания в порядке отчёта: приоритет, затем время создания."""
    restored = [note if isinstance(note, ApiNote) else ApiNote.from_dict(note) for note in notes]
    return sorted(restored, key=lambda note: (note.priority_rank, note.created_at))


def notes_by_priority(
    notes: Sequence[dict[str, Any] | ApiNote],
) -> dict[str, list[ApiNote]]:
    """Замечания, сгруппированные по приоритету (P0/P1/P2)."""
    grouped: dict[str, list[ApiNote]] = {priority: [] for priority in PRIORITIES}
    for note in sorted_notes(notes):
        grouped.setdefault(note.priority, []).append(note)
    return grouped


def has_note(notes: Sequence[dict[str, Any] | ApiNote], title_prefix: str) -> bool:
    """True, если замечание с таким началом заголовка уже зарегистрировано."""
    prefix = title_prefix.strip().lower()
    return any(
        (note.title if isinstance(note, ApiNote) else str(note.get("title", "")))
        .strip()
        .lower()
        .startswith(prefix)
        for note in notes
    )


#: Известные дефекты/пробелы API, проверенные на живом сервисе.
#: Пульт предлагает их как готовые замечания (FR-T7) — испытателю не нужно набирать текст вручную.
KNOWN_DEFECTS: tuple[dict[str, str], ...] = (
    {
        "title": "Скачивание файлов с не-ASCII именами не работает (latin-1)",
        "module": "File Import",
        "endpoint": "GET /api/data/file/{file_id}/download",
        "priority": "P0",
        "fact": "Ответ 404 с detail \"'latin-1' codec can't encode characters…\"; заголовок "
        "Content-Disposition формируется без filename*=UTF-8''.",
        "expected": "Скачивание файла с любым допустимым именем (RFC 6266, filename*), HTTP 200.",
        "reproduction": "Скачать markup-файл субдатасета с не-ASCII именем (например, 3Дпринтер.markup.csv).",
    },
    {
        "title": "Скачивание отдаёт файл целиком: нет Content-Length, Range, ETag",
        "module": "File Import",
        "endpoint": "GET /api/data/file/{file_id}/download",
        "priority": "P1",
        "fact": "Спецификация обещает application/json со схемой {}; фактически "
        "application/octet-stream без Content-Length и без Accept-Ranges (RAW-файлы до 2.1 ГБ).",
        "expected": "Content-Length + поддержка Range/ETag либо presigned URL для больших файлов.",
        "reproduction": "GET download крупного RAW-файла: проверить отсутствие Content-Length и Range.",
    },
    {
        "title": "GET /api/data/files игнорирует limit/offset",
        "module": "File Import",
        "endpoint": "GET /api/data/files",
        "priority": "P1",
        "fact": "Параметры не описаны в спецификации и не влияют на результат (приходит весь реестр).",
        "expected": "Серверная пагинация: limit/offset + общее число записей; либо документировать "
        "полную выдачу.",
        "reproduction": "Запросить GET /api/data/files?limit=5&offset=0 и сравнить count/размер выдачи.",
    },
    {
        "title": (
            "Loads: в ответе не было phase_connection, фильтр ph_n не работал "
            "(закрыто контрактом 17.09.2026)"
        ),
        "module": "Loads",
        "endpoint": "GET /api/loads/list",
        "priority": "P2",
        "fact": "На сборке dev@58ac72f схема ответа в спецификации была пуста; фактически "
        "приходило {loads, result_size, limit, offset} без phase_connection, тогда как POST/PUT "
        "требовали поле, а фильтр ph_n ничего не фильтровал. **Контракт 17.09.2026 закрыл "
        "расхождение**: phase_connection убран из LoadDevice/UpdateLoadRequest (фаза серверу "
        "больше не нужна), параметр ph_n убран из GET /api/loads/list.",
        "expected": "снято контрактом 17.09.2026: поле фазы не требуется, параметра ph_n нет; "
        "не закрыто — ответ списка по-прежнему описан пустой схемой (P1), см. docs/09 "
        "«Контракт API 17.09.2026 — изменения и план работ» §7.3.",
        "reproduction": "POST /api/loads принимает тело без phase_connection; "
        "GET /api/loads/list?ph_n=Ph_A либо игнорирует параметр (200), либо отклоняет (400/422).",
    },
    {
        "title": "Нет сущности «субдатасет» и связи «запись ↔ пара RAW+markup»",
        "module": "Записи и разметка",
        "endpoint": "GET /api/data/files",
        "priority": "P0",
        "fact": "Реестр файлов плоский: RAW и markup одной записи не связаны, дубли имён приходят "
        "разными файлами, различаясь только size/import_date/s3_path.",
        "expected": "Поля пары (record_id/role) либо сущность субдатасета; checksum и updated_at для "
        "различения версий и дублей.",
        "reproduction": "Найти в реестре два файла Antminer_S19.raw.csv с разным размером.",
    },
    {
        "title": "Нет связи «датасет ↔ файлы» и агрегатов (записи/чанки)",
        "module": "Datasets",
        "endpoint": "GET /api/datasets/{dataset_id}",
        "priority": "P1",
        "fact": "Ответ содержит только метаданные: id, creation_date, name, description, type. "
        "Состав датасета и структурные характеристики (число записей и чанков) недоступны.",
        "expected": "GET /api/datasets/{id}/summary с составом и агрегатами либо отдельные эндпоинты.",
        "reproduction": "GET /api/datasets/{dataset_id} — убедиться в отсутствии состава.",
    },
    {
        "title": "POST /api/datasets/fill/{id} не возвращает task_id",
        "module": "Datasets",
        "endpoint": "POST /api/datasets/fill/{dataset_id}",
        "priority": "P1",
        "fact": "Ответ 202 не содержит идентификатора задачи, поэтому мониторинг возможен только "
        "поиском задачи dataset-fill в GET /api/tasks/.",
        "expected": "task_id в ответе (как у train/check/inference).",
        "reproduction": "Выполнить fill и сопоставить ответ с задачами в GET /api/tasks/.",
    },
    {
        "title": "Типы файлов вне перечисления: H5, REPORT_ZIP",
        "module": "File Import",
        "endpoint": "GET /api/data/files",
        "priority": "P2",
        "fact": "В спецификации описаны RAW/LOADS/ONNX, фактически встречаются H5 и REPORT_ZIP.",
        "expected": "Расширить перечисление/справочник file_type и документировать его.",
        "reproduction": "Отфильтровать реестр по file_type=H5 и file_type=REPORT_ZIP.",
    },
    {
        "title": "DELETE /api/{file_id} расположен в корне /api/",
        "module": "File Import",
        "endpoint": "DELETE /api/{file_id}",
        "priority": "P2",
        "fact": "Маршрут удаления файла находится в корне /api/ (риск коллизий), ответ в "
        "спецификации описан пустой схемой, batch-удаления пары нет.",
        "expected": "DELETE /api/data/file/{file_id} и batch-операции для пары/записи.",
        "reproduction": "Вызвать DELETE /api/{file_id} и проверить отсутствие batch-варианта.",
    },
    {
        "title": "В GET /api/tasks/ нет статуса задачи: монитор требует N+1 запросов",
        "module": "Task service",
        "endpoint": "GET /api/tasks/",
        "priority": "P1",
        "fact": "Схема `CeleryTask` (спецификация и живой ответ) содержит только `type`, "
        "`name`, `description`, `id`, `created_at` — статуса нет, он доступен лишь в "
        "карточке `runtimes[-1].status`. Список со статусами = запрос на каждую задачу: "
        "замер на стенде 16.09.2026 — 10 карточек ≈ 7,4 с последовательно и ≈ 0,7 с через "
        "пульт (5 потоков). Фильтр `status` при этом есть, а показать статус по фильтру "
        "нечем. Ни времени завершения (`end_time`/`finished_at`), ни признака архива в "
        "элементе списка нет, поэтому пульт держит локальный снимок статусов "
        "(`acceptance_data/task_snapshot.json`) и ведёт архив задач на своей стороне; "
        "верхняя граница `limit` в спецификации не объявлена — список читается целиком "
        "(проверено: `limit=1000` вернул все задачи).",
        "expected": "Поле `status` (или `last_runtime_status`) в элементе `GET /api/tasks/`; "
        "при необходимости — `last_status_changed_at`, `end_time` и признак архива, чтобы "
        "монитор списка и архив работали без N+1 и без локального файла.",
        "reproduction": "GET /api/tasks/ — в элементах нет `status`; затем GET /api/tasks/{id} "
        "по каждой задаче списка: статус есть только в `runtimes[]`.",
    },
    {
        "title": "Сортировка задач не поддерживается, порядок выдачи не хронологический",
        "module": "Task service",
        "endpoint": "GET /api/tasks/",
        "priority": "P2",
        "fact": "Параметров сортировки (`sort`/`order`) в спецификации нет; порядок ответа "
        "не совпадает с порядком создания: при `limit=100` последовательность `created_at` "
        "идёт от 2026-09-09T14:02:44 к 2026-09-15T08:48:20, но не является возрастающей. "
        "Пульт сортирует список сам (`created_at` ↓, затем `id`) и раскладывает его по "
        "страницам на клиенте — на этом держатся виды «Активные | Архив | Все».",
        "expected": "Параметры сортировки (по `created_at`, тип, статус) или гарантированный "
        "порядок «сначала новые»; в пульте — сортировка клиентской таблицы по столбцу.",
        "reproduction": "GET /api/tasks/?limit=100 — сравнить `created_at` элементов: "
        "`sorted(values) != values`.",
    },
    {
        "title": "Семантика фильтров периода в GET /api/tasks/ не описана",
        "module": "Task service",
        "endpoint": "GET /api/tasks/",
        "priority": "P2",
        "fact": "start_date/end_date объявлены как `string`/`format: date-time`, но "
        "семантика границ не описана: строка только с датой отклоняется 422 "
        "(datetime_parsing), end_date — исключающая граница (created_at < end_date), "
        "а на start_date задача, созданная за 0,5 с до границы, ещё попадает в выборку.",
        "expected": "Описание семантики границ (включающая/исключающая) и допустимых "
        "форматов в спецификации; при необходимости — приём строки только с датой.",
        "reproduction": "GET /api/tasks/?start_date=2026-09-09 → 422; "
        "start_date=2026-09-09T00:00:00&end_date=2026-09-10T00:00:00 → сутки 09.09 целиком.",
    },
    {
        "title": "Удаление нагрузки отсутствует, категория — свободный текст",
        "module": "Loads",
        "endpoint": "GET /api/loads/list",
        "priority": "P2",
        "fact": "Эндпоинта удаления нагрузки нет; category — свободный текст с регистровыми "
        "дублями, load_id/category фильтруются только точно.",
        "expected": "DELETE /api/loads/{load_id}, справочник категорий, регистронезависимые фильтры.",
        "reproduction": "Создать нагрузку через POST /api/loads и попытаться её удалить.",
    },
    {
        "title": "У моделей не заполнено поле signals",
        "module": "ML models",
        "endpoint": "GET /api/ml_models/models",
        "priority": "P2",
        "fact": "signals пусто, классы описаны только в signal_aliases и не связаны с реестром нагрузок.",
        "expected": "Заполнять signals при создании модели и/или полю device_id в реестре нагрузок.",
        "reproduction": "GET /api/ml_models/models — проверить пустые signals у живых моделей.",
    },
)


#: Перспективные требования к API (не блокируют испытания текущей версии сервера).
PROSPECTIVE_REQUIREMENTS: tuple[dict[str, str], ...] = (
    {
        "title": "Субдатасет как сущность первого класса",
        "module": "Записи и разметка",
        "priority": "P1",
        "detail": "POST/GET /api/subdatasets и /api/subdatasets/{id}/files; агрегаты (число записей "
        "и чанков); признак группировки (дата измерений, назначение). Альтернатива — поля пары в "
        "файле (record_id/pair_id/role) и группирующий признак.",
    },
    {
        "title": "Агрегаты и структурные характеристики",
        "module": "Datasets",
        "priority": "P1",
        "detail": "GET /api/datasets/{id}/summary (состав, записи, чанки, нагрузки) и серверные "
        "характеристики субдатасета вместо клиентского разбора markup-файла.",
    },
    {
        "title": "Связи load ↔ субдатасет ↔ датасет",
        "module": "Loads",
        "priority": "P1",
        "detail": "Позволяют отказаться от эвристического «набора нагрузок» (по markup-файлу или по "
        "имени субдатасета) и от ручного сравнения с реестром (замечание №2 к макету).",
    },
    {
        "title": "Справочники и единый контракт",
        "module": "Прочее",
        "priority": "P1",
        "detail": "Справочники фаз (Ph_n), категорий, file_type, DatasetType; единый формат ошибок; "
        "описание формата markup-файла; updated_at и checksum для изменяемых сущностей.",
    },
    {
        "title": "Эксплуатационные улучшения",
        "module": "Прочее",
        "priority": "P2",
        "detail": "presigned URL/Range для больших файлов, batch-операции, сортировка, ETag, "
        "securitySchemes/X-Api-Version, примеры в спецификации.",
    },
)


def known_defect_notes() -> list[ApiNote]:
    """Готовые замечания по известным дефектам (шаблоны для оператора)."""
    return [
        new_note(
            defect["title"],
            module=defect["module"],
            endpoint=defect["endpoint"],
            priority=defect["priority"],
            fact=defect["fact"],
            expected=defect["expected"],
            reproduction=defect["reproduction"],
            source=SOURCE_AUTO,
        )
        for defect in KNOWN_DEFECTS
    ]


def prospective_requirements() -> list[dict[str, str]]:
    """Перспективные требования к API (раздел отчёта, не блокирует испытания)."""
    return [dict(item) for item in PROSPECTIVE_REQUIREMENTS]


# ---------------------------------------------------------------------------
# Дефекты API и проверки чек-листа (этап T3, FR-T7)
# ---------------------------------------------------------------------------
#: Проверка чек-листа → заголовок известного дефекта (`KNOWN_DEFECTS`).
#: По этой связи движок формирует замечание автоматически, когда проверка получает
#: статус «блокировано API»: испытателю не нужно искать текст замечания вручную.
DEFECT_BY_CHECK: dict[str, str] = {
    "TC-FILE-10": "Скачивание файлов с не-ASCII именами",
    "TC-REC-05": "Скачивание файлов с не-ASCII именами",
    "TC-REC-04": "Нет сущности «субдатасет» и связи «запись ↔ пара RAW+markup»",
    "TC-FILE-05": "GET /api/data/files игнорирует limit/offset",
    "TC-FILE-09": "Скачивание отдаёт файл целиком: нет Content-Length, Range, ETag",
    "TC-LOAD-06": "Удаление нагрузки отсутствует, категория — свободный текст",
    # этап T5: замечания к модулю «Datasets» — состав/агрегаты и обходной путь задачи fill
    "TC-DS-03": "Нет связи «датасет ↔ файлы» и агрегатов (записи/чанки)",
    "TC-DS-05": "POST /api/datasets/fill/{id} не возвращает task_id",
}


def defect_for(check_id: str) -> dict[str, str] | None:
    """Известный дефект API, связанный с проверкой чек-листа (None — связи нет)."""
    title = DEFECT_BY_CHECK.get(str(check_id).strip().upper())
    if not title:
        return None
    return next(
        (dict(defect) for defect in KNOWN_DEFECTS if title.lower() in defect["title"].lower()),
        None,
    )


def note_from_check(check_id: str, *, evidence: str = "") -> ApiNote | None:
    """Готовое замечание к API по проверке, для которой известен дефект (FR-T7)."""
    defect = defect_for(check_id)
    if defect is None:
        return None
    return new_note(
        defect["title"],
        module=defect["module"],
        endpoint=defect["endpoint"],
        priority=defect["priority"],
        fact=defect["fact"],
        expected=defect["expected"],
        reproduction=defect["reproduction"],
        check_id=str(check_id).strip().upper(),
        evidence=evidence,
        source=SOURCE_AUTO,
    )
