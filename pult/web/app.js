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
  renderRunMl(next.ml || {});
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
      // Варианты приходят с сервера: у датасета и модели — из реестров стенда
      // (`llm_tasks/49`, задача 1б), у «что делать с артефактами» — список действий ядра.
      const options = (field.options || []).length
        ? field.options
        : (card.artifacts || []).map((value) => ({ value: value, label: value }));
      if (!text(saved)) {
        // Пустого варианта нет в реестре: без него браузер молча выбрал бы первый
        // датасет, и в карточку попали бы данные, которых оператор не выбирал.
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "не выбрано";
        select.appendChild(empty);
      }
      options.forEach((option) => {
        const element = document.createElement("option");
        element.value = text(option.value);
        element.textContent = text(option.label);
        select.appendChild(element);
      });
      select.value = text(saved) || text((options[0] || {}).value);
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

/** Параметры ML-проверки без карточки: датасет и модель (`llm_tasks/49`, задача 1б). */
function renderRunMl(ml) {
  const box = document.getElementById("run-ml");
  const fields = ml.fields || [];
  box.hidden = ml.panel !== "params" || !fields.length;
  if (box.hidden) {
    return;
  }
  const item = ((runView.data || {}).next || {}).item || {};
  document.querySelector("#run-ml h4").textContent =
    `Параметры проверки: ${text(item.check_id)} — данные и модель`;
  document.getElementById("run-ml-note").textContent = text(ml.note);
  document.getElementById("run-ml-hint").textContent = text(ml.hint);
  const form = document.getElementById("run-ml-fields");
  form.textContent = "";
  const draft = runView.drafts[`ml:${text(item.check_id)}`] || {};
  fields.forEach((field) => {
    const label = document.createElement("label");
    label.textContent = field.required ? `${field.label} *` : field.label;
    const saved = draft[field.name] !== undefined ? draft[field.name] : field.value;
    if (field.kind === "select" && (field.options || []).length) {
      const select = document.createElement("select");
      select.id = `run-ml-${field.name}`;
      // Молчаливый «первый из реестра» недопустим: выбор делает оператор (`FR-P-25`).
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "не выбрано";
      select.appendChild(empty);
      (field.options || []).forEach((option) => {
        const element = document.createElement("option");
        element.value = text(option.value);
        element.textContent = text(option.label);
        select.appendChild(element);
      });
      select.value = text(saved);
      select.addEventListener("change", () => saveMlDraft(field.name, select.value));
      label.appendChild(select);
    } else {
      const input = document.createElement("input");
      input.id = `run-ml-${field.name}`;
      input.value = text(saved);
      input.size = 38;
      input.placeholder = "идентификатор из реестра стенда";
      input.addEventListener("input", () => saveMlDraft(field.name, input.value));
      label.appendChild(input);
    }
    form.appendChild(label);
  });
}

/** Значения параметров проверки из формы: то, что уходит в `action=ml-params`. */
function mlValues() {
  const item = ((runView.data || {}).next || {}).item || {};
  const values = {};
  (((item.ml || {}).fields) || []).forEach((field) => {
    const element = document.getElementById(`run-ml-${field.name}`);
    if (element) {
      values[field.name] = element.value;
    }
  });
  return values;
}

/** Незаконченный выбор параметров: помним по пункту и полю до сохранения. */
function saveMlDraft(name, value) {
  const key = `ml:${runCardCheckId()}`;
  runView.drafts[key] = { ...runView.drafts[key], [name]: value };
}

/** Выбор параметров сохранён: черновик больше не нужен. */
function clearMlDraft() {
  delete runView.drafts[`ml:${runCardCheckId()}`];
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

/** Состояние страницы «Результаты»: фильтры протокола, датасет, состав и лимит строк. */
const resultsView = { data: null, composition: "", dataset: "", source: "", all: false };

/** Запрос страницы: фильтры протокола и фильтр данных стенда по датасету (`UC-19`). */
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
  if (resultsView.dataset) {
    params.set("dataset", resultsView.dataset);
  }
  if (resultsView.source) {
    params.set("source", resultsView.source);
  }
  if (document.getElementById("results-compositions").checked) {
    params.set("compositions", "1");
  }
  if (resultsView.all) {
    // «Показать все строки»: лимит снимает сервер (`limit=0`), интерфейс только просит.
    params.set("limit", "0");
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

/** Наполняет `select` готовыми вариантами сервера и ставит текущее значение. */
function fillOptions(element, options, value) {
  element.textContent = "";
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = text(item.value);
    option.textContent = text(item.label);
    element.appendChild(option);
  });
  element.value = text(value);
}

/** Фильтр «Данных стенда»: варианты, текущий выбор и подпись источника состава (`UC-19`).
 *
 *  Фильтр не считает ничего сам: варианты, режим, достоверность и подпись
 *  («датасет «skfu-train» · сервер (по задаче наполнения `dataset-fill`) · файлов 104»)
 *  приходят из `view.dataset_filter_view`, поэтому экран не может разойтись с ядром.
 */
