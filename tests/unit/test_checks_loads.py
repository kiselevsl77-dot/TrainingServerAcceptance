"""Тесты сценариев проверок модуля «Loads» (TC-LOAD-01…07, этап T3).

Стенд повторяет живой реестр нагрузок (замечание №2):

    * `result_size` и серверный срез `limit`/`offset` работают;
    * в элементе нет `phase_connection` — контракт 17.09.2026 убрал требование фазы,
      поэтому это больше не дефект (`TC-LOAD-02` фиксирует состав полей);
    * параметр `ph_n` из контракта убран: `TC-LOAD-03` фиксирует, игнорирует его
      сервер или отклоняет; `load_id`/`category` — точные и регистрозависимые,
      `description_search` — по подстроке;
    * маршрута удаления нагрузки нет (проба `TC-LOAD-07`).

Проверки `TC-LOAD-02/03` больше **не** «блокированы API»: закрытые контрактом дефекты
не порождают автоматических замечаний (FR-T7).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from acceptance import notes
from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import loads as check_loads
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.schemas import LoadItem
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]


def load_record(
    load_id: str, category: str, description: str = "", *, phase_connection: str | None = None
) -> dict[str, object]:
    """Элемент реестра нагрузок.

    `phase_connection` задаётся только для подмен, проверяющих поведение параметра `ph_n`:
    в контракте 17.09.2026 поля фазы нет, и в живом реестре его тоже нет.
    """
    record: dict[str, object] = {
        "load_id": load_id,
        "category": category,
        "description": description,
    }
    if phase_connection is not None:
        record["phase_connection"] = phase_connection
    return record


def default_loads() -> list[dict[str, object]]:
    """Реестр нагрузок: категории с регистровым дублем и служебная `__OTHER__`."""
    return [
        load_record("antminer_s19", "Antminer", "Стенд Antminer S19, фаза A"),
        load_record("tektronix_pws", "Tektronix", "Источник питания Tektronix"),
        load_record("__OTHER__", "__OTHER__", "Служебная нагрузка, категория не задана"),
        load_record("chip_1", "antminer", "Резервная нагрузка Antminer"),
    ]


def spec_of(check_id: str):
    """Описание проверки каталога с проверкой наличия."""
    spec = catalog.find(check_id)
    assert spec is not None
    return spec


class FakeLoads:
    """Подменённый сервер нагрузок: реестр, фильтры и отсутствие удаления."""

    def __init__(
        self,
        loads: list[dict[str, object]] | None = None,
        *,
        ignore_ph_n: bool = True,
        break_page: bool = False,
        delete_status: int = 404,
        empty_descriptions: bool = False,
    ) -> None:
        source = default_loads() if loads is None else loads
        self.loads = [dict(record) for record in source]
        if empty_descriptions:
            for record in self.loads:
                record["description"] = None
        self.ignore_ph_n = ignore_ph_n
        self.break_page = break_page
        self.delete_status = delete_status
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера."""
        path = request.url.path
        if path == "/api/loads/list":
            return self._list(dict(request.url.params))
        if request.method == "DELETE" and path.startswith("/api/loads/"):
            load_id = path.rsplit("/", 1)[-1]
            if self.delete_status == 204:
                self.deleted.append(load_id)
                return httpx.Response(204)
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(404, json={"detail": "Not Found"})

    def _list(self, query: dict[str, str]) -> httpx.Response:
        """Реестр с фильтрами: `ph_n` (убран из контракта) не влияет на выдачу."""
        rows = list(self.loads)

        def match(record: dict[str, object]) -> bool:
            if query.get("load_id") and str(record["load_id"]) != query["load_id"]:
                return False
            if query.get("category") and str(record["category"]) != query["category"]:
                return False
            if query.get("description_search"):
                needle = query["description_search"].lower()
                if needle not in str(record["description"] or "").lower():
                    return False
            if query.get("ph_n") and not self.ignore_ph_n:
                return str(record.get("phase_connection") or "") == query["ph_n"]
            return True

        rows = [record for record in rows if match(record)]
        total = len(rows)
        if query.get("limit"):
            limit = int(query["limit"])
            offset = int(query.get("offset") or 0)
            if self.break_page and limit == 1:
                limit = max(limit, 2)
            rows = rows[offset : offset + limit]
        # фаза нужна только фильтру внутри подмены: в живом ответе её нет (TC-LOAD-02)
        payload = [
            {key: value for key, value in record.items() if key != "phase_connection"}
            for record in rows
        ]
        return httpx.Response(
            200,
            json={
                "loads": payload,
                "result_size": total,
                "limit": query.get("limit"),
                "offset": query.get("offset"),
            },
        )


