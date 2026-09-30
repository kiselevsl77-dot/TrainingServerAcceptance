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
    ? `Дальше: ${text(next.check_id)} · ${text(queue.mode_label)} · команда на вкладке «Прогон»`
    : text(next.reason);

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

/** Состояние экрана прогона: последний вид и раскрытый пункт (только отображение). */
const runView = { data: null, openRow: "", drafts: {} };

/** Прогон: ревизия, режим, управление, строки пунктов и итоги (`SCR-301`). */
function renderRun(data) {
  const rows = data.rows || [];
  const progress = data.progress || {};
  runView.data = data;
  document.getElementById("run-meta").textContent =
    `ревизия ${text(data.revision)} · режим «${text(data.mode_label)}» · выполнено ${text(progress.done)} из ${text(progress.total)}` +
    (progress.stop_reason ? ` · останов: ${text(progress.stop_reason)}` : "");

  renderRunModes(data.modes || {});
  renderRunConsole(data);
  renderRunOutcomes((data.last_run || {}).rows || []);

  fillTable(document.querySelector("#run-table tbody"), rows, (row) => {
    const actions = document.createElement("span");
    actions.appendChild(
      button("подробно", "btn btn--tiny", () => {
        runView.openRow = runView.openRow === row.check_id ? "" : row.check_id;
        renderRun(runView.data);
      })
    );
    if (row.state === "снята") {
      actions.appendChild(
        button("вернуть", "btn btn--tiny", () =>
          runAction(`/api/checks/${encodeURIComponent(row.check_id)}`, { action: "resume" })
        )
      );
    } else {
      actions.appendChild(
        button("снять", "btn btn--tiny btn--ghost", () => {
          const reason = window.prompt(`Причина снятия пункта ${row.check_id}:`, "");
          if (reason) {
            runAction(`/api/checks/${encodeURIComponent(row.check_id)}`, {
              action: "suspend",
              reason,
            });
          }
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
  renderRunDetails();
}

/** Режим прогона: «только невыполненное / всё подряд» — варианты и подписи с сервера. */
function renderRunModes(modes) {
  const select = document.getElementById("run-mode");
  const options = modes.options || [];
  select.textContent = "";
  options.forEach((option) => {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    select.appendChild(element);
  });
  select.value = text(modes.current);
  document.getElementById("run-mode-meta").textContent = text(modes.current_label);
  const hint = options.find((option) => option.value === select.value);
  document.getElementById("run-mode-hint").textContent = hint ? text(hint.hint) : "";
}

/** Блок управления: причина «нельзя» с кнопкой-выходом, требования пункта и формы. */
function renderRunConsole(data) {
  const next = data.next || {};
  const item = next.item || {};
  const reason = text(next.reason);
  document.getElementById("run-blocked").hidden = !reason;
  document.getElementById("run-blocked-reason").textContent = reason;
  const fix = document.getElementById("run-fix");
  const fixLabel = text((next.fix || {}).label);
  fix.hidden = !(reason && fixLabel);
  fix.textContent = fixLabel;
  fix.dataset.panel = text((next.fix || {}).panel);

  const notice = document.getElementById("run-notice");
  notice.hidden = !text(next.notice);
  notice.textContent = text(next.notice);

  document.getElementById("run-next-line").textContent = item.check_id
    ? `Следующая: ${text(item.check_id)} — «${text(item.title)}» · ${text(item.class_label)}` +
      ` · раздел ${text(item.section)}` +
      (item.requirement ? ` · требование ${text(item.requirement)}` : "") +
      (item.expected ? ` · ожидаем: ${text(item.expected)}` : "")
    : "";

  const nextButton = document.getElementById("run-next");
  nextButton.disabled = !next.runnable;
  nextButton.textContent = item.check_id
    ? `▶ Следующая: ${text(item.check_id)}`
    : text(next.label) || "▶ Следующая";
  const batch = data.batch || {};
  const batchButton = document.getElementById("run-batch");
  batchButton.disabled = !next.runnable || !batch.enabled;
  batchButton.textContent = text(batch.label) || "▶▶ Пачка tech";

  renderRunCard(item.card || {}, Boolean(next.runnable));
  renderRunMark(item.mark || {}, Boolean(next.runnable));
  renderRunPayload(reason ? {} : item.payload || {});
  renderRunSteps(item.steps || []);
}

/** Карточка запуска `live`/`heavy`: поля, подтверждение и подсказка (`FR-P-19`). */
function renderRunCard(card, runnable) {
  const box = document.getElementById("run-card");
  box.hidden = !card.required;
  if (!card.required) {
    return;
  }
  const item = ((runView.data || {}).next || {}).item || {};
  document.getElementById("run-card-title").textContent =
    `Карточка запуска: ${text(item.check_id)} (${text(item.class_label)})`;
  document.getElementById("run-card-note").textContent = text(card.note);
  const fields = document.getElementById("run-card-fields");
  fields.textContent = "";
  const draft = runView.drafts[runCardCheckId()] || {};
  (card.fields || []).forEach((field) => {
    const label = document.createElement("label");
    label.textContent = field.label;
    // Незаконченный ввод карточки переживает автообновление и смену вкладки:
    // оператор не должен заново набирать подтверждение ресурсов.
    const saved =
      draft[field.name] !== undefined ? draft[field.name] : field.value;
    if (field.kind === "checkbox") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = `run-card-${field.name}`;
      input.checked = Boolean(saved);
      input.addEventListener("change", () => saveCardDraft(field.name, input.checked));
      label.appendChild(input);
    } else if (field.kind === "select") {
      const select = document.createElement("select");
      select.id = `run-card-${field.name}`;
      (card.artifacts || []).forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      });
      select.value = text(saved) || text((card.artifacts || [])[0]);
      select.addEventListener("change", () => saveCardDraft(field.name, select.value));
      label.appendChild(select);
    } else {
      const input = document.createElement("input");
      input.id = `run-card-${field.name}`;
      input.value = text(saved);
      input.size = 34;
      input.addEventListener("input", () => saveCardDraft(field.name, input.value));
      label.appendChild(input);
    }
    fields.appendChild(label);
  });
  const missing = card.missing || [];
  document.getElementById("run-card-missing").textContent = missing.length
    ? `не заполнено: ${missing.join(", ")}`
    : "карточка заполнена";
  document.getElementById("run-card-save").disabled = !runnable;
  document.getElementById("run-card-run").disabled = !runnable;
}

/** Пункт, для которого показывается карточка запуска: следующий по программе. */
function runCardCheckId() {
  return text((((runView.data || {}).next || {}).item || {}).check_id);
}

/** Значения карточки из формы: собираются по описанию полей, пришедшему с сервера. */
function cardValues() {
  const item = ((runView.data || {}).next || {}).item || {};
  const values = {};
  (((item.card || {}).fields) || []).forEach((field) => {
    const element = document.getElementById(`run-card-${field.name}`);
    if (!element) {
      return;
    }
    values[field.name] = field.kind === "checkbox" ? element.checked : element.value;
  });
  return values;
}

/** Незаконченный ввод карточки: помним по пункту и полю до сохранения или запуска. */
function saveCardDraft(name, value) {
  const checkId = runCardCheckId();
  runView.drafts[checkId] = { ...runView.drafts[checkId], [name]: value };
}

/** Ввод карточки отработан: правки больше не нужны. */
function clearCardDraft() {
  delete runView.drafts[runCardCheckId()];
}

/** Ручная отметка `manual`: статус и заключение (`FR-P-32`). */
function renderRunMark(mark, runnable) {
  const box = document.getElementById("run-mark");
  box.hidden = !mark.required;
  if (!mark.required) {
    return;
  }
  document.getElementById("run-mark-title").textContent = `Ручная отметка: ${text(mark.check_id)}`;
  document.getElementById("run-mark-note").textContent =
    `${text(mark.note)} · ${text(mark.note_hint)}`;
  const select = document.getElementById("run-mark-status");
  select.textContent = "";
  (mark.statuses || []).forEach((status) => {
    const option = document.createElement("option");
    option.value = status.value;
    option.textContent = status.label;
    select.appendChild(option);
  });
  select.value = text(mark.default_status);
  // Заключение оператора тоже не теряется при автообновлении: его пишут руками.
  const textBox = document.getElementById("run-mark-text");
  const saved = runView.drafts[`mark:${text(mark.check_id)}`];
  if (saved !== undefined && textBox.value === "") {
    textBox.value = text(saved);
  }
  document.getElementById("run-mark-save").disabled = !runnable;
}

/** Предпросмотр «что будет отправлено»: строки считает Python (`FR-P-19`). */
function renderRunPayload(payload) {
  const lines = payload.lines || [];
  document.getElementById("run-payload").hidden = lines.length === 0;
  document.getElementById("run-payload-summary").textContent = text(payload.summary);
  const list = document.getElementById("run-payload-lines");
  list.textContent = "";
  lines.forEach((line) => {
    const item = document.createElement("li");
    item.textContent = line;
    list.appendChild(item);
  });
}

/** Шаги выбранного пункта: что именно будет вызвано (`FR-P-20`). */
function renderRunSteps(steps) {
  document.getElementById("run-steps").hidden = steps.length === 0;
  const list = document.getElementById("run-steps-list");
  list.textContent = "";
  steps.forEach((step) => {
    const item = document.createElement("li");
    item.textContent =
      `${text(step.label)}${step.summary ? " — " + text(step.summary) : ""}` +
      `${step.safety ? ` · ${text(step.safety)}` : ""}`;
    list.appendChild(item);
  });
}

/** Итоги последнего запуска: чем закончился шаг и что делать дальше (`FR-P-30`). */
function renderRunOutcomes(rows) {
  const outcomes = document.getElementById("run-outcomes");
  outcomes.textContent = "";
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = `outcome outcome--${row.ok ? "ok" : "bad"}`;
    const journal = row.journal ? ` · журнал ${text(row.journal)}` : "";
    const tail = row.stop_reason ? ` · останов: ${text(row.stop_reason)}` : "";
    const hint = row.hint ? ` · ${text(row.hint)}` : "";
    line.textContent =
      `${text(row.icon)} ${text(row.check_id)}: ${text(row.verdict)}${journal}${tail}${hint}`;
    outcomes.appendChild(line);
  });
}

/** Раскрытие пункта: шаги, результат, доказательства и предпросмотр payload (`FR-P-20`). */
function renderRunDetails() {
  if (!runView.openRow) {
    return;
  }
  const rows = (runView.data || {}).rows || [];
  const index = rows.findIndex((row) => row.check_id === runView.openRow);
  const line = document.querySelector("#run-table tbody").children[index];
  if (index < 0 || !line) {
    return;
  }
  const row = rows[index];
  const details = row.details || {};
  const cell = document.createElement("td");
  cell.className = "details";
  cell.colSpan = 9;
  const add = (label, value) => {
    if (!text(value)) {
      return;
    }
    const pre = document.createElement("pre");
    pre.textContent = `${label}: ${text(value)}`;
    cell.appendChild(pre);
  };
  add("Ожидаемый результат", row.expected);
  add("Требование", row.requirement);
  add(
    "Записано",
    details.at ? `${details.at} · ${text(details.author)} · ревизия ${text(details.revision)}` : ""
  );
  add("Заключение / причина", details.note || row.reason);
  (details.steps || []).forEach((step) => {
    const pre = document.createElement("pre");
    pre.textContent = `  ${text(step.label)}${step.summary ? " — " + text(step.summary) : ""}`;
    cell.appendChild(pre);
  });
  const payload = details.payload || {};
  const caption = document.createElement("pre");
  caption.textContent = `Что будет отправлено: ${text(payload.summary) || "…"}`;
  cell.appendChild(caption);
  (payload.lines || []).forEach((lineText) => {
    const pre = document.createElement("pre");
    pre.textContent = `  ${text(lineText)}`;
    cell.appendChild(pre);
  });
  const holder = document.createElement("tr");
  holder.appendChild(cell);
  line.after(holder);
}

/** Действие прогона: вызов, сообщение и обновление вида с шапкой. */
async function runAction(path, body, message) {
  const data = await act(path, body);
  if (!data) {
    return null;
  }
  if (message) {
    flash("ok", message);
  }
  await refresh();
  return data;
}

/** Делает сессию текущей: используется кнопками списка сессий (`FR-P-4`). */
async function openSession(sessionId) {
  await actRefresh(
    "/api/sessions",
    { action: "select", session_id: sessionId },
    `Сессия ${text(sessionId)} открыта`
  );
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
          button("завершить…", "btn btn--tiny btn--primary", () => {
            openSession(row.id);
            showPanel("results");
          })
        );
      }
      return actions;
    })(),
  ]);
}

