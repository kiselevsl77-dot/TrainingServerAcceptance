"use strict";

/**
 * Интерфейс пульта: рисует готовые view-модели сервера и отправляет действия.
 *
 * Правило архитектуры B (`llm_tasks/31.4` §3): в JS нет бизнес-логики — ни подписей
 * статусов, ни расчёта прогресса, ни выбора следующего пункта. Всё это приходит из
 * `pult/view.py`, поэтому браузер и Python не могут разойтись в трактовке.
 */

const state = { auto: null };

/** Вызов API: единый конверт `{ok, data}` или `{ok, error}` (§4 ТЗ). */
async function call(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => null);
  if (!payload || payload.ok !== true) {
    const error = (payload && payload.error) || { kind: "network", message: "ответ сервера не разобран" };
    const where = error.exchange_seq ? ` (журнал №${error.exchange_seq})` : "";
    const failure = new Error(`${error.message}${where}`);
    failure.kind = error.kind;
    throw failure;
  }
  return payload.data;
}

/** Сообщение оператору: успех или ошибка с указанием обмена (`FR-P-22`). */
function flash(kind, message) {
  const box = document.getElementById("flash");
  box.className = `flash flash--${kind}`;
  box.textContent = message;
  box.hidden = false;
  window.setTimeout(() => {
    box.hidden = true;
  }, kind === "error" ? 12000 : 6000);
}

/** Значение строкой: `null` и `undefined` — пустая строка, а не «null». */
function text(value) {
  return value === null || value === undefined ? "" : String(value);
}

/** Перерисовывает таблицу: `cells` строит содержимое строки из её данных. */
function fillTable(body, rows, cells) {
  body.textContent = "";
  rows.forEach((row) => {
    const line = document.createElement("tr");
    cells(row).forEach((content) => {
      const cell = document.createElement("td");
      if (content instanceof Node) {
        cell.appendChild(content);
      } else {
        cell.textContent = text(content);
      }
      line.appendChild(cell);
    });
    body.appendChild(line);
  });
}

/** Кнопка таблицы с обработчиком: действия оператора остаются явными. */
function button(label, className, handle) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = className;
  element.textContent = label;
  element.addEventListener("click", handle);
  return element;
}

/** KPI-плитки: подпись и значение (шапки «Протокол» и «Программа»). */
function fillKpi(element, items) {
  element.textContent = "";
  items.forEach(([label, value]) => {
    const box = document.createElement("div");
    box.className = "kpi__item";
    const title = document.createElement("span");
    title.className = "kpi__label";
    title.textContent = label;
    const number = document.createElement("span");
    number.className = "kpi__value";
    number.textContent = text(value);
    box.appendChild(title);
    box.appendChild(number);
    element.appendChild(box);
  });
}

/** Наполняет `select` значениями: первым идёт «все» с пустым значением. */
function fillSelect(element, empty, options, valueOf, labelOf) {
  const current = element.value;
  element.textContent = "";
  const first = document.createElement("option");
  first.value = "";
  first.textContent = empty;
  element.appendChild(first);
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = valueOf(item);
    option.textContent = labelOf(item);
    element.appendChild(option);
  });
  element.value = current;
}

