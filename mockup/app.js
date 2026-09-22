/* ПУЛЬТ испытаний — кликабельный прототип целевого интерфейса.
   Документы: docs/15 (ТЗ, требования FR-P/NFR-P/IR-P), docs/16 (макет, экраны SCR-01…SCR-13).
   Прототип офлайн: без сети, без сборки, без внешних зависимостей. */

(function () {
  "use strict";

  var DEMO = window.PULT_DEMO;

  /* ------------------------------------------------------------------ состояние */
  var ST = {
    screen: "SCR-14",
    ui: {
      state: "normal",                 // normal | empty | loading | error | nosession
      req: "сводка", res: "тело",      // подробность запроса и ответа (независимо)
      onlyErrors: false,
      group: "", status: "", search: "",
      feedRows: 8,
      offFor: null,                    // пункт, снимаемый с очереди
      offReason: "",
      markFor: null, markStatus: "", markNote: "",
      repeatFor: null, repeatPath: "", repeatBody: "",
      taskTab: "Наблюдение", dataTab: "Записи",
      toolsTab: "Консоль", notesTab: "Замечания",
      runCard: null,                   // пункт, ожидающий карточку запуска
      setFor: null,                    // набор, открытый в редакторе SCR-14
      setSearch: "", setKlass: "", inSet: "all",
      reduction: ""                    // обоснование сокращения при утверждении программы
    },
    queue: buildQueue(),
    sets: buildSets(),
    programme: { revision: DEMO.programme.revision, status: DEMO.programme.status,
                 author: DEMO.programme.author, reduction: DEMO.programme.reduction },
    exchanges: DEMO.exchanges.slice(),
    selectedId: null,
    lastRunId: null,                   // «последняя» проверка — для чтения ленты
    programmeOrder: null,              // ручной порядок пунктов программы (SCR-15)
    programmeBaseline: null,           // состав предыдущей ревизии — для дельты
    seq: DEMO.exchanges.length ? DEMO.exchanges[DEMO.exchanges.length - 1].seq : 0,
    auto: { on: false, timer: null, pause: 3 }
  };

  /* Наборы проверок (SCR-14): библиотека планирования. */
  function buildSets() {
    return DEMO.sets.map(function (s) {
      return {
        id: s.id, title: s.title, section: s.section, target: s.target,
        status: s.status, revision: s.revision, author: s.author,
        mandatory: s.mandatory, selected: s.selected, items: s.items.slice()
      };
    });
  }

  function buildQueue() {
    return DEMO.catalog.map(function (c) {
      var init = DEMO.initialStatus[c.id];
      return {
        id: c.id, title: c.title, group: c.group, klass: c.klass, req: c.req,
        expected: c.expected, payload: c.payload, calls: c.calls,
        enabled: true, reason: "",
        status: init ? init[0] : "не выполнена",
        verdict: init ? init[1] : "",
        journal: "", ms: null
      };
    });
  }

  /* --------------------------------------------------------------- утилиты */
  function esc(text) {
    return String(text === null || text === undefined ? "" : text).replace(
      /[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }
    );
  }
  function byId(id) {
    for (var i = 0; i < ST.queue.length; i++) { if (ST.queue[i].id === id) { return ST.queue[i]; } }
    return null;
  }
  function isDone(item) { return item.status !== "не выполнена"; }

  /* ---------------------------- планирование: наборы, программа, очередь (SCR-14/15) */
  function setById(id) {
    for (var i = 0; i < ST.sets.length; i++) { if (ST.sets[i].id === id) { return ST.sets[i]; } }
    return null;
  }
  function sectionTitle(group) {
    var s = DEMO.sections[group];
    return s ? s.title : group;
  }
  function sectionTarget(group) {
    var s = DEMO.sections[group];
    return s ? s.target : "стандарт";
  }
  /* Объединение выбранных наборов по логическому ИЛИ: дедупликация + происхождение. */
  function programmeItems() {
    var order = [], map = {};
    ST.sets.forEach(function (set) {
      if (!set.selected) { return; }
      set.items.forEach(function (id) {
        if (!map[id]) { map[id] = { id: id, sources: [], mandatory: set.mandatory }; order.push(map[id]); }
        if (map[id].sources.indexOf(set.id) < 0) { map[id].sources.push(set.id); }
      });
    });
    return order;
  }
  function programmeIdList() {
    return programmeItems().map(function (p) { return p.id; });
  }
  function sourcesOf(id) {
    var found = programmeItems().filter(function (p) { return p.id === id; })[0];
    return found ? found.sources : [];
  }
  function inProgramme(id) { return programmeIdList().indexOf(id) >= 0; }
  /* Очередь прогона — производная утверждённой программы (состав не редактируется). */
  function queueItems() {
    var items = [];
    programmeOrder().forEach(function (id) {
      var item = byId(id);
      if (item) { items.push(item); }
    });
    return items;
  }
  function nextItem() {
    var items = queueItems();
    for (var i = 0; i < items.length; i++) {
      if (items[i].enabled && !isDone(items[i])) { return items[i]; }
    }
    return null;
  }
  function statsOf(items) {
    var s = { total: items.length, done: 0, success: 0, fail: 0, blocked: 0, skipped: 0, off: 0, pending: 0 };
    items.forEach(function (i) {
      if (!i.enabled) { s.off += 1; return; }
      if (i.status === "успех") { s.success += 1; s.done += 1; }
      else if (i.status === "отказ") { s.fail += 1; s.done += 1; }
      else if (i.status === "блокировано") { s.blocked += 1; s.done += 1; }
      else if (i.status === "пропущена" || i.status === "прервана") { s.skipped += 1; s.done += 1; }
      else if (i.status === "выполняется") { s.done += 1; }
      else { s.pending += 1; }
    });
    return s;
  }
  function stats() { return statsOf(queueItems()); }
  /* Покрытие разделов: сколько проверок раздела в программе против всего каталога. */
  function coverage() {
    var groups = [], map = {};
    ST.queue.forEach(function (i) {
      if (!map[i.group]) {
        map[i.group] = { group: i.group, title: sectionTitle(i.group), target: sectionTarget(i.group),
                         total: 0, included: 0 };
        groups.push(map[i.group]);
      }
      map[i.group].total += 1;
      if (inProgramme(i.id)) { map[i.group].included += 1; }
    });
    groups.forEach(function (g) {
      g.level = g.included === 0 ? "не покрыт" : (g.included === g.total ? "покрыт" : "частично");
    });
    return groups;
  }
  function uncovered() {
    return coverage().filter(function (g) { return g.level === "не покрыт"; });
  }
  function overlaps() {
    return programmeItems().filter(function (p) { return p.sources.length > 1; });
  }
  function icon(status) {
    var map = { "не выполнена": "⚪", "выполняется": "▶", "успех": "✅", "отказ": "❌",
                "блокировано": "🚫", "пропущена": "⏭", "прервана": "⛔" };
    return map[status] || "⚪";
  }
  function klassLabel(klass) {
    return { tech: "tech (безопасная)", live: "live (боевая)", heavy: "heavy (ресурсоёмкая)",
             manual: "manual (ручная)" }[klass] || klass;
  }
  function needsCard(item) { return item.klass === "live" || item.klass === "heavy"; }
  function openNotes() {
    return DEMO.notes.filter(function (n) { return n.status !== "закрыто"; });
  }
  function snapshotEnd() { return DEMO.snapshots.end; }
  function readiness() {
    var s = stats(), missing = [];
    if (!snapshotEnd()) { missing.push("снимок «Окончание»"); }
    if (s.pending) { missing.push("невыполненных проверок: " + s.pending); }
    var open = DEMO.notes.filter(function (n) { return n.priority === "P0" && n.status !== "закрыто"; });
    if (open.length) { missing.push("открытых P0: " + open.length); }
    if (DEMO.session.title.length < 10) { missing.push("реквизиты сессии"); }
    var total = s.total, done = s.done;
    var percent = Math.round(100 * done / total) - missing.length * 4;
    return { percent: Math.max(0, Math.min(100, percent)), missing: missing };
  }
  function toast(message) {
    var box = document.getElementById("toast");
    box.textContent = message;
    box.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () { box.hidden = true; }, 3200);
  }
  function curlOf(x) {
    var base = DEMO.stand.baseUrl;
    var body = x.m === "GET" || x.m === "DELETE" ? "" : " \\\n  -H 'Content-Type: application/json' -d '" + x.req + "'";
    return "curl -X " + x.m + " '" + base + x.p + "'" + body;
  }
  function statusBadge(x) {
    var cls = x.s >= 200 && x.s < 300 ? "ok" : (x.s >= 400 && x.s < 500 ? "warn" : "err");
    return '<span class="badge ' + cls + '">' + x.s + " " + x.m + "</span>";
  }

  /* ------------------------------------------------------- реестр экранов (docs/16) */
  var SCREENS = [
    { id: "SCR-14", title: "Наборы проверок", group: "Планирование испытаний", phase: "Ф0",
      req: "FR-P-65…FR-P-67, DR-P-13, IR-P-17, IR-P-18, IR-P-21", render: renderSets },
    { id: "SCR-15", title: "Программа сессии", group: "Планирование испытаний", phase: "Ф0, Ф3",
      req: "FR-P-41, FR-P-68…FR-P-70, DR-P-14, IR-P-19, IR-P-21", render: renderProgramme },
    { id: "SCR-01", title: "Обзор испытаний", group: "Подготовка", phase: "Ф0–Ф4",
      req: "FR-P-11, FR-P-12, FR-P-48, IR-P-8", render: renderOverview },
    { id: "SCR-02", title: "Стенд", group: "Подготовка", phase: "Ф0, Ф4",
      req: "FR-P-1, FR-P-6, FR-P-47, DR-P-8", render: renderStand },
    { id: "SCR-03", title: "Сессия испытаний", group: "Подготовка", phase: "Ф0, Ф4",
      req: "FR-P-2…FR-P-5, FR-P-52, FR-P-55, DR-P-1", render: renderSession },
    { id: "SCR-04", title: "Данные стенда", group: "Подготовка", phase: "Ф0",
      req: "FR-P-7…FR-P-10, DR-P-9", render: renderData },
    { id: "SCR-05", title: "Прогон (единое рабочее место)", group: "Испытания", phase: "Ф1–Ф2",
      req: "FR-P-13…FR-P-23, FR-P-27, FR-P-29, IR-P-3, IR-P-4, IR-P-13", render: renderRun },
    { id: "SCR-06", title: "Карточка проверки", group: "Испытания", phase: "Ф1–Ф2",
      req: "FR-P-14, FR-P-20, FR-P-33, IR-P-5", render: renderCheckCard },
    { id: "SCR-07", title: "$Задачи", group: "Испытания", phase: "Ф1–Ф2",
      req: "FR-P-24…FR-P-26, DR-P-10, IR-P-2", render: renderTasks },
    { id: "SCR-08", title: "Протокол проверок", group: "Результаты", phase: "Ф2",
      req: "FR-P-30, FR-P-32, FR-P-34…FR-P-36, FR-P-64, IR-P-4", render: renderProtocol },
    { id: "SCR-09", title: "Журнал обмена", group: "Результаты", phase: "Ф1–Ф4",
      req: "FR-P-28, FR-P-57, DR-P-4", render: renderJournal },
    { id: "SCR-10", title: "Замечания к API и перспективные требования", group: "Результаты", phase: "Ф2–Ф3",
      req: "FR-P-31, FR-P-37…FR-P-40, FR-P-44, DR-P-6", render: renderNotes },
    { id: "SCR-11", title: "Отчёт испытаний", group: "Результаты", phase: "Ф4",
      req: "FR-P-45…FR-P-56, DR-P-7", render: renderReport },
    { id: "SCR-12", title: "Сравнение сессий и сборок", group: "Результаты", phase: "Ф3–Ф4",
      req: "FR-P-42, FR-P-43", render: renderCompare },
    { id: "SCR-13", title: "Инструменты: консоль, диагностика, настройки, справка", group: "Инструменты",
      phase: "вне процесса", req: "FR-P-58…FR-P-63, IR-P-16", render: renderTools }
  ];

  var GROUPS = ["Планирование испытаний", "Подготовка", "Испытания", "Результаты", "Инструменты"];

  function currentScreen() {
    for (var i = 0; i < SCREENS.length; i++) { if (SCREENS[i].id === ST.screen) { return SCREENS[i]; } }
    return SCREENS[0];
  }

  /* ------------------------------------------------------------ каркас и маршрут */
  function renderSidebar() {
    var html = "";
    GROUPS.forEach(function (group) {
      html += '<div class="navgroup">' + esc(group) + "</div>";
      SCREENS.filter(function (s) { return s.group === group; }).forEach(function (s) {
        html += '<button class="navbtn' + (s.id === ST.screen ? " active" : "") + '" data-act="nav" data-id="' +
          s.id + '">' + esc(s.id + " · " + s.title) + "</button>";
      });
    });
    document.getElementById("sidebar").innerHTML = html;
  }

  function renderTop() {
    document.getElementById("top-stand").textContent = "Стенд: " + DEMO.stand.baseUrl.replace(/^https?:\/\//, "");
    document.getElementById("top-session").textContent = "Сессия: " + DEMO.session.id + " · " + DEMO.session.status;
    document.getElementById("top-build").textContent = "Сборка: " + DEMO.stand.build;
    var prog = document.getElementById("top-programme");
    if (prog) {
      prog.textContent = "Программа: ревизия " + ST.programme.revision + " · " +
        (ST.programme.status === "утверждён" ? "утв." : "черновик") +
        " · " + queueItems().length + " п. из " + ST.queue.length;
    }
  }

  function stateBanner() {
    var u = ST.ui.state;
    if (u === "loading") {
      return '<div class="banner"><div class="skeleton"></div><p class="hint">Загрузка данных стенда… (состояние экрана: загрузка)</p></div>';
    }
    if (u === "empty") {
      return '<div class="banner"><b>Данных пока нет.</b><p class="hint">Пустое состояние: пульт объясняет, что сделать, ' +
        'чтобы данные появились (IR-P-8).</p></div>';
    }
    if (u === "error") {
      return '<div class="banner err"><b>Не удалось получить данные: таймаут 15 с.</b>' +
        '<p class="hint">Что предпринять: проверить адрес стенда в «Инструментах» и повторить. ' +
        'Где след: обмен №218 в журнале обмена (IR-P-9).</p></div>';
    }
    if (u === "nosession") {
      return '<div class="banner"><b>Сессия испытаний не выбрана.</b><p class="hint">Результаты прогона не попадут в протокол. ' +
        'Откройте <a href="#scr-03">SCR-03 Сессия испытаний</a> и создайте сессию.</p></div>';
    }
    return "";
  }

  function render() {
    var screen = currentScreen();
    renderSidebar();
    renderTop();
    document.getElementById("screen").innerHTML =
      "<h2 class=\"title\">" + esc(screen.id + ". " + screen.title) + "</h2>" +
      '<p class="subtitle">Группа: ' + esc(screen.group) + " · Фаза процесса: " + esc(screen.phase) +
      " · Состояние UI: " + esc(ST.ui.state) + "</p>" +
      '<p class="reqline">Требования ТЗ (docs/15): ' + esc(screen.req) + "</p>" +
      stateBanner() +
      screen.render();
    if (window.location.hash !== "#" + screen.id.toLowerCase()) {
      window.history.replaceState(null, "", "#" + screen.id.toLowerCase());
    }
  }

  function go(id) {
    stopAuto();
    ST.screen = id;
    ST.ui.runCard = null;
    render();
  }

  function boot() {
    var hash = (window.location.hash || "").replace("#", "").toUpperCase();
    var match = null;
    SCREENS.forEach(function (s) { if (s.id === hash) { match = s.id; } });
    ST.screen = match || "SCR-14";
    ST.selectedId = nextItem() ? nextItem().id : (ST.queue.length ? ST.queue[0].id : null);
    ST.programmeBaseline = programmeIdList();
    render();
  }

  /* ------------------- SCR-14. Наборы проверок (планирование: каталог ↔ состав набора) */
  function currentSet() {
    return setById(ST.ui.setFor) || ST.sets[0] || null;
  }

  function setCatalogFiltered() {
    var set = currentSet();
    return ST.queue.filter(function (i) {
      if (ST.ui.group && i.group !== ST.ui.group) { return false; }
      if (ST.ui.setKlass && i.klass !== ST.ui.setKlass) { return false; }
      var inSet = !!set && set.items.indexOf(i.id) >= 0;
      if (ST.ui.inSet === "in" && !inSet) { return false; }
      if (ST.ui.inSet === "out" && inSet) { return false; }
      if (ST.ui.setSearch &&
          (i.id + " " + i.title).toLowerCase().indexOf(ST.ui.setSearch.toLowerCase()) < 0) { return false; }
      return true;
    });
  }

  function renderSets() {
    var set = currentSet();
    if (!set) { return '<div class="banner">Наборов нет: создайте набор из каталога или возьмите шаблон.</div>'; }
    var rows = setCatalogFiltered();
    var groups = ["TC-SYS", "TC-FILE", "TC-REC", "TC-LOAD", "TC-TASK", "TC-DS", "TC-MOD", "TC-TR", "TC-INF", "TC-CLEAN"];
    var left = '<div class="card"><h3>Каталог проверок (' + ST.queue.length + ")</h3>" +
      '<div class="row">' +
        '<select data-set="group"><option value="">все разделы</option>' + groups.map(function (g) {
          return '<option value="' + g + '"' + (ST.ui.group === g ? " selected" : "") + ">" + g + " · " +
            esc(sectionTitle(g)) + "</option>";
        }).join("") + "</select>" +
        '<select data-set="setKlass"><option value="">все классы</option>' +
          ["tech", "live", "heavy", "manual"].map(function (k) {
            return '<option value="' + k + '"' + (ST.ui.setKlass === k ? " selected" : "") + ">" + k + "</option>";
          }).join("") + "</select>" +
        '<select data-set="inSet">' +
          [["all", "все"], ["in", "в наборе"], ["out", "не в наборе"]].map(function (o) {
            return '<option value="' + o[0] + '"' + (ST.ui.inSet === o[0] ? " selected" : "") + ">" + o[1] + "</option>";
          }).join("") + "</select>" +
        '<input data-set="setSearch" placeholder="поиск" value="' + esc(ST.ui.setSearch) + '">' +
      "</div>" +
      '<div class="row"><button class="act ghost" data-act="set-add-filtered">＋ добавить отфильтрованные (' +
      rows.length + ")</button>" +
      '<span class="hint">в наборе: ' + set.items.length + " · в каталоге: " + ST.queue.length + "</span></div>" +
      '<div class="queue">' +
      (rows.length ? rows.map(function (i) {
        var inSet = set.items.indexOf(i.id) >= 0;
        return '<div class="qitem' + (inSet ? " next" : "") + '">' +
          '<span class="cid">' + icon(i.status) + " " + esc(i.id) + "</span>" +
          "<span>" + esc(i.title) + "</span>" +
          '<span class="km">' + esc(i.group) + " · " + esc(i.klass) + "</span>" +
          '<button class="mini" data-act="' + (inSet ? "set-remove" : "set-add") + '" data-id="' + i.id + '">' +
          (inSet ? "− убрать" : "＋ в набор") + "</button></div>";
      }).join("") : '<p class="hint">Ничего не найдено — измените фильтры.</p>') +
      "</div>" +
      '<p class="hint">Статус рядом с проверкой — её состояние в текущей сессии (только чтение): ' +
      "повторный прогон выполненной проверки сохранит историю (FR-P-36).</p></div>";
    var kpi = { tech: 0, live: 0, heavy: 0, manual: 0 };
    set.items.forEach(function (id) {
      var item = byId(id);
      if (item) { kpi[item.klass] = (kpi[item.klass] || 0) + 1; }
    });
    var right = '<div class="card"><h3>' + esc(set.id + " «" + set.title + "»") + "</h3>" +
      "<p>Раздел: <b>" + esc(set.section + " · " + sectionTitle(set.section)) + "</b> · Объём: <b>" +
      esc(set.target) + "</b> · Автор: " + esc(set.author) + "</p>" +
      "<p>Ревизия: <b>" + set.revision + "</b> · Статус: " +
      (set.status === "утверждён" ? "✅ утверждён" : "📝 черновик") +
      (set.mandatory ? ' · <span class="badge warn">обязательный смоук</span>' : "") + "</p>" +
      '<div class="queue">' + (set.items.length ? set.items.map(function (id, index) {
        var item = byId(id);
        if (!item) {
          return '<div class="qitem off"><span class="cid">' + esc(id) + "</span><span>нет в каталоге</span></div>";
        }
        return '<div class="qitem' + (item.klass === "heavy" ? " next" : "") + '">' +
          "<span>" + (index + 1) + ".</span>" +
          '<span class="cid">' + icon(item.status) + " " + esc(item.id) + "</span>" +
          "<span>" + esc(item.title) + "</span>" +
          '<span class="km">' + esc(item.klass) + " · " + esc(item.group) + "</span>" +
          '<button class="mini" data-act="set-up" data-id="' + item.id + '">↑</button>' +
          '<button class="mini" data-act="set-down" data-id="' + item.id + '">↓</button>' +
          '<button class="mini" data-act="set-remove" data-id="' + item.id + '">−</button></div>';
      }).join("") : '<p class="hint">Состав пуст: добавьте проверки из каталога слева.</p>') + "</div>" +
      '<div class="kpi" style="margin-top:8px">' + cell("В наборе", set.items.length) + cell("tech", kpi.tech) +
      cell("live", kpi.live) + cell("heavy", kpi.heavy) + cell("manual", kpi.manual) + "</div>" +
      '<p class="hint">Наборы: ' + ST.sets.map(function (s) {
        return '<button class="mini" data-act="set-open" data-id="' + s.id + '">' +
          (s.id === set.id ? "● " : "") + esc(s.id) + (s.selected ? " ✓" : "") + "</button>";
      }).join(" ") + "</p>" +
      '<div class="row"><button class="act" data-act="set-approve">✅ Утвердить</button>' +
      '<button class="act ghost" data-act="set-save">💾 Сохранить ревизию</button>' +
      '<button class="act ghost" data-act="set-new">＋ новый набор</button>' +
      '<button class="act ghost" data-act="set-copy">⧉ копировать</button>' +
      '<button class="act ghost" data-act="nav" data-id="SCR-15">к программе →</button></div>' +
      '<p class="hint">Команд запуска в разделе «Планирование» нет: запуск — только «Следующая» на «Прогоне» ' +
      "(IR-P-4, IR-P-18). Изменение утверждённого набора выпускает новую ревизию (FR-P-66).</p></div>";
    return '<div class="grid3"><div>' + left + "</div><div>" + right + "</div></div>";
  }

  /* --------------------- SCR-15. Программа сессии (объединение наборов, покрытие) */
  function programmeOrder() {
    var base = programmeIdList();
    if (!ST.programmeOrder) { return base; }
    var ordered = ST.programmeOrder.filter(function (id) { return base.indexOf(id) >= 0; });
    base.forEach(function (id) { if (ordered.indexOf(id) < 0) { ordered.push(id); } });
    return ordered;
  }

  function delta() {
    var current = programmeIdList(), base = ST.programmeBaseline || [];
    return {
      added: current.filter(function (id) { return base.indexOf(id) < 0; }),
      removed: base.filter(function (id) { return current.indexOf(id) < 0; })
    };
  }

  function renderProgramme() {
    var items = programmeItems();
    var d = delta();
    var left = '<div class="card"><h3>Наборы (' + ST.sets.length + ")</h3>" +
      '<p class="hint">Программа — объединение выбранных наборов по логическому ИЛИ: проверка, ' +
      "встречающаяся в нескольких наборах, входит в программу один раз (FR-P-68).</p>" +
      '<div class="queue">' + ST.sets.map(function (s) {
        return '<div class="qitem' + (s.mandatory ? " next" : "") + '">' +
          '<input type="checkbox" data-act="prog-toggle" data-id="' + s.id + '"' + (s.selected ? " checked" : "") + ">" +
          "<span>" + esc(s.id) + " «" + esc(s.title) + "»</span>" +
          '<span class="km">' + esc(s.section) + " · " + s.items.length + " п. · " + esc(s.status) +
          (s.mandatory ? " · обязательный" : "") + "</span>" +
          '<button class="mini" data-act="set-open" data-id="' + s.id + '">✎</button></div>';
      }).join("") + "</div>" +
      '<div class="row">Шаблон: ' +
        [["полная", "полная"], ["смоук", "смоук"], ["регресс", "регресс"]].map(function (t) {
          return '<button class="act ghost" data-act="prog-template" data-id="' + t[0] + '">' + t[1] + "</button>";
        }).join("") + "</div>" +
      '<p class="hint">Шаблон «смоук» — обязательные наборы; «регресс» — наборы, в которых есть отказы и ' +
      "блокировки текущей сессии (FR-P-41, FR-P-70).</p></div>";

    var s = stats();
    var cov = coverage();
    var bad = uncovered();
    var right = '<div class="card"><h3>Программа сессии · ревизия ' + ST.programme.revision + " · " +
      (ST.programme.status === "утверждён" ? "✅ утверждена" : "📝 черновик") + "</h3>" +
      "<p>Автор: " + esc(ST.programme.author) + "</p>" +
      "<p>Включено по ИЛИ: <b>" + items.length + "</b> пункт(ов) · пересечений: " +
      overlaps().length + " (учтены один раз) · выбранных наборов: " +
      ST.sets.filter(function (x) { return x.selected; }).length + "</p>" +
      '<div class="kpi">' + cell("В программе", items.length) + cell("Выполнено", s.done) +
      cell("Успех", s.success) + cell("Отказ", s.fail) + cell("Блокировано", s.blocked) + "</div>" +
      "<h3>Покрытие разделов</h3>" +
      "<table><tr><th>Раздел</th><th>Объём</th><th>В программе</th><th>Состояние</th></tr>" +
      cov.map(function (g) {
        return "<tr><td>" + esc(g.group + " · " + g.title) + "</td><td>" + esc(g.target) + "</td><td>" +
          g.included + " / " + g.total + "</td><td>" +
          (g.level === "не покрыт" ? "⛔ не покрыт" : (g.level === "покрыт" ? "✅ покрыт" : "🟡 частично")) +
          "</td></tr>";
      }).join("") + "</table>" +
      (bad.length
        ? '<div class="banner"><b>Не покрыты разделы: ' + esc(bad.map(function (g) { return g.group; }).join(", ")) +
          "</b>" +
          '<p class="hint">При утверждении нужно обоснование сокращения — оно попадёт в протокол и отчёт (FR-P-69).</p>' +
          '<div class="row"><input data-set="reduction" placeholder="обоснование сокращения" value="' +
          esc(ST.ui.reduction || ST.programme.reduction) + '"></div></div>'
        : '<p class="hint">✅ все разделы каталога покрыты.</p>') +
      "<h3>Состав программы (" + items.length + ")</h3>" +
      '<div class="queue">' + (items.length ? items.map(function (p) {
        var item = byId(p.id);
        return '<div class="qitem' + (p.sources.length > 1 ? " next" : "") + '">' +
          '<span class="cid">' + (item ? icon(item.status) : "•") + " " + esc(p.id) + "</span>" +
          "<span>" + esc(item ? item.title : "") + "</span>" +
          '<span class="km">из: ' + esc(p.sources.join(", ")) + "</span>" +
          '<button class="mini" data-act="prog-up" data-id="' + p.id + '">↑</button>' +
          '<button class="mini" data-act="prog-down" data-id="' + p.id + '">↓</button></div>';
      }).join("") : '<p class="hint">Наборы не выбраны: программа пуста — включите наборы слева.</p>') + "</div>" +
      '<p class="hint">Дельта с ревизией ' + Math.max(1, ST.programme.revision - 1) + ": +" +
      d.added.length + " (" + esc(d.added.slice(0, 6).join(", ") || "—") + ") · −" +
      d.removed.length + " (" + esc(d.removed.slice(0, 6).join(", ") || "—") + ")</p>" +
      '<div class="row"><button class="act" data-act="prog-approve">✅ Утвердить программу</button>' +
      '<button class="act ghost" data-act="prog-revision">➕ Новая ревизия</button>' +
      '<button class="act ghost" data-act="nav" data-id="SCR-05">к прогону →</button>' +
      '<button class="act ghost" data-act="toast" data-msg="Заглушка: программа выгружается в json/csv (FR-P-70)">⬇ json</button>' +
      '<button class="act ghost" data-act="nav" data-id="SCR-14">✎ наборы</button></div>' +
      '<p class="hint">Состав программы меняет только руководитель испытаний; в «Испытаниях» правка недоступна — ' +
      "только «снять с причиной» как результат прогона (FR-P-15, IR-P-18).</p></div>";
    return '<div class="grid3"><div>' + left + "</div><div>" + right + "</div></div>";
  }

  /* ------------------------------------------------- SCR-01. Обзор испытаний */
  function renderOverview() {
    var s = stats(), r = readiness(), open = openNotes();
    var p0 = open.filter(function (n) { return n.priority === "P0"; }).length;
    var p1 = open.filter(function (n) { return n.priority === "P1"; }).length;
    var phases = [["Ф0", "done"], ["Ф1", "now"], ["Ф2", ""], ["Ф3", ""], ["Ф4", ""]];
    var percent = Math.round(100 * s.done / s.total);
    return '<div class="grid3">' +
      '<div><div class="card"><h3>Положение в процессе</h3>' +
        '<div class="phases">' + phases.map(function (p) {
          return '<span class="phase ' + p[1] + '">' + p[0] + "</span>";
        }).join("") + "</div>" +
        '<p class="hint">Сессия: ' + esc(DEMO.session.title) + " · " + esc(DEMO.session.started) + "</p>" +
        '<p class="hint">Методика: ' + esc(DEMO.session.method) + " · Оператор: " + esc(DEMO.session.operator) + "</p>" +
      "</div>" +
      '<div class="card"><h3>Программа и покрытие</h3>' +
        "<p>Ревизия <b>" + ST.programme.revision + "</b> · " +
        (ST.programme.status === "утверждён" ? "✅ утверждена" : "📝 черновик") +
        " · пунктов: " + queueItems().length + " из " + ST.queue.length + "</p>" +
        "<p>Разделов покрыто: " +
        coverage().filter(function (g) { return g.level !== "не покрыт"; }).length +
        " из " + coverage().length +
        (uncovered().length
          ? " · ⛔ не покрыты: " + esc(uncovered().map(function (g) { return g.group; }).join(", "))
          : "") + "</p>" +
        '<div class="row"><button class="act ghost" data-act="nav" data-id="SCR-15">К программе →</button>' +
        '<button class="act ghost" data-act="nav" data-id="SCR-14">К наборам →</button></div>' +
      "</div>" +
      '<div class="card"><h3>Прогресс очереди</h3>' +
        '<div class="bar green"><span style="width:' + percent + '%"></span></div>' +
        "<p>выполнено <b>" + s.done + "</b> из " + s.total + " (" + percent + " %)</p>" +
        '<div class="kpi">' +
          cell("Успех", s.success) + cell("Отказ", s.fail) + cell("Блокировано", s.blocked) +
          cell("Пропущ./прерв.", s.skipped) + cell("Снято", s.off) +
        "</div>" +
        '<div class="row"><button class="act" data-act="nav" data-id="SCR-05">К прогону →</button>' +
        '<button class="act ghost" data-act="nav" data-id="SCR-08">К протоколу →</button></div>' +
      "</div>" +
      '<div class="card"><h3>Готовность отчёта: ' + r.percent + " %</h3>" +
        (r.missing.length
          ? '<ul class="list">' + r.missing.map(function (m) { return "<li>⛔ " + esc(m) + "</li>"; }).join("") + "</ul>"
          : '<p class="hint">✅ комплект можно формировать</p>') +
        '<div class="row"><button class="act ghost" data-act="nav" data-id="SCR-11">К отчёту →</button></div>' +
      "</div></div>" +
      '<div><div class="card"><h3>Стенд</h3><p>✅ ' + esc(DEMO.stand.baseUrl) + " · " + esc(DEMO.stand.build) +
        '</p><p class="hint">Контур: ' + esc(DEMO.stand.namespace) + " · health: " + esc(DEMO.stand.health) + "</p>" +
        '<p class="hint">Обменов за сессию: ' + ST.exchanges.length + " · ошибок: " +
        ST.exchanges.filter(function (x) { return x.s >= 400; }).length + "</p>" +
        '<button class="act ghost" data-act="nav" data-id="SCR-02">Открыть стенд →</button></div>' +
      '<div class="card"><h3>Внимание</h3>' +
        "<p>Открытые <b>P0</b>: " + p0 + " · <b>P1</b>: " + p1 + "</p>" +
        '<ul class="list">' + open.slice(0, 4).map(function (n) {
          return "<li>" + esc(n.priority + " · " + n.check + " — " + n.title) + "</li>";
        }).join("") + "</ul>" +
        '<button class="act ghost" data-act="nav" data-id="SCR-10">К замечаниям →</button></div>' +
      '<div class="card"><h3>Уборка `__TEST__`</h3>' +
        "<p>Создано в сессии: 3 · удалено: 3 · хвостов нет</p>" +
        '<p class="hint">Контроль обязателен до формирования отчёта (FR-P-46).</p></div>' +
      '<div class="card"><h3>Что дальше</h3><ol class="list">' +
        "<li>Продолжить прогон очереди: следующий пункт — " + esc(nextItem() ? nextItem().id : "нет") + "</li>" +
        "<li>Обработать открытые замечания по приоритетам</li>" +
        "<li>Снять снимок «Окончание» и сформировать комплект</li></ol>" +
        '<button class="act" data-act="nav" data-id="SCR-05">К прогону →</button></div></div></div>';
  }

  function cell(label, value) {
    return '<div class="cell"><b>' + esc(value) + "</b>" + esc(label) + "</div>";
  }

  /* ------------------------------------------------------------- SCR-02. Стенд */
  function renderStand() {
    var snap = DEMO.snapshots, reg = DEMO.registries;
    var delta = snap.end
      ? "файлы " + (snap.end.files - snap.begin.files) + " · датасеты " + (snap.end.datasets - snap.begin.datasets) +
        " · модели " + (snap.end.models - snap.begin.models)
      : "появится после снимка «Окончание»";
    return '<div class="grid3"><div>' +
      '<div class="card"><h3>Сервер и сборка</h3>' +
        "<p>Адрес: <code>" + esc(DEMO.stand.baseUrl) + "</code></p>" +
        "<p>Сборка: <code>" + esc(DEMO.stand.build) + "</code> · branch " + esc(DEMO.stand.branch) +
        " · собрана " + esc(DEMO.stand.builtAt) + "</p>" +
        "<p>Изолированный контур: <code>" + esc(DEMO.stand.namespace) + "</code></p>" +
        "<p>Ответ health: " + esc(DEMO.stand.health) + "</p>" +
        '<p class="hint">Журнал запуска: <code>' + esc(DEMO.stand.appLog) + "</code></p>" +
        '<div class="row"><button class="act" data-act="check-stand">🔄 Проверить стенд</button>' +
        '<button class="act ghost" data-act="snapshot">📸 Снять снимок</button></div></div>' +
      '<div class="card"><h3>Реестры (снимок «Начало»)</h3>' +
        "<p>`$Файлы`: " + reg.files.total + " (" + reg.files.size + ") · RAW " + reg.files.raw +
        " · markup " + reg.files.markup + " · ONNX " + reg.files.onnx + "</p>" +
        "<p>Записи: " + reg.records.total + " (дубли " + reg.records.duplicates + ", без разметки " +
        reg.records.withoutMarkup + ", без RAW " + reg.records.withoutRaw + ")</p>" +
        "<p>`$Нагрузки`: " + reg.loads + " · `$Датасеты`: " + reg.datasets + " · `$Модели`: " + reg.models + "</p>" +
        "<p>`$Задачи`: " + reg.tasks.total + " (активных " + reg.tasks.active + ", архив " + reg.tasks.archive + ")</p>" +
        '<button class="act ghost" data-act="nav" data-id="SCR-04">Открыть данные стенда →</button></div></div>' +
      '<div><div class="card"><h3>Снимки и сравнение</h3>' +
        "<p><b>Начало:</b> " + esc(snap.begin.at) + " · файлы " + snap.begin.files + " · датасеты " +
        snap.begin.datasets + "</p>" +
        "<p><b>Окончание:</b> " + (snap.end ? esc(snap.end.at) : "— не снят") + "</p>" +
        "<p><b>Дельта:</b> " + esc(delta) + "</p>" +
        '<p class="hint">Ошибки снимка: нет — снимок полный (FR-P-6).</p>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: сравнение снимков выгружается в артефакты (FR-P-47)">Экспорт сравнения</button>' +
      "</div>" +
      '<div class="card"><h3>Что здесь не делается</h3>' +
        '<p class="hint">Настройки подключения — в «Инструментах» (SCR-13), наблюдение за обменами — ' +
        "в «Журнале обмена» (SCR-09). Этот экран подтверждает объект испытаний и фиксирует ресурсы.</p></div></div></div>";
  }

  /* ---------------------------------------------------- SCR-03. Сессия испытаний */
  function renderSession() {
    var s = DEMO.session;
    var history = [
      "10:11 сессия создана (Петров П.П.)",
      "10:12 снимок «Начало»: файлы 259, датасеты 8",
      "10:20 очередь построена: " + ST.queue.length + " пунктов",
      "11:05 замечание P1 создано (TC-DS-03)",
      "12:02 проверка TC-FILE-13 выполнена (live, с карточкой запуска)"
    ];
    return '<div class="grid3"><div>' +
      '<div class="card"><h3>Реквизиты (шапка отчёта)</h3>' +
        field("Наименование *", s.title) +
        field("Объект испытаний *", s.object) +
        field("Программа-методика *", s.method) +
        field("Заказчик", s.customer) +
        field("Испытательная организация", s.org) +
        field("Комиссия", s.commission) +
        field("Критерии оценки", s.criteria) +
        '<div class="row"><button class="act" data-act="toast" data-msg="Заглушка: реквизиты сохраняются в файл сессии (DR-P-1)">💾 Сохранить реквизиты</button>' +
        '<span class="hint">Обязательные поля заполнены</span></div></div>' +
      '<div class="card"><h3>История событий</h3><ul class="list">' +
        history.map(function (h) { return "<li>" + esc(h) + "</li>"; }).join("") + "</ul></div></div>" +
      '<div><div class="card"><h3>Состояние</h3>' +
        "<p>Статус: <b>" + esc(s.status) + "</b> · начало " + esc(s.started) + "</p>" +
        "<p>Обменов: " + ST.exchanges.length + " · проверок: " + stats().done + "/" + stats().total +
        " · снимков: " + (DEMO.snapshots.end ? 2 : 1) + "</p>" +
        '<div class="row"><button class="act ghost" data-act="finish-session">⏹ Завершить сессию</button>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: переоткрытие сессии фиксируется с причиной (FR-P-5)">↗ Переоткрыть</button></div></div>' +
      '<div class="card"><h3>Передача между машинами</h3>' +
        "<p>Файл сессии: <code>" + esc(s.id) + ".json</code> + артефакты</p>" +
        '<div class="row"><button class="act ghost" data-act="toast" data-msg="Заглушка: сессия выгружается файлом (FR-P-52)">⬇ Выгрузить</button>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: сессия загружается и продолжается (FR-P-52)">⬆ Загрузить</button></div>' +
      "</div>" +
      '<div class="card"><h3>Правила</h3><ul class="list">' +
        "<li>Одна сессия — один прогон: для повторной приёмки создаётся новая.</li>" +
        "<li>Завершённая сессия блокирует прогон до переоткрытия (FR-P-55).</li></ul></div></div></div>";
  }

  function field(label, value) {
    return "<p><b>" + esc(label) + "</b><br>" + esc(value) + "</p>";
  }

  /* ------------------------------------------------------ SCR-04. Данные стенда */
  function renderData() {
    var tabs = ["Записи", "$Файлы", "$Нагрузки", "$Датасеты", "$Модели"];
    var html = '<div class="tabs">' + tabs.map(function (t) {
      return '<button class="tab' + (ST.ui.dataTab === t ? " active" : "") + '" data-act="data-tab" data-id="' +
        esc(t) + '">' + esc(t) + "</button>";
    }).join("") + "</div>";
    if (ST.ui.dataTab === "Записи") {
      html += '<div class="grid3"><div class="card"><h3>Записи RAW + markup (' + DEMO.records.length +
        " показано из " + DEMO.registries.records.total + ")</h3>" +
        "<table><tr><th>Запись</th><th>Состав</th><th>Состояние</th><th>Источник и примечание</th></tr>" +
        DEMO.records.map(function (r) {
          return "<tr><td>" + esc(r.name) + "</td><td>" + esc(r.size) + "</td><td>" + esc(r.state) +
            '</td><td class="hint">' + esc(r.note) + "</td></tr>";
        }).join("") + "</table>" +
        '<div class="row"><button class="act ghost" data-act="toast" data-msg="Заглушка: CSV реестра записей (FR-P-7)">⬇ CSV</button>' +
        '<span class="hint">Правило объединения и уверенность показаны рядом с записью (FR-P-8).</span></div></div>' +
        '<div><div class="card"><h3>Выбранная запись: Antminer_S19</h3>' +
          "<p>RAW: <code>7f3a…</code> (2 107 657 231 Б, 21.08.2026)</p>" +
          "<p>markup: <code>91b2…</code> (12 МБ, 21.08.2026)</p>" +
          "<p>Уверенность объединения: <b>высокая</b> (имя + время импорта)</p>" +
          '<p class="hint">Актуальная версия: по времени импорта — сервер не отдаёт checksum/updated_at ' +
          "(замечание P1; источник решения — сервер)</p>" +
          '<div class="row"><button class="act ghost" data-act="toast" data-msg="Заглушка: скачивание RAW (крупный файл — с подтверждением, NFR-P-6)">⬇ RAW</button>' +
          '<button class="act ghost" data-act="toast" data-msg="Заглушка: скачивание markup">⬇ markup</button>' +
          '<button class="act ghost" data-act="nav" data-id="SCR-05">В прогон →</button></div></div>' +
        '<div class="card"><h3>Частичные данные</h3>' +
          '<p class="hint">108 записей с не-ASCII именами не разбираются из-за дефекта P0 — это видно ' +
          "здесь и зафиксировано замечанием.</p>" +
          '<button class="act ghost" data-act="nav" data-id="SCR-10">Смотреть замечание P0 →</button></div></div></div>';
    } else if (ST.ui.dataTab === "$Датасеты") {
      html += tableCard("`$Датасеты` (9)", ["id", "Название", "Тип", "Состав", "Источник"],
        [["ds-12", "__TEST__dataset-12", "GENERAL", "6 записей (наполнено пультом)", "пульт"],
         ["ds-09", "Antminer", "GENERAL", "состав недоступен (P1)", "—"],
         ["ds-07", "3Dпринтер", "GENERAL", "состав недоступен (P1)", "—"]]);
    } else if (ST.ui.dataTab === "$Модели") {
      html += tableCard("`$Модели` (6)", ["id", "Название", "Версия", "Метрики", "Источник"],
        [["m-3", "__TEST__model-3", "v2", "accuracy 0.93, f1 0.91", "сервер"],
         ["m-1", "Antminer-base", "v1", "accuracy 0.88", "сервер"]]);
    } else if (ST.ui.dataTab === "$Нагрузки") {
      html += tableCard("`$Нагрузки` (12)", ["id", "Название", "Устройства", "Фаза", "Источник"],
        [["l-4", "Antminer S19", "3 устройства", "ph_1", "сервер"],
         ["l-2", "Печь №4", "1 устройство", "ph_2", "сервер"]]);
    } else {
      html += tableCard("`$Файлы` (265)", ["id", "Имя", "Тип", "Размер", "Импорт"],
        [["7f3a", "Antminer_S19.raw.csv", "RAW", "2 107 657 231 Б", "21.08.2026"],
         ["91b2", "Antminer_S19.markup.json", "markup", "12 287 921 Б", "21.08.2026"],
         ["a105", "3Dпринтер.raw.csv", "RAW", "24 566 291 Б", "18.08.2026"],
         ["a106", "3Dпринтер.raw.csv", "RAW", "1 282 054 Б", "20.08.2026"]]);
    }
    return html;
  }

  function tableCard(title, head, rows) {
    return '<div class="card"><h3>' + title + "</h3><table><tr>" +
      head.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("") + "</tr>" +
      rows.map(function (r) {
        return "<tr>" + r.map(function (c) { return "<td>" + esc(c) + "</td>"; }).join("") + "</tr>";
      }).join("") + "</table>" +
      '<div class="row"><button class="act ghost" data-act="toast" data-msg="Заглушка: выгрузка реестра в CSV (FR-P-7)">⬇ CSV</button>' +
      '<span class="hint">Источник каждого факта подписан: сервер / пульт / оператор (IR-P-7).</span></div></div>';
  }

  /* --------------------------------------------- SCR-05. Прогон (рабочее место) */
  function queueHtml() {
    var items = queueItems();
    var filtered = items.filter(function (i) {
      if (ST.ui.group && i.group !== ST.ui.group) { return false; }
      if (ST.ui.status && i.status !== ST.ui.status) { return false; }
      if (ST.ui.search && (i.id + " " + i.title).toLowerCase().indexOf(ST.ui.search.toLowerCase()) < 0) { return false; }
      return true;
    });
    var next = nextItem();
    var rows = filtered.map(function (i) {
      var mark = "";
      if (next && next.id === i.id) { mark = " (следующая)"; }
      else if (i.status === "выполняется") { mark = " (выполняется)"; }
      else if (ST.lastRunId === i.id) { mark = " (последняя)"; }
      var cls = next && next.id === i.id ? " next" : (ST.lastRunId === i.id ? " last" : "");
      return '<div class="qitem' + cls + (i.enabled ? "" : " off") + '" data-act="select" data-id="' + i.id + '">' +
        '<span class="cid">' + icon(i.status) + " " + esc(i.id) + "</span>" +
        "<span>" + esc(i.title) + "</span>" +
        '<span class="km">' + esc(i.klass) + mark + (i.reason ? " · снят: " + esc(i.reason) : "") + "</span></div>";
    }).join("");
    return '<div class="card"><h3>Очередь программы (' + filtered.length + " из " + items.length + ")</h3>" +
      '<p class="hint">Программа: ревизия ' + ST.programme.revision + " · " +
      (ST.programme.status === "утверждён" ? "утверждена" : "черновик") +
      " (состав меняется на «Программе сессии», SCR-15 — здесь только чтение).</p>" +
      '<div class="row">' +
        '<select data-set="group"><option value="">все разделы</option>' +
          ["TC-SYS", "TC-FILE", "TC-REC", "TC-LOAD", "TC-TASK", "TC-DS", "TC-MOD", "TC-TR", "TC-INF", "TC-CLEAN"].map(function (g) {
            return '<option value="' + g + '"' + (ST.ui.group === g ? " selected" : "") + ">" + g + "</option>";
          }).join("") + "</select>" +
        '<select data-set="status"><option value="">любой статус</option>' +
          ["не выполнена", "успех", "отказ", "блокировано", "пропущена", "прервана"].map(function (s) {
            return '<option value="' + s + '"' + (ST.ui.status === s ? " selected" : "") + ">" + esc(s) + "</option>";
          }).join("") + "</select>" +
        '<input data-set="search" placeholder="поиск по id и названию" value="' + esc(ST.ui.search) + '">' +
      "</div>" +
      '<div class="queue">' + (rows || '<p class="hint">Ничего не найдено — измените фильтры.</p>') + "</div>" +
      (ST.ui.offFor ? offForm() : "") +
      '<div class="row"><button class="act ghost" data-act="screenshot">снять с причиной</button>' +
      '<button class="act ghost" data-act="nav" data-id="SCR-15">изменить программу →</button></div></div>';
  }

  function offForm() {
    var item = byId(ST.ui.offFor);
    return '<div class="banner"><b>Снятие пункта ' + esc(item.id) + " с очереди</b>" +
      '<p class="hint">Снятие фиксируется причиной и отражается в протоколе и отчёте (FR-P-15).</p>' +
      '<div class="row"><input data-set="offReason" placeholder="причина снятия" value="' + esc(ST.ui.offReason) + '">' +
      '<button class="act" data-act="off-save">снять с причиной</button>' +
      '<button class="act ghost" data-act="off-cancel">отмена</button></div></div>';
  }

  function feedHtml(rows) {
    var list = ST.exchanges.filter(function (x) { return !ST.ui.onlyErrors || x.s >= 400; });
    list = list.slice(Math.max(0, list.length - rows));
    var html = list.slice().reverse().map(function (x) {
      var cls = x.s >= 200 && x.s < 300 ? "" : (x.s >= 400 && x.s < 500 ? "warn" : "bad");
      return '<div class="xchg ' + cls + '">' +
        '<div class="xhead">' + statusBadge(x) + "<span>#" + x.seq + "</span><span>" + esc(x.label) + "</span>" +
        "<span>" + esc(x.m + " " + x.p) + "</span><span>" + x.ms + " мс</span>" +
        "<span>" + esc(x.verdict) + "</span></div>" +
        '<pre>ЗАПРОС (' + esc(ST.ui.req) + "): " + esc(x.req) + "</pre>" +
        '<pre>ОТВЕТ  (' + esc(ST.ui.res) + "): " + esc(x.res) + "</pre>" +
        '<div class="row"><button class="mini" data-act="curl" data-seq="' + x.seq + '">копировать curl</button>' +
        '<button class="mini" data-act="repeat" data-seq="' + x.seq + '">повторить с правками</button>' +
        '<button class="mini" data-act="note-from-exchange" data-seq="' + x.seq + '">замечание</button>' +
        '<button class="mini" data-act="nav" data-id="SCR-09">в журнал</button></div>' +
        (ST.ui.repeatFor === x.seq ? repeatForm(x) : "") + "</div>";
    }).join("");
    return html || '<p class="hint">Обменов пока нет: нажмите «Пуск» — в ленте появится запрос и ответ (FR-P-21).</p>';
  }

  function repeatForm(x) {
    return '<div class="banner"><b>Повтор с правками (#' + x.seq + ")</b>" +
      '<div class="row"><input data-set="repeatPath" value="' + esc(ST.ui.repeatPath || x.p) + '" size="60"></div>' +
      '<textarea data-set="repeatBody" rows="3">' + esc(ST.ui.repeatBody || x.req) + "</textarea>" +
      '<div class="row"><button class="act" data-act="repeat-send" data-seq="' + x.seq + '">отправить повтор</button>' +
      '<button class="act ghost" data-act="repeat-cancel">отмена</button></div></div>';
  }

  function runCardHtml(item) {
    return '<div class="banner"><b>Карточка запуска: ' + esc(item.id) + " (" + esc(klassLabel(item.klass)) + ")</b>" +
      '<p class="hint">Изменяющая/ресурсоёмкая проверка: без заполненных полей и подтверждения пуск невозможен ' +
      "(FR-P-19, NFR-P-5).</p>" +
      '<div class="grid2">' +
        '<div><input data-set="card-goal" placeholder="Цель проверки *" value="' + esc(ST.ui["card-goal"] || "") + '">' +
        '<p><input data-set="card-data" placeholder="Используемые данные *" value="' + esc(ST.ui["card-data"] || "") + '"></p>' +
        '<input data-set="card-params" placeholder="Параметры запуска" value="' + esc(ST.ui["card-params"] || "") + '"></div>' +
        '<div><input data-set="card-responsible" placeholder="Ответственный *" value="' + esc(ST.ui["card-responsible"] || "") + '">' +
        '<p><select data-set="card-artifacts">' +
          ["удалить после проверки", "оставить как доказательство", "решение испытателя"].map(function (o) {
            return '<option' + ((ST.ui["card-artifacts"] || "удалить после проверки") === o ? " selected" : "") + ">" + o + "</option>";
          }).join("") + "</select></p>" +
        '<label><input type="checkbox" data-act="card-confirm"' + (ST.ui["card-confirm"] ? " checked" : "") + "> " +
        "подтверждаю расход ресурсов</label></div>" +
      "</div>" +
      '<div class="row"><button class="act" data-act="card-run" data-id="' + item.id + '">▶ Подтвердить и запустить</button>' +
      '<button class="act ghost" data-act="card-cancel">отмена</button></div></div>';
  }

  function controlsHtml() {
    var next = nextItem();
    var selected = byId(ST.selectedId) || next;
    var html = '<div class="card"><h3>Управление</h3>';
    if (!next) {
      html += '<p class="hint">Программа пройдена: осталось оформить протокол и отчёт.</p>';
    } else {
      html += "<p>Следующая: <b>" + esc(next.id) + "</b> — «" + esc(next.title) + "» (" + esc(klassLabel(next.klass)) + ")</p>";
    }
    html += '<p class="hint">Режим: сценарием проверки целиком (по умолчанию). Пауза авто-прогона: ' +
      '<input data-set="pause" type="number" min="0.5" max="120" step="0.5" value="' + ST.auto.pause + '" size="4"> с</p>' +
      '<div class="row">' +
        '<button class="act" data-act="run-next"' + (next ? "" : " disabled") + ">▶ Следующая" +
        (next ? ": " + esc(next.id) : "") + "</button>" +
        '<button class="act ghost" data-act="run-batch"' + (next ? "" : " disabled") + ">▶▶ Пачка tech</button>" +
        '<button class="act ghost" data-act="auto">' + (ST.auto.on ? "⏸ Стоп авто" : "⏯ Авто с паузой") + "</button>" +
      "</div>" +
      '<p class="hint">Команда запускает ровно один пункт — следующий по программе, и называет его ' +
      "(IR-P-4, IR-P-20). Обойти пункт нельзя: только «снять с причиной» или новая ревизия программы.</p>" +
      '<div class="kpi" style="margin-top:8px">' + cell("Выполнено", stats().done) + cell("Успех", stats().success) +
      cell("Отказ", stats().fail) + cell("Блокировано", stats().blocked) + "</div>";
    if (ST.ui.runCard) {
      html += runCardHtml(byId(ST.ui.runCard));
    } else if (next && needsCard(next)) {
      html += '<p class="hint">⚠️ Следующий пункт класса <code>' + esc(next.klass) +
        "</code>: при «Пуске» потребуется карточка запуска.</p>";
    }
    if (selected) {
      html += '<p class="hint">Выбран пункт: <b>' + esc(selected.id) + '</b> — «' + esc(selected.title) +
        '». Открыть подробности: <button class="mini" data-act="nav" data-id="SCR-06">карточка проверки</button></p>';
    }
    return html + "</div>";
  }

  function renderRun() {
    var rowsOptions = [4, 8, 25, 100];
    var feedControls = '<div class="row"><span class="hint">Подробность запроса:</span>' +
      '<select data-set="req">' + ["кратко", "сводка", "заголовки", "тело", "полностью"].map(function (l) {
        return '<option' + (ST.ui.req === l ? " selected" : "") + ">" + l + "</option>";
      }).join("") + "</select>" +
      '<span class="hint">Подробность ответа:</span>' +
      '<select data-set="res">' + ["кратко", "сводка", "заголовки", "тело", "полностью"].map(function (l) {
        return '<option' + (ST.ui.res === l ? " selected" : "") + ">" + l + "</option>";
      }).join("") + "</select>" +
      '<label><input type="checkbox" data-act="only-errors"' + (ST.ui.onlyErrors ? " checked" : "") + "> только ошибки</label>" +
      '<select data-set="feedRows">' + rowsOptions.map(function (n) {
        return '<option value="' + n + '"' + (ST.ui.feedRows === n ? " selected" : "") + ">" + n + " записей</option>";
      }).join("") + "</select></div>";
    return '<div class="grid3"><div>' + queueHtml() + "</div><div>" + controlsHtml() +
      '<div class="card"><h3>Лента обмена</h3>' + feedControls +
      '<div class="feed">' + feedHtml(ST.ui.feedRows) + "</div>" +
      '<p class="hint">Запрос и ответ показаны по отдельности; подробность выбирается независимо (FR-P-21, IR-P-13).</p>' +
      "</div></div></div>";
  }

  /* --------------------------------------------- SCR-06. Карточка проверки */
  function renderCheckCard() {
    var item = byId(ST.selectedId) || nextItem() || ST.queue[0];
    if (!item) { return '<div class="banner">Каталог проверок пуст.</div>'; }
    var steps = item.calls.map(function (c, index) {
      return "<li><code>" + esc((index + 1) + ". " + c.m.toUpperCase() + " " + c.p) + "</code></li>";
    }).join("");
    var own = ST.exchanges.filter(function (x) { return x.label === item.id; });
    var range = own.length ? "#" + own[0].seq + "–#" + own[own.length - 1].seq : "—";
    return '<div class="grid3"><div>' +
      '<div class="card"><h3>' + esc(item.id + ". " + item.title) + "</h3>" +
        "<p>Класс: <b>" + esc(klassLabel(item.klass)) + "</b> · Модуль: " + esc(item.group) +
        " · Требования: " + esc(item.req) + "</p>" +
        "<p>Подтверждение перед пуском: <b>" + (needsCard(item) ? "обязательно (карточка запуска)" : "не требуется") + "</b></p>" +
        "<p>Ожидаемый результат: " + esc(item.expected) + "</p>" +
        "<p>Используется в программе: <b>" + (sourcesOf(item.id).length ? esc(sourcesOf(item.id).join(", ")) : "вне программы") +
        "</b> · ревизия программы: " + ST.programme.revision + "</p>" +
        '<p class="hint">Команда запуска — «Следующая» на экране «Прогон» (SCR-05): правило одной команды (IR-P-4).</p>' +
        '<div class="row"><button class="act ghost" data-act="nav" data-id="SCR-05">К прогону →</button>' +
        '<button class="act ghost" data-act="next-check">→ следующий пункт</button></div></div>' +
      '<div class="card"><h3>Шаги проверки</h3><ol class="list">' + steps + "</ol>" +
        '<p class="hint">Каждый вызов помечается меткой ' + esc(item.id) + " и виден в журнале обмена.</p></div></div>" +
      '<div><div class="card"><h3>Что будет отправлено (FR-P-20)</h3>' +
        "<p>Payload: " + esc(item.payload || "не создаётся (проверка только для чтения)") + "</p>" +
        "<p>Имена и тела формирует пульт: префикс <code>__TEST__</code>, уборка после проверки.</p>" +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: предпросмотр содержимого файла и тела запроса">Проверить перед запуском</button></div>' +
      '<div class="card"><h3>След прогона (лента внутри карточки)</h3>' +
        "<p>Диапазон журнала: <b>" + esc(range) + "</b> · обменов: " + own.length + "</p>" +
        '<div class="feed">' + (own.length ? feedHtml(own.length) : '<p class="hint">Проверка ещё не выполнялась.</p>') + "</div></div>" +
      '<div class="card"><h3>Доказательства</h3>' +
        "<p>Статус: " + esc(icon(item.status) + " " + item.status) + " · Вердикт: " + esc(item.verdict || "—") + "</p>" +
        "<p>Длительность: " + (item.ms === null ? "—" : esc(item.ms) + " мс") + " · Журнал: " + esc(item.journal || range) + "</p>" +
        '<p class="hint">Созданные `$сущности`, размеры и метрики попадают в доказательства (FR-P-33).</p>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: доказательства выгружаются в артефакты сессии">💾 В артефакты</button></div></div></div>';
  }

  /* ------------------------------------------------------- SCR-07. $Задачи */
  function renderTasks() {
    var tabs = ["Наблюдение", "Список", "Архив", "Диагностика"];
    var html = '<div class="tabs">' + tabs.map(function (t) {
      return '<button class="tab' + (ST.ui.taskTab === t ? " active" : "") + '" data-act="task-tab" data-id="' +
        esc(t) + '">' + esc(t) + "</button>";
    }).join("") + "</div>";
    if (ST.ui.taskTab === "Наблюдение") {
      html += '<div class="grid3"><div class="card"><h3>Наблюдение (' + DEMO.tasks.length + ")</h3>" +
        "<table><tr><th>id</th><th>Тип</th><th>Объект</th><th>Статус</th><th>Прошло</th><th>Команды</th></tr>" +
        DEMO.tasks.map(function (t) {
          return "<tr><td><code>" + esc(t.id) + "</code></td><td>" + esc(t.type) + "</td><td>" + esc(t.target) +
            "</td><td>" + esc(t.status) + "</td><td>" + esc(t.elapsed) + '</td><td class="hint">' +
            esc(t.actions.length ? t.actions.join(", ") : "—") + "</td></tr>";
        }).join("") + "</table>" +
        '<div class="row"><button class="act ghost" data-act="poll">⟳ опросить все сейчас</button>' +
        '<span class="hint">интервал опроса 2.0 с (настройка — SCR-13)</span></div>' +
        '<p class="hint">Команды берутся только из списка, разрешённого сервером (FR-P-25).</p></div>' +
        '<div><div class="card"><h3>Команды: задача 5f1d (training)</h3>' +
          "<p>Статус: <b>running</b> · доступно: pause, interrupt</p>" +
          '<div class="row"><button class="act ghost" data-act="task-cmd" data-id="5f1d" data-cmd="pause">⏸ пауза</button>' +
          '<button class="act ghost" data-act="task-cmd" data-id="5f1d" data-cmd="interrupt">⛔ прервать</button>' +
          '<button class="act ghost" data-act="task-cmd" data-id="c4d0" data-cmd="resume">▶ возобновить (c4d0)</button></div>' +
          '<p class="hint">Запрещённая команда не отправляется: пульт объясняет причину.</p></div>' +
        '<div class="card"><h3>История FSM-1 (5f1d)</h3><ul class="list">' +
          DEMO.tasks[0].history.map(function (h) {
            return "<li>" + esc(h[0] + ": " + h[1] + " → " + h[2] + " (журнал #" + h[3] + ")") + "</li>";
          }).join("") + "</ul>" +
          '<p class="hint">История сохраняется при перезапуске пульта и идёт в отчёт (FR-P-26).</p></div></div></div>';
    } else if (ST.ui.taskTab === "Диагностика") {
      html += '<div class="card"><h3>Диагностическая задача celery-test</h3>' +
        "<p>Дешёвый способ проверить очередь, FSM-1 и команды без расхода ресурсов обучения.</p>" +
        '<p class="hint">Операция изменяет состояние стенда: требуется подтверждение (NFR-P-5).</p>' +
        '<div class="row"><label>длительность, с <input data-set="diag" type="number" min="1" max="300" value="30" size="4"></label>' +
        '<label><input type="checkbox" data-act="diag-confirm"> подтверждаю запуск</label>' +
        '<button class="act" data-act="diag-run">🩺 запустить celery-test</button></div></div>';
    } else {
      html += tableCard(ST.ui.taskTab === "Архив" ? "Архив (211)" : "Список `$Задач` (последние 4)",
        ["id", "Тип", "Название", "Создана", "Статус и источник"],
        [["5f1d", "training", "Обучение __TEST__model-3", "21.09 11:41", "running (карточка)"],
         ["7a2e", "dataset-fill", "Наполнение ds-12", "21.09 11:58", "paused (опрос)"],
         ["91bc", "celery-test", "Диагностика", "21.09 11:40", "completed (снимок)"],
         ["c4d0", "model-testing", "Проверка m-3", "21.09 11:20", "not_found (карточка)"]]);
    }
    return html;
  }

  /* ---------------------------------------------------- SCR-08. Протокол */
  function filteredQueue() {
    return ST.queue.filter(function (i) {
      if (ST.ui.group && i.group !== ST.ui.group) { return false; }
      if (ST.ui.status && i.status !== ST.ui.status) { return false; }
      if (ST.ui.search && (i.id + " " + i.title).toLowerCase().indexOf(ST.ui.search.toLowerCase()) < 0) { return false; }
      return true;
    });
  }

  function renderProtocol() {
    var rows = filteredQueue();
    var s = statsOf(rows);
    var html = '<div class="grid3"><div class="card"><h3>Протокол (' + rows.length + " из " + ST.queue.length + ")</h3>" +
      '<div class="row">' +
        '<select data-set="group"><option value="">все группы</option>' +
          ["TC-SYS", "TC-FILE", "TC-REC", "TC-LOAD", "TC-TASK", "TC-DS", "TC-MOD", "TC-TR", "TC-INF", "TC-CLEAN"].map(function (g) {
            return '<option value="' + g + '"' + (ST.ui.group === g ? " selected" : "") + ">" + g + "</option>";
          }).join("") + "</select>" +
        '<select data-set="status"><option value="">любой статус</option>' +
          ["не выполнена", "успех", "отказ", "блокировано", "пропущена", "прервана"].map(function (st) {
            return '<option value="' + st + '"' + (ST.ui.status === st ? " selected" : "") + ">" + esc(st) + "</option>";
          }).join("") + "</select>" +
        '<input data-set="search" placeholder="поиск" value="' + esc(ST.ui.search) + '">' +
      "</div>" +
      "<table><tr><th>ID</th><th>Класс</th><th>Статус</th><th>Вердикт</th><th>Журнал</th><th>Программа</th><th>Причина снятия</th></tr>" +
      rows.map(function (i) {
        var src = sourcesOf(i.id);
        return '<tr class="' + (ST.ui.markFor === i.id ? "sel" : "") + '"><td><code>' + esc(i.id) + "</code></td><td>" +
          esc(i.klass) + "</td><td>" + esc(icon(i.status) + " " + i.status) + "</td><td>" + esc(i.verdict || "—") +
          "</td><td>" + esc(i.journal || "—") + '</td><td class="hint">' +
          (src.length ? esc(src.join(", ")) : "вне программы") +
          '</td><td class="hint">' + esc(i.reason || "—") + "</td></tr>";
      }).join("") + "</table>" +
      '<div class="row"><button class="act ghost" data-act="toast" data-msg="Заглушка: CSV протокола (14 колонок, FR-P-34)">⬇ CSV</button>' +
      '<button class="act ghost" data-act="toast" data-msg="Заглушка: протокол сохраняется в артефакты сессии">💾 В артефакты</button>' +
      '<span class="hint">Команд запуска здесь нет: единственная команда — «Следующая» на «Прогоне» (IR-P-4).</span></div>' +
      (ST.ui.markFor ? markForm() : "") + "</div>" +
      '<div><div class="card"><h3>KPI выборки</h3><div class="kpi">' +
        cell("В выборке", rows.length) + cell("Выполнено", s.done) + cell("Успех", s.success) +
        cell("Отказ", s.fail) + cell("Блокировано", s.blocked) + cell("Не выполнено", s.pending) + "</div>" +
        '<p class="hint">Готовность протокола: ' + Math.round(100 * s.done / (s.total || 1)) + " %</p>" +
        '<div class="row"><button class="act" data-act="mark-start" data-id="' + esc(ST.selectedId || (nextItem() ? nextItem().id : "")) +
        '">🖐 Ручная отметка</button>' +
        '<button class="act ghost" data-act="nav" data-id="SCR-15">Программа: ревизия ' + ST.programme.revision + " →</button></div></div>" +
      '<div class="card"><h3>Правила протокола</h3><ul class="list">' +
        "<li>Ручная отметка без заключения не принимается (FR-P-32).</li>" +
        "<li>Снятые с причиной и «вне программы» видны раздельно (FR-P-35).</li>" +
        "<li>Повтор проверки сохраняет предыдущий результат как историю (FR-P-36).</li></ul></div></div></div>";
    return html;
  }

  function markForm() {
    var item = byId(ST.ui.markFor);
    return '<div class="banner"><b>Ручная отметка: ' + esc(item.id) + "</b>" +
      '<div class="row"><select data-set="markStatus">' +
        ["выполнена вручную", "пропущена", "блокировано", "прервана"].map(function (st) {
          return '<option' + (ST.ui.markStatus === st ? " selected" : "") + ">" + esc(st) + "</option>";
        }).join("") + "</select></div>" +
      '<textarea data-set="markNote" rows="2" placeholder="заключение оператора (обязательно)">' + esc(ST.ui.markNote) + "</textarea>" +
      '<div class="row"><button class="act" data-act="mark-save">записать отметку</button>' +
      '<button class="act ghost" data-act="mark-cancel">отмена</button></div></div>';
  }

  /* ----------------------------------------------------- SCR-09. Журнал обмена */
  function renderJournal() {
    var list = ST.exchanges.filter(function (x) { return !ST.ui.onlyErrors || x.s >= 400; });
    return '<div class="grid3"><div class="card"><h3>Журнал обмена (' + list.length + " из " + ST.exchanges.length + ")</h3>" +
      '<div class="row"><label><input type="checkbox" data-act="only-errors"' + (ST.ui.onlyErrors ? " checked" : "") +
      "> только ошибки</label>" +
      '<select data-set="label"><option value="">все метки</option>' + ST.queue.map(function (i) {
        return "<option" + (ST.ui.label === i.id ? " selected" : "") + ">" + esc(i.id) + "</option>";
      }).join("") + "</select>" +
      '<input data-set="search" placeholder="подстрока в пути" value="' + esc(ST.ui.search) + '">' +
      '<button class="act ghost" data-act="toast" data-msg="Заглушка: выгрузка JSONL сессии (FR-P-57)">⬇ JSONL</button></div>' +
      "<table><tr><th>#</th><th>Время</th><th>Метка</th><th>Запрос</th><th>Статус</th><th>мс</th></tr>" +
      list.slice().reverse().map(function (x) {
        var cls = x.s >= 200 && x.s < 300 ? "ok" : (x.s >= 400 && x.s < 500 ? "warn" : "err");
        return '<tr><td>#' + x.seq + "</td><td>" + esc(x.ts) + "</td><td><code>" + esc(x.label) + "</code></td><td>" +
          esc(x.m + " " + x.p) + '</td><td><span class="badge ' + cls + '">' + x.s + "</span></td><td>" + x.ms + "</td></tr>";
      }).join("") + "</table></div>" +
      '<div class="card"><h3>Детали обмена</h3>' + feedHtml(3) +
      '<p class="hint">Тела усечены лимитом 4 096 символов с пометкой (NFR-P-4).</p></div></div>';
  }

  /* ------------------------------------------- SCR-10. Замечания к API */
  function renderNotes() {
    var tabNotes = ST.ui.notesTab === "Перспективные";
    var html = '<div class="tabs">' + ["Замечания", "Перспективные"].map(function (t) {
      return '<button class="tab' + (ST.ui.notesTab === t ? " active" : "") + '" data-act="notes-tab" data-id="' +
        esc(t) + '">' + esc(t) + "</button>";
    }).join("") + "</div>";
    if (tabNotes) {
      return html + '<div class="card"><h3>Перспективные требования к API (' + DEMO.prospective.length + ")</h3>" +
        "<table><tr><th>Требование</th><th>Зачем</th></tr>" +
        DEMO.prospective.map(function (p) {
          return "<tr><td>" + esc(p.title) + '</td><td class="hint">' + esc(p.why) + "</td></tr>";
        }).join("") + "</table>" +
        '<p class="hint">Перспективные требования не влияют на статусы проверок (FR-P-39).</p></div>';
    }
    return html + '<div class="grid3"><div class="card"><h3>Реестр замечаний (' + DEMO.notes.length + ")</h3>" +
      '<div class="row">' +
        '<select data-set="priority"><option value="">все приоритеты</option>' +
          ["P0", "P1", "P2"].map(function (p) {
            return '<option' + (ST.ui.priority === p ? " selected" : "") + ">" + p + "</option>";
          }).join("") + "</select>" +
        '<select data-set="module"><option value="">все модули</option>' +
          ["File Import", "Datasets", "Система", "Task service"].map(function (m) {
            return '<option' + (ST.ui.module === m ? " selected" : "") + ">" + esc(m) + "</option>";
          }).join("") + "</select>" +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: выгрузка реестра замечаний в MD/JSON/CSV (FR-P-40)">📤 Комплект</button></div>' +
      "<table><tr><th>Приоритет</th><th>Проверка</th><th>Замечание</th><th>Статус</th></tr>" +
      DEMO.notes.map(function (n) {
        var cls = n.priority === "P0" ? "err" : (n.priority === "P1" ? "warn" : "");
        return '<tr><td><span class="badge ' + cls + '">' + esc(n.priority) + "</span></td><td><code>" + esc(n.check) +
          "</code></td><td>" + esc(n.title) + "</td><td>" + esc(n.status) + "</td></tr>";
      }).join("") + "</table>" +
      '<p class="hint">Отказ и блокировка создают замечание автоматически (FR-P-31); без воспроизведения ' +
      "замечание блокирует готовность отчёта (FR-P-48).</p></div>" +
      '<div><div class="card"><h3>N-01 · P0 · в работе</h3>' +
        "<p>Проверка: <code>TC-FILE-10</code> · Модуль: File Import</p>" +
        "<p>Эндпоинт: <code>GET /api/data/file/{id}/download</code></p>" +
        "<p>Факт: " + esc(DEMO.notes[0].fact) + "</p>" +
        "<p>Ожидание: " + esc(DEMO.notes[0].expected) + "</p>" +
        "<p>Воспроизведение: <code>" + esc(DEMO.notes[0].repro) + "</code></p>" +
        "<p>Доказательства: обмены " + esc(DEMO.notes[0].evidence) + " · блокирует проверок: " + DEMO.notes[0].blocks + "</p>" +
        '<div class="row"><button class="act" data-act="regress" data-id="N-01">🔁 Проверить исправление (перепрогон)</button>' +
        '<button class="act ghost" data-act="note-close" data-id="N-01">✅ Закрыть (с подтверждением)</button>' +
        '<button class="act ghost" data-act="nav" data-id="SCR-12">Сравнить сессии →</button></div></div>' +
      '<div class="card"><h3>Правила</h3><ul class="list">' +
        "<li>Замечание содержит факт, ожидание и воспроизведение (FR-P-37).</li>" +
        "<li>Связи «замечание ↔ проверка ↔ обмены» сохраняются (FR-P-38).</li>" +
        "<li>Закрытие требует подтверждения воспроизведением (FR-P-44).</li></ul></div></div></div>";
  }

  /* ------------------------------------------------- SCR-11. Отчёт испытаний */
  function renderReport() {
    var r = readiness(), s = stats();
    var sections = [
      "1 Сессия и реквизиты", "2 Программа и объём", "3 Снимки и сравнение", "4 Сводка KPI",
      "5 Детализация проверок", "6 `$Задачи` и FSM-1", "7 Данные и состав", "8 Артефакты и `__TEST__`",
      "9 Замечания P0/P1/P2", "10 Перспективные требования", "11 Повторные прогоны", "12 Итог и подписи",
      "13 Приложения"
    ];
    return '<div class="grid3"><div>' +
      '<div class="card"><h3>Шапка отчёта</h3>' +
        "<p>Сессия: " + esc(DEMO.session.id) + " · " + esc(DEMO.session.title) + "</p>" +
        "<p>Объект: " + esc(DEMO.stand.build) + " · стенд: " + esc(DEMO.stand.baseUrl) + "</p>" +
        "<p>Период: " + esc(DEMO.session.started) + " — " +
        (DEMO.snapshots.end ? esc(DEMO.snapshots.end.at) : "не завершена") + "</p>" +
        "<p>Проверок: " + s.done + "/" + s.total + " · замечаний открыто: " + openNotes().length + "</p></div>" +
      '<div class="card"><h3>Разделы комплекта</h3><ul class="list">' +
        sections.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul></div></div>" +
      '<div><div class="card"><h3>Готовность: ' + r.percent + " %</h3>" +
        (r.missing.length
          ? '<ul class="list">' + r.missing.map(function (m) { return "<li>⛔ " + esc(m) + "</li>"; }).join("") + "</ul>"
          : '<p class="hint">✅ все условия выполнены</p>') +
        '<div class="row"><button class="act" data-act="toast" data-msg="Заглушка: комплект report.md + report.json (FR-P-49)">⬇ Комплект (md + json)</button>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: приложения checks.csv, notes.csv, test_entities.csv, plan.csv, journal.jsonl">⬇ Приложения CSV</button>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: комплект сохраняется в артефакты сессии">💾 В артефакты</button></div>' +
        '<p class="hint">Отчёт формируется по данным сессии и журнала, без обращений к стенду (FR-P-49).</p></div>' +
      '<div class="card"><h3>Итоговое решение</h3>' +
        '<div class="row"><select data-set="decision">' +
          ["годен", "годен с замечаниями", "не годен", "не определён"].map(function (d) {
            return '<option' + (ST.ui.decision === d ? " selected" : "") + ">" + esc(d) + "</option>";
          }).join("") + "</select></div>" +
        '<p class="hint">Критерии: ' + esc(DEMO.session.criteria) + ". Подписи: " + esc(DEMO.session.operator) + "</p>" +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: решение и подписи фиксируются в сессии (FR-P-51)">✍ Внести подписи</button></div></div></div>';
  }

  /* ---------------------------------------- SCR-12. Сравнение сессий и сборок */
  function renderCompare() {
    var changed = ST.queue.filter(function (i) {
      return DEMO.initialStatus[i.id] && DEMO.initialStatus[i.id][0] !== i.status;
    });
    var rows = changed.length ? changed : ST.queue.slice(0, 5).map(function (i) {
      return { id: i.id, title: i.title, status: i.status };
    });
    return '<div class="grid3"><div><div class="card"><h3>Что сравниваем</h3>' +
      "<p>Базовая сессия: <b>20260918-140006</b> · сборка <code>dev@58ac72f</code></p>" +
      "<p>Текущая сессия: <b>" + esc(DEMO.session.id) + "</b> · сборка <code>" + esc(DEMO.stand.build) + "</code></p>" +
      '<div class="row"><button class="act ghost" data-act="toast" data-msg="Заглушка: выбор другой пары сессий">⇄ Поменять</button>' +
      '<button class="act ghost" data-act="toast" data-msg="Заглушка: дельта выгружается в CSV">⬇ Дельта CSV</button>' +
      '<button class="act ghost" data-act="regress" data-id="all">🔁 В регресс</button></div>' +
      '<p class="hint">Регресс собирает затронутые группы и смоук-набор для новой сборки (FR-P-41).</p></div>' +
      '<div class="card"><h3>Изменившие результаты проверки (' + rows.length + ")</h3>" +
      "<table><tr><th>Проверка</th><th>Было</th><th>Стало</th></tr>" +
      rows.map(function (i) {
        var was = DEMO.initialStatus[i.id] ? DEMO.initialStatus[i.id][0] : "не выполнена";
        return "<tr><td><code>" + esc(i.id) + "</code></td><td>" + esc(was) + "</td><td>" + esc(i.status) + "</td></tr>";
      }).join("") + "</table></div></div>" +
      '<div><div class="card"><h3>Дельта замечаний</h3><ul class="list">' +
        "<li>✅ TC-SYS-03: 405 → 404 (исправлено)</li>" +
        "<li>⏳ TC-FILE-10: остаётся открытым (P0)</li>" +
        "<li>⏳ TC-DS-03: остаётся открытым (P1)</li>" +
        "<li>➕ Замечание P2 по формату ошибок (новое)</li></ul>" +
        '<button class="act ghost" data-act="nav" data-id="SCR-10">К замечаниям →</button></div>' +
      '<div class="card"><h3>Дельта реестров</h3>' +
        "<p>`$Файлы` +6 · `$Датасеты` +2 · `$Модели` +1 · `$Задачи` +7</p>" +
        '<p class="hint">Изменение объясняется созданными `__TEST__`-сущностями и задачами прогона (FR-P-47).</p></div></div></div>';
  }

  /* ----------------------------------------------------- SCR-13. Инструменты */
  function renderTools() {
    var tabs = ["Консоль", "Диагностика", "Настройки", "Журналы запусков", "Справка", "Словарь"];
    var html = '<div class="tabs">' + tabs.map(function (t) {
      return '<button class="tab' + (ST.ui.toolsTab === t ? " active" : "") + '" data-act="tools-tab" data-id="' +
        esc(t) + '">' + esc(t) + "</button>";
    }).join("") + "</div>";
    if (ST.ui.toolsTab === "Консоль") {
      html += '<div class="grid3"><div class="card"><h3>Произвольный вызов операции</h3>' +
        '<div class="row">' +
          '<select data-set="consoleModule">' + ["File Import", "Datasets", "Loads", "ML models", "Task service"].map(function (m) {
            return "<option" + (ST.ui.consoleModule === m ? " selected" : "") + ">" + esc(m) + "</option>";
          }).join("") + "</select>" +
          '<select data-set="consoleOp">' + ["POST /api/data/file", "GET /api/data/files", "DELETE /api/{file_id}"].map(function (o) {
            return "<option" + (ST.ui.consoleOp === o ? " selected" : "") + ">" + esc(o) + "</option>";
          }).join("") + "</select>" +
          '<span class="badge warn">класс: write</span></div>' +
        '<p class="hint">Реестр операций берётся из контракта: 40 операций, 10 модулей (AR-P-2).</p>' +
        "<textarea rows=\"3\">{ \"file_type\": \"RAW\" }</textarea>" +
        '<div class="row"><label><input type="checkbox" checked> префикс `__TEST__` имён</label>' +
        '<button class="act ghost" data-act="toast" data-msg="Предпросмотр: имя файла __TEST__console_2f6a.csv, тело запроса (AR-P-4)">⚙ Предпросмотр</button>' +
        '<button class="act" data-act="console-send">↗ Отправить</button></div></div>' +
        '<div class="card"><h3>Результат</h3>' + feedHtml(2) +
        '<p class="hint">Вызов консоли помечается как инструмент и не меняет статусы проверок, ' +
        "пока оператор не привяжет его к пункту.</p></div></div>" +
        '<div class="card"><h3>Правила безопасности</h3><ul class="list">' +
        "<li>Изменяющие операции — с предпросмотром и подтверждением (IR-P-11).</li>" +
        "<li>Удаляющие — с подтверждением идентификатора (IR-P-12).</li>" +
        "<li>Повтор разрешён только для чтения.</li></ul></div></div>";
    } else if (ST.ui.toolsTab === "Диагностика") {
      html += '<div class="card"><h3>Проверка готовности интеграции (AR-P-10)</h3>' +
        "<table><tr><th>Пункт</th><th>Результат</th></tr>" +
        [["Связь со стендом", "✅ 42 мс"], ["GET /health", "✅ 200"], ["GET /version", "✅ 200 · dev@83319ae"],
         ["Запись (тестовый файл)", "✅ 201 (создан и удалён)"], ["Очередь `$Задач`", "✅ 4 активных"],
         ["Изолированный контур", "✅ test-stand/acceptance"]].map(function (row) {
          return "<tr><td>" + esc(row[0]) + "</td><td>" + esc(row[1]) + "</td></tr>";
        }).join("") + "</table>" +
        '<div class="row"><button class="act" data-act="toast" data-msg="Заглушка: повторная диагностика выполнена">🔄 Повторить диагностику</button></div></div>';
    } else if (ST.ui.toolsTab === "Настройки") {
      html += '<div class="grid2"><div class="card"><h3>Подключение</h3>' +
        "<p>Адрес стенда: <code>" + esc(DEMO.stand.baseUrl) + "</code></p>" +
        "<p>Таймаут запросов: 15 с · скачивания: 600 с</p>" +
        "<p>Порог крупного файла: 200 МБ (требует подтверждения)</p></div>" +
        '<div class="card"><h3>Журналирование и опрос</h3>' +
        "<p>Уровень журнала: INFO · тела: сохранять (лимит 4 096 символов)</p>" +
        "<p>Интервал опроса `$Задач`: 2.0 с</p>" +
        "<p>Квоты: обучение 2 запуска в сутки, инференс 10</p>" +
        '<div class="row"><button class="act" data-act="toast" data-msg="Заглушка: настройки сохраняются в .env (FR-P-61)">💾 Сохранить</button>' +
        '<button class="act ghost" data-act="toast" data-msg="Заглушка: сброс к значениям .env">↺ Сброс</button></div></div></div>';
    } else if (ST.ui.toolsTab === "Журналы запусков") {
      html += tableCard("Журналы запусков (FR-P-58)", ["Файл", "Размер", "Начало", ""],
        [["applog_20260921-101005.log", "1.2 МБ", "21.09 10:10", "текущий запуск"],
         ["applog_20260920-090210.log", "3.4 МБ", "20.09 09:02", "скачать"],
         ["applog_20260918-140006.log", "2.8 МБ", "18.09 14:00", "скачать"]]);
    } else if (ST.ui.toolsTab === "Справка") {
      html += '<div class="card"><h3>Как работать с пультом</h3><ol class="list">' +
        "<li>Подготовка: стенд, сессия, данные (SCR-01…SCR-04).</li>" +
        "<li>Прогон: очередь и «Пуск», наблюдение запроса и ответа (SCR-05).</li>" +
        "<li>Оценка: вердикты, доказательства, протокол (SCR-06, SCR-08).</li>" +
        "<li>Разбор: замечания и регресс (SCR-10, SCR-12).</li>" +
        "<li>Финализация: уборка, снимок, отчёт, решение (SCR-11).</li></ol>" +
        '<p class="hint">Правила гигиены: только `__TEST__`-сущности, подтверждения, «дефект = результат испытаний».</p></div>';
    } else {
      html += '<div class="card"><h3>Словарь терминов</h3>' +
        "<table><tr><th>Термин</th><th>Значение</th></tr>" +
        [["$Файл, $Датасет, $Модель, $Задача", "сущности испытываемого сервера (приходят и уходят по API)"],
         ["проверка, пункт очереди, обмен", "внутренние сущности пульта"],
         ["вердикт", "суждение о соответствии факта ожиданию"],
         ["доказательство", "данные, по которым результат воспроизводим"],
         ["гейт", "условие перехода между фазами процесса (G0–G4)"]].map(function (row) {
          return "<tr><td>" + esc(row[0]) + '</td><td class="hint">' + esc(row[1]) + "</td></tr>";
        }).join("") + "</table></div>";
    }
    return html;
  }

  /* --------------------------------------------------------- прогон (симуляция) */
  function verdictFor(call) {
    return call.expect.indexOf(call.s) >= 0 ? "соответствует ожиданию" : "не соответствует ожиданию";
  }

  function pushExchange(item, call) {
    ST.seq += 1;
    var exchange = {
      seq: ST.seq,
      ts: new Date().toLocaleTimeString("ru-RU") + ".000",
      label: item.id,
      m: call.m.toUpperCase(),
      p: call.p,
      s: call.s,
      ms: 40 + Math.round(Math.random() * 400),
      req: item.payload || (call.m === "get" ? "query / без тела" : "{}"),
      res: call.resp,
      verdict: verdictFor(call)
    };
    ST.exchanges.push(exchange);
  }

  function runItem(item, onFinish) {
    item.status = "выполняется";
    render();
    var index = 0;
    var from = ST.seq + 1;
    function step() {
      if (index >= item.calls.length) {
        var miss = null;
        item.calls.forEach(function (c) {
          if (miss === null && c.expect.indexOf(c.s) < 0) { miss = c; }
        });
        item.status = miss ? "отказ" : "успех";
        item.verdict = miss ? "ответ " + miss.s + ", ожидалось " + miss.expect.join("/")
                            : "соответствует ожиданию";
        item.journal = "#" + from + "–#" + ST.seq;
        item.ms = 120 + Math.round(Math.random() * 1500);
        ST.lastRunId = item.id;
        if (miss) { toast(item.id + ": отказ — сформировано замечание к API (FR-P-31)"); }
        render();
        if (onFinish) { onFinish(item); }
        return;
      }
      pushExchange(item, item.calls[index]);
      index += 1;
      render();
      window.setTimeout(step, 350);
    }
    step();
  }

  function announce(item) {
    if (item.status === "успех") { toast(item.id + ": успех — вердикт «соответствует ожиданию»"); }
    else if (item.status === "отказ") { toast(item.id + ": отказ — см. ленту обмена и замечания"); }
  }

  function runNext() {
    var item = nextItem();
    if (!item) { toast("Очередь пройдена: все включённые пункты выполнены"); return; }
    if (needsCard(item) && !ST.ui["card-confirm"]) {
      ST.ui.runCard = item.id;
      render();
      toast("Нужна карточка запуска: " + item.id + " (" + item.klass + ")");
      return;
    }
    ST.ui.runCard = null;
    runItem(item, announce);
  }

  function runBatch() {
    var tech = ST.queue.filter(function (i) {
      return i.enabled && i.klass === "tech" && i.status === "не выполнена";
    });
    if (!tech.length) { toast("Безопасных невыполненных проверок нет"); return; }
    toast("Пачка tech: " + tech.length + " проверок выполняется");
    var index = 0;
    function next() {
      if (index >= tech.length) { toast("Пачка tech завершена: " + tech.length + " проверок"); return; }
      var item = tech[index];
      index += 1;
      runItem(item, next);
    }
    next();
  }

  function startAuto() {
    if (ST.auto.on) { return; }
    ST.auto.on = true;
    ST.auto.timer = window.setInterval(function () {
      var item = nextItem();
      if (!item) { stopAuto(); toast("Авто-прогон завершён: очередь пройдена"); return; }
      if (needsCard(item) || item.klass === "manual") {
        ST.ui.runCard = needsCard(item) ? item.id : null;
        stopAuto();
        render();
        toast("Авто-прогон остановлен: пункт " + item.id + " требует оператора");
        return;
      }
      runItem(item, announce);
    }, Math.max(500, ST.auto.pause * 1000));
    render();
  }

  function stopAuto() {
    if (ST.auto.timer) { window.clearInterval(ST.auto.timer); ST.auto.timer = null; }
    ST.auto.on = false;
  }

  function pseudoItem(label) {
    return { id: label, payload: "" };
  }

  /* ------------------------------------------------------------- действия */
  function closeForms() {
    ST.ui.runCard = null;
    ST.ui.offFor = null;
    ST.ui.markFor = null;
    ST.ui.repeatFor = null;
  }

  function regress(id) {
    ST.queue.forEach(function (i) {
      if (id === "all" && (i.status === "отказ" || i.status === "блокировано")) {
        i.status = "не выполнена";
        i.verdict = "";
      }
      if (id === "N-01" && i.id === "TC-FILE-10") { i.status = "не выполнена"; i.verdict = ""; }
    });
    toast("Регрессионный прогон собран: затронутые проверки возвращены в очередь (FR-P-41)");
    render();
  }

  var ACTIONS = {
    nav: function (el) { go(el.dataset.id); },
    toast: function (el) { toast(el.dataset.msg || "Готово"); },
    select: function (el) { ST.selectedId = el.dataset.id; render(); },
    screenshot: function () {
      ST.ui.offFor = ST.selectedId || (nextItem() ? nextItem().id : null);
      ST.ui.offReason = "";
      render();
    },
    "off-save": function () {
      var item = byId(ST.ui.offFor);
      if (item) { item.enabled = false; item.reason = ST.ui.offReason || "причина не указана"; }
      ST.ui.offFor = null;
      toast("Пункт снят с очереди: снятие зафиксировано с причиной (FR-P-15)");
      render();
    },
    "off-cancel": function () { ST.ui.offFor = null; render(); },
    curl: function (el) {
      var x = ST.exchanges.filter(function (item) { return item.seq === Number(el.dataset.seq); })[0];
      if (!x) { return; }
      var text = curlOf(x);
      if (window.navigator.clipboard) { window.navigator.clipboard.writeText(text); }
      toast("curl скопирован: " + text.slice(0, 90) + "…");
    },
    repeat: function (el) {
      ST.ui.repeatFor = Number(el.dataset.seq);
      ST.ui.repeatPath = "";
      ST.ui.repeatBody = "";
      render();
    },
    "repeat-send": function (el) {
      var x = ST.exchanges.filter(function (item) { return item.seq === Number(el.dataset.seq); })[0];
      if (!x) { return; }
      ST.seq += 1;
      ST.exchanges.push({
        seq: ST.seq, ts: new Date().toLocaleTimeString("ru-RU") + ".000", label: x.label,
        m: x.m, p: ST.ui.repeatPath || x.p, s: x.s, ms: 30 + Math.round(Math.random() * 300),
        req: ST.ui.repeatBody || x.req, res: x.res, verdict: "повтор с правками"
      });
      ST.ui.repeatFor = null;
      toast("Повтор отправлен с правками: обмен #" + ST.seq);
      render();
    },
    "repeat-cancel": function () { ST.ui.repeatFor = null; render(); },
    "note-from-exchange": function (el) {
      toast("Замечание создано из обмена #" + el.dataset.seq + " (черновик в реестре, FR-P-38)");
    },
    "run-next": function () { runNext(); },
    "run-batch": function () { runBatch(); },
    auto: function () { if (ST.auto.on) { stopAuto(); render(); } else { startAuto(); } },
    "card-run": function () {
      var item = byId(ST.ui.runCard);
      if (!item) { return; }
      var missing = [];
      if (!ST.ui["card-goal"]) { missing.push("цель проверки"); }
      if (!ST.ui["card-data"]) { missing.push("используемые данные"); }
      if (!ST.ui["card-responsible"]) { missing.push("ответственный"); }
      if (!ST.ui["card-confirm"]) { missing.push("подтверждение расхода"); }
      if (missing.length) {
        toast("Карточка запуска не заполнена: " + missing.join(", ") + " (FR-P-19)");
        return;
      }
      ST.ui.runCard = null;
      toast("Карточка запуска принята: " + item.id);
      runItem(item, announce);
    },
    "card-cancel": function () { ST.ui.runCard = null; render(); },
    "next-check": function () {
      var order = ST.queue.map(function (i) { return i.id; });
      var index = order.indexOf(ST.selectedId);
      ST.selectedId = order[(index + 1) % order.length];
      render();
    },
    "mark-start": function (el) {
      ST.ui.markFor = el.dataset.id || (nextItem() ? nextItem().id : null);
      ST.ui.markStatus = "выполнена вручную";
      ST.ui.markNote = "";
      render();
    },
    "mark-save": function () {
      var item = byId(ST.ui.markFor);
      if (!item) { return; }
      if (!String(ST.ui.markNote || "").trim()) {
        toast("Ручная отметка не принята: нужно заключение оператора (FR-P-32)");
        return;
      }
      item.status = ST.ui.markStatus === "блокировано" ? "блокировано"
        : (ST.ui.markStatus === "пропущена" ? "пропущена"
        : (ST.ui.markStatus === "прервана" ? "прервана" : "успех"));
      item.verdict = ST.ui.markStatus + ": " + ST.ui.markNote;
      ST.ui.markFor = null;
      toast("Отметка записана: " + item.id + " — " + ST.ui.markStatus);
      render();
    },
    "mark-cancel": function () { ST.ui.markFor = null; render(); },
    "check-stand": function () {
      pushExchange(pseudoItem("инструмент"), { m: "get", p: "/health", s: 200, expect: [200], resp: "{\"status\":\"ok\"}" });
      pushExchange(pseudoItem("инструмент"), { m: "get", p: "/version", s: 200, expect: [200], resp: "{\"revision\":\"83319ae\"}" });
      toast("Стенд доступен: dev@83319ae, health ok (записи #" + (ST.seq - 1) + "–#" + ST.seq + ")");
      render();
    },
    snapshot: function () {
      if (DEMO.snapshots.end) { toast("Снимок «Окончание» уже снят"); render(); return; }
      DEMO.snapshots.end = { at: new Date().toLocaleString("ru-RU"), files: 265, records: 129, loads: 12,
        datasets: 9, models: 6, tasks: 216, errors: [] };
      toast("Снимок «Окончание» снят: дельта рассчитана (FR-P-47)");
      render();
    },
    "finish-session": function () {
      DEMO.session.status = "завершена";
      stopAuto();
      toast("Сессия завершена: прогон заблокирован до переоткрытия (FR-P-55)");
      render();
    },
    poll: function () { toast("Опрос наблюдений выполнен: 4 `$Задачи`, переходов нет"); },
    "task-cmd": function (el) {
      var task = DEMO.tasks.filter(function (t) { return t.id === el.dataset.id; })[0];
      if (!task) { return; }
      if (task.actions.indexOf(el.dataset.cmd) < 0) {
        toast("Команда «" + el.dataset.cmd + "» недоступна для статуса " + task.status + " (FR-P-25)");
        return;
      }
      toast("Команда «" + el.dataset.cmd + "» отправлена задаче " + task.id + " (только из available_actions)");
    },
    "diag-run": function () {
      if (!ST.ui["diag-confirm"]) {
        toast("Нужно подтверждение: задача celery-test изменяет стенд (NFR-P-5)");
        return;
      }
      pushExchange(pseudoItem("TC-TASK-01"),
        { m: "post", p: "/api/tasks/test?duration=30", s: 202, expect: [202], resp: "{\"task_id\":\"7a2e…\"}" });
      toast("celery-test запущен: task_id 7a2e, наблюдение включено (FR-P-24)");
      render();
    },
    "console-send": function () {
      pushExchange(pseudoItem("инструмент"),
        { m: "post", p: "/api/data/file", s: 201, expect: [201], resp: "{\"id\":\"__TEST__console…\"}" });
      toast("Вызов выполнен: обмен #" + ST.seq + " (метка «инструмент», статусы проверок не меняются)");
      render();
    },
    regress: function (el) { regress(el.dataset.id); },
    "note-close": function () {
      DEMO.notes[0].status = "закрыто";
      toast("Замечание N-01 закрыто после подтверждения воспроизведением (FR-P-44)");
      render();
    },
    "data-tab": function (el) { ST.ui.dataTab = el.dataset.id; render(); },
    "task-tab": function (el) { ST.ui.taskTab = el.dataset.id; render(); },
    "notes-tab": function (el) { ST.ui.notesTab = el.dataset.id; render(); },
    "tools-tab": function (el) { ST.ui.toolsTab = el.dataset.id; render(); },
    /* ------------------------------- планирование: наборы (SCR-14) и программа (SCR-15) */
    "set-open": function (el) { ST.ui.setFor = el.dataset.id; go("SCR-14"); },
    "set-add": function (el) {
      var set = currentSet();
      if (set && set.items.indexOf(el.dataset.id) < 0) { set.items.push(el.dataset.id); }
      render();
    },
    "set-remove": function (el) {
      var set = currentSet();
      if (set) { set.items = set.items.filter(function (id) { return id !== el.dataset.id; }); }
      render();
    },
    "set-add-filtered": function () {
      var set = currentSet();
      if (!set) { return; }
      var added = 0;
      setCatalogFiltered().forEach(function (i) {
        if (set.items.indexOf(i.id) < 0) { set.items.push(i.id); added += 1; }
      });
      toast("В набор " + set.id + " добавлено проверок: " + added + " (массовый выбор по фильтру, IR-P-17)");
      render();
    },
    "set-up": function (el) { moveInSet(el.dataset.id, -1); },
    "set-down": function (el) { moveInSet(el.dataset.id, 1); },
    "set-save": function () {
      var set = currentSet();
      if (!set) { return; }
      set.revision += 1;
      set.status = "черновик";
      toast("Набор " + set.id + " сохранён как ревизия " + set.revision + " (FR-P-66)");
      render();
    },
    "set-approve": function () {
      var set = currentSet();
      if (!set) { return; }
      if (!set.items.length) { toast("Пустой набор нельзя утвердить: добавьте проверки"); return; }
      set.status = "утверждён";
      ST.programmeBaseline = programmeIdList();
      toast("Набор " + set.id + " утверждён (ревизия " + set.revision + ", автор и время зафиксированы)");
      render();
    },
    "set-new": function () {
      var id = "Н-" + String(ST.sets.length + 1).padStart(2, "0");
      ST.sets.push({ id: id, title: "Новый набор", section: "TC-SYS", target: "смоук",
        status: "черновик", revision: 1, author: "Иванов И.И., руководитель испытаний",
        mandatory: false, selected: false, items: [] });
      ST.ui.setFor = id;
      toast("Создан " + id + ": соберите состав из каталога");
      render();
    },
    "set-copy": function () {
      var set = currentSet();
      if (!set) { return; }
      var id = "Н-" + String(ST.sets.length + 1).padStart(2, "0");
      ST.sets.push({ id: id, title: set.title + " (копия)", section: set.section, target: set.target,
        status: "черновик", revision: 1, author: set.author, mandatory: false, selected: false,
        items: set.items.slice() });
      ST.ui.setFor = id;
      toast("Набор скопирован: " + id);
      render();
    },
    "prog-toggle": function (el) {
      var set = setById(el.dataset.id);
      if (!set) { return; }
      set.selected = !set.selected;
      ST.programmeOrder = null;
      render();
    },
    "prog-template": function (el) {
      var kind = el.dataset.id;
      ST.sets.forEach(function (s) {
        if (kind === "полная") { s.selected = true; }
        else if (kind === "смоук") { s.selected = !!s.mandatory; }
        else if (kind === "регресс") {
          s.selected = s.items.some(function (id) {
            var item = byId(id);
            return item && (item.status === "отказ" || item.status === "блокировано");
          }) || s.mandatory;
        }
      });
      ST.programmeOrder = null;
      toast("Применён шаблон «" + kind + "»: наборов выбрано " +
        ST.sets.filter(function (s) { return s.selected; }).length);
      render();
    },
    "prog-up": function (el) { moveInProgramme(el.dataset.id, -1); },
    "prog-down": function (el) { moveInProgramme(el.dataset.id, 1); },
    "prog-approve": function () {
      var bad = uncovered();
      var reduction = ST.ui.reduction || ST.programme.reduction;
      if (bad.length && !String(reduction).trim()) {
        toast("Утверждение с сокращением требует обоснования: не покрыты " +
          bad.map(function (g) { return g.group; }).join(", ") + " (FR-P-69)");
        return;
      }
      ST.programme.reduction = reduction;
      ST.programme.status = "утверждён";
      ST.programmeBaseline = programmeIdList();
      toast("Программа утверждена: ревизия " + ST.programme.revision + ", пунктов " +
        programmeIdList().length + (bad.length ? " (сокращено, обоснование зафиксировано)" : ""));
      render();
    },
    "prog-revision": function () {
      ST.programme.revision += 1;
      ST.programme.status = "черновик";
      ST.programmeBaseline = programmeIdList();
      toast("Выпущена новая ревизия программы: " + ST.programme.revision +
        " — ранее полученные результаты сохранены (DR-P-14)");
      render();
    }
  };

  function moveInSet(id, step) {
    var set = currentSet();
    if (!set) { return; }
    var index = set.items.indexOf(id);
    var target = index + step;
    if (index < 0 || target < 0 || target >= set.items.length) { return; }
    set.items.splice(index, 1);
    set.items.splice(target, 0, id);
    render();
  }

  function moveInProgramme(id, step) {
    var order = programmeOrder();
    var index = order.indexOf(id);
    var target = index + step;
    if (index < 0 || target < 0 || target >= order.length) { return; }
    order.splice(index, 1);
    order.splice(target, 0, id);
    ST.programmeOrder = order;
    render();
  }

  /* --------------------------------------------------------------- события */
  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-act]");
    if (!el) { return; }
    var act = el.dataset.act;
    if (act === "card-confirm" || act === "only-errors" || act === "diag-confirm") {
      ST.ui[act] = el.checked;
      render();
      return;
    }
    var handler = ACTIONS[act];
    if (handler) {
      event.preventDefault();
      handler(el);
    }
  });

  document.addEventListener("change", function (event) {
    var el = event.target;
    if (!el.dataset || !el.dataset.set) { return; }
    var key = el.dataset.set;
    var value = el.type === "checkbox" ? el.checked : el.value;
    if (key === "pause") {
      ST.auto.pause = Math.max(0.5, Math.min(120, Number(value) || 3));
    } else if (key === "feedRows") {
      ST.ui.feedRows = Number(value);
    } else {
      ST.ui[key] = value;
    }
    var isTextInput = el.tagName === "INPUT" && el.type === "text";
    if (key.indexOf("card-") === 0 || isTextInput) {
      return;                         // текстовые поля применяются по Enter или следующему действию
    }
    render();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && event.target && event.target.dataset && event.target.dataset.set) {
      event.preventDefault();
      render();
      return;
    }
    if (event.key === "Escape") { closeForms(); stopAuto(); render(); return; }
    if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); runNext(); return; }
    if (event.altKey && (event.key === "n" || event.key === "N")) {
      event.preventDefault();
      go("SCR-14");
      return;
    }
    if (event.altKey && (event.key === "p" || event.key === "P")) {
      event.preventDefault();
      go("SCR-15");
      return;
    }
    if (event.altKey && /^[1-9]$/.test(event.key)) {
      var index = Number(event.key) - 1;
      if (SCREENS[index]) { event.preventDefault(); go(SCREENS[index].id); }
    }
  });

  window.addEventListener("hashchange", function () {
    var target = (window.location.hash || "").replace("#", "").toUpperCase();
    var screen = SCREENS.filter(function (s) { return s.id === target; })[0];
    if (screen && screen.id !== ST.screen) { stopAuto(); ST.screen = screen.id; render(); }
  });

  var stateSelect = document.getElementById("ui-state");
  if (stateSelect) {
    stateSelect.addEventListener("change", function () {
      ST.ui.state = stateSelect.value;
      render();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