/** Зона 2: протокол — KPI, фильтры, таблица и «что требует внимания» (`SCR-401`). */
function renderProtocol(data) {
  const summary = data.summary || {};
  document.getElementById("protocol-meta").textContent =
    `в программе ${text(summary.programme_size)} · закрыто ${text(summary.closed)} (${text(summary.coverage)}) · всего строк ${text(data.total)}`;
  fillKpi(document.getElementById("protocol-kpi"), [
    ["Успех", summary.passed],
    ["Отказ", summary.failed],
    ["Блокировано", summary.blocked],
    ["Пропущено", summary.skipped],
    ["Вручную", summary.manual],
    ["Не выполнено", summary.not_run],
    ["Повторов", summary.repeats],
    ["Снято", summary.suspended],
  ]);
  const status = document.getElementById("protocol-status");
  const section = document.getElementById("protocol-section");
  fillSelect(status, "все результаты", data.statuses || [], (item) => item.value, (item) => item.label);
  fillSelect(section, "все разделы", data.sections || [], (item) => item.value, (item) => item.label);
  // Выбранное сервер возвращает вместе с данными: перерисовка не сбрасывает фильтр.
  status.value = text(data.status);
  section.value = text(data.section);
  document.getElementById("protocol-q").value = text(data.query);
  document.getElementById("protocol-csv").href = `/api/protocol?format=csv${resultsQuery()}`;
  const rows = data.rows || [];
  fillTable(document.querySelector("#protocol-table tbody"), rows, (row) => [
    row.in_programme ? row.order : "—",
    row.check_id,
    `${text(row.title)}${row.in_programme ? "" : " (вне программы)"}`,
    row.result,
    row.origin,
    row.journal,
    row.repeats,
    row.reason,
  ]);
  renderLines(
    "protocol-attention",
    rows.filter((row) => row.reason),
    (row) => `${text(row.check_id)} · ${text(row.result)} · ${text(row.reason)}`
  );
  renderLines(
    "protocol-outside",
    rows.filter((row) => !row.in_programme),
    (row) => `${text(row.check_id)} · ${text(row.result)} · ${text(row.origin)}`
  );
}

