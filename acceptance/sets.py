"""Библиотека наборов проверок: состав, порядок, ревизии, утверждение (FR-P-65…FR-P-67).

Набор (`CheckSet`) — именованный список проверок каталога с порядком, **разделом
испытаний** и **целевым объёмом** (`смоук` / `стандарт` / `полный`). Библиотека
наборов — общие данные пульта (наборы переиспользуются сессиями как шаблоны),
поэтому она живёт отдельным файлом `acceptance_data/check_sets.json` — по образцу
ручных решений по записям (`records_overrides.json`). Сессия ссылается на наборы
по `set_id` и номеру ревизии, а состав программы собирает`programme.py`.

Ключевые правила:

* изменение **утверждённого** набора запрещено: сначала выпускается ревизия N+1
  (`FR-P-66`), прежние ревизии неизменяемы и доступны для чтения и восстановления;
* набор утверждает руководитель испытаний — фиксируются автор и время (`FR-P-67`);
* в разделе «Испытания» состав только читается (`IR-P-18`), правка состава —
  экран `SCR-101` «Наборы проверок».
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from acceptance.checks import catalog
from acceptance.checks.registry import CheckClass
from acceptance.paths import DATA_DIR, ensure_dirs
from acceptance.session import now_iso

#: Имя файла библиотеки наборов в каталоге данных пульта.
SETS_FILENAME = "check_sets.json"

#: Версия схемы файла библиотеки наборов.
SETS_SCHEMA_VERSION = 1

#: Статусы набора (FR-P-67): черновик редактируется, утверждённый — нет.
SET_DRAFT = "черновик"
SET_APPROVED = "утверждён"
SET_STATUSES = (SET_DRAFT, SET_APPROVED)

#: Целевой объём набора (FR-P-65).
SCOPE_SMOKE = "смоук"
SCOPE_STANDARD = "стандарт"
SCOPE_FULL = "полный"
SCOPES = (SCOPE_SMOKE, SCOPE_STANDARD, SCOPE_FULL)

#: Подписи целевых объёмов (что объём означает по смыслу).
SCOPE_LABELS: dict[str, str] = {
    SCOPE_SMOKE: "смоук — обязательный минимум (не исключается из программы)",
    SCOPE_STANDARD: "стандарт — все описанные проверки раздела",
    SCOPE_FULL: "полный — весь раздел по программе испытаний",
}

#: Идентификатор набора «Смоук-минимум» (FR-P-70).
SMOKE_SET_ID = "SET-SMOKE"


def section_options() -> tuple[str, ...]:
    """Ключи разделов испытаний (группы каталога, включая ненаполненные)."""
    return tuple(plan.key for plan in catalog.PLANNED_GROUPS)


def section_title(section: str) -> str:
    """Название раздела испытаний по ключу группы (`TC-TASK` → «Task service»)."""
    key = str(section).strip().upper()
    plan = next((item for item in catalog.PLANNED_GROUPS if item.key == key), None)
    return plan.module if plan is not None else str(section)


def section_totals(section: str) -> dict[str, int]:
    """Объём раздела: `planned` (по программе испытаний) и `implemented` (в каталоге)."""
    key = str(section).strip().upper()
    plan = next((item for item in catalog.PLANNED_GROUPS if item.key == key), None)
    found = catalog.group(key)
    return {
        "planned": plan.checks_total if plan is not None else 0,
        "implemented": len(found.checks) if found is not None else 0,
    }


def suggested_set_id(section: str, *, taken: tuple[str, ...] | list[str] = ()) -> str:
    """Идентификатор набора по разделу: `SET-SYS`, при занятости — `SET-SYS-2`, `-3`, …"""
    key = str(section).strip().upper()
    base = f"SET-{key[3:]}" if key.startswith("TC-") else "SET-NEW"
    used = {str(item).strip().upper() for item in taken}
    if base.upper() not in used:
        return base
    index = 2
    while f"{base}-{index}".upper() in used:
        index += 1
    return f"{base}-{index}"


@dataclass
class SetItem:
    """Пункт набора: проверка каталога с порядком и признаком обязательности."""

    check_id: str
    order: int = 1
    mandatory: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Сериализует пункт набора."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetItem:
        """Восстанавливает пункт набора из JSON (терпимо к неизвестным ключам)."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload["check_id"] = str(payload.get("check_id") or "")
        payload["order"] = int(payload.get("order") or 1)
        payload["mandatory"] = bool(payload.get("mandatory"))
        return cls(**payload)


