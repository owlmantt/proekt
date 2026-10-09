"""Гибридный поиск: векторный Chroma + BM25, слияние через Reciprocal Rank Fusion."""
from __future__ import annotations

import math
import re
import json

from pathlib import Path
from collections import Counter, defaultdict

from roles import allows, chroma_kind_where
from vault_state import VaultLocked, current_key, open_document, seal_document

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
BM25_INDEX_PATH = Path("./chroma_db/bm25_index.json")

def tokenize(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text)
    result: list[str] = []

    for token in tokens:
        result.append(token.lower())

        # Сохраняем исходное написание для case-sensitive совпадений.
        if token != token.lower():
            result.append(token)

    return result


class BM25Index:
    def __init__(
        self,
        documents: list[str],
        doc_ids: list[str] | None = None,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.doc_ids = (
            list(doc_ids)
            if doc_ids is not None
                else [str(i) for i in range(len(documents))]
            )

        if len(self.doc_ids) != len(documents):
            raise ValueError("Количество doc_ids должно совпадать с количеством documents")

        self.id_to_index = {
            doc_id: i
            for i, doc_id in enumerate(self.doc_ids)
        }
        self.docs = [tokenize(doc) for doc in documents]
        self.doc_len = [len(doc) or 1 for doc in self.docs]
        self.avgdl = (
            sum(self.doc_len) / len(self.doc_len)
            if self.doc_len
            else 1.0
        )
        self.doc_freqs: list[Counter[str]] = [
            Counter(doc) for doc in self.docs
        ]
        self.df: Counter[str] = Counter()
        self.inverted_index: dict[str, set[int]] = defaultdict(set)

        for doc_id, freq in enumerate(self.doc_freqs):
            for token in freq.keys():
                self.df[token] += 1
                self.inverted_index[token].add(doc_id)

        self.n = len(self.docs)

    def _candidate_documents(self, query_tokens: list[str]) -> set[int]:
        candidates: set[int] = set()

        for token in set(query_tokens):
            candidates.update(
                self.inverted_index.get(token, set())
            )

        return candidates

    def remove_document(self, doc_id: str) -> bool:
        """Удаляет документ из BM25-индекса."""
        index = self.id_to_index.get(doc_id)

        if index is None:
            return False

        tokens = self.docs[index]

        for token in set(tokens):
            self.df[token] -= 1

            document_set = self.inverted_index.get(token)
            if document_set is not None:
                document_set.discard(index)

                if not document_set:
                    self.inverted_index.pop(token, None)

            if self.df[token] <= 0:
                self.df.pop(token, None)

        self.docs.pop(index)
        self.doc_len.pop(index)
        self.doc_freqs.pop(index)
        self.doc_ids.pop(index)

        self.n -= 1

        self.id_to_index = {
            doc_id: i
            for i, doc_id in enumerate(self.doc_ids)
        }

        self.avgdl = (
            sum(self.doc_len) / self.n
            if self.n
            else 1.0
        )

        # После удаления индексы документов могли сдвинуться.
        self.inverted_index = defaultdict(set)

        for i, freq in enumerate(self.doc_freqs):
            for token in freq:
                self.inverted_index[token].add(i)

        return True

    def add_document(
        self,
        doc_id: str,
        document: str,
    ) -> None:
        """Добавляет один документ в BM25-индекс."""

        if doc_id in self.id_to_index:
            self.remove_document(doc_id)

        tokens = tokenize(document)
        length = len(tokens) or 1
        frequencies = Counter(tokens)

        index = len(self.docs)

        self.doc_ids.append(doc_id)
        self.docs.append(tokens)
        self.doc_len.append(length)
        self.doc_freqs.append(frequencies)

        self.id_to_index[doc_id] = index

        for token in frequencies:
            self.df[token] += 1
            self.inverted_index[token].add(index)

        self.n += 1

        self.avgdl = (
            sum(self.doc_len) / self.n
            if self.n
            else 1.0
        )

    def scores(self, query: str) -> list[float]:
        q_tokens = tokenize(query)

        if not q_tokens or self.n == 0:
            return [0.0] * self.n

        scores = [0.0] * self.n
        candidates = self._candidate_documents(q_tokens)

        if not candidates:
            return scores

        for token in set(q_tokens):
            df = self.df.get(token, 0)

            if df == 0:
                continue
            idf = math.log(
                1 + (self.n - df + 0.5) / (df + 0.5)
            )
            token_documents = self.inverted_index.get(token, set())
            for doc_id in token_documents:
                if doc_id not in candidates:
                    continue

                tf = self.doc_freqs[doc_id].get(token, 0)

                if tf == 0:
                    continue

                denom = (
                    tf
                    + self.k1
                    * (
                        1
                        - self.b
                        + self.b
                        * self.doc_len[doc_id]
                        / self.avgdl
                    )
                )

                scores[doc_id] += (
                    idf
                    * (tf * (self.k1 + 1))
                    / denom
                )

        return scores

    def to_dict(self) -> dict:
        """Сериализует индекс для сохранения на диск."""
        return {
            "k1": self.k1,
            "b": self.b,
            "docs": self.docs,
            "doc_ids": self.doc_ids,
            "doc_len": self.doc_len,
            "avgdl": self.avgdl,
            "doc_freqs": [
                dict(freq)
                for freq in self.doc_freqs
            ],
            "df": dict(self.df),
            "inverted_index": {
                token: list(doc_ids)
                for token, doc_ids in self.inverted_index.items()
            },
            "n": self.n,
        }


    @classmethod
    def from_dict(cls, data: dict) -> "BM25Index":
        """Восстанавливает BM25Index из сохранённых данных."""
        obj = cls.__new__(cls)

        obj.k1 = float(data["k1"])
        obj.b = float(data["b"])
        obj.doc_ids = list(data["doc_ids"])
        obj.id_to_index = {
            doc_id: i
            for i, doc_id in enumerate(obj.doc_ids)
        }
        obj.docs = [
            list(doc)
            for doc in data["docs"]
        ]

        obj.doc_len = [
            int(length)
            for length in data["doc_len"]
        ]

        obj.avgdl = float(data["avgdl"])

        obj.doc_freqs = [
            Counter(freq)
            for freq in data["doc_freqs"]
        ]

        obj.df = Counter(data["df"])

        obj.inverted_index = defaultdict(set)

        for token, doc_ids in data["inverted_index"].items():
            obj.inverted_index[token] = set(doc_ids)

        obj.n = int(data["n"])

        return obj


_bm25_cache: dict[int, tuple[tuple[str, ...], BM25Index]] = {}


def save_bm25_index(index: BM25Index) -> None:
    BM25_INDEX_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = json.dumps(
        index.to_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if current_key() is not None:
        payload = seal_document(payload)

    BM25_INDEX_PATH.write_text(payload, encoding="utf-8")


def load_bm25_index() -> BM25Index | None:
    if not BM25_INDEX_PATH.exists():
        return None

    try:
        raw = BM25_INDEX_PATH.read_text(encoding="utf-8")
        if raw.startswith("enc1:"):
            raw = open_document(raw)
        data = json.loads(raw)

        return BM25Index.from_dict(data)

    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, VaultLocked):
        return None

class BM25Store:
    """Управляет постоянным BM25-индексом."""

    def __init__(self) -> None:
        self.index = load_bm25_index()

    def get_or_create(
        self,
        documents: list[str],
        doc_ids: list[str],
    ) -> BM25Index:
        if self.index is None:
            self.index = BM25Index(
                documents,
                doc_ids=doc_ids,
            )

            self.save()

        return self.index

    def add(
        self,
        doc_id: str,
        document: str,
    ) -> None:
        if self.index is None:
            self.index = BM25Index(
                [document],
                doc_ids=[doc_id],
            )
        else:
            self.index.add_document(
                doc_id,
                document,
            )

    def remove(self, doc_id: str) -> None:
        if self.index is None:
            return

        self.index.remove_document(doc_id)

    def save(self) -> None:
        if self.index is not None:
            save_bm25_index(self.index)

    def invalidate(self) -> None:
        self.index = None

        if BM25_INDEX_PATH.exists():
            BM25_INDEX_PATH.unlink()

_bm25_store = BM25Store()

def get_bm25_index(
    collection,
    ids: list[str],
    documents: list[str],
) -> BM25Index:
    collection_key = id(collection)
    document_key = tuple(ids)

    cached = _bm25_cache.get(collection_key)

    if cached is not None:
        cached_key, cached_index = cached

        if cached_key == document_key:
            return cached_index

    index = _bm25_store.get_or_create(
        documents,
        ids,
    )

    _bm25_cache[collection_key] = (
        document_key,
        index,
    )

    return index

def add_to_bm25(
    doc_id: str,
    document: str,
) -> None:
    _bm25_store.add(
        doc_id,
        document,
    )


def remove_from_bm25(
    doc_id: str,
) -> None:
    _bm25_store.remove(doc_id)


def save_bm25() -> None:
    _bm25_store.save()

def invalidate_bm25_cache(collection) -> None:
    """Сбрасывает только RAM-кэш BM25."""
    _bm25_cache.pop(id(collection), None)

def update_bm25(
    collection,
    removed_ids: list[str],
    added_documents: list[tuple[str, str]],
) -> None:
    index = _bm25_store.index

    if index is None:
        raw = collection.get(
            include=["documents"],
        )

        ids = raw.get("ids") or []
        documents = raw.get("documents") or []

        if ids:
            index = BM25Index(
                documents,
                doc_ids=ids,
            )
            _bm25_store.index = index
        else:
            return

    for doc_id in removed_ids:
        index.remove_document(doc_id)

    for doc_id, document in added_documents:
        index.add_document(doc_id, document)

    _bm25_store.save()

    _bm25_cache.pop(id(collection), None)
    _bm25_cache[id(collection)] = (
        tuple(index.doc_ids),
        index,
    )

def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)

    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] += 1.0 / (k + rank)

    return sorted(
        scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )


