from __future__ import annotations

import ipaddress
import json
import mimetypes
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Image, MCPServer
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, ConfigDict

from .catalog import SUPPORTED_EXTENSIONS, create_job, storage_usage
from .config import settings
from .db import connect, now
from .extraction import extract_document
from .retrieval import calculate_table, fetch_chunk, search
from .security import authenticate_token, new_token, token_hash


class VaultTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = authenticate_token(token)
        except Exception:
            return None
        return AccessToken(
            token=token,
            client_id=principal.token_id,
            scopes=list(principal.scopes),
            expires_at=None,
            resource=settings().remote_base_url.rstrip("/") + "/mcp",
            subject=principal.vault_id,
        )


def current_vault(scope: str = "search") -> str:
    access = get_access_token()
    if not access or not access.subject:
        raise PermissionError("Authenticated vault context is required")
    if scope not in access.scopes and "admin" not in access.scopes:
        raise PermissionError(f"Access token lacks {scope} scope")
    return access.subject


class OpenAIFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


INSTRUCTIONS = """
Personal Vault contains user-controlled evidence. Before making collection-specific claims, search for evidence.
Use fetch_evidence to expand relevant passages and investigate conflicting revisions or sources. Cite the returned
document title, stable document/version ID, and exact page/slide/sheet/row/time locator. State when sources are
offline, the indexed revision differs, visual sampling may be incomplete, or evidence is insufficient. The service
does not see the whole conversation automatically: pass a resolved, self-contained question to search_vault.
Saving is explicit; never tell the user an attachment is permanently saved unless save_chat_artifact returns saved.
""".strip()

mcp = MCPServer(
    "Personal Vault",
    instructions=INSTRUCTIONS,
    token_verifier=VaultTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings().remote_base_url.rstrip("/")),
        resource_server_url=AnyHttpUrl(settings().remote_base_url.rstrip("/") + "/mcp"),
        required_scopes=["search"],
    ),
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False))
def search_vault(question: str, limit: int = 8, source_id: str | None = None, media_type: str | None = None, path_prefix: str | None = None) -> dict[str, Any]:
    """Hybrid search. Pass the resolved question, including relevant conversation context."""
    vault_id = current_vault("search")
    return search(vault_id, question, min(max(limit, 1), 20), {"source_id": source_id, "media_type": media_type, "path_prefix": path_prefix})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False))
def fetch_evidence(chunk_id: str, adjacent_passages: int = 2) -> dict[str, Any]:
    """Expand a search result with adjacent passages from the same indexed revision."""
    return fetch_chunk(current_vault("search"), chunk_id, min(max(adjacent_passages, 0), 5))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False))
def calculate_spreadsheet(document_id: str, sheet_name: str, cell_range: str, operation: str = "sum") -> dict[str, Any]:
    """Calculate sum/average/min/max/count over up to 10,000 stored spreadsheet cells with a range citation."""
    return calculate_table(current_vault("search"), document_id, sheet_name, cell_range, operation)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False))
def inspect_visual(document_id: str, preview_index: int = 0) -> Image:
    """Return one bounded derived page/image/video preview for visual inspection."""
    vault_id = current_vault("search")
    with connect() as db:
        row = db.execute(
            """SELECT p.path FROM previews p JOIN documents d ON d.vault_id=p.vault_id AND d.current_version_id=p.version_id
               WHERE p.vault_id=? AND p.document_id=? AND p.ordinal=? AND d.deleted_at IS NULL""",
            (vault_id, document_id, min(max(preview_index, 0), 119)),
        ).fetchone()
    if not row or not Path(row["path"]).is_file():
        raise KeyError("No retained visual preview exists for that document/index")
    return Image(path=row["path"])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False))
def collection_status() -> dict[str, Any]:
    """Show sources, indexing state, availability, quotas, and provider readiness."""
    vault_id = current_vault("search")
    with connect() as db:
        sources = [dict(row) for row in db.execute("SELECT id,name,platform,root_label,online,last_seen_at,last_successful_sync_at FROM sources WHERE vault_id=?", (vault_id,))]
        states = [dict(row) for row in db.execute("SELECT state,COUNT(*) count FROM documents WHERE vault_id=? GROUP BY state", (vault_id,))]
        vault = dict(db.execute("SELECT embedding_model,visual_model,rerank_model,provider_ciphertext IS NOT NULL provider_configured FROM vaults WHERE id=?", (vault_id,)).fetchone())
    return {"sources": sources, "documents": states, "storage": storage_usage(vault_id), "provider": vault}


def _safe_remote_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Attachment download URL must be HTTPS")
    for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise ValueError("Attachment URL resolves to a non-public address")


