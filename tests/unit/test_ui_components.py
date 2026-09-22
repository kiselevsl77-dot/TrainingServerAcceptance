"""Тесты компонентов интерфейса: подписи, отбор записей ленты, карточка запуска.

Компоненты (`acceptance/ui/components/**`) держат логику отбора и форматирования в
чистых функциях именно для того, чтобы её можно было проверить без Streamlit. Здесь
проверяется то, на что опираются экраны:

* `layout` — подпись шапки (код, группа, фаза), склейка подписей, склонения, готовность;
* `status` — маркеры очереди, состояния пункта, статусы проверок и происхождение значения;
* `journal` — отбор записей (метка проверки, «только ошибки», поиск, лимит, свежие сверху),
  строка записи и половины «запрос»/«ответ»;
* `run_card` — обязательные поля карточки запуска и запись подтверждения в доказательства;
* `flash` — вид сообщения и защита от опечатки в виде.

Стенд не нужен: записи журнала строятся напрямую (`HttpExchange`), а рисующие
функции компонентов проверяются смоук-тестами экранов.
"""

from __future__ import annotations

import pytest

from acceptance.checks.registry import CheckClass, CheckStatus
from acceptance.http_log import HttpExchange
from acceptance.programme import ProgrammeItem
from acceptance.queue import MARKER_LAST, MARKER_NEXT, MARKER_RUNNING, QUEUE_OFF, QUEUE_RUNNING
from acceptance.results import ORIGIN_MANUAL, ORIGIN_RUN
from acceptance.ui import nav
from acceptance.ui.components import flash, journal, layout, run_card, status


def _record(
    seq: int,
    *,
    label: str = "TC-SYS-01",
    method: str = "GET",
    path: str = "/health",
    status_code: int | None = 200,
    duration_ms: float = 12.0,
    error: str | None = None,
    request_body: str | None = None,
    response_body: str | None = '{"status":"ok"}',
    request_bytes: int | None = 0,
    response_bytes: int | None = 13,
    headers: tuple[tuple[str, str], ...] = (("content-type", "application/json"),),
) -> HttpExchange:
    """Запись журнала обмена для проверок ленты."""
    return HttpExchange(
        seq=seq,
        started_at="2026-09-22T10:11:12",
        method=method,
        path=path,
        query="",
        status=status_code,
        duration_ms=duration_ms,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        content_type="application/json",
        error=error,
        label=label,
        request_body=request_body,
        response_body=response_body,
        request_headers=headers,
        response_headers=headers,
    )


def _item(check_id: str = "TC-FILE-13", check_class: CheckClass = CheckClass.TECH) -> ProgrammeItem:
    """Пункт программы для карточки запуска."""
    return ProgrammeItem(
        check_id=check_id, order=1, title="Round-trip", check_class=str(check_class)
    )


# ---------------------------------------------------------------------------
# layout: шапка, склейка, склонения
# ---------------------------------------------------------------------------
def test_header_caption_names_code_group_and_phase():
    """Шапка экрана называет код, группу и фазу — по ней экран сличается с макетом."""
    spec = nav.by_key("scr301_run")

    assert layout.header_caption(spec) == "SCR-301 · группа «Испытания» · фаза Ф1–Ф2"
    assert layout.header_caption(spec, "ревизия 2").endswith("· ревизия 2")


def test_join_parts_skips_empty_parts():
    """Пустые части подписи не оставляют висящих разделителей."""
    assert layout.join_parts("a", "", None, "b") == "a · b"
    assert layout.join_parts("", "") == ""


@pytest.mark.parametrize(
    ("number", "expected"),
    [(1, "1 пункт"), (2, "2 пункта"), (5, "5 пунктов"), (11, "11 пунктов"), (22, "22 пункта")],
)
def test_plural_uses_russian_cases(number: int, expected: str):
    """Число пунктов показывается по-русски: 1 пункт, 3 пункта, 11 пунктов."""
    assert layout.plural(number, ("пункт", "пункта", "пунктов")) == expected


def test_percent_counts_share_of_done():
    """Готовность показывается и числами, и процентами; без пунктов — честный текст."""
    assert layout.percent(34, 41) == "34 из 41 · 83 %"
    assert layout.percent(0, 0) == "нет пунктов"


# ---------------------------------------------------------------------------
# status: маркеры очереди, состояния и статусы
# ---------------------------------------------------------------------------
def test_marker_text_distinguishes_queue_markers():
    """Три маркера очереди различимы: «сейчас», «следующая», «последняя» (`IR-P-3`)."""
    assert status.marker_text(MARKER_RUNNING) == "▶ сейчас"
    assert status.marker_text(MARKER_NEXT) == "▷ следующая"
    assert status.marker_text(MARKER_LAST) == "🕘 последняя"
    assert status.marker_text("") == ""
    assert status.marker_text("неизвестный маркер") == ""


