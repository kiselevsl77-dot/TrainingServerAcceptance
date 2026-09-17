"""Каталог проверок чек-листа: группы `TC-*` и описания проверок (FR-T4).

Каталог — программное представление программы испытаний (`docs/02. Чек-лист
испытаний.md`): каждая проверка описана `CheckSpec` (модель `registry.py`) с
трассировкой на требования постановки, классом, шагами, ожидаемым результатом и
эндпоинтами испытуемого API.

Этап T4 наполнил каталог **первой группой — `TC-TASK`** (8 проверок модуля
«Task service»): монитор задач — фундамент всех асинхронных проверок, поэтому
инфраструктура чек-листа появилась сразу с рабочим сценарием. Этап T3 добавляет
группы `TC-SYS`, `TC-FILE`, `TC-REC`, `TC-LOAD` (33 проверки) и вместе с ними —
первую версию отчёта. Структура каталога рассчитана на все 10 групп
(69 проверок, `PLANNED_GROUPS`): группы T5–T10 добавляются без переделки —
достаточно дописать кортеж проверок и зарегистрировать группу в `CHECKS_BY_GROUP`.

Сверка каталога с таблицей чек-листа (`docs/02`): id, название, группа и класс —
тест `tests/unit/test_checks_catalog.py` (по образцу `test_endpoints.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from acceptance.checks.registry import CheckClass, CheckSpec


@dataclass(frozen=True)
class GroupPlan:
    """План группы проверок: полный состав каталога (10 групп, 69 проверок)."""

    key: str
    title: str
    module: str
    stage: str
    checks_total: int


#: Полный состав программы испытаний (`docs/02` §14–§15). Этап указывает, на
#: каком этапе пульта группа наполняется проверками (T3 — системы/файлы/записи/
#: нагрузки, T4 — задачи, T5 — датасеты, …).
PLANNED_GROUPS: tuple[GroupPlan, ...] = (
    GroupPlan("TC-SYS", "TC-SYS — модуль «Система»", "System", "T3", 6),
    GroupPlan("TC-FILE", "TC-FILE — модуль «File Import»", "File Import", "T3", 14),
    GroupPlan("TC-REC", "TC-REC — записи (RAW + markup)", "Записи и разметка", "T3", 6),
    GroupPlan("TC-LOAD", "TC-LOAD — модуль «Loads»", "Loads", "T3", 7),
    GroupPlan("TC-TASK", "TC-TASK — модуль «Task service»", "Task service", "T4", 8),
    GroupPlan("TC-DS", "TC-DS — модуль «Datasets»", "Datasets", "T5", 7),
    GroupPlan("TC-MOD", "TC-MOD — модуль «ML models»", "ML models", "T6", 7),
    GroupPlan("TC-TR", "TC-TR — обучение, проверка, ONNX", "ML models", "T7", 6),
    GroupPlan("TC-INF", "TC-INF — инференс", "ML models", "T8", 4),
    GroupPlan("TC-CLEAN", "TC-CLEAN — уборка и учёт ресурсов", "Прочее", "T9", 4),
)

PLANNED_CHECKS_TOTAL = sum(group.checks_total for group in PLANNED_GROUPS)


@dataclass(frozen=True)
class CheckGroup:
    """Группа проверок одного модуля (наполняется по этапам)."""

    plan: GroupPlan
    checks: tuple[CheckSpec, ...] = ()

    @property
    def key(self) -> str:
        """Ключ группы (`TC-TASK`)."""
        return self.plan.key

    @property
    def title(self) -> str:
        """Заголовок группы для интерфейса."""
        return self.plan.title

    @property
    def module(self) -> str:
        """Модуль испытуемого API, к которому относится группа."""
        return self.plan.module

    @property
    def stage(self) -> str:
        """Этап пульта, на котором группа наполняется проверками."""
        return self.plan.stage

    @property
    def is_implemented(self) -> bool:
        """True, если проверки группы уже описаны в каталоге."""
        return bool(self.checks)

    @property
    def is_complete(self) -> bool:
        """True, если в группе описан полный состав проверок по программе."""
        return len(self.checks) == self.plan.checks_total


# ---------------------------------------------------------------------------
# TC-SYS — модуль «Система» (FR-1, UC-01, BR-R8, NFR-2) — этап T3
# ---------------------------------------------------------------------------
SYSTEM_MODULE = "Система"

TC_SYS_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        check_id="TC-SYS-01",
        title="Доступность сервиса",
        module=SYSTEM_MODULE,
        requirement="FR-1, UC-01, BR-R8",
        check_class=CheckClass.TECH,
        steps=(
            "GET /health без параметров",
            "Зафиксировать код ответа и тело",
            "Убедиться, что пульт считает сервис доступным и отмечает это в журнале",
        ),
        expected="HTTP 200, сервис помечен доступным",
        endpoints=("get /health",),
        automation="system.health",
    ),
    CheckSpec(
        check_id="TC-SYS-02",
        title="Версия сборки и её состав",
        module=SYSTEM_MODULE,
        requirement="FR-1, BR-R8",
        check_class=CheckClass.TECH,
        steps=(
            "GET /version",
            "Сверить состав ответа с ожидаемым: `branch`, `revision`, `commit`, `build_date`",
            "Сохранить сборку в сессии (снимок стенда и шапка отчёта)",
        ),
        expected=(
            "HTTP 200; в ответе есть `branch`, `revision`, `commit`, `build_date` "
            "(или зафиксировано, чего нет); сборка сохранена в сессии"
        ),
        endpoints=("get /version",),
        automation="system.version",
    ),
    CheckSpec(
        check_id="TC-SYS-03",
        title="Поведение на неизвестном пути",
        module=SYSTEM_MODULE,
        requirement="NFR-2",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/__test_unknown__ (пути нет в спецификации)",
            "Зафиксировать код ответа: ожидается 404, а не 5xx",
            'Разобрать тело ошибки как `{"detail": …}`',
        ),
        expected='HTTP 404 (не 5xx), ошибка разобрана как `{"detail": …}`',
        probe_paths=("GET /api/__test_unknown__",),
        automation="system.unknown_path",
    ),
    CheckSpec(
        check_id="TC-SYS-04",
        title="Неизвестная задача",
        module=SYSTEM_MODULE,
        requirement="BR-R7, UC-27",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/tasks/{несуществующий_uuid}",
            "Зафиксировать вариант ответа: статус `not_found` либо 404",
            "Убедиться, что пульт различает «не найдена» и «сервер недоступен»",
        ),
        expected="статус `not_found` либо 404 — вариант зафиксирован и понятен UI",
        endpoints=("get /api/tasks/{task_id}",),
        automation="system.unknown_task",
    ),
    CheckSpec(
        check_id="TC-SYS-05",
        title="Единый формат ошибок",
        module=SYSTEM_MODULE,
        requirement="NFR-2, BR-R2",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/files?id=not-a-uuid",
            "GET /api/datasets/{not-uuid}",
            "Сравнить формат ошибки валидации: `HTTPValidationError.detail[]` либо строка",
        ),
        expected=(
            "согласованный формат ошибки валидации (`HTTPValidationError.detail[]` либо "
            "строка); расхождения фиксируются как замечание"
        ),
        endpoints=("get /api/data/files", "get /api/datasets/{dataset_id}"),
        automation="system.error_format",
    ),
    CheckSpec(
        check_id="TC-SYS-06",
        title="Различимость сборок",
        module=SYSTEM_MODULE,
        requirement="BR-R8",
        check_class=CheckClass.MANUAL,
        steps=(
            "Сравнить `GET /version` текущей сессии со сборкой предыдущей сессии испытаний",
            "Проверить, что `revision`/`build_date` позволяют отличить сборки",
            "Зафиксировать заключение оператора с указанием обеих сборок",
        ),
        expected="оператор подтверждает, что `revision`/`build_date` позволяют отличить сборки",
        endpoints=("get /version",),
        automation=None,
    ),
)


# ---------------------------------------------------------------------------
# TC-FILE — модуль «File Import» (FR-2, UC-02…UC-05) — этап T3
# ---------------------------------------------------------------------------
FILE_MODULE = "File Import"

TC_FILE_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        check_id="TC-FILE-01",
        title="Реестр файлов получен полностью",
        module=FILE_MODULE,
        requirement="UC-03, FR-2, BR-F1",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/files без фильтров",
            "Сверить `count` с числом элементов `files`",
            "Проверить, что каждый элемент содержит обязательные поля метаданных",
        ),
        expected="HTTP 200; `count` = числу элементов `files`",
        endpoints=("get /api/data/files",),
        automation="files.list_registry",
    ),
    CheckSpec(
        check_id="TC-FILE-02",
        title="Фильтр `file_name` — подстрока",
        module=FILE_MODULE,
        requirement="UC-03, NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "Взять часть имени из живого реестра",
            "GET /api/data/files?file_name=<подстрока>",
            "Проверить, что выдача — подмножество полного реестра и все имена содержат подстроку",
        ),
        expected="выдача — подмножество, все имена содержат подстроку",
        endpoints=("get /api/data/files",),
        automation="files.name_filter",
    ),
    CheckSpec(
        check_id="TC-FILE-03",
        title="Фильтр `file_type` — точный и регистрозависимый",
        module=FILE_MODULE,
        requirement="UC-03, NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/files?file_type=RAW",
            "GET /api/data/files?file_type=raw",
            "Сравнить выдачи; расхождение со спецификацией зафиксировать замечанием",
        ),
        expected="`RAW` даёт непустую выдачу, `raw` — пустую; расхождение фиксируется замечанием",
        endpoints=("get /api/data/files",),
        automation="files.type_filter",
    ),
    CheckSpec(
        check_id="TC-FILE-04",
        title="Фильтр `import_date` — только дата",
        module=FILE_MODULE,
        requirement="UC-03, BR-R2",
        check_class=CheckClass.TECH,
        steps=(
            "Взять дату импорта из живого реестра",
            "GET /api/data/files?import_date=YYYY-MM-DD",
            "Проверить, что в выдаче только файлы этой календарной даты",
        ),
        expected="фильтрация по календарной дате; невозможность фильтра по времени зафиксирована",
        endpoints=("get /api/data/files",),
        automation="files.import_date_filter",
    ),
    CheckSpec(
        check_id="TC-FILE-05",
        title="Пагинация `limit`/`offset`",
        module=FILE_MODULE,
        requirement="NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/files?limit=5&offset=0",
            "Сравнить размер выдачи с полным реестром",
            "Зафиксировать факт: пагинация серверная или клиентская",
        ),
        expected="если параметры игнорируются — замечание к API; UI использует клиентскую пагинацию",
        endpoints=("get /api/data/files",),
        automation="files.pagination",
    ),
    CheckSpec(
        check_id="TC-FILE-06",
        title="Объём реестра и суммарный размер",
        module=FILE_MODULE,
        requirement="UC-03",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/files",
            "Посчитать число файлов, суммарный размер и распределение по типам",
            "Сверить показатели со снимком стенда",
        ),
        expected="счётчики, суммарный размер и распределение по типам попадают в снимок стенда",
        endpoints=("get /api/data/files",),
        automation="files.volume",
    ),
    CheckSpec(
        check_id="TC-FILE-07",
        title="Дубли имён файлов",
        module=FILE_MODULE,
        requirement="замечание №1, BR-R2",
        check_class=CheckClass.TECH,
        steps=(
            "Найти имена, встречающиеся в реестре дважды",
            "Сравнить версии по `id`/`size`/`import_date`/`s3_path`",
            "Зафиксировать отсутствие `checksum`/`updated_at`",
        ),
        expected=(
            "для одноимённых файлов найдено ≥ 2 записи, различающиеся "
            "`id`/`size`/`import_date`/`s3_path`; отсутствие `checksum`/`updated_at` зафиксировано"
        ),
        endpoints=("get /api/data/files",),
        automation="files.duplicates",
    ),
    CheckSpec(
        check_id="TC-FILE-08",
        title="Скачивание файла (ASCII-имя)",
        module=FILE_MODULE,
        requirement="UC-04, BR-F1",
        check_class=CheckClass.TECH,
        steps=(
            "Выбрать файл с ASCII-именем и markup-типом (малый размер)",
            "GET /api/data/file/{file_id}/download",
            "Сверить `Content-Type`, имя в `Content-Disposition` и размер с `size` реестра",
        ),
        expected=(
            "HTTP 200, `application/octet-stream`, имя в `Content-Disposition`, "
            "размер совпадает с `size` реестра"
        ),
        endpoints=("get /api/data/file/{file_id}/download",),
        automation="files.download_ascii",
    ),
    CheckSpec(
        check_id="TC-FILE-09",
        title="Заголовки скачивания",
        module=FILE_MODULE,
        requirement="замечание к API №2",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/file/{file_id}/download того же файла",
            "Проверить наличие `Content-Length`, `Accept-Ranges`, `ETag`",
            "Расхождение со спецификацией оформить замечанием к API",
        ),
        expected=(
            "проверяется отсутствие `Content-Length`/`Accept-Ranges`/`ETag`; "
            "результат — замечание к API"
        ),
        endpoints=("get /api/data/file/{file_id}/download",),
        automation="files.download_headers",
    ),
    CheckSpec(
        check_id="TC-FILE-10",
        title="Скачивание файла с не-ASCII именем",
        module=FILE_MODULE,
        requirement="замечание к API (P0)",
        check_class=CheckClass.TECH,
        steps=(
            "Найти в реестре файл с не-ASCII именем",
            "GET /api/data/file/{file_id}/download",
            "Зафиксировать статус и текст ошибки (ожидается 404 latin-1)",
        ),
        expected="**ожидаемо блокировано**: `404 latin-1`; замечание формируется автоматически",
        endpoints=("get /api/data/file/{file_id}/download",),
        blocked_by_api="скачивание не-ASCII имён (404 latin-1)",
        automation="files.download_non_ascii",
    ),
    CheckSpec(
        check_id="TC-FILE-11",
        title="Загрузка CSV без обязательных колонок",
        module=FILE_MODULE,
        requirement="UC-02, BR-F1",
        check_class=CheckClass.LIVE,
        steps=(
            "Собрать CSV без обязательных колонок (только заголовок и одна строка)",
            "POST /api/data/file (multipart, имя `__TEST__…csv`, `file_type=RAW`)",
            "Зафиксировать код ответа и текст `detail`; убедиться, что файл не создан",
        ),
        expected="HTTP 400 со строкой `detail`; текст показывается оператору",
        endpoints=("post /api/data/file",),
        automation="files.upload_invalid_csv",
    ),
    CheckSpec(
        check_id="TC-FILE-12",
        title="Загрузка с MIME `application/octet-stream`",
        module=FILE_MODULE,
        requirement="замечание к API №5",
        check_class=CheckClass.LIVE,
        steps=(
            "Отправить валидный CSV частью с `Content-Type: application/octet-stream`",
            "POST /api/data/file",
            "Зафиксировать, что сервер проверяет MIME-тип части, а не содержимое",
        ),
        expected=(
            "HTTP 400 «Only CSV files are allowed»: фиксируется, что проверяется MIME-тип "
            "части, а не содержимое"
        ),
        endpoints=("post /api/data/file",),
        automation="files.upload_wrong_mime",
    ),
    CheckSpec(
        check_id="TC-FILE-13",
        title="Round-trip `upload → list → download → delete`",
        module=FILE_MODULE,
        requirement="UC-02…UC-05",
        check_class=CheckClass.LIVE,
        steps=(
            "Загрузить CSV `__TEST__*.csv` (multipart, `file_type=RAW`, MIME `text/csv`)",
            "Найти файл в реестре фильтром по `file_id`",
            "Скачать файл и сверить байты с загруженными",
            "Удалить файл (`DELETE /api/{file_id}`) и убедиться, что следов в реестре нет",
        ),
        expected=(
            "файл `__TEST__*.csv` создан, найден в реестре, скачан (байты совпадают), удалён; "
            "следов в реестре не осталось"
        ),
        endpoints=(
            "post /api/data/file",
            "get /api/data/files",
            "get /api/data/file/{file_id}/download",
            "delete /api/{file_id}",
        ),
        automation="files.round_trip",
    ),
    CheckSpec(
        check_id="TC-FILE-14",
        title="Удаление несуществующего файла",
        module=FILE_MODULE,
        requirement="UC-05, NFR-2",
        check_class=CheckClass.TECH,
        steps=(
            "DELETE /api/{uuid} для файла, которого нет в реестре",
            "Зафиксировать код ответа (ожидается 404 или иной согласованный код)",
            "Отметить отсутствие batch-удаления пары RAW+markup",
        ),
        expected="HTTP 404 (или иной согласованный код) — фиксируется; batch-удаления пары нет",
        endpoints=("delete /api/{file_id}",),
        automation="files.delete_missing",
    ),
)


# ---------------------------------------------------------------------------
# TC-REC — записи (RAW + markup), дубли и разметка — этап T3
# ---------------------------------------------------------------------------
RECORDS_MODULE = "Записи и разметка"

TC_REC_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        check_id="TC-REC-01",
        title="Объединение RAW+markup в записи",
        module=RECORDS_MODULE,
        requirement="замечание №1 к макету",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/data/files — прочитать живой реестр",
            "Объединить файлы в записи правилом «имя + время импорта»",
            "Зафиксировать правило, уверенность и число записей в доказательствах",
        ),
        expected=(
            "записи сформированы по явному правилу (имя + время импорта); правило и "
            "уверенность показаны в таблице и карточке; число записей зафиксировано в отчёте"
        ),
        endpoints=("get /api/data/files",),
        automation="records.pairing",
    ),
    CheckSpec(
        check_id="TC-REC-02",
        title="Обработка дублей при сопоставлении",
        module=RECORDS_MODULE,
        requirement="замечание №1, п. 3.1 ТЗ",
        check_class=CheckClass.TECH,
        steps=(
            "Найти базы имён с несколькими версиями записи",
            "Сопоставить версии с разметкой и собрать манифест объединения",
            "Проверить контроль полноты: каждый файл реестра ровно один раз",
        ),
        expected=(
            "видны все версии записи (`id`/`size`/`import_date`/`s3_path`); контроль полноты "
            "манифеста пройден; результат — манифест CSV/JSON"
        ),
        endpoints=("get /api/data/files",),
        automation="records.duplicates_manifest",
    ),
    CheckSpec(
        check_id="TC-REC-03",
        title="Ручная перепривязка пары",
        module=RECORDS_MODULE,
        requirement="п. 3.1 ТЗ",
        check_class=CheckClass.MANUAL,
        steps=(
            "Открыть экран «Записи» → вкладку «Дубли» (или «Несопоставленные»)",
            "Привязать markup к RAW / свободную разметку к записи без разметки",
            "Проверить, что решение попало в `records_overrides.json`, журнал и историю сессии",
        ),
        expected=(
            "оператор привязывает markup к RAW при дублях и свободную разметку к записи без "
            "разметки; решение зафиксировано в `records_overrides.json`, журнале и истории "
            "сессии (с ФИО и комментарием)"
        ),
        automation=None,
    ),
    CheckSpec(
        check_id="TC-REC-04",
        title="Несопоставленные файлы",
        module=RECORDS_MODULE,
        requirement="п. 3.1 ТЗ",
        check_class=CheckClass.TECH,
        steps=(
            "Выделить записи без разметки и markup без RAW",
            "Проверить, что прочие файлы (`H5`, `REPORT_ZIP`, `.onnx`) не попали в записи",
            "Проверить, что при этом они присутствуют в манифесте",
        ),
        expected=(
            "записи без разметки и разметка без RAW выделены в отдельные разделы с пояснением; "
            "прочие файлы (`H5`, `REPORT_ZIP`, `.onnx`) не попадают в записи, но есть в манифесте"
        ),
        endpoints=("get /api/data/files",),
        automation="records.unpaired",
    ),
    CheckSpec(
        check_id="TC-REC-05",
        title="Записи и чанки разметки",
        module=RECORDS_MODULE,
        requirement="замечания №3/№4",
        check_class=CheckClass.TECH,
        steps=(
            "Выбрать доступные markup-файлы (только ASCII-имена и малый размер)",
            "GET /api/data/file/{file_id}/download и разобрать содержимое",
            "Посчитать записи и уникальные `chunkID`, сохранить оценку в сессии",
        ),
        expected=(
            "для доступных markup-файлов посчитаны записи и уникальные `chunkID`; результат "
            "помечен как клиентская оценка и сохранён в сессии; для не-ASCII имён — статус "
            "«заблокировано API» + замечание"
        ),
        endpoints=("get /api/data/file/{file_id}/download",),
        blocked_by_api="разбор разметки невозможен для не-ASCII имён (404 latin-1)",
        automation="records.markup_stats",
    ),
    CheckSpec(
        check_id="TC-REC-06",
        title="Сравнение дублей одной записи",
        module=RECORDS_MODULE,
        requirement="замечание №1",
        check_class=CheckClass.TECH,
        steps=(
            "Взять базу имени с несколькими версиями записи",
            "Сопоставить размеры RAW/markup и Δ времени импорта по версиям",
            "Определить актуальную версию (последняя по `import_date` или решение оператора)",
        ),
        expected=(
            "для одноимённых версий сопоставлены размеры RAW/markup, Δ импорта и структурные "
            "характеристики; расхождения включены в отчёт; актуальная версия отмечена"
        ),
        endpoints=("get /api/data/files",),
        automation="records.duplicate_versions",
    ),
)


# ---------------------------------------------------------------------------
# TC-LOAD — модуль «Loads» (FR-3, UC-06…UC-08, замечание №2) — этап T3
# ---------------------------------------------------------------------------
LOADS_MODULE = "Loads"

TC_LOAD_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        check_id="TC-LOAD-01",
        title="Реестр нагрузок получен",
        module=LOADS_MODULE,
        requirement="UC-06, BR-F2",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/loads/list без фильтров",
            "Сверить `result_size` с числом элементов `loads`",
            "Проверить срез `limit`/`offset`",
        ),
        expected=(
            "HTTP 200; `result_size` соответствует числу нагрузок; срез `limit`/`offset` работает"
        ),
        endpoints=("get /api/loads/list",),
        automation="loads.registry",
    ),
    CheckSpec(
        check_id="TC-LOAD-02",
        title="Состав полей элемента реестра",
        module=LOADS_MODULE,
        requirement="FR-3, замечание к API",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/loads/list и прочитать состав полей элемента",
            "Проверить наличие `phase_connection` (требуется `POST`/`PUT`)",
            "Зафиксировать расхождение замечанием к API",
        ),
        expected=(
            "**ожидаемо**: в ответе нет `phase_connection`, хотя `POST`/`PUT` его требуют "
            "→ замечание к API (P0)"
        ),
        endpoints=("get /api/loads/list",),
        blocked_by_api="в ответе списка нет `phase_connection`",
        automation="loads.fields",
    ),
    CheckSpec(
        check_id="TC-LOAD-03",
        title="Фильтр `ph_n`",
        module=LOADS_MODULE,
        requirement="FR-3",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/loads/list — запомнить полный состав",
            "GET /api/loads/list?ph_n=Ph_A",
            "Сравнить выдачи и зафиксировать факт",
        ),
        expected="**ожидаемо**: фильтр не влияет на выдачу → замечание к API",
        endpoints=("get /api/loads/list",),
        blocked_by_api="фильтр `ph_n` не влияет на выдачу",
        automation="loads.ph_n_filter",
    ),
    CheckSpec(
        check_id="TC-LOAD-04",
        title="Точность фильтров `load_id`/`category`",
        module=LOADS_MODULE,
        requirement="UC-06, NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/loads/list?load_id=<из реестра>",
            "GET /api/loads/list?category=<из реестра> и с другим регистром",
            "Зафиксировать регистрозависимость точного совпадения",
        ),
        expected=(
            "фильтры работают только при точном (регистрозависимом) совпадении — зафиксировано"
        ),
        endpoints=("get /api/loads/list",),
        automation="loads.exact_filters",
    ),
    CheckSpec(
        check_id="TC-LOAD-05",
        title="Поиск по описанию",
        module=LOADS_MODULE,
        requirement="UC-06, NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "Взять часть описания из живого реестра",
            "GET /api/loads/list?description_search=<подстрока>",
            "Проверить, что выдача — подмножество с вхождением подстроки",
        ),
        expected="поиск по подстроке работает",
        endpoints=("get /api/loads/list",),
        automation="loads.description_search",
    ),
    CheckSpec(
        check_id="TC-LOAD-06",
        title="Качество данных реестра",
        module=LOADS_MODULE,
        requirement="замечание №2",
        check_class=CheckClass.TECH,
        steps=(
            "Собрать категории нагрузок из живого реестра",
            "Найти регистровые дубли и служебную категорию `__OTHER__`",
            "Оформить результат замечанием о справочнике категорий",
        ),
        expected=(
            "выявлены регистровые дубли категорий и служебная `__OTHER__`; "
            "результат — замечание о справочнике категорий"
        ),
        endpoints=("get /api/loads/list",),
        automation="loads.categories",
    ),
    CheckSpec(
        check_id="TC-LOAD-07",
        title="Отсутствие удаления нагрузки",
        module=LOADS_MODULE,
        requirement="замечание к API",
        check_class=CheckClass.TECH,
        steps=(
            "DELETE /api/loads/{load_id} для нагрузки из реестра",
            "Зафиксировать код ответа: ожидается отсутствие маршрута (404/405)",
            "Оформить замечание к API (P2); `create_load` — только на `__TEST__`-нагрузке",
        ),
        expected=(
            "маршрут отсутствует (404/405) → замечание к API (P2); "
            "`create_load` выполняется только на `__TEST__`-нагрузке"
        ),
        probe_paths=("DELETE /api/loads/{load_id}",),
        automation="loads.missing_delete",
    ),
)


# ---------------------------------------------------------------------------
# TC-TASK — модуль «Task service» (FR-8, UC-26…UC-28, FSM-1) — этап T4
# ---------------------------------------------------------------------------
TASK_MODULE = "Task service"

TC_TASK_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        check_id="TC-TASK-01",
        title="Запуск диагностической задачи",
        module=TASK_MODULE,
        requirement="UC-26, FR-8",
        check_class=CheckClass.LIVE,
        steps=(
            "Экран «Задачи» → «Диагностика»: указать длительность (2 с) и подтвердить запуск",
            "POST /api/tasks/test?duration=2",
            "Убедиться, что создана задача типа celery-test и она поставлена на наблюдение",
            "Проверить, что вызов и задача зафиксированы в журнале и в сессии",
        ),
        expected="задача типа celery-test создана; факт фиксируется в журнале и в сессии",
        endpoints=("post /api/tasks/test",),
        automation="tasks.run_test_task",
    ),
    CheckSpec(
        check_id="TC-TASK-02",
        title="Список задач, фильтры и пагинация",
        module=TASK_MODULE,
        requirement="UC-27, NFR-4",
        check_class=CheckClass.TECH,
        steps=(
            "GET /api/tasks/ без фильтров: получить `tasks` и `count`",
            "Проверить фильтры `task_type` и `status` (значения из перечислений спецификации)",
            "Проверить фильтры `start_date`/`end_date`",
            "Проверить пагинацию `limit` (по умолчанию 100)/`offset`",
        ),
        expected=(
            "`tasks` + `count`; работают фильтры `task_type`/`status`/`start_date`/`end_date`, "
            "`limit` (по умолчанию 100)/`offset`"
        ),
        endpoints=("get /api/tasks/",),
        automation="tasks.list_tasks",
    ),
    CheckSpec(
        check_id="TC-TASK-03",
        title="Карточка задачи с прогонами",
        module=TASK_MODULE,
        requirement="UC-27, BR-F7",
        check_class=CheckClass.TECH,
        steps=(
            "Выбрать задачу из списка и открыть карточку",
            "GET /api/tasks/{task_id}",
            "Сверить состав `runtimes[]` с ожидаемым перечнем полей",
        ),
        expected=(
            "приходит `TaskWithRuntimes` с `runtimes[]` (`status`, `start_time`, `end_time`, "
            "`result`, `intermediate_result`, `celery_task_id`)"
        ),
        endpoints=("get /api/tasks/{task_id}",),
        automation="tasks.task_card",
    ),
    CheckSpec(
        check_id="TC-TASK-04",
        title="Переходы FSM-1 без расхода ресурсов",
        module=TASK_MODULE,
        requirement="FSM-1, UC-28, BR-F7",
        check_class=CheckClass.LIVE,
        steps=(
            "Запустить задачу `celery-test` с длительностью, достаточной для двух команд",
            "POST /api/tasks/{task_id}/pause из `running` — дождаться `paused` поллингом",
            "POST /api/tasks/{task_id}/resume из `paused` — дождаться `running` поллингом",
            "Сверить историю переходов задачи с ожидаемой цепочкой",
        ),
        expected=(
            "`running → pausing → paused → running`; каждый переход виден в журнале поллинга "
            "и в истории задачи"
        ),
        endpoints=(
            "post /api/tasks/{task_id}/pause",
            "post /api/tasks/{task_id}/resume",
            "get /api/tasks/{task_id}",
        ),
        automation="tasks.fsm_transitions",
    ),
    CheckSpec(
        check_id="TC-TASK-05",
        title="Прерывание задачи",
        module=TASK_MODULE,
        requirement="FSM-1, UC-28",
        check_class=CheckClass.LIVE,
        steps=(
            "Выбрать выполняющуюся (`running`) задачу `celery-test` на экране «Задачи»",
            "POST /api/tasks/{task_id}/interrupt",
            "Поллингом убедиться, что задача перешла в `interrupted` и наблюдение остановлено",
        ),
        expected="задача переходит в `interrupted` из `running`/`pausing`/`paused`",
        endpoints=(
            "post /api/tasks/{task_id}/interrupt",
            "get /api/tasks/{task_id}",
        ),
        automation="tasks.interrupt",
    ),
    CheckSpec(
        check_id="TC-TASK-06",
        title="Идемпотентность команд и запрет из терминального состояния",
        module=TASK_MODULE,
        requirement="BR-R3",
        check_class=CheckClass.TECH,
        steps=(
            "Повторить `pause` на уже приостановленной задаче",
            "Повторить `resume` на выполняющейся задаче",
            "Повторить `interrupt` на прерванной задаче",
            "Зафиксировать вариант поведения: 409, сообщение API или no-op без побочных эффектов",
        ),
        expected=(
            "повтор не создаёт побочных эффектов; команда из терминального состояния → "
            "`409`/no-op — вариант фиксируется"
        ),
        endpoints=(
            "post /api/tasks/{task_id}/pause",
            "post /api/tasks/{task_id}/resume",
            "post /api/tasks/{task_id}/interrupt",
        ),
        automation="tasks.idempotency",
    ),
    CheckSpec(
        check_id="TC-TASK-07",
        title="Неизвестная задача",
        module=TASK_MODULE,
        requirement="BR-R7",
        check_class=CheckClass.TECH,
        steps=(
            "Открыть карточку по несуществующему `task_id` (валидный UUID)",
            "GET /api/tasks/{несуществующий}",
            "Убедиться, что UI различает «не найдена» и ошибку запроса",
        ),
        expected="`not_found` (или 404) — UI корректно очищает карточку",
        endpoints=("get /api/tasks/{task_id}",),
        automation="tasks.unknown_task",
    ),
    CheckSpec(
        check_id="TC-TASK-08",
        title="Наблюдение за внешней задачей",
        module=TASK_MODULE,
        requirement="BR-R5",
        check_class=CheckClass.MANUAL,
        steps=(
            "Запустить задачу вне пульта (например, из основного UI или `curl`)",
            "Ввести её `task_id` на экране «Задачи» → «Внешняя задача»",
            "Убедиться, что статусы и история переходов приходят, а задача помечена «внешняя»",
        ),
        expected=(
            "оператор вводит `task_id` задачи, запущенной вне пульта, и получает её статусы; "
            "задача помечена как «внешняя»"
        ),
        endpoints=("get /api/tasks/{task_id}",),
        automation=None,
    ),
)


#: Проверки по группам: ключ группы → кортеж описаний (заполняется по этапам).
CHECKS_BY_GROUP: dict[str, tuple[CheckSpec, ...]] = {
    "TC-SYS": TC_SYS_SPECS,
    "TC-FILE": TC_FILE_SPECS,
    "TC-REC": TC_REC_SPECS,
    "TC-LOAD": TC_LOAD_SPECS,
    "TC-TASK": TC_TASK_SPECS,
}

#: Группы каталога в порядке программы испытаний (`docs/02` §14).
GROUPS: tuple[CheckGroup, ...] = tuple(
    CheckGroup(plan=plan, checks=CHECKS_BY_GROUP.get(plan.key, ())) for plan in PLANNED_GROUPS
)

#: Все описанные проверки каталога (в порядке групп).
CHECKS: tuple[CheckSpec, ...] = tuple(check for group in GROUPS for check in group.checks)

#: Проверка по её идентификатору (`TC-TASK-01`).
CHECK_INDEX: dict[str, CheckSpec] = {check.check_id: check for check in CHECKS}

#: Группа каждой описанной проверки (`TC-TASK-01` → `TC-TASK`).
CHECK_GROUP_INDEX: dict[str, str] = {
    check.check_id: group.key for group in GROUPS for check in group.checks
}


def groups(*, implemented_only: bool = False) -> tuple[CheckGroup, ...]:
    """Группы каталога (при `implemented_only` — только наполненные проверками)."""
    if implemented_only:
        return tuple(group for group in GROUPS if group.is_implemented)
    return GROUPS


def group(key: str) -> CheckGroup | None:
    """Группа каталога по ключу (`TC-TASK`)."""
    return next((item for item in GROUPS if item.key == key), None)


def by_group(key: str) -> tuple[CheckSpec, ...]:
    """Проверки группы (`TC-TASK`)."""
    found = group(key)
    return found.checks if found is not None else ()


def find(check_id: str) -> CheckSpec | None:
    """Описание проверки по идентификатору (`TC-TASK-01`)."""
    return CHECK_INDEX.get(str(check_id).strip().upper())


def group_of(check_id: str) -> str:
    """Ключ группы проверки (пустая строка, если проверки нет в каталоге)."""
    return CHECK_GROUP_INDEX.get(str(check_id).strip().upper(), "")


def check_ids() -> tuple[str, ...]:
    """Идентификаторы всех описанных проверок."""
    return tuple(check.check_id for check in CHECKS)


def stage_of(check_id: str) -> str:
    """Этап пульта, на котором выполняется проверка (`T4` для `TC-TASK-*`)."""
    found = group(group_of(check_id))
    return found.stage if found is not None else ""


def catalog_summary() -> dict[str, Any]:
    """Сводка каталога: полный состав программы и степень наполнения по этапам."""
    by_stage: dict[str, int] = {}
    for group_plan in PLANNED_GROUPS:
        by_stage[group_plan.stage] = by_stage.get(group_plan.stage, 0) + group_plan.checks_total

    return {
        "groups_total": len(PLANNED_GROUPS),
        "groups_implemented": sum(1 for item in GROUPS if item.is_implemented),
        "checks_total": PLANNED_CHECKS_TOTAL,
        "checks_implemented": len(CHECKS),
        "by_stage": by_stage,
        "classes": _class_counts(),
    }


def _class_counts() -> dict[str, int]:
    """Число описанных проверок по классам (`tech`/`live`/`heavy`/`manual`)."""
    counts: dict[str, int] = {}
    for check in CHECKS:
        counts[str(check.check_class)] = counts.get(str(check.check_class), 0) + 1
    return counts