/** Шапка: стенд, сессия, программа, фаза, прогресс и «что дальше» (`SCR-201`). */
function renderHeader(data) {
  const stand = data.stand || {};
  const session = data.session || {};
  const programme = data.programme || {};
  const queue = data.queue || {};
  const next = data.next || {};
  document.getElementById("phase").textContent = text((data.phase || {}).code);
  document.getElementById("phase-note").textContent = text((data.phase || {}).label);
  document.getElementById("stand").textContent = stand.configured
    ? `Стенд: ${text(stand.base_url)}${stand.build ? " · сборка " + text(stand.build) : ""}`
    : "Стенд: адрес не задан (.env)";
  document.getElementById("session-line").textContent = session.id
    ? `Сессия: ${text(session.id)} · ${text(session.status)}`
    : "Сессия: не выбрана";
  document.getElementById("programme-line").textContent = programme.items
    ? `Программа: ревизия ${text(programme.revision)} · ${text(programme.status_label)} · ${text(programme.items)} проверок`
    : "Программа: не собрана";
  document.getElementById("progress-line").textContent = `Прогон: ${text(queue.done)} из ${text(queue.total)}`;
  document.getElementById("next-reason").textContent = next.runnable
    ? `Дальше: ${text(next.check_id)} · ${text(queue.mode_label)}`
    : text(next.reason);

  const nextButton = document.getElementById("btn-next");
  nextButton.textContent = next.command || next.label || "▶ Следующая";
  nextButton.disabled = !next.runnable;
  document.getElementById("btn-batch").disabled = !next.runnable;

  const warnings = document.getElementById("warnings");
  warnings.textContent = "";
  const lines = [];
  if (stand.warning) {
    lines.push(stand.warning);
  }
  (programme.warnings || []).forEach((warning) => lines.push(warning));
  lines.forEach((line) => {
    const item = document.createElement("div");
    item.textContent = line;
    warnings.appendChild(item);
  });
  warnings.hidden = lines.length === 0;
}

/** Прогон: строки ревизии, KPI и итоги последнего запуска (`SCR-301`). */
function renderRun(data) {
  const rows = data.rows || [];
  const progress = data.progress || {};
  document.getElementById("run-meta").textContent =
    `ревизия ${text(data.revision)} · режим «${text(data.mode_label)}» · выполнено ${text(progress.done)} из ${text(progress.total)}` +
    (progress.stop_reason ? ` · останов: ${text(progress.stop_reason)}` : "");

  const outcomes = document.getElementById("run-outcomes");
  outcomes.textContent = "";
  ((data.last_run || {}).rows || []).forEach((row) => {
    const line = document.createElement("div");
    line.className = `outcome outcome--${row.ok ? "ok" : "bad"}`;
    const tail = row.stop_reason ? ` · останов: ${text(row.stop_reason)}` : "";
    const journal = row.journal ? ` · журнал ${text(row.journal)}` : "";
    line.textContent = `${text(row.icon)} ${text(row.check_id)}: ${text(row.verdict)}${journal}${tail}`;
    outcomes.appendChild(line);
  });

  fillTable(document.querySelector("#run-table tbody"), rows, (row) => {
    const actions = document.createElement("span");
    if (row.state === "снята") {
      actions.appendChild(
        button("вернуть", "btn btn--tiny", () => act(`/api/checks/${encodeURIComponent(row.check_id)}`, { action: "resume" }))
      );
    } else {
      actions.appendChild(
        button("снять", "btn btn--tiny btn--ghost", () => {
          const reason = window.prompt(`Причина снятия пункта ${row.check_id}:`, "");
          if (!reason) {
            return;
          }
          act(`/api/checks/${encodeURIComponent(row.check_id)}`, { action: "suspend", reason });
        })
      );
    }
    return [
      row.order,
      row.marker,
      `${text(row.check_id)} — ${text(row.title)}`,
      row.check_class,
      row.queue_state,
      row.result,
      `${text(row.origin)}${row.done_by ? " · " + text(row.done_by) : ""}`,
      `${text(row.journal)}${row.repeats ? " · повторов " + text(row.repeats) : ""}`,
      actions,
    ];
  });
}

