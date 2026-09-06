from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .db import connect, now, transaction

EXTRACTION_VERSION = "pvr-extract-1"
SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".markdown",
    ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff",
    ".mp3", ".m4a", ".wav", ".flac", ".mp4", ".mov", ".mkv", ".webm",
}


def stable_document_id(source_id: str, relative_path: str) -> str:
    value = f"{source_id}\0{relative_path.replace(os.sep, '/').casefold()}"
    return "doc_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def stable_version_id(document_id: str, content_sha256: str, mtime_ns: int) -> str:
    return "ver_" + hashlib.sha256(f"{document_id}\0{content_sha256}\0{mtime_ns}".encode()).hexdigest()[:32]


def create_job(db, vault_id: str, kind: str, payload: dict[str, Any], priority: int = 100) -> str:
    job_id = "job_" + uuid.uuid4().hex
    stamp = now()
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    existing = db.execute(
        "SELECT id FROM jobs WHERE vault_id=? AND kind=? AND payload_json=? AND state IN ('queued','running')",
        (vault_id, kind, payload_json),
    ).fetchone()
    if existing:
        return existing["id"]
    failed = db.execute(
        "SELECT id FROM jobs WHERE vault_id=? AND kind=? AND payload_json=? AND state='failed' ORDER BY created_at DESC LIMIT 1",
        (vault_id, kind, payload_json),
    ).fetchone()
    if failed:
        db.execute(
            "UPDATE jobs SET state='queued',attempts=0,not_before=0,lease_owner=NULL,lease_expires_at=NULL,updated_at=?,error=NULL WHERE id=?",
            (stamp, failed["id"]),
        )
        return failed["id"]
    db.execute(
        """INSERT INTO jobs(id,vault_id,kind,payload_json,state,priority,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (job_id, vault_id, kind, payload_json, "queued", priority, stamp, stamp),
    )
    return job_id


def _enqueue_missing_embedding_jobs(db, vault_id: str, version_id: str | None = None) -> dict[str, int]:
    vault = db.execute(
        "SELECT provider_ciphertext,monthly_budget_microusd,embedding_model,visual_model FROM vaults WHERE id=?",
        (vault_id,),
    ).fetchone()
    queued = {"text": 0, "visual": 0}
    if not vault or not vault["provider_ciphertext"] or vault["monthly_budget_microusd"] <= 0:
        return queued
    version_clause = " AND v.id=?" if version_id else ""
    params: list[Any] = [vault_id]
    if version_id:
        params.append(version_id)
    rows = db.execute(
        f"""SELECT v.id,
                   EXISTS(SELECT 1 FROM chunks c WHERE c.vault_id=v.vault_id AND c.version_id=v.id AND c.kind<>'visual' AND c.text<>'') has_text,
                   EXISTS(SELECT 1 FROM previews p WHERE p.vault_id=v.vault_id AND p.version_id=v.id) has_visual
            FROM versions v JOIN documents d ON d.vault_id=v.vault_id AND d.current_version_id=v.id
            WHERE v.vault_id=? AND d.deleted_at IS NULL AND v.lexical_indexed_at IS NOT NULL{version_clause}""",
        params,
    ).fetchall()
    for row in rows:
        readiness = db.execute(
            "SELECT text_indexed_at,embedding_model,visual_indexed_at,visual_model FROM versions WHERE vault_id=? AND id=?",
            (vault_id, row["id"]),
        ).fetchone()
        if row["has_text"] and (not readiness["text_indexed_at"] or readiness["embedding_model"] != vault["embedding_model"]):
            create_job(db, vault_id, "embed_text", {"version_id": row["id"], "model": vault["embedding_model"]}, priority=80)
            queued["text"] += 1
        if row["has_visual"] and (not readiness["visual_indexed_at"] or readiness["visual_model"] != vault["visual_model"]):
            create_job(db, vault_id, "embed_visual", {"version_id": row["id"], "model": vault["visual_model"]}, priority=85)
            queued["visual"] += 1
    return queued


def enqueue_missing_embedding_jobs(vault_id: str, version_id: str | None = None) -> dict[str, int]:
    with connect() as db, transaction(db, immediate=True):
        return _enqueue_missing_embedding_jobs(db, vault_id, version_id)


def register_upload(
    vault_id: str,
    source_id: str,
    relative_path: str,
    display_name: str,
    content_sha256: str,
    mtime_ns: int,
    size_bytes: int,
    staging_path: Path,
    media_type: str | None = None,
    scan_generation: int = 0,
    document_id: str | None = None,
) -> tuple[str, str, str]:
    suffix = Path(display_name).suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {suffix or '(none)'}")
    document_id = document_id or stable_document_id(source_id, relative_path)
    if not re.fullmatch(r"doc_[A-Za-z0-9_-]{20,80}", document_id):
        raise ValueError("Invalid stable document ID")
    version_id = stable_version_id(document_id, content_sha256, mtime_ns)
    media_type = media_type or mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    stamp = now()
    with connect() as db, transaction(db, immediate=True):
        source = db.execute(
            "SELECT 1 FROM sources WHERE vault_id=? AND id=?", (vault_id, source_id)
        ).fetchone()
        if not source:
            raise ValueError("Unknown source for this vault")
        owner = db.execute(
            "SELECT source_id FROM documents WHERE vault_id=? AND id=?", (vault_id, document_id)
        ).fetchone()
        if owner and owner["source_id"] != source_id:
            raise ValueError("Stable document ID belongs to a different source")
        existing = db.execute(
            "SELECT current_version_id FROM documents WHERE vault_id=? AND id=?",
            (vault_id, document_id),
        ).fetchone()
        db.execute(
            """INSERT INTO documents(
                 id,vault_id,source_id,relative_path,display_name,media_type,size_bytes,
                 revision_mtime_ns,content_sha256,current_version_id,state,availability,
                 last_seen_generation,last_seen_at,missing_confirmations,deleted_at,error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)
               ON CONFLICT(vault_id,id) DO UPDATE SET
                 relative_path=excluded.relative_path, display_name=excluded.display_name,
                 media_type=excluded.media_type, size_bytes=excluded.size_bytes,
                 revision_mtime_ns=excluded.revision_mtime_ns, content_sha256=excluded.content_sha256,
                 availability='source_online', last_seen_generation=excluded.last_seen_generation,
                 last_seen_at=excluded.last_seen_at, missing_confirmations=0, deleted_at=NULL""",
            (
                document_id, vault_id, source_id, relative_path, display_name, media_type,
                size_bytes, mtime_ns, content_sha256, existing["current_version_id"] if existing else None,
                "queued", "source_online", scan_generation, stamp,
            ),
        )
        version = db.execute(
            "SELECT indexed_at,visual_recovery_needed FROM versions WHERE vault_id=? AND id=?", (vault_id, version_id)
        ).fetchone()
        if version and version["indexed_at"]:
            if version["visual_recovery_needed"]:
                expiry = stamp + settings().staging_ttl_hours * 3600
                db.execute(
                    "UPDATE versions SET raw_staging_path=?,raw_expires_at=? WHERE vault_id=? AND id=?",
                    (str(staging_path), expiry, vault_id, version_id),
                )
                job_id = create_job(db, vault_id, "render_visual", {"document_id": document_id, "version_id": version_id}, priority=70)
                return document_id, version_id, job_id
            db.execute(
                "UPDATE documents SET current_version_id=?, state='indexed' WHERE vault_id=? AND id=?",
                (version_id, vault_id, document_id),
            )
            create_job(
                db,
                vault_id,
                "mark_historical",
                {"document_id": document_id, "current_version_id": version_id},
                priority=30,
            )
            _enqueue_missing_embedding_jobs(db, vault_id, version_id)
            return document_id, version_id, "unchanged"
        expiry = stamp + settings().staging_ttl_hours * 3600
        db.execute(
            """INSERT INTO versions(
                 id,vault_id,document_id,content_sha256,source_mtime_ns,extraction_version,
                 created_at,raw_staging_path,raw_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vault_id,id) DO UPDATE SET
                 raw_staging_path=excluded.raw_staging_path, raw_expires_at=excluded.raw_expires_at""",
            (
                version_id, vault_id, document_id, content_sha256, mtime_ns,
                EXTRACTION_VERSION, stamp, str(staging_path), expiry,
            ),
        )
        job_id = create_job(db, vault_id, "extract", {"document_id": document_id, "version_id": version_id})
        db.execute("UPDATE documents SET state='queued', error=NULL WHERE vault_id=? AND id=?", (vault_id, document_id))
    return document_id, version_id, job_id


def complete_reconcile(vault_id: str, source_id: str, generation: int, complete: bool) -> dict[str, int]:
    stamp = now()
    deleted = 0
    derived_paths: list[Path] = []
    derived_directories: list[Path] = []
    with connect() as db, transaction(db, immediate=True):
        db.execute(
            """UPDATE sources SET online=?,last_seen_at=?,last_successful_sync_at=CASE WHEN ? THEN ? ELSE last_successful_sync_at END,
               scan_generation=MAX(scan_generation,?) WHERE vault_id=? AND id=?""",
            (1 if complete else 0, stamp, 1 if complete else 0, stamp, generation, vault_id, source_id),
        )
        if not complete:
            db.execute(
                "UPDATE documents SET availability='source_offline' WHERE vault_id=? AND source_id=? AND deleted_at IS NULL",
                (vault_id, source_id),
            )
            return {"confirmed_deleted": 0}
        db.execute(
            """UPDATE documents SET missing_confirmations=missing_confirmations+1
               WHERE vault_id=? AND source_id=? AND deleted_at IS NULL AND last_seen_generation<?""",
            (vault_id, source_id, generation),
        )
        rows = db.execute(
            """SELECT id,current_version_id FROM documents WHERE vault_id=? AND source_id=? AND deleted_at IS NULL
               AND missing_confirmations>=?""",
            (vault_id, source_id, settings().reconcile_delete_confirmations),
        ).fetchall()
        for row in rows:
            version_ids = [
                item["id"]
                for item in db.execute(
                    "SELECT id FROM versions WHERE vault_id=? AND document_id=?",
                    (vault_id, row["id"]),
                ).fetchall()
            ]
            derived_directories.extend(settings().preview_dir / vault_id / version_id for version_id in version_ids)
            derived_paths.extend(
                Path(item["path"])
                for item in db.execute(
                    "SELECT path FROM previews WHERE vault_id=? AND document_id=?",
                    (vault_id, row["id"]),
                ).fetchall()
            )
            derived_paths.extend(
                Path(item["raw_staging_path"])
                for item in db.execute(
                    "SELECT raw_staging_path FROM versions WHERE vault_id=? AND document_id=? AND raw_staging_path IS NOT NULL",
                    (vault_id, row["id"]),
                ).fetchall()
            )
            db.execute(
                "UPDATE documents SET state='deleted',availability='deleted',deleted_at=? WHERE vault_id=? AND id=?",
                (stamp, vault_id, row["id"]),
            )
            db.execute("DELETE FROM chunks_fts WHERE vault_id=? AND document_id=?", (vault_id, row["id"]))
            db.execute("DELETE FROM versions WHERE vault_id=? AND document_id=?", (vault_id, row["id"]))
            create_job(db, vault_id, "delete_vectors", {"document_id": row["id"]}, priority=20)
            deleted += 1
        db.execute(
            """UPDATE documents SET availability='source_online' WHERE vault_id=? AND source_id=?
               AND deleted_at IS NULL AND last_seen_generation=?""",
            (vault_id, source_id, generation),
        )
    for path in derived_paths:
        path.unlink(missing_ok=True)
    for directory in derived_directories:
        shutil.rmtree(directory, ignore_errors=True)
    try:
        (settings().preview_dir / vault_id).rmdir()
    except OSError:
        pass
    return {"confirmed_deleted": deleted}


def storage_usage(vault_id: str) -> dict[str, int]:
    with connect() as db:
        row = db.execute(
            """SELECT COALESCE(SUM(text_bytes),0) text_bytes,COALESCE(SUM(preview_bytes),0) preview_bytes
               FROM versions WHERE vault_id=?""", (vault_id,)
        ).fetchone()
        staging = db.execute(
            """SELECT COALESCE(SUM(d.size_bytes),0) bytes FROM versions v JOIN documents d
               ON d.vault_id=v.vault_id AND d.id=v.document_id
               WHERE v.vault_id=? AND v.raw_staging_path IS NOT NULL""", (vault_id,)
        ).fetchone()["bytes"]
        quota = db.execute("SELECT quota_bytes,min_free_bytes,paused FROM vaults WHERE id=?", (vault_id,)).fetchone()
    disk = shutil.disk_usage(settings().data_dir)
    total = row["text_bytes"] + row["preview_bytes"] + staging
    return {
        "derived_text_bytes": row["text_bytes"],
        "preview_bytes": row["preview_bytes"],
        "temporary_staging_bytes": staging,
        "accounted_bytes": total,
        "quota_bytes": quota["quota_bytes"],
        "free_bytes": disk.free,
        "minimum_free_bytes": quota["min_free_bytes"],
        "paused": bool(quota["paused"]),
    }


def cleanup_expired() -> dict[str, int]:
    stamp = now()
    removed = 0
    with connect() as db, transaction(db, immediate=True):
        rows = db.execute(
            "SELECT vault_id,id,raw_staging_path FROM versions WHERE raw_staging_path IS NOT NULL AND raw_expires_at<?",
            (stamp,),
        ).fetchall()
        artifacts = db.execute(
            "SELECT id,staging_path FROM pending_artifacts WHERE state IN ('pending','failed') AND expires_at<?",
            (stamp,),
        ).fetchall()
        inspections = db.execute(
            "SELECT id,staging_path FROM temporary_inspections WHERE expires_at<?", (stamp,)
        ).fetchall()
        previews = db.execute(
            "SELECT vault_id,id,version_id,path FROM previews WHERE created_at<?",
            (stamp - settings().preview_ttl_days * 86400,),
        ).fetchall()
        for row in [*rows, *artifacts]:
            path = row["raw_staging_path"] if "raw_staging_path" in row.keys() else row["staging_path"]
            if path:
                Path(path).unlink(missing_ok=True)
                removed += 1
        for row in inspections:
            if row["staging_path"]:
                Path(row["staging_path"]).unlink(missing_ok=True)
                removed += 1
        db.execute("UPDATE versions SET raw_staging_path=NULL WHERE raw_staging_path IS NOT NULL AND raw_expires_at<?", (stamp,))
        db.execute("UPDATE pending_artifacts SET state='expired',staging_path=NULL WHERE state IN ('pending','failed') AND expires_at<?", (stamp,))
        db.execute("DELETE FROM temporary_inspections WHERE expires_at<?", (stamp,))
        for row in previews:
            Path(row["path"]).unlink(missing_ok=True)
            removed += 1
        expired_versions = {(row["vault_id"], row["version_id"]) for row in previews}
        db.execute("DELETE FROM previews WHERE created_at<?", (stamp - settings().preview_ttl_days * 86400,))
        for vault_id, version_id in expired_versions:
            db.execute(
                "DELETE FROM embedding_batches WHERE vault_id=? AND version_id=? AND modality='visual'",
                (vault_id, version_id),
            )
            db.execute(
                """UPDATE versions SET preview_bytes=0,visual_indexed_at=NULL,visual_model=NULL,visual_recovery_needed=1
                   WHERE vault_id=? AND id=?""",
                (vault_id, version_id),
            )
            create_job(db, vault_id, "delete_version_vectors", {"version_id": version_id, "modality": "visual"}, priority=20)
    return {"files_removed": removed}