function renderDataFilter(filter) {
  fillOptions(
    document.getElementById("results-dataset"),
    filter.options || [],
    text(resultsView.dataset)
  );
  const modes = filter.sources || [];
  const current = text(resultsView.source) || text((modes[0] || {}).value);
  fillOptions(document.getElementById("results-source"), modes, current);
  resultsView.source = current;
  document.getElementById("results-filter-label").textContent = text(filter.label);
  if (filter.mode === "dataset" && filter.dataset_id) {
    // Панель состава показывает тот датасет, по которому отобраны строки.
    resultsView.composition = text(filter.dataset_id);
  }
}

/** Зона 1: данные стенда — файлы, записи, `$Нагрузки`, `$Датасеты` и `$Модели` (`UC-03`). */
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

  const sections = data.sections || [];
  renderDataSections(sections);
  renderDataFilter(data.filter || {});
  document.getElementById("results-files-meta").textContent = sectionPage(sections, "files");
  document.getElementById("results-records-meta").textContent = joinText([
    sectionPage(sections, "records"),
    `требуют внимания: ${text(data.records_attention)}`,
  ]);

  fillTable(document.querySelector("#results-files tbody"), data.files || [], (row) => [
    row.name,
    row.type,
    row.size_text,
    row.imported_at,
    row.path,
  ]);
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
  fillTable(document.querySelector("#results-models tbody"), data.models || [], (row) => [
    row.name || row.id,
    row.architecture,
    row.version,
    row.phase_type,
    row.output_size,
    row.created_at,
    row.updated_at,
  ]);
  const chosen = (data.datasets || []).find((row) => row.id === resultsView.composition);
  renderComposition(chosen ? chosen.composition : null);
}

/** Подразделы «Данных стенда»: счётчик в заголовке и кнопка «показать все» (`llm_tasks/49`).
 *
 *  Подписи («$Файлы · 388», «показано 200 из 388» и «показать все строки (388)») считает
 *  Python: браузер печатает готовый текст и не решает, полон ли список.
 */
function renderDataSections(sections) {
  sections.forEach((section) => {
    const counter = document.getElementById(`results-${section.key}-count`);
    if (counter) {
      counter.textContent = text(section.label);
    }
    const showAll = document.getElementById(`results-${section.key}-all`);
    if (showAll) {
      showAll.hidden = !section.can_show_all;
      showAll.textContent = `показать все строки (${text(section.count)})`;
    }
  });
}

/** Подпись обрезки подраздела («показано 200 из 388»); пусто — список полон. */
function sectionPage(sections, key) {
  const section = sections.find((item) => item.key === key);
  return section ? text(section.page_label) : "";
}