def test_queue_state_labels_and_icons_come_from_core():
    """Состояние пункта очереди и его пиктограмма берутся из ядра, а не переписываются."""
    assert status.queue_state_label(QUEUE_RUNNING) == "выполняется"
    assert status.queue_state_label("что-то своё") == "ожидает"
    assert status.queue_state_icon(QUEUE_OFF) == "⏭"


def test_status_icon_matches_checks_registry():
    """Статус проверки показывается той же пиктограммой, что и в ядре."""
    assert status.status_icon(str(CheckStatus.PASSED)) == "✅"
    assert status.status_icon(str(CheckStatus.FAILED)) == "❌"
    assert status.status_icon("нет такого статуса") == "⚪"
    assert status.status_labels()[0] == str(CheckStatus.FAILED), "в фильтре первыми идут отказы"


def test_origin_label_names_the_source():
    """Рядом со значением видно, кто его дал (`IR-P-7`): пульт или оператор."""
    assert status.origin_label(ORIGIN_RUN) == "пульт"
    assert status.origin_label(ORIGIN_MANUAL) == "оператор"
    assert status.origin_label(ORIGIN_RUN, suspended=True) == "оператор"


def test_class_helpers_follow_registry_rules():
    """Класс проверки определяет требования к запуску: подтверждение и ручная отметка."""
    assert status.is_confirmable(CheckClass.LIVE) is True
    assert status.is_confirmable(CheckClass.HEAVY) is True
    assert status.is_confirmable(CheckClass.TECH) is False
    assert status.is_manual(CheckClass.MANUAL) is True
    assert status.class_label(CheckClass.MANUAL) != "manual", "класс показывается словами"


def test_result_text_joins_status_and_verdict():
    """Строка результата для таблицы: пиктограмма, статус и вердикт."""
    row = {"status": str(CheckStatus.FAILED), "verdict": "405 вместо 404"}

    assert status.result_text(row) == "❌ отказ · 405 вместо 404"
    assert status.result_text({"status": str(CheckStatus.PASSED)}) == "✅ успех"


# ---------------------------------------------------------------------------
# journal: отбор и разбор записей ленты
# ---------------------------------------------------------------------------
def test_feed_is_newest_first_and_limited():
    """Лента показывает свежие обмены сверху и не длиннее выбранного лимита."""
    records = [_record(1), _record(2), _record(3)]

    assert [item.seq for item in journal.select(records, limit=2)] == [3, 2]
    assert [item.seq for item in journal.select(records, limit=0)] == []


def test_feed_filters_by_label_errors_and_needle():
    """Отбор ленты: по метке проверки (`SCR-302`), «только ошибки» и по подстроке."""
    records = [
        _record(1, label="TC-SYS-01"),
        _record(2, label="TC-FILE-13", path="/api/data/file", method="POST", status_code=201),
        _record(3, label="TC-FILE-13", path="/api/data/file/b41c", status_code=500),
    ]

    assert [item.seq for item in journal.select(records, label="TC-FILE-13")] == [3, 2]
    assert [item.seq for item in journal.select(records, only_errors=True)] == [3]
    assert [item.seq for item in journal.select(records, needle="b41c")] == [3]
    assert [item.seq for item in journal.select(records, needle="post")] == [2]


def test_feed_caption_names_check_call_and_result():
    """Строка ленты: номер обмена, метка проверки, вызов и итог с длительностью."""
    caption = journal.feed_caption(
        _record(214, label="TC-FILE-13", method="POST", status_code=201, duration_ms=412.4)
    )

    assert caption.startswith("#214 · TC-FILE-13 · POST /health · → 201")
    assert "412 мс" in caption
    assert "application/json" in caption


def test_feed_caption_reports_connection_error():
    """Ошибка соединения видна в строке обмена вместо статуса."""
    record = _record(7, status_code=None, error="ConnectError: timeout")

    assert "нет ответа" not in journal.feed_caption(record)
    assert "ConnectError: timeout" in journal.feed_caption(record)
    assert record.is_error is True


def test_feed_caption_without_label_says_so():
    """Обмен вне прогона (консоль, подготовка) честно помечается «без проверки»."""
    assert "без проверки" in journal.feed_caption(_record(1, label=""))


