"""Программа сессии: объединение наборов по ИЛИ, покрытие, ревизии, утверждение.

Программа (`Programme`) отвечает требованиям `FR-P-68` (объединение выбранных
наборов по логическому **ИЛИ** с дедупликацией и происхождением), `FR-P-69`
(покрытие разделов и обоснование сокращения) и `FR-P-70` (шаблоны программ).
Программа — **единственный источник состава**: очередь прогона строится как снимок
утверждённой ревизии (`queue.py`), а результаты проверок ссылаются на ревизию
(`DR-P-14`), поэтому выпуск новой ревизии не меняет уже полученные результаты.

Правила, заложенные в модель:

* порядок пунктов наследуется по приоритету наборов: проверка из пересечения берёт
  место **первого** набора, в котором встретилась (`FR-P-68`); порядок можно править
  руками — это правка программы, а не набора;
* обязательные пункты (смоук-минимум) идут **первыми** и не исключаются
  из программы (`FR-P-70`);
* утверждение фиксирует ревизию (статус, автор, время); правка состава после
  утверждения запрещена — сначала выпускается новая ревизия (по аналогии с `FR-P-66`);
* утверждение с сокращением (есть непокрытые разделы) требует обоснования, которое
  попадает в протокол и отчёт (`FR-P-69`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from acceptance.checks import catalog
from acceptance.session import now_iso
from acceptance.sets import (
    SCOPE_FULL,
    SCOPE_SMOKE,
    SCOPE_STANDARD,
    SMOKE_SET_ID,
    SetItem,
    SetsLibrary,
    section_options,
    section_title,
    section_totals,
    smoke_check_ids,
)

#: Статусы программы сессии (по аналогии с набором).
PROGRAMME_DRAFT = "черновик"
PROGRAMME_APPROVED = "утверждена"
PROGRAMME_STATUSES = (PROGRAMME_DRAFT, PROGRAMME_APPROVED)

#: Шаблоны программы (`FR-P-70`).
TEMPLATE_FULL = "Полная программа"
TEMPLATE_SMOKE = "Смоук-минимум"
TEMPLATE_REGRESS = "Регресс после исправления"
TEMPLATES = (TEMPLATE_FULL, TEMPLATE_SMOKE, TEMPLATE_REGRESS)

TEMPLATE_HINTS: dict[str, str] = {
    TEMPLATE_FULL: "все разделы испытаний: наборы разделов, кроме смоук-минимума",
    TEMPLATE_SMOKE: "только обязательный минимум — быстрая проверка после установки",
    TEMPLATE_REGRESS: "отказы и блокировки прошлой сессии плюс смоук их разделов",
}

#: Подпись источника для пунктов, собранных шаблоном (а не набором библиотеки).
TEMPLATE_SOURCE = "шаблон"


@dataclass
class ProgrammeItem:
    """Пункт программы: проверка, её происхождение и снимок описания из каталога.

    Снимок описания (`title`, `module`, `check_class`, …) хранится в самой программе:
    утверждённая ревизия должна читаться и после того, как каталог изменится
    (`DR-P-14` — очередь и результаты ссылаются именно на ревизию).
    """

    check_id: str
    order: int = 1
    mandatory: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    module: str = ""
    group: str = ""
    check_class: str = ""
    requirement: str = ""
    expected: str = ""
    automation: str = ""

    @property
    def section(self) -> str:
        """Раздел испытаний пункта (ключ группы каталога)."""
        return self.group

    @property
    def source_ids(self) -> list[str]:
        """Идентификаторы наборов-источников проверки (`FR-P-68`)."""
        return [str(item.get("set_id") or "") for item in self.sources]

    @property
    def source_note(self) -> str:
        """Происхождение пункта строкой: «SET-SYS (рев. 1), SET-REC (рев. 2)»."""
        labels = [
            f"{item.get('set_id')} (рев. {item.get('revision')})"
            if item.get("revision")
            else str(item.get("set_id") or "")
            for item in self.sources
        ]
        return ", ".join(label for label in labels if label)

    @property
    def has_snapshot(self) -> bool:
        """True, если пункт несёт снимок описания проверки."""
        return bool(self.title and self.check_class)

    @property
    def in_catalog(self) -> bool:
        """True, если проверка описана в каталоге (иначе её нельзя исполнить)."""
        return catalog.find(self.check_id) is not None

    def to_dict(self) -> dict[str, Any]:
        """Сериализует пункт программы."""
        payload = asdict(self)
        payload["sources"] = [dict(item) for item in self.sources]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgrammeItem:
        """Восстанавливает пункт программы из JSON."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload["check_id"] = str(payload.get("check_id") or "")
        payload["order"] = int(payload.get("order") or 1)
        payload["mandatory"] = bool(payload.get("mandatory"))
        payload["sources"] = [dict(item) for item in (data.get("sources") or [])]
        return cls(**payload)


