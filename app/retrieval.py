from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from typing import Any
from urllib.parse import urlencode

from qdrant_client import QdrantClient, models
from openpyxl.utils.cell import range_boundaries

from .budget import BudgetUnavailable
from .config import settings
from .db import connect
from .provider import embed_texts, embed_visual_inputs, rerank
from .security import sign_view


def collection_name(vault_id: str, modality: str = "text") -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", vault_id)
    return f"pvr_{safe}_{modality}_v1"


def qdrant() -> QdrantClient:
    return QdrantClient(url=settings().qdrant_url, timeout=20)


def ensure_collection(vault_id: str, dimensions: int, modality: str = "text") -> str:
    name = collection_name(vault_id, modality)
    client = qdrant()
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.DOT),
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
            optimizers_config=models.OptimizersConfigDiff(indexing_threshold=1000),
        )
        client.create_payload_index(name, "document_id", models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "version_id", models.PayloadSchemaType.KEYWORD)
    return name


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"personal-vault-rag:{chunk_id}"))


def upsert_text_vectors(vault_id: str, chunks: list[dict[str, Any]], vectors: list[list[float]], dimensions: int) -> None:
    name = ensure_collection(vault_id, dimensions)
    points = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        points.append(
            models.PointStruct(
                id=point_id(chunk["id"]),
                vector=vector,
                payload={
                    "vault_id": vault_id,
                    "chunk_id": chunk["id"],
                    "document_id": chunk["document_id"],
                    "version_id": chunk["version_id"],
                    "kind": chunk["kind"],
                },
            )
        )
    qdrant().upsert(name, points=points, wait=True)


def upsert_visual_vectors(vault_id: str, items: list[dict[str, Any]], vectors: list[list[float]], dimensions: int) -> None:
    name = ensure_collection(vault_id, dimensions, "visual")
    points = [
        models.PointStruct(
            id=point_id(item["id"]), vector=vector,
            payload={"vault_id": vault_id, "chunk_id": item["chunk_id"], "document_id": item["document_id"], "version_id": item["version_id"], "preview_path": item["preview_path"]},
        )
        for item, vector in zip(items, vectors, strict=True)
    ]
    qdrant().upsert(name, points=points, wait=True)


def delete_document_vectors(vault_id: str, document_id: str) -> None:
    client = qdrant()
    for modality in ("text", "visual"):
        name = collection_name(vault_id, modality)
        if client.collection_exists(name):
            client.delete(
                name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))])
                ),
                wait=True,
            )


def _fts_expression(query: str) -> str:
    tokens = re.findall(r"[\w.-]{2,}", query, flags=re.UNICODE)[:24]
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def _lexical(vault_id: str, query: str, limit: int, filters: dict[str, Any]) -> list[str]:
    expression = _fts_expression(query)
    if not expression:
        return []
    sql = """SELECT f.chunk_id FROM chunks_fts f JOIN documents d
             ON d.vault_id=f.vault_id AND d.id=f.document_id
             WHERE chunks_fts MATCH ? AND f.vault_id=? AND d.deleted_at IS NULL"""
    params: list[Any] = [expression, vault_id]
    if filters.get("source_id"):
        sql += " AND d.source_id=?"
        params.append(filters["source_id"])
    if filters.get("media_type"):
        sql += " AND d.media_type LIKE ?"
        params.append(filters["media_type"] + "%")
    if filters.get("path_prefix"):
        sql += " AND d.relative_path LIKE ? ESCAPE '\\'"
        escaped = str(filters["path_prefix"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(escaped + "%")
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    params.append(limit)
    with connect() as db:
        return [row["chunk_id"] for row in db.execute(sql, params).fetchall()]


def _dense(vault_id: str, query: str, limit: int, filters: dict[str, Any]) -> tuple[list[str], str | None]:
    with connect() as db:
        vault = db.execute("SELECT embedding_dimensions FROM vaults WHERE id=?", (vault_id,)).fetchone()
    try:
        vector = embed_texts(vault_id, [query], "query", f"query:{uuid.uuid4().hex}")[0]
        name = collection_name(vault_id)
        client = qdrant()
        if not client.collection_exists(name):
            return [], "Dense index has not been built yet"
        must = [models.FieldCondition(key="vault_id", match=models.MatchValue(value=vault_id))]
        if filters.get("document_id"):
            must.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=filters["document_id"])))
        result = client.query_points(name, query=vector, query_filter=models.Filter(must=must), limit=limit, with_payload=True)
        return [str(point.payload["chunk_id"]) for point in result.points], None
    except BudgetUnavailable as exc:
        return [], str(exc)
    except Exception as exc:
        return [], f"Dense retrieval unavailable: {type(exc).__name__}: {exc}"