/** Сессии: список на диске, выбор текущей, создание и жизненный цикл (`FR-P-2`…`FR-P-4`). */
function renderSessions(data) {
  document.getElementById("sessions-meta").textContent = data.current
    ? `выбрана: ${text(data.current)}`
    : "сессия не выбрана";
  fillTable(document.querySelector("#sessions-table tbody"), data.sessions || [], (row) => [
    `${text(row.id)}${row.current ? " ✓" : ""}`,
    row.title,
    row.build,
    row.status,
    `${text(row.done)} из ${text(row.checks)}`,
    row.created_at,
    (() => {
      const actions = document.createElement("span");
      actions.appendChild(
        button(row.current ? "открыта" : "открыть", "btn btn--tiny", () =>
          actRefresh("/api/sessions", { action: "select", session_id: row.id }, `Сессия ${text(row.id)} открыта`)
        )
      );
      if (row.status === "черновик") {
        actions.appendChild(
          button("старт", "btn btn--tiny btn--primary", () =>
            actRefresh(
              `/api/sessions/${encodeURIComponent(row.id)}/start`,
              {},
              `Сессия ${text(row.id)}: снимок «Начало» снят, прогон начат`
            )
          )
        );
      } else if (row.status === "идёт") {
        actions.appendChild(
          button("завершить", "btn btn--tiny", () =>
            actRefresh(
              `/api/sessions/${encodeURIComponent(row.id)}/finish`,
              { close: true },
              `Сессия ${text(row.id)} завершена`
            )
          )
        );
      }
      return actions;
    })(),
  ]);
}

/** Протокол: KPI и строки результатов, включая «вне программы» (`SCR-401`). */
function renderProtocol(data) {
  const summary = data.summary || {};
  document.getElementById("protocol-meta").textContent =
    `в программе ${text(summary.programme_size)} · закрыто ${text(summary.closed)} (${text(summary.coverage)})`;
  const kpi = document.getElementById("protocol-kpi");
  fillKpi(kpi, [
    ["Успех", summary.passed],
    ["Отказ", summary.failed],
    ["Блокировано", summary.blocked],
    ["Пропущено", summary.skipped],
    ["Вручную", summary.manual],
    ["Не выполнено", summary.not_run],
    ["Повторов", summary.repeats],
    ["Снято", summary.suspended],
  ]);
  document.getElementById("protocol-csv").href = "/api/protocol?format=csv";
  fillTable(document.querySelector("#protocol-table tbody"), data.rows || [], (row) => [
    row.in_programme ? row.order : "—",
    row.check_id,
    `${text(row.title)}${row.in_programme ? "" : " (вне программы)"}`,
    row.result,
    row.origin,
    row.journal,
    row.repeats,
  ]);
}

/** Лента обмена: записи журнала с независимой подробностью тел (`FR-P-21`). */
function renderJournal(data) {
  const summary = data.summary || {};
  document.getElementById("journal-meta").textContent =
    `записей ${text(summary.total)} · ошибок ${text(summary.errors)} · запрос ${text(summary.request_bytes)} Б · ответ ${text(summary.response_bytes)} Б`;
  const box = document.getElementById("journal-rows");
  box.textContent = "";
  (data.rows || []).forEach((row) => {
    const line = document.createElement("details");
    line.className = `journal__row journal__row--${row.ok ? "ok" : "bad"}`;
    const title = document.createElement("summary");
    title.textContent = `#${text(row.seq)} ${text(row.at)} ${text(row.method)} ${text(row.path)} → ${text(row.status)} · ${text(row.duration_ms)} мс${row.label ? " · " + text(row.label) : ""}${row.error ? " · " + text(row.error) : ""}`;
    line.appendChild(title);
    [
      ["Запрос", row.request],
      ["Ответ", row.response],
      ["curl", row.curl],
    ].forEach(([label, body]) => {
      const caption = document.createElement("div");
      caption.className = "journal__label";
      caption.textContent = label;
      const pre = document.createElement("pre");
      pre.className = "journal__body";
      pre.textContent = text(body) || "(тело не сохранялось)";
      line.appendChild(caption);
      line.appendChild(pre);
    });
    box.appendChild(line);
  });
}

// ---------------------------------------------------------------------------
// Планирование: каталог → наборы → программа (UC-05, UC-06)
// ---------------------------------------------------------------------------
/** Состояние страницы: последние виды с сервера, отметки каталога и открытый набор. */
const planning = { catalog: null, sets: null, programme: null, picked: new Set(), openSet: "" };

/** Фильтры каталога: фильтрует сервер, браузер только передаёт выбор оператора. */
function catalogQuery() {
  const params = new URLSearchParams();
  ["q", "section", "class"].forEach((key) => {
    const value = document.getElementById(`catalog-${key}`).value.trim();
    if (value) {
      params.set(key, value);
    }
  });
  return params.toString();
}

