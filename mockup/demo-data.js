/* Демонстрационные данные прототипа ПУЛЬТА (docs/15, docs/16).
   Данные правдоподобны по опыту испытаний T0–M1, но статичны: запросы к стенду не выполняются. */

window.PULT_DEMO = (function () {
  "use strict";

  /* Каталог проверок — сокращённый каталог программы проверок (docs/02).
     Формат строки: id, название, группа, класс, требования, ожидание, payload, вызовы.
     Вызов: [метод, путь, статус ответа, ожидаемые статусы, фрагмент тела ответа]. */
  var catalogRaw = [
    ["TC-SYS-01", "Доступность сервиса", "TC-SYS", "tech", "FR-1, UC-01", "HTTP 200, сервис доступен", "",
      [["get", "/health", 200, [200], "{\"status\":\"ok\",\"db\":true}"]]],
    ["TC-SYS-02", "Версия сборки и её состав", "TC-SYS", "tech", "FR-1, BR-R8", "HTTP 200, поля сборки заполнены", "",
      [["get", "/version", 200, [200], "{\"branch\":\"dev\",\"revision\":\"83319ae\"}"]]],
    ["TC-SYS-03", "Поведение на неизвестном пути", "TC-SYS", "tech", "NFR-2", "HTTP 404 и разобранная ошибка", "",
      [["get", "/api/__test_unknown__", 405, [404], "{\"detail\":\"Method Not Allowed\"}"]]],
    ["TC-SYS-04", "Неизвестная задача", "TC-SYS", "tech", "BR-R7, UC-27", "404 или статус not_found", "",
      [["get", "/api/tasks/{task_id}", 404, [404, 200], "{\"detail\":\"Task not found\"}"]]],
    ["TC-SYS-06", "Различимость сборок", "TC-SYS", "manual", "BR-R8", "оператор подтверждает различимость сборок", "",
      [["get", "/version", 200, [200], "revision + build_date позволяют отличить сборки"]]],
    ["TC-FILE-01", "Реестр файлов получен полностью", "TC-FILE", "tech", "UC-03, FR-2", "HTTP 200, count = элементам", "",
      [["get", "/api/data/files", 200, [200], "{\"count\":265}"]]],
    ["TC-FILE-03", "Фильтр file_type точный и регистрозависимый", "TC-FILE", "tech", "UC-03, NFR-4", "RAW непусто, raw пусто", "",
      [["get", "/api/data/files?file_type=RAW", 200, [200], "{\"count\":240}"],
       ["get", "/api/data/files?file_type=raw", 200, [200], "{\"count\":0}"]]],
    ["TC-FILE-08", "Скачивание файла (ASCII-имя)", "TC-FILE", "tech", "UC-04, BR-F1", "200, octet-stream, размер совпадает", "",
      [["get", "/api/data/file/{file_id}/download", 200, [200], "octet-stream, 1 048 576 Б"]]],
    ["TC-FILE-10", "Скачивание файла с не-ASCII именем", "TC-FILE", "tech", "замечание P0", "ожидаемо блокировано (404 latin-1)", "",
      [["get", "/api/data/file/{file_id}/download", 404, [200], "{\"detail\":\"Not Found\"}"]]],
    ["TC-FILE-13", "Round-trip upload → list → download → delete", "TC-FILE", "live", "UC-02…UC-05",
      "файл создан, найден, скачан, удалён", "file: __TEST__TC-FILE-13_2f6a.csv (1 024 Б), уборка: да",
      [["post", "/api/data/file", 201, [201], "{\"id\":\"b41c…\",\"file_name\":\"__TEST__TC-FILE-13_2f6a.csv\"}"],
       ["get", "/api/data/files?file_name=__TEST__TC-FILE-13", 200, [200], "{\"count\":1}"],
       ["get", "/api/data/file/{file_id}/download", 200, [200], "octet-stream, 1 024 Б"],
       ["delete", "/api/{file_id}", 204, [204, 200], "удалено"]]],
    ["TC-REC-01", "Объединение RAW+markup в записи", "TC-REC", "tech", "замечание №1", "записи по явному правилу, уверенность показана", "",
      [["get", "/api/data/files", 200, [200], "129 записей, дублей 128"]]],
    ["TC-REC-02", "Обработка дублей при сопоставлении", "TC-REC", "tech", "замечание №1, §3.1", "все версии видны, манифест полон", "",
      [["get", "/api/data/files", 200, [200], "контроль полноты манифеста пройден"]]],
    ["TC-REC-03", "Ручная перепривязка пары", "TC-REC", "manual", "§3.1 ТЗ", "решение оператора зафиксировано", "",
      [["get", "/api/data/files", 200, [200], "решение: markup 91b2 → RAW 7f3a"]]],
    ["TC-LOAD-01", "Реестр нагрузок получен", "TC-LOAD", "tech", "FR-3, UC-06", "HTTP 200, состав полей зафиксирован", "",
      [["get", "/api/loads/list", 200, [200], "12 нагрузок"]]],
    ["TC-LOAD-04", "Создание и удаление тестовой нагрузки", "TC-LOAD", "live", "UC-07, UC-08", "нагрузка создана и удалена", "name: __TEST__load-…",
      [["post", "/api/loads/", 201, [201], "{\"id\":\"__TEST__load-7c1\"}"],
       ["delete", "/api/loads/{load_id}", 204, [204, 200], "удалено"]]],
    ["TC-TASK-01", "Диагностическая задача celery-test", "TC-TASK", "tech", "UC-26, FR-8", "202 и task_id, задача завершилась", "",
      [["post", "/api/tasks/test?duration=2", 202, [202], "{\"task_id\":\"91bc…\"}"],
       ["get", "/api/tasks/{task_id}", 200, [200], "{\"status\":\"completed\"}"]]],
    ["TC-TASK-02", "Список задач и фильтры", "TC-TASK", "tech", "UC-26, NFR-4", "список получен, фильтры влияют", "",
      [["get", "/api/tasks/?task_type=training", 200, [200], "{\"count\":3}"]]],
    ["TC-TASK-04", "Наблюдение за задачей", "TC-TASK", "live", "UC-27, FSM-1", "статусы FSM-1 отслежены до терминального", "",
      [["post", "/api/tasks/test?duration=30", 202, [202], "{\"task_id\":\"7a2e…\"}"],
       ["get", "/api/tasks/{task_id}", 200, [200], "{\"status\":\"paused\"}"]]],
    ["TC-TASK-08", "Наблюдение за внешней задачей", "TC-TASK", "manual", "UC-27, BR-R5", "внешняя задача подтверждена оператором", "",
      [["get", "/api/tasks/{task_id}", 200, [200], "{\"status\":\"running\"}"]]],
    ["TC-DS-01", "Создание и удаление датасета", "TC-DS", "live", "UC-09, UC-13", "датасет создан и удалён", "name: __TEST__dataset-…",
      [["post", "/api/datasets/", 201, [201], "{\"dataset_id\":\"ds-12\"}"],
       ["delete", "/api/datasets/{dataset_id}", 204, [204, 200], "удалено"]]],
    ["TC-DS-03", "Состав датасета и агрегаты", "TC-DS", "tech", "UC-12, замечание P1", "ожидаемо блокировано (состава нет)", "",
      [["get", "/api/datasets/{dataset_id}", 200, [200], "состав отсутствует в ответе"]]],
    ["TC-DS-05", "Наполнение датасета (fill)", "TC-DS", "heavy", "UC-14, замечание P1", "202 и задача dataset-fill отслежена", "raw_file_ids: 6 пар",
      [["post", "/api/datasets/fill/{dataset_id}", 202, [202], "202 без task_id"]]],
    ["TC-DS-07", "Удаление датасета и самоочистка", "TC-DS", "live", "UC-13, NFR-T4", "датасет удалён, отсутствие подтверждено", "",
      [["delete", "/api/datasets/{dataset_id}", 204, [204, 200], "удалено"]]],
    ["TC-MOD-01", "Каталог архитектур моделей", "TC-MOD", "tech", "UC-15, FR-5", "каталог получен", "",
      [["get", "/api/ml_models/architectures", 200, [200], "{\"architectures\":[…]}"]]],
    ["TC-TR-02", "Запуск обучения", "TC-TR", "heavy", "UC-21, BR-F5", "202 и task_id, обучение отслежено", "epochs 5, batch 32",
      [["post", "/api/ml_models/models/{model_id}/train", 202, [202], "{\"task_id\":\"5f1d…\"}"]]],
    ["TC-INF-01", "Асинхронный инференс по датасету", "TC-INF", "heavy", "UC-24, BR-F6", "202 и task_id, инференс отслежен", "",
      [["post", "/api/ml_models/models/{model_id}/inference", 202, [202], "{\"task_id\":\"c4d0…\"}"]]],
    ["TC-CLEAN-01", "Самоочистка тестовых сущностей", "TC-CLEAN", "live", "NFR-T4", "все __TEST__-сущности удалены", "",
      [["delete", "/api/{file_id}", 204, [204, 200], "удалено"],
       ["delete", "/api/datasets/{dataset_id}", 204, [204, 200], "удалено"]]],
    ["TC-CLEAN-03", "Сверка снимков стенда", "TC-CLEAN", "tech", "FR-T1, BR-R6", "дельта объяснена созданным и удалённым", "",
      [["get", "/api/data/files", 200, [200], "счётчики совпадают со снимком «Начало»"]]]
  ];

  var catalog = catalogRaw.map(function (row) {
    return {
      id: row[0], title: row[1], group: row[2], klass: row[3],
      req: row[4], expected: row[5], payload: row[6],
      calls: row[7].map(function (c) {
        return { m: c[0], p: c[1], s: c[2], expect: c[3], resp: c[4] };
      })
    };
  });

  /* Начальное состояние части проверок (результаты живого прогона 18–21.09.2026). */
  var initialStatus = {
    "TC-SYS-01": ["успех", "соответствует ожиданию"],
    "TC-SYS-02": ["успех", "соответствует ожиданию"],
    "TC-SYS-03": ["отказ", "ответ 405, ожидалось 404"],
    "TC-FILE-01": ["успех", "соответствует ожиданию"],
    "TC-FILE-10": ["блокировано", "404 latin-1 на не-ASCII имени"],
    "TC-REC-01": ["успех", "соответствует ожиданию"],
    "TC-LOAD-01": ["успех", "соответствует ожиданию"],
    "TC-TASK-02": ["успех", "соответствует ожиданию"],
    "TC-DS-03": ["блокировано", "состав датасета отсутствует (P1)"]
  };

  /* Планирование испытаний (SCR-14, SCR-15): разделы, наборы и программа сессии.
     «Раздел испытаний» = группа каталога (модуль API). Объём: смоук / стандарт / полный. */
  var sections = {
    "TC-SYS": { title: "Система", target: "смоук" },
    "TC-FILE": { title: "Файлы (импорт и скачивание)", target: "стандарт" },
    "TC-REC": { title: "Записи RAW+markup", target: "стандарт" },
    "TC-LOAD": { title: "Нагрузки", target: "стандарт" },
    "TC-TASK": { title: "Задачи и FSM-1", target: "стандарт" },
    "TC-DS": { title: "Датасеты", target: "стандарт" },
    "TC-MOD": { title: "Модели", target: "смоук" },
    "TC-TR": { title: "Обучение и ONNX", target: "смоук" },
    "TC-INF": { title: "Инференс", target: "смоук" },
    "TC-CLEAN": { title: "Уборка и сверка", target: "смоук" }
  };

  var sets = [
    { id: "Н-01", title: "Система (смоук-минимум)", section: "TC-SYS", target: "смоук",
      status: "утверждён", revision: 2, author: "Иванов И.И., руководитель испытаний",
      mandatory: true, selected: true,
      items: ["TC-SYS-01", "TC-SYS-03", "TC-CLEAN-03"] },
    { id: "Н-02", title: "Файлы и записи", section: "TC-FILE", target: "стандарт",
      status: "черновик", revision: 3, author: "Иванов И.И., руководитель испытаний",
      mandatory: false, selected: true,
      items: ["TC-FILE-01", "TC-FILE-03", "TC-FILE-08", "TC-FILE-10", "TC-FILE-13",
              "TC-REC-01", "TC-REC-02", "TC-REC-03", "TC-CLEAN-03"] },
    { id: "Н-03", title: "Нагрузки", section: "TC-LOAD", target: "смоук",
      status: "черновик", revision: 1, author: "Иванов И.И., руководитель испытаний",
      mandatory: false, selected: false,
      items: ["TC-LOAD-01", "TC-LOAD-04"] },
    { id: "Н-04", title: "Задачи и FSM-1", section: "TC-TASK", target: "стандарт",
      status: "утверждён", revision: 1, author: "Иванов И.И., руководитель испытаний",
      mandatory: false, selected: true,
      items: ["TC-TASK-01", "TC-TASK-02", "TC-TASK-04", "TC-TASK-08"] },
    { id: "Н-05", title: "Датасеты и модели (полная)", section: "TC-DS", target: "полный",
      status: "черновик", revision: 1, author: "Иванов И.И., руководитель испытаний",
      mandatory: false, selected: false,
      items: ["TC-DS-01", "TC-DS-03", "TC-DS-05", "TC-DS-07", "TC-MOD-01",
              "TC-TR-02", "TC-INF-01"] }
  ];

  var programme = {
    revision: 2,
    status: "черновик",              // черновик | утверждён
    author: "Иванов И.И., руководитель испытаний",
    reduction: ""                    // обоснование сокращения (если раздел не покрыт)
  };

  /* Данные стенда и сессии (демонстрационные). */
  var stand = {
    baseUrl: "https://ai-center.online",
    build: "dev@83319ae",
    branch: "dev",
    builtAt: "20.09.2026 18:40",
    health: "test stand ready",
    appLog: "logs/applog/applog_20260921-101005.log",
    namespace: "test-stand/acceptance"
  };

  var session = {
    id: "20260921-2f6a",
    title: "Приёмка сервера обучения, сборка dev@83319ae",
    object: "dev@83319ae",
    method: "Методика ПМИ-2026-09, ред. 3",
    customer: "Энергомера",
    org: "Испытательная лаборатория",
    commission: "Иванов И.И. (председатель), Петров П.П.",
    criteria: "годен при отсутствии открытых P0/P1",
    status: "идёт",
    started: "21.09.2026 10:11",
    operator: "Петров П.П., инженер-испытатель"
  };

  var registries = {
    files: { total: 265, size: "47.3 ГБ", raw: 240, markup: 18, onnx: 7 },
    records: { total: 129, duplicates: 128, withoutMarkup: 3, withoutRaw: 1 },
    loads: 12, datasets: 9, models: 6,
    tasks: { total: 215, active: 4, archive: 211 }
  };

  var records = [
    { name: "Antminer_S19", size: "2.1 ГБ / 12 МБ", state: "видимо", note: "RAW + markup, версия по импорту (нет checksum)" },
    { name: "3Dпринтер", size: "24 МБ / 1.2 МБ", state: "дубль", note: "2 версии: 24 566 291 Б и 1 282 054 Б" },
    { name: "Печь_№4", size: "88 МБ", state: "без разметки", note: "markup не найден: свободная разметка отсутствует" },
    { name: "Принтер-2", size: "3 МБ", state: "без RAW", note: "markup без пары: кандидаты не найдены" }
  ];

  var tasks = [
    { id: "5f1d", type: "training", target: "dataset ds-12", status: "running", elapsed: "03:41",
      actions: ["pause", "interrupt"],
      history: [["11:41", "new", "running", 198], ["12:04", "running", "pausing", 231]] },
    { id: "7a2e", type: "dataset-fill", target: "dataset ds-12", status: "paused", elapsed: "00:12",
      actions: ["resume", "interrupt"],
      history: [["11:58", "new", "running", 187], ["12:00", "running", "paused", 196]] },
    { id: "91bc", type: "celery-test", target: "—", status: "completed", elapsed: "00:02",
      actions: [], history: [["11:40", "new", "running", 171], ["11:40", "running", "completed", 173]] },
    { id: "c4d0", type: "model-testing", target: "model m-3", status: "not_found", elapsed: "00:05",
      actions: [], history: [["11:20", "new", "running", 162], ["11:25", "running", "not_found", 166]] }
  ];

  var notes = [
    { id: "N-01", check: "TC-FILE-10", title: "404 на не-ASCII имени файла (latin-1)", priority: "P0",
      module: "File Import", endpoint: "GET /api/data/file/{id}/download", status: "в работе",
      fact: "Скачивание файла с не-ASCII именем отвечает 404 (кодировка latin-1)",
      expected: "Файл отдаётся независимо от имени", repro: "curl -O …/download", evidence: "#88, #92",
      blocks: 108 },
    { id: "N-02", check: "TC-DS-03", title: "Нет состава датасета и агрегатов", priority: "P1",
      module: "Datasets", endpoint: "GET /api/datasets/{id}", status: "открыто",
      fact: "Ответ не содержит перечень записей и агрегаты (число записей/чанков)",
      expected: "Состав и агрегаты доступны по API", repro: "curl …/api/datasets/ds-12", evidence: "#142", blocks: 2 },
    { id: "N-03", check: "TC-DS-05", title: "`fill` не возвращает `task_id`", priority: "P1",
      module: "Datasets", endpoint: "POST /api/datasets/fill/{id}", status: "открыто",
      fact: "Ответ 202 без идентификатора задачи; задача ищется в списке",
      expected: "202 с `task_id`", repro: "curl -X POST …/fill/ds-12", evidence: "#150", blocks: 1 },
    { id: "N-04", check: "TC-SYS-03", title: "Неизвестный путь отвечает 405 вместо 404", priority: "P2",
      module: "Система", endpoint: "GET /api/__test_unknown__", status: "открыто",
      fact: "Корневой маршрут DELETE /api/{file_id} перехватывает запрос и отдаёт 405",
      expected: "Согласованный 404 с разобранной ошибкой", repro: "curl -i …/api/__test_unknown__", evidence: "#44", blocks: 0 },
    { id: "N-05", check: "—", title: "Нет статуса в списке `$Задач`", priority: "P1",
      module: "Task service", endpoint: "GET /api/tasks/", status: "открыто",
      fact: "Список задач не содержит статуса (только type/name/description/id/created_at)",
      expected: "Статус в списке или вложенная карточка", repro: "curl …/api/tasks/", evidence: "#120", blocks: 1 }
  ];

  var prospective = [
    { title: "Изолированный контур испытаний (namespace и квоты)", why: "безопасность боевых данных" },
    { title: "`checksum`/`updated_at` у файлов", why: "актуальная версия записи без ручного решения" },
    { title: "`Range`/`Content-Length` при скачивании", why: "крупные файлы (до 2.1 ГБ) и воспроизводимость" },
    { title: "Субдатасеты как серверная сущность", why: "явные подмножества данных для обучения" },
    { title: "Статус задачи в списке и сортировка", why: "меньше запросов и предсказуемый порядок" }
  ];

  var initialExchanges = [
    { seq: 211, ts: "11:58:02.118", label: "TC-DS-05", m: "POST", p: "/api/datasets/fill/ds-12", s: 202, ms: 412,
      req: "{\"raw_file_ids\":[…] (6 пар)}", res: "202 Accepted без task_id (замечание P1)", verdict: "соответствует ожиданию" },
    { seq: 212, ts: "11:58:05.004", label: "TC-DS-05", m: "GET", p: "/api/tasks/?task_type=dataset-fill", s: 200, ms: 88,
      req: "query: task_type=dataset-fill", res: "{\"count\":1} — задача 7a2e найдена (обходной путь)", verdict: "соответствует ожиданию" },
    { seq: 213, ts: "12:00:11.771", label: "TC-TASK-04", m: "GET", p: "/api/tasks/7a2e", s: 200, ms: 64,
      req: "GET карточки $задачи", res: "{\"status\":\"paused\",\"intermediate_result\":0.42}", verdict: "соответствует ожиданию" },
    { seq: 214, ts: "12:02:14.221", label: "TC-FILE-13", m: "POST", p: "/api/data/file", s: 201, ms: 412,
      req: "multipart: file=__TEST__TC-FILE-13_2f6a.csv (1 024 Б), file_type=RAW",
      res: "{\"id\":\"b41c…\",\"file_name\":\"__TEST__TC-FILE-13_2f6a.csv\"}", verdict: "соответствует ожиданию" },
    { seq: 215, ts: "12:02:14.640", label: "TC-FILE-13", m: "GET", p: "/api/data/file/b41c…/download", s: 200, ms: 88,
      req: "GET …/download", res: "octet-stream, 1 024 Б", verdict: "соответствует ожиданию" },
    { seq: 216, ts: "12:02:15.102", label: "TC-FILE-13", m: "DELETE", p: "/api/b41c…", s: 204, ms: 51,
      req: "DELETE файла", res: "удалено", verdict: "соответствует ожиданию" }
  ];

  var snapshots = {
    begin: { at: "21.09.2026 10:12", files: 259, records: 129, loads: 12, datasets: 8, models: 6, tasks: 209, errors: [] },
    end: null
  };

  return {
    catalog: catalog,
    initialStatus: initialStatus,
    sections: sections,
    sets: sets,
    programme: programme,
    stand: stand,
    session: session,
    registries: registries,
    records: records,
    tasks: tasks,
    notes: notes,
    prospective: prospective,
    exchanges: initialExchanges,
    snapshots: snapshots
  };
})();
