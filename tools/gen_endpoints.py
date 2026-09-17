"""Генератор реестра эндпоинтов `acceptance/endpoints.py` из `docs/SOM1.json`.

Зачем генератор, а не рукописный список: реестр консоли запросов (FR-T3) обязан
совпадать со спецификацией испытуемого сервера (32 операции). Ручной список
неизбежно расходится со спекой, поэтому реестр **выводится** из `docs/SOM1.json`,
а расхождение ловят тест (`tests/unit/test_endpoints.py`) и проверка
`python -m tools.gen_endpoints --check`.

Использование:

    python -m tools.gen_endpoints            # обновить блок ENDPOINTS в acceptance/endpoints.py
    python -m tools.gen_endpoints --check    # только проверить актуальность (exit 1, если устарел)
    python -m tools.gen_endpoints --report   # напечатать разбор операций, ничего не писать
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "SOM1.json"
TARGET_PATH = ROOT / "acceptance" / "endpoints.py"

BEGIN_MARKER = "# --- BEGIN GENERATED: ENDPOINTS ---"
END_MARKER = "# --- END GENERATED: ENDPOINTS ---"

HTTP_METHODS = ("get", "post", "put", "delete", "patch")

#: Ресурсоёмкие операции (FR-T10): требуют карточки запуска и подтверждения.
HEAVY_PATHS = {
    "/api/datasets/fill/{dataset_id}",
    "/api/ml_models/models/{model_id}/train",
    "/api/ml_models/{model_id}/check",
    "/api/ml_models/models/{model_id}/inference",
}

#: Явные исключения из правил по методу (иначе: DELETE → destructive,
#: POST/PUT/PATCH → write, GET → read).
SAFETY_RULES: dict[tuple[str, str], str] = {
    # диагностическая задача: создаёт задачу, но не тратит ресурсы обучения
    ("post", "/api/tasks/test"): "write",
    # одиночный инференс — синхронное вычисление без изменения данных стенда
    ("post", "/api/ml_models/models/{model_id}/inference/single"): "read",
}

#: Известные особенности эндпоинта (замечания к API из `SOM1. Комментарии.md`).
ENDPOINT_NOTES: dict[str, str] = {
    "/api/data/file/{file_id}/download": (
        "ответ специфицирован как application/json со схемой {} — фактически "
        "application/octet-stream с Content-Disposition; нет Content-Length/Range/ETag; "
        "не-ASCII имена → 404 (дефект latin-1, P0)"
    ),
    "/api/data/files": "limit/offset игнорируются сервером (P2) — пагинация клиентская",
    "/api/{file_id}": "маршрут удаления в корне /api/; batch-удаления пары нет",
    "/api/loads/list": "в ответе нет phase_connection, фильтр ph_n не влияет (P0)",
    "/api/datasets/fill/{dataset_id}": (
        "ответ без task_id (P1) — созданную задачу искать в GET /api/tasks/"
    ),
    "/api/datasets/{dataset_id}": "состав датасета и агрегаты (записи/чанки) отсутствуют (P1)",
    "/api/ml_models/models": "поле signals не заполнено (P2); типы H5/REPORT_ZIP вне enum",
    "/api/ml_models/models/upload": "multipart: файл модели (.h5) + поля формы",
    "/api/ml_models/models/{model_id}/download_onnx": (
        "модель отдаётся файлом, но спецификация описывает ответ как application/json"
    ),
    "/api/tasks/": "limit по умолчанию 100; фильтры task_type/status/start_date/end_date",
    "/api/tasks/{task_id}": "статус not_found для неизвестной задачи (BR-R7)",
}

#: Фактический вид ответа там, где спецификация расходится с сервером
#: (скачивания описаны как `application/json`, фактически отдаётся файл).
RESPONSE_OVERRIDES: dict[str, str] = {
    "/api/data/file/{file_id}/download": "binary",
    "/api/ml_models/models/{model_id}/download_onnx": "binary",
}

_SCHEMAS: dict[str, Any] = {}


def load_spec(path: Path | None = None) -> dict[str, Any]:
    """Читает спецификацию и запоминает схемы для разыменования `$ref`."""
    global _SCHEMAS  # noqa: PLW0603 — простая кэш-переменная модуля
    spec = json.loads(Path(path or SPEC_PATH).read_text(encoding="utf-8"))
    _SCHEMAS = (spec.get("components") or {}).get("schemas") or {}
    return spec


def resolve(schema: Any) -> dict[str, Any]:
    """Разыменовывает схему (`$ref`, `anyOf`, `oneOf`, `allOf`)."""
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        return _SCHEMAS.get(ref.rsplit("/", 1)[-1]) or {}
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                resolved = resolve(variant)
                if resolved:
                    return resolved
    return schema


def sample_value(name: str, schema: Any) -> Any:
    """Строит пример значения по схеме (для заготовки тела запроса)."""
    resolved = resolve(schema)
    if "default" in resolved:
        return resolved["default"]
    if resolved.get("enum"):
        return resolved["enum"][0]

    kind = resolved.get("type")
    if kind == "integer":
        return 1
    if kind == "number":
        return 0.5
    if kind == "boolean":
        return False
    if kind == "array":
        return [sample_value(f"{name}_item", resolved.get("items") or {})]
    if kind == "object" or resolved.get("properties"):
        return sample_object(resolved)

    fmt = resolved.get("format")
    if fmt == "uuid":
        return "<uuid>"
    if fmt in ("date-time", "date"):
        return "2026-09-15T10:00:00"
    return f"<{name}>"


def sample_object(schema: dict[str, Any]) -> dict[str, Any]:
    """Пример объекта: обязательные свойства и свойства со значением по умолчанию."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    selected = {
        key: value
        for key, value in properties.items()
        if key in required or "default" in resolve(value)
    }
    return {key: sample_value(key, value) for key, value in selected.items()}