/** Счётчик выбранных проверок: отметки живут до перезагрузки страницы. */
function renderPicked() {
  document.getElementById("catalog-selected").textContent = `выбрано ${planning.picked.size}`;
}

/** Каталог: фильтры, массовый выбор и «в набор» (`UC-05`). */
function renderCatalog(data) {
  const rows = data.rows || [];
  const summary = data.summary || {};
  document.getElementById("catalog-meta").textContent =
    `описано ${text(summary.checks_implemented)} из ${text(summary.checks_total)} проверок · показано ${text(rows.length)}`;
  fillSelect(
    document.getElementById("catalog-section"),
    "все разделы",
    data.sections || [],
    (item) => item.key,
    (item) => `${item.key} · ${item.title}`
  );
  fillSelect(
    document.getElementById("catalog-class"),
    "все классы",
    data.classes || [],
    (item) => item.key,
    (item) => item.label
  );

  fillTable(document.querySelector("#catalog-table tbody"), rows, (row) => {
    const mark = document.createElement("input");
    mark.type = "checkbox";
    mark.checked = planning.picked.has(row.check_id);
    mark.addEventListener("change", () => {
      if (mark.checked) {
        planning.picked.add(row.check_id);
      } else {
        planning.picked.delete(row.check_id);
      }
      renderPicked();
    });
    const membership = [
      row.in_set ? "в наборе" : "",
      row.in_programme ? "в программе" : "",
    ]
      .filter(Boolean)
      .join(" · ");
    return [
      mark,
      row.check_id,
      row.title,
      row.check_class,
      row.section,
      row.requirement,
      membership || "—",
      `${text(row.status_icon)} ${text(row.status)}`,
    ];
  });
  renderPicked();
}

/** Библиотека наборов: статус, ревизия, состав и правки (`UC-05`). */
function renderSets(data) {
  const summary = data.summary || {};
  const rows = data.rows || [];
  document.getElementById("sets-meta").textContent =
    `наборов ${text(summary.sets)} · утверждено ${text(summary.approved)} · черновиков ${text(summary.drafts)} · проверок ${text(summary.checks)}`;
  fillSelect(
    document.getElementById("catalog-set"),
    "выберите набор",
    rows,
    (item) => item.set_id,
    (item) => `${item.set_id} · ${item.title}`
  );

  fillTable(document.querySelector("#sets-table tbody"), rows, (row) => {
    const actions = document.createElement("span");
    actions.appendChild(button("состав", "btn btn--tiny", () => showSetItems(row.set_id)));
    if (row.approved) {
      actions.appendChild(
        button("ревизия", "btn btn--tiny", () =>
          planningAction(
            "/api/sets",
            { action: "revise", set_id: row.set_id, comment: "переоткрытие набора" },
            `Набор ${row.set_id}: открыта новая ревизия`
          )
        )
      );
    } else {
      actions.appendChild(
        button("утвердить", "btn btn--tiny btn--primary", () =>
          planningAction(
            "/api/sets",
            { action: "approve", set_id: row.set_id, comment: "утверждено с пульта" },
            `Набор ${row.set_id} утверждён`
          )
        )
      );
    }
    actions.appendChild(
      button("удалить", "btn btn--tiny btn--ghost", () => {
        const ask = row.approved
          ? `Набор ${row.set_id} утверждён: удаление требует подтверждения (FR-P-67). Удалить?`
          : `Удалить набор ${row.set_id}?`;
        if (window.confirm(ask)) {
          planningAction(
            "/api/sets",
            { action: "delete", set_id: row.set_id, force: row.approved },
            `Набор ${row.set_id} удалён`
          );
        }
      })
    );
    return [
      row.set_id,
      row.title,
      row.section,
      `${row.approved ? "✅" : "✎"} ${text(row.status)}`,
      row.revision,
      row.checks,
      row.mandatory,
      row.in_programme ? "да" : "—",
      row.overlap ? text(row.overlap) : "—",
      actions,
    ];
  });
  renderSetItems();
}

