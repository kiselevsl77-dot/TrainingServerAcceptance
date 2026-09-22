"""Тесты глоссария терминологии (`acceptance/glossary.py`).

Пожелание заказчика (21.09.2026, п. 4): термины испытываемого сервера обязаны
отличаться от внутренних терминов пульта. Правило механическое и проверяемое:

    * сущности стенда — с префиксом ``$`` (``$Задача``, ``$Датасет``, ``$Модель``);
    * внутренние сущности пульта — без префикса (проверка, карточка запуска, запись);
    * «карточка», «программа» и «монитор» всегда уточняются (иначе теряется различие
      между карточкой проверки, карточкой запуска и карточкой $задачи).

Тест связывает три места, которые иначе разъезжаются: код глоссария, таблицу §1.4
руководства (`docs/12`) и подписи экранов (`acceptance/app.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acceptance import glossary

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
DOC_PATH = DOCS_DIR / "12. Руководство пользователя пульта испытаний (функционал и правила).md"
APP_PATH = Path(__file__).resolve().parents[2] / "acceptance" / "app.py"

TABLE_BEGIN = "### 1.4."
TABLE_END = "## 2."

#: Подписи экранов, которые запрещены: `$Задачи` — сущность стенда, префикс обязателен.
FORBIDDEN_NAV = ('"scr303_tasks": "Задачи"', '"scr303_tasks": ("⏱️ Задачи"')


@pytest.fixture(scope="module")
def document() -> str:
    """Текст руководства пользователя."""
    return DOC_PATH.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Убирает обратные кавычки и лишние пробелы (для сверки таблицы с кодом)."""
    return re.sub(r"\s+", " ", str(text).replace("`", "")).strip()


def _table_rows(document: str) -> list[tuple[str, str, str]]:
    """Строки таблицы §1.4: «термин → принадлежность → значение»."""
    fragment = document[
        document.index(TABLE_BEGIN) : document.index(TABLE_END, document.index(TABLE_BEGIN))
    ]
    rows: list[tuple[str, str, str]] = []
    for line in fragment.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3 or cells[1] not in (glossary.SCOPE_SERVER, glossary.SCOPE_PULT):
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def test_server_terms_are_prefixed() -> None:
    """Все сущности стенда названы с префиксом `$`."""
    assert glossary.server_terms(), "в глоссарии нет ни одного термина стенда"
    for item in glossary.server_terms():
        assert item.title.startswith(glossary.SERVER_PREFIX), item.title


def test_pult_terms_have_no_prefix() -> None:
    """Внутренние сущности пульта префикса не имеют."""
    assert glossary.pult_terms(), "в глоссарии нет ни одного термина пульта"
    for item in glossary.pult_terms():
        assert not item.title.startswith(glossary.SERVER_PREFIX), item.title


def test_terms_are_complete_and_unique() -> None:
    """У каждого термина есть значение, а подписи не повторяются."""
    titles = [item.title for item in glossary.terms()]
    assert len(titles) == len(set(titles))
    for item in glossary.terms():
        assert item.meaning.strip(), f"термин {item.key} без значения"
        assert item.scope in (glossary.SCOPE_SERVER, glossary.SCOPE_PULT)


def test_guide_table_matches_glossary(document: str) -> None:
    """Таблица §1.4 руководства посимвольно соответствует глоссарию (термин, принадлежность, значение)."""
    rows = _table_rows(document)
    expected = [
        (_normalize(item.title), item.scope, _normalize(item.meaning)) for item in glossary.terms()
    ]
    parsed = [(_normalize(title), scope, _normalize(meaning)) for title, scope, meaning in rows]

    assert len(rows) == len(glossary.terms()), "в таблице §1.4 не все термины"
    assert parsed == expected


def test_rules_disambiguate_card_program_and_monitor() -> None:
    """Правила глоссария прямо разводят «карточку», «программу» и «монитор»."""
    text = " ".join(glossary.RULES)

    assert glossary.SERVER_PREFIX in text
    for word in ("Карточка", "карточка", "Программа", "программа", "Монитор", "монитор"):
        assert word in text
    assert "карточка $задачи" in text
    assert "программа испытаний" in text
    assert "монитор обмена" in text


def test_application_navigation_uses_glossary() -> None:
    """Подписи экранов берутся из глоссария, а не набираются в `app.py` руками."""
    source = APP_PATH.read_text(encoding="utf-8")

    assert "glossary.nav_label(" in source, "app.py собирает подписи не из глоссария"
    assert "RENDERERS" in source, "реестр экранов должен собираться из acceptance/ui/screens"
    for forbidden in FORBIDDEN_NAV:
        assert forbidden not in source, f"устаревшая подпись экрана: {forbidden}"


def test_screens_of_server_entities_are_prefixed() -> None:
    """Экраны сущностей стенда названы с префиксом `$`, экраны пульта — без."""
    assert glossary.is_server_label(glossary.screen_label("scr303_tasks"))

    for key in ("scr101_sets", "scr301_run", "scr402_journal", "scr501_tools"):
        assert not glossary.is_server_label(glossary.screen_label(key)), key


def test_every_screen_has_icon_and_label() -> None:
    """У каждого экрана есть и заголовок, и пиктограмма (иначе навигация сломается)."""
    assert set(glossary.SCREEN_LABELS) == set(glossary.SCREEN_ICONS)
    for key in glossary.SCREEN_LABELS:
        label = glossary.nav_label(key)
        assert label.startswith(glossary.SCREEN_ICONS[key])
        assert glossary.SCREEN_LABELS[key] in label


def test_screen_keys_match_navigation_registry() -> None:
    """Глоссарий и реестр экранов описывают один и тот же набор экранов.

    Подписи экранов — единственный источник для меню и заголовков: расхождение глоссария и
    `acceptance/ui/nav.py` дало бы экран без подписи (или подпись без экрана).
    """
    from acceptance.ui import nav

    assert set(glossary.SCREEN_LABELS) == set(nav.screens())
