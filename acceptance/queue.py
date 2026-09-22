"""Очередь прогона: снимок утверждённой ревизии программы (`FR-P-13`, `IR-P-18`).

Очередь — **производная** программы сессии: её состав и порядок копируются из
ревизии в момент начала прогона и дальше не редактируются. Второго списка проверок
в ПУЛЬТЕ нет, поэтому у очереди **нет** методов добавления, удаления и перестановки
пунктов: состав меняет только руководитель испытаний на экране «Программа сессии»
(`SCR-15`) — новой ревизией программы. Инженер-испытатель может снять пункт с
причиной, но это **результат прогона** (`FR-P-15`), поэтому причина снятия живёт
как результат (`results.py`, `DR-P-5`), а очередь хранит лишь состояние пункта.

Что очередь хранит сама (состояние прогона):

* состояние пункта — `ожидает`, `выполняется`, `выполнена`, `снята`;
* причину снятия и примечание прогона, параметры запуска вызова (`DR-P-3`);
* индекс вызова в режиме «по вызовам» и диапазон номеров журнала обмена;
* указатель прогона и «последний» пункт — для маркеров «следующая»,
  «выполняется», «последняя» на экране «Прогон» (`SCR-05`);
* режим исполнения и паузу авто-прогона.

Статус проверки, вердикт и доказательства очередь **не хранит**: единственный
источник результата — `results.py` (`DR-P-5`), поэтому двойной учёт невозможен.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from acceptance import results as results_api
from acceptance.checks.registry import CheckStatus
from acceptance.programme import Programme
from acceptance.session import now_iso

#: Состояние пункта очереди (состояние прогона, а не результат проверки).
QUEUE_PENDING = "ожидает"
QUEUE_RUNNING = "выполняется"
QUEUE_DONE = "выполнена"
QUEUE_OFF = "снята"
QUEUE_STATES: tuple[str, ...] = (QUEUE_PENDING, QUEUE_RUNNING, QUEUE_DONE, QUEUE_OFF)

QUEUE_ICONS: dict[str, str] = {
    QUEUE_PENDING: "⚪",
    QUEUE_RUNNING: "▶",
    QUEUE_DONE: "✅",
    QUEUE_OFF: "⏭",
}

#: Маркеры пункта на экране «Прогон» (макет `docs/16`, `SCR-05`).
MARKER_NEXT = "следующая"
MARKER_RUNNING = "выполняется"
MARKER_LAST = "последняя"

#: Режим исполнения: что делает команда «Следующая».
MODE_CHECK = "сценарием"
MODE_CALL = "по вызовам"
MODE_LABELS: dict[str, str] = {
    MODE_CHECK: "выполнять проверку целиком (сценарием)",
    MODE_CALL: "выполнять по вызовам",
}

DEFAULT_PAUSE_SECONDS = 3.0
MIN_PAUSE_SECONDS = 0.5
MAX_PAUSE_SECONDS = 120.0


def clamp_pause(value: Any) -> float:
    """Приводит паузу авто-прогона к допустимому диапазону."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PAUSE_SECONDS
    return float(min(MAX_PAUSE_SECONDS, max(MIN_PAUSE_SECONDS, seconds)))


def _key(check_id: str) -> str:
    """Идентификатор проверки в нормальном виде (без пробелов, верхний регистр)."""
    return str(check_id).strip().upper()


@dataclass
class QueueItem:
    """Пункт очереди: что и в каком порядке отправляется на стенд.

    Описание проверки (название, класс, требование) здесь **не дублируется**: его
    читают из программы (`ProgrammeItem` — снимок описания ревизии, `DR-P-14`).
    """

    check_id: str
    order: int = 1
    state: str = QUEUE_PENDING
    reason: str = ""
    note: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    call_index: int = 0
    journal_from: int | None = None
    journal_to: int | None = None

    @property
    def is_pending(self) -> bool:
        """True, если пункт ещё не выполнялся."""
        return self.state == QUEUE_PENDING

    @property
    def is_running(self) -> bool:
        """True, если пункт выполняется прямо сейчас."""
        return self.state == QUEUE_RUNNING

    @property
    def is_done(self) -> bool:
        """True, если прогон по пункту завершён (в том числе «снят»)."""
        return self.state in (QUEUE_DONE, QUEUE_OFF)

    @property
    def is_off(self) -> bool:
        """True, если пункт снят с очереди (с причиной, `FR-P-15`)."""
        return self.state == QUEUE_OFF

    @property
    def icon(self) -> str:
        """Иконка состояния прогона."""
        return QUEUE_ICONS.get(self.state, "⚪")

    @property
    def journal_range(self) -> str:
        """Диапазон номеров журнала обмена прогона пункта: «#214–#217»."""
        if self.journal_from is None and self.journal_to is None:
            return ""
        start = "" if self.journal_from is None else f"#{self.journal_from}"
        finish = "" if self.journal_to is None else f"#{self.journal_to}"
        return f"{start}–{finish}".strip("–")

    def to_dict(self) -> dict[str, Any]:
        """Сериализует пункт очереди."""
        payload = asdict(self)
        payload["params"] = dict(self.params)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QueueItem:
        """Восстанавливает пункт очереди из JSON (терпимо к неизвестным полям)."""
        allowed = cls.__dataclass_fields__
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        payload["check_id"] = _key(str(payload.get("check_id") or ""))
        payload["order"] = int(payload.get("order") or 1)
        state = str(payload.get("state") or QUEUE_PENDING)
        payload["state"] = state if state in QUEUE_STATES else QUEUE_PENDING
        payload["reason"] = str(payload.get("reason") or "")
        payload["note"] = str(payload.get("note") or "")
        payload["params"] = dict(payload.get("params") or {})
        payload["call_index"] = max(0, int(payload.get("call_index") or 0))
        return cls(**payload)