@dataclass
class SetRevision:
    """Неизменяемая ревизия набора: состав, автор, время, комментарий (FR-P-66)."""

    revision: int
    title: str = ""
    section: str = ""
    scope: str = SCOPE_STANDARD
    author: str = ""
    at: str = field(default_factory=now_iso)
    comment: str = ""
    items: list[SetItem] = field(default_factory=list)

    @property
    def check_ids(self) -> list[str]:
        """Идентификаторы проверок ревизии (в порядке состава)."""
        return [item.check_id for item in self.items]

    @property
    def size(self) -> int:
        """Число проверок в ревизии."""
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует ревизию."""
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetRevision:
        """Восстанавливает ревизию из JSON."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload["revision"] = int(payload.get("revision") or 1)
        payload["items"] = [SetItem.from_dict(item) for item in (data.get("items") or [])]
        return cls(**payload)


@dataclass
class CheckSet:
    """Набор проверок: состав с порядком, раздел, целевой объём, ревизии, утверждение."""

    set_id: str
    title: str
    section: str = ""
    scope: str = SCOPE_STANDARD
    status: str = SET_DRAFT
    author: str = ""
    revision: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    note: str = ""
    items: list[SetItem] = field(default_factory=list)
    revisions: list[SetRevision] = field(default_factory=list)

    # -- свойства -------------------------------------------------------------
    @property
    def is_approved(self) -> bool:
        """True, если набор утверждён (правка состава запрещена, FR-P-67)."""
        return self.status == SET_APPROVED

    @property
    def size(self) -> int:
        """Число проверок в составе набора."""
        return len(self.items)

    @property
    def check_ids(self) -> list[str]:
        """Идентификаторы проверок набора (в порядке состава)."""
        return [item.check_id for item in self.items]

    @property
    def mandatory_ids(self) -> list[str]:
        """Проверки набора, обязательные для программы (смоук-минимум, FR-P-70)."""
        return [item.check_id for item in self.items if item.mandatory]

    @property
    def scope_label(self) -> str:
        """Расшифровка целевого объёма набора."""
        return SCOPE_LABELS.get(self.scope, self.scope)

    @property
    def section_title(self) -> str:
        """Название раздела испытаний набора."""
        return section_title(self.section)

    @property
    def icon(self) -> str:
        """Индикатор статуса набора для дерева библиотеки."""
        return "✅" if self.is_approved else "✎"

    def has(self, check_id: str) -> bool:
        """True, если проверка входит в состав набора."""
        return self.find(check_id) is not None

    def find(self, check_id: str) -> SetItem | None:
        """Пункт набора по идентификатору проверки."""
        key = str(check_id).strip().upper()
        return next((item for item in self.items if item.check_id.upper() == key), None)

    def present(self, check_ids: Sequence[str]) -> list[str]:
        """Идентификаторы набора, попавшие в переданный список (в порядке состава)."""
        keys = {str(item).strip().upper() for item in check_ids}
        return [item.check_id for item in self.items if item.check_id in keys]

    # -- правка состава (только черновик, FR-P-66) ----------------------------
    def _require_draft(self) -> None:
        """Запрещает правку утверждённого набора (нужна новая ревизия)."""
        if self.is_approved:
            raise ValueError(
                f"набор {self.set_id} утверждён: изменение возможно только новой ревизией (FR-P-66)"
            )

    def add(
        self,
        check_ids: Sequence[str],
        *,
        mandatory: bool | None = None,
        section: str = "",
    ) -> list[str]:
        """Добавляет проверки в конец состава; возвращает фактически добавленные."""
        self._require_draft()
        added: list[str] = []
        known = self.check_ids
        for raw in check_ids:
            check_id = str(raw).strip().upper()
            if not check_id or check_id in known:
                continue
            known.append(check_id)
            self.items.append(
                SetItem(check_id=check_id, order=len(self.items) + 1, mandatory=bool(mandatory))
            )
            added.append(check_id)
        if added:
            if section:
                self.section = str(section).strip().upper()
            self._touch()
        return added

    def remove(self, check_ids: Sequence[str]) -> list[str]:
        """Убирает проверки из состава; возвращает фактически убранные."""
        self._require_draft()
        removed = self.present(check_ids)
        if removed:
            keys = {str(item).strip().upper() for item in check_ids}
            self.items = [item for item in self.items if item.check_id not in keys]
            self._reindex()
            self._touch()
        return removed

    def replace_all(self, check_ids: Sequence[str]) -> None:
        """Заменяет состав целиком (импорт набора, FR-P-70)."""
        self._require_draft()
        self.items = []
        self.add(check_ids)

    def set_mandatory(self, check_id: str, mandatory: bool = True) -> bool:
        """Ставит/снимает признак обязательности проверки (смоук-минимум)."""
        self._require_draft()
        item = self.find(check_id)
        if item is None:
            return False
        item.mandatory = bool(mandatory)
        self._touch()
        return True

    def move(self, check_id: str, delta: int) -> bool:
        """Сдвигает проверку по порядку на `delta` позиций (↑/↓ в списке состава)."""
        self._require_draft()
        key = str(check_id).strip().upper()
        index = next((i for i, item in enumerate(self.items) if item.check_id == key), -1)
        if index < 0:
            return False
        target = max(0, min(len(self.items) - 1, index + int(delta)))
        if target == index:
            return False
        self.items.insert(target, self.items.pop(index))
        self._reindex()
        self._touch()
        return True

    def reorder(self, check_ids: Sequence[str]) -> None:
        """Задаёт порядок состава: переданные проверки — впереди, остальные — как были."""
        self._require_draft()
        order = [str(item).strip().upper() for item in check_ids]
        placed = {item.check_id for item in self.items if item.check_id in set(order)}
        head = [item for key in order for item in self.items if item.check_id == key]
        tail = [item for item in self.items if item.check_id not in placed]
        self.items = head + tail
        self._reindex()
        self._touch()

    def rename(self, title: str, *, note: str | None = None) -> None:
        """Переименовывает набор (и, при необходимости, меняет примечание)."""
        self._require_draft()
        self.title = str(title).strip() or self.title
        if note is not None:
            self.note = str(note)
        self._touch()

    def set_section(self, section: str, *, scope: str | None = None) -> None:
        """Меняет раздел испытаний и (опционально) целевой объём набора (FR-P-65)."""
        self._require_draft()
        self.section = str(section).strip().upper()
        if scope is not None:
            self.scope = scope if scope in SCOPES else self.scope
        self._touch()

    def _reindex(self) -> None:
        """Перенумеровывает порядок состава (`order` = 1…N)."""
        for index, item in enumerate(self.items, start=1):
            item.order = index

    def _touch(self) -> None:
        """Отмечает набор изменённым и открывает следующую ревизию, если она нужна.

        Правило `FR-P-66`: ревизии неизменяемы. Если текущий номер уже зафиксирован
        в архиве (набор правили после фиксации/утверждения), рабочий номер поднимается
        до следующего свободного — иначе правка «затёрла» бы зафиксированный состав
        под тем же номером.
        """
        self.updated_at = now_iso()
        if self.find_revision(self.revision) is not None:
            self.revision = max(item.revision for item in self.revisions) + 1

    # -- ревизии и утверждение (FR-P-66, FR-P-67) -----------------------------
    def snapshot(self, *, author: str = "", comment: str = "") -> SetRevision:
        """Снимок текущего состояния набора как ревизии (без изменения набора)."""
        return SetRevision(
            revision=self.revision,
            title=self.title,
            section=self.section,
            scope=self.scope,
            author=str(author or self.author),
            comment=str(comment),
            items=[SetItem(**item.to_dict()) for item in self.items],
        )

    def find_revision(self, number: int) -> SetRevision | None:
        """Архивная ревизия набора по номеру (или None)."""
        return next((item for item in self.revisions if item.revision == int(number)), None)

    def archive(self, *, author: str = "", comment: str = "") -> SetRevision:
        """Записывает текущее состояние набора в архив ревизий (идемпотентно)."""
        stored = self.find_revision(self.revision)
        if stored is not None:
            return stored
        stored = self.snapshot(author=author, comment=comment)
        self.revisions.append(stored)
        self.revisions.sort(key=lambda item: item.revision)
        return stored

    def revise(self, *, author: str = "", comment: str = "") -> SetRevision:
        """Выпускает новую ревизию: архивирует текущую и открывает следующую (черновик)."""
        stored = self.archive(author=author, comment=comment)
        self.revision = max((item.revision for item in self.revisions), default=self.revision) + 1
        self.status = SET_DRAFT
        self.author = str(author or self.author)
        self._touch()
        return stored

    def reopen(self, *, author: str = "", comment: str = "") -> SetRevision:
        """Переоткрывает утверждённый набор новой ревизией (правило FR-P-66)."""
        return self.revise(author=author, comment=comment or "переоткрытие набора")

    def approve(self, *, author: str = "", comment: str = "") -> SetRevision:
        """Утверждает текущую ревизию набора: статус, автор, время (FR-P-67).

        Утверждённая ревизия остаётся с тем же номером (её фиксирует архив), поэтому
        отметка времени ставится напрямую: `_touch` открыл бы новую ревизию и снял
        только что поставленный статус.
        """
        stored = self.archive(author=author, comment=comment)
        self.status = SET_APPROVED
        self.author = str(author or self.author)
        self.updated_at = now_iso()
        return stored

    def restore_revision(self, number: int, *, author: str = "", comment: str = "") -> SetRevision:
        """Восстанавливает состав архивной ревизии как новую ревизию (FR-P-66).

        Архивная ревизия не переписывается: её состав копируется в новую ревизию N+1,
        поэтому история остаётся неизменяемой, а набор — редактируемым.
        """
        source = self.find_revision(number)
        if source is None:
            raise ValueError(f"ревизия {number} набора {self.set_id} не найдена")
        self.archive(author=author)
        self.revision = max((item.revision for item in self.revisions), default=self.revision) + 1
        self.title = source.title or self.title
        self.section = source.section or self.section
        self.scope = source.scope or self.scope
        self.items = [SetItem(**item.to_dict()) for item in source.items]
        self.status = SET_DRAFT
        self.author = str(author or self.author)
        self._reindex()
        self._touch()
        return source

    # -- представление --------------------------------------------------------
    def revision_rows(self) -> list[dict[str, Any]]:
        """История ревизий для панели «Ревизии»: архив плюс текущая рабочая ревизия.

        Рабочая ревизия может быть ещё не зафиксирована в архиве (её фиксируют
        «Сохранить ревизию» / «Утвердить»), но руководителю испытаний она нужна в
        истории — иначе после выпуска новой ревизии список выглядел бы пустым.
        """
        items = list(self.revisions)
        if self.find_revision(self.revision) is None:
            items.append(self.snapshot())
        return [
            {
                "revision": item.revision,
                "at": item.at,
                "author": item.author,
                "comment": item.comment,
                "size": item.size,
                "title": item.title,
                "current": item.revision == self.revision,
            }
            for item in sorted(items, key=lambda entry: entry.revision, reverse=True)
        ]

    def summary(self) -> dict[str, Any]:
        """KPI набора для карточки библиотеки (`SCR-101`)."""
        classes: dict[str, int] = {}
        unknown = 0
        for check_id in self.check_ids:
            spec = catalog.find(check_id)
            if spec is None:
                unknown += 1
                continue
            key = str(spec.check_class)
            classes[key] = classes.get(key, 0) + 1
        return {
            "set_id": self.set_id,
            "title": self.title,
            "section": self.section,
            "section_title": self.section_title,
            "scope": self.scope,
            "scope_label": self.scope_label,
            "status": self.status,
            "is_approved": self.is_approved,
            "author": self.author,
            "revision": self.revision,
            "size": self.size,
            "mandatory": len(self.mandatory_ids),
            "revisions": len(self.revisions),
            "classes": classes,
            "unknown": unknown,
            "updated_at": self.updated_at,
            "note": self.note,
        }

    def copy(self, *, set_id: str, title: str = "") -> CheckSet:
        """Копия набора: новый идентификатор, черновик, история ревизий не переносится."""
        return CheckSet(
            set_id=str(set_id),
            title=str(title).strip() or f"{self.title} (копия)",
            section=self.section,
            scope=self.scope,
            author=self.author,
            note=f"копия набора {self.set_id}",
            items=[SetItem(**item.to_dict()) for item in self.items],
        )

    # -- сериализация ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Сериализует набор целиком (состав и архив ревизий)."""
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        payload["revisions"] = [item.to_dict() for item in self.revisions]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckSet:
        """Восстанавливает набор из JSON (терпимо к отсутствующим полям)."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in data.items() if key in allowed}
        payload["set_id"] = str(payload.get("set_id") or "SET-NEW")
        payload["title"] = str(payload.get("title") or payload["set_id"])
        payload["revision"] = int(payload.get("revision") or 1)
        status = payload.get("status")
        payload["status"] = status if status in SET_STATUSES else SET_DRAFT
        payload["items"] = [SetItem.from_dict(item) for item in (data.get("items") or [])]
        payload["revisions"] = [
            SetRevision.from_dict(item) for item in (data.get("revisions") or [])
        ]
        return cls(**payload)


@dataclass
class SetsLibrary:
    """Библиотека наборов проверок: хранение, поиск, создание и копирование (FR-P-65)."""

    sets: list[CheckSet] = field(default_factory=list)
    schema_version: int = SETS_SCHEMA_VERSION

    # -- чтение --------------------------------------------------------------
    def find(self, set_id: str) -> CheckSet | None:
        """Набор по идентификатору (`SET-SYS`)."""
        key = str(set_id).strip().upper()
        return next((item for item in self.sets if item.set_id.upper() == key), None)

    def ids(self) -> list[str]:
        """Идентификаторы всех наборов библиотеки."""
        return [item.set_id for item in self.sets]

    def by_section(self, section: str) -> list[CheckSet]:
        """Наборы раздела испытаний (`TC-SYS`)."""
        key = str(section).strip().upper()
        return [item for item in self.sets if item.section.upper() == key]

    def approved(self) -> list[CheckSet]:
        """Утверждённые наборы библиотеки."""
        return [item for item in self.sets if item.is_approved]

    def drafts(self) -> list[CheckSet]:
        """Наборы-черновики библиотеки."""
        return [item for item in self.sets if not item.is_approved]

    # -- запись --------------------------------------------------------------
    def upsert(self, item: CheckSet) -> CheckSet:
        """Добавляет набор или заменяет существующий с тем же `set_id`."""
        stored = self.find(item.set_id)
        if stored is None:
            self.sets.append(item)
            return item
        index = self.sets.index(stored)
        self.sets[index] = item
        return item

    def create(
        self,
        *,
        title: str,
        section: str = "",
        scope: str = SCOPE_STANDARD,
        check_ids: Sequence[str] = (),
        author: str = "",
        note: str = "",
        mandatory: bool = False,
    ) -> CheckSet:
        """Создаёт новый набор (идентификатор выводится из раздела)."""
        key = str(section).strip().upper()
        item = CheckSet(
            set_id=suggested_set_id(key, taken=self.ids()),
            title=str(title).strip() or "Новый набор",
            section=key,
            scope=scope if scope in SCOPES else SCOPE_STANDARD,
            author=str(author),
            note=str(note),
        )
        if check_ids:
            item.add(check_ids, mandatory=mandatory)
        self.upsert(item)
        return item

    def copy(self, set_id: str, *, title: str = "", set_id_new: str = "") -> CheckSet:
        """Копирует набор под новым идентификатором (FR-P-65)."""
        source = self.find(set_id)
        if source is None:
            raise ValueError(f"набор {set_id} не найден в библиотеке")
        new_id = str(set_id_new).strip() or suggested_set_id(source.section, taken=self.ids())
        clone = source.copy(set_id=new_id, title=title)
        self.upsert(clone)
        return clone

    def remove(self, set_id: str, *, force: bool = False) -> bool:
        """Удаляет набор: черновик — свободно, утверждённый — только с `force`.

        Утверждённый набор — документ испытаний, поэтому по умолчанию удаление
        запрещено: сначала набор переоткрывают новой ревизией (FR-P-66).
        """
        item = self.find(set_id)
        if item is None:
            return False
        if item.is_approved and not force:
            raise ValueError(f"набор {item.set_id} утверждён: удаление требует force (FR-P-67)")
        self.sets.remove(item)
        return True

    # -- представление -------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Сводка библиотеки для шапки `SCR-101`."""
        return {
            "sets": len(self.sets),
            "approved": len(self.approved()),
            "drafts": len(self.drafts()),
            "checks": sum(item.size for item in self.sets),
            "sections": len({item.section for item in self.sets if item.section}),
        }

    def section_rows(self) -> list[dict[str, Any]]:
        """Разделы библиотеки: сколько наборов и проверок приходится на раздел (FR-P-65)."""
        rows: list[dict[str, Any]] = []
        for key in section_options():
            items = self.by_section(key)
            totals = section_totals(key)
            rows.append(
                {
                    "section": key,
                    "title": section_title(key),
                    "sets": len(items),
                    "approved": sum(1 for item in items if item.is_approved),
                    "checks": len({check_id for item in items for check_id in item.check_ids}),
                    "planned": totals["planned"],
                    "implemented": totals["implemented"],
                }
            )
        return rows

    def to_dict(self) -> dict[str, Any]:
        """Сериализует библиотеку наборов."""
        return {
            "schema_version": self.schema_version,
            "sets": [item.to_dict() for item in self.sets],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetsLibrary:
        """Восстанавливает библиотеку из JSON."""
        items = [CheckSet.from_dict(item) for item in (data.get("sets") or [])]
        return cls(
            sets=items,
            schema_version=int(data.get("schema_version", SETS_SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Файловое хранение библиотеки и стартовое наполнение из каталога
# ---------------------------------------------------------------------------
def sets_path(directory: Path | None = None) -> Path:
    """Путь к файлу библиотеки наборов (`acceptance_data/check_sets.json`)."""
    base = Path(directory) if directory is not None else DATA_DIR
    return base / SETS_FILENAME


def load_sets(directory: Path | None = None) -> SetsLibrary:
    """Читает библиотеку наборов; при отсутствии или повреждении файла — пустую."""
    path = sets_path(directory)
    if not path.exists():
        return SetsLibrary()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SetsLibrary()

    if not isinstance(payload, dict):
        return SetsLibrary()
    return SetsLibrary.from_dict(payload)


def save_sets(library: SetsLibrary, directory: Path | None = None) -> Path:
    """Сохраняет библиотеку наборов на диск (UTF-8, читаемый JSON)."""
    ensure_dirs()
    path = sets_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(library.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def smoke_check_ids(specs: Sequence[Any] | None = None) -> list[str]:
    """Смоук-минимум: по одной первой `tech`-проверке каждого наполненного раздела."""
    source = tuple(specs) if specs is not None else catalog.CHECKS
    ids: list[str] = []
    seen: set[str] = set()
    for spec in source:
        group = catalog.group_of(spec.check_id)
        if not group or group in seen or spec.check_class != CheckClass.TECH:
            continue
        seen.add(group)
        ids.append(spec.check_id)
    return ids


def library_from_catalog(*, author: str = "", smoke: bool = True) -> SetsLibrary:
    """Стартовая библиотека: набор на каждый наполненный раздел + «Смоук-минимум».

    Нужна при первом запуске пульта (файла `check_sets.json` ещё нет): руководитель
    испытаний получает готовые наборы и правит их, а не собирает библиотеку с нуля.
    """
    library = SetsLibrary()
    for group in catalog.groups(implemented_only=True):
        scope = SCOPE_FULL if len(group.checks) == group.plan.checks_total else SCOPE_STANDARD
        title = (
            f"{group.module} — полный объём раздела"
            if scope == SCOPE_FULL
            else f"{group.module} — стандарт (описанные проверки)"
        )
        item = CheckSet(
            set_id=suggested_set_id(group.key, taken=library.ids()),
            title=title,
            section=group.key,
            scope=scope,
            author=str(author),
            note=f"создан из каталога: {group.title} (этап {group.stage})",
        )
        item.add([spec.check_id for spec in group.checks])
        library.upsert(item)

    if smoke:
        ids = smoke_check_ids()
        if ids:
            item = CheckSet(
                set_id=SMOKE_SET_ID,
                title="Смоук-минимум (обязательный)",
                section="",
                scope=SCOPE_SMOKE,
                author=str(author),
                note="обязательные проверки: не исключаются из программы и идут первыми (FR-P-70)",
            )
            item.add(ids, mandatory=True)
            library.upsert(item)
    return library