def sample_body(schema: Any) -> dict[str, Any]:
    """Пример JSON-тела по схеме запроса."""
    resolved = resolve(schema)
    body = sample_object(resolved)
    if body:
        return body
    properties = resolved.get("properties") or {}
    return {key: sample_value(key, value) for key, value in list(properties.items())[:3]}


def build_param(raw: dict[str, Any]) -> dict[str, Any]:
    """Собирает описание параметра операции из спецификации."""
    schema = resolve(raw.get("schema") or {})
    enum = tuple(str(item) for item in schema.get("enum") or ())
    kind = schema.get("type")
    if not kind:
        kind = "array" if "items" in schema else "string"
    raw_default = raw.get("default", schema.get("default"))

    return {
        "name": str(raw.get("name", "")),
        "location": str(raw.get("in", "query")),
        "type": str(kind),
        "format": str(schema.get("format") or ""),
        "required": bool(raw.get("required")),
        "description": _one_line(raw.get("description") or schema.get("description") or ""),
        "enum": enum,
        "default": "" if raw_default is None else str(raw_default),
    }


def build_one(raw_method: str, path: str, operation: dict[str, Any]) -> dict[str, Any]:
    """Собирает описание одной операции (поля `EndpointSpec`)."""
    method = raw_method.upper()
    params = [build_param(raw) for raw in operation.get("parameters") or []]
    placeholders = set(re.findall(r"\{([^}]+)\}", path))
    declared = {param["name"] for param in params if param["location"] == "path"}
    missing = placeholders - declared
    if missing:
        raise ValueError(f"{method} {path}: в спецификации нет path-параметров {sorted(missing)}")

    body = operation.get("requestBody") or {}
    body_kind, body_fields, body_sample = _body_details(body)

    return {
        "key": f"{raw_method} {path}",
        "module": _module(operation, path),
        "method": method,
        "path": path,
        "summary": _one_line(operation.get("summary") or operation.get("operationId") or path),
        "operation_id": str(operation.get("operationId") or ""),
        "params": params,
        "body_kind": body_kind,
        "body_required": bool(body.get("required")),
        "body_fields": body_fields,
        "body_sample": body_sample,
        "response_kind": RESPONSE_OVERRIDES.get(path)
        or _response_kind(operation.get("responses") or {}),
        "safety": classify(method, path),
        "note": ENDPOINT_NOTES.get(path, ""),
    }