def stand(handler: Handler, check_id: str, *, session=None, **params):
    """Контекст сценария поверх подменённого транспорта (без реальной сети)."""
    journal = Journal(max_records=200)
    transport = httpx.MockTransport(handler)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    context = check_runner.AutomationContext(
        session=session or new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=Apis.build(client),
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )
    return context, journal


# ---------------------------------------------------------------------------
# TC-LOAD-01/02 — реестр и состав полей
# ---------------------------------------------------------------------------
def test_registry_passes_with_pagination():
    """Реестр получен: `result_size` совпадает, срез различает страницы (UC-06)."""
    fake = FakeLoads()
    context, journal = stand(fake.handler, "TC-LOAD-01")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["returned"] == 4
    assert outcome.evidence["offset_works"] is True
    assert journal.records[-1].path == "/api/loads/list"


def test_registry_fails_when_page_ignores_limit():
    """Страница `limit=1` вернула больше одной нагрузки — отказ."""
    fake = FakeLoads(break_page=True)
    context, _ = stand(fake.handler, "TC-LOAD-01")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "больше одной нагрузки" in outcome.verdict


def test_registry_skipped_on_empty_registry():
    """Пустой реестр нагрузок — «пропущена»: проверять нечего."""
    fake = FakeLoads(loads=[])
    context, _ = stand(fake.handler, "TC-LOAD-01")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "реестр нагрузок пуст" in outcome.verdict


def test_fields_reflect_contract_without_phase():
    """Контракт снял фазу: обязательные поля на месте, `phase_connection` не требуется."""
    fake = FakeLoads()
    session = new_session(base_url=BASE_URL)
    context, _ = stand(fake.handler, "TC-LOAD-02", session=session)

    result = check_runner.automate(context)

    assert result is not None and result.status == CheckStatus.PASSED
    assert "phase_connection" in result.verdict and "17.09.2026" in result.verdict
    assert result.evidence["phase_connection"] is False
    assert result.evidence["missing"] == []
    # дефект P0 закрыт контрактом: автозамечание по проверке больше не создаётся
    assert notes.note_from_check("TC-LOAD-02") is None


def test_fields_report_phase_when_present(monkeypatch: pytest.MonkeyPatch):
    """Если поле фазы вернулось в выдачу, проверка фиксирует это и не падает."""
    fields = {**LoadItem.model_fields, "phase_connection": LoadItem.model_fields["category"]}
    monkeypatch.setattr(check_loads.LoadItem, "model_fields", fields)
    fake = FakeLoads()
    context, _ = stand(fake.handler, "TC-LOAD-02")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "поле фазы присутствует" in outcome.verdict
    assert outcome.evidence["phase_connection"] is True


# ---------------------------------------------------------------------------
# TC-LOAD-03/04 — фильтры
# ---------------------------------------------------------------------------
def test_unknown_param_ignored_is_recorded():
    """`ph_n` нет в контракте: сервер игнорирует параметр — факт фиксируется, отказа нет."""
    fake = FakeLoads(ignore_ph_n=True)
    context, journal = stand(fake.handler, "TC-LOAD-03")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert "игнорируется" in outcome.verdict
    assert outcome.evidence["filtered_size"] == outcome.evidence["full_size"] == 4
    assert outcome.evidence["contract"] == "параметр `ph_n` в контракте 17.09.2026 отсутствует"
    assert journal.records[-1].query == "ph_n=Ph_A"
    assert journal.records[-1].label == "TC-LOAD-03"  # проба помечена меткой проверки


def test_unknown_param_filtered_is_recorded():
    """Если сервер снова фильтрует по `ph_n`, проверка фиксирует возврат параметра."""
    loads = [
        load_record("antminer_s19", "Antminer", "Стенд Antminer", phase_connection="Ph_B"),
        load_record("tektronix_pws", "Tektronix", "Источник", phase_connection="Ph_A"),
    ]
    fake = FakeLoads(loads, ignore_ph_n=False)
    context, _ = stand(fake.handler, "TC-LOAD-03")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["filtered_size"] == 1
    assert "вернулся в сборку" in outcome.verdict


def test_exact_filters_pass_and_record_case_sensitivity():
    """`load_id`/`category` фильтруются точно; регистрозависимость — факт в доказательствах."""
    fake = FakeLoads()
    context, _ = stand(fake.handler, "TC-LOAD-04")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["load_id_filter"] == 1
    assert outcome.evidence["category_filter"] == 1
    assert outcome.evidence["case_sensitive"]["load_id"].startswith("регистр влияет")