/** Простые строки блока: подпись строки приходит готовой, браузер только печатает. */
function renderLines(elementId, rows, lineOf) {
  const box = document.getElementById(elementId);
  box.textContent = "";
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "нет строк";
    box.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.textContent = lineOf(row);
    box.appendChild(line);
  });
}

/** Склейка непустых частей подписи через « · » (та же типографика, что в Python). */
function joinText(parts) {
  return parts.map((part) => text(part)).filter((part) => part).join(" · ");
}

/** Состояние страницы «Результаты»: фильтры протокола и выбранный состав (`SCR-401`). */
const resultsView = { data: null, composition: "" };

/** Запрос страницы: фильтры протокола и режим чтения составов датасетов (`UC-19`). */
function resultsQuery(force) {
  const params = new URLSearchParams();
  const status = text(document.getElementById("protocol-status").value);
  const section = text(document.getElementById("protocol-section").value);
  const needle = document.getElementById("protocol-q").value.trim();
  if (status) {
    params.set("status", status);
  }
  if (section) {
    params.set("section", section);
  }
  if (needle) {
    params.set("q", needle);
  }
  if (document.getElementById("results-compositions").checked) {
    params.set("compositions", "1");
  }
  if (force) {
    params.set("refresh", "1");
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** Страница «Результаты»: данные стенда, протокол и отчёт-завершение (`SCR-401`/`SCR-404`). */
function renderResults(data) {
  const session = data.session || {};
  document.getElementById("results-meta").textContent = session.id
    ? `сессия ${text(session.id)} · ${text(session.status)}`
    : "сессия не выбрана";
  renderResultsData(data.data || {}, data.data_summary || []);
  renderProtocol(data.protocol || {});
  renderResultsReport(data.report || {}, data.snapshots || {}, session);
}

/** Зона 1: данные стенда — файлы, записи, `$Нагрузки` и `$Датасеты` с составом (`UC-03`). */
function renderResultsData(data, summary) {
  fillKpi(
    document.getElementById("results-data-kpi"),
    summary.map((item) => [item.label, item.value])
  );
  const errors = data.errors || {};
  const lines = Object.entries(errors)
    .filter(([, value]) => value)
    .map(([key, value]) => `${key}: ${value}`);
  (data.written || []).forEach((checkId) =>
    lines.push(`закрыто по факту запроса: ${checkId}`)
  );
  renderLines("results-data-warnings", lines, (line) => line);
  document.getElementById("results-data-warnings").hidden = lines.length === 0;

  fillTable(document.querySelector("#results-files tbody"), data.files || [], (row) => [
    row.name,
    row.type,
    row.size_text,
    row.imported_at,
    row.path,
  ]);
  document.getElementById("results-records-meta").textContent =
    `требуют внимания: ${text(data.records_attention)}`;
  fillTable(document.querySelector("#results-records tbody"), data.records || [], (row) => [
    row.row_kind,
    row.record_id || row.base_name,
    row.version,
    row.group_size,
    row.duplicate,
  ]);
  fillTable(document.querySelector("#results-loads tbody"), data.loads || [], (row) => [
    row.id,
    row.name,
    row.description,
    row.created_at,
  ]);
  fillTable(document.querySelector("#results-datasets tbody"), data.datasets || [], (row) => [
    row.name,
    row.type,
    row.description,
    row.created_at,
    compositionText(row.composition),
    button("прочитать состав", "btn btn--tiny", () => {
      resultsView.composition = row.id;
      refreshResults();
    }),
  ]);
  const chosen = (data.datasets || []).find((row) => row.id === resultsView.composition);
  renderComposition(chosen ? chosen.composition : null);
}

/** Подпись состава датасета: источник факта и числа (считает Python, `UC-19`). */
function compositionText(composition) {
  if (!composition) {
    return "не читался";
  }
  return joinText([
    composition.source_label,
    `файлов ${text(composition.files)}`,
    `RAW ${text(composition.raw)}`,
    `markup ${text(composition.markup)}`,
    composition.trusted ? "достоверно" : "требует проверки",
  ]);
}

/** Состав выбранного датасета: подпись, строки состава и заметка (`UC-19`). */
function renderComposition(composition) {
  const table = document.getElementById("results-composition-table");
  const box = document.getElementById("results-composition");
  box.textContent = "";
  table.hidden = !composition;
  fillTable(
    document.querySelector("#results-composition-table tbody"),
    composition ? composition.rows || [] : [],
    (row) => [
      row.name,
      row.source,
      row.files,
      row.raw,
      row.markup,
      row.size,
      row.counts,
      row.task_id,
      row.captured_at,
      row.confidence,
      row.note,
    ]
  );
  if (!composition) {
    return;
  }
  const head = document.createElement("div");
  head.textContent = joinText([compositionText(composition), composition.note]);
  box.appendChild(head);
}

/** Состояние ленты обмена: открытая запись и незаконченные правки повтора (`FR-P-24`). */
const journalView = { open: 0, drafts: {} };

/** Зона 3: готовность отчёта, сверка снимков «Начало»/«Окончание» и завершение (`SCR-404`). */
function renderResultsReport(report, snapshots, session) {
  document.getElementById("report-meta").textContent = report.ready
    ? "полнота отчёта: замечаний нет"
    : `полноты не хватает: ${(report.warnings || []).length}`;
  const points = document.getElementById("report-points");
  points.textContent = "";
  (report.points || []).forEach((point) => {
    const line = document.createElement("div");
    line.className = `point point--${point.ok ? "ok" : "bad"}`;
    line.textContent = `${point.ok ? "✅" : "⚠️"} ${text(point.label)}: ${text(point.value)}`;
    points.appendChild(line);
  });
  renderLines("report-warnings", report.warnings || [], (line) => line);
  document.getElementById("report-warnings").hidden = !(report.warnings || []).length;

  const start = snapshots.taken || {};
  renderLines("snapshot-delta", snapshots.delta || [], (row) =>
    `${text(row.counter)}: начало ${text(row.before)} → окончание ${text(row.after)}${row.delta ? ` (${text(row.delta)})` : ""}`
  );
  const notes = (snapshots.labels || []).map(
    (item) => `снимок «${text(item.label)}»: ${item.taken ? text(item.at) : "не снят"}`
  );
  notes.push(`реестры «Начало» / «Окончание»: ${start.start ? "есть" : "нет"} / ${start.end ? "есть" : "нет"}`);
  if (snapshots.errors) {
    notes.push(snapshots.errors);
  }
  resultsView.snapshotNotes = notes;
  renderReportFiles();

  const hint = document.getElementById("finish-hint");
  hint.textContent = session.can_finish
    ? "Завершение снимет снимок «Окончание», сверит реестры с «Началом» и закроет сессию; решение обязательно."
    : "Сессия закрыта: страница показывает протокол и комплект отчёта.";
  document.getElementById("finish-session").disabled = !session.can_finish;
  document.getElementById("snapshot-end").disabled = !session.can_finish;
}

/** Строки комплекта отчёта: что собрано на диск и чего не хватает (`FR-P-50`). */
function renderReportKit(data) {
  resultsView.kit = data;
  renderReportFiles();
}

/** Блок «Комплект отчёта»: снимки сессии плюс файлы последней сборки. */
function renderReportFiles() {
  const kit = resultsView.kit || {};
  const lines = [...(resultsView.snapshotNotes || [])];
  Object.entries(kit.paths || {}).forEach(([key, path]) => lines.push(`собрано · ${key}: ${path}`));
  (kit.annexes || []).forEach((name) => lines.push(`приложение: ${name}`));
  (kit.missing || []).forEach((name) => lines.push(`не найдено: ${name}`));
  renderLines("report-files", lines, (line) => line);
}

/** Загрузка страницы «Результаты» одним запросом; `force` — перечитать реестры стенда. */
async function refreshResults(force) {
  const data = await call("GET", `/api/results${resultsQuery(force)}`);
  resultsView.data = data;
  renderResults(data);
}

/** Лента обмена: записи журнала с независимой подробностью тел (`FR-P-21`). */
function renderJournal(data) {
  const summary = data.summary || {};
  document.getElementById("journal-meta").textContent =
    `записей ${text(summary.total)} · ошибок ${text(summary.errors)} · запрос ${text(summary.request_bytes)} Б · ответ ${text(summary.response_bytes)} Б` +
    (data.bodies_logged === false
      ? " · тела не сохраняются (PULT_LOG_BODIES=false)"
      : "");
  const box = document.getElementById("journal-rows");
  box.textContent = "";
  (data.rows || []).forEach((row) => {
    const line = document.createElement("details");
    line.className = `journal__row journal__row--${row.ok ? "ok" : "bad"}`;
    line.dataset.seq = text(row.seq);
    // Открытая запись переживает автообновление: браузер не должен «захлопывать»
    // то, что оператор только что раскрыл (жалоба 29.09.2026).
    line.open = Number(row.seq) === Number(journalView.open);
    line.addEventListener("toggle", () => {
      journalView.open = line.open ? Number(row.seq) : 0;
    });
    const title = document.createElement("summary");
    title.textContent = `#${text(row.seq)} ${text(row.at)} ${text(row.method)} ${text(row.path)} → ${text(row.status)} · ${text(row.duration_ms)} мс${row.label ? " · " + text(row.label) : ""}${row.error ? " · " + text(row.error) : ""}${row.request_note ? " · " + text(row.request_note) : ""}`;
    line.appendChild(title);
    [
      ["Запрос", row.request, row.request_note],
      ["Ответ", row.response, row.response_note],
      ["curl", row.curl, ""],
    ].forEach(([label, body, note]) => {
      const caption = document.createElement("div");
      caption.className = "journal__label";
      caption.textContent = label;
      const pre = document.createElement("pre");
      pre.className = "journal__body";
      pre.textContent = text(body) || text(note) || "(тела нет)";
      line.appendChild(caption);
      line.appendChild(pre);
    });
    line.appendChild(repeatBox(row));
    box.appendChild(line);
  });
}

/** Повтор вызова с правками: путь, метод и тело (`FR-P-24`). */
function repeatBox(row) {
  const seq = Number(row.seq);
  const draft = journalView.drafts[seq] || {};
  const holder = document.createElement("div");
  holder.className = "journal__repeat";
  const path = document.createElement("input");
  path.value = text(draft.path !== undefined ? draft.path : row.path);
  path.size = 60;
  path.className = "journal__path";
  path.dataset.role = "repeat-path";
  path.addEventListener("input", () => {
    journalView.drafts[seq] = { ...journalView.drafts[seq], path: path.value };
  });
  const body = document.createElement("textarea");
  body.rows = 3;
  body.dataset.role = "repeat-body";
  body.value = text(draft.body !== undefined ? draft.body : row.request);
  body.addEventListener("input", () => {
    journalView.drafts[seq] = { ...journalView.drafts[seq], body: body.value };
  });
  const actions = document.createElement("div");
  actions.className = "row";
  actions.appendChild(
    button("⛓ ссылка на запись", "btn btn--tiny", () => {
      journalView.open = seq;
      window.location.hash = `journal/${seq}`;
      flash("ok", `Ссылка на запись #${text(row.seq)}: ${window.location.href}`);
    })
  );
  actions.appendChild(
    button("⧉ повторить с правками", "btn btn--tiny", async () => {
      let parsed = null;
      try {
        parsed = body.value.trim() ? JSON.parse(body.value) : {};
      } catch (error) {
        flash("error", `Тело повтора не разобрано как JSON: ${error.message}`);
        return;
      }
      const answer = await act("/api/journal/repeat", {
        method: text(row.method) || "GET",
        path: path.value.trim(),
        json: parsed,
        label: `повтор #${text(row.seq)}`,
      });
      if (answer) {
        delete journalView.drafts[seq];
        flash("ok", `Повтор #${text(row.seq)} отправлен: ${text(answer.status)}`);
        await refresh();
      }
    })
  );
  holder.appendChild(path);
  holder.appendChild(body);
  holder.appendChild(actions);
  return holder;
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
      row.scenario,
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
  } else if (name === "results") {
    await refreshResults();
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

/** Порядок вкладок, согласованный с заказчиком (стража — тест в `test_pult_server.py`).
 *  Стартовая вкладка — «Планирование»: пульт открывается на подготовке испытаний.
 */
const PANELS = ["planning", "sessions", "run", "results", "journal"];

/** Панель из адреса страницы: `#planning`, `#journal/12` — открыть вкладку и запись (`IR-P-4`). */
function panelFromHash() {
  const raw = (window.location.hash || "").replace("#", "");
  const [name, argument] = raw.split("/");
  return { panel: PANELS.includes(name) ? name : PANELS[0], argument: argument || "" };
}

/** Показывает панель и подгружает её данные. */
async function showPanel(name, argument) {
  active = name;
  if (name === "journal" && argument) {
    journalView.open = Number(argument) || 0;
  }
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.panel === name);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${name}`);
  });
  window.location.hash = argument ? `${name}/${argument}` : name;
  await refresh();
}

/** Оператор вводит текст или правит поле: перерисовка панели помешает работе. */
function busyTyping() {
  const element = document.activeElement;
  if (!element || !["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName)) {
    return false;
  }
  if (element.type === "checkbox" || element.type === "radio") {
    return false;
  }
  return !element.dataset.allowRefresh;
}

/** Автообновление: фон не дёргает экран под руками оператора (`NFR-P-5`, жалоба 29.09.2026). */
async function autoRefresh() {
  const box = document.getElementById("auto-refresh");
  if (box && !box.checked) {
    return;
  }
  if (busyTyping()) {
    try {
      renderHeader(await call("GET", "/api/state"));
    } catch (error) {
      flash("error", `[${error.kind || "?"}] ${error.message}`);
    }
    return;
  }
  await refresh();
}

/** Подписки на действия оператора. */
function bind() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => showPanel(tab.dataset.panel));
  });
  document.getElementById("btn-refresh").addEventListener("click", refresh);
  document.getElementById("auto-refresh").addEventListener("change", (event) => {
    flash(
      "ok",
      event.target.checked
        ? "Автообновление включено (раз в 15 секунд)"
        : "Автообновление выключено: экран обновляется кнопкой «Обновить» и действиями"
    );
  });

  // «Результаты»: данные стенда, протокол, отчёт и завершение сессии (`SCR-401`, `SCR-404`)
  document.getElementById("results-refresh").addEventListener("click", () => refreshResults(true));
  document.getElementById("results-compositions").addEventListener("change", refreshResults);
  ["protocol-status", "protocol-section"].forEach((id) => {
    document.getElementById(id).addEventListener("change", refreshResults);
  });
  document.getElementById("protocol-q").addEventListener("keyup", (event) => {
    if (event.key === "Enter") {
      refreshResults();
    }
  });
  document.getElementById("snapshot-end").addEventListener("click", async () => {
    const session = (resultsView.data || {}).session || {};
    await actRefresh(
      `/api/sessions/${encodeURIComponent(text(session.id))}/snapshot`,
      { phase: "end" },
      "Снимок «Окончание» снят: реестры сверены с «Началом»"
    );
  });
  document.getElementById("report-build").addEventListener("click", async () => {
    const data = await act("/api/report/build", { build: true });
    if (data) {
      renderReportKit(data);
      flash("ok", `Комплект отчёта собран: файлов ${Object.keys(data.paths || {}).length}`);
      await refreshResults();
    }
  });
  document.getElementById("finish-session").addEventListener("click", async () => {
    const session = (resultsView.data || {}).session || {};
    const conclusion = document.getElementById("finish-decision").value.trim();
    if (!conclusion) {
      flash("error", "Укажите итоговое решение сессии: без него отчёт неполон");
      return;
    }
    const data = await act(`/api/sessions/${encodeURIComponent(text(session.id))}/finish`, {
      close: document.getElementById("finish-close").checked,
      conclusion,
    });
    if (data) {
      document.getElementById("finish-decision").value = "";
      flash("ok", data.closed ? "Сессия закрыта, решение зафиксировано" : "Снимок «Окончание» снят, сессия не закрыта");
      await refresh();
    }
  });

  // Прогон: команды и формы живут на вкладке «Прогон» (правило одной команды, `IR-P-4`)
  document.getElementById("run-next").addEventListener("click", async () => {
    const data = await act("/api/run", { action: "next" });
    if (data) {
      runView.openRow = "";
      renderRun(data);
      flash("ok", `Пункт выполнен: ${text((data.outcomes || [{}])[0].check_id)}`);
      await refresh();
    }
  });
  document.getElementById("run-batch").addEventListener("click", async () => {
    const data = await act("/api/run", { action: "batch-tech" });
    if (data) {
      runView.openRow = "";
      renderRun(data);
      flash("ok", `Пачка tech: выполнено пунктов — ${text((data.outcomes || []).length)}`);
      await refresh();
    }
  });
  document.getElementById("run-mode").addEventListener("change", (event) => {
    runAction("/api/run", { action: "mode", mode: event.target.value }, "Режим прогона изменён");
  });
  document.getElementById("run-fix").addEventListener("click", (event) => {
    const panel = event.target.dataset.panel;
    if (panel) {
      showPanel(panel);
    } else {
      flash("error", "Эту причину пульт устранить не может: проверьте .env и перезапустите");
    }
  });
  document.getElementById("run-card-save").addEventListener("click", async () => {
    const answer = await act(`/api/checks/${encodeURIComponent(runCardCheckId())}`, {
      action: "run-card",
      values: cardValues(),
    });
    if (answer) {
      clearCardDraft();
      flash("ok", "Карточка запуска сохранена");
      await refresh();
    }
  });
  document.getElementById("run-card-run").addEventListener("click", async () => {
    const checkId = runCardCheckId();
    const saved = await act(`/api/checks/${encodeURIComponent(checkId)}`, {
      action: "run-card",
      values: cardValues(),
    });
    if (!saved) {
      return;
    }
    const data = await act("/api/run", { action: "next" });
    if (data) {
      clearCardDraft();
      runView.openRow = "";
      renderRun(data);
      flash("ok", `Пункт ${text(checkId)}: карточка подтверждена, запуск выполнен`);
      await refresh();
    }
  });
  document.getElementById("run-mark-text").addEventListener("input", (event) => {
    const item = ((runView.data || {}).next || {}).item || {};
    runView.drafts[`mark:${text(item.check_id)}`] = event.target.value;
  });
  document.getElementById("run-mark-save").addEventListener("click", async () => {
    const item = ((runView.data || {}).next || {}).item || {};
    const answer = await act(`/api/checks/${encodeURIComponent(text(item.check_id))}`, {
      action: "mark",
      status: document.getElementById("run-mark-status").value,
      note: document.getElementById("run-mark-text").value,
    });
    if (answer) {
      document.getElementById("run-mark-text").value = "";
      delete runView.drafts[`mark:${text(item.check_id)}`];
      runView.openRow = "";
      flash("ok", `Отметка по пункту ${text(item.check_id)} записана`);
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
    const target = panelFromHash();
    if (target.panel !== active || (target.argument && target.argument !== String(journalView.open))) {
      showPanel(target.panel, target.argument);
    }
  });
  window.setInterval(autoRefresh, 15000);
}

bind();
const start = panelFromHash();
showPanel(start.panel, start.argument);