/** Сворачивает или разворачивает подразделы «Данных стенда» (состояние живёт в DOM). */
function setDataSectionsOpen(open) {
  document.querySelectorAll("#panel-results details.zone__sub").forEach((box) => {
    box.open = Boolean(open);
  });
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

const tasksView = { data: null };

/** Панель «Задачи»: фоновые прогоны и наблюдение за задачами стенда (`FR-T11`). */
function renderTasks(data) {
  tasksView.data = data || {};
  const view = tasksView.data;
  const jobs = view.jobs || [];
  const jobsView = view.jobs_view || {};
  const tasks = view.tasks || {};
  const observed = tasks.observed || [];
  const list = tasks.list || [];

  document.getElementById("tasks-meta").textContent = [
    `заданий: ${jobs.length}`,
    `наблюдается: ${observed.length}`,
    `в списке стенда: ${list.length}`,
  ].join(" · ");

  const note = document.getElementById("tasks-note");
  const noteText = text(tasks.note || tasks.list_error || "");
  note.hidden = !noteText;
  note.textContent = noteText;

  document.getElementById("jobs-note").textContent = text(jobsView.note || "");
  fillTable(document.getElementById("jobs-table").querySelector("tbody"), jobsView.rows || [], (row) => [
    row.job_id,
    row.label || row.check_id,
    `${row.icon} ${row.status}`,
    row.created_at,
    row.duration,
    row.error || row.verdict,
    row.journal_to ? `#${row.journal_from || "?"}…#${row.journal_to}` : "—",
  ]);

  document.getElementById("observed-meta").textContent = text(
    observed.length ? `команд доступно: ${observed.reduce((sum, row) => sum + (row.actions || []).length, 0)}` : "наблюдений нет"
  );
  fillTable(document.getElementById("observed-table").querySelector("tbody"), observed, (row) => [
    row.short_id,
    row.task_type,
    row.check_id || row.origin,
    `${row.icon} ${row.status_label}`,
    row.active ? `активен (${row.interval} с)` : "пауза",
    row.polls,
    commandButtons(row),
  ]);

  document.getElementById("task-list-meta").textContent = text(
    tasks.list_error || `статус берётся из наблюдения: «опрос» или «нет данных»`
  );
  fillTable(document.getElementById("task-list-table").querySelector("tbody"), list, (row) => [
    row.short_id,
    row.task_type,
    row.name || row.description,
    row.created_at,
    `${row.glyph} ${row.status_label}`,
    row.source,
    observeButton(row),
  ]);
}

const artifactsView = { data: null };

/** Панель «Артефакты»: объекты стенда и решения по ним (`FR-T10`, `TC-CLEAN-04`). */
function renderArtifacts(data) {
  artifactsView.data = data || {};
  const view = artifactsView.data;
  const panel = view.view || {};
  const rows = panel.rows || [];
  const summary = panel.summary || view.summary || {};

  document.getElementById("artifacts-meta").textContent = [
    `объектов: ${summary.total || 0}`,
    `наша сессия: ${summary.ours || 0}`,
    `ждут решения: ${summary.undecided || 0}`,
    summary.undecided ? "TC-CLEAN-04 ставить рано" : "TC-CLEAN-04 можно ставить",
  ].join(" · ");

  const note = document.getElementById("artifacts-note");
  const noteText = text((view.errors || []).join("; ") || panel.note || "");
  note.hidden = !noteText;
  note.textContent = noteText;

  fillTable(document.getElementById("artifacts-table").querySelector("tbody"), rows, (row) => [
    row.kind_label,
    row.version ? `${row.name} · ${row.version}` : row.name,
    row.size,
    row.owner,
    row.decided ? `${row.decision_label} · ${row.decided_at}` : "решение не принято",
    row.check_id || "—",
    artifactButtons(row),
  ]);
}

/** Кнопки решения по строке: «💾 в артефакты» и «🗑 удалить» (решение пишется в сессию). */
function artifactButtons(row) {
  const cell = document.createElement("span");
  const payload = { kind: row.kind, id: row.entity_id, check_id: row.check_id || "" };
  // файл стенда становится доказательством только выгруженным: просим скачивание
  const keep = row.kind === "file" ? { ...payload, download: true } : payload;
  cell.appendChild(
    button(
      "💾 в артефакты",
      row.decision === "keep" ? "btn btn--tiny btn--primary" : "btn btn--tiny",
      async () => {
        const data = await act("/api/artifacts", { action: "keep", ...keep });
        if (data) {
          flash("ok", `Артефакт зафиксирован. ${text(data.conclusion)}`);
          renderArtifacts({ ...artifactsView.data, view: data.view, summary: data.summary });
        }
      }
    )
  );
  cell.appendChild(
    button("🗑 удалить", "btn btn--tiny btn--ghost", async () => {
      const data = await act("/api/artifacts", { action: "delete", ...payload });
      if (data) {
        flash("ok", `Объект удалён со стенда. ${text(data.conclusion)}`);
        renderArtifacts({ ...artifactsView.data, view: data.view, summary: data.summary });
      }
    })
  );
  return cell;
}

/** Ячейка с кнопками FSM-1: пускаются только команды, допустимые статусом. */
function commandButtons(row) {
  const cell = document.createElement("span");
  (row.actions || []).forEach((action) => {
    cell.appendChild(
      button(action, "btn btn--tiny", async () => {
        const data = await act(`/api/tasks/${encodeURIComponent(row.task_id)}`, { action });
        if (data) {
          renderTasks({ ...tasksView.data, tasks: data.tasks });
          flash(
            data.allowed ? "ok" : "error",
            data.allowed
              ? `Команда ${action} отправлена: ${text((data.command || {}).status_after) || "статус обновлён"}`
              : `Команда ${action} не отправлена: ${text((data.command || {}).variant || (data.command || {}).error)}`
          );
        }
      })
    );
  });
  cell.appendChild(
    button("опрос", "btn btn--tiny btn--ghost", async () => {
      const data = await act(`/api/tasks/${encodeURIComponent(row.task_id)}`, { action: "poll" });
      if (data) {
        renderTasks({ ...tasksView.data, tasks: data.tasks });
        flash("ok", `Задача ${row.short_id}: ${text((data.poll || {}).status_label)}`);
      }
    })
  );
  cell.appendChild(
    button("снять", "btn btn--tiny btn--ghost", async () => {
      const data = await act(`/api/tasks/${encodeURIComponent(row.task_id)}`, { action: "unobserve" });
      if (data) {
        renderTasks({ ...tasksView.data, tasks: data.tasks });
        flash("ok", `Задача ${row.short_id} снята с наблюдения`);
      }
    })
  );
  return cell;
}

/** Кнопка «наблюдать» для строки списка задач стенда. */
function observeButton(row) {
  const cell = document.createElement("span");
  cell.appendChild(
    button(row.observed ? "наблюдается" : "наблюдать", "btn btn--tiny", async () => {
      const data = await act(`/api/tasks/${encodeURIComponent(row.task_id)}`, {
        action: "observe",
        task_type: row.task_type,
        name: row.name,
      });
      if (data) {
        renderTasks({ ...tasksView.data, tasks: data.tasks });
      }
    })
  );
  return cell;
}

/**
 * «Реальная тренировка» (контекст К2, `llm_tasks/59`): отбор конкретной модели и профиль
 * прогона. Браузер ничего не считает: строки, подписи и шаги приходят из `/api/train`.
 */
const trainView = { data: null, model: "" };

/** Фильтры вкладки из полей формы: отбор делает сервер, он же отвечает подписями. */
function trainQuery(refresh) {
  const parts = [
    `query=${encodeURIComponent(document.getElementById("train-query").value.trim())}`,
    `architecture=${encodeURIComponent(document.getElementById("train-arch").value)}`,
    `phase_type=${encodeURIComponent(document.getElementById("train-phase").value)}`,
    `output_size=${encodeURIComponent(document.getElementById("train-output").value)}`,
    `only_trained=${document.getElementById("train-only-trained").checked ? 1 : 0}`,
    `only_test=${document.getElementById("train-only-test").checked ? 1 : 0}`,
    `only_battle=${document.getElementById("train-only-battle").checked ? 1 : 0}`,
  ];
  if (trainView.model) {
    parts.push(`model=${encodeURIComponent(trainView.model)}`);
  }
  if (refresh) {
    parts.push("refresh=1");
  }
  return parts.join("&");
}

/** Обновляет вкладку с сервера: отбор моделей, карточка и профиль (Э1 К2). */
async function refreshTrain(refresh) {
  renderTrain(await call("GET", `/api/train?${trainQuery(refresh)}`));
}

/** Действие профиля: сохранение, выбор модели и снятие выбора. */
async function actTrain(payload, message) {
  const data = await act("/api/train", payload);
  if (data) {
    renderTrain(data);
    flash("ok", message);
    await refresh();
  }
  return data;
}

/** Поля профиля подставляются из сессии, пока оператор в них не печатает. */
function trainSetInputs(profile) {
  const values = profile || {};
  [
    ["train-object", "object_of_test"],
    ["train-goal", "goal"],
    ["train-responsible", "responsible"],
  ].forEach(([id, key]) => {
    const element = document.getElementById(id);
    if (document.activeElement !== element) {
      element.value = text(values[key]);
    }
  });
  [
    ["train-full-load", "full_load"],
    ["train-dataset-validated", "dataset_validated"],
  ].forEach(([id, key]) => {
    document.getElementById(id).checked = Boolean(values[key]);
  });
}

/** Строка таблицы моделей: свойства и кнопка «свойства» (карточка читается по клику). */
function trainRowCells(row) {
  const profile = (trainView.data || {}).profile || {};
  const chosen = text(profile.model_id) === row.id;
  return [
    `${chosen ? "✔ " : ""}${text(row.name)}`,
    text(row.owner),
    text(row.architecture),
    text(row.phase_type),
    text(row.output_size),
    text(row.signal_count),
    text(row.trained_label),
    text(row.accuracy),
    text(row.updated_at_text),
    button(chosen ? "выбрана" : "свойства", "btn btn--tiny btn--ghost", () => {
      trainView.model = row.id;
      refreshTrain();
    }),
  ];
}

/** Карточка модели: свойства, сигналы, классы, метрики, история и слои. */
function renderTrainCard(card) {
  const meta = document.getElementById("train-card-meta");
  const props = document.getElementById("train-card-props");
  if (!card.id) {
    meta.textContent = "выберите модель в таблице — её свойства появятся здесь";
    props.textContent = "";
    [
      "train-card-metrics",
      "train-card-signals",
      "train-card-classes",
      "train-card-stats",
      "train-card-training",
      "train-card-testing",
      "train-card-layers",
    ].forEach((id) => {
      const box = document.getElementById(id);
      if (box.tagName === "TABLE") {
        fillTable(box.querySelector("tbody"), [], () => []);
      } else {
        renderLines(id, [], () => "");
      }
    });
    return;
  }
  meta.textContent = joinText([
    text(card.name),
    card.test_model ? "модель испытаний (`__TEST__`)" : "боевая модель",
    text(card.metrics_meta),
    card.file.size_text ? `файл ${text(card.file.name)} · ${text(card.file.size_text)}` : "",
    card.updated_at_text ? `обновлена ${text(card.updated_at_text)}` : "",
  ]);
  renderLines("train-card-props", card.rows || [], (row) =>
    `${text(row.label)}: ${text(row.value) || "—"}`
  );
  document.getElementById("train-card-signals-count").textContent = joinText([
    "Группы сигналов и классы",
    text(card.signals_meta),
  ]);
  fillTable(document.querySelector("#train-card-signals tbody"), card.signals || [], (row) => [
    text(row.signal_num),
    text(row.count),
    text(row.devices_text),
    (row.categories || []).join(", "),
  ]);
  fillTable(document.querySelector("#train-card-classes tbody"), card.classes || [], (row) => [
    text(row.signal_num),
    text(row.alias),
  ]);
  const metrics = card.metrics || {};
  renderLines("train-card-metrics", metrics.rows || [], (row) =>
    `${text(row.label)}: ${text(row.value)}`
  );
  fillTable(document.querySelector("#train-card-stats tbody"), metrics.dataset_stats || [], (row) => [
    text(row.label),
    text(row.value),
  ]);
  fillTable(document.querySelector("#train-card-training tbody"), card.training || [], (row) => [
    text(row.epoch),
    text(row.loss),
  ]);
  fillTable(document.querySelector("#train-card-testing tbody"), card.testing || [], (row) => [
    text(row.accuracy),
    text(row.dataset_id),
    text(row.report_file_id),
    text(row.tested_at),
  ]);
  document.getElementById("train-card-layers-count").textContent = joinText([
    "Слои архитектуры",
    `${(card.layers || []).length}`,
  ]);
  fillTable(document.querySelector("#train-card-layers tbody"), card.layers || [], (row) => [
    text(row.order),
    text(row.layer_type),
    text(row.parameters),
  ]);
}

/** MIME для загрузки: стенд принимает CSV только как `text/csv` (иначе отвечает 400). */
function uploadContentType(name) {
  const lower = text(name).toLowerCase();
  if (lower.endsWith(".csv")) {
    return "text/csv";
  }
  if (lower.endsWith(".json")) {
    return "application/json";
  }
  return "application/octet-stream";
}

/**
 * Загрузка файла **сырым телом** с прогрессом (Э2.1, `llm_tasks/64` §2).
 *
 * Тело запроса — сам файл, параметры — в query-строке: боевой RAW доходит до 2,18 ГБ,
 * и только такой способ не тянет его через память браузера. Ответ разбирается тем же
 * конвертом `{ok, data|error}`, что и у JSON-запросов, поэтому формат ошибок знаком.
 */
function uploadFile(file, role, note, onProgress) {
  return new Promise((resolve, reject) => {
    const params = new URLSearchParams({
      file_name: file.name,
      file_type: role,
      description: note || "",
    });
    const request = new XMLHttpRequest();
    request.open("POST", `/api/data/files?${params.toString()}`);
    request.setRequestHeader("Content-Type", uploadContentType(file.name));
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    });
    request.addEventListener("load", () => {
      let body = {};
      try {
        body = JSON.parse(request.responseText);
      } catch (error) {
        body = {};
      }
      if (body.ok) {
        resolve(body.data);
        return;
      }
      const failure = body.error || {};
      reject(new Error(`[${request.status}] ${failure.message || "загрузка не выполнена"}`));
    });
    request.addEventListener("error", () => reject(new Error("сеть недоступна: файл не загружен")));
    request.send(file);
  });
}