def test_exact_filters_fail_on_wrong_category():
    """Фильтр `category` вернул другую категорию — отказ."""
    fake = FakeLoads()

    def ignore_category(request: httpx.Request) -> httpx.Response:
        """Подменённый сервер: фильтр `category` игнорируется."""
        query = dict(request.url.params)
        query.pop("category", None)
        return fake._list(query)

    context, _ = stand(ignore_category, "TC-LOAD-04")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "другие категории" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-LOAD-05 — поиск по описанию
# ---------------------------------------------------------------------------
def test_description_search_uses_substring():
    """Поиск по описанию находит подстроку и не возвращает лишних нагрузок."""
    fake = FakeLoads()
    context, _ = stand(fake.handler, "TC-LOAD-05")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["token"] == "Стенд"
    assert outcome.evidence["found"] == 1
    assert outcome.evidence["of_total"] == 4


def test_description_search_fails_on_empty_answer():
    """Подстрока есть в реестре, но поиск пуст — отказ."""
    fake = FakeLoads()

    def ignore_search(request: httpx.Request) -> httpx.Response:
        """Подменённый сервер: поиск по описанию всегда возвращает пустую выдачу."""
        query = dict(request.url.params)
        if request.url.path == "/api/loads/list" and query.get("description_search"):
            return httpx.Response(200, json={"loads": [], "result_size": 0})
        return fake.handler(request)

    context, _ = stand(ignore_search, "TC-LOAD-05")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "пустую выдачу" in outcome.verdict


def test_description_search_skipped_without_descriptions():
    """Без описаний в реестре поиск проверить нельзя — «пропущена»."""
    fake = FakeLoads(empty_descriptions=True)
    context, _ = stand(fake.handler, "TC-LOAD-05")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет нагрузок с описанием" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-LOAD-06 — качество справочника категорий
# ---------------------------------------------------------------------------
def test_categories_report_case_duplicates_and_other():
    """Регистровые дубли категорий и служебная `__OTHER__` попадают в замечание."""
    fake = FakeLoads()
    context, _ = stand(fake.handler, "TC-LOAD-06")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["case_duplicates"] == [["Antminer", "antminer"]]
    assert outcome.evidence["other_category"] == ["__OTHER__"]
    assert "замечание о справочнике категорий" in outcome.verdict


def test_categories_clean_registry_has_no_facts():
    """Чистый справочник категорий — отдельный вердикт без замечаний."""
    loads = [
        load_record("antminer_s19", "Antminer", "Стенд"),
        load_record("tektronix_pws", "Tektronix", "Источник"),
    ]
    fake = FakeLoads(loads)
    context, _ = stand(fake.handler, "TC-LOAD-06")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["case_duplicates"] == []
    assert "дублей и служебных записей нет" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-LOAD-07 — отсутствие удаления нагрузки (негативная проба)
# ---------------------------------------------------------------------------
def test_missing_delete_passes_on_404():
    """Маршрута удаления нагрузки нет (404) — замечание к API P2, проверка пройдена."""
    fake = FakeLoads()
    context, journal = stand(fake.handler, "TC-LOAD-07")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["was_in_registry"] is True
    assert "замечание к API P2" in outcome.verdict
    assert journal.records[-1].method == "DELETE"
    assert journal.records[-1].path == "/api/loads/antminer_s19"
    assert journal.records[-1].label == "TC-LOAD-07"  # проба помечена меткой проверки


def test_missing_delete_fails_when_load_removed():
    """Если DELETE удалил нагрузку, проверка терпит отказ: маршрута быть не должно."""
    fake = FakeLoads(delete_status=204)
    context, _ = stand(fake.handler, "TC-LOAD-07", load_id="__OTHER__")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert fake.deleted == ["__OTHER__"]
    assert "проверьте реестр нагрузок" in outcome.verdict


def test_missing_delete_prefers_test_load():
    """Проба удаления по умолчанию выбирает `__TEST__`-нагрузку, если она есть."""
    loads = [
        load_record("antminer_s19", "Antminer", "Стенд"),
        load_record("__TEST__probe", "Antminer", "Тестовая нагрузка"),
    ]
    fake = FakeLoads(loads)
    context, _ = stand(fake.handler, "TC-LOAD-07")

    outcome = check_loads.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["load_id"] == "__TEST__probe"