/** Состав открытого набора: порядок, обязательность и удаление проверок (`FR-P-66`). */
function renderSetItems() {
  const body = document.querySelector("#set-items-table tbody");
  const meta = document.getElementById("set-items-meta");
  const row = ((planning.sets || {}).rows || []).find((item) => item.set_id === planning.openSet);
  if (!row) {
    body.textContent = "";
    meta.textContent = "набор не выбран — нажмите «состав» в строке";
    return;
  }
  meta.textContent =
    `${row.set_id} · ревизия ${text(row.revision)} · проверок ${text(row.checks)}` +
    (row.approved ? " · утверждён: правка только новой ревизией" : "");
  fillTable(body, row.items || [], (item) => {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = Boolean(item.mandatory);
    box.addEventListener("change", () =>
      planningAction(
        "/api/sets",
        { action: "mandatory", set_id: row.set_id, check_id: item.check_id, mandatory: box.checked },
        `Проверка ${item.check_id}: обязательность ${box.checked ? "включена" : "снята"}`
      )
    );
    const actions = document.createElement("span");
    actions.appendChild(button("↑", "btn btn--tiny", () => setItemAction(row.set_id, item.check_id, -1)));
    actions.appendChild(button("↓", "btn btn--tiny", () => setItemAction(row.set_id, item.check_id, 1)));
    actions.appendChild(
      button("✕", "btn btn--tiny btn--ghost", () =>
        planningAction(
          "/api/sets",
          { action: "remove", set_id: row.set_id, check_ids: [item.check_id] },
          `Проверка ${item.check_id} убрана из набора ${row.set_id}`
        )
      )
    );
    return [item.order, item.check_id, item.title, item.check_class, box, actions];
  });
}

/** Открывает состав набора (повторный клик обновляет вид). */
function showSetItems(setId) {
  planning.openSet = setId;
  renderSetItems();
}

/** Сдвиг проверки в составе набора (↑/↓). */
function setItemAction(setId, checkId, delta) {
  planningAction(
    "/api/sets",
    { action: "move", set_id: setId, check_id: checkId, delta },
    `Порядок набора ${setId}: проверка ${checkId} сдвинута`
  );
}