def _download_openai_file(file: OpenAIFile) -> Path:
    suffix = Path(file.file_name or "attachment").suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported attachment type: {suffix or '(none)'}")
    target = settings().staging_dir / f"chat-{uuid.uuid4().hex}{suffix}"
    total = 0
    try:
        current_url = file.download_url
        with httpx.Client(follow_redirects=False, timeout=60) as client:
            for _attempt in range(4):
                _safe_remote_url(current_url)
                with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Attachment redirect has no destination")
                        current_url = str(response.url.join(location))
                        continue
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > settings().max_upload_bytes:
                        raise ValueError("Attachment exceeds the configured byte limit")
                    with target.open("xb") as output:
                        for block in response.iter_bytes(1024 * 1024):
                            total += len(block)
                            if total > settings().max_upload_bytes:
                                raise ValueError("Attachment exceeds the configured byte limit")
                            output.write(block)
                    return target
            raise ValueError("Attachment exceeded the redirect limit")
    except Exception:
        target.unlink(missing_ok=True)
        raise


@mcp.tool(
    meta={"openai/fileParams": ["file"]},
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True, destructiveHint=False),
)
def inspect_chat_attachment(file: OpenAIFile) -> dict[str, Any]:
    """Queue bounded temporary extraction of a supplied chat file; this does not save it to the vault."""
    vault_id = current_vault("search")
    path = _download_openai_file(file)
    inspection_id = "inspect_" + uuid.uuid4().hex
    with connect() as db:
        db.execute(
            """INSERT INTO temporary_inspections(id,vault_id,file_id,file_name,staging_path,state,created_at,expires_at)
               VALUES(?,?,?,?,?,'queued',?,?)""",
            (inspection_id, vault_id, file.file_id, file.file_name or path.name, str(path), now(), now() + settings().staging_ttl_hours * 3600),
        )
        create_job(db, vault_id, "inspect_temporary", {"inspection_id": inspection_id}, priority=60)
    return {"inspection_id": inspection_id, "state": "queued", "temporary": True, "saved": False, "message": "Poll get_chat_inspection for the extracted context."}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False))
def get_chat_inspection(inspection_id: str) -> dict[str, Any]:
    """Poll a temporary attachment inspection until it is complete or failed."""
    vault_id = current_vault("search")
    with connect() as db:
        row = db.execute(
            "SELECT id,file_id,file_name,state,extracted_text,warnings_json,error,expires_at FROM temporary_inspections WHERE vault_id=? AND id=?",
            (vault_id, inspection_id),
        ).fetchone()
    if not row:
        raise KeyError("Temporary inspection not found in this vault")
    result = dict(row)
    result["temporary"], result["saved"] = True, False
    result["warnings"] = json.loads(result.pop("warnings_json") or "[]")
    return result


@mcp.tool(
    meta={"openai/fileParams": ["file"]},
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True, destructiveHint=False),
)
def save_chat_artifact(file: OpenAIFile, note: str | None = None) -> dict[str, Any]:
    """Stage a chat file for the Windows connector to write into its configured Saved Artifacts folder."""
    vault_id = current_vault("write")
    path = _download_openai_file(file)
    artifact_id = "artifact_" + uuid.uuid4().hex
    expires = now() + settings().staging_ttl_hours * 3600
    with connect() as db:
        db.execute(
            """INSERT INTO pending_artifacts(id,vault_id,file_name,media_type,staging_path,note_text,provenance_json,state,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (artifact_id, vault_id, file.file_name or path.name, file.mime_type, str(path), note, json.dumps({"client": "chatgpt", "file_id": file.file_id, "received_at": now()}), "pending", now(), expires),
        )
    return {"artifact_id": artifact_id, "state": "pending_source_delivery", "saved": False, "expires_at": expires, "message": "The connector must be online and confirm the original was written before this becomes saved."}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False, destructiveHint=False))
def save_chat_note(title: str, text: str, author_label: str = "user") -> dict[str, Any]:
    """Stage a text note with provenance for explicit delivery to the Saved Artifacts folder."""
    vault_id = current_vault("write")
    if len(text.encode()) > 1024 * 1024:
        raise ValueError("Note exceeds the 1 MiB limit")
    artifact_id = "artifact_" + uuid.uuid4().hex
    expires = now() + settings().staging_ttl_hours * 3600
    with connect() as db:
        db.execute(
            """INSERT INTO pending_artifacts(id,vault_id,file_name,media_type,note_text,provenance_json,state,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (artifact_id, vault_id, f"{title}.md", "text/markdown", text, json.dumps({"author_label": author_label, "created_at": now(), "statement_type": "user_note"}), "pending", now(), expires),
        )
    return {"artifact_id": artifact_id, "state": "pending_source_delivery", "saved": False, "expires_at": expires}
