"""MCP Context Hub server.

Serves three things from one process:
  1. The web UI (static files from ./web) at "/"
  2. A REST API (/api/search, /api/stats) that the web UI calls, backed by
     the same ChromaDB collection and hybrid search used everywhere else.
  3. The original MCP tools (search_code, list_files, ...) exposed over SSE
     at /mcp/sse, for Cursor / Claude / VS Code MCP clients.

Run with:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from pathlib import Path

import chromadb
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from vault_state import VaultLocked
from wallet_api import agent_context, mount_wallet_api, proof_text

from contracts import format_budget, format_client_context, format_errors, format_manifest, measure_context
from hybrid_search import hybrid_search
from indexer import ALLOWED_EXTENSIONS, PROJECT_ROOT, clear_index, index_single_file, is_indexable_file
from roles import allows

CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "codebase"
WEB_DIR = Path(__file__).resolve().parent / "web"

# ---------------------------------------------------------------------------
# Shared Chroma client / collection (used by both the MCP tools and the API)
# ---------------------------------------------------------------------------

client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

# Роль — пакет видов контекста из roles.allows, а не фильтр по расширению.

mcp = FastMCP("Local Code Hub")


def _format_hit(idx: int, item: dict) -> str:
    metadata = item.get("metadata") or {}
    file_path = metadata.get("path", "unknown")
    name = metadata.get("name", "")
    kind = metadata.get("kind", "")
    start_line = metadata.get("start_line")
    end_line = metadata.get("end_line")
    rrf_score = item.get("rrf_score")
    distance = item.get("vector_distance")
    http_method = metadata.get("http_method") or ""
    api_path = metadata.get("api_path") or ""

    location = file_path
    if start_line:
        location += f":{start_line}"
        if end_line:
            location += f"-{end_line}"

    header = f"--- Результат {idx}: {location}"
    extras: list[str] = []
    if kind:
        extras.append(str(kind))
    if name:
        extras.append(str(name))
    if http_method and api_path:
        extras.append(f"{http_method} {api_path}")
    if rrf_score is not None:
        extras.append(f"rrf={rrf_score:.4f}")
    if distance is not None:
        extras.append(f"distance={distance:.4f}")
    if extras:
        header += f" ({', '.join(extras)})"
    return f"{header}\n{item.get('document') or ''}"


def _all_rows() -> list[dict]:
    raw = collection.get(include=["documents", "metadatas"])
    ids = raw.get("ids") or []
    documents = raw.get("documents") or []
    metadatas = raw.get("metadatas") or []
    rows = []
    for i, doc_id in enumerate(ids):
        rows.append(
            {
                "id": doc_id,
                "document": documents[i] if i < len(documents) else "",
                "metadata": metadatas[i] if i < len(metadatas) else {},
            }
        )
    return rows


def _filter_rows(
    kind: str = "",
    path: str = "",
    name: str = "",
    http_method: str = "",
    api_path: str = "",
) -> list[dict]:
    rows = _all_rows()
    result = []
    for row in rows:
        meta = row.get("metadata") or {}
        if kind and str(meta.get("kind", "")).lower() != kind.lower():
            continue
        if path and path not in str(meta.get("path", "")):
            continue
        if name and name.lower() not in str(meta.get("name", "")).lower():
            continue
        if http_method and str(meta.get("http_method", "")).upper() != http_method.upper():
            continue
        if api_path and api_path not in str(meta.get("api_path", "")):
            continue
        result.append(row)
    return result


def _outline_line(row: dict) -> str:
    meta = row.get("metadata") or {}
    kind = meta.get("kind", "")
    name = meta.get("name", "")
    start = meta.get("start_line", "")
    end = meta.get("end_line", "")
    path = meta.get("path", "")
    extra = ""
    if meta.get("http_method") and meta.get("api_path"):
        extra = f" [{meta.get('http_method')} {meta.get('api_path')}]"
    return f"{path}:{start}-{end}  {kind}  {name}{extra}"


def _row_covers_line(row: dict, line: int) -> bool:
    meta = row.get("metadata") or {}
    try:
        start = int(meta.get("start_line") or 1)
        end = int(meta.get("end_line") or start)
    except (TypeError, ValueError):
        return False
    return start <= line <= end


def _span_size(row: dict) -> int:
    meta = row.get("metadata") or {}
    try:
        start = int(meta.get("start_line") or 1)
        end = int(meta.get("end_line") or start)
    except (TypeError, ValueError):
        return 10**9
    return max(end - start, 0)


@mcp.tool(description="Гибридный поиск. role: frontend, flutter, backend, devops или all. frontend и flutter видят только контракт API.")
def search_code(query: str, limit: int = 3, kind: str = "", path: str = "", role: str = "") -> str:
    fetch = max(limit, limit * 8) if (kind or path) else limit
    try:
        results = hybrid_search(collection, query=query, limit=fetch, role=role)
    except VaultLocked:
        return "Хранилище закрыто. Введите пароль проекта на этой машине. С диска текст чанков не читается."
    if kind or path:
        filtered = []
        for item in results:
            meta = item.get("metadata") or {}
            if kind and str(meta.get("kind", "")).lower() != kind.lower():
                continue
            if path and path not in str(meta.get("path", "")):
                continue
            filtered.append(item)
        results = filtered[:limit]
    if not results:
        return "Ничего не найдено."
    return "\n\n".join(_format_hit(idx, item) for idx, item in enumerate(results, start=1))


@mcp.tool(description="Список проиндексированных файлов.")
def list_files() -> str:
    paths = sorted({(row["metadata"] or {}).get("path", "") for row in _all_rows() if (row["metadata"] or {}).get("path")})
    if not paths:
        return "Индекс пуст."
    return "\n".join(f"{i}. {path}" for i, path in enumerate(paths, start=1))


@mcp.tool(description="Оглавление символов. kind: function, class, method, constant, openapi_endpoint, openapi_schema.")
def list_symbols(kind: str = "", path: str = "") -> str:
    rows = _filter_rows(kind=kind, path=path)
    rows.sort(key=lambda row: (
        (row["metadata"] or {}).get("path", ""),
        (row["metadata"] or {}).get("start_line", 0),
    ))
    if not rows:
        return "Символы не найдены."
    return "\n".join(_outline_line(row) for row in rows)


@mcp.tool(description="Найти символ по имени (функция, класс, константа, operationId).")
def get_symbol(name: str) -> str:
    rows = _filter_rows(name=name)
    if not rows:
        return f"Символ '{name}' не найден."
    parts = []
    for idx, row in enumerate(rows, start=1):
        parts.append(_format_hit(idx, row))
    return "\n\n".join(parts)


@mcp.tool(description="Структура одного файла: чанки по порядку строк.")
def get_file_outline(path: str) -> str:
    rows = _filter_rows(path=path)
    if not rows:
        return f"Файл '{path}' не найден в индексе."
    rows.sort(key=lambda row: (row["metadata"] or {}).get("start_line", 0))
    return "\n".join(_outline_line(row) for row in rows)


@mcp.tool(description="Прочитать исходник с диска (опционально диапазон строк).")
def read_source(path: str, start_line: int = 1, end_line: int = 0) -> str:
    if os.environ.get("HUB_PUBLIC", "").strip().lower() in {"1", "true", "yes"}:
        return "В публичном режиме чтение файлов с диска отключено. Используйте поиск по индексу."
    file_path = (PROJECT_ROOT / path).resolve()
    try:
        file_path.relative_to(PROJECT_ROOT)
    except ValueError:
        return "Путь вне проекта."
    if not is_indexable_file(file_path) and not file_path.is_file():
        return "Файл недоступен."
    if not file_path.is_file():
        return "Файл не найден."
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start_line)
    end = end_line if end_line > 0 else len(lines)
    end = min(end, len(lines))
    if start > end:
        return "Пустой диапазон."
    numbered = [f"{i:>4}| {lines[i - 1]}" for i in range(start, end + 1)]
    return f"{path}:{start}-{end}\n" + "\n".join(numbered)


@mcp.tool(description="Самый узкий чанк, покрывающий указанную строку файла.")
def get_chunk_at_line(path: str, line: int) -> str:
    rows = [row for row in _filter_rows(path=path) if _row_covers_line(row, line)]
    if not rows:
        return f"Чанк для {path}:{line} не найден."
    rows.sort(key=_span_size)
    return _format_hit(1, rows[0])


@mcp.tool(description="Чанк на строке и соседние чанки того же файла.")
def get_neighbors(path: str, line: int) -> str:
    rows = _filter_rows(path=path)
    if not rows:
        return f"Файл '{path}' не найден в индексе."
    rows.sort(key=lambda row: (row["metadata"] or {}).get("start_line", 0))
    current_idx = None
    for i, row in enumerate(rows):
        if _row_covers_line(row, line):
            if current_idx is None or _span_size(row) < _span_size(rows[current_idx]):
                current_idx = i
    if current_idx is None:
        return f"Чанк для {path}:{line} не найден."
    start = max(0, current_idx - 1)
    end = min(len(rows), current_idx + 2)
    parts = []
    for offset, row in enumerate(rows[start:end], start=1):
        label = "текущий" if start + offset - 1 == current_idx else "сосед"
        parts.append(f"[{label}]\n{_format_hit(offset, row)}")
    return "\n\n".join(parts)


@mcp.tool(description="Поиск подстроки или regex по тексту чанков.")
def grep_code(query: str, path: str = "", kind: str = "", regex: bool = False, limit: int = 20) -> str:
    rows = _filter_rows(kind=kind, path=path)
    hits: list[dict] = []
    pattern = None
    if regex:
        try:
            pattern = re.compile(query)
        except re.error as exc:
            return f"Некорректный regex: {exc}"
    for row in rows:
        text = row.get("document") or ""
        if pattern is not None:
            if not pattern.search(text):
                continue
        elif query.lower() not in text.lower():
            continue
        hits.append(row)
        if len(hits) >= limit:
            break
    if not hits:
        return "Совпадений нет."
    return "\n\n".join(_format_hit(idx, row) for idx, row in enumerate(hits, start=1))


@mcp.tool(description="Упоминания символа в чанках: и определения, и использования.")
def find_references(symbol: str, limit: int = 20) -> str:
    token = re.escape(symbol)
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){token}(?![A-Za-z0-9_])")
    hits = []
    for row in _all_rows():
        text = row.get("document") or ""
        if not pattern.search(text):
            continue
        hits.append(row)
        if len(hits) >= limit:
            break
    if not hits:
        return f"Упоминания '{symbol}' не найдены."
    return "\n\n".join(_format_hit(idx, row) for idx, row in enumerate(hits, start=1))


@mcp.tool(description="Все OpenAPI-ручки: METHOD path — operationId / summary.")
def list_api_endpoints() -> str:
    rows = _filter_rows(kind="openapi_endpoint")
    if not rows:
        return "OpenAPI-эндпоинты не найдены."
    lines = []
    for row in sorted(rows, key=lambda item: (item["metadata"] or {}).get("api_path", "")):
        meta = row["metadata"] or {}
        method = meta.get("http_method", "")
        api_path = meta.get("api_path", "")
        op = meta.get("operation_id", "")
        name = meta.get("name", "")
        lines.append(f"{method:6} {api_path}  ({op or name})")
    return "\n".join(lines)


@mcp.tool(description="Полный контракт одной ручки, например method='POST', path='/api/v1/auth/login'.")
def get_api_endpoint(method: str, path: str) -> str:
    rows = _filter_rows(kind="openapi_endpoint", http_method=method, api_path=path)
    if not rows:
        return f"Эндпоинт {method.upper()} {path} не найден."
    return "\n\n".join(_format_hit(idx, row) for idx, row in enumerate(rows, start=1))


@mcp.tool(description="Компонент OpenAPI/Swagger по имени схемы, например LoginRequest.")
def get_openapi_schema(name: str) -> str:
    rows = _filter_rows(kind="openapi_schema", name=name)
    if not rows:
        return f"Схема '{name}' не найдена."
    return "\n\n".join(_format_hit(idx, row) for idx, row in enumerate(rows, start=1))


@mcp.tool(description="Коды ошибок OpenAPI, статусы 4xx и 5xx. method и path необязательны.")
def list_api_errors(method: str = "", path: str = "") -> str:
    return format_errors(PROJECT_ROOT, method, path)


@mcp.tool(description="Срез ручки для роли. Frontend и Flutter после релиза получают его снимок, не черновик файла.")
def get_client_context(method: str, path: str, role: str = "frontend", service: str = "auth") -> str:
    try:
        return agent_context(PROJECT_ROOT, method, path, role=role, service=service)
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        return f"REFUSED: {detail or exc}"


@mcp.tool(description="Манифест проекта: сервисы, владельцы и контракты.")
def get_project_manifest() -> str:
    return format_manifest(PROJECT_ROOT)


@mcp.tool(description="Сравнение размера всего OpenAPI и среза одной ручки. Оценка токенов — символы / 4.")
def context_budget(method: str, path: str, role: str = "frontend") -> str:
    return format_budget(measure_context(PROJECT_ROOT, method, path, role=role))


@mcp.tool(description="Проверка якоря контракта в Solana. Не подтверждено, пока хеш не совпал с проверенной транзакцией.")
def verify_contract_anchor(service: str = "auth") -> str:
    return proof_text(PROJECT_ROOT, service)


@mcp.tool(description="Сводка индекса: файлы, чанки, виды символов.")
def index_stats() -> str:
    rows = _all_rows()
    if not rows:
        return "Индекс пуст."
    paths = {(row["metadata"] or {}).get("path", "") for row in rows}
    kinds = Counter(str((row["metadata"] or {}).get("kind") or "unknown") for row in rows)
    kind_lines = "\n".join(f"  {kind}: {count}" for kind, count in sorted(kinds.items()))
    return (
        f"Файлов: {len(paths)}\n"
        f"Чанков: {len(rows)}\n"
        f"По типам:\n{kind_lines}"
    )


# ---------------------------------------------------------------------------
# REST API models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    role: str = Field(default="all", description="frontend | flutter | backend | devops | all")
    limit: int = Field(default=8, ge=1, le=50)


class SearchHit(BaseModel):
    id: str
    document: str
    path: str
    name: str
    kind: str
    start_line: int | None = None
    end_line: int | None = None
    http_method: str | None = None
    api_path: str | None = None
    rrf_score: float | None = None
    vector_distance: float | None = None


class SearchResponse(BaseModel):
    query: str
    role: str
    count: int
    results: list[SearchHit]


class StatsResponse(BaseModel):
    status: str
    files_indexed: int
    chunks_indexed: int
    kinds: dict[str, int]


# ---------------------------------------------------------------------------
# FastAPI app: REST API first, MCP SSE mounted, static UI mounted last so it
# doesn't shadow the API routes above it.
# ---------------------------------------------------------------------------

app = FastAPI(title="MCP Context Hub")

_cors = [
    item.strip()
    for item in os.environ.get(
        "HUB_CORS_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)

_rate_buckets: dict[str, list[float]] = {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            hits = [item for item in _rate_buckets.get(ip, []) if now - item < 60]
            if len(hits) >= 60:
                return JSONResponse({"detail": "rate_limited"}, status_code=429)
            hits.append(now)
            _rate_buckets[ip] = hits
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)

mount_wallet_api(app, PROJECT_ROOT, collection)

UPLOAD_DIR = PROJECT_ROOT / "uploads"
MAX_UPLOAD_BYTES = 2_000_000


@app.post("/api/uploads")
async def upload_files(files: list[UploadFile] = File(...)) -> dict:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    rejected: list[str] = []
    for item in files:
        name = Path(item.filename or "").name
        suffix = Path(name).suffix.lower()
        if not name or suffix not in ALLOWED_EXTENSIONS:
            rejected.append(name or "unnamed")
            continue
        data = await item.read()
        if len(data) > MAX_UPLOAD_BYTES:
            rejected.append(name)
            continue
        destination = UPLOAD_DIR / name
        destination.write_bytes(data)
        saved.append(index_single_file(destination))
    if not saved and rejected:
        raise HTTPException(status_code=400, detail="unsupported_file")
    return {"saved": saved, "rejected": rejected}


@app.post("/api/index/clear")
def clear_uploaded_index() -> dict:
    clear_index()
    return {"ok": True}


@app.post("/api/search", response_model=SearchResponse)
def api_search(req: SearchRequest) -> SearchResponse:
    """Real hybrid search (vector + BM25) against ChromaDB, scoped by role."""
    query = (req.query or "").strip()
    if not query:
        return SearchResponse(query=query, role=req.role, count=0, results=[])

    role = (req.role or "all").lower()
    try:
        raw_results = hybrid_search(collection, query=query, limit=req.limit, role=role, decrypt=False)
    except VaultLocked as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=423, detail="vault_locked") from exc

    hits: list[SearchHit] = []
    for item in raw_results:
        meta = item.get("metadata") or {}
        if not allows(meta, role):
            continue
        hits.append(
            SearchHit(
                id=item.get("id", ""),
                document=item.get("document") or "",
                path=str(meta.get("path", "")),
                name=str(meta.get("name", "")),
                kind=str(meta.get("kind", "")),
                start_line=meta.get("start_line"),
                end_line=meta.get("end_line"),
                http_method=meta.get("http_method") or None,
                api_path=meta.get("api_path") or None,
                rrf_score=item.get("rrf_score"),
                vector_distance=item.get("vector_distance"),
            )
        )
        if len(hits) >= req.limit:
            break

    return SearchResponse(query=query, role=role, count=len(hits), results=hits)


@app.get("/api/stats", response_model=StatsResponse)
def api_stats() -> StatsResponse:
    """Real counts pulled straight from the Chroma collection, no mock data."""
    try:
        rows = _all_rows()
    except Exception:
        return StatsResponse(status="unavailable", files_indexed=0, chunks_indexed=0, kinds={})

    if not rows:
        return StatsResponse(status="empty", files_indexed=0, chunks_indexed=0, kinds={})

    paths = {(row["metadata"] or {}).get("path", "") for row in rows if (row["metadata"] or {}).get("path")}
    kinds = Counter(str((row["metadata"] or {}).get("kind") or "unknown") for row in rows)

    return StatsResponse(
        status="ok",
        files_indexed=len(paths),
        chunks_indexed=len(rows),
        kinds=dict(sorted(kinds.items(), key=lambda kv: kv[1], reverse=True)),
    )


# ---------------------------------------------------------------------------
# MCP over SSE, wired by hand with the standard mcp.server.sse transport.
#
# FastMCP's own sse_app()/http_app() helpers vary across versions (and some
# versions don't have sse_app() at all), so instead we reach into the
# low-level mcp.server.Server that FastMCP builds internally (mcp._mcp_server)
# and drive it ourselves with SseServerTransport. This is the same pattern
# used directly against the official MCP SDK and is stable across FastMCP
# versions.
#
# IMPORTANT — no nested sub-app here. SseServerTransport is given the full,
# absolute path ("/mcp/messages/") and sends that exact path to the client
# in the SSE "endpoint" event. If the SSE route were reached through an
# app.mount("/mcp", sub_app) wrapper, ASGI would set scope["root_path"] to
# "/mcp" for everything inside that sub-app, and the transport would prepend
# root_path on top of the already-absolute path — producing the doubled
# "/mcp/mcp/messages/" 404 seen before. So the SSE connect route is attached
# directly on the top-level `app` (root_path stays empty) and only the
# messages POST handler is mounted, since it doesn't build any URLs itself
# and is unaffected by root_path.
#
# Endpoints exposed:
#   GET  /mcp/sse            -> the SSE event stream the client connects to
#   POST /mcp/messages        (with or without trailing slash) -> client
#                               posting messages back
# ---------------------------------------------------------------------------

sse = SseServerTransport("/mcp/messages/")


async def handle_sse(request: Request) -> None:
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


# Direct route on the top-level app (NOT nested under app.mount("/mcp", ...))
# so scope["root_path"] stays empty and the endpoint URL sent to clients is
# exactly "/mcp/messages/", not "/mcp/mcp/messages/".
app.add_route("/mcp/sse", handle_sse, methods=["GET"])

# Mounting is safe here: Starlette's Mount matches both the bare path
# ("/mcp/messages") and anything under it ("/mcp/messages/", "/mcp/messages/?session_id=..."),
# and handle_post_message doesn't construct URLs, so root_path doesn't matter.
app.mount("/mcp/messages", app=sse.handle_post_message)

# Static web UI last, so it doesn't intercept /api/* or /mcp/* routes.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
