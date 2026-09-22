"""Стоп-тесты целевого комплекта: ТЗ (`docs/15`), макет (`docs/16`), прототип (`mockup/`).

Комплект из трёх частей обязан быть согласованным, иначе «сверка кода с ТЗ» становится
бессмысленной: перечень экранов, идентификаторы требований и ссылки должны совпадать
механически. Проверяется:

    * непрерывность нумерации требований (`FR-P`, `DR-P`, `IR-P`, `AR-P`, `NFR-P`, `AC-P`) —
      §1.5 ТЗ требует непрерывную нумерацию: удаление ID запрещено, новое требование
      добавляется в конец;
    * совпадение перечня экранов `SCR-01…SCR-13` в трёх местах: таблица §6.1 ТЗ, разделы
      макета и реестр экранов прототипа (`mockup/app.js`);
    * наличие требований у каждого экрана макета (трассировка не разорвана);
    * офлайн-пригодность прототипа: без внешних ресурсов, сборки и сетевых вызовов;
    * регистрация документов в перечнях (`docs/README.md`, `README.md`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
TZ_PATH = DOCS_DIR / "15. ТЗ на Пульт испытаний (целевое, по концепции).md"
MOCKUP_DOC_PATH = DOCS_DIR / "16. Макет интерфейса ПУЛЬТА (целевые экраны).md"
MOCKUP_DIR = ROOT / "mockup"

APP_JS = MOCKUP_DIR / "app.js"
INDEX_HTML = MOCKUP_DIR / "index.html"
DEMO_JS = MOCKUP_DIR / "demo-data.js"
STYLES_CSS = MOCKUP_DIR / "styles.css"

SCREEN_ID = re.compile(r"SCR-\d{2}")
REQUIREMENT_ID = re.compile(r"\b(FR-P|DR-P|IR-P|AR-P|NFR-P|AC-P)-(\d+)\b")
SCREEN_HEADING = re.compile(r"^## (SCR-\d{2})\.", re.MULTILINE)
SCREEN_REGISTRY = re.compile(r'id:\s*"(SCR-\d{2})"')
SCREEN_SECTION = re.compile(r"^## (SCR-\d{2})\..*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)

#: Ожидаемый состав требований: префикс → последний номер (нумерация 1..N без пропусков).
EXPECTED_TOTALS: dict[str, int] = {
    "FR-P": 70,
    "DR-P": 14,
    "IR-P": 21,
    "AR-P": 10,
    "NFR-P": 12,
    "AC-P": 27,
}

#: Экраны целевого макета: планирование (`SCR-14`, `SCR-15`) + экраны `SCR-01`…`SCR-13`.
SCREEN_TOTAL = 15

#: Экраны раздела «Планирование испытаний»: команд запуска в них быть не должно (`IR-P-18`).
PLANNING_SCREENS = ("SCR-14", "SCR-15")
PLANNING_RENDERERS = ("renderSets", "renderProgramme")

#: Локальные ресурсы прототипа: всё остальное в разметке считается внешним.
LOCAL_ASSETS = ("styles.css", "app.js", "demo-data.js")

#: Признаки внешних зависимостей и сборки, которых в прототипе быть не должно.
FORBIDDEN_IN_PROTOTYPE = ("cdn.", "unpkg", "jsdelivr", "node_modules", "fetch(", "import(")

README_PATHS = (DOCS_DIR / "README.md", ROOT / "README.md")


@pytest.fixture(scope="module")
def tz() -> str:
    """Текст целевого ТЗ."""
    assert TZ_PATH.exists(), f"нет ТЗ: {TZ_PATH.as_posix()}"
    return TZ_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mockup_doc() -> str:
    """Текст макета целевых экранов."""
    assert MOCKUP_DOC_PATH.exists(), f"нет макета: {MOCKUP_DOC_PATH.as_posix()}"
    return MOCKUP_DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    """Исходник прототипа (реестр экранов и отрисовка)."""
    assert APP_JS.exists(), f"нет прототипа: {APP_JS.as_posix()}"
    return APP_JS.read_text(encoding="utf-8")


def _numbers(text: str, prefix: str) -> list[int]:
    """Номера требований указанного префикса в порядке появления."""
    return [
        int(match.group(2)) for match in REQUIREMENT_ID.finditer(text) if match.group(1) == prefix
    ]


@pytest.mark.parametrize("prefix", sorted(EXPECTED_TOTALS))
def test_requirement_numbers_are_continuous(tz: str, prefix: str) -> None:
    """Нумерация требований непрерывна: 1..N без пропусков и выбросов (§1.5 ТЗ)."""
    numbers = sorted(set(_numbers(tz, prefix)))

    assert numbers, f"в ТЗ нет требований {prefix}"
    assert numbers == list(range(1, EXPECTED_TOTALS[prefix] + 1)), (
        f"{prefix}: нумерация разорвана — {numbers}"
    )


def test_requirement_groups_are_documented(tz: str) -> None:
    """Каждая группа требований объявлена своим разделом ТЗ."""
    for title in (
        "## 4. Функциональные требования",
        "## 5. Требования к данным",
        "## 6. Требования к интерфейсу",
        "## 7. Требования к интеграции",
        "## 8. Нефункциональные требования",
        "## 9. Критерии приёмки",
    ):
        assert title in tz, f"в ТЗ нет раздела «{title}»"


def test_screen_ids_match_in_all_three_parts(tz: str, mockup_doc: str, app_js: str) -> None:
    """Экраны `SCR-01…SCR-15` совпадают в ТЗ (§6.1), макете и прототипе."""
    from_tz = sorted(set(SCREEN_ID.findall(tz)))
    from_doc = sorted(set(SCREEN_ID.findall(mockup_doc)))
    from_js = sorted(set(SCREEN_REGISTRY.findall(app_js)))

    assert from_doc == from_js, "макет и прототип расходятся в перечне экранов"
    assert from_doc == from_tz, "ТЗ и макет расходятся в перечне экранов"
    assert from_doc == [f"SCR-{number:02d}" for number in range(1, SCREEN_TOTAL + 1)]


def test_every_screen_is_described_in_mockup(mockup_doc: str) -> None:
    """Каждый экран макета описан разделом и указывает требования ТЗ."""
    headings = SCREEN_HEADING.findall(mockup_doc)
    sections = SCREEN_SECTION.findall(mockup_doc)

    assert len(headings) == len(sections) == SCREEN_TOTAL, (
        f"в макете не {SCREEN_TOTAL} описаний экранов"
    )
    for match in SCREEN_SECTION.finditer(mockup_doc):
        screen_id, body = match.group(1), match.group(0)
        assert REQUIREMENT_ID.search(body), f"{screen_id} не ссылается на требования ТЗ"
        assert "**Назначение.**" in body, f"{screen_id} без описания назначения"
        assert "**Состояния.**" in body, f"{screen_id} без описания состояний"


def test_mockup_requirements_exist_in_tz(tz: str, mockup_doc: str) -> None:
    """Требования, на которые ссылается макет, действительно есть в ТЗ."""
    defined = {(match.group(1), int(match.group(2))) for match in REQUIREMENT_ID.finditer(tz)}

    for match in REQUIREMENT_ID.finditer(mockup_doc):
        key = (match.group(1), int(match.group(2)))
        assert key in defined, f"макет ссылается на отсутствующее требование {match.group(0)}"


def test_screens_are_registered_once_in_prototype(app_js: str) -> None:
    """В реестре прототипа каждый экран зарегистрирован ровно один раз."""
    ids = SCREEN_REGISTRY.findall(app_js)

    assert len(ids) == SCREEN_TOTAL, f"в реестре прототипа {len(ids)} экранов"
    assert len(ids) == len(set(ids)), "экран зарегистрирован дважды"


def _js_function(app_js: str, name: str) -> str:
    """Тело функции прототипа: от объявления до следующего объявления верхнего уровня."""
    start = app_js.index(f"function {name}(")
    tail = app_js[start:]
    end = tail.find("\n  function ")
    return tail if end < 0 else tail[:end]


def test_planning_screens_have_no_launch_command(mockup_doc: str, app_js: str) -> None:
    """Правило «планирование отделено от исполнения» (`IR-P-18`): в планировании нет запуска."""
    for screen_id, body in (
        (match.group(1), match.group(0)) for match in SCREEN_SECTION.finditer(mockup_doc)
    ):
        if screen_id not in PLANNING_SCREENS:
            continue
        assert "Ни одной команды запуска" in body or "нет команд запуска" in body, (
            f"{screen_id}: в макете не зафиксирован запрет команд запуска"
        )
        assert "[ ▶ Следующая" not in body, f"{screen_id}: команда запуска попала в планирование"

    for name in PLANNING_RENDERERS:
        body = _js_function(app_js, name)
        for marker in ("run-next", "run-batch", "runItem("):
            assert marker not in body, f"прототип: {name} содержит команду запуска ({marker})"


def test_prototype_has_single_launch_command(app_js: str) -> None:
    """В прототипе ровно одна команда запуска — «Следующая» на `SCR-05` (`IR-P-4`)."""
    assert app_js.count('data-act="run-next"') == 1, "команд запуска больше одной"
    assert "▶ Следующая" in app_js, "команда запуска не названа «Следующая»"


def test_tz_defines_single_launch_command(tz: str, mockup_doc: str) -> None:
    """ТЗ и макет фиксируют одну команду запуска; «кнопка Пуск» выведена из требований."""
    assert "одна команда запуска" in tz, "ТЗ не фиксирует одну команду запуска"
    assert "«Следующая»" in tz, "ТЗ не называет команду запуска"
    assert "кнопка «Пуск» существует только" not in tz, "в ТЗ осталась старая формулировка `IR-P-4`"
    assert "[ ▶ Пуск ]" not in mockup_doc, "в макете осталась кнопка «Пуск»"


def test_task_roles_are_fixed(tz: str) -> None:
    """Роли целевого процесса: руководитель испытаний (планирование) и инженер-испытатель."""
    assert "**Руководитель испытаний**" in tz
    assert "**Инженер-испытатель**" in tz
    assert "оператор-испытатель" not in tz.lower(), (
        "в ТЗ осталась старая роль «оператор-испытатель»"
    )


def test_navigation_has_five_groups_with_planning_first(
    tz: str, mockup_doc: str, app_js: str
) -> None:
    """Навигация целевого пульта: пять групп, первая — «Планирование испытаний» (`IR-P-1`)."""
    assert "пять групп" in tz
    for text in (mockup_doc, app_js):
        assert "ПЛАНИРОВАНИЕ ИСПЫТАНИЙ" in text or "Планирование испытаний" in text
    planning = app_js.index('"Планирование испытаний"')
    preparation = app_js.index('"Подготовка"')
    assert planning < preparation, "группа «Планирование испытаний» должна идти первой"


def test_prototype_files_exist() -> None:
    """Прототип собран из ожидаемых файлов (каркас, стили, данные, логика, описание)."""
    for path in (INDEX_HTML, STYLES_CSS, APP_JS, DEMO_JS, MOCKUP_DIR / "README.md"):
        assert path.exists(), f"нет файла прототипа: {path.as_posix()}"


def test_prototype_is_offline() -> None:
    """Прототип работает офлайн: локальные файлы, без внешних ресурсов и сборки."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    for marker in ('src="http', "src='http", 'href="http', "href='http"):
        assert marker not in html, f"внешний ресурс в index.html: {marker}"
    for marker in LOCAL_ASSETS:
        assert marker in html, f"index.html не подключает {marker}"

    for path in (APP_JS, DEMO_JS, STYLES_CSS):
        text = path.read_text(encoding="utf-8").lower()
        for marker in FORBIDDEN_IN_PROTOTYPE:
            assert marker.lower() not in text, f"{path.name}: запрещённая зависимость {marker}"


def test_documents_are_registered() -> None:
    """ТЗ, макет и прототип внесены в перечень документов и в README проекта."""
    for path in README_PATHS:
        text = path.read_text(encoding="utf-8")
        assert TZ_PATH.name in text, f"{path.name} не ссылается на ТЗ (docs/15)"
        assert MOCKUP_DOC_PATH.name in text, f"{path.name} не ссылается на макет (docs/16)"
        assert "mockup/" in text, f"{path.name} не ссылается на прототип"
