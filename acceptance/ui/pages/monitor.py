"""Экран «Монитор обмена» (FR-T6 + FR-T3): лента вызовов API и ответов.

Требование заказчика (21.09.2026, п. 1–3): приёмной комиссии нужна прозрачность —
видеть **по раздельности** каждую отправленную команду и полученный ответ, уметь
повторить вызов с правками, скопировать запрос для `curl` и видеть, какие команды
будут следующими по программе испытаний.

Экран состоит из двух частей (они видны одновременно, потому что «какие команды
следующие» и «что реально ушло» решаются вместе):

    * слева — «Программа испытаний»: список запланированных вызовов с галочками,
      кнопками «Выполнить» (по одной) и «Авто с паузой» (см. `acceptance.plan`);
    * справа — «Монитор обмена»: вертикальная лента обменов с независимой
      подробностью запроса и ответа, копированием `curl` и повтором с правками
      (`acceptance/ui/common/exchange_monitor.py`).

Полная таблица обменов с фильтрами и выгрузками осталась на экране «Журнал»:
монитор — про ход испытаний «здесь и сейчас», журнал — про разбор после прогона.
"""

from __future__ import annotations

import streamlit as st

from acceptance import glossary
from acceptance import plan as plan_api
from acceptance.ui import state
from acceptance.ui.common import exchange_monitor, plan_panel
from acceptance.ui.common.flash import render_flash

KEY_PREFIX = "monitor"


def render() -> None:
    """Отрисовывает экран «Монитор обмена» (лента вызовов и программа испытаний)."""
    st.title(glossary.screen_label("monitor"))
    st.caption(
        "Прозрачность испытаний: лента отправленных вызовов и полученных ответов "
        "(подробность запроса и ответа выбирается по отдельности), повтор с правками, "
        "копирование запроса для `curl`. Рядом — программа испытаний: что будет вызвано "
        "следующим и чем закончился каждый вызов."
    )
    st.caption(glossary.PREFIX_HINT)
    render_flash()

    runtime = state.get_runtime()
    if runtime is None:
        st.error("Адрес испытуемого сервера не задан (`TRAINING_SERVER_BASE_URL` в `.env`).")
        return

    session = state.current_session()
    if session is None:
        st.warning(
            "Сессия испытаний не выбрана: обмены видны, но результаты программы "
            "испытаний не попадут в отчёт."
        )

    _render_summary(runtime)
    labels = sorted({record.label for record in runtime.journal.records if record.label})
    settings = exchange_monitor.render_controls(key_prefix=KEY_PREFIX, labels=labels)
    st.divider()

    col_plan, col_feed = st.columns([2, 3])
    with col_plan:
        plan = plan_panel.render(session, runtime)
    with col_feed:
        st.subheader("Лента обмена")
        exchange_monitor.render_feed(
            runtime.journal.records,
            settings=settings,
            session=session,
            base_url=runtime.settings.base_url,
            key_prefix=KEY_PREFIX,
            verdicts=plan_api.verdicts_for_feed(plan, runtime.journal.records),
        )


def _render_summary(runtime: state.Runtime) -> None:
    """Сводка по журналу текущего запуска: сколько вызовов, ошибок и объёмов."""
    summary = runtime.journal.summary()
    col_total, col_errors, col_slow, col_out, col_in = st.columns(5)
    col_total.metric("Обменов", summary["total"])
    col_errors.metric("С ошибкой", summary["errors"])
    col_slow.metric("Максимум, мс", f"{summary['slowest_ms']:.0f}")
    col_out.metric("Отправлено, байт", summary["request_bytes"])
    col_in.metric("Получено, байт", summary["response_bytes"])
    if summary["labels"]:
        st.caption(f"Вызовов по меткам: {summary['labels']}")
