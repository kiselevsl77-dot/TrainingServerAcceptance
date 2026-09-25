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
  kpi.textContent = "";
  [
    ["Успех", summary.passed],
    ["Отказ", summary.failed],
    ["Блокировано", summary.blocked],
    ["Пропущено", summary.skipped],
    ["Вручную", summary.manual],
    ["Не выполнено", summary.not_run],
    ["Повторов", summary.repeats],
    ["Снято", summary.suspended],
  ].forEach(([label, value]) => {
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
    kpi.appendChild(box);
  });
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

/** Показывает панель и подгружает её данные. */
async function showPanel(name) {
  active = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.panel === name);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${name}`);
  });
  await loadPanel(name);
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
}

bind();
refresh();
window.setInterval(refresh, 15000);