/** Программа сессии: покрытие, ревизии, дельта и пункты (`UC-06`). */
function renderProgramme(data) {
  const programme = data.programme || {};
  const rows = programme.rows || [];
  const coverage = programme.coverage || [];
  document.getElementById("planning-meta").textContent = data.session
    ? `сессия ${text(data.session)}`
    : "сессия не выбрана: программу собирать не в чем (вкладка «Сессии»)";
  document.getElementById("programme-meta").textContent =
    `ревизия ${text(programme.revision)} · ${text(programme.status_label)} · пунктов ${text(programme.size)} · обязательных ${text(programme.mandatory)}`;
  fillKpi(document.getElementById("programme-kpi"), [
    ["Пунктов", programme.size],
    ["Обязательных", programme.mandatory],
    ["Наборов", (programme.selected_sets || []).length],
    ["Разделов", coverage.length],
    ["Не покрыто", coverage.filter((row) => row.uncovered).length],
    ["Пересечений", rows.filter((row) => row.sources_count > 1).length],
    ["Вне каталога", rows.filter((row) => !row.in_catalog).length],
  ]);

  const setsBox = document.getElementById("programme-sets");
  setsBox.textContent = "";
  (data.sets || []).forEach((row) => {
    const label = document.createElement("label");
    label.className = "check";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = Boolean(row.in_programme);
    box.dataset.setId = row.set_id;
    const caption = document.createElement("span");
    caption.textContent = `${row.set_id} · ${text(row.title)} (${text(row.checks)})`;
    label.appendChild(box);
    label.appendChild(caption);
    setsBox.appendChild(label);
  });
  if (!(data.sets || []).length) {
    setsBox.textContent = "наборов нет: соберите их в каталоге выше";
  }

  const warnings = document.getElementById("programme-warnings");
  warnings.textContent = "";
  (programme.warnings || []).forEach((line) => {
    const item = document.createElement("div");
    item.textContent = line;
    warnings.appendChild(item);
  });
  warnings.hidden = (programme.warnings || []).length === 0;

  fillTable(document.querySelector("#coverage-table tbody"), coverage, (row) => [
    `${text(row.title)} (${text(row.section)})`,
    row.in_programme,
    row.implemented,
    row.planned,
    row.target,
    row.uncovered ? `⚠ ${text(row.missing)}` : "—",
  ]);

  fillTable(document.querySelector("#revisions-table tbody"), programme.revisions || [], (row) => {
    const actions = document.createElement("span");
    if (!row.current) {
      actions.appendChild(
        button("вернуть", "btn btn--tiny btn--ghost", () =>
          planningAction(
            "/api/programme",
            { action: "restore", revision: row.revision, comment: "возврат ревизии" },
            `Программа: возвращена ревизия ${text(row.revision)}`
          )
        )
      );
    }
    return [
      row.revision,
      row.at,
      row.author,
      row.comment || "—",
      row.size,
      actions,
    ];
  });

  const deltaBox = document.getElementById("delta-rows");
  deltaBox.textContent = "";
  programmeDeltaLines(programme.delta).forEach(([kind, line]) => {
    const item = document.createElement("div");
    item.className = `outcome${kind ? ` outcome--${kind}` : ""}`;
    item.textContent = line;
    deltaBox.appendChild(item);
  });

  fillTable(document.querySelector("#programme-table tbody"), rows, (item) => {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = Boolean(item.mandatory);
    box.addEventListener("change", () => programmeMandatory(item.check_id, box.checked));
    const actions = document.createElement("span");
    actions.appendChild(button("↑", "btn btn--tiny", () => programmeMove(item.check_id, -1)));
    actions.appendChild(button("↓", "btn btn--tiny", () => programmeMove(item.check_id, 1)));
    return [
      item.order,
      item.check_id,
      item.title,
      item.check_class,
      box,
      item.sources || (item.in_catalog ? "" : "не описана в каталоге"),
      `${text(item.icon)} ${text(item.status)}`,
      actions,
    ];
  });
}

/** Строки дельты: что добавилось и что ушло против предыдущей ревизии (`SCR-102`). */
function programmeDeltaLines(delta) {
  const data = delta || {};
  if (data.missing) {
    return [["bad", `ревизия ${text(data.revision)} в архиве не найдена — сравнить не с чем`]];
  }
  if (data.revision === null || data.revision === undefined) {
    return [["", "предыдущей ревизии нет: это первая ревизия программы"]];
  }
  const lines = [["", `против ревизии ${text(data.revision)}`]];
  (data.added || []).forEach((checkId) => lines.push(["ok", `+ ${checkId}`]));
  (data.removed || []).forEach((checkId) => lines.push(["bad", `− ${checkId}`]));
  if (!(data.added || []).length && !(data.removed || []).length) {
    lines.push(["", "состав не изменился"]);
  }
  return lines;
}

/** Правки программы: сдвиг пункта по порядку (только черновик, `FR-P-13`). */
function programmeMove(checkId, delta) {
  planningAction(
    "/api/programme",
    { action: "move", check_id: checkId, delta },
    `Пункт ${checkId} сдвинут вручную`
  );
}

/** Признак «обязательная» у пункта программы (смоук-минимум, `FR-P-70`). */
function programmeMandatory(checkId, mandatory) {
  planningAction(
    "/api/programme",
    { action: "mandatory", check_id: checkId, mandatory },
    `Пункт ${checkId}: обязательность ${mandatory ? "включена" : "снята"}`
  );
}

/** Наборы, отмеченные галочками в зоне программы: из них собирается ревизия. */
function selectedSetIds() {
  return Array.from(document.querySelectorAll("#programme-sets input[type=checkbox]"))
    .filter((box) => box.checked)
    .map((box) => box.dataset.setId);
}

/** Действие страницы планирования: вызов, сообщение и перезагрузка трёх зон. */
async function planningAction(path, body, message) {
  const data = await act(path, body);
  if (!data) {
    return;
  }
  if (message) {
    flash("ok", message);
  }
  await refreshPlanning();
}