/** Загрузка выбранных файлов по очереди: стенд принимает пофайлово, не архивом.
 *
 * Ошибка одного файла не прерывает остальные: каждый результат попадает в таблицу
 * под формой (роль по факту, отказ с причиной), а не «пачкой» прячется в одном flash.
 */
async function uploadChosenFiles() {
  const input = document.getElementById("train-upload-file");
  const role = document.getElementById("train-upload-role").value || "RAW";
  const note = document.getElementById("train-upload-note").value.trim();
  const files = Array.from(input.files || []);
  if (!files.length) {
    flash("error", "Выберите файл (файлы) для загрузки на стенд");
    return;
  }
  const progress = document.getElementById("train-upload-progress");
  const status = document.getElementById("train-upload-status");
  progress.hidden = false;
  const results = [];
  let last = null;
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    const label = `${file.name} (${index + 1}/${files.length})`;
    status.textContent = `загрузка: ${label}`;
    try {
      const data = await uploadFile(file, role, note, (percent) => {
        progress.value = percent;
        status.textContent = `загрузка: ${label} — ${percent}%`;
      });
      last = data;
      const uploaded = (data && data.file) || {};
      results.push({ name: file.name, ok: true, role: uploaded.role_label || role, note: uploaded.size_text || "" });
    } catch (error) {
      results.push({ name: file.name, ok: false, role: role, note: error.message });
    }
    progress.value = 0;
  }
  progress.hidden = true;
  if (last) {
    trainView.data = last;
    renderTrain(last);
    input.value = "";
  }
  renderUploadResults(results);
  const okCount = results.filter((item) => item.ok).length;
  status.textContent = `загружено: ${okCount} из ${results.length}`;
  if (okCount === results.length) {
    flash("ok", `Файлы отправлены на стенд: ${okCount}`);
  } else {
    flash("error", `Загружено ${okCount} из ${results.length}: подробности под формой загрузки`);
  }
}

