"""Карточка запуска ресурсоёмкой операции (FR-T10, NFR-T4).

Ресурсоёмкие операции (`train`, `check`, async-инференс, `fill` больших датасетов)
и любые изменяющие действия на общем стенде выполняются только осознанно, поэтому
перед запуском пульт требует заполнить карточку: цель проверки, используемые данные,
параметры, ответственного, ожидаемую длительность и решение о судьбе созданных
артефактов, а также подтверждение расхода ресурсов.

Карточка вынесена в общий модуль: её используют консоль запросов (этап T2) и
чек-лист проверок (этапы T4/T5/T7), поэтому требования к подтверждению одинаковы.
Значения карточки попадают в сессию (`console_calls`/`checks`) и в отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import streamlit as st

from acceptance.endpoints import EndpointSpec

#: Решения о судьбе созданных артефактов после проверки.
ARTIFACT_POLICIES = (
    "удалить после проверки",
    "оставить как доказательство",
    "решение принимает испытатель",
)

DEFAULT_ARTIFACT_POLICY = ARTIFACT_POLICIES[0]

#: Подсказки полей карточки (общие для консоли и чек-листа).
FIELD_HINTS: dict[str, str] = {
    "goal": "что именно проверяем и какой вывод должен быть сделан",
    "data": "идентификаторы датасета/модели/записей и их имена",
    "params": "ключевые параметры запуска (эпохи, порог, батч и т.п.)",
    "responsible": "ФИО и должность запускающего",
    "expected": "ожидаемая длительность и ресурсы (по опыту или оценке)",
    "artifacts": "что делаем с созданными задачами/моделями/файлами",
    "comment": "дополнительные условия и ограничения",
}


@dataclass
class RunCard:
    """Значения карточки запуска ресурсоёмкой операции."""

    goal: str = ""
    data: str = ""
    params: str = ""
    responsible: str = ""
    expected: str = ""
    artifacts: str = DEFAULT_ARTIFACT_POLICY
    comment: str = ""
    confirmed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def missing(self) -> tuple[str, ...]:
        """Обязательные поля, которые оператор не заполнил."""
        required = {
            "цель проверки": self.goal,
            "используемые данные": self.data,
            "ответственный": self.responsible,
        }
        return tuple(name for name, value in required.items() if not value.strip())

    @property
    def is_ready(self) -> bool:
        """True, если карточка заполнена и подтверждена — можно запускать."""
        return not self.missing and self.confirmed

    def to_dict(self) -> dict[str, Any]:
        """Представление карточки для сессии и отчёта."""
        return {
            "goal": self.goal.strip(),
            "data": self.data.strip(),
            "params": self.params.strip(),
            "responsible": self.responsible.strip(),
            "expected": self.expected.strip(),
            "artifacts": self.artifacts,
            "comment": self.comment.strip(),
            "confirmed": self.confirmed,
            **self.extra,
        }


def render_run_card(
    spec: EndpointSpec,
    *,
    key_prefix: str,
    default_responsible: str = "",
    default_params: str = "",
) -> RunCard:
    """Отрисовывает карточку запуска ресурсоёмкой операции и возвращает её значения."""
    st.warning(
        f"🟣 Ресурсоёмкая операция `{spec.title}`: запуск только после заполнения карточки "
        "и подтверждения расхода ресурсов (FR-T10, NFR-T4)."
    )
    card = RunCard(
        params=default_params,
        responsible=default_responsible,
        extra={"endpoint": spec.key, "endpoint_note": spec.note},
    )

    col_left, col_right = st.columns(2)
    card.goal = col_left.text_input(
        "Цель проверки *",
        key=f"{key_prefix}_goal",
        placeholder=FIELD_HINTS["goal"],
    )
    card.data = col_right.text_input(
        "Используемые данные *",
        key=f"{key_prefix}_data",
        placeholder=FIELD_HINTS["data"],
    )
    card.params = st.text_area(
        "Параметры запуска",
        value=card.params,
        key=f"{key_prefix}_params",
        height=80,
        placeholder=FIELD_HINTS["params"],
    )
    col_resp, col_expect = st.columns(2)
    card.responsible = col_resp.text_input(
        "Ответственный *",
        value=card.responsible,
        key=f"{key_prefix}_responsible",
        placeholder=FIELD_HINTS["responsible"],
    )
    card.expected = col_expect.text_input(
        "Ожидаемая длительность/ресурсы",
        key=f"{key_prefix}_expected",
        placeholder=FIELD_HINTS["expected"],
    )
    card.artifacts = st.selectbox(
        "Судьба созданных артефактов",
        ARTIFACT_POLICIES,
        index=ARTIFACT_POLICIES.index(DEFAULT_ARTIFACT_POLICY),
        key=f"{key_prefix}_artifacts",
        help=FIELD_HINTS["artifacts"],
    )
    card.comment = st.text_input(
        "Комментарий к запуску",
        key=f"{key_prefix}_comment",
        placeholder=FIELD_HINTS["comment"],
    )
    card.confirmed = st.checkbox(
        "Подтверждаю запуск ресурсоёмкой операции и расход ресурсов стенда",
        key=f"{key_prefix}_confirm",
    )

    if card.missing:
        st.caption("Для запуска заполните: " + ", ".join(card.missing) + ".")
    return card