def _visual(vault_id: str, query: str, limit: int, filters: dict[str, Any]) -> tuple[list[str], str | None]:
    try:
        vector = embed_visual_inputs(vault_id, [[query]], "query", f"visual-query:{uuid.uuid4().hex}")[0]
        name = collection_name(vault_id, "visual")
        client = qdrant()
        if not client.collection_exists(name):
            return [], None
        must = [models.FieldCondition(key="vault_id", match=models.MatchValue(value=vault_id))]
        if filters.get("document_id"):
            must.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=filters["document_id"])))
        result = client.query_points(name, query=vector, query_filter=models.Filter(must=must), limit=limit, with_payload=True)
        return [str(point.payload["chunk_id"]) for point in result.points], None
    except BudgetUnavailable as exc:
        return [], str(exc)
    except Exception as exc:
        return [], f"Visual retrieval unavailable: {type(exc).__name__}: {exc}"


def _load_chunks(vault_id: str, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    with connect() as db:
        rows = db.execute(
            f"""SELECT c.*,d.display_name,d.relative_path,d.source_id,d.availability,d.revision_mtime_ns,
                       v.source_mtime_ns,v.indexed_at
                FROM chunks c JOIN documents d ON d.vault_id=c.vault_id AND d.id=c.document_id
                JOIN versions v ON v.vault_id=c.vault_id AND v.id=c.version_id
                WHERE c.vault_id=? AND c.id IN ({placeholders}) AND d.deleted_at IS NULL""",
            [vault_id, *chunk_ids],
        ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def search(vault_id: str, query: str, limit: int = 8, filters: dict[str, Any] | None = None, include_adjacent: bool = True) -> dict[str, Any]:
    filters = filters or {}
    candidate_limit = min(100, max(20, limit * 5))
    lexical = _lexical(vault_id, query, candidate_limit, filters)
    dense, dense_warning = _dense(vault_id, query, candidate_limit, filters)
    visual, visual_warning = _visual(vault_id, query, candidate_limit, filters)
    scores: defaultdict[str, float] = defaultdict(float)
    channels: defaultdict[str, list[str]] = defaultdict(list)
    for channel, ranking in (("lexical", lexical), ("dense", dense), ("visual", visual)):
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] += 1.0 / (60 + rank)
            channels[chunk_id].append(channel)
    ranked_ids = sorted(scores, key=scores.get, reverse=True)[: min(50, candidate_limit * 2)]
    loaded = _load_chunks(vault_id, ranked_ids)
    ranked_ids = [value for value in ranked_ids if value in loaded]
    rerank_warning = None
    if len(ranked_ids) > 1:
        try:
            reranked = rerank(
                vault_id,
                query,
                [loaded[item]["contextual_text"] or loaded[item]["text"] for item in ranked_ids],
                f"rerank:{uuid.uuid4().hex}",
                min(len(ranked_ids), max(limit * 3, 12)),
            )
            original_ids = ranked_ids
            ranked_ids = [original_ids[index] for index, _score in reranked]
            for index, score in reranked:
                scores[original_ids[index]] += score
        except BudgetUnavailable as exc:
            rerank_warning = str(exc)
        except Exception as exc:
            rerank_warning = f"Reranking unavailable: {type(exc).__name__}: {exc}"
    selected: list[str] = []
    per_document: defaultdict[str, int] = defaultdict(int)
    for chunk_id in ranked_ids:
        doc_id = loaded[chunk_id]["document_id"]
        if per_document[doc_id] >= 3 and len(selected) < limit // 2:
            continue
        selected.append(chunk_id)
        per_document[doc_id] += 1
        if len(selected) >= limit:
            break
    results: list[dict[str, Any]] = []
    expires = int(time.time()) + 3600
    for rank, chunk_id in enumerate(selected, 1):
        row = loaded[chunk_id]
        locator = json.loads(row["locator_json"])
        params = {
            "vault": vault_id, "version": row["version_id"], "expires": expires,
            "signature": sign_view(vault_id, row["document_id"], row["version_id"], expires),
        }
        results.append({
            "rank": rank,
            "source_id": row["source_id"],
            "document_id": row["document_id"],
            "version_id": row["version_id"],
            "chunk_id": chunk_id,
            "title": row["display_name"],
            "relative_path": row["relative_path"],
            "locator": locator,
            "heading_path": row["heading_path"],
            "preview": row["text"][:1200],
            "retrieval_channels": channels[chunk_id],
            "availability": row["availability"],
            "indexed_at": row["indexed_at"],
            "indexed_source_mtime_ns": row["source_mtime_ns"],
            "current_source_mtime_ns": row["revision_mtime_ns"],
            "revision_matches": row["source_mtime_ns"] == row["revision_mtime_ns"],
            "viewer_url": f"{settings().public_base_url}/view/{row['document_id']}?{urlencode(params)}",
        })
    warnings = list(dict.fromkeys(message for message in (dense_warning, visual_warning, rerank_warning) if message))
    if not results:
        warnings.append("No supporting evidence was found. Do not infer a collection-specific answer.")
    return {"query": query, "results": results, "warnings": warnings, "candidate_counts": {"lexical": len(lexical), "dense": len(dense), "visual": len(visual)}}


def fetch_chunk(vault_id: str, chunk_id: str, adjacent: int = 2) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM chunks WHERE vault_id=? AND id=?", (vault_id, chunk_id)
        ).fetchone()
        if not row:
            raise KeyError("Chunk not found in this vault")
        rows = db.execute(
            """SELECT id,ordinal,kind,heading_path,locator_json,text FROM chunks
               WHERE vault_id=? AND document_id=? AND version_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal""",
            (vault_id, row["document_id"], row["version_id"], row["ordinal"] - adjacent, row["ordinal"] + adjacent),
        ).fetchall()
    return {
        "document_id": row["document_id"], "version_id": row["version_id"], "requested_chunk_id": chunk_id,
        "passages": [{**dict(item), "locator": json.loads(item["locator_json"])} for item in rows],
    }


