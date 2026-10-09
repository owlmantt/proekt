"""Разбиение исходников на логические чанки: AST для Python, эвристики для JS/TS/MD/JSON."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path


HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")
OPENAPI_FILENAMES = {"openapi.json", "swagger.json", "openapi.yaml", "swagger.yaml"}


@dataclass
class Chunk:
    chunk_id: str
    text: str
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    http_method: str = ""
    api_path: str = ""
    operation_id: str = ""

    def metadata(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "http_method": self.http_method,
            "api_path": self.api_path,
            "operation_id": self.operation_id,
        }


def chunk_file(path: Path, content: str) -> list[Chunk]:
    suffix = path.suffix.lower()
    relative = path.as_posix()
    if suffix == ".py":
        return chunk_python(relative, content)
    if suffix in {".js", ".ts"}:
        return chunk_javascript(relative, content)
    if suffix == ".json":
        if _looks_like_openapi(path, content):
            return chunk_openapi(relative, content)
        return chunk_json(relative, content)
    if suffix == ".md":
        return chunk_markdown(relative, content)
    return [_whole_file_chunk(relative, content, kind="file")]


def chunk_python(path: str, source: str) -> list[Chunk]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [_whole_file_chunk(path, source, kind="file")]

    lines = source.splitlines()
    chunks: list[Chunk] = []

    module_header = _module_header_chunk(path, source, tree, lines)
    if module_header:
        chunks.append(module_header)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunks.append(_python_node_chunk(path, source, node, kind="function"))
        elif isinstance(node, ast.ClassDef):
            chunks.append(_python_node_chunk(path, source, node, kind="class"))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chunks.append(
                        _python_node_chunk(
                            path,
                            source,
                            item,
                            kind="method",
                            name=f"{node.name}.{item.name}",
                        )
                    )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = _assignment_names(node)
            if names:
                chunks.append(
                    _python_node_chunk(
                        path,
                        source,
                        node,
                        kind="constant",
                        name=", ".join(names),
                    )
                )

    return chunks or [_whole_file_chunk(path, source, kind="file")]


def chunk_javascript(path: str, source: str) -> list[Chunk]:
    pattern = re.compile(
        r"(?P<kind>export\s+)?(?P<body>"
        r"(?:async\s+)?function\s+(?P<fn>\w+)"
        r"|class\s+(?P<cls>\w+)"
        r"|(?:const|let|var)\s+(?P<ident>\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>"
        r")",
        re.MULTILINE,
    )
    chunks: list[Chunk] = []
    used_spans: list[tuple[int, int]] = []

    for match in pattern.finditer(source):
        start = match.start()
        block, end = _read_js_block(source, match.end())
        text = source[start:end].strip()
        if not text:
            continue
        name = match.group("fn") or match.group("cls") or match.group("ident") or "anonymous"
        kind = "class" if match.group("cls") else "function"
        chunks.append(
            _make_chunk(path, text, name=name, kind=kind, source=source, start_char=start)
        )
        used_spans.append((start, end))

    leftover = _uncovered_text(source, used_spans)
    if leftover.strip():
        chunks.insert(0, _make_chunk(path, leftover.strip(), name="<module>", kind="module", source=source, start_char=0))

    return chunks or [_whole_file_chunk(path, source, kind="file")]


def chunk_openapi(path: str, source: str) -> list[Chunk]:
    try:
        spec = json.loads(source)
    except json.JSONDecodeError:
        return [_whole_file_chunk(path, source, kind="file")]

    if not isinstance(spec, dict):
        return [_whole_file_chunk(path, source, kind="file")]

    chunks: list[Chunk] = []
    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    title = str(info.get("title") or "OpenAPI")
    version = str(info.get("version") or "")
    description = str(info.get("description") or "")
    header_lines = [
        f"# OpenAPI: {title} {version}".strip(),
        description,
        f"openapi: {spec.get('openapi') or spec.get('swagger') or ''}",
    ]
    header_text = "\n".join(line for line in header_lines if line).strip()
    chunks.append(
        _make_chunk(path, header_text, name=title, kind="openapi_info", source=source, start_char=0)
    )

    paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
    for api_path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        path_params = operations.get("parameters")
        for method, operation in operations.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            chunks.append(
                _openapi_endpoint_chunk(
                    path=path,
                    source=source,
                    spec=spec,
                    method=method.upper(),
                    api_path=str(api_path),
                    operation=operation,
                    path_params=path_params if isinstance(path_params, list) else [],
                )
            )

    schemas: dict = {}
    components = spec.get("components") if isinstance(spec.get("components"), dict) else {}
    if isinstance(components.get("schemas"), dict):
        schemas.update(components["schemas"])
    if isinstance(spec.get("definitions"), dict):
        schemas.update(spec["definitions"])
    if schemas:
        for schema_name, schema in schemas.items():
            text = (
                f"# Schema {schema_name}\n"
                f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
            )
            start_char = source.find(f'"{schema_name}"')
            chunks.append(
                _make_chunk(
                    path,
                    text,
                    name=str(schema_name),
                    kind="openapi_schema",
                    source=source,
                    start_char=start_char,
                )
            )

    return chunks or [_whole_file_chunk(path, source, kind="file")]


def chunk_json(path: str, source: str) -> list[Chunk]:
    try:
        data = json.loads(source)
    except json.JSONDecodeError:
        return [_whole_file_chunk(path, source, kind="file")]

    if isinstance(data, dict) and data:
        chunks = []
        for key, value in data.items():
            text = json.dumps({key: value}, ensure_ascii=False, indent=2)
            chunks.append(_make_chunk(path, text, name=str(key), kind="json_key", source=source, start_char=source.find(f'"{key}"')))
        return chunks
    return [_whole_file_chunk(path, source, kind="json")]


def chunk_markdown(path: str, source: str) -> list[Chunk]:
    parts = re.split(r"(?m)(?=^#{1,6}\s+)", source)
    chunks: list[Chunk] = []
    offset = 0
    for part in parts:
        text = part.strip()
        if not text:
            offset += len(part)
            continue
        heading = text.splitlines()[0]
        name = re.sub(r"^#{1,6}\s+", "", heading).strip() or "<section>"
        chunks.append(
            _make_chunk(path, text, name=name, kind="section", source=source, start_char=source.find(part, offset))
        )
        offset += len(part)
    return chunks or [_whole_file_chunk(path, source, kind="file")]


def _python_node_chunk(
    path: str,
    source: str,
    node: ast.AST,
    kind: str,
    name: str | None = None,
) -> Chunk:
    text = ast.get_source_segment(source, node) or ""
    display_name = name or getattr(node, "name", kind)
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start)
    return Chunk(
        chunk_id=_chunk_id(path, kind, display_name, start),
        text=text,
        path=path,
        name=display_name,
        kind=kind,
        start_line=start,
        end_line=end,
    )


def _module_header_chunk(path: str, source: str, tree: ast.Module, lines: list[str]) -> Chunk | None:
    first_def_line = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first_def_line = node.lineno
            break

    end_line = (first_def_line - 1) if first_def_line else len(lines)
    header_lines = lines[:end_line]
    # Константы индексируются отдельно — в модульном чанке остаются импорты и docstring.
    filtered: list[str] = []
    tree_assigns_lines = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for lineno in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                tree_assigns_lines.add(lineno)

    for idx, line in enumerate(header_lines, start=1):
        if idx in tree_assigns_lines:
            continue
        filtered.append(line)

    text = "\n".join(filtered).strip()
    if not text:
        return None
    return Chunk(
        chunk_id=_chunk_id(path, "module", "<module>", 1),
        text=text,
        path=path,
        name="<module>",
        kind="module",
        start_line=1,
        end_line=max(end_line, 1),
    )


def _assignment_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names.append(node.target.id)
    return names


def _whole_file_chunk(path: str, source: str, kind: str) -> Chunk:
    end_line = source.count("\n") + 1
    return Chunk(
        chunk_id=_chunk_id(path, kind, "<file>", 1),
        text=source,
        path=path,
        name="<file>",
        kind=kind,
        start_line=1,
        end_line=end_line,
    )


def _make_chunk(path: str, text: str, name: str, kind: str, source: str, start_char: int) -> Chunk:
    start_line = source[: max(start_char, 0)].count("\n") + 1
    end_line = start_line + text.count("\n")
    return Chunk(
        chunk_id=_chunk_id(path, kind, name, start_line),
        text=text,
        path=path,
        name=name,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
    )


def _chunk_id(path: str, kind: str, name: str, start_line: int) -> str:
    return f"{path}::{kind}::{name}::{start_line}"


def _looks_like_openapi(path: Path, source: str) -> bool:
    if path.name.lower() in OPENAPI_FILENAMES:
        return True
    try:
        data = json.loads(source)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data) and "paths" in data


def _resolve_ref(spec: dict, node, depth: int = 0):
    if depth > 4:
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


def _openapi_endpoint_chunk(
    path: str,
    source: str,
    spec: dict,
    method: str,
    api_path: str,
    operation: dict,
    path_params: list,
) -> Chunk:
    operation_id = str(operation.get("operationId") or "")
    summary = str(operation.get("summary") or "")
    tags = operation.get("tags") or []
    params = list(path_params) + list(operation.get("parameters") or [])
    request_body = _resolve_ref(spec, operation.get("requestBody")) if operation.get("requestBody") else None
    responses = _resolve_ref(spec, operation.get("responses")) if operation.get("responses") else {}

    lines = [
        f"{method} {api_path}",
        f"operationId: {operation_id}" if operation_id else "",
        f"summary: {summary}" if summary else "",
        f"tags: {', '.join(str(tag) for tag in tags)}" if tags else "",
    ]
    if params:
        lines.append("## Parameters")
        lines.append(json.dumps(_resolve_ref(spec, params), ensure_ascii=False, indent=2))
    if request_body:
        lines.append("## Request body")
        lines.append(json.dumps(request_body, ensure_ascii=False, indent=2))
    if responses:
        lines.append("## Responses")
        lines.append(json.dumps(responses, ensure_ascii=False, indent=2))

    text = "\n".join(line for line in lines if line).strip()
    name = f"{method} {api_path}"
    start_char = source.find(f'"{api_path}"')
    chunk = _make_chunk(path, text, name=name, kind="openapi_endpoint", source=source, start_char=start_char)
    chunk.http_method = method
    chunk.api_path = api_path
    chunk.operation_id = operation_id
    return chunk


def _read_js_block(source: str, from_index: int) -> tuple[str, int]:
    brace = source.find("{", from_index)
    semicolon = source.find(";", from_index)
    if brace == -1 or (semicolon != -1 and semicolon < brace):
        end = semicolon + 1 if semicolon != -1 else len(source)
        return source[from_index:end], end

    depth = 0
    in_string = None
    escaped = False
    for i in range(brace, len(source)):
        ch = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in {'"', "'", "`"}:
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[from_index : i + 1], i + 1
    return source[from_index:], len(source)


def _uncovered_text(source: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return source
    spans = sorted(spans)
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        if cursor < start:
            pieces.append(source[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(source):
        pieces.append(source[cursor:])
    return "".join(pieces)