/** Загрузка страницы планирования: каталог, наборы и программа одним заходом. */
async function refreshPlanning() {
  try {
    const query = catalogQuery();
    const [catalog, sets, programme] = await Promise.all([
      call("GET", `/api/catalog${query ? `?${query}` : ""}`),
      call("GET", "/api/sets"),
      call("GET", "/api/programme"),
    ]);
    planning.catalog = catalog;
    planning.sets = sets;
    planning.programme = programme;
    renderCatalog(catalog);
    renderSets(sets);
    renderProgramme(programme);
  } catch (error) {
    flash("error", `[${error.kind || "?"}] ${error.message}`);
  }
}

/** Каталог → набор: массовый выбор с признаком «обязательная» (`FR-P-65`, `FR-P-70`). */
function addPickedToSet() {
  const setId = document.getElementById("catalog-set").value;
  if (!setId) {
    flash("error", "Выберите набор, в который добавить проверки");
    return;
  }
  const checkIds = Array.from(planning.picked);
  if (!checkIds.length) {
    flash("error", "Отметьте проверки в каталоге");
    return;
  }
  planningAction(
    "/api/sets",
    {
      action: "save",
      set_id: setId,
      check_ids: checkIds,
      mandatory: document.getElementById("catalog-mandatory").checked,
    },
    `В набор ${setId} добавлено проверок: ${checkIds.length}`
  );
}

/** Новый набор из отмеченных проверок: раздел берётся у первой из них (`FR-P-65`). */
function createSetFromPicked() {
  const checkIds = Array.from(planning.picked);
  if (!checkIds.length) {
    flash("error", "Отметьте проверки в каталоге");
    return;
  }
  const title = document.getElementById("catalog-new-title").value.trim();
  const first = ((planning.catalog || {}).rows || []).find((row) => row.check_id === checkIds[0]);
  planningAction(
    "/api/sets",
    {
      action: "save",
      title: title || `Набор ${checkIds[0]}`,
      section: first ? first.section : "",
      check_ids: checkIds,
      mandatory: document.getElementById("catalog-mandatory").checked,
    },
    `Создан набор из ${checkIds.length} проверок`
  );
}

/** Действие оператора: вызов, сообщение и обновление экрана в одном месте. */
async function act(path, body) {
  try {
    return await call("POST", path, body);
  } catch (error) {
    flash("error", `[${error.kind || "?"}] ${error.message}`);
    return null;
  }
}

/** Действие с сообщением и полным обновлением экрана (жизненный цикл сессии). */
async function actRefresh(path, body, message) {
  const data = await act(path, body);
  if (data) {
    if (message) {
      flash("ok", message);
    }
    await refresh();
  }
  return data;
}

let active = "run";

/** Запрос ленты по текущим фильтрам панели (`FR-P-28`). */
function journalQuery() {
  const params = new URLSearchParams({ limit: "100" });
  if (document.getElementById("journal-errors").checked) {
    params.set("errors_only", "1");
  }
  if (!document.getElementById("journal-request").checked) {
    params.set("request", "0");
  }
  if (!document.getElementById("journal-response").checked) {
    params.set("response", "0");
  }
  return params.toString();
}

/** Перерисовывает активную панель с сервера (сервер — единственный источник правды). */
async function loadPanel(name) {
  if (name === "run") {
    renderRun(await call("GET", "/api/run"));
  } else if (name === "planning") {
    await refreshPlanning();
  } else if (name === "sessions") {
    renderSessions(await call("GET", "/api/sessions"));
  } else if (name === "protocol") {
    renderProtocol(await call("GET", "/api/protocol"));
  } else if (name === "journal") {
    renderJournal(await call("GET", `/api/journal?${journalQuery()}`));
  }
}

/** Полное обновление: шапка и активная панель. */
async function refresh() {
  try {
    renderHeader(await call("GET", "/api/state"));
    await loadPanel(active);
  } catch (error) {
    flash("error", `[${error.kind || "?"}] ${error.message}`);
  }
}

