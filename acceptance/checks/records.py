"""Автоматические сценарии проверок записей (RAW + markup) — TC-REC-01…06, этап T3.

Записи в API нет: пульт объединяет плоский реестр файлов сам (`acceptance.records`)
по правилу «имя + время импорта». Сценарии проверяют именно это представление:

    * `records.pairing` — записи сформированы, правило и уверенность видны;
    * `records.duplicates_manifest` — версии записи и **контроль полноты** манифеста;
    * `records.unpaired` — RAW без разметки и разметка без RAW выделены, «прочие»
      файлы не попали в записи, но присутствуют в манифесте;
    * `records.markup_stats` — записи и чанки разметки (клиентская оценка, `lib.markup_stats`);
    * `records.duplicate_versions` — сравнение одноимённых версий и актуальная версия.

`TC-REC-03` — ручная проверка (оператор перепривязывает пару на экране «Записи»),
сценария нет. Ожидаемый дефект `TC-REC-05` (не-ASCII имена) фиксируется в
доказательствах, а замечание формируется проверкой `TC-FILE-10` (та же причина).
"""

from __future__ import annotations

from typing import Any

from acceptance.checks.files import (
    MAX_PROBE_BYTES,
    _download,
    _is_ascii,
    _markup_files,
    _registry,
)
from acceptance.checks.registry import CheckStatus
from acceptance.checks.runner import (
    AutomationContext,
    CheckOutcome,
    PreconditionError,
    automate,
    evaluate,
    scenario,
)
from acceptance.overrides import load_overrides
from acceptance.records import (
    RULE_NAME_TIME,
    MarkupStatsEntry,
    RecordsOverview,
    build_records,
)
from acceptance.session import set_markup_stats
from lib.markup_stats import parse_markup_stats

__all__ = [
    "AUTOMATIONS",
    "AutomationContext",
    "CheckOutcome",
    "PreconditionError",
    "automate",
    "evaluate",
    "scenario",
]

#: Сколько markup-файлов разбирает сценарий по умолчанию (остальные — по параметру).
DEFAULT_MARKUP_FILES = 3

#: Параметр запуска: сколько markup-файлов разобрать.
MARKUP_FILES_PARAM = "markup_files"


def _overview(context: AutomationContext) -> RecordsOverview:
    """Объединение живого реестра в записи (с учётом решений оператора и статистики).

    Raises:
        PreconditionError: реестр недоступен/пуст или объединение невозможно.
    """
    files = _registry(context)
    stats_map = {
        str(key): MarkupStatsEntry.from_dict(value)
        for key, value in context.session.markup_stats.items()
    }
    try:
        return build_records(files, overrides=load_overrides(), markup_stats=stats_map)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PreconditionError(f"объединение реестра в записи не выполнено: {exc}") from exc


def _pairing(context: AutomationContext) -> CheckOutcome:
    """TC-REC-01: записи сформированы по правилу «имя + время импорта» (замечание №1)."""
    overview = _overview(context)
    stats = overview.stats
    evidence: dict[str, Any] = {
        "rule": RULE_NAME_TIME,
        "window_seconds": overview.window_seconds,
        "stats": stats.to_dict(),
        "records": len(overview.records),
        "requires_attention": len(overview.requires_attention),
        "sample": [
            {
                "record_id": record.record_id,
                "base_name": record.base_name,
                "rule": record.rule,
                "confidence": record.confidence,
                "delta": record.delta_note,
            }
            for record in overview.records[:5]
        ],
    }
    if not overview.records:
        raise PreconditionError(
            "в реестре нет пар RAW + markup: объединение в записи проверить нельзя"
        )
    return CheckOutcome(
        CheckStatus.PASSED,
        f"записей {stats.records} из {stats.files} файлов (правило «{RULE_NAME_TIME}», "
        f"покрытие разметкой {stats.coverage}%)",
        evidence,
    )