def hybrid_search(
    collection,
    query: str,
    limit: int = 3,
    candidate_multiplier: int = 8,
    role: str = "",
    decrypt: bool = True,
) -> list[dict]:
    raw = collection.get(
        include=["documents", "metadatas"]
    )

    ids: list[str] = raw.get("ids") or []
    documents: list[str] = raw.get("documents") or []
    metadatas: list[dict] = raw.get("metadatas") or []

    sealed_documents = list(documents)
    documents = [open_document(doc) for doc in sealed_documents]

    normalized_role = (role or "").strip().lower()
    if normalized_role in ("", "all"):
        allowed_ids = set(ids)
    else:
        allowed_ids = {
            doc_id
            for doc_id, meta in zip(ids, metadatas)
            if allows(meta or {}, normalized_role)
        }
    if not allowed_ids:
        return []

    shown = documents if decrypt else sealed_documents
    by_id = {
        doc_id: {
            "id": doc_id,
            "document": shown[i],
            "metadata": metadatas[i] or {},
        }
        for i, doc_id in enumerate(ids)
        if doc_id in allowed_ids
    }

    fetch_n = min(
        len(allowed_ids),
        max(limit * candidate_multiplier, limit),
    )

    query_kwargs: dict = {
        "query_texts": [query],
        "n_results": fetch_n,
    }
    kind_where = chroma_kind_where(normalized_role)
    if kind_where:
        query_kwargs["where"] = kind_where

    vector = collection.query(**query_kwargs)

    vector_ids: list[str] = []
    vector_distances: dict[str, float] = {}

    if vector.get("ids") and vector["ids"][0]:
        vector_ids = [doc_id for doc_id in vector["ids"][0] if doc_id in allowed_ids]

        distances = (vector.get("distances") or [[]])[0]

        for i, doc_id in enumerate(vector["ids"][0]):
            if i < len(distances) and doc_id in allowed_ids:
                vector_distances[doc_id] = distances[i]

    bm25 = get_bm25_index(
        collection,
        ids,
        documents,
    )

    bm25_scores = bm25.scores(query)
    scored_ids = list(getattr(bm25, "doc_ids", ids))
    bm25_ranked = [
        doc_id
        for doc_id, _score in sorted(
            zip(scored_ids, bm25_scores),
            key=lambda item: item[1],
            reverse=True,
        )
        if _score > 0 and doc_id in allowed_ids
    ]

    fused = reciprocal_rank_fusion(
        [vector_ids, bm25_ranked]
    )

    results: list[dict] = []

    for doc_id, rrf_score in fused[:limit]:
        source = by_id.get(doc_id)
        if source is None:
            continue
        item = dict(source)

        item["rrf_score"] = rrf_score
        item["vector_distance"] = vector_distances.get(doc_id)

        results.append(item)

    return results