def calculate_table(vault_id: str, document_id: str, sheet_name: str, cell_range: str, operation: str) -> dict[str, Any]:
    """Perform a bounded calculation over persisted workbook cells, never generated prose."""
    if operation not in {"sum", "average", "min", "max", "count"}:
        raise ValueError("operation must be sum, average, min, max, or count")
    min_col, min_row, max_col, max_row = range_boundaries(cell_range.upper())
    if (max_col - min_col + 1) * (max_row - min_row + 1) > 10_000:
        raise ValueError("table calculation is limited to 10,000 cells")
    with connect() as db:
        document = db.execute(
            "SELECT current_version_id,display_name FROM documents WHERE vault_id=? AND id=? AND deleted_at IS NULL",
            (vault_id, document_id),
        ).fetchone()
        if not document or not document["current_version_id"]:
            raise KeyError("Indexed spreadsheet not found in this vault")
        rows = db.execute(
            """SELECT cell_ref,raw_value,numeric_value,formula FROM table_cells
               WHERE vault_id=? AND document_id=? AND version_id=? AND sheet_name=?
               AND row_number BETWEEN ? AND ? AND column_number BETWEEN ? AND ? ORDER BY row_number,column_number""",
            (vault_id, document_id, document["current_version_id"], sheet_name, min_row, max_row, min_col, max_col),
        ).fetchall()
    numbers = [float(row["numeric_value"]) for row in rows if row["numeric_value"] is not None]
    if operation != "count" and not numbers:
        raise ValueError("The selected range has no stored numeric constants; formulas are not recalculated")
    value = {
        "sum": lambda: sum(numbers),
        "average": lambda: sum(numbers) / len(numbers),
        "min": lambda: min(numbers),
        "max": lambda: max(numbers),
        "count": lambda: len(numbers),
    }[operation]()
    return {
        "document_id": document_id,
        "version_id": document["current_version_id"],
        "title": document["display_name"],
        "sheet": sheet_name,
        "range": cell_range.upper(),
        "operation": operation,
        "value": value,
        "numeric_cell_count": len(numbers),
        "formula_cells_excluded": [row["cell_ref"] for row in rows if row["formula"]],
        "citation": f"{document['display_name']} [{document_id}/{document['current_version_id']}] {sheet_name}!{cell_range.upper()}",
    }