@dataclass
class Queue:
    """Очередь прогона: снимок ревизии программы плюс состояние прогона.

    Состав очереди неприкосновенен (`IR-P-18`) — правок состава в этом классе нет
    по построению, а расхождение с программой ловится `is_current()` по отпечатку
    состава (`signature`).
    """

    revision: int = 0
    approved: bool = False
    draft: bool = False
    signature: str = ""
    created_at: str = field(default_factory=now_iso)
    author: str = ""
    items: list[QueueItem] = field(default_factory=list)
    pointer: int = 0
    last_check_id: str = ""
    mode: str = MODE_CHECK
    pause_seconds: float = DEFAULT_PAUSE_SECONDS
    stop_reason: str = ""

    # -- чтение ---------------------------------------------------------------
    @property
    def size(self) -> int:
        """Число пунктов очереди."""
        return len(self.items)

    @property
    def is_empty(self) -> bool:
        """True, если очередь пуста (программа не собрана)."""
        return not self.items

    @property
    def check_ids(self) -> list[str]:
        """Идентификаторы пунктов очереди в порядке прогона."""
        return [item.check_id for item in self.items]

    @property
    def pending_ids(self) -> list[str]:
        """Идентификаторы пунктов, которые ещё не выполнялись."""
        return [item.check_id for item in self.items if item.is_pending]

    @property
    def mode_label(self) -> str:
        """Подпись режима исполнения для интерфейса."""
        return MODE_LABELS.get(self.mode, self.mode)

    def find(self, check_id: str) -> QueueItem | None:
        """Пункт очереди по идентификатору проверки."""
        key = _key(check_id)
        return next((item for item in self.items if _key(item.check_id) == key), None)

    def index_of(self, check_id: str) -> int:
        """Позиция пункта в очереди (-1, если его там нет)."""
        key = _key(check_id)
        for index, item in enumerate(self.items):
            if _key(item.check_id) == key:
                return index
        return -1

    def next_item(self) -> QueueItem | None:
        """«Следующая»: первый ожидающий пункт (выполненные и снятые пропускаются).

        Обойти пункт нельзя (`FR-P-16`): чтобы пропустить его, нужно снять пункт
        с причиной или выпустить новую ревизию программы.
        """
        return next((item for item in self.items if item.is_pending), None)

    def current(self) -> QueueItem | None:
        """Пункт, который выполняется прямо сейчас («выполняется»)."""
        return next((item for item in self.items if item.is_running), None)

    def last(self) -> QueueItem | None:
        """«Последняя»: пункт, который прогнали последним (по нему читают ленту)."""
        return self.find(self.last_check_id) if self.last_check_id else None

    def marker(self, check_id: str) -> str:
        """Маркер пункта: «следующая», «выполняется», «последняя» или пусто."""
        item = self.find(check_id)
        if item is None:
            return ""
        if item.is_running:
            return MARKER_RUNNING
        following = self.next_item()
        if following is not None and _key(following.check_id) == _key(item.check_id):
            return MARKER_NEXT
        if self.last_check_id and _key(self.last_check_id) == _key(item.check_id):
            return MARKER_LAST
        return ""

    def rows(self, results: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Строки очереди для экрана «Прогон» (`SCR-05`).

        Args:
            results: состояние проверок из `results.py` (`check_id` → строка
                результата). Очередь статус **не хранит** (`DR-P-5`), поэтому
                таблица подтягивает его снаружи.
        """
        states = dict(results or {})
        rows: list[dict[str, Any]] = []
        for item in self.items:
            state = states.get(item.check_id)
            status = results_api.STATUS_NOT_RUN
            icon = "⚪"
            verdict = ""
            repeats = 0
            revision: Any = None
            if isinstance(state, Mapping):
                status = str(state.get("status") or results_api.STATUS_NOT_RUN)
                icon = str(state.get("icon") or "⚪")
                verdict = str(state.get("verdict") or "")
                repeats = int(state.get("repeats") or 0)
                revision = state.get("revision")
            rows.append(
                {
                    "order": item.order,
                    "check_id": item.check_id,
                    "state": item.state,
                    "state_icon": item.icon,
                    "marker": self.marker(item.check_id),
                    "reason": item.reason,
                    "note": item.note,
                    "params": dict(item.params),
                    "call_index": item.call_index,
                    "journal_from": item.journal_from,
                    "journal_to": item.journal_to,
                    "journal_range": item.journal_range,
                    "status": status,
                    "icon": icon,
                    "verdict": verdict,
                    "repeats": repeats,
                    "revision": revision,
                }
            )
        return rows

    def summary(self, results: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """KPI очереди: сколько пройдено, сколько осталось, чем закончилось."""
        rows = self.rows(results)
        states = [item.state for item in self.items]
        following = self.next_item()
        return {
            "revision": self.revision,
            "approved": self.approved,
            "draft": self.draft,
            "mode": self.mode,
            "mode_label": self.mode_label,
            "pause_seconds": self.pause_seconds,
            "total": len(self.items),
            "pending": states.count(QUEUE_PENDING),
            "running": states.count(QUEUE_RUNNING),
            "done": states.count(QUEUE_DONE),
            "off": states.count(QUEUE_OFF),
            "passed": sum(1 for row in rows if row["status"] == str(CheckStatus.PASSED)),
            "failed": sum(1 for row in rows if row["status"] == str(CheckStatus.FAILED)),
            "blocked": sum(1 for row in rows if row["status"] == str(CheckStatus.BLOCKED)),
            "next": "" if following is None else following.check_id,
            "last": self.last_check_id,
            "pointer": self.pointer,
            "stop_reason": self.stop_reason,
        }

    # -- прогон ---------------------------------------------------------------
    def begin(self, check_id: str) -> QueueItem | None:
        """Отмечает пункт выполняющимся (перед отправкой на стенд)."""
        item = self.find(check_id)
        if item is None or item.is_off:
            return None
        item.state = QUEUE_RUNNING
        item.note = ""
        self.last_check_id = item.check_id
        self.stop_reason = ""
        return item

    def finish(
        self,
        check_id: str,
        *,
        state: str = QUEUE_DONE,
        journal_from: int | None = None,
        journal_to: int | None = None,
        note: str = "",
    ) -> QueueItem | None:
        """Закрывает пункт после прогона и двигает указатель прогона."""
        item = self.find(check_id)
        if item is None:
            return None
        item.state = state if state in QUEUE_STATES else QUEUE_DONE
        item.journal_from = journal_from
        item.journal_to = journal_to
        if note:
            item.note = str(note)
        self.last_check_id = item.check_id
        self.pointer = max(self.pointer, self.index_of(item.check_id) + 1)
        return item

    def take_off(
        self,
        check_id: str,
        reason: str,
        *,
        session: Any = None,
        author: str = "",
    ) -> QueueItem | None:
        """Снимает пункт с очереди **с причиной** — это результат, а не правка состава.

        Состав программы при снятии не меняется (`FR-P-15`): пункт остаётся в
        очереди с состоянием «снята», а причина попадает в протокол как результат
        (`results.suspend` → `session.checks`).

        Raises:
            ValueError: если причина не задана.
        """
        key = _key(check_id)
        text = str(reason or "").strip()
        if not text:
            raise ValueError(f"снятие пункта {key or '?'} требует причины (FR-P-15)")
        item = self.find(key)
        if item is None:
            return None
        item.state = QUEUE_OFF
        item.reason = text
        if item.call_index:
            item.note = f"снят после {item.call_index} вызовов: {text}"
        if session is not None:
            results_api.suspend(session, item.check_id, text, author=author, revision=self.revision)
        return item

    def return_to_queue(
        self, check_id: str, *, session: Any = None, author: str = ""
    ) -> QueueItem | None:
        """Возвращает снятый пункт в очередь (результат снятия убирается)."""
        item = self.find(check_id)
        if item is None:
            return None
        item.state = QUEUE_PENDING
        item.reason = ""
        item.note = ""
        item.call_index = 0
        if session is not None:
            results_api.resume(session, item.check_id, author=author)
        return item

    def reset(self, *, session: Any = None, author: str = "") -> None:
        """Начинает прогон заново: отметки сняты, состав очереди неприкосновенен."""
        for item in self.items:
            if item.is_off and session is not None:
                results_api.resume(session, item.check_id, author=author)
            item.state = QUEUE_PENDING
            item.reason = ""
            item.note = ""
            item.call_index = 0
            item.journal_from = None
            item.journal_to = None
        self.pointer = 0
        self.last_check_id = ""
        self.stop_reason = ""

    def set_mode(self, mode: str) -> str:
        """Задаёт режим исполнения («сценарием» / «по вызовам»)."""
        self.mode = mode if mode in MODE_LABELS else MODE_CHECK
        return self.mode

    def set_pause(self, seconds: Any) -> float:
        """Задаёт паузу авто-прогона (с приведением к диапазону)."""
        self.pause_seconds = clamp_pause(seconds)
        return self.pause_seconds

    def is_current(self, programme: Programme) -> bool:
        """True, если состав очереди совпадает с составом текущей ревизии программы."""
        return bool(self.signature) and self.signature == queue_signature(programme)

    # -- сериализация ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Сериализует очередь в сессию (схема v7, поле `queue`)."""
        return {
            "revision": self.revision,
            "approved": self.approved,
            "draft": self.draft,
            "signature": self.signature,
            "created_at": self.created_at,
            "author": self.author,
            "items": [item.to_dict() for item in self.items],
            "pointer": self.pointer,
            "last_check_id": self.last_check_id,
            "mode": self.mode,
            "pause_seconds": self.pause_seconds,
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Queue:
        """Восстанавливает очередь из JSON (терпимо к отсутствующим полям)."""
        payload = dict(data or {})
        mode = str(payload.get("mode") or MODE_CHECK)
        raw_items = payload.get("items")
        items = [
            QueueItem.from_dict(item)
            for item in (raw_items if isinstance(raw_items, list) else [])
            if isinstance(item, Mapping)
        ]
        return cls(
            revision=int(payload.get("revision") or 0),
            approved=bool(payload.get("approved")),
            draft=bool(payload.get("draft")),
            signature=str(payload.get("signature") or ""),
            created_at=str(payload.get("created_at") or now_iso()),
            author=str(payload.get("author") or ""),
            items=items,
            pointer=max(0, int(payload.get("pointer") or 0)),
            last_check_id=str(payload.get("last_check_id") or ""),
            mode=mode if mode in MODE_LABELS else MODE_CHECK,
            pause_seconds=clamp_pause(payload.get("pause_seconds")),
            stop_reason=str(payload.get("stop_reason") or ""),
        )


# ---------------------------------------------------------------------------
# Снимок программы и хранение очереди в сессии
# ---------------------------------------------------------------------------
def queue_signature(programme: Programme) -> str:
    """Отпечаток состава программы: по нему видно, что очередь снята с этой ревизии.

    Состав (и только он) определяет очередь, поэтому отпечаток считается по
    упорядоченным идентификаторам проверок: тот же состав даёт тот же отпечаток
    даже после выпуска новой ревизии программы.
    """
    text = "\n".join(programme.check_ids)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def build_queue(programme: Programme, *, author: str = "") -> Queue:
    """Строит очередь — снимок **текущей ревизии** программы (`FR-P-13`).

    Прогон по черновику программы разрешён, но очередь это помечает (`draft`):
    черновик виден и в очереди, и в протоколе (`docs/15` §11.2 п.12).
    """
    return Queue(
        revision=programme.revision,
        approved=programme.is_approved,
        draft=not programme.is_approved,
        signature=queue_signature(programme),
        author=str(author),
        items=[QueueItem(check_id=item.check_id, order=item.order) for item in programme.items],
    )


def load_queue(session: Any) -> Queue:
    """Очередь сессии: из поля `queue` (при отсутствии — пустая)."""
    return Queue.from_dict(getattr(session, "queue", None) or {})


def save_queue(
    session: Any,
    queue: Queue,
    *,
    event: str = "",
    message: str = "",
) -> Queue:
    """Сохраняет очередь в сессию (схема v7) и, при необходимости, отмечает событием."""
    session.queue = queue.to_dict()
    if event:
        summary = queue.summary()
        session.add_history(
            event,
            message or event,
            revision=queue.revision,
            status="черновик" if queue.draft else "утверждена",
            items=summary["total"],
            next=summary["next"],
        )
    return queue
