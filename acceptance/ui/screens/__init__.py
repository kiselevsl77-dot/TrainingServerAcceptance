"""Экраны пульта (`SCR-101`…`SCR-501`): по модулю на экран.

Реестр `RENDERERS` — единственное место, где экран связывается с ключом маршрута: его
проверяет `acceptance/app.py` (`nav.validate`), а каркас раскладки даёт
`acceptance/ui/components/screen.py`. Наполнение экранов данными — этап 3.
"""

from __future__ import annotations

from collections.abc import Callable

from acceptance.ui.screens import (
    scr101_sets,
    scr102_programme,
    scr201_overview,
    scr202_stand,
    scr203_session,
    scr204_data,
    scr301_run,
    scr302_check,
    scr303_tasks,
    scr401_protocol,
    scr402_journal,
    scr403_notes,
    scr404_report,
    scr405_compare,
    scr501_tools,
)

#: Ключ маршрута → функция отрисовки экрана (порядок — как в `nav.SCREENS`).
RENDERERS: dict[str, Callable[[], None]] = {
    scr101_sets.KEY: scr101_sets.render,
    scr102_programme.KEY: scr102_programme.render,
    scr201_overview.KEY: scr201_overview.render,
    scr202_stand.KEY: scr202_stand.render,
    scr203_session.KEY: scr203_session.render,
    scr204_data.KEY: scr204_data.render,
    scr301_run.KEY: scr301_run.render,
    scr302_check.KEY: scr302_check.render,
    scr303_tasks.KEY: scr303_tasks.render,
    scr401_protocol.KEY: scr401_protocol.render,
    scr402_journal.KEY: scr402_journal.render,
    scr403_notes.KEY: scr403_notes.render,
    scr404_report.KEY: scr404_report.render,
    scr405_compare.KEY: scr405_compare.render,
    scr501_tools.KEY: scr501_tools.render,
}