/** Итоги загрузки по файлам: роль (по факту), размер или причина отказа (Э2.1). */
function renderUploadResults(results) {
  const box = document.getElementById("train-upload-results");
  box.textContent = "";
  (results || []).forEach((item) => {
    const line = document.createElement("div");
    line.className = `outcome outcome--${item.ok ? "ok" : "bad"}`;
    line.textContent = item.ok
      ? `✔ ${text(item.name)} → ${text(item.role)}${item.note ? " · " + text(item.note) : ""}`
      : `✖ ${text(item.name)} → ${text(item.note)}`;
    box.appendChild(line);
  });
}

/** Отмеченные значения множественного выбора: `id` реестра (пустой «все» отбрасывается). */
function selectedOptions(element) {
  return Array.from((element && element.selectedOptions) || [])
    .map((option) => option.value)
    .filter(Boolean);
}

/** Файлы реестра стенда для выбора в датасет: `id` и «имя · размер» (Э2.1). */
function fillRegistry(elementId, rows, fileType) {
  const element = document.getElementById(elementId);
  const chosen = new Set(selectedOptions(element));
  element.textContent = "";
  (rows || [])
    .filter((row) => (fileType === "RAW" ? row.file_type === "RAW" : row.file_type !== "RAW"))
    .forEach((row) => {
      const option = document.createElement("option");
      option.value = text(row.id);
      option.textContent = `${text(row.file_name)} · ${text(row.size_text)}`;
      option.selected = chosen.has(text(row.id));
      element.appendChild(option);
    });
}