/** Панель из адреса страницы: `#planning` открывает вкладку по ссылке (`IR-P-4`). */
function panelFromHash() {
  const name = (window.location.hash || "").replace("#", "");
  return ["run", "planning", "sessions", "protocol", "journal"].includes(name) ? name : "run";
}

/** Показывает панель и подгружает её данные. */
async function showPanel(name) {
  active = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.panel === name);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${name}`);
  });
  window.location.hash = name;
  await refresh();
}

/** Подписки на действия оператора. */
function bind() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => showPanel(tab.dataset.panel));
  });
  document.getElementById("btn-refresh").addEventListener("click", refresh);
  document.getElementById("btn-next").addEventListener("click", async () => {
    const data = await act("/api/run", { action: "next" });
    if (data) {
      renderRun(data);
      flash("ok", `Пункт выполнен: ${text((data.outcomes || [{}])[0].check_id)}`);
      await refresh();
    }
  });
  document.getElementById("btn-batch").addEventListener("click", async () => {
    const data = await act("/api/run", { action: "batch-tech" });
    if (data) {
      renderRun(data);
      flash("ok", `Пачка tech: выполнено пунктов — ${text((data.outcomes || []).length)}`);
      await refresh();
    }
  });
  document.getElementById("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const info = {
      title: document.getElementById("new-title").value,
      object_of_test: document.getElementById("new-object").value,
      operator_fio: document.getElementById("new-operator").value,
      lab: document.getElementById("new-lab").value,
    };
    const data = await act("/api/sessions", { info });
    if (data) {
      flash("ok", `Сессия ${text(data.id)} создана · ${text(data.status)}`);
      await refresh();
    }
  });
  ["journal-errors", "journal-request", "journal-response"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => loadPanel("journal"));
  });

  // Планирование: фильтры каталога, массовый выбор и правки наборов/программы (`UC-05`, `UC-06`)
  document.getElementById("planning-refresh").addEventListener("click", refreshPlanning);
  document.getElementById("catalog-add").addEventListener("click", addPickedToSet);
  document.getElementById("catalog-create").addEventListener("click", createSetFromPicked);
  document.getElementById("catalog-all").addEventListener("click", () => {
    ((planning.catalog || {}).rows || []).forEach((row) => planning.picked.add(row.check_id));
    renderCatalog(planning.catalog || {});
  });
  document.getElementById("catalog-none").addEventListener("click", () => {
    planning.picked.clear();
    renderCatalog(planning.catalog || {});
  });
  ["catalog-q", "catalog-section", "catalog-class"].forEach((id) => {
    document.getElementById(id).addEventListener("change", refreshPlanning);
  });
  document.getElementById("catalog-q").addEventListener("keyup", (event) => {
    if (event.key === "Enter") {
      refreshPlanning();
    }
  });
  document.getElementById("programme-rebuild").addEventListener("click", () => {
    const setIds = selectedSetIds();
    if (!setIds.length) {
      flash("error", "Отметьте хотя бы один набор для программы");
      return;
    }
    planningAction(
      "/api/programme",
      { action: "rebuild", set_ids: setIds },
      `Программа пересобрана из наборов: ${setIds.length}`
    );
  });
  document.getElementById("programme-approve").addEventListener("click", () =>
    planningAction(
      "/api/programme",
      {
        action: "approve",
        comment: "утверждено с пульта",
        reduction: document.getElementById("programme-reduction").value.trim(),
      },
      "Ревизия программы утверждена: прогон пойдёт по ней"
    )
  );
  document.getElementById("programme-revise").addEventListener("click", () =>
    planningAction(
      "/api/programme",
      {
        action: "revise",
        comment: document.getElementById("programme-reduction").value.trim() || "переоткрытие программы",
      },
      "Открыта новая ревизия программы (черновик)"
    )
  );
  window.addEventListener("hashchange", () => {
    const name = panelFromHash();
    if (name !== active) {
      showPanel(name);
    }
  });
}

bind();
showPanel(panelFromHash());
window.setInterval(refresh, 15000);