def _duplicates_manifest(context: AutomationContext) -> CheckOutcome:
    """TC-REC-02: версии записи видны, манифест полон (замечание №1, п. 3.1 ТЗ)."""
    overview = _overview(context)
    complete = overview.manifest_is_complete()
    rows = overview.manifest_rows()
    ids = overview.manifest_file_ids()
    bases = overview.duplicate_bases
    versions = {
        base: [
            {
                "record_id": record.record_id,
                "raw_id": str(record.raw.id),
                "raw_size": record.raw.size,
                "markup_id": str(record.markup.id) if record.markup else None,
                "import_date": str(record.raw.import_date),
                "s3_path": record.raw.s3_path,
            }
            for record in overview.for_base(base)
        ]
        for base in bases[:3]
    }
    evidence: dict[str, Any] = {
        "manifest_rows": len(rows),
        "manifest_ids": len(ids),
        "files_in_registry": overview.stats.files,
        "complete": complete,
        "duplicate_bases": list(bases[:10]),
        "duplicate_total": overview.stats.duplicate_bases,
        "versions": versions,
    }
    if not complete:
        return CheckOutcome(
            CheckStatus.FAILED,
            "контроль полноты манифеста не пройден: каждый файл реестра должен попасть в "
            f"манифест ровно один раз (файлов {overview.stats.files}, строк манифеста {len(ids)})",
            evidence,
        )
    verdict = (
        f"манифест полон ({len(ids)} файлов, {len(rows)} строк), баз с дублями "
        f"{overview.stats.duplicate_bases}"
    )
    if not bases:
        verdict += " · одноимённых версий в реестре нет"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _unpaired(context: AutomationContext) -> CheckOutcome:
    """TC-REC-04: несопоставленные и прочие файлы учтены манифестом (п. 3.1 ТЗ)."""
    overview = _overview(context)
    in_records = {
        str(file.id)
        for record in overview.records
        for file in (record.raw, record.markup)
        if file is not None
    }
    manifest_ids = set(overview.manifest_file_ids())
    lost_others = [
        file.file_name for file in overview.other_files if str(file.id) not in manifest_ids
    ]
    leaked = [file.file_name for file in overview.other_files if str(file.id) in in_records]

    evidence: dict[str, Any] = {
        "raw_without_markup": len(overview.unpaired_raw),
        "markup_without_raw": len(overview.unpaired_markup),
        "other_files": len(overview.other_files),
        "other_not_in_manifest": lost_others[:5],
        "other_in_records": leaked[:5],
        "examples": {
            "raw_without_markup": [file.file_name for file in overview.unpaired_raw[:3]],
            "markup_without_raw": [file.file_name for file in overview.unpaired_markup[:3]],
            "other": [file.file_name for file in overview.other_files[:3]],
        },
    }
    problems: list[str] = []
    if leaked:
        problems.append("прочие файлы попали в записи: " + ", ".join(leaked[:3]))
    if lost_others:
        problems.append("прочие файлы отсутствуют в манифесте: " + ", ".join(lost_others[:3]))
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)
    return CheckOutcome(
        CheckStatus.PASSED,
        f"несопоставленные выделены: RAW без разметки {len(overview.unpaired_raw)}, "
        f"разметка без RAW {len(overview.unpaired_markup)}; прочих файлов "
        f"{len(overview.other_files)} — все в манифесте",
        evidence,
    )