def build_specs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Все операции спецификации в порядке документа."""
    return [
        build_one(raw_method, path, operation)
        for path, item in (spec.get("paths") or {}).items()
        for raw_method, operation in item.items()
        if raw_method in HTTP_METHODS
    ]


def classify(method: str, path: str) -> str:
    """Класс безопасности операции (`read`/`write`/`destructive`/`heavy`)."""
    rule = SAFETY_RULES.get((method.lower(), path))
    if rule:
        return rule
    if path in HEAVY_PATHS:
        return "heavy"
    if method.upper() == "DELETE":
        return "destructive"
    if method.upper() in ("POST", "PUT", "PATCH"):
        return "write"
    return "read"


def _module(operation: dict[str, Any], path: str) -> str:
    """Модуль операции: первый тег спецификации (иначе — сегмент пути)."""
    tags = operation.get("tags") or []
    if tags:
        return str(tags[0])
    parts = [part for part in path.split("/") if part]
    return parts[1] if len(parts) > 1 else "Прочее"


def _body_details(body: dict[str, Any]) -> tuple[str, list[str], str]:
    """Вид тела, список полей и заготовка JSON-тела."""
    content = body.get("content") or {}
    if not content:
        return "none", [], ""
    if "multipart/form-data" in content:
        schema = resolve((content["multipart/form-data"] or {}).get("schema") or {})
        fields = _fields(schema)
        return "multipart", fields, ""
    if "application/json" in content:
        schema = (content["application/json"] or {}).get("schema") or {}
        sample = sample_body(schema)
        return "json", _fields(resolve(schema)), json.dumps(sample, ensure_ascii=False, indent=2)
    return "none", [], ""


def _fields(schema: dict[str, Any]) -> list[str]:
    """Поля схемы как «имя: тип» (для `binary`/`uuid` предпочитается формат)."""
    properties = schema.get("properties") or {}
    fields: list[str] = []
    for key, value in properties.items():
        resolved = resolve(value)
        fmt = resolved.get("format")
        kind = fmt if fmt in ("binary", "uuid", "date-time") else resolved.get("type")
        fields.append(f"{key}: {kind or fmt or 'object'}")
    return fields


def _response_kind(responses: dict[str, Any]) -> str:
    """Вид ответа: `json`, `binary` (файл) или `empty` (нет тела)."""
    for status in ("200", "201", "202", "204"):
        response = responses.get(status)
        if not response:
            continue
        content = (response or {}).get("content") or {}
        if not content:
            return "empty"
        if "application/octet-stream" in content:
            return "binary"
        return "json"
    return "json"


def _one_line(value: Any) -> str:
    """Схлопывает многострочные описания спецификации в одну строку."""
    return " ".join(str(value).split())


# ---------------------------------------------------------------------------
# Рендеринг блока реестра и обновление файла
# ---------------------------------------------------------------------------
def py_str(value: str) -> str:
    """Python-литерал строки: короткие — в кавычках, многострочные — в тройных."""
    if "\n" in value and "'''" not in value and not value.endswith("'"):
        return "'''" + value + "'''"
    if '"' not in value and "\\" not in value:
        return f'"{value}"'
    if "'" not in value and "\\" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def render_param(param: dict[str, Any], indent: str = " " * 12) -> str:
    """Пишет вызов `ParamSpec(...)`."""
    lines = [f"{indent}ParamSpec("]
    lines.append(f"{indent}    name={py_str(param['name'])},")
    lines.append(f"{indent}    location={param['location'].upper()},")
    lines.append(f"{indent}    type={py_str(param['type'])},")
    if param["format"]:
        lines.append(f"{indent}    format={py_str(param['format'])},")
    if param["required"]:
        lines.append(f"{indent}    required=True,")
    if param["description"]:
        lines.append(f"{indent}    description={py_str(param['description'])},")
    if param["enum"]:
        items = ", ".join(py_str(item) for item in param["enum"])
        lines.append(f"{indent}    enum=({items},),")
    if param["default"]:
        lines.append(f"{indent}    default={py_str(param['default'])},")
    lines.append(f"{indent}),")
    return "\n".join(lines)


def render_spec(spec: dict[str, Any]) -> str:
    """Пишет вызов `EndpointSpec(...)` одной операции."""
    indent = " " * 8
    lines = [f"{indent}EndpointSpec("]
    lines.append(f"{indent}    key={py_str(spec['key'])},")
    lines.append(f"{indent}    module={py_str(spec['module'])},")
    lines.append(f"{indent}    method={py_str(spec['method'])},")
    lines.append(f"{indent}    path={py_str(spec['path'])},")
    lines.append(f"{indent}    summary={py_str(spec['summary'])},")
    if spec["operation_id"]:
        lines.append(f"{indent}    operation_id={py_str(spec['operation_id'])},")
    if spec["params"]:
        lines.append(f"{indent}    params=(")
        for param in spec["params"]:
            lines.append(render_param(param, indent + "    "))
        lines.append(f"{indent}    ),")
    if spec["body_kind"] != "none":
        lines.append(f"{indent}    body_kind=BODY_{spec['body_kind'].upper()},")
        if spec["body_required"]:
            lines.append(f"{indent}    body_required=True,")
        if spec["body_fields"]:
            items = ", ".join(py_str(field) for field in spec["body_fields"])
            lines.append(f"{indent}    body_fields=({items},),")
        if spec["body_sample"]:
            lines.append(f"{indent}    body_sample={py_str(spec['body_sample'])},")
    lines.append(f"{indent}    response_kind=RESPONSE_{spec['response_kind'].upper()},")
    lines.append(f"{indent}    safety=Safety.{spec['safety'].upper()},")
    if spec["note"]:
        lines.append(f"{indent}    note={py_str(spec['note'])},")
    lines.append(f"{indent}),")
    return "\n".join(lines)


def render_registry(specs: list[dict[str, Any]]) -> str:
    """Пишет сгенерированный блок `ENDPOINTS` целиком (вместе с маркерами)."""
    lines = [
        BEGIN_MARKER,
        "# Блок сформирован командой `python -m tools.gen_endpoints` из `docs/SOM1.json`.",
        "# Руками не правится: изменения теряются при следующем запуске генератора;",
        "# расхождение со спецификацией проверяется тестом tests/unit/test_endpoints.py.",
        "ENDPOINTS: tuple[EndpointSpec, ...] = (",
    ]
    lines.extend(render_spec(spec) for spec in specs)
    lines.append(")")
    lines.append(END_MARKER)
    return "\n".join(lines)


def patch_text(text: str, block: str) -> str:
    """Заменяет блок между маркерами (или добавляет его в конец файла)."""
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin == -1 or end == -1:
        raise ValueError(f"в файле нет маркеров {BEGIN_MARKER} / {END_MARKER}")
    return f"{text[:begin]}{block}{text[end + len(END_MARKER) :]}"


def current_block(text: str) -> str:
    """Содержимое блока между маркерами (как есть)."""
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin == -1 or end == -1:
        raise ValueError("в файле нет маркеров сгенерированного блока")
    return text[begin : end + len(END_MARKER)]


# ---------------------------------------------------------------------------
# Сверка реестра со спецификацией (устойчива к форматированию кода)
# ---------------------------------------------------------------------------
def spec_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """Поля сгенерированного описания операции в виде, пригодном для сравнения."""
    return {
        "key": spec["key"],
        "module": spec["module"],
        "method": spec["method"],
        "path": spec["path"],
        "summary": spec["summary"],
        "operation_id": spec["operation_id"],
        "params": tuple(
            (
                param["name"],
                param["location"],
                param["type"],
                param["format"],
                param["required"],
                param["description"],
                tuple(param["enum"]),
                param["default"],
            )
            for param in spec["params"]
        ),
        "body_kind": spec["body_kind"],
        "body_required": spec["body_required"],
        "body_fields": tuple(spec["body_fields"]),
        "body_sample": spec["body_sample"],
        "response_kind": spec["response_kind"],
        "safety": spec["safety"],
        "note": spec["note"],
    }


def endpoint_fields(endpoint: Any) -> dict[str, Any]:
    """Поля готового `EndpointSpec` из реестра — в том же виде."""
    return {
        "key": endpoint.key,
        "module": endpoint.module,
        "method": endpoint.method,
        "path": endpoint.path,
        "summary": endpoint.summary,
        "operation_id": endpoint.operation_id,
        "params": tuple(
            (
                param.name,
                param.location,
                param.type,
                param.format,
                param.required,
                param.description,
                tuple(param.enum),
                param.default,
            )
            for param in endpoint.params
        ),
        "body_kind": endpoint.body_kind,
        "body_required": endpoint.body_required,
        "body_fields": tuple(endpoint.body_fields),
        "body_sample": endpoint.body_sample,
        "response_kind": endpoint.response_kind,
        "safety": str(endpoint.safety),
        "note": endpoint.note,
    }


def differences(specs: list[dict[str, Any]], endpoints: Any) -> list[str]:
    """Расхождения между спецификацией и реестром (пустой список — всё совпадает)."""
    registry = tuple(endpoints)
    problems: list[str] = []
    if len(registry) != len(specs):
        problems.append(f"операций в спецификации {len(specs)}, в реестре {len(registry)}")
        return problems

    for spec, endpoint in zip(specs, registry, strict=False):
        expected = spec_fields(spec)
        actual = endpoint_fields(endpoint)
        for field, value in expected.items():
            if actual.get(field) != value:
                problems.append(
                    f"{spec['key']}: поле {field} — в спецификации {value!r}, в реестре "
                    f"{actual.get(field)!r}"
                )
    return problems


def format_with_ruff(path: Path) -> bool:
    """Форматирует файл `ruff format`, если он доступен (необязательный шаг)."""
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "ruff", "format", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    """Обновляет или проверяет реестр эндпоинтов."""
    parser = argparse.ArgumentParser(description="Реестр эндпоинтов консоли из docs/SOM1.json")
    parser.add_argument("--check", action="store_true", help="только проверить актуальность")
    parser.add_argument("--report", action="store_true", help="напечатать разбор операций")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH, help="путь к спецификации")
    parser.add_argument("--target", type=Path, default=TARGET_PATH, help="файл реестра")
    parser.add_argument(
        "--no-format", action="store_true", help="не запускать ruff format после записи"
    )
    args = parser.parse_args(argv)

    specs = build_specs(load_spec(args.spec))
    if args.report:
        for spec in specs:
            print(
                f"{spec['module']:<14} {spec['method']:<6} {spec['path']:<46} "
                f"{spec['safety']:<11} тело={spec['body_kind']:<9} "
                f"параметров={len(spec['params'])} ответ={spec['response_kind']}"
            )
        print(f"всего операций: {len(specs)}")

    if args.check:
        problems = differences(specs, registry_endpoints())
        if not problems:
            print(f"реестр актуален: {len(specs)} операций ({args.target.as_posix()})")
            return 0
        print(
            "реестр УСТАРЕЛ: запустите `python -m tools.gen_endpoints`, чтобы пересобрать его из "
            f"{args.spec.as_posix()}",
            file=sys.stderr,
        )
        for problem in problems[:20]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if args.report:
        return 0

    text = args.target.read_text(encoding="utf-8")
    patched = patch_text(text, render_registry(specs))
    compile(patched, str(args.target), "exec")  # защита от неверной генерации
    args.target.write_text(patched, encoding="utf-8")
    formatted = not args.no_format and format_with_ruff(args.target)
    print(
        f"реестр обновлён: {len(specs)} операций → {args.target.as_posix()}"
        + ("" if formatted else " (stdlib-форматирование: запустите ruff format вручную)")
    )
    return 0


def registry_endpoints() -> Any:
    """Реестр из `acceptance/endpoints.py` (импорт откладывается до проверки)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from acceptance.endpoints import ENDPOINTS

    return ENDPOINTS


if __name__ == "__main__":
    raise SystemExit(main())
