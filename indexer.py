"""Индексация кодовой базы: AST-чанки, MD5-дельта, .gitignore, коллекция ChromaDB."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import chromadb
 
from chunking import chunk_file
from ingestion import process_file
from ignore import is_ignored, load_ignore_spec
from hybrid_search import invalidate_bm25_cache, get_bm25_index, save_bm25_index, update_bm25
from vault_state import seal_document

PROJECT_ROOT = Path(".").resolve()
CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "codebase"
HASHES_PATH = Path("./chroma_db/file_hashes.json")
ALLOWED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".md", ".txt", ".html", ".htm", ".pdf", ".docx", ".doc", ".rtf"}


def collect_files(root: Path | None = None) -> list[Path]:
    root = (root or PROJECT_ROOT).resolve()
    spec = load_ignore_spec(root)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames if not is_ignored(root, current / name, spec)
        ]
        for name in filenames:
            path = current / name
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            if is_ignored(root, path, spec):
                continue
            files.append(path)
    return sorted(files)


def is_indexable_file(path: Path, root: Path | None = None) -> bool:
    root = (root or PROJECT_ROOT).resolve()
    path = path.resolve()
    if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return False
    return not is_ignored(root, path)


def md5_text(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def relative_posix(path: Path, root: Path | None = None) -> str:
    root = (root or PROJECT_ROOT).resolve()
    return path.resolve().relative_to(root).as_posix()


def load_hashes() -> dict[str, str]:
    if not HASHES_PATH.exists():
        return {}
    try:
        data = json.loads(HASHES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_hashes(hashes: dict[str, str]) -> None:
    HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    HASHES_PATH.write_text(
        json.dumps(hashes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=CHROMA_PATH)


def get_collection(client: chromadb.PersistentClient | None = None, hashes: dict[str, str] | None = None):
    """Открывает коллекцию. Пустой file_hashes.json её не удаляет."""
    del hashes
    client = client or get_client()
    return client.get_or_create_collection(name=COLLECTION_NAME)


def delete_file_chunks(collection, relative_path: str) -> list[str]:
    existing = collection.get(
        where={"path": relative_path},
        include=["metadatas"],
    )

    ids = existing.get("ids") or []

    if ids:
        collection.delete(ids=ids)
        return ids

    return []


def upsert_chunks(
    collection,
    relative_path: str,
    content: str,
    extra_metadata: dict | None = None,
    on_progress=None,
) -> list[tuple[str, str]]:
    chunks = chunk_file(Path(relative_path), content)

    if not chunks:
        return []

    metadatas = []

    for chunk in chunks:
        meta = chunk.metadata()

        if extra_metadata:
            meta.update(extra_metadata)

        metadatas.append(meta)

    chunk_ids = [chunk.chunk_id for chunk in chunks]
    # Длинные схемы не нужны целиком для поиска, а пачка из сотни
    # полных документов роняет бесплатный сервер по памяти.
    chunk_documents = [chunk.text[:4000] for chunk in chunks]
    stored_documents = [seal_document(text) for text in chunk_documents]
    added: list[tuple[str, str]] = []
    batch_size = 8

    for start in range(0, len(chunks), batch_size):
        end = min(start + batch_size, len(chunks))
        collection.upsert(
            ids=chunk_ids[start:end],
            documents=stored_documents[start:end],
            metadatas=metadatas[start:end],
        )
        added.extend(zip(chunk_ids[start:end], chunk_documents[start:end]))
        if on_progress is not None:
            on_progress(end, len(chunks))
        time.sleep(0.05)

    return added


def index_single_file(path: Path, collection=None, hashes: dict[str, str] | None = None, on_progress=None) -> str:
    """Переиндексирует один файл. Возвращает краткий статус."""
    path = path.resolve()
    own_hashes = hashes is None
    hashes = load_hashes() if own_hashes else hashes
    collection = collection or get_collection(hashes=hashes)

    if not is_indexable_file(path):
        try:
            relative_path = relative_posix(path)
        except ValueError:
            return f"Skipped, outside the project: {path}"
        if relative_path in hashes:
            removed_ids = delete_file_chunks(collection, relative_path)
            update_bm25(collection, removed_ids, [])
            hashes.pop(relative_path, None)
            if own_hashes:
                save_hashes(hashes)
            return f"Removed from the index: {relative_path}"
        return f"Skipped, this file is not indexed: {path}"

    relative_path = relative_posix(path)
    parsed = process_file(path, relative_path)
    content = str(parsed.get("content") or "")
    digest = md5_text(content)
    if hashes.get(relative_path) == digest:
        return f"Already indexed: {relative_path}"

    removed_ids = delete_file_chunks(collection, relative_path)

    added_documents = upsert_chunks(
        collection,
        relative_path,
        content,
        {
            "role": parsed.get("role", "general"),
            "source_kind": parsed.get("kind", "file_chunk"),
        },
        on_progress=on_progress,
    )

    update_bm25(
        collection,
        removed_ids,
        added_documents,
    )

    chunk_count = len(added_documents)
    
    hashes[relative_path] = digest
    if own_hashes:
        save_hashes(hashes)
    return f"Updated: {relative_path} ({chunk_count} pieces)"


def remove_indexed_file(
    path: Path,
    collection=None,
    hashes: dict[str, str] | None = None,
) -> str:
    own_hashes = hashes is None
    hashes = load_hashes() if own_hashes else hashes
    collection = collection or get_collection(hashes=hashes)

    try:
        relative_path = relative_posix(path)
    except ValueError:
        return f"Пропущен (вне проекта): {path}"

    if relative_path not in hashes:
        return f"Не было в индексе: {relative_path}"

    removed_ids = delete_file_chunks(
        collection,
        relative_path,
    )

    update_bm25(
        collection,
        removed_ids,
        [],
    )

    hashes.pop(relative_path, None)

    if own_hashes:
        save_hashes(hashes)

    return f"Удалён из индекса: {relative_path}"


def index_codebase() -> None:
    client = get_client()
    hashes = load_hashes()
    collection = get_collection(client, hashes)

    files = collect_files(PROJECT_ROOT)
    current_paths = {relative_posix(path) for path in files}

    removed = [path for path in hashes if path not in current_paths]
    for relative_path in removed:
        
        removed_ids = delete_file_chunks(
            collection,
            relative_path,
        )

        update_bm25(
            collection,
            removed_ids,
            [],
        )

        print(f"Удалён из индекса: {relative_path}")
        hashes.pop(relative_path, None)

    indexed_files = 0
    indexed_chunks = 0
    skipped = 0

    for file_path in files:
        relative_path = relative_posix(file_path)
        parsed = process_file(file_path, relative_path)
        content = str(parsed.get("content") or "")
        digest = md5_text(content)

        if hashes.get(relative_path) == digest:
            print(f"Без изменений: {relative_path}")
            skipped += 1
            continue

        removed_ids = delete_file_chunks(
            collection,
            relative_path,
        )
        print(f"Индексируется: {relative_path}", flush = True)
        added_documents = upsert_chunks(
            collection,
            relative_path,
            content,
            {
                "role": parsed.get("role", "general"),
                "source_kind": parsed.get("kind", "file_chunk"),
            },
        )

        update_bm25(
            collection,
            removed_ids,
            added_documents,
        )

        chunk_count = len(added_documents)
        hashes[relative_path] = digest
        indexed_files += 1
        indexed_chunks += chunk_count
        print(f"Обновлён: {relative_path} ({chunk_count} чанков)")

    save_hashes(hashes)
    print(
        f"\nГотово. Файлов обновлено: {indexed_files}, "
        f"чанков записано: {indexed_chunks}, пропущено: {skipped}, удалено: {len(removed)}"
    )


def clear_index() -> None:
    """Удаляет индекс и загруженные файлы. Каталог проекта не трогает."""
    client = get_client()
    names = [item.name for item in client.list_collections()]
    if COLLECTION_NAME in names:
        client.delete_collection(COLLECTION_NAME)
    if HASHES_PATH.exists():
        HASHES_PATH.unlink()
    bm25_path = Path(CHROMA_PATH) / "bm25_index.json"
    if bm25_path.exists():
        bm25_path.unlink()
    upload_dir = PROJECT_ROOT / "uploads"
    if upload_dir.is_dir():
        for child in upload_dir.iterdir():
            if child.is_file():
                child.unlink()


if __name__ == "__main__":
    index_codebase()