def _markup_stats(context: AutomationContext) -> CheckOutcome:
    """TC-REC-05: записи и уникальные `chunkID` по доступным markup-файлам (замечания №3/№4)."""
    files = _registry(context)
    markup = _markup_files(files)
    if not markup:
        raise PreconditionError("в реестре нет markup-файлов: разбор разметки неприменим")

    limit = int(context.param(MARKUP_FILES_PARAM) or DEFAULT_MARKUP_FILES)
    accessible = [
        file for file in markup if _is_ascii(file.file_name) and file.size <= MAX_PROBE_BYTES
    ][: max(1, limit)]
    blocked: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    problems: list[str] = []

    for file in accessible:
        content, error = _download(context, str(file.id))
        if content is None:
            blocked.append({"file_id": str(file.id), "file_name": file.file_name, "error": error})
            continue
        parsed = parse_markup_stats(content)
        set_markup_stats(
            context.session,
            file.id,
            records=parsed.records,
            chunks=parsed.chunks,
            status="рассчитано (клиентская оценка)",
            note=f"проверка {context.spec.check_id}: разбор markup-файла пультом",
        )
        rows.append(
            {
                "file_id": str(file.id),
                "file_name": file.file_name,
                "size": file.size,
                "records": parsed.records,
                "chunks": parsed.chunks,
            }
        )
        if parsed.records <= 0:
            problems.append(f"«{file.file_name}»: записей не найдено (0)")

    # не-ASCII имена: тот же дефект latin-1, что и в TC-FILE-10 (замечание формируется там)
    non_ascii = [file for file in markup if not _is_ascii(file.file_name)][: max(1, limit)]
    for file in non_ascii:
        probe = context.probe_request("GET", f"/api/data/file/{file.id}/download", binary=True)
        blocked.append(
            {
                "file_id": str(file.id),
                "file_name": file.file_name,
                "status": probe.status,
                "reason": "не-ASCII имя (дефект latin-1)",
            }
        )

    evidence: dict[str, Any] = {
        "markup_files_total": len(markup),
        "parsed": rows,
        "blocked": blocked,
        "session_stats": len(context.session.markup_stats),
        "estimate_note": "записи и чанки — клиентская оценка: серверных агрегатов в API нет",
    }
    if not rows:
        if blocked:
            return CheckOutcome(
                CheckStatus.BLOCKED,
                "разбор разметки не выполнен: ни один markup-файл не скачался (дефект latin-1, P0)",
                evidence,
            )
        return CheckOutcome(CheckStatus.SKIPPED, "доступных markup-файлов нет", evidence)
    if problems:
        return CheckOutcome(CheckStatus.FAILED, "; ".join(problems), evidence)

    total_records = sum(int(row["records"]) for row in rows)
    total_chunks = sum(int(row["chunks"]) for row in rows)
    verdict = f"разметка разобрана по {len(rows)} файл(ам): записей {total_records}, чанков {total_chunks}"
    if blocked:
        verdict += f" · недоступно (не-ASCII) {len(blocked)} файл(ов)"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


def _duplicate_versions(context: AutomationContext) -> CheckOutcome:
    """TC-REC-06: одноимённые версии записи сопоставлены, актуальная отмечена (замечание №1)."""
    overview = _overview(context)
    bases = overview.duplicate_bases
    if not bases:
        return CheckOutcome(
            CheckStatus.SKIPPED,
            "одноимённых версий записи нет: сравнение дублей неприменимо",
            {"duplicate_bases": 0},
        )

    base = bases[0]
    records = overview.for_base(base)
    rows = [
        {
            "record_id": record.record_id,
            "raw_name": record.raw.file_name,
            "raw_size": record.raw.size,
            "markup_id": str(record.markup.id) if record.markup else None,
            "markup_size": record.markup.size if record.markup else None,
            "import_date": str(record.raw.import_date),
            "delta_seconds": record.delta_seconds,
            "records": record.stats.records,
            "chunks": record.stats.chunks,
            "manual": record.manual,
            "excluded": record.excluded,
        }
        for record in records
    ]
    sizes = {row["raw_size"] for row in rows}
    actual = max(records, key=lambda record: record.raw.import_date)
    actual_manual = next(
        (record for record in records if record.is_current and record.manual), None
    )
    evidence: dict[str, Any] = {
        "base_name": base,
        "versions": rows,
        "raw_size_variants": len(sizes),
        "actual_version": actual.record_id,
        "actual_source": "решение оператора" if actual_manual else "последняя по `import_date`",
    }
    verdict = (
        f"версии «{base}» сопоставлены ({len(rows)}), актуальная — {evidence['actual_version']} "
        f"({evidence['actual_source']})"
    )
    if len(sizes) == 1:
        verdict += " · размер RAW у версий одинаковый (различить по размеру нельзя)"
    return CheckOutcome(CheckStatus.PASSED, verdict, evidence)


#: Сценарии проверок записей: ключ — `CheckSpec.automation`.
AUTOMATIONS = {
    "records.pairing": _pairing,
    "records.duplicates_manifest": _duplicates_manifest,
    "records.unpaired": _unpaired,
    "records.markup_stats": _markup_stats,
    "records.duplicate_versions": _duplicate_versions,
}
