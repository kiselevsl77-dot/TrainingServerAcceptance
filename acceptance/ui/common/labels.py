"""Общие подписи интерфейса пульта.

`build_label` формирует краткую идентификацию сборки испытуемого сервера
(`branch@revision`), которая используется в шапке сессии, на экране «Стенд»
и в отчёте об испытаниях.
"""

from __future__ import annotations

from typing import Any


def build_label(version: Any) -> str:
    """Подпись сборки сервера из ответа `GET /version` (или «не определена»)."""
    if not isinstance(version, dict):
        return "не определена"

    revision = str(version.get("revision") or version.get("commit") or "")[:12]
    branch = str(version.get("branch") or "")
    if revision and branch:
        return f"{branch}@{revision}"
    return revision or branch or "не определена"