@dataclass
class ProgrammeRevision:
    """Неизменяемая ревизия программы: состав, выбранные наборы, утверждение."""

    revision: int
    at: str = field(default_factory=now_iso)
    author: str = ""
    comment: str = ""
    approved: bool = False
    reduction: str = ""
    set_refs: list[dict[str, Any]] = field(default_factory=list)
    items: list[ProgrammeItem] = field(default_factory=list)

    @property
    def check_ids(self) -> list[str]:
        """Идентификаторы проверок ревизии (в порядке программы)."""
        return [item.check_id for item in self.items]

    @property
    def size(self) -> int:
        """Число пунктов ревизии."""
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует ревизию программы."""
        payload = asdict(self)
        payload["set_refs"] = [dict(item) for item in self.set_refs]
        payload["items"] = [item.to_dict() for item in self.items]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgrammeRevision:
        """Восстанавливает ревизию программы из JSON."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload["revision"] = int(payload.get("revision") or 1)
        payload["approved"] = bool(payload.get("approved"))
        payload["set_refs"] = [dict(item) for item in (data.get("set_refs") or [])]
        payload["items"] = [ProgrammeItem.from_dict(item) for item in (data.get("items") or [])]
        return cls(**payload)


def item_from_set(entry: SetItem, source: dict[str, Any]) -> ProgrammeItem:
    """Пункт программы по пункту набора: происхождение плюс снимок описания каталога."""
    spec = catalog.find(entry.check_id)
    return ProgrammeItem(
        check_id=entry.check_id,
        mandatory=bool(entry.mandatory),
        sources=[dict(source)],
        title=spec.title if spec is not None else "",
        module=spec.module if spec is not None else "",
        group=catalog.group_of(entry.check_id),
        check_class=str(spec.check_class) if spec is not None else "",
        requirement=spec.requirement if spec is not None else "",
        expected=spec.expected if spec is not None else "",
        automation=(spec.automation or "") if spec is not None else "",
    )


