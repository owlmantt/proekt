"""Контракты API: манифест, срез одной ручки и сравнение размера с полным OpenAPI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from ignore import is_ignored, load_ignore_spec

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
CONTRACT_NAMES = {"openapi.json", "swagger.json", "openapi.yaml", "swagger.yaml", "openapi.yml", "swagger.yml"}
MANIFEST_NAME = "hub_manifest.json"


def estimate_tokens(text: str) -> int:
    """Грубая оценка: около 4 символов на токен. Это не токенизатор модели."""
    return max(1, (len(text) + 3) // 4)


def load_manifest(root: Path) -> dict:
    path = root / MANIFEST_NAME
    if not path.is_file():
        return {"project": root.name, "services": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"project": root.name, "services": []}


def discover_contracts(root: Path) -> list[Path]:
    root = root.resolve()
    spec = load_ignore_spec(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not is_ignored(root, current / name, spec)]
        for name in filenames:
            if name.lower() not in CONTRACT_NAMES:
                continue
            path = current / name
            if is_ignored(root, path, spec):
                continue
            found.append(path)
    return sorted(found)


def contract_files(root: Path) -> list[Path]:
    root = root.resolve()
    uploaded: list[Path] = []
    upload_dir = root / "uploads"
    if upload_dir.is_dir():
        uploaded = sorted(
            (path for path in upload_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    manifest = load_manifest(root)
    listed: list[Path] = []
    for service in manifest.get("services") or []:
        if not isinstance(service, dict):
            continue
        for rel in service.get("contracts") or []:
            path = (root / str(rel)).resolve()
            if path.is_file():
                listed.append(path)
    discovered = listed or discover_contracts(root)
    result: list[Path] = []
    seen: set[str] = set()
    for path in [*uploaded, *discovered]:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def load_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _resolve_ref(spec: dict, node, depth: int = 0):
    if depth > 6:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            current = spec
            for part in ref[2:].split("/"):
                if not isinstance(current, dict):
                    return node
                current = current.get(part)
                if current is None:
                    return node
            return _resolve_ref(spec, current, depth + 1)
        return {key: _resolve_ref(spec, value, depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve_ref(spec, item, depth + 1) for item in node]
    return node


def _operations(spec: dict):
    paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
    for api_path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            yield method.upper(), str(api_path), operation


def _find_operation(spec: dict, method: str, api_path: str):
    method = method.upper()
    for item_method, item_path, operation in _operations(spec):
        if item_method == method and item_path == api_path:
            return operation
    return None


def _ref_tail(node: dict) -> str:
    if not isinstance(node, dict):
        return ""
    content = node.get("content") if isinstance(node.get("content"), dict) else {}
    app_json = content.get("application/json") if isinstance(content.get("application/json"), dict) else {}
    schema = app_json.get("schema") if isinstance(app_json.get("schema"), dict) else {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        return ref.rsplit("/", 1)[-1]
    return ""


def _schema_name(schema: dict) -> str:
    if not isinstance(schema, dict):
        return ""
    title = schema.get("title")
    return str(title) if title else ""


def _field_lines(schema: dict) -> list[str]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required") or []}
    lines: list[str] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            lines.append(f"  {name}")
            continue
        type_name = str(prop.get("type") or "object")
        notes: list[str] = []
        if name in required:
            notes.append("required")
        if prop.get("description"):
            notes.append(str(prop["description"]))
        if "minLength" in prop:
            notes.append(f"minLength={prop['minLength']}")
        suffix = f" ({', '.join(notes)})" if notes else ""
        lines.append(f"  {name}: {type_name}{suffix}")
    return lines


def _response_schema(spec: dict, response: dict) -> dict:
    if not isinstance(response, dict):
        return {}
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    app_json = content.get("application/json") if isinstance(content.get("application/json"), dict) else {}
    schema = app_json.get("schema")
    if not isinstance(schema, dict):
        return {}
    resolved = _resolve_ref(spec, schema)
    return resolved if isinstance(resolved, dict) else {}


def format_manifest(root: Path) -> str:
    manifest = load_manifest(root)
    lines = [f"Проект: {manifest.get('project') or root.name}"]
    services = manifest.get("services") or []
    if not services:
        lines.append("Сервисы не заданы.")
        return "\n".join(lines)
    for service in services:
        if not isinstance(service, dict):
            continue
        lines.append(f"Сервис {service.get('name') or 'unnamed'}")
        lines.append(f"  владелец: {service.get('owner') or 'не указан'}")
        consumers = service.get("consumers") or []
        if consumers:
            lines.append(f"  потребители: {', '.join(str(item) for item in consumers)}")
        contracts = service.get("contracts") or []
        if contracts:
            lines.append(f"  контракты: {', '.join(str(item) for item in contracts)}")
    return "\n".join(lines)


def format_errors(root: Path, method: str = "", api_path: str = "") -> str:
    method = method.upper()
    blocks: list[str] = []
    for path in contract_files(root):
        spec = load_spec(path)
        rel = path.resolve().relative_to(root.resolve()).as_posix()
        for item_method, item_path, operation in _operations(spec):
            if method and item_method != method:
                continue
            if api_path and item_path != api_path:
                continue
            responses = operation.get("responses") if isinstance(operation.get("responses"), dict) else {}
            errors = []
            for code, response in responses.items():
                if not str(code).isdigit() or int(code) < 400:
                    continue
                description = ""
                if isinstance(response, dict):
                    description = str(response.get("description") or "")
                errors.append(f"  {code} — {description}".rstrip())
            if not errors:
                continue
            blocks.append(f"{item_method} {item_path}  ({rel})\n" + "\n".join(errors))
    if not blocks:
        target = f"{method} {api_path}".strip()
        return f"Коды ошибок не найдены{': ' + target if target else ''}."
    return "\n\n".join(blocks)


def format_client_context_from_text(
    text: str,
    rel: str,
    method: str,
    api_path: str,
    role: str = "frontend",
) -> str:
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        return f"Ручка {method.upper()} {api_path} не найдена в релизе."
    rendered = _render_operation(spec if isinstance(spec, dict) else {}, rel, method, api_path, role)
    return rendered or f"Ручка {method.upper()} {api_path} не найдена в релизе."


def format_client_context(root: Path, method: str, api_path: str, role: str = "frontend") -> str:
    method = method.upper()
    role = (role or "frontend").strip().lower()
    if role not in {"frontend", "flutter"}:
        role = "frontend"
    for path in contract_files(root):
        spec = load_spec(path)
        rel = path.resolve().relative_to(root.resolve()).as_posix()
        rendered = _render_operation(spec, rel, method, api_path, role)
        if rendered:
            return rendered
    return f"Ручка {method.upper()} {api_path} не найдена в контрактах."


def _render_operation(spec: dict, rel: str, method: str, api_path: str, role: str) -> str | None:
    method = method.upper()
    role = (role or "frontend").strip().lower()
    if role not in {"frontend", "flutter"}:
        role = "frontend"
    operation = _find_operation(spec, method, api_path)
    if operation is None:
        return None
    lines = [
        f"role: {role}",
        f"{method} {api_path}",
        f"contract: {rel}",
    ]
    if operation.get("operationId"):
        lines.append(f"operationId: {operation['operationId']}")
    if operation.get("summary"):
        lines.append(f"summary: {operation['summary']}")

    request = operation.get("requestBody")
    if isinstance(request, dict):
        resolved = _resolve_ref(spec, request)
        content = resolved.get("content") if isinstance(resolved, dict) else {}
        app_json = content.get("application/json") if isinstance(content, dict) else {}
        schema = app_json.get("schema") if isinstance(app_json, dict) else {}
        if isinstance(schema, dict):
            title = _ref_tail(request) or _schema_name(schema) or "body"
            required = "required" if resolved.get("required") else "optional"
            lines.append(f"Request {title} ({required}):")
            lines.extend(_field_lines(schema) or ["  схема без полей"])

    parameters = operation.get("parameters") if isinstance(operation.get("parameters"), list) else []
    if parameters:
        lines.append("Parameters:")
        for param in parameters:
            if not isinstance(param, dict):
                continue
            where = param.get("in") or "query"
            name = param.get("name") or "param"
            necessity = "required" if param.get("required") else "optional"
            lines.append(f"  {where} {name} ({necessity})")

    responses = operation.get("responses") if isinstance(operation.get("responses"), dict) else {}
    if responses:
        lines.append("Responses:")
        for code, response in responses.items():
            description = response.get("description") if isinstance(response, dict) else ""
            schema_name = _ref_tail(response) if isinstance(response, dict) else ""
            label = f"{code} {schema_name}".strip()
            lines.append(f"  {label}: {description}")
            fields = _field_lines(_response_schema(spec, response if isinstance(response, dict) else {}))
            lines.extend(f"  {line}" if line.startswith("  ") else line for line in fields)
    return "\n".join(lines)


def measure_context(root: Path, method: str, api_path: str, role: str = "frontend") -> dict:
    method = method.upper()
    slice_text = format_client_context(root, method, api_path, role=role)
    full_text = ""
    contract = ""
    for path in contract_files(root):
        spec = load_spec(path)
        if _find_operation(spec, method, api_path) is None:
            continue
        full_text = path.read_text(encoding="utf-8")
        contract = path.resolve().relative_to(root.resolve()).as_posix()
        break
    full_tokens = estimate_tokens(full_text) if full_text else 0
    slice_tokens = estimate_tokens(slice_text)
    return {
        "method": method,
        "path": api_path,
        "role": role,
        "contract": contract,
        "full_chars": len(full_text),
        "full_tokens": full_tokens,
        "slice_chars": len(slice_text),
        "slice_tokens": slice_tokens,
        "slice": slice_text,
    }


def format_budget(measured: dict) -> str:
    full_tokens = int(measured.get("full_tokens") or 0)
    slice_tokens = int(measured.get("slice_tokens") or 0)
    share = (slice_tokens / full_tokens * 100) if full_tokens else 0
    return "\n".join(
        [
            f"Роль: {measured.get('role')}",
            f"Ручка: {measured.get('method')} {measured.get('path')}",
            f"Контракт: {measured.get('contract') or 'не найден'}",
            f"Весь файл: {measured.get('full_chars')} символов, около {full_tokens} токенов",
            f"Срез ручки: {measured.get('slice_chars')} символов, около {slice_tokens} токенов",
            f"Срез занимает {share:.0f}% файла",
            "Оценка токенов: длина текста в символах / 4, это не токенизатор модели.",
        ]
    )
