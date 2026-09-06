from __future__ import annotations

import json
import os
import shutil
import socket
import time
import traceback
import uuid
from pathlib import Path

from PIL import Image
from openpyxl import load_workbook

from .catalog import cleanup_expired, create_job, enqueue_missing_embedding_jobs
from .config import settings
from .db import connect, init_db, now, transaction
from .extraction import extract_document
from .provider import embed_texts, embed_visual_inputs, provider_settings
from .retrieval import delete_document_vectors, delete_version_vectors, mark_document_vectors_historical, upsert_text_vectors, upsert_visual_vectors


WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def claim_job():
    stamp = now()
    with connect() as db, transaction(db, immediate=True):
        db.execute(
            """UPDATE jobs SET state='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
               WHERE state='running' AND lease_expires_at<?""", (stamp, stamp)
        )
        row = db.execute(
            """SELECT j.* FROM jobs j JOIN vaults v ON v.id=j.vault_id
               WHERE j.state='queued' AND j.not_before<=? AND v.paused=0
               ORDER BY COALESCE(v.last_job_claim_at,0),j.priority,j.created_at LIMIT 1""", (stamp,)
        ).fetchone()
        if not row:
            return None
        db.execute(
            """UPDATE jobs SET state='running',attempts=attempts+1,lease_owner=?,lease_expires_at=?,updated_at=?
               WHERE id=? AND state='queued'""", (WORKER_ID, stamp + 1800, stamp, row["id"])
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            return None
        db.execute("UPDATE vaults SET last_job_claim_at=? WHERE id=?", (stamp, row["vault_id"]))
        return dict(row)


def finish_job(job_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE jobs SET state='complete',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?", (now(), job_id))


def fail_job(job: dict, exc: Exception) -> None:
    retry = job["attempts"] + 1 < job["max_attempts"]
    state = "queued" if retry else "failed"
    delay = min(300, 5 * 2 ** job["attempts"])
    message = f"{type(exc).__name__}: {exc}"[:2000]
    with connect() as db:
        db.execute(
            """UPDATE jobs SET state=?,not_before=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?,error=? WHERE id=?""",
            (state, now() + delay, now(), message, job["id"]),
        )
        if not retry and job["kind"] == "inspect_temporary":
            inspection_id = json.loads(job["payload_json"])["inspection_id"]
            row = db.execute("SELECT staging_path FROM temporary_inspections WHERE vault_id=? AND id=?", (job["vault_id"], inspection_id)).fetchone()
            if row and row["staging_path"]:
                Path(row["staging_path"]).unlink(missing_ok=True)
            db.execute(
                "UPDATE temporary_inspections SET state='failed',error=?,staging_path=NULL,completed_at=? WHERE vault_id=? AND id=?",
                (message, now(), job["vault_id"], inspection_id),
            )


def _replace_chunks(vault_id: str, document_id: str, version_id: str, extracted) -> list[dict]:
    stamp = now()
    records = []
    for ordinal, chunk in enumerate(extracted.chunks):
        chunk_id = "chk_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{version_id}:{ordinal}:{chunk.text[:120]}").hex
        records.append({
            "id": chunk_id, "vault_id": vault_id, "document_id": document_id,
            "version_id": version_id, "ordinal": ordinal, "kind": chunk.kind,
            "heading_path": chunk.heading_path, "locator_json": json.dumps(chunk.locator, ensure_ascii=False),
            "text": chunk.text, "contextual_text": None, "token_estimate": max(1, len(chunk.text) // 4),
        })
    with connect() as db, transaction(db, immediate=True):
        db.execute("DELETE FROM chunks_fts WHERE vault_id=? AND version_id=?", (vault_id, version_id))
        db.execute("DELETE FROM chunks WHERE vault_id=? AND version_id=?", (vault_id, version_id))
        db.executemany(
            """INSERT INTO chunks(id,vault_id,document_id,version_id,ordinal,kind,heading_path,locator_json,text,contextual_text,token_estimate,created_at)
               VALUES(:id,:vault_id,:document_id,:version_id,:ordinal,:kind,:heading_path,:locator_json,:text,:contextual_text,:token_estimate,:created_at)""",
            [{**record, "created_at": stamp} for record in records],
        )
        db.executemany(
            """INSERT INTO chunks_fts(text,contextual_text,heading_path,vault_id,chunk_id,document_id,version_id)
               VALUES(:text,'',:heading_path,:vault_id,:id,:document_id,:version_id)""", records,
        )
    return records


def _replace_table_cells(vault_id: str, document_id: str, version_id: str, raw_path: Path, display_name: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM table_cells WHERE vault_id=? AND version_id=?", (vault_id, version_id))
        if Path(display_name).suffix.casefold() != ".xlsx":
            return
        workbook = load_workbook(raw_path, read_only=True, data_only=False)
        rows = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    formula = str(cell.value) if cell.data_type == "f" else None
                    numeric = float(cell.value) if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool) else None
                    rows.append((vault_id, document_id, version_id, sheet.title, cell.coordinate, cell.row, cell.column, str(cell.value), numeric, formula))
        db.executemany(
            """INSERT INTO table_cells(vault_id,document_id,version_id,sheet_name,cell_ref,row_number,column_number,raw_value,numeric_value,formula)
               VALUES(?,?,?,?,?,?,?,?,?,?)""", rows,
        )


def _locator_matches(preview_locator: dict, chunk_locator: dict) -> bool:
    if "page" in preview_locator and "page" in chunk_locator:
        return chunk_locator["page"] <= preview_locator["page"] <= chunk_locator.get("page_end", chunk_locator["page"])
    if "slide" in preview_locator and "slide" in chunk_locator:
        return preview_locator["slide"] == chunk_locator["slide"]
    if "image" in preview_locator and "image" in chunk_locator:
        return preview_locator["image"] == chunk_locator["image"]
    if "time_start" in preview_locator and "time_start" in chunk_locator:
        return preview_locator.get("time_end", preview_locator["time_start"]) >= chunk_locator["time_start"] and preview_locator["time_start"] <= chunk_locator.get("time_end", chunk_locator["time_start"])
    return False


def _replace_visual_evidence(vault_id: str, document_id: str, version_id: str, previews, text_records: list[dict]) -> list[dict]:
    stamp = now()
    visual_records = []
    preview_records = []
    for index, preview in enumerate(previews):
        nearby = next(
            (record for record in text_records if _locator_matches(preview.locator, json.loads(record["locator_json"]))),
            None,
        )
        chunk_id = "chk_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{version_id}:visual:{index}").hex
        preview_id = "preview_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{version_id}:preview:{index}").hex
        visual_records.append(
            {
                "id": chunk_id,
                "vault_id": vault_id,
                "document_id": document_id,
                "version_id": version_id,
                "ordinal": len(text_records) + index,
                "kind": "visual",
                "heading_path": nearby["heading_path"] if nearby else "Visual evidence",
                "locator_json": json.dumps(preview.locator, ensure_ascii=False),
                "text": "Visual evidence is available; inspect the identified preview before making a visual claim.",
                "contextual_text": nearby["text"][:1200] if nearby else None,
                "token_estimate": 1,
                "created_at": stamp,
            }
        )
        preview_records.append(
            (
                preview_id,
                vault_id,
                document_id,
                version_id,
                index,
                str(preview.path),
                "image/" + ("webp" if preview.path.suffix.casefold() == ".webp" else "jpeg"),
                json.dumps(preview.locator, ensure_ascii=False),
                chunk_id,
                stamp,
            )
        )
    with connect() as db, transaction(db, immediate=True):
        db.execute("DELETE FROM previews WHERE vault_id=? AND version_id=?", (vault_id, version_id))
        db.execute("DELETE FROM chunks WHERE vault_id=? AND version_id=? AND kind='visual'", (vault_id, version_id))
        db.executemany(
            """INSERT INTO chunks(id,vault_id,document_id,version_id,ordinal,kind,heading_path,locator_json,text,contextual_text,token_estimate,created_at)
               VALUES(:id,:vault_id,:document_id,:version_id,:ordinal,:kind,:heading_path,:locator_json,:text,:contextual_text,:token_estimate,:created_at)""",
            visual_records,
        )
        db.executemany(
            """INSERT INTO previews(id,vault_id,document_id,version_id,ordinal,path,media_type,locator_json,text_chunk_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            preview_records,
        )
    return visual_records


def _preview_bytes(previews) -> int:
    return sum(preview.path.stat().st_size for preview in previews if preview.path.exists())


def _media_filters(media_type: str | None) -> list[str]:
    value = (media_type or "application/octet-stream").casefold()
    return list(dict.fromkeys((value, value.split("/", 1)[0])))


def _path_prefixes(relative_path: str) -> list[str]:
    parts = [part for part in relative_path.replace("\\", "/").strip("/").casefold().split("/") if part]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def process_extract(job: dict) -> None:
    payload = json.loads(job["payload_json"])
    with connect() as db:
        row = db.execute(
            """SELECT v.*,d.display_name,d.size_bytes,d.source_id,d.media_type,d.relative_path FROM versions v JOIN documents d
               ON d.vault_id=v.vault_id AND d.id=v.document_id WHERE v.vault_id=? AND v.id=? AND d.id=?""",
            (job["vault_id"], payload["version_id"], payload["document_id"]),
        ).fetchone()
    if not row or not row["raw_staging_path"]:
        raise RuntimeError("Staged source is no longer available; reconnect the source to retry")
    raw_path = Path(row["raw_staging_path"])
    preview_dir = settings().preview_dir / job["vault_id"] / payload["version_id"]
    shutil.rmtree(preview_dir, ignore_errors=True)
    extracted = extract_document(raw_path, row["display_name"], preview_dir)
    records = _replace_chunks(job["vault_id"], payload["document_id"], payload["version_id"], extracted)
    _replace_table_cells(job["vault_id"], payload["document_id"], payload["version_id"], raw_path, row["display_name"])
    warnings = list(extracted.warnings)
    _replace_visual_evidence(job["vault_id"], payload["document_id"], payload["version_id"], extracted.previews, records)
    raw_path.unlink(missing_ok=True)
    text_bytes = sum(len(item["text"].encode()) for item in records)
    preview_bytes = _preview_bytes(extracted.previews)
    with connect() as db, transaction(db, immediate=True):
        db.execute(
            """UPDATE versions SET indexed_at=?,lexical_indexed_at=?,embedding_model=NULL,visual_model=NULL,
               text_indexed_at=NULL,visual_indexed_at=NULL,visual_recovery_needed=0,raw_staging_path=NULL,
               text_bytes=?,preview_bytes=? WHERE vault_id=? AND id=?""",
            (now(), now(), text_bytes, preview_bytes, job["vault_id"], payload["version_id"]),
        )
        db.execute(
            """UPDATE documents SET current_version_id=?,state='indexed',error=? WHERE vault_id=? AND id=?""",
            (payload["version_id"], "\n".join(warnings) or None, job["vault_id"], payload["document_id"]),
        )
        obsolete = db.execute(
            """SELECT id,raw_staging_path FROM versions WHERE vault_id=? AND document_id=?
               AND id<>? AND indexed_at IS NULL""",
            (job["vault_id"], payload["document_id"], payload["version_id"]),
        ).fetchall()
        for old in obsolete:
            if old["raw_staging_path"]:
                Path(old["raw_staging_path"]).unlink(missing_ok=True)
            db.execute("DELETE FROM versions WHERE vault_id=? AND id=?", (job["vault_id"], old["id"]))
        db.execute(
            """DELETE FROM jobs WHERE vault_id=? AND kind='extract' AND state='failed'
               AND json_extract(payload_json,'$.document_id')=?""",
            (job["vault_id"], payload["document_id"]),
        )
        create_job(db, job["vault_id"], "mark_historical", {"document_id": payload["document_id"], "current_version_id": payload["version_id"]}, priority=30)
    enqueue_missing_embedding_jobs(job["vault_id"], payload["version_id"])


def process_render_visual(job: dict) -> None:
    payload = json.loads(job["payload_json"])
    with connect() as db:
        row = db.execute(
            """SELECT v.raw_staging_path,d.display_name FROM versions v JOIN documents d
               ON d.vault_id=v.vault_id AND d.id=v.document_id
               WHERE v.vault_id=? AND v.id=? AND d.id=? AND d.current_version_id=v.id AND d.deleted_at IS NULL""",
            (job["vault_id"], payload["version_id"], payload["document_id"]),
        ).fetchone()
        text_records = [dict(item) for item in db.execute(
            "SELECT * FROM chunks WHERE vault_id=? AND version_id=? AND kind<>'visual' ORDER BY ordinal",
            (job["vault_id"], payload["version_id"]),
        )]
    if not row or not row["raw_staging_path"]:
        raise RuntimeError("Source content is required to regenerate expired visual previews")
    raw_path = Path(row["raw_staging_path"])
    preview_dir = settings().preview_dir / job["vault_id"] / payload["version_id"]
    shutil.rmtree(preview_dir, ignore_errors=True)
    extracted = extract_document(raw_path, row["display_name"], preview_dir)
    delete_version_vectors(job["vault_id"], payload["version_id"], "visual")
    with connect() as db:
        db.execute(
            "DELETE FROM embedding_batches WHERE vault_id=? AND version_id=? AND modality='visual'",
            (job["vault_id"], payload["version_id"]),
        )
    _replace_visual_evidence(job["vault_id"], payload["document_id"], payload["version_id"], extracted.previews, text_records)
    raw_path.unlink(missing_ok=True)
    with connect() as db:
        db.execute(
            """UPDATE versions SET raw_staging_path=NULL,preview_bytes=?,visual_indexed_at=NULL,visual_model=NULL,
               visual_recovery_needed=0 WHERE vault_id=? AND id=?""",
            (_preview_bytes(extracted.previews), job["vault_id"], payload["version_id"]),
        )
    enqueue_missing_embedding_jobs(job["vault_id"], payload["version_id"])


def process_embed_text(job: dict) -> None:
    payload = json.loads(job["payload_json"])
    provider = provider_settings(job["vault_id"])
    if not provider or payload["model"] != provider["embedding_model"]:
        return
    with connect() as db:
        version = db.execute(
            """SELECT v.id,d.id document_id,d.source_id,d.media_type,d.relative_path FROM versions v JOIN documents d
               ON d.vault_id=v.vault_id AND d.current_version_id=v.id
               WHERE v.vault_id=? AND v.id=? AND d.deleted_at IS NULL""",
            (job["vault_id"], payload["version_id"]),
        ).fetchone()
        records = [dict(item) for item in db.execute(
            "SELECT * FROM chunks WHERE vault_id=? AND version_id=? AND kind<>'visual' AND text<>'' ORDER BY ordinal",
            (job["vault_id"], payload["version_id"]),
        )]
    if not version or not records:
        return
    for record in records:
        record["source_id"] = version["source_id"]
        record["media_filters"] = _media_filters(version["media_type"])
        record["path_prefixes"] = _path_prefixes(version["relative_path"])
    for start in range(0, len(records), 64):
        with connect() as db:
            done = db.execute(
                "SELECT 1 FROM embedding_batches WHERE vault_id=? AND version_id=? AND modality='text' AND batch_start=? AND model=?",
                (job["vault_id"], payload["version_id"], start, payload["model"]),
            ).fetchone()
        if done:
            continue
        batch = records[start:start + 64]
        vectors = embed_texts(job["vault_id"], [item["text"] for item in batch], "document", f"embed:{payload['version_id']}:{start}:{payload['model']}")
        upsert_text_vectors(job["vault_id"], batch, vectors, provider["embedding_dimensions"])
        with connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO embedding_batches(vault_id,version_id,modality,batch_start,model,item_count,completed_at) VALUES(?,?,'text',?,?,?,?)",
                (job["vault_id"], payload["version_id"], start, payload["model"], len(batch), now()),
            )
    with connect() as db:
        db.execute(
            "UPDATE versions SET text_indexed_at=?,embedding_model=? WHERE vault_id=? AND id=?",
            (now(), payload["model"], job["vault_id"], payload["version_id"]),
        )


def process_embed_visual(job: dict) -> None:
    payload = json.loads(job["payload_json"])
    provider = provider_settings(job["vault_id"])
    if not provider or payload["model"] != provider["visual_model"]:
        return
    with connect() as db:
        version = db.execute(
            """SELECT v.id,d.id document_id,d.source_id,d.media_type,d.relative_path FROM versions v JOIN documents d
               ON d.vault_id=v.vault_id AND d.current_version_id=v.id
               WHERE v.vault_id=? AND v.id=? AND d.deleted_at IS NULL""",
            (job["vault_id"], payload["version_id"]),
        ).fetchone()
        rows = [dict(item) for item in db.execute(
            """SELECT p.*,c.contextual_text,c.text FROM previews p JOIN chunks c
               ON c.vault_id=p.vault_id AND c.id=p.text_chunk_id
               WHERE p.vault_id=? AND p.version_id=? ORDER BY p.ordinal""",
            (job["vault_id"], payload["version_id"]),
        )]
    if not version or not rows:
        return
    if any(not Path(row["path"]).is_file() for row in rows):
        with connect() as db:
            db.execute(
                "DELETE FROM embedding_batches WHERE vault_id=? AND version_id=? AND modality='visual'",
                (job["vault_id"], payload["version_id"]),
            )
            db.execute(
                "UPDATE versions SET visual_indexed_at=NULL,visual_model=NULL,visual_recovery_needed=1 WHERE vault_id=? AND id=?",
                (job["vault_id"], payload["version_id"]),
            )
        delete_version_vectors(job["vault_id"], payload["version_id"], "visual")
        return
    for start in range(0, len(rows), 8):
        with connect() as db:
            done = db.execute(
                "SELECT 1 FROM embedding_batches WHERE vault_id=? AND version_id=? AND modality='visual' AND batch_start=? AND model=?",
                (job["vault_id"], payload["version_id"], start, payload["model"]),
            ).fetchone()
        if done:
            continue
        batch_rows = rows[start:start + 8]
        inputs = []
        items = []
        for row in batch_rows:
            with Image.open(row["path"]) as image:
                parts: list[object] = []
                nearby = row["contextual_text"]
                if nearby:
                    parts.append(nearby[:1200])
                parts.append(image.convert("RGB").copy())
            inputs.append(parts)
            items.append(
                {
                    "id": f"visual:{payload['version_id']}:{row['ordinal']}",
                    "chunk_id": row["text_chunk_id"],
                    "document_id": version["document_id"],
                    "version_id": payload["version_id"],
                    "preview_path": row["path"],
                    "preview_id": row["id"],
                    "preview_ordinal": row["ordinal"],
                    "locator_json": row["locator_json"],
                    "source_id": version["source_id"],
                    "media_filters": _media_filters(version["media_type"]),
                    "path_prefixes": _path_prefixes(version["relative_path"]),
                }
            )
        vectors = embed_visual_inputs(job["vault_id"], inputs, "document", f"visual:{payload['version_id']}:{start}:{payload['model']}")
        upsert_visual_vectors(job["vault_id"], items, vectors, provider["embedding_dimensions"])
        with connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO embedding_batches(vault_id,version_id,modality,batch_start,model,item_count,completed_at) VALUES(?,?,'visual',?,?,?,?)",
                (job["vault_id"], payload["version_id"], start, payload["model"], len(items), now()),
            )
    with connect() as db:
        db.execute(
            "UPDATE versions SET visual_indexed_at=?,visual_model=?,visual_recovery_needed=0 WHERE vault_id=? AND id=?",
            (now(), payload["model"], job["vault_id"], payload["version_id"]),
        )


def process_temporary_inspection(job: dict) -> None:
    inspection_id = json.loads(job["payload_json"])["inspection_id"]
    with connect() as db:
        row = db.execute(
            "SELECT * FROM temporary_inspections WHERE vault_id=? AND id=? AND state='queued'",
            (job["vault_id"], inspection_id),
        ).fetchone()
    if not row or not row["staging_path"]:
        raise RuntimeError("Temporary inspection expired or is unavailable")
    raw_path = Path(row["staging_path"])
    preview_dir = settings().preview_dir / "temporary" / inspection_id
    try:
        extracted = extract_document(raw_path, row["file_name"], preview_dir)
        full_text = "\n\n".join(chunk.text for chunk in extracted.chunks)
        truncated = len(full_text) > 16000
        warnings = [*extracted.warnings]
        if truncated:
            warnings.append("Temporary extracted context was truncated to 16,000 characters")
        with connect() as db:
            db.execute(
                """UPDATE temporary_inspections SET state='complete',extracted_text=?,warnings_json=?,staging_path=NULL,completed_at=?
                   WHERE vault_id=? AND id=?""",
                (full_text[:16000], json.dumps(warnings), now(), job["vault_id"], inspection_id),
            )
    finally:
        raw_path.unlink(missing_ok=True)
        for item in preview_dir.glob("*") if preview_dir.exists() else []:
            item.unlink(missing_ok=True)
        preview_dir.rmdir() if preview_dir.exists() else None


def process_job(job: dict) -> None:
    if job["kind"] == "extract":
        process_extract(job)
    elif job["kind"] == "render_visual":
        process_render_visual(job)
    elif job["kind"] == "embed_text":
        process_embed_text(job)
    elif job["kind"] == "embed_visual":
        process_embed_visual(job)
    elif job["kind"] == "mark_historical":
        payload = json.loads(job["payload_json"])
        mark_document_vectors_historical(job["vault_id"], payload["document_id"], payload["current_version_id"])
    elif job["kind"] == "inspect_temporary":
        process_temporary_inspection(job)
    elif job["kind"] == "delete_vectors":
        payload = json.loads(job["payload_json"])
        delete_document_vectors(job["vault_id"], payload["document_id"])
    elif job["kind"] == "delete_version_vectors":
        payload = json.loads(job["payload_json"])
        delete_version_vectors(job["vault_id"], payload["version_id"], payload.get("modality"))
    elif job["kind"] == "cleanup":
        cleanup_expired()
    else:
        raise ValueError(f"Unknown job kind {job['kind']}")


def main() -> None:
    init_db()
    with connect() as db:
        vault_ids = [row["id"] for row in db.execute("SELECT id FROM vaults WHERE provider_ciphertext IS NOT NULL AND monthly_budget_microusd>0")]
    for vault_id in vault_ids:
        enqueue_missing_embedding_jobs(vault_id)
    last_cleanup = 0.0
    while True:
        job = claim_job()
        if job:
            try:
                process_job(job)
                finish_job(job["id"])
            except Exception as exc:
                traceback.print_exc()
                fail_job(job, exc)
            continue
        if now() - last_cleanup > 3600:
            cleanup_expired()
            last_cleanup = now()
        time.sleep(settings().worker_poll_seconds)


if __name__ == "__main__":
    main()