/** Действие с датасетом: create/choose/fill/refresh/validate/delete (`POST /api/datasets`). */
async function actDataset(payload, message) {
  const data = await act("/api/datasets", payload);
  if (data) {
    renderTrain(data.view);
    flash("ok", message);
    await refresh();
  }
  return data;
}

/** Отрисовка зоны данных: что залито, датасеты, паспорт сборки и состав (`Э2.1`). */
function renderTrainData(data) {
  document.getElementById("train-data-meta").textContent = joinText([
    `файлов загружено: ${text(data.upload_count)}`,
    `измерений: ${text(data.raw_count)}`,
    `разметки: ${text(data.markup_count)}`,
    data.size_total_text ? `объём: ${text(data.size_total_text)}` : "",
    data.dataset_id ? `датасет: ${text(data.dataset_id)}` : "датасет не выбран",
    data.max_upload_text ? `лимит файла: ${text(data.max_upload_text)}` : "",
  ]);
  document.getElementById("train-upload-hint").textContent =
    "Роль «авто» определяется по заголовку файла (измерения или разметка), поэтому " +
    "смешанную загрузку можно делать одним проходом; пульт проверяет формат до отправки. " +
    `Файл уходит на стенд сырым телом, имя — по правилу ${text(data.test_prefix)}… ` +
    "(боевые имена — только в режиме полной нагрузки). Размер и sha256 считает пульт.";

  const roles = document.getElementById("train-upload-role");
  const role = roles.value;
  fillSelect(
    roles,
    "",
    data.roles || [],
    (item) => item.value,
    (item) => item.label
  );
  roles.value = role || ((data.roles || [])[0] || {}).value || "RAW";

  const registry = data.registry || [];
  fillRegistry("train-registry-raw", registry, "RAW");
  fillRegistry("train-registry-markup", registry, "LOADS");
  document.getElementById("train-registry-count").textContent =
    "Файлы стенда для датасета · RAW " +
    `${registry.filter((row) => row.file_type === "RAW").length} · разметка ` +
    `${registry.filter((row) => row.file_type !== "RAW").length}`;

  renderLines("train-data-steps", data.steps || [], (step) =>
    `${text(step.title)}: ${text(step.status)}` +
    (step.note ? ` — ${text(step.note)}` : "") +
    (step.at ? ` (${text(step.at)})` : "")
  );

  fillTable(document.querySelector("#train-uploads tbody"), data.uploads || [], (row) => [
    text(row.name),
    text(row.role_label),
    text(row.size_text),
    text(row.sha256_short),
    text(row.at),
    text(row.file_id),
  ]);

  const datasetSelect = document.getElementById("train-datasets");
  const chosenDataset = datasetSelect.value;
  fillSelect(
    datasetSelect,
    "датасет: не выбран",
    data.datasets || [],
    (item) => item.id,
    (item) => item.name
  );
  datasetSelect.value = text(data.dataset_id) || chosenDataset;

  renderLines("train-dataset-manifest", data.manifest_rows || [], (row) =>
    `${text(row.label)}: ${text(row.value)}`
  );
  fillTable(
    document.querySelector("#train-manifest-files tbody"),
    data.manifest_files || [],
    (row) => [
      text(row.name),
      text(row.role_label),
      text(row.size_text),
      text(row.sha256_short),
      text(row.file_id),
    ]
  );

  const composition = data.composition || {};
  const compositionMeta = document.getElementById("train-composition-meta");
  compositionMeta.textContent = Object.keys(composition).length
    ? joinText([
        "состав по учёту пульта:",
        `файлов ${text(composition.files)}`,
        `RAW ${text(composition.raw)}`,
        `разметка ${text(composition.markup)}`,
        text(composition.size),
        text(composition.source),
        `задача ${text(composition.task_id)}`,
      ])
    : "состав не зафиксирован: наполните датасет или нажмите «Перечитать состав»";

  const chosen = Boolean(text(data.dataset_id));
  ["train-dataset-fill", "train-dataset-validate", "train-dataset-delete"].forEach((id) => {
    document.getElementById(id).disabled = !chosen;
  });
}