@dataclass
class Programme:
    """Программа сессии: состав, происхождение, покрытие, ревизии, утверждение."""

    items: list[ProgrammeItem] = field(default_factory=list)
    set_refs: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 1
    status: str = PROGRAMME_DRAFT
    reduction: str = ""
    approved: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=now_iso)
    revisions: list[ProgrammeRevision] = field(default_factory=list)

    # -- чтение --------------------------------------------------------------
    @property
    def size(self) -> int:
        """Число пунктов программы."""
        return len(self.items)

    @property
    def check_ids(self) -> list[str]:
        """Идентификаторы проверок программы (в порядке прогона)."""
        return [item.check_id for item in self.items]

    @property
    def mandatory_ids(self) -> list[str]:
        """Обязательные пункты программы (смоук-минимум, `FR-P-70`)."""
        return [item.check_id for item in self.items if item.mandatory]

    @property
    def is_approved(self) -> bool:
        """True, если текущая ревизия программы утверждена."""
        return self.status == PROGRAMME_APPROVED

    @property
    def status_label(self) -> str:
        """Статус программы с номером ревизии: «утверждена (ревизия 2)»."""
        return f"{self.status} (ревизия {self.revision})"

    def find(self, check_id: str) -> ProgrammeItem | None:
        """Пункт программы по идентификатору проверки."""
        key = str(check_id).strip().upper()
        return next((item for item in self.items if item.check_id.upper() == key), None)

    def of_section(self, section: str) -> list[ProgrammeItem]:
        """Пункты программы одного раздела испытаний (в порядке программы)."""
        key = str(section).strip().upper()
        return [item for item in self.items if item.group.upper() == key]

    def selected_set_ids(self) -> list[str]:
        """Идентификаторы выбранных наборов (в порядке приоритета)."""
        return [str(item.get("set_id") or "") for item in self.set_refs]

    # -- составление (FR-P-68) ------------------------------------------------
    @classmethod
    def from_sets(
        cls,
        library: SetsLibrary,
        set_ids: Sequence[str],
        *,
        author: str = "",
        comment: str = "",
    ) -> Programme:
        """Собирает программу как объединение выбранных наборов по логическому ИЛИ."""
        programme = cls()
        programme.rebuild(library, set_ids)
        if author:
            programme.approved = {"author": str(author), "comment": str(comment)}
        return programme

    def rebuild(self, library: SetsLibrary, set_ids: Sequence[str]) -> dict[str, Any]:
        """Пересобирает состав программы объединением выбранных наборов.

        Возвращает отчёт о сборке: сколько проверок добавлено, сколько убрано и
        сколько проверок пришло из нескольких наборов (пересечения — они и есть
        повод для дедупликации, `FR-P-68`). Ручной порядок прежних пунктов
        сохраняется: пересборка не должна терять настроенный прогон.
        """
        self._require_draft()
        before = self.check_ids
        previous = {item.check_id.upper(): item for item in self.items}
        previous_order = {item.check_id.upper(): index for index, item in enumerate(self.items)}

        merged: list[ProgrammeItem] = []
        index: dict[str, ProgrammeItem] = {}
        sequence = 0
        for set_id in set_ids:
            found = library.find(set_id)
            if found is None:
                continue
            source = {"set_id": found.set_id, "revision": found.revision}
            for entry in found.items:
                key = entry.check_id.upper()
                sequence += 1
                existing = index.get(key)
                if existing is None:
                    stored = previous.get(key)
                    if stored is not None:
                        stored.sources = _merge_sources(stored.sources, source)
                        stored.mandatory = stored.mandatory or bool(entry.mandatory)
                        _refresh_snapshot(stored)
                        item = stored
                    else:
                        item = item_from_set(entry, source)
                        item.order = sequence
                    index[key] = item
                    merged.append(item)
                else:
                    existing.sources = _merge_sources(existing.sources, source)
                    if entry.mandatory:
                        existing.mandatory = True

        merged.sort(key=lambda item: previous_order.get(item.check_id.upper(), 10**6 + item.order))
        self.items = [item for item in merged if item.mandatory] + [
            item for item in merged if not item.mandatory
        ]
        self.set_refs = [
            {
                "set_id": found.set_id,
                "revision": found.revision,
                "title": found.title,
                "section": found.section,
                "scope": found.scope,
            }
            for found in (library.find(set_id) for set_id in set_ids)
            if found is not None
        ]
        self._reindex()
        self._touch()

        after = self.check_ids
        return {
            "added": [check_id for check_id in after if check_id not in before],
            "removed": [check_id for check_id in before if check_id not in after],
            "kept": sum(1 for check_id in after if check_id in before),
            "sets": len(self.set_refs),
            "intersections": sum(1 for item in self.items if len(item.sources) > 1),
        }

    # -- правка состава (только черновик) -------------------------------------
    def _require_draft(self) -> None:
        """Запрещает правку утверждённой программы (нужна новая ревизия)."""
        if self.is_approved:
            raise ValueError(
                f"программа утверждена (ревизия {self.revision}): правка состава требует "
                "новой ревизии (FR-P-68)"
            )

    def add_check(self, check_id: str, *, mandatory: bool = False) -> bool:
        """Добавляет проверку в программу вручную (ручная правка состава, `FR-P-68`)."""
        self._require_draft()
        key = str(check_id).strip().upper()
        if not key or self.find(key) is not None:
            return False
        item = item_from_set(
            SetItem(check_id=key, mandatory=bool(mandatory)),
            {"set_id": "вручную", "revision": 0},
        )
        if item.mandatory:
            self.items.insert(len(self.mandatory_ids), item)
        else:
            self.items.append(item)
        self._reindex()
        self._touch()
        return True

    def exclude(self, check_ids: Sequence[str]) -> list[str]:
        """Убирает пункты из программы; обязательные исключить нельзя (`FR-P-70`).

        Raises:
            ValueError: если среди указанных пунктов есть обязательные (смоук-минимум).
        """
        self._require_draft()
        keys = {str(item).strip().upper() for item in check_ids}
        blocked = [item.check_id for item in self.items if item.check_id in keys and item.mandatory]
        if blocked:
            raise ValueError(
                f"обязательные пункты не исключаются из программы (FR-P-70): {', '.join(blocked)}"
            )
        removed = [item.check_id for item in self.items if item.check_id in keys]
        self.items = [item for item in self.items if item.check_id not in keys]
        self._reindex()
        self._touch()
        return removed

    def move(self, check_id: str, delta: int) -> bool:
        """Сдвигает пункт по порядку на `delta` позиций (ручная правка порядка).

        Обязательные пункты остаются в начале программы: сдвиг через границу
        обязательной части не выполняется (`FR-P-70`).
        """
        self._require_draft()
        key = str(check_id).strip().upper()
        index = next((i for i, item in enumerate(self.items) if item.check_id == key), -1)
        if index < 0:
            return False
        target = max(0, min(len(self.items) - 1, index + int(delta)))
        boundary = len(self.mandatory_ids)
        if (index < boundary) != (target < boundary) or target == index:
            return False
        self.items.insert(target, self.items.pop(index))
        self._reindex()
        self._touch()
        return True

    def reorder(self, check_ids: Sequence[str]) -> None:
        """Задаёт порядок пунктов: переданные — впереди, остальные — как были."""
        self._require_draft()
        order = {str(key).strip().upper(): position for position, key in enumerate(check_ids)}
        current = {item.check_id: index for index, item in enumerate(self.items)}

        def sort_key(item: ProgrammeItem) -> int:
            return order.get(item.check_id, 10**6 + current.get(item.check_id, 0))

        mandatory = sorted((item for item in self.items if item.mandatory), key=sort_key)
        optional = sorted((item for item in self.items if not item.mandatory), key=sort_key)
        self.items = mandatory + optional
        self._reindex()
        self._touch()

    def set_mandatory(self, check_id: str, mandatory: bool = True) -> bool:
        """Помечает пункт обязательным или снимает признак (смоук-минимум)."""
        self._require_draft()
        item = self.find(check_id)
        if item is None:
            return False
        item.mandatory = bool(mandatory)
        self.items.sort(key=lambda entry: 0 if entry.mandatory else 1)
        self._reindex()
        self._touch()
        return True

    # -- покрытие и предупреждения (FR-P-69) ----------------------------------
    def targets(self) -> dict[str, int]:
        """Целевой объём по разделам — из целевых объёмов выбранных наборов.

        `полный` — весь объём раздела по программе испытаний, `стандарт` — все
        описанные в каталоге проверки раздела, `смоук` — обязательные пункты
        раздела (набор смоук-минимума не привязан к одному разделу, поэтому его
        вклад считается по фактически включённым обязательным пунктам).
        """
        targets: dict[str, int] = {}
        for ref in self.set_refs:
            section = str(ref.get("section") or "")
            if not section:
                continue
            totals = section_totals(section)
            scope = str(ref.get("scope") or SCOPE_STANDARD)
            if scope == SCOPE_FULL:
                value = totals["planned"]
            elif scope == SCOPE_SMOKE:
                value = sum(1 for item in self.of_section(section) if item.mandatory)
            else:
                value = totals["implemented"]
            targets[section] = max(targets.get(section, 0), value)

        for section in section_options():
            smoke = sum(1 for item in self.of_section(section) if item.mandatory)
            targets[section] = max(targets.get(section, 0), smoke)
        return targets

    def coverage(self) -> list[dict[str, Any]]:
        """Матрица покрытия разделов: «в программе / всего» против объёма (`FR-P-69`)."""
        targets = self.targets()
        rows: list[dict[str, Any]] = []
        for section in section_options():
            totals = section_totals(section)
            items = self.of_section(section)
            target = targets.get(section, 0)
            rows.append(
                {
                    "section": section,
                    "title": section_title(section),
                    "in_programme": len(items),
                    "implemented": totals["implemented"],
                    "planned": totals["planned"],
                    "target": target,
                    "missing": max(0, target - len(items)),
                    "uncovered": target > len(items),
                    "mandatory": sum(1 for item in items if item.mandatory),
                    "sets": [
                        str(ref.get("set_id") or "")
                        for ref in self.set_refs
                        if str(ref.get("section") or "") == section
                    ],
                }
            )
        return rows

    def uncovered_sections(self) -> list[dict[str, Any]]:
        """Разделы, где программа не добирает целевой объём (`FR-P-69`)."""
        return [row for row in self.coverage() if row["uncovered"]]

    def warnings(self, library: SetsLibrary | None = None) -> list[str]:
        """Предупреждения по программе перед утверждением (`FR-P-68`, `FR-P-69`)."""
        notes: list[str] = []
        if not self.items:
            notes.append("в программе нет ни одной проверки: выберите наборы в разделе «Наборы»")
        if library is not None:
            for set_id in self.selected_set_ids():
                found = library.find(set_id)
                if found is not None and not found.items:
                    notes.append(
                        f"набор {set_id} пуст: он не добавляет в программу ни одной проверки"
                    )
        unknown = [item.check_id for item in self.items if not item.in_catalog]
        if unknown:
            notes.append(
                "проверки не описаны в каталоге и не могут быть исполнены: " + ", ".join(unknown)
            )
        intersections = [item for item in self.items if len(item.sources) > 1]
        if intersections:
            notes.append(
                f"пересечение наборов: {len(intersections)} проверок пришли из нескольких "
                "наборов — в программе они учтены один раз"
            )
        uncovered = self.uncovered_sections()
        if uncovered:
            notes.append(
                "не покрыты разделы: "
                + ", ".join(str(row["title"]) for row in uncovered)
                + " — утверждение с сокращением требует обоснования"
            )
        return notes

    # -- представление --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """KPI программы для шапки `SCR-102` и отчёта."""
        classes: dict[str, int] = {}
        for item in self.items:
            key = item.check_class or "?"
            classes[key] = classes.get(key, 0) + 1
        coverage = self.coverage()
        return {
            "revision": self.revision,
            "status": self.status,
            "status_label": self.status_label,
            "is_approved": self.is_approved,
            "items": self.size,
            "mandatory": len(self.mandatory_ids),
            "sets": len(self.set_refs),
            "sections": len({item.group for item in self.items if item.group}),
            "sections_total": len(section_options()),
            "sections_uncovered": sum(1 for row in coverage if row["uncovered"]),
            "classes": classes,
            "intersections": sum(1 for item in self.items if len(item.sources) > 1),
            "unknown": sum(1 for item in self.items if not item.in_catalog),
            "coverage": coverage,
            "warnings": self.warnings(),
            "reduction": self.reduction,
            "approved_by": str(self.approved.get("author") or ""),
            "approved_at": str(self.approved.get("at") or ""),
            "updated_at": self.updated_at,
        }

    def rows(self, results: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Строки программы для таблицы `SCR-102`, отчёта и выгрузки `csv`.

        Args:
            results: состояние проверок из `results.py` (`check_id` → словарь со
                `status` и `icon`). Очередь и программа **не хранят** статус сами:
                единственный источник результата — `DR-P-5`, поэтому таблица
                программы подтягивает его снаружи.
        """
        states = dict(results or {})
        rows: list[dict[str, Any]] = []
        for item in self.items:
            state = states.get(item.check_id)
            if isinstance(state, Mapping):
                status = str(state.get("status") or "")
                icon = str(state.get("icon") or "")
                verdict = str(state.get("verdict") or "")
            else:
                status, icon, verdict = str(state or ""), "", ""
            rows.append(
                {
                    "order": item.order,
                    "check_id": item.check_id,
                    "title": item.title,
                    "module": item.module,
                    "section": item.group,
                    "section_title": section_title(item.group) if item.group else "",
                    "check_class": item.check_class,
                    "mandatory": item.mandatory,
                    "sources": item.source_note,
                    "sources_count": len(item.sources),
                    "in_catalog": item.in_catalog,
                    "status": status,
                    "icon": icon,
                    "verdict": verdict,
                }
            )
        return rows

    def delta(self, revision_number: int | None = None) -> dict[str, Any]:
        """Дельта с предыдущей ревизией: что добавилось и что ушло (`SCR-102`).

        Args:
            revision_number: ревизия для сравнения; по умолчанию — предыдущая
                зафиксированная ревизия (ближайшая младшая к текущей).
        """
        if revision_number is not None:
            previous = self.find_revision(revision_number)
            if previous is None:
                return {"revision": None, "missing": True, "added": [], "removed": []}
        else:
            candidates = [item for item in self.revisions if item.revision < self.revision]
            previous = max(candidates, key=lambda item: item.revision, default=None)
            if previous is None:
                return {"revision": None, "missing": False, "added": [], "removed": []}

        current = set(self.check_ids)
        before = set(previous.check_ids)
        return {
            "revision": previous.revision,
            "missing": False,
            "added": [check_id for check_id in self.check_ids if check_id not in before],
            "removed": [check_id for check_id in previous.check_ids if check_id not in current],
        }

    # -- ревизии и утверждение (FR-P-68, FR-P-69) -----------------------------
    def snapshot(
        self, *, author: str = "", comment: str = "", approved: bool | None = None
    ) -> ProgrammeRevision:
        """Снимок текущего состояния программы как ревизии (без её изменения)."""
        return ProgrammeRevision(
            revision=self.revision,
            author=str(author),
            comment=str(comment),
            approved=self.is_approved if approved is None else bool(approved),
            reduction=self.reduction,
            set_refs=[dict(ref) for ref in self.set_refs],
            items=[ProgrammeItem(**item.to_dict()) for item in self.items],
        )

    def find_revision(self, number: int) -> ProgrammeRevision | None:
        """Архивная ревизия программы по номеру (или None)."""
        return next((item for item in self.revisions if item.revision == int(number)), None)

    def archive(
        self,
        *,
        author: str = "",
        comment: str = "",
        approved: bool | None = None,
    ) -> ProgrammeRevision:
        """Фиксирует текущее состояние программы в архиве ревизий (идемпотентно).

        Ревизия неизменяема: повторный вызов с тем же номером ничего не переписывает,
        кроме признака утверждения (`approved`) — утверждение не меняет состав.
        """
        stored = self.find_revision(self.revision)
        if stored is not None:
            if approved is not None:
                stored.approved = bool(approved)
            return stored
        stored = self.snapshot(author=author, comment=comment, approved=approved)
        self.revisions.append(stored)
        self.revisions.sort(key=lambda item: item.revision)
        return stored

    def revise(self, *, author: str = "", comment: str = "") -> ProgrammeRevision:
        """Выпускает новую ревизию: фиксирует текущую и открывает следующую (черновик).

        Утверждённая программа не редактируется: изменение состава — это новая ревизия
        (`FR-P-68`), а ранее полученные результаты остаются привязанными к прежней
        ревизии (`DR-P-14`) и не теряются.
        """
        stored = self.archive(author=author, comment=comment)
        self.revision = max((item.revision for item in self.revisions), default=self.revision) + 1
        self.status = PROGRAMME_DRAFT
        self.approved = {}
        self.updated_at = now_iso()
        return stored

    def approve(
        self,
        *,
        by: str = "",
        comment: str = "",
        reduction: str | None = None,
    ) -> ProgrammeRevision:
        """Утверждает текущую ревизию программы (`FR-P-69`, `AC-P-26`).

        Raises:
            ValueError: если программа пуста или в ней есть непокрытые разделы,
                а обоснование сокращения не задано — утверждение с сокращением
                требует обоснования, попадающего в протокол и отчёт.
        """
        self._require_draft()
        if reduction is not None:
            self.reduction = str(reduction).strip()
        if not self.items:
            raise ValueError("пустую программу утвердить нельзя: выберите хотя бы один набор")
        uncovered = self.uncovered_sections()
        if uncovered and not self.reduction.strip():
            raise ValueError(
                "утверждение с сокращением требует обоснования (FR-P-69): не покрыты разделы "
                + ", ".join(str(row["title"]) for row in uncovered)
            )
        stored = self.archive(author=by, comment=comment, approved=True)
        self.status = PROGRAMME_APPROVED
        self.approved = {
            "revision": self.revision,
            "author": str(by),
            "at": now_iso(),
            "comment": str(comment),
            "reduction": self.reduction.strip(),
        }
        self.updated_at = now_iso()
        return stored

    def restore_revision(
        self,
        number: int,
        *,
        author: str = "",
        comment: str = "",
    ) -> ProgrammeRevision:
        """Восстанавливает состав архивной ревизии как новую ревизию.

        Архивная ревизия не переписывается: её состав копируется в новую ревизию,
        поэтому история остаётся неизменяемой.
        """
        source = self.find_revision(number)
        if source is None:
            raise ValueError(f"ревизия {number} программы не найдена")
        self.archive(author=author)
        self.revision = max((item.revision for item in self.revisions), default=self.revision) + 1
        self.items = [ProgrammeItem(**item.to_dict()) for item in source.items]
        self.set_refs = [dict(ref) for ref in source.set_refs]
        self.reduction = source.reduction
        self.status = PROGRAMME_DRAFT
        self.approved = {}
        self._reindex()
        self.updated_at = now_iso()
        return source

    def revision_rows(self) -> list[dict[str, Any]]:
        """История ревизий: архив плюс текущая рабочая ревизия (панель «Ревизии»)."""
        items = list(self.revisions)
        if self.find_revision(self.revision) is None:
            items.append(self.snapshot())
        return [
            {
                "revision": item.revision,
                "at": item.at,
                "author": item.author,
                "comment": item.comment,
                "approved": item.approved,
                "size": item.size,
                "sets": len(item.set_refs),
                "current": item.revision == self.revision,
                "reduction": item.reduction,
            }
            for item in sorted(items, key=lambda entry: entry.revision, reverse=True)
        ]

    # -- сериализация ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Сериализует программу целиком (состав, выбранные наборы, ревизии)."""
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        payload["set_refs"] = [dict(ref) for ref in self.set_refs]
        payload["revisions"] = [item.to_dict() for item in self.revisions]
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Programme:
        """Восстанавливает программу из JSON (терпимо к отсутствующим полям)."""
        if not data:
            return cls()
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        payload["revision"] = int(payload.get("revision") or 1)
        status = payload.get("status")
        payload["status"] = status if status in PROGRAMME_STATUSES else PROGRAMME_DRAFT
        payload["reduction"] = str(payload.get("reduction") or "")
        payload["approved"] = dict(payload.get("approved") or {})
        payload["items"] = [ProgrammeItem.from_dict(item) for item in (data.get("items") or [])]
        payload["set_refs"] = [dict(ref) for ref in (data.get("set_refs") or [])]
        payload["revisions"] = [
            ProgrammeRevision.from_dict(item) for item in (data.get("revisions") or [])
        ]
        return cls(**payload)

    def _reindex(self) -> None:
        """Перенумеровывает порядок пунктов программы (`order` = 1…N)."""
        for index, item in enumerate(self.items, start=1):
            item.order = index

    def _touch(self) -> None:
        """Отмечает программу изменённой и открывает новую ревизию, если она нужна."""
        self.updated_at = now_iso()
        if self.find_revision(self.revision) is not None:
            self.revision = max(item.revision for item in self.revisions) + 1


def _merge_sources(
    sources: Sequence[dict[str, Any]], source: dict[str, Any]
) -> list[dict[str, Any]]:
    """Добавляет происхождение в список, если такого источника там ещё нет."""
    merged = [dict(item) for item in sources]
    if dict(source) not in merged:
        merged.append(dict(source))
    return merged


def _refresh_snapshot(item: ProgrammeItem) -> None:
    """Обновляет снимок описания пункта по каталогу (если проверка в каталоге есть)."""
    spec = catalog.find(item.check_id)
    if spec is None:
        return
    item.title = spec.title
    item.module = spec.module
    item.group = catalog.group_of(item.check_id)
    item.check_class = str(spec.check_class)
    item.requirement = spec.requirement
    item.expected = spec.expected
    item.automation = spec.automation or ""


# ---------------------------------------------------------------------------
# Шаблоны программ (FR-P-70) и хранение программы в сессии
# ---------------------------------------------------------------------------
def regress_check_ids(failed_ids: Sequence[str]) -> list[str]:
    """Состав «регресса после исправления»: отказы плюс смоук их разделов (`FR-P-70`)."""
    ids: list[str] = []
    for raw in failed_ids:
        check_id = str(raw).strip().upper()
        if check_id and check_id not in ids:
            ids.append(check_id)
    sections = {catalog.group_of(check_id) for check_id in ids}
    for check_id in smoke_check_ids():
        if catalog.group_of(check_id) in sections and check_id not in ids:
            ids.append(check_id)
    return ids


def template_programme(
    name: str,
    library: SetsLibrary,
    *,
    failed_ids: Sequence[str] = (),
    author: str = "",
) -> Programme:
    """Собирает программу по шаблону (`FR-P-70`).

    * «Полная программа» — наборы всех разделов (кроме смоук-минимума);
    * «Смоук-минимум» — только обязательный набор: быстрая проверка после установки;
    * «Регресс после исправления» — отказы и блокировки прошлой сессии плюс
      смоук-минимум их разделов.

    Raises:
        ValueError: если шаблон неизвестен.
    """
    if name == TEMPLATE_SMOKE:
        return Programme.from_sets(library, [SMOKE_SET_ID], author=author, comment=name)
    if name == TEMPLATE_FULL:
        set_ids = [item.set_id for item in library.sets if item.scope != SCOPE_SMOKE]
        return Programme.from_sets(library, set_ids, author=author, comment=name)
    if name == TEMPLATE_REGRESS:
        programme = Programme()
        smoke = set(smoke_check_ids())
        for check_id in regress_check_ids(failed_ids):
            programme.add_check(check_id, mandatory=check_id in smoke)
        programme.set_refs = [
            {
                "set_id": f"{TEMPLATE_SOURCE}: регресс",
                "revision": 0,
                "title": name,
                "section": "",
                "scope": SCOPE_SMOKE,
            }
        ]
        programme.approved = {"author": str(author), "comment": name} if author else {}
        return programme
    raise ValueError(f"неизвестный шаблон программы: {name}")


def load_programme(session: Any) -> Programme:
    """Программа сессии: из поля `programme`, при отсутствии — пустая."""
    return Programme.from_dict(getattr(session, "programme", None) or {})


def save_programme(
    session: Any,
    programme: Programme,
    *,
    event: str = "",
    message: str = "",
) -> Programme:
    """Сохраняет программу в сессию (схема v7) и, при необходимости, отмечает событием."""
    session.programme = programme.to_dict()
    if event:
        session.add_history(
            event,
            message or event,
            revision=programme.revision,
            status=programme.status,
            items=programme.size,
            sets=len(programme.set_refs),
        )
    return programme