def test_request_and_response_halves_are_separate():
    """Запрос и ответ показываются раздельно (`DR-P-4`): тела не смешиваются."""
    record = _record(
        5,
        method="POST",
        path="/api/data/file",
        request_body='{"file_name":"__TEST__.csv"}',
        response_body='{"id":"b41c"}',
    )

    request = "\n".join(journal.request_lines(record, level="тело"))
    response = "\n".join(journal.response_lines(record, level="тело"))

    assert '{"file_name":"__TEST__.csv"}' in request
    assert '{"file_name":"__TEST__.csv"}' not in response
    assert '{"id":"b41c"}' in response
    assert '{"id":"b41c"}' not in request
    assert "content-type" in request, "на уровне «тело» видны заголовки"


def test_request_lines_short_level_has_one_line():
    """Уровень «кратко» оставляет только строку вызова — для плотной ленты."""
    record = _record(5, method="POST", path="/api/data/file", request_body="{}")

    assert journal.request_lines(record, level="кратко") == ["POST /api/data/file"]


def test_error_share_and_summary_items_count_errors():
    """Сводка ленты считает ошибки и объёмы — по ней заполняется панель контекста."""
    records = [_record(1), _record(2, status_code=500), _record(3, status_code=None, error="boom")]

    assert journal.error_share(records) == "ошибок 2 из 3"
    assert journal.error_share([]) == ""

    summary = journal.summary_items(records)

    assert summary["total"] == 3
    assert summary["errors"] == 2
    assert summary["response_bytes"] == 39
    assert journal.summary_items([])["slowest_ms"] == 0.0


def test_rows_and_index_are_ready_for_tables():
    """Записи превращаются в строки таблицы журнала и в индекс по номеру обмена."""
    records = [_record(1, path="/health"), _record(2, path="/version", status_code=None)]

    rows = journal.as_rows(records)
    index = journal.mapping_of(records)

    assert rows[0]["path"] == "/health"
    assert rows[1]["status"] == "", "нет ответа — пустое значение, а не выдуманный статус"
    assert set(index) == {1, 2}
    assert index[2].path == "/version"


# ---------------------------------------------------------------------------
# run_card: карточка запуска изменяющих проверок (`FR-P-19`)
# ---------------------------------------------------------------------------
def test_run_card_defaults_prefill_goal_and_data():
    """Карточка подставляет цель и данные: оператор правит, а не набирает с нуля."""
    values = run_card.defaults(_item(), session_id="20260922-2f6a", author="Петров П.П.")

    assert values["goal"].startswith("TC-FILE-13")
    assert "20260922-2f6a" in values["data"]
    assert values["responsible"] == "Петров П.П."
    assert values["confirm"] is False


def test_run_card_requires_fields_and_confirmation():
    """Без цели, данных, ответственного и подтверждения расхода запуск не подтверждается."""
    values = run_card.defaults(_item())

    assert run_card.validate(values) == ["Ответственный", "Подтверждаю расход ресурсов"]

    values["responsible"] = "Петров П.П."
    values["confirm"] = True

    assert run_card.validate(values) == []


def test_run_card_evidence_records_operator_decision():
    """Подтверждение попадает в доказательства результата: видно, на каких данных получен."""
    values = run_card.defaults(_item(), session_id="20260922-2f6a", author="Петров П.П.")
    values["confirm"] = True

    evidence = run_card.evidence(values, author="Петров П.П.", at="2026-09-22T12:03:00")

    assert evidence["filled"] is True
    assert evidence["missing"] == []
    assert evidence["author"] == "Петров П.П."
    assert evidence["at"] == "2026-09-22T12:03:00"
    assert evidence["artifacts"] == run_card.ARTIFACT_ACTIONS[0]


def test_run_card_evidence_marks_unfilled_fields():
    """Незаполненная карточка честно помечается: доказательство не выдаёт себя за полное."""
    evidence = run_card.evidence(run_card.defaults(_item(), session_id="s"), author="")

    assert evidence["filled"] is False
    assert run_card.FIELD_LABELS["responsible"] in evidence["missing"]


# ---------------------------------------------------------------------------
# flash: сообщения экрана
# ---------------------------------------------------------------------------
def test_flash_message_keeps_kind_and_text():
    """Сообщение хранит вид и текст — их показывает `render` после перерисовки."""
    assert flash.message("success", "набор сохранён") == {
        "kind": "success",
        "text": "набор сохранён",
    }
    assert set(flash.KINDS) == {"success", "warning", "error", "info"}


def test_flash_rejects_unknown_kind():
    """Опечатка в виде сообщения ловится сразу, а не превращается в «подсказку»."""
    with pytest.raises(ValueError):
        flash.message("ok", "текст")
