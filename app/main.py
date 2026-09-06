from __future__ import annotations

import contextlib
import hashlib
from html import escape
import json
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .catalog import SUPPORTED_EXTENSIONS, complete_reconcile, register_upload, storage_usage
from .config import settings
from .db import connect, init_db, now, transaction
from .mcp_server import mcp
from .oauth import router as oauth_router
from .retrieval import calculate_table, fetch_chunk, search
from .security import Principal, check_view_signature, encrypt_secret, new_token, require_principal, token_hash


def allowed_hosts() -> list[str]:
    values = {"127.0.0.1:*", "localhost:*"}
    for value in (settings().public_base_url, settings().remote_base_url):
        parsed = urlparse(value)
        if parsed.netloc:
            values.add(parsed.netloc)
    return sorted(values)


mcp_app = mcp.streamable_http_app(
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts(),
        allowed_origins=[settings().remote_base_url.rstrip("/"), "https://chatgpt.com", "https://chat.openai.com"],
    ),
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    settings().ensure_dirs()
    init_db()
    bootstrap = settings().secret_dir / "bootstrap-code"
    with connect() as db:
        has_vault = db.execute("SELECT 1 FROM vaults LIMIT 1").fetchone()
    if not has_vault and not bootstrap.exists():
        bootstrap.write_text(new_token("setup"))
        bootstrap.chmod(0o600)
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(title="Personal Vault RAG", version="0.1.0", lifespan=lifespan)
app.include_router(oauth_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/api/health")
def health() -> dict:
    with connect() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ok", "version": "0.1.0", "qdrant": settings().qdrant_url}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


class SetupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@app.post("/api/setup")
def setup_vault(body: SetupRequest, x_bootstrap_code: str = Header()) -> dict:
    path = settings().secret_dir / "bootstrap-code"
    if not path.exists() or not secrets.compare_digest(path.read_text().strip(), x_bootstrap_code.strip()):
        raise HTTPException(403, "Invalid bootstrap code")
    vault_id = "vault_" + uuid.uuid4().hex
    access = new_token("vault")
    stamp = now()
    with connect() as db, transaction(db, immediate=True):
        if db.execute("SELECT 1 FROM vaults LIMIT 1").fetchone():
            raise HTTPException(409, "Initial setup is already complete")
        db.execute(
            """INSERT INTO vaults(id,name,created_at,quota_bytes,min_free_bytes) VALUES(?,?,?,?,?)""",
            (vault_id, body.name, stamp, settings().default_vault_quota_bytes, settings().min_free_bytes),
        )
        db.execute(
            """INSERT INTO access_tokens(id,vault_id,token_hash,label,scopes,created_at) VALUES(?,?,?,?,?,?)""",
            ("tok_" + uuid.uuid4().hex, vault_id, token_hash(access), "Initial administrator", "admin search write ingest", stamp),
        )
    path.unlink(missing_ok=True)
    return {"vault_id": vault_id, "access_token": access, "message": "Store this token in a password manager; it is shown once."}


@app.post("/api/vaults")
def create_vault(body: SetupRequest, principal: Principal = Depends(require_principal)) -> dict:
    """Create another isolated vault. The new token is shown exactly once."""
    principal.require("admin")
    vault_id = "vault_" + uuid.uuid4().hex
    access = new_token("vault")
    stamp = now()
    with connect() as db, transaction(db, immediate=True):
        db.execute(
            "INSERT INTO vaults(id,name,created_at,quota_bytes,min_free_bytes) VALUES(?,?,?,?,?)",
            (vault_id, body.name, stamp, settings().default_vault_quota_bytes, settings().min_free_bytes),
        )
        db.execute(
            "INSERT INTO access_tokens(id,vault_id,token_hash,label,scopes,created_at) VALUES(?,?,?,?,?,?)",
            ("tok_" + uuid.uuid4().hex, vault_id, token_hash(access), "Initial administrator", "admin search write ingest", stamp),
        )
    return {"vault_id": vault_id, "access_token": access, "message": "Store this token now; it cannot be recovered."}


class PairRequest(BaseModel):
    expires_minutes: int = Field(default=30, ge=5, le=1440)


@app.post("/api/pairing-codes")
def pairing_code(body: PairRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("ingest")
    code = "-".join([secrets.token_hex(2).upper() for _ in range(3)])
    expires = now() + body.expires_minutes * 60
    with connect() as db:
        db.execute("INSERT INTO pairing_codes(code_hash,vault_id,expires_at) VALUES(?,?,?)", (token_hash(code), principal.vault_id, expires))
    return {"pairing_code": code, "expires_at": expires}


class PairClaim(BaseModel):
    pairing_code: str
    device_name: str = Field(min_length=1, max_length=100)


@app.post("/api/pair/claim")
def claim_pairing(body: PairClaim) -> dict:
    stamp = now()
    with connect() as db, transaction(db, immediate=True):
        row = db.execute("SELECT * FROM pairing_codes WHERE code_hash=?", (token_hash(body.pairing_code.upper()),)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < stamp:
            raise HTTPException(400, "Invalid or expired pairing code")
        db.execute("UPDATE pairing_codes SET used_at=? WHERE code_hash=?", (stamp, token_hash(body.pairing_code.upper())))
        token = new_token("connector")
        db.execute(
            """INSERT INTO access_tokens(id,vault_id,token_hash,label,scopes,created_at) VALUES(?,?,?,?,?,?)""",
            ("tok_" + uuid.uuid4().hex, row["vault_id"], token_hash(token), f"Connector: {body.device_name}", "search write ingest", stamp),
        )
    return {"vault_id": row["vault_id"], "access_token": token}


class SourceRequest(BaseModel):
    source_id: str = Field(pattern=r"^src_[A-Za-z0-9_-]{8,80}$")
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(pattern=r"^(windows|linux)$")
    root_label: str = Field(min_length=1, max_length=300)


@app.post("/api/sources")
def upsert_source(body: SourceRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("ingest")
    with connect() as db:
        db.execute(
            """INSERT INTO sources(id,vault_id,name,platform,root_label,last_seen_at,online) VALUES(?,?,?,?,?,?,1)
               ON CONFLICT(vault_id,id) DO UPDATE SET name=excluded.name,platform=excluded.platform,
               root_label=excluded.root_label,last_seen_at=excluded.last_seen_at,online=1""",
            (body.source_id, principal.vault_id, body.name, body.platform, body.root_label, now()),
        )
    return {"source_id": body.source_id, "state": "registered"}


def _check_capacity(vault_id: str, incoming: int) -> None:
    usage = storage_usage(vault_id)
    if usage["paused"]:
        raise HTTPException(423, "Indexing is paused for this vault")
    if usage["accounted_bytes"] + incoming > usage["quota_bytes"]:
        raise HTTPException(507, "Vault derived-data quota would be exceeded")
    if usage["free_bytes"] - incoming < usage["minimum_free_bytes"]:
        with connect() as db:
            db.execute("UPDATE vaults SET paused=1 WHERE id=?", (vault_id,))
        raise HTTPException(507, "Indexing paused to preserve the configured free-space reserve")


@app.post("/api/ingest")
async def ingest(
    source_id: str = Form(), relative_path: str = Form(), display_name: str = Form(),
    content_sha256: str = Form(pattern=r"^[0-9a-f]{64}$"), mtime_ns: int = Form(),
    size_bytes: int = Form(ge=0), scan_generation: int = Form(ge=0),
    media_type: str | None = Form(default=None), file: UploadFile = File(),
    document_id: str | None = Form(default=None),
    principal: Principal = Depends(require_principal),
) -> dict:
    principal.require("ingest")
    if Path(display_name).suffix.casefold() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(415, "Unsupported file type")
    if size_bytes > settings().max_upload_bytes:
        raise HTTPException(413, "File exceeds configured upload limit")
    _check_capacity(principal.vault_id, size_bytes)
    target = settings().staging_dir / f"upload-{uuid.uuid4().hex}{Path(display_name).suffix.casefold()}"
    digest, received = hashlib.sha256(), 0
    try:
        with target.open("xb") as output:
            while block := await file.read(1024 * 1024):
                received += len(block)
                if received > settings().max_upload_bytes or received > size_bytes + 1:
                    raise HTTPException(413, "Upload exceeded declared or configured size")
                digest.update(block)
                output.write(block)
        if received != size_bytes or digest.hexdigest() != content_sha256:
            raise HTTPException(422, "Uploaded bytes do not match declared size/fingerprint")
        document_id, version_id, state = register_upload(
            principal.vault_id, source_id, relative_path, display_name, content_sha256,
            mtime_ns, size_bytes, target, media_type, scan_generation, document_id,
        )
        if state == "unchanged":
            target.unlink(missing_ok=True)
        return {"document_id": document_id, "version_id": version_id, "state": state}
    except Exception:
        target.unlink(missing_ok=True)
        raise


class SeenRequest(BaseModel):
    source_id: str
    document_id: str = Field(pattern=r"^doc_[A-Za-z0-9_-]{20,80}$")
    relative_path: str
    display_name: str
    mtime_ns: int
    size_bytes: int = Field(ge=0)
    scan_generation: int = Field(ge=0)


@app.post("/api/seen")
def seen(body: SeenRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("ingest")
    with connect() as db, transaction(db, immediate=True):
        row = db.execute(
            "SELECT source_id FROM documents WHERE vault_id=? AND id=?", (principal.vault_id, body.document_id)
        ).fetchone()
        if not row or row["source_id"] != body.source_id:
            raise HTTPException(404, "Document is not registered to this source")
        db.execute(
            """UPDATE documents SET relative_path=?,display_name=?,revision_mtime_ns=?,size_bytes=?,
               last_seen_generation=?,last_seen_at=?,missing_confirmations=0,availability='source_online'
               WHERE vault_id=? AND id=?""",
            (body.relative_path, body.display_name, body.mtime_ns, body.size_bytes, body.scan_generation, now(), principal.vault_id, body.document_id),
        )
    return {"document_id": body.document_id, "state": "seen"}


class ReconcileRequest(BaseModel):
    source_id: str
    generation: int = Field(ge=0)
    complete: bool
    unavailable_reason: str | None = None


@app.post("/api/reconcile")
def reconcile(body: ReconcileRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("ingest")
    return complete_reconcile(principal.vault_id, body.source_id, body.generation, body.complete)


class ProviderRequest(BaseModel):
    voyage_api_key: str | None = Field(default=None, min_length=10)
    monthly_budget_usd: float = Field(ge=0, le=10000)


@app.put("/api/settings/provider")
def provider_config(body: ProviderRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("admin")
    encrypted = encrypt_secret(body.voyage_api_key) if body.voyage_api_key else None
    with connect() as db:
        if encrypted:
            db.execute("UPDATE vaults SET provider_ciphertext=?,monthly_budget_microusd=? WHERE id=?", (encrypted, int(body.monthly_budget_usd * 1_000_000), principal.vault_id))
        else:
            db.execute("UPDATE vaults SET monthly_budget_microusd=? WHERE id=?", (int(body.monthly_budget_usd * 1_000_000), principal.vault_id))
    return {"configured": encrypted is not None, "monthly_budget_usd": body.monthly_budget_usd, "note": "The allowance limits this app only."}


@app.get("/api/status")
def status(principal: Principal = Depends(require_principal)) -> dict:
    with connect() as db:
        vault = dict(db.execute("SELECT id,name,quota_bytes,paused,monthly_budget_microusd,provider_ciphertext IS NOT NULL provider_configured,embedding_model,visual_model,rerank_model FROM vaults WHERE id=?", (principal.vault_id,)).fetchone())
        vault["monthly_budget_usd"] = vault.pop("monthly_budget_microusd") / 1_000_000
        sources = [dict(row) for row in db.execute("SELECT * FROM sources WHERE vault_id=?", (principal.vault_id,))]
        jobs = [dict(row) for row in db.execute("SELECT state,kind,COUNT(*) count FROM jobs WHERE vault_id=? GROUP BY state,kind", (principal.vault_id,))]
        documents = [dict(row) for row in db.execute("SELECT state,COUNT(*) count FROM documents WHERE vault_id=? GROUP BY state", (principal.vault_id,))]
    return {"vault": vault, "sources": sources, "jobs": jobs, "documents": documents, "storage": storage_usage(principal.vault_id)}


class SearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=20)
    source_id: str | None = None
    media_type: str | None = None
    path_prefix: str | None = None


@app.post("/api/search")
def api_search(body: SearchRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("search")
    return search(principal.vault_id, body.question, body.limit, body.model_dump(exclude={"question", "limit"}, exclude_none=True))


@app.get("/api/chunks/{chunk_id}")
def api_fetch(chunk_id: str, adjacent: int = 2, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("search")
    try:
        return fetch_chunk(principal.vault_id, chunk_id, min(max(adjacent, 0), 5))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/documents/{document_id}/previews/{preview_index}")
def api_visual_preview(document_id: str, preview_index: int, principal: Principal = Depends(require_principal)):
    principal.require("search")
    with connect() as db:
        row = db.execute(
            """SELECT p.path,p.media_type FROM previews p JOIN documents d
               ON d.vault_id=p.vault_id AND d.id=p.document_id AND d.current_version_id=p.version_id
               WHERE p.vault_id=? AND p.document_id=? AND p.ordinal=? AND d.deleted_at IS NULL""",
            (principal.vault_id, document_id, min(max(preview_index, 0), 119)),
        ).fetchone()
    if not row or not Path(row["path"]).is_file():
        raise HTTPException(404, "Visual preview is unavailable or expired")
    return FileResponse(row["path"], media_type=row["media_type"], headers={"Cache-Control": "private, no-store"})


class TableCalculationRequest(BaseModel):
    document_id: str
    sheet_name: str
    cell_range: str = Field(pattern=r"^[A-Za-z]{1,3}[1-9][0-9]*:[A-Za-z]{1,3}[1-9][0-9]*$")
    operation: str = Field(default="sum", pattern=r"^(sum|average|min|max|count)$")


@app.post("/api/table/calculate")
def api_table_calculate(body: TableCalculationRequest, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("search")
    try:
        return calculate_table(principal.vault_id, body.document_id, body.sheet_name, body.cell_range, body.operation)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/connector/artifacts")
def pending_artifacts(principal: Principal = Depends(require_principal)) -> dict:
    principal.require("ingest")
    with connect() as db:
        rows = db.execute("SELECT id,file_name,media_type,note_text,provenance_json,created_at,expires_at FROM pending_artifacts WHERE vault_id=? AND state='pending' AND expires_at>? ORDER BY created_at LIMIT 20", (principal.vault_id, now())).fetchall()
    return {"artifacts": [{**dict(row), "provenance": json.loads(row["provenance_json"]), "has_file": row["note_text"] is None} for row in rows]}


@app.get("/api/connector/artifacts/{artifact_id}/content")
def artifact_content(artifact_id: str, principal: Principal = Depends(require_principal)):
    principal.require("ingest")
    with connect() as db:
        row = db.execute("SELECT * FROM pending_artifacts WHERE vault_id=? AND id=? AND state='pending' AND expires_at>?", (principal.vault_id, artifact_id, now())).fetchone()
    if not row:
        raise HTTPException(404, "Pending artifact not found")
    if row["note_text"] is not None:
        return JSONResponse({"text": row["note_text"]})
    return FileResponse(row["staging_path"], filename=row["file_name"], media_type=row["media_type"])


class ArtifactComplete(BaseModel):
    success: bool
    saved_relative_path: str | None = None
    error: str | None = None


@app.post("/api/connector/artifacts/{artifact_id}/complete")
def artifact_complete(artifact_id: str, body: ArtifactComplete, principal: Principal = Depends(require_principal)) -> dict:
    principal.require("ingest")
    with connect() as db, transaction(db, immediate=True):
        row = db.execute("SELECT * FROM pending_artifacts WHERE vault_id=? AND id=? AND state='pending'", (principal.vault_id, artifact_id)).fetchone()
        if not row:
            raise HTTPException(404, "Pending artifact not found")
        if body.success and not body.saved_relative_path:
            raise HTTPException(422, "saved_relative_path is required on success")
        db.execute("UPDATE pending_artifacts SET state=?,completed_at=?,error=? WHERE vault_id=? AND id=?", ("saved" if body.success else "failed", now(), body.error, principal.vault_id, artifact_id))
    if row["staging_path"]:
        Path(row["staging_path"]).unlink(missing_ok=True)
    return {"artifact_id": artifact_id, "state": "saved" if body.success else "failed", "saved": body.success, "saved_relative_path": body.saved_relative_path}


@app.get("/view/{document_id}", response_class=HTMLResponse)
def view_document(document_id: str, vault: str, version: str, expires: int, signature: str) -> HTMLResponse:
    if not check_view_signature(vault, document_id, version, expires, signature):
        raise HTTPException(403, "Invalid or expired viewer link")
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE vault_id=? AND id=? AND deleted_at IS NULL", (vault, document_id)).fetchone()
        chunks = db.execute("SELECT ordinal,heading_path,locator_json,text FROM chunks WHERE vault_id=? AND document_id=? AND version_id=? ORDER BY ordinal", (vault, document_id, version)).fetchall()
        previews = db.execute("SELECT id,ordinal FROM previews WHERE vault_id=? AND document_id=? AND version_id=? ORDER BY ordinal", (vault, document_id, version)).fetchall()
    if not document or not chunks:
        raise HTTPException(404, "Indexed revision not found")
    body = "".join(f"<article><h3>{escape(item['heading_path'] or 'Passage')} — {escape(item['locator_json'])}</h3><pre>{escape(item['text'])}</pre></article>" for item in chunks)
    preview_html = "".join(f'<figure><img loading="lazy" style="max-width:100%" src="{settings().public_base_url}/preview/{escape(item["id"])}?vault={escape(vault)}&document={escape(document_id)}&version={escape(version)}&expires={expires}&signature={escape(signature)}"><figcaption>Derived visual preview {item["ordinal"] + 1}</figcaption></figure>' for item in previews)
    warning = "<p class=warning>This is an older indexed revision, not the source's current revision.</p>" if document["current_version_id"] != version else ""
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{escape(document['display_name'])}</title><style>body{{font:16px system-ui;max-width:70rem;margin:auto;padding:2rem}}pre{{white-space:pre-wrap}}.warning{{background:#fff3cd;padding:1rem}}</style><h1>{escape(document['display_name'])}</h1><p>{escape(document['relative_path'])} · availability: {escape(document['availability'])} · version: {escape(version)}</p>{warning}{preview_html}{body}", headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})


@app.get("/preview/{preview_id}")
def visual_preview(preview_id: str, vault: str, document: str, version: str, expires: int, signature: str):
    if not check_view_signature(vault, document, version, expires, signature):
        raise HTTPException(403, "Invalid or expired preview link")
    with connect() as db:
        row = db.execute(
            "SELECT path,media_type FROM previews WHERE vault_id=? AND id=? AND document_id=? AND version_id=?",
            (vault, preview_id, document, version),
        ).fetchone()
    if not row or not Path(row["path"]).is_file():
        raise HTTPException(404, "Visual preview is unavailable or expired")
    return FileResponse(row["path"], media_type=row["media_type"], headers={"Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex, nofollow"})


app.mount("/", mcp_app)