/** Отрисовка вкладки «Реальная тренировка» целиком (сервер — источник правды). */
function renderTrain(data) {
  trainView.data = data;
  const profile = data.profile || {};
  if (profile.model_id) {
    trainView.model = text(profile.model_id);
  }
  document.getElementById("train-meta").textContent = joinText([
    data.configured ? "стенд подключён" : "стенд не задан",
    data.session_id ? `сессия ${text(data.session_id)}` : "сессия не выбрана",
    `в отборе моделей: ${text(data.total)}`,
    data.file_count ? `файлов в реестре: ${text(data.file_count)}` : "",
  ]);
  const warnings = document.getElementById("train-warnings");
  warnings.hidden = !text(data.note);
  warnings.textContent = text(data.note);

  renderLines("train-steps", data.steps || [], (step) =>
    `${step.ready ? "✅" : "▫️"} ${text(step.stage)} · ${text(step.title)}` +
    ` — ${text(step.status)}: ${text(step.note)}`
  );
  renderLines("train-profile", data.profile_rows || [], (row) =>
    `${text(row.label)}: ${text(row.value) || "—"}`
  );
  trainSetInputs(profile);
  renderTrainData(data.data || {});

  const options = data.options || {};
  fillSelect(
    document.getElementById("train-arch"),
    "архитектура: все",
    options.architectures || [],
    (item) => item.value,
    (item) => item.label
  );
  fillSelect(
    document.getElementById("train-phase"),
    "фаза: все",
    options.phases || [],
    (item) => item.value,
    (item) => item.label
  );
  fillSelect(
    document.getElementById("train-output"),
    "выход: любой",
    options.output_sizes || [],
    (item) => item.value,
    (item) => item.label
  );
  const filters = data.filters || {};
  document.getElementById("train-arch").value = text(filters.architecture);
  document.getElementById("train-phase").value = text(filters.phase_type);
  document.getElementById("train-output").value = text(filters.output_size);
  document.getElementById("train-only-trained").checked = Boolean(filters.only_trained);
  document.getElementById("train-only-test").checked = Boolean(filters.only_test);
  document.getElementById("train-only-battle").checked = Boolean(filters.only_battle);
  const query = document.getElementById("train-query");
  if (document.activeElement !== query) {
    query.value = text(filters.query);
  }

  fillTable(document.querySelector("#train-models tbody"), data.rows || [], trainRowCells);
  renderTrainCard(data.card || {});
  const chosen = Boolean(text(profile.model_id));
  const chooseButton = document.getElementById("train-choose");
  chooseButton.disabled = !trainView.model;
  chooseButton.textContent = chosen ? "✔ Заменить выбор модели" : "✔ Выбрать для прогона";
  document.getElementById("train-note").textContent = chosen
    ? joinText([
        "Выбрана модель",
        text(profile.model_label) || trainView.model,
        "— данные (Э2) и обучение (Э3) подключатся к тому же профилю сессии",
      ])
    : "Отбор моделей идёт на стороне пульта: фильтр `device_id` у стенда отвечает 500 " +
      "(`TC-MOD-05`), поэтому фильтры применяются локально.";
}

/** Перерисовывает активную панель с сервера (сервер — единственный источник правды). */
async function loadPanel(name) {
  if (name === "run") {
    renderRun(await call("GET", "/api/run"));
  } else if (name === "planning") {
    await refreshPlanning();
  } else if (name === "sessions") {
    renderSessions(await call("GET", "/api/sessions"));
  } else if (name === "tasks") {
    // панель показывает и прогоны, и судьбу их результатов: два независимых запроса,
    // чтобы отказ одного не прятал другой (ошибка видна в своей зоне)
    const [jobs, artifacts] = await Promise.all([
      call("GET", "/api/jobs"),
      call("GET", "/api/artifacts"),
    ]);
    renderTasks(jobs);
    renderArtifacts(artifacts);
  } else if (name === "train") {
    // «Реальная тренировка» (контекст К2): профиль сессии, отбор моделей и карточка
    await refreshTrain();
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
const PANELS = ["planning", "sessions", "run", "train", "tasks", "results", "journal"];

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
  // «Задачи»: фоновые прогоны и наблюдение (`FR-T11`)
  document.getElementById("tasks-refresh").addEventListener("click", () => loadPanel("tasks"));
  document.getElementById("external-observe").addEventListener("click", async () => {
    const taskId = document.getElementById("external-task-id").value.trim();
    if (!taskId) {
      flash("error", "Укажите task_id внешней задачи: наблюдение ставится по нему");
      return;
    }
    const data = await act(`/api/tasks/${encodeURIComponent(taskId)}`, {
      action: "observe",
      check_id: document.getElementById("external-task-check").value.trim(),
    });
    if (data) {
      document.getElementById("external-task-id").value = "";
      renderTasks({ ...tasksView.data, tasks: data.tasks });
      flash("ok", `Внешняя задача ${taskId.slice(0, 8)} поставлена на наблюдение`);
    }
  });

  // «Реальная тренировка» (контекст К2): профиль прогона, отбор и выбор модели (`llm_tasks/59`)
  document.getElementById("train-refresh").addEventListener("click", () => refreshTrain(true));
  document.getElementById("train-filter").addEventListener("click", () => refreshTrain());
  document.getElementById("train-query").addEventListener("keyup", (event) => {
    if (event.key === "Enter") {
      refreshTrain();
    }
  });
  ["train-arch", "train-phase", "train-output", "train-only-trained", "train-only-test",
    "train-only-battle"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => refreshTrain());
  });
  document.getElementById("train-save-profile").addEventListener("click", () =>
    actTrain(
      {
        action: "save-profile",
        object_of_test: document.getElementById("train-object").value,
        goal: document.getElementById("train-goal").value,
        responsible: document.getElementById("train-responsible").value,
        full_load: document.getElementById("train-full-load").checked,
        dataset_validated: document.getElementById("train-dataset-validated").checked,
      },
      "Профиль прогона записан в сессию испытаний"
    )
  );
  document.getElementById("train-clear-profile").addEventListener("click", () =>
    actTrain({ action: "clear", kind: "profile" }, "Профиль прогона сброшен")
  );
  document.getElementById("train-choose").addEventListener("click", () => {
    const rows = (trainView.data || {}).rows || [];
    const row = rows.find((item) => item.id === trainView.model) || {};
    actTrain(
      { action: "choose", kind: "model", id: trainView.model, label: text(row.name) },
      `Модель выбрана для прогона: ${text(row.name) || trainView.model}`
    );
  });
  document.getElementById("train-choose-clear").addEventListener("click", () =>
    actTrain({ action: "clear", kind: "model" }, "Выбор модели снят")
  );

  // Данные (Э2.1 К2): загрузка файлов сырым телом, датасет и паспорт сборки
  document.getElementById("train-upload").addEventListener("click", uploadChosenFiles);
  document.getElementById("train-files-refresh").addEventListener("click", () => refreshTrain(true));
  document.getElementById("train-dataset-refresh").addEventListener("click", () =>
    actDataset(
      { action: "refresh", dataset_id: document.getElementById("train-datasets").value },
      "Состав датасета перечитан"
    )
  );
  document.getElementById("train-dataset-create").addEventListener("click", () =>
    actDataset(
      { action: "create", name: document.getElementById("train-dataset-name").value },
      "Датасет создан и выбран для прогона"
    )
  );
  document.getElementById("train-dataset-choose").addEventListener("click", () => {
    const value = document.getElementById("train-datasets").value;
    const rows = ((trainView.data || {}).data || {}).datasets || [];
    const row = rows.find((item) => item.id === value) || {};
    actDataset(
      { action: "choose", dataset_id: value, label: text(row.name) },
      `Датасет выбран: ${text(row.name) || value}`
    );
  });
  document.getElementById("train-dataset-fill").addEventListener("click", () => {
    const raw = selectedOptions(document.getElementById("train-registry-raw"));
    const markup = selectedOptions(document.getElementById("train-registry-markup"));
    actDataset(
      { action: "fill", raw_file_ids: raw, markup_file_ids: markup },
      raw.length || markup.length
        ? `Наполнение запущено: файлов стенда RAW ${raw.length}, разметка ${markup.length}`
        : "Наполнение датасета запущено (задача `dataset-fill`)"
    );
  });
  document.getElementById("train-dataset-validate").addEventListener("click", () =>
    actDataset({ action: "validate" }, "Отметка «проверен вне пульта» записана в сессию")
  );
  document.getElementById("train-dataset-delete").addEventListener("click", () =>
    actDataset(
      { action: "delete", dataset_id: document.getElementById("train-datasets").value },
      "Датасет удалён со стенда"
    )
  );

  // «Результаты»: данные стенда, протокол, отчёт и завершение сессии (`SCR-401`, `SCR-404`)
  document.getElementById("results-refresh").addEventListener("click", () => refreshResults(true));
  document.getElementById("results-compositions").addEventListener("change", refreshResults);
  document.getElementById("results-collapse").addEventListener("click", () =>
    setDataSectionsOpen(false)
  );
  document.getElementById("results-expand").addEventListener("click", () =>
    setDataSectionsOpen(true)
  );
  ["files", "records"].forEach((key) => {
    document.getElementById(`results-${key}-all`).addEventListener("click", () => {
      resultsView.all = true;
      flash("ok", "Лимит строк снят: показаны все строки списков данных стенда");
      refreshResults();
    });
  });
  document.getElementById("results-dataset").addEventListener("change", (event) => {
    resultsView.dataset = event.target.value;
    refreshResults();
  });
  document.getElementById("results-source").addEventListener("change", (event) => {
    resultsView.source = event.target.value;
    refreshResults();
  });
  document.getElementById("results-filter-apply").addEventListener("click", () => {
    resultsView.dataset = document.getElementById("results-dataset").value;
    refreshResults();
  });
  document.getElementById("results-filter-reset").addEventListener("click", () => {
    resultsView.dataset = "";
    resultsView.source = "";
    resultsView.all = false;
    flash("ok", "Фильтр по датасету сброшен: показан весь реестр");
    refreshResults();
  });
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
  document.getElementById("run-next-bg").addEventListener("click", async () => {
    const data = await act("/api/run", { action: "next", background: true });
    if (data) {
      const job = data.job || {};
      document.getElementById("run-job").textContent = job.job_id
        ? `Задание ${job.job_id}: ${job.label || job.check_id} — прогресс на вкладке «Задачи»`
        : "";
      flash("ok", `Задание запущено в фоне: ${text(job.check_id)} (прогресс — вкладка «Задачи»)`);
      showPanel("tasks");
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
  document.getElementById("run-ml-save").addEventListener("click", async () => {
    const checkId = runCardCheckId();
    const answer = await act(`/api/checks/${encodeURIComponent(checkId)}`, {
      action: "ml-params",
      values: mlValues(),
    });
    if (answer) {
      clearMlDraft();
      flash("ok", `Параметры проверки ${checkId} сохранены`);
